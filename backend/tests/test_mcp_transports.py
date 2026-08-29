from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx
import pytest

import backend.commercial_gateway as commercial_gateway
from backend.commercial_gateway import (
    GatewayDenied,
    McpRegistry,
    McpStdioTransport,
    McpStreamableHttpTransport,
    ToolExecutionRequest,
    ToolGateway,
)
from backend.groups import GroupRepository
from backend.task_runtime.contracts import McpServerConfig, RiskLevel, ToolDescriptor
from backend.task_runtime.service import TaskRuntimeService


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat().replace(
        "+00:00", "Z"
    )


@pytest.mark.asyncio
async def test_streamable_http_transport_never_discovers_prompts_and_invokes_json_rpc() -> None:
    calls: list[tuple[str, Mapping[str, Any]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        method = str(payload["method"])
        calls.append((method, payload.get("params") or {}))
        assert request.url == httpx.URL("https://mcp.example/rpc")
        if method != "initialize":
            assert request.headers["Mcp-Session-Id"] == "session-opaque"
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "test", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{
                "name": "lookup", "description": "Lookup a course",
                "inputSchema": {"type": "object", "additionalProperties": False},
            }]}
        elif method == "resources/list":
            result = {"resources": [{"uri": "course://catalog", "name": "Catalog"}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "ok"}]}
        else:
            raise AssertionError(f"unexpected MCP method: {method}")
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
            headers={"Mcp-Session-Id": "session-opaque"},
        )

    config = McpServerConfig(
        server_id="mcp_" + "a" * 32,
        name="remote",
        transport="streamable_http",
        url="https://mcp.example/rpc",
        allowed_hosts=("mcp.example",),
        allowed_tools=("lookup",),
        allowed_resources=("course://catalog",),
    )
    policy = McpRegistry()
    policy.register(config)
    transport = McpStreamableHttpTransport(
        config,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    accepted = await policy.handshake(config.server_id, transport)
    assert accepted["tools"] == ("lookup",)
    assert accepted["resources"] == ("course://catalog",)
    assert "prompts/list" not in [name for name, _ in calls]
    assert await transport.invoke("lookup", {"course": "math"}) == {
        "content": [{"type": "text", "text": "ok"}]
    }
    assert calls[-1] == ("tools/call", {"name": "lookup", "arguments": {"course": "math"}})
    await transport.close()


@pytest.mark.asyncio
async def test_streamable_http_transport_rejects_redirect_without_following() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://evil.example/steal"})

    config = McpServerConfig(
        server_id="mcp_" + "b" * 32,
        name="remote",
        transport="streamable_http",
        url="https://mcp.example/rpc",
        allowed_hosts=("mcp.example",),
    )
    transport = McpStreamableHttpTransport(
        config,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GatewayDenied, match="redirect"):
        await transport.initialize()
    assert seen == ["https://mcp.example/rpc"]
    await transport.close()


@pytest.mark.asyncio
async def test_streamable_http_transport_rejects_oversized_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (1_048_576 + 1))

    config = McpServerConfig(
        server_id="mcp_" + "e" * 32, name="remote", transport="streamable_http",
        url="https://mcp.example/rpc", allowed_hosts=("mcp.example",),
    )
    transport = McpStreamableHttpTransport(
        config,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GatewayDenied, match="size"):
        await transport.initialize()
    await transport.close()


@pytest.mark.asyncio
async def test_streamable_http_transport_rejects_invalid_session_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
            headers={"Mcp-Session-Id": "session id with spaces"},
        )

    config = McpServerConfig(
        server_id="mcp_" + "9" * 32, name="remote", transport="streamable_http",
        url="https://mcp.example/rpc", allowed_hosts=("mcp.example",),
    )
    transport = McpStreamableHttpTransport(
        config,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GatewayDenied, match="session id"):
        await transport.initialize()
    await transport.close()


@pytest.mark.asyncio
async def test_streamable_http_transport_requires_mcp_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            content=__import__("json").dumps({
                "jsonrpc": "2.0", "id": payload["id"], "result": {},
            }).encode(),
            headers={"Content-Type": "text/plain"},
        )

    config = McpServerConfig(
        server_id="mcp_" + "8" * 32, name="remote", transport="streamable_http",
        url="https://mcp.example/rpc", allowed_hosts=("mcp.example",),
    )
    transport = McpStreamableHttpTransport(
        config,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GatewayDenied, match="content type|framing"):
        await transport.initialize()
    await transport.close()


