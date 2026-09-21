from __future__ import annotations

import json
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
    policy = McpRegistry(
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    policy.register(config)
    # C-R2：transport 由 registry 按 config 自建并密封，调用方不再传入实例。
    accepted = await policy.handshake(config.server_id)
    assert accepted["tools"] == ("lookup",)
    assert accepted["resources"] == ("course://catalog",)
    assert "prompts/list" not in [name for name, _ in calls]
    transport = policy._transport_for(config.server_id)
    assert await transport.invoke("lookup", {"course": "math"}) == {
        "content": [{"type": "text", "text": "ok"}]
    }
    assert calls[-1] == ("tools/call", {"name": "lookup", "arguments": {"course": "math"}})
    await policy.close()


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
    """服务器宣告 prompts 能力时，必须在 discovery 之前拒绝。

    C-R2 之后 transport 由 registry 按 config 构造，所以这里改用真实 HTTP transport +
    MockTransport 来驱动，验证的是**真实代码路径**而不是一个假 transport 对象。
    """

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = str(payload["method"])
        calls.append(method)
        if method == "notifications/initialized" or "id" not in payload:
            # JSON-RPC 通知没有 id，peer 回 202 即可。
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0", "id": payload["id"],
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"prompts": {"listChanged": True}},
                    "serverInfo": {"name": "advertises-prompts", "version": "1"},
                },
            },
            headers={"Mcp-Session-Id": "session-opaque"},
        )

    config = McpServerConfig(
        server_id="mcp_" + "f" * 32, name="remote", transport="streamable_http",
        url="https://mcp.example/rpc", allowed_hosts=("mcp.example",),
        allowed_tools=("lookup",),
    )
    policy = McpRegistry(
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    policy.register(config)
    with pytest.raises(GatewayDenied, match="prompt"):
        await policy.handshake(config.server_id)
    # 拒绝发生在 discovery 之前：不得发出 tools/list 或 resources/list
    assert "tools/list" not in calls
    assert "resources/list" not in calls
    await policy.close()


@pytest.mark.asyncio
async def test_registry_binds_http_effect_to_the_registered_endpoint_host() -> None:
    """An allowlisted sibling host cannot replace the inspected MCP endpoint."""

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        payload = json.loads(request.content)
        method = str(payload["method"])
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0", "id": payload["id"],
                    "result": {"content": [{"type": "text", "text": "ok"}]},
                },
                headers={"Mcp-Session-Id": "session-opaque"},
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0", "id": payload["id"],
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "sibling", "version": "1"},
                },
            },
            headers={"Mcp-Session-Id": "session-opaque"},
        )

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
    policy = McpRegistry(
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    policy.register(config)
    policy.accept_discovery(config.server_id, {
        "tools": [{"name": "lookup", "inputSchema": {"type": "object"}}],
        "resources": [],
    })
    # 兄弟主机在 allowlist 里，但**不是**注册的端点主机 → 必须在 effect 之前拒绝。
    request = ToolExecutionRequest(
        group_id="group_" + "1" * 32, task_id="task_" + "2" * 32,
        run_id="run_" + "3" * 32, tool_name="mcp_lookup",
        idempotency_key="host-swap", purpose="mcp_call",
        network_hosts=("other.example",), arguments={"course": "math"},
    )

    with pytest.raises(GatewayDenied, match="endpoint host"):
        await policy.invoke(
            config.server_id, "lookup", {"course": "math"},
            gateway=Gateway(), request=request,  # type: ignore[arg-type]
        )
    # 一次网络调用都没发生：被拒绝的是 effect 本身，而不是事后才发现连错主机。
    assert calls == []
    await policy.close()


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
async def test_stdio_requires_a_trusted_factory_and_never_launches_a_process(tmp_path: Path) -> None:
    """C-R2 之后：stdio 的进程创建只由**注入的受信 factory** 负责。

    旧语义把启动授权交给调用方每次传入的 launch request（于是"授权指向 A、实际启动 B"
    可以错位）；现在 registry 按密封 config 构造 transport，启动通道必须是构造时注入的
    factory。没有它就 fail closed，且不得启动任何真实进程。
    """

    launches = 0

    async def factory(_config: McpServerConfig) -> "_FakeStdioSession":
        nonlocal launches
        launches += 1
        return _FakeStdioSession()

    config = McpServerConfig(
        server_id="mcp_" + "c" * 32, name="local", transport="stdio",
        command=sys.executable, args=("-m", "trusted_mcp"),
        allowed_tools=("lookup",),
    )

    # 1) 缺少受信 factory → 拒绝，且一次都没启动
    policy_without_factory = McpRegistry()
    policy_without_factory.register(config)
    with pytest.raises(GatewayDenied, match="trusted sandbox"):
        await policy_without_factory.handshake(config.server_id)
    assert launches == 0

    # 2) 注入受信 factory → 握手成功，且 factory 恰好被调用一次
    policy = McpRegistry(session_factory=factory)
    policy.register(config)
    accepted = await policy.handshake(config.server_id)
    assert accepted["tools"] == ("lookup",)
    assert launches == 1
    await policy.close()

    assert tmp_path.exists()


@pytest.mark.asyncio
async def test_stdio_launch_request_from_caller_cannot_redirect_the_peer(tmp_path: Path) -> None:
    """C-R2 的核心不变量：调用方无法再通过传入 transport/launch request 换掉 peer。

    registry 只持有自己按 config 构造的密封 transport，因此"identity 记录 A、
    实际连接 B"这条错位路径在签名层面就不存在了。
    """

    launched: list[str] = []

    async def factory(config: McpServerConfig) -> "_FakeStdioSession":
        launched.append(str(config.command))
        return _FakeStdioSession()

    trusted = McpServerConfig(
        server_id="mcp_" + "d" * 32, name="trusted", transport="stdio",
        command=sys.executable, args=("-m", "trusted_mcp"),
        allowed_tools=("lookup",),
    )
    rogue = McpServerConfig(
        server_id="mcp_" + "e" * 32, name="rogue", transport="stdio",
        command="C:/evil/steal.exe", args=("-c", "steal()"),
    )

    policy = McpRegistry(session_factory=factory)
    policy.register(trusted)

    # 旧 API 的形状已被移除：handshake 不接受 transport，invoke/read 不接受外部 effect。
    with pytest.raises(TypeError):
        await policy.handshake(trusted.server_id, object())  # type: ignore[misc]
    with pytest.raises(TypeError):
        await policy.handshake(trusted.server_id, transport=object())  # type: ignore[misc]

    accepted = await policy.handshake(trusted.server_id)
    assert accepted["tools"] == ("lookup",)
    # 只可能启动注册过的那一个 peer
    assert launched == [sys.executable]
    assert str(rogue.command) not in launched
    await policy.close()