@pytest.mark.asyncio
async def test_handshake_rejects_server_advertised_prompts_before_discovery() -> None:
    class AdvertisesPrompts:
        discovered = False

        async def initialize(self) -> Mapping[str, Any]:
            return {
                "protocolVersion": "2025-06-18",
                "capabilities": {"prompts": {"listChanged": True}},
            }

        async def discover(self) -> Mapping[str, Any]:
            self.discovered = True
            return {"tools": [], "resources": []}

        async def invoke(self, _name: str, _arguments: Mapping[str, Any]) -> Any:
            raise AssertionError

    config = McpServerConfig(
        server_id="mcp_" + "f" * 32, name="local", transport="stdio",
        command=sys.executable,
    )
    policy = McpRegistry()
    policy.register(config)
    transport = AdvertisesPrompts()
    with pytest.raises(GatewayDenied, match="prompt"):
        await policy.handshake(config.server_id, transport)
    assert transport.discovered is False


@pytest.mark.asyncio
async def test_registry_binds_http_effect_to_the_registered_endpoint_host() -> None:
    """An allowlisted sibling host cannot replace the inspected MCP endpoint."""

    class Transport:
        calls = 0

        async def invoke(self, _name: str, _arguments: Mapping[str, Any]) -> Any:
            self.calls += 1
            return {"content": []}

    class Gateway:
        @staticmethod
        def descriptor(_tool_name: str) -> ToolDescriptor:
            return ToolDescriptor(
                tool_name="mcp_lookup", kind="mcp", risk=RiskLevel.HIGH,
                network_hosts=("mcp.example", "other.example"),
            )

        @staticmethod
        async def invoke_with_effect(
            request: ToolExecutionRequest, effect: Any,
        ) -> Any:
            return await effect(request)

    config = McpServerConfig(
        server_id="mcp_" + "7" * 32, name="remote", transport="streamable_http",
        url="https://mcp.example/rpc",
        allowed_hosts=("mcp.example", "other.example"),
        allowed_tools=("lookup",),
    )
    policy = McpRegistry()
    policy.register(config)
    policy.accept_discovery(config.server_id, {
        "tools": [{"name": "lookup", "inputSchema": {"type": "object"}}],
        "resources": [],
    })
    request = ToolExecutionRequest(
        group_id="group_" + "1" * 32, task_id="task_" + "2" * 32,
        run_id="run_" + "3" * 32, tool_name="mcp_lookup",
        idempotency_key="host-swap", purpose="mcp_call",
        network_hosts=("other.example",), arguments={"course": "math"},
    )
    transport = Transport()

    with pytest.raises(GatewayDenied, match="endpoint host"):
        await policy.invoke(
            config.server_id, "lookup", {"course": "math"},
            transport=transport, gateway=Gateway(), request=request,  # type: ignore[arg-type]
        )
    assert transport.calls == 0


@pytest.mark.asyncio
async def test_stdio_json_rpc_framing_is_one_bounded_newline_message() -> None:
    class Input:
        def __init__(self) -> None:
            self.value = b""

        def write(self, value: bytes) -> None:
            self.value += value

        async def drain(self) -> None:
            return None

    class Output:
        async def readline(self) -> bytes:
            return b'{"jsonrpc":"2.0","id":7,"result":{}}\n'

    class Process:
        stdin = Input()
        stdout = Output()
        returncode = None

    process = Process()
    session = commercial_gateway._JsonLineMcpSession(process)  # type: ignore[arg-type]
    response = await session.request({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert response["id"] == 7
    assert process.stdin.value.endswith(b"\n")
    assert process.stdin.value.count(b"\n") == 1
    assert len(process.stdin.value) <= 1_048_576


@pytest.mark.asyncio
async def test_stdio_json_rpc_rejects_oversized_stdout_message() -> None:
    class Input:
        def write(self, _value: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

    class Output:
        async def readline(self) -> bytes:
            return b"x" * (1_048_576 + 1)

    class Process:
        stdin = Input()
        stdout = Output()
        returncode = None

    session = commercial_gateway._JsonLineMcpSession(Process())  # type: ignore[arg-type]
    with pytest.raises(GatewayDenied, match="oversized"):
        await session.request({"jsonrpc": "2.0", "id": 8, "method": "tools/list"})


@pytest.mark.asyncio
async def test_stdio_json_rpc_requires_newline_framing() -> None:
    class Input:
        def write(self, _value: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

    class Output:
        async def readline(self) -> bytes:
            return b'{"jsonrpc":"2.0","id":10,"result":{}}'

    class Process:
        stdin = Input()
        stdout = Output()
        returncode = None

    session = commercial_gateway._JsonLineMcpSession(Process())  # type: ignore[arg-type]
    with pytest.raises(GatewayDenied, match="framing|newline"):
        await session.request({"jsonrpc": "2.0", "id": 10, "method": "tools/list"})


@pytest.mark.asyncio
async def test_stdio_json_rpc_has_a_fixed_response_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class Input:
        def write(self, _value: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

    class Output:
        async def readline(self) -> bytes:
            return b""

    class Process:
        stdin = Input()
        stdout = Output()
        returncode = None

    observed: list[float] = []

    async def timeout(awaitable: Any, *, timeout: float) -> Any:
        observed.append(timeout)
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(commercial_gateway.asyncio, "wait_for", timeout)
    session = commercial_gateway._JsonLineMcpSession(Process())  # type: ignore[arg-type]
    with pytest.raises(GatewayDenied, match="timed out"):
        await session.request({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
    assert observed == [30.0]


class _FakeStdioSession:
    def __init__(self) -> None:
        self.methods: list[str] = []

    async def request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        method = str(payload["method"])
        self.methods.append(method)
        if method == "initialize":
            result: Mapping[str, Any] = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}, "resources": {}},
            }
        elif method == "tools/list":
            result = {"tools": [{
                "name": "lookup", "inputSchema": {"type": "object"},
            }]}
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "tools/call":
            result = {"content": []}
        else:
            raise AssertionError(method)
        return {"jsonrpc": "2.0", "id": payload["id"], "result": result}

    async def notify(self, _payload: Mapping[str, Any]) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_stdio_transport_launch_is_exact_and_governed_by_shell_gateway(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("stdio")
    run = service.submit(group.group_id, title="stdio", request_key="submit")
    run = service.schedule()[0]
    argv = ("-m", "trusted_mcp")
    scope = ToolExecutionRequest.shell_scope(
        executable=sys.executable, argv=argv, cwd=tmp_path,
        read_paths=(tmp_path,), write_paths=(), network_hosts=(), child_processes=False,
    )
    approval = service.request_approval(
        group.group_id, run.run_id, kind="tool", summary="stdio launch",
        risk=RiskLevel.HIGH, request_key="approve",
        requested_capabilities=("mcp_stdio_launch",),
        tool_name="mcp_stdio_launch", operation_scope=scope, purpose="mcp_stdio_launch",
        expires_at=_future(),
    )
    _decision, grant, _run = service.resolve_approval(
        group.group_id, approval.approval_id, approved=True,
        capabilities=("mcp_stdio_launch",),
        request_key="approved",
    )
    assert grant is not None
    gateway = ToolGateway(service)
    gateway.register(ToolDescriptor(
        tool_name="mcp_stdio_launch", kind="shell", risk=RiskLevel.HIGH,
        executable_allowlist=(sys.executable,), cwd_roots=(str(tmp_path),),
        read_roots=(str(tmp_path),), write_roots=(), allow_child_processes=False,
    ), lambda _request: None, trusted_sandbox=True)
    launch = ToolExecutionRequest(
        group_id=group.group_id, task_id=run.task_id, run_id=run.run_id,
        tool_name="mcp_stdio_launch", idempotency_key="stdio-launch-1",
        purpose="mcp_stdio_launch", approval_id=approval.approval_id, grant_id=grant.grant_id,
        executable=sys.executable, argv=argv, cwd=str(tmp_path),
        read_paths=(str(tmp_path),), child_processes=False,
    )
    session = _FakeStdioSession()
    launches = 0

    async def factory(_config: McpServerConfig, _request: ToolExecutionRequest) -> _FakeStdioSession:
        nonlocal launches
        launches += 1
        return session

    config = McpServerConfig(
        server_id="mcp_" + "c" * 32, name="local", transport="stdio",
        command=sys.executable, args=argv, allowed_tools=("lookup",),
    )
    transport = McpStdioTransport(
        config, gateway=gateway, launch_request=launch, session_factory=factory,
        trusted_test_seam=True,
    )
    policy = McpRegistry()
    policy.register(config)
    await policy.handshake(config.server_id, transport)
    assert launches == 1
    await transport.close()


@pytest.mark.asyncio
async def test_stdio_transport_command_mismatch_fails_before_process_factory(tmp_path: Path) -> None:
    config = McpServerConfig(
        server_id="mcp_" + "d" * 32, name="local", transport="stdio",
        command=sys.executable, args=("-m", "trusted_mcp"),
    )
    request = ToolExecutionRequest(
        group_id="group_" + "1" * 32, task_id="task_" + "2" * 32,
        run_id="run_" + "3" * 32, tool_name="mcp_stdio_launch",
        idempotency_key="forged", purpose="mcp_stdio_launch",
        executable=sys.executable, argv=("-c", "steal()"), cwd=str(tmp_path),
    )
    launches = 0

    async def factory(_config: McpServerConfig, _request: ToolExecutionRequest) -> _FakeStdioSession:
        nonlocal launches
        launches += 1
        return _FakeStdioSession()

    transport = McpStdioTransport(
        config, gateway=object(), launch_request=request, session_factory=factory,
    )
    with pytest.raises(GatewayDenied, match="exact"):
        await transport.initialize()
    assert launches == 0
