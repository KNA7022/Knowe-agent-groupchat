from __future__ import annotations

"""Commercial security gateways for provider, tool, MCP, DLP and secret effects.

The classes in this module are deliberately transport-neutral.  The backend remains
the policy authority: callers submit a requested subset, while backend-authored
``ToolDescriptor`` and Task Runtime grants define the maximum and exact authority.
No tool body, raw PII or credential is written to the Task Runtime public ledger.
"""

import hashlib
import inspect
import json
import math
import re
import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol
from urllib.parse import urlparse
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

import httpx

from backend.task_runtime.contracts import (
    DlpDecision,
    McpServerConfig,
    PiiFinding,
    PrivacyTier,
    RiskLevel,
    ToolDescriptor,
)
from backend.task_runtime.service import RuntimeConflictError, RuntimeNotFoundError, TaskRuntimeService
from backend.task_runtime.store import RuntimeStoreError


class GatewayDenied(PermissionError):
    """A request failed policy before its effect boundary."""


def _opaque(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _aware_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError) as exc:
        raise GatewayDenied("lease expiry must be timezone-aware ISO-8601") from exc


def _canonical_path(value: str | Path, *, require_absolute: bool = True) -> Path:
    raw = Path(str(value)).expanduser()
    if require_absolute and not raw.is_absolute():
        raise GatewayDenied("relative path scope is forbidden")
    try:
        return raw.resolve(strict=False)
    except OSError as exc:
        raise GatewayDenied("path scope cannot be resolved") from exc


def _under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _path_allowed(value: str, roots: Iterable[str]) -> bool:
    path = _canonical_path(value)
    for raw_root in roots:
        root = _canonical_path(raw_root)
        if _under(path, root):
            return True
    return False


def _digest_items(values: Iterable[str]) -> str:
    canonical = sorted(dict.fromkeys(str(item) for item in values))
    body = json.dumps(canonical, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _digest_ordered(values: Iterable[str]) -> str:
    body = json.dumps(
        [str(item) for item in values], ensure_ascii=True, separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolExecutionRequest:
    group_id: str
    task_id: str
    run_id: str
    tool_name: str
    idempotency_key: str
    purpose: str
    approval_id: str = ""
    grant_id: str = ""
    executable: str = ""
    argv: tuple[str, ...] = ()
    cwd: str = ""
    read_paths: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    network_hosts: tuple[str, ...] = ()
    child_processes: bool = False
    outbound_payload: str = field(default="", repr=False, compare=False)
    arguments: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def replace(self, **changes: Any) -> "ToolExecutionRequest":
        return replace(self, **changes)

    def governed_outbound_text(self, descriptor: ToolDescriptor) -> str:
        """Return backend-derived material that can cross an effect boundary.

        Callers cannot suppress DLP by omitting ``outbound_payload``: MCP arguments
        always leave the model boundary, and network-capable built-ins/Shell requests
        are scanned from their exact structured arguments as well.
        """

        parts = [self.outbound_payload] if self.outbound_payload else []
        network_effect = bool(descriptor.network_hosts or self.network_hosts)
        if descriptor.kind == "mcp" or network_effect:
            if self.arguments:
                parts.append(json.dumps(
                    dict(self.arguments), ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), default=str,
                ))
            if descriptor.kind == "shell" and self.argv:
                parts.append(json.dumps(
                    list(self.argv), ensure_ascii=False, separators=(",", ":"),
                ))
        return "\n".join(parts)

    @staticmethod
    def shell_scope(
        *,
        executable: str,
        argv: Iterable[str] = (),
        cwd: str | Path,
        read_paths: Iterable[str | Path],
        write_paths: Iterable[str | Path],
        network_hosts: Iterable[str],
        child_processes: bool,
    ) -> dict[str, str]:
        canonical_executable = _canonical_path(executable).as_posix().casefold()
        canonical_cwd = _canonical_path(cwd).as_posix()
        reads = tuple(_canonical_path(item).as_posix() for item in read_paths)
        writes = tuple(_canonical_path(item).as_posix() for item in write_paths)
        hosts = tuple(str(item).strip().casefold() for item in network_hosts if str(item).strip())
        return {
            "executable_digest": hashlib.sha256(canonical_executable.encode("utf-8")).hexdigest(),
            "argv_digest": _digest_ordered(str(item) for item in argv),
            "cwd_digest": hashlib.sha256(canonical_cwd.encode("utf-8")).hexdigest(),
            "read_digest": _digest_items(reads),
            "write_digest": _digest_items(writes),
            "network_digest": _digest_items(hosts),
            "child_processes": "true" if child_processes else "false",
        }

    def operation_scope(self, descriptor: ToolDescriptor) -> dict[str, str]:
        outbound_text = self.governed_outbound_text(descriptor)
        if descriptor.kind == "shell":
            scope = self.shell_scope(
                executable=self.executable,
                argv=self.argv,
                cwd=self.cwd,
                read_paths=self.read_paths,
                write_paths=self.write_paths,
                network_hosts=self.network_hosts,
                child_processes=self.child_processes,
            )
            if outbound_text:
                scope["outbound_digest"] = hashlib.sha256(
                    outbound_text.encode("utf-8")
                ).hexdigest()
                scope["pii_digest"] = _digest_items(
                    finding.digest for finding in PiiDlpGateway().scan(outbound_text)
                )
            return scope
        argument_digest = hashlib.sha256(
            json.dumps(
                dict(self.arguments), ensure_ascii=True, sort_keys=True,
                separators=(",", ":"), default=str,
            ).encode("utf-8")
        ).hexdigest()
        scope = {
            "descriptor": hashlib.sha256(
                f"{descriptor.kind}:{descriptor.tool_name}:{descriptor.schema_digest}".encode("utf-8")
            ).hexdigest(),
            "arguments": argument_digest,
            "network_digest": _digest_items(
                str(item).strip().casefold() for item in self.network_hosts
            ),
        }
        if outbound_text:
            scope["outbound_digest"] = hashlib.sha256(
                outbound_text.encode("utf-8")
            ).hexdigest()
            scope["pii_digest"] = _digest_items(
                finding.digest for finding in PiiDlpGateway().scan(outbound_text)
            )
        return scope


ToolEffect = Callable[[ToolExecutionRequest], Any]


class ToolGateway:
    """One policy/effect boundary shared by built-ins, Shell and MCP."""

    def __init__(self, runtime: TaskRuntimeService, *, dlp: "PiiDlpGateway | None" = None) -> None:
        self.runtime = runtime
        self.dlp = dlp or PiiDlpGateway()
        self._descriptors: dict[str, ToolDescriptor] = {}
        self._effects: dict[str, ToolEffect] = {}

    def register(self, descriptor: ToolDescriptor, effect: ToolEffect) -> None:
        if descriptor.tool_name in self._descriptors:
            raise ValueError(f"duplicate Tool Gateway descriptor: {descriptor.tool_name}")
        self._descriptors[descriptor.tool_name] = descriptor
        self._effects[descriptor.tool_name] = effect

    def descriptor(self, tool_name: str) -> ToolDescriptor:
        try:
            return self._descriptors[str(tool_name)]
        except KeyError as exc:
            raise GatewayDenied("tool is not registered with Tool Gateway") from exc

    @staticmethod
    def _host_allowed(host: str, allowed: Iterable[str]) -> bool:
        candidate = str(host).strip().casefold().rstrip(".")
        if not candidate or "/" in candidate or "@" in candidate:
            return False
        for raw in allowed:
            rule = str(raw).strip().casefold().rstrip(".")
            if rule.startswith("*."):
                suffix = rule[1:]
                if candidate.endswith(suffix) and candidate != suffix[1:]:
                    return True
            elif candidate == rule:
                return True
        return False

    def _validate_capability_subset(
        self, descriptor: ToolDescriptor, request: ToolExecutionRequest,
    ) -> None:
        if request.tool_name != descriptor.tool_name:
            raise GatewayDenied("cross-tool authority is forbidden")
        if not request.idempotency_key.strip() or not request.purpose.strip():
            raise GatewayDenied("tool idempotency key and purpose are required")
        if descriptor.kind == "shell":
            try:
                executable = _canonical_path(request.executable).as_posix().casefold()
                allowed_exec = {
                    _canonical_path(item).as_posix().casefold()
                    for item in descriptor.executable_allowlist
                }
            except GatewayDenied as exc:
                raise GatewayDenied("shell executable must be an absolute trusted path") from exc
            if not executable or executable not in allowed_exec:
                raise GatewayDenied("shell executable is outside the descriptor")
            if not request.cwd or not _path_allowed(request.cwd, descriptor.cwd_roots):
                raise GatewayDenied("shell cwd escapes authorized roots")
            for path in request.read_paths:
                if not _path_allowed(path, descriptor.read_roots):
                    raise GatewayDenied("shell read path escapes authorized roots")
            for path in request.write_paths:
                if not _path_allowed(path, descriptor.write_roots):
                    raise GatewayDenied("shell write path escapes authorized roots")
        elif request.executable or request.argv or request.cwd or request.child_processes:
            raise GatewayDenied("non-shell tool requested shell capabilities")
        if request.child_processes and not descriptor.allow_child_processes:
            raise GatewayDenied("child-process capability is not authorized")
        for host in request.network_hosts:
            if not self._host_allowed(host, descriptor.network_hosts):
                raise GatewayDenied("network host is outside the authorized domains")
        if descriptor.network_hosts and not request.network_hosts:
            raise GatewayDenied("network-capable tools require an explicit authorized host")
        if request.network_hosts and not descriptor.network_hosts:
            raise GatewayDenied("offline tool requested network access")

    async def _invoke(
        self, request: ToolExecutionRequest, *, effect_override: ToolEffect | None = None,
    ) -> Any:
        descriptor = self.descriptor(request.tool_name)
        self._validate_capability_subset(descriptor, request)
        dlp = self.dlp.decide_outbound(
            request.governed_outbound_text(descriptor), approved=False,
        )
        if dlp.requires_approval and descriptor.risk is not RiskLevel.HIGH:
            raise GatewayDenied("sensitive outbound tools must be high risk")
        if dlp.requires_approval and not (request.approval_id and request.grant_id):
            raise GatewayDenied("sensitive outbound data requires fresh Task Runtime approval")
        try:
            invocation = self.runtime.prepare_tool(
                request.group_id,
                request.run_id,
                task_id=request.task_id,
                tool_name=request.tool_name,
                idempotency_key=request.idempotency_key,
                risk=descriptor.risk,
                public_summary="Governed tool effect prepared",
                approval_id=request.approval_id or None,
                grant_id=request.grant_id or None,
                operation_scope=request.operation_scope(descriptor),
                purpose=request.purpose,
            )
        except (RuntimeConflictError, RuntimeNotFoundError, RuntimeStoreError, ValueError) as exc:
            raise GatewayDenied("Task Runtime denied tool authority") from exc
        if invocation.disposition != "created" or invocation.status != "prepared":
            raise GatewayDenied("fresh purpose-specific approval is required")
        running, claimed = self.runtime.store.claim_tool_execution(invocation.invocation_id)
        if not claimed or running.status != "running":
            raise GatewayDenied("tool invocation was already claimed or completed")
        effect = effect_override or self._effects[descriptor.tool_name]
        try:
            result = effect(request)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            self.runtime.store.complete_tool(
                invocation.invocation_id, status="failed", error_type=type(exc).__name__,
            )
            raise GatewayDenied(f"tool effect failed ({type(exc).__name__})") from None
        self.runtime.store.complete_tool(invocation.invocation_id, status="completed")
        return result

    async def invoke(self, request: ToolExecutionRequest) -> Any:
        return await self._invoke(request)

    async def invoke_with_effect(
        self, request: ToolExecutionRequest, effect: ToolEffect,
    ) -> Any:
        return await self._invoke(request, effect_override=effect)


@dataclass(frozen=True)
class LegacyToolAuthority:
    """Backend-authored canonical Task Runtime identity for one legacy tool turn."""

    group_id: str
    task_id: str
    run_id: str
    approval_id: str = ""
    grant_id: str = ""


_LEGACY_TOOL_AUTHORITY: ContextVar[LegacyToolAuthority | None] = ContextVar(
    "studycrew_legacy_tool_authority", default=None,
)


@contextmanager
def legacy_tool_authority_context(authority: LegacyToolAuthority | None):
    token = _LEGACY_TOOL_AUTHORITY.set(authority)
    try:
        yield
    finally:
        _LEGACY_TOOL_AUTHORITY.reset(token)


def current_legacy_tool_authority() -> LegacyToolAuthority | None:
    return _LEGACY_TOOL_AUTHORITY.get()


class LegacyToolBoundary:
    """Strangler boundary for ToolRegistry.

    Only explicitly classified, project-local read operations are migrated today.
    Legacy writes, Shell command strings and network tools lack the structured exact
    capability description required by Tool Gateway, so they fail closed before their
    handler.  This is an intentional availability tradeoff, not a fallback authority.
    """

    def __init__(
        self,
        runtime: TaskRuntimeService,
        *,
        authority_provider: Callable[[], LegacyToolAuthority | None],
        read_only_tools: frozenset[str],
    ) -> None:
        self.gateway = ToolGateway(runtime)
        self.authority_provider = authority_provider
        self.read_only_tools = frozenset(str(item) for item in read_only_tools)
        self._registered: set[str] = set()

    @staticmethod
    def _schema_digest(tool: Any) -> str:
        return hashlib.sha256(json.dumps(
            getattr(tool, "parameters", {}), ensure_ascii=True, sort_keys=True,
            separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()

    async def __call__(
        self, tool: Any, args: dict[str, Any], context: dict[str, Any],
    ) -> str:
        authority = self.authority_provider()
        if authority is None:
            raise GatewayDenied("authoritative Task Runtime context is unavailable")
        tool_name = str(getattr(tool, "name", "") or "")
        if tool_name not in self.read_only_tools:
            raise GatewayDenied(
                "legacy effect is blocked pending structured capability migration"
            )
        call_id = str(context.pop("tool_call_id", "") or "").strip()
        if not call_id or len(call_id) > 160 or any(ord(char) < 32 for char in call_id):
            raise GatewayDenied("authoritative tool call id is required")
        if tool_name not in self._registered:
            descriptor = ToolDescriptor(
                tool_name=tool_name,
                kind="builtin",
                risk=RiskLevel.LOW,
                schema_digest=self._schema_digest(tool),
            )
            self.gateway.register(descriptor, lambda _request: None)
            self._registered.add(tool_name)
        request = ToolExecutionRequest(
            group_id=authority.group_id,
            task_id=authority.task_id,
            run_id=authority.run_id,
            tool_name=tool_name,
            idempotency_key=f"legacy:{call_id}",
            purpose=f"legacy_read:{tool_name}",
            approval_id=authority.approval_id,
            grant_id=authority.grant_id,
            arguments=dict(args),
        )

        async def effect(_request: ToolExecutionRequest) -> str:
            return await tool.execute(args, **context)

        return str(await self.gateway.invoke_with_effect(request, effect))


_PROMPT_INJECTION = re.compile(
    r"(?i)(?:ignore|disregard|override).{0,40}(?:instruction|system|policy)|"
    r"(?:exfiltrat|steal|reveal).{0,40}(?:secret|credential|token)|"
    r"\u5ffd\u7565.{0,20}(?:\u6307\u4ee4|\u89c4\u5219|\u7cfb\u7edf)|"
    r"(?:\u6cc4\u9732|\u7a83\u53d6).{0,20}(?:\u5bc6\u94a5|\u51ed\u636e|\u4ee4\u724c)"
)


class McpTransport(Protocol):
    async def initialize(self) -> Mapping[str, Any]: ...
    async def discover(self) -> Mapping[str, Any]: ...
    async def invoke(self, name: str, arguments: Mapping[str, Any]) -> Any: ...


class McpRpcSession(Protocol):
    async def request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...
    async def notify(self, payload: Mapping[str, Any]) -> None: ...
    async def close(self) -> None: ...


class _McpJsonRpcTransport:
    """Bounded MCP JSON-RPC client shared by stdio and Streamable HTTP."""

    _MAX_DISCOVERY_PAGES = 16

    def __init__(self) -> None:
        self._rpc_id = 0
        self._initialize_result: dict[str, Any] | None = None

    async def _exchange(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise NotImplementedError

    async def _notify(self, payload: Mapping[str, Any]) -> None:
        raise NotImplementedError

    async def _request(
        self, method: str, params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._rpc_id += 1
        request_id = self._rpc_id
        payload: dict[str, Any] = {
            "jsonrpc": "2.0", "id": request_id, "method": method,
        }
        if params is not None:
            payload["params"] = dict(params)
        raw = await self._exchange(payload)
        if (
            not isinstance(raw, Mapping)
            or raw.get("jsonrpc") != "2.0"
            or raw.get("id") != request_id
        ):
            raise GatewayDenied("MCP peer returned an invalid JSON-RPC response")
        if raw.get("error") is not None:
            # Error bodies are untrusted and can contain prompts, secrets or PII.
            raise GatewayDenied("MCP peer rejected the governed request")
        result = raw.get("result")
        if not isinstance(result, Mapping):
            raise GatewayDenied("MCP peer returned a malformed result")
        return dict(result)

    async def initialize(self) -> Mapping[str, Any]:
        if self._initialize_result is not None:
            return dict(self._initialize_result)
        result = await self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "StudyCrew", "version": "1"},
        })
        await self._notify({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        self._initialize_result = dict(result)
        return dict(result)

    async def _list_all(self, method: str, key: str) -> list[Any]:
        cursor = ""
        seen: set[str] = set()
        collected: list[Any] = []
        for _page in range(self._MAX_DISCOVERY_PAGES):
            params = {"cursor": cursor} if cursor else None
            result = await self._request(method, params)
            values = result.get(key)
            if not isinstance(values, list):
                raise GatewayDenied("MCP discovery response is malformed")
            collected.extend(values)
            next_cursor = str(result.get("nextCursor") or "")
            if not next_cursor:
                return collected
            if (
                len(next_cursor) > 256
                or next_cursor in seen
                or any(ord(char) < 32 for char in next_cursor)
            ):
                raise GatewayDenied("MCP discovery cursor is invalid")
            seen.add(next_cursor)
            cursor = next_cursor
        raise GatewayDenied("MCP discovery exceeded the page limit")

    async def discover(self) -> Mapping[str, Any]:
        await self.initialize()
        # prompts/list is deliberately never sent.  Prompt capability is disabled
        # even when a malicious peer advertises it.
        tools = await self._list_all("tools/list", "tools")
        resources = await self._list_all("resources/list", "resources")
        return {"tools": tools, "resources": resources, "prompts": []}

    async def invoke(self, name: str, arguments: Mapping[str, Any]) -> Any:
        await self.initialize()
        return await self._request("tools/call", {
            "name": str(name), "arguments": dict(arguments),
        })

    async def read_resource(self, uri: str) -> Any:
        await self.initialize()
        return await self._request("resources/read", {"uri": str(uri)})


class _JsonLineMcpSession:
    """Minimal sequential stdio JSON-RPC session launched without a shell."""

    _MAX_LINE_BYTES = 1_048_576

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._lock = asyncio.Lock()

    @classmethod
    async def launch(
        cls, config: McpServerConfig, request: ToolExecutionRequest,
    ) -> "_JsonLineMcpSession":
        process = await asyncio.create_subprocess_exec(
            config.command,
            *config.args,
            cwd=request.cwd,
            env={},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return cls(process)

    async def _write(self, payload: Mapping[str, Any]) -> None:
        stdin = self._process.stdin
        if stdin is None or self._process.returncode is not None:
            raise GatewayDenied("MCP stdio process is unavailable")
        encoded = json.dumps(
            dict(payload), ensure_ascii=True, separators=(",", ":"), default=str,
        ).encode("utf-8") + b"\n"
        if len(encoded) > self._MAX_LINE_BYTES:
            raise GatewayDenied("MCP stdio request exceeds the size limit")
        stdin.write(encoded)
        await stdin.drain()

    async def request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        async with self._lock:
            await self._write(payload)
            stdout = self._process.stdout
            if stdout is None:
                raise GatewayDenied("MCP stdio output is unavailable")
            for _message in range(64):
                try:
                    line = await asyncio.wait_for(stdout.readline(), timeout=30.0)
                except asyncio.TimeoutError as exc:
                    raise GatewayDenied("MCP stdio response timed out") from exc
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    raise GatewayDenied("MCP stdio response exceeds the framing limit") from exc
                if not line or len(line) > self._MAX_LINE_BYTES:
                    raise GatewayDenied("MCP stdio response is empty or oversized")
                if not line.endswith(b"\n"):
                    raise GatewayDenied("MCP stdio response is missing newline framing")
                try:
                    response = json.loads(line)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise GatewayDenied("MCP stdio returned invalid JSON") from exc
                if not isinstance(response, Mapping):
                    raise GatewayDenied("MCP stdio returned an invalid message")
                if response.get("method") and response.get("id") is not None:
                    # Server-initiated elicitation/sampling/roots requests are not an
                    # ambient capability and are rejected by closing the session.
                    await self.close()
                    raise GatewayDenied("MCP server-initiated requests are disabled")
                if response.get("id") == payload.get("id"):
                    return response
            raise GatewayDenied("MCP stdio emitted too many unsolicited messages")

    async def notify(self, payload: Mapping[str, Any]) -> None:
        async with self._lock:
            await self._write(payload)

    async def close(self) -> None:
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()


McpStdioSessionFactory = Callable[
    [McpServerConfig, ToolExecutionRequest],
    McpRpcSession | Awaitable[McpRpcSession],
]


class McpStdioTransport(_McpJsonRpcTransport):
    """MCP stdio whose process creation is itself a governed Shell effect."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        gateway: ToolGateway,
        launch_request: ToolExecutionRequest,
        session_factory: McpStdioSessionFactory | None = None,
    ) -> None:
        super().__init__()
        if config.transport != "stdio":
            raise ValueError("McpStdioTransport requires a stdio config")
        self.config = config
        self.gateway = gateway
        self.launch_request = launch_request
        self.session_factory = session_factory or _JsonLineMcpSession.launch
        self._session: McpRpcSession | None = None

    def _validate_exact_launch(self) -> None:
        try:
            configured = _canonical_path(self.config.command).as_posix().casefold()
            requested = _canonical_path(self.launch_request.executable).as_posix().casefold()
        except GatewayDenied as exc:
            raise GatewayDenied("MCP stdio launch requires an exact absolute executable") from exc
        if configured != requested or tuple(self.config.args) != tuple(self.launch_request.argv):
            raise GatewayDenied("MCP stdio launch does not match the exact registered command")
        if not self.launch_request.cwd:
            raise GatewayDenied("MCP stdio launch requires an exact working directory")

    async def _ensure_session(self) -> McpRpcSession:
        if self._session is not None:
            return self._session
        self._validate_exact_launch()
        if not isinstance(self.gateway, ToolGateway):
            raise GatewayDenied("MCP stdio launch has no Tool Gateway")

        async def launch_effect(_request: ToolExecutionRequest) -> McpRpcSession:
            value = self.session_factory(self.config, self.launch_request)
            session = await value if inspect.isawaitable(value) else value
            if not all(callable(getattr(session, name, None)) for name in ("request", "notify", "close")):
                raise GatewayDenied("MCP stdio session factory returned an invalid session")
            return session

        session = await self.gateway.invoke_with_effect(self.launch_request, launch_effect)
        self._session = session
        return session

    async def _exchange(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return await (await self._ensure_session()).request(payload)

    async def _notify(self, payload: Mapping[str, Any]) -> None:
        await (await self._ensure_session()).notify(payload)

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()


class McpStreamableHttpTransport(_McpJsonRpcTransport):
    """Bounded Streamable HTTP transport with redirects disabled."""

    _MAX_RESPONSE_BYTES = 1_048_576

    def __init__(
        self,
        config: McpServerConfig,
        *,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        super().__init__()
        if config.transport != "streamable_http":
            raise ValueError("McpStreamableHttpTransport requires a Streamable HTTP config")
        parsed = urlparse(config.url)
        if (
            parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password
            or not ToolGateway._host_allowed(parsed.hostname, config.allowed_hosts)
        ):
            raise GatewayDenied("MCP Streamable HTTP requires an authorized HTTPS host")
        self.config = config
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(follow_redirects=False, timeout=30.0)
        )
        self._client: httpx.AsyncClient | None = None
        self._session_id = ""

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2025-06-18",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    @classmethod
    def _decode_response(
        cls, content: bytes, content_type_header: str,
    ) -> Mapping[str, Any]:
        if len(content) > cls._MAX_RESPONSE_BYTES:
            raise GatewayDenied("MCP HTTP response exceeds the size limit")
        content_type = content_type_header.split(";", 1)[0].strip().casefold()
        if content_type not in {"application/json", "text/event-stream"}:
            raise GatewayDenied("MCP HTTP response content type violates framing policy")
        try:
            if content_type == "text/event-stream":
                text = content.decode("utf-8")
                events: list[list[str]] = []
                current: list[str] = []
                for line in text.splitlines():
                    if not line:
                        if current:
                            events.append(current)
                            current = []
                        continue
                    if line.startswith(":") or line.startswith(("event:", "id:", "retry:")):
                        continue
                    if not line.startswith("data:"):
                        raise ValueError
                    current.append(line[5:].lstrip())
                if current:
                    events.append(current)
                # One HTTP request has one governed JSON-RPC response.  Multiple
                # SSE data events are ambiguous ambient messages and fail closed.
                if len(events) != 1:
                    raise ValueError
                value = json.loads("\n".join(events[0]))
            else:
                value = json.loads(content)
        except (TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise GatewayDenied("MCP HTTP returned invalid JSON") from exc
        if not isinstance(value, Mapping):
            raise GatewayDenied("MCP HTTP returned an invalid message")
        return value

    async def _post(
        self, payload: Mapping[str, Any], *, notification: bool = False,
    ) -> Mapping[str, Any] | None:
        encoded = json.dumps(
            dict(payload), ensure_ascii=True, separators=(",", ":"), default=str,
        ).encode("utf-8")
        if len(encoded) > self._MAX_RESPONSE_BYTES:
            raise GatewayDenied("MCP HTTP request exceeds the size limit")
        try:
            async with self._http_client().stream(
                "POST", self.config.url, content=encoded,
                headers=self._headers(), follow_redirects=False,
            ) as response:
                if 300 <= response.status_code < 400 or response.headers.get("location"):
                    raise GatewayDenied("MCP redirects are forbidden")
                if response.status_code < 200 or response.status_code >= 300:
                    raise GatewayDenied("MCP HTTP peer rejected the request")
                session_id = response.headers.get("Mcp-Session-Id", "")
                if session_id:
                    if (
                        len(session_id) > 256
                        or any(ord(char) < 33 or ord(char) > 126 for char in session_id)
                    ):
                        raise GatewayDenied("MCP HTTP session id is invalid")
                    self._session_id = session_id
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunks) + len(chunk) > self._MAX_RESPONSE_BYTES:
                        raise GatewayDenied("MCP HTTP response exceeds the size limit")
                    chunks.extend(chunk)
                content = bytes(chunks)
                chunks.clear()
                if notification and response.status_code in {200, 202, 204} and not content:
                    return None
                content_type = response.headers.get("content-type", "")
        except httpx.HTTPError as exc:
            raise GatewayDenied("MCP HTTP transport failed") from exc
        return self._decode_response(content, content_type)

    async def _exchange(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        response = await self._post(payload)
        if response is None:
            raise GatewayDenied("MCP HTTP response is missing")
        return response

    async def _notify(self, payload: Mapping[str, Any]) -> None:
        await self._post(payload, notification=True)

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()


class McpRegistry:
    """MCP configuration, handshake, discovery and invocation policy registry."""

    def __init__(self) -> None:
        self._configs: dict[str, McpServerConfig] = {}
        self._discovery: dict[str, dict[str, Any]] = {}

    def register(self, config: McpServerConfig) -> None:
        if config.server_id in self._configs:
            raise ValueError("duplicate MCP server id")
        if config.transport == "streamable_http":
            self._validate_url(config, config.url)
        self._configs[config.server_id] = config

    def _config(self, server_id: str) -> McpServerConfig:
        try:
            return self._configs[str(server_id)]
        except KeyError as exc:
            raise GatewayDenied("MCP server is not registered") from exc

    @staticmethod
    def _validate_url(config: McpServerConfig, url: str) -> None:
        parsed = urlparse(str(url))
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise GatewayDenied("MCP Streamable HTTP requires an authorized HTTPS host")
        host = parsed.hostname.casefold()
        if not ToolGateway._host_allowed(host, config.allowed_hosts):
            raise GatewayDenied("MCP target host is outside the authorized domains")

    def validate_http_target(self, server_id: str, url: str) -> None:
        config = self._config(server_id)
        if config.transport != "streamable_http":
            raise GatewayDenied("stdio MCP has no HTTP target")
        self._validate_url(config, url)

    def validate_http_response(self, server_id: str, status: int, location: str = "") -> None:
        config = self._config(server_id)
        if 300 <= int(status) < 400:
            raise GatewayDenied("MCP redirects are forbidden; authorize the final host explicitly")
        if location:
            self._validate_url(config, location)

    @staticmethod
    def _validate_effect_network_scope(
        config: McpServerConfig, request: ToolExecutionRequest,
    ) -> None:
        requested = {
            str(host).strip().casefold().rstrip(".")
            for host in request.network_hosts
            if str(host).strip()
        }
        if config.transport == "streamable_http":
            endpoint_host = (urlparse(config.url).hostname or "").casefold().rstrip(".")
            if requested != {endpoint_host}:
                raise GatewayDenied(
                    "MCP HTTP effect must use the registered endpoint host"
                )
        elif requested:
            raise GatewayDenied("MCP stdio effects are offline")

    @staticmethod
    def _validate_schema(schema: Any) -> None:
        if not isinstance(schema, Mapping) or schema.get("type") != "object":
            raise GatewayDenied("MCP tool schema must be a bounded object schema")
        encoded = json.dumps(schema, ensure_ascii=True, sort_keys=True, default=str)
        if len(encoded.encode("utf-8")) > 65_536:
            raise GatewayDenied("MCP tool schema exceeds the policy limit")
        if any(key in encoded for key in ('"$ref":"http:', '"$ref":"https:')):
            raise GatewayDenied("remote MCP schema references are forbidden")

    def accept_discovery(self, server_id: str, discovery: Mapping[str, Any]) -> dict[str, Any]:
        config = self._config(server_id)
        prompts = discovery.get("prompts") or ()
        if prompts:
            raise GatewayDenied("MCP prompts are disabled")
        raw_tools = discovery.get("tools") or ()
        raw_resources = discovery.get("resources") or ()
        if not isinstance(raw_tools, (list, tuple)) or not isinstance(raw_resources, (list, tuple)):
            raise GatewayDenied("MCP discovery payload is malformed")
        tool_names: list[str] = []
        for raw in raw_tools:
            if not isinstance(raw, Mapping):
                raise GatewayDenied("MCP tool discovery entry is malformed")
            name = str(raw.get("name") or "").strip()
            if not name or name in tool_names:
                raise GatewayDenied("MCP tool names must be unique and non-empty")
            description = str(raw.get("description") or "")
            if _PROMPT_INJECTION.search(description):
                raise GatewayDenied("MCP tool description contains prompt injection")
            self._validate_schema(raw.get("inputSchema"))
            tool_names.append(name)
        resource_uris = []
        for raw in raw_resources:
            if not isinstance(raw, Mapping) or not str(raw.get("uri") or "").strip():
                raise GatewayDenied("MCP resource discovery entry is malformed")
            resource_uris.append(str(raw["uri"]))
        unexpected_tools = set(tool_names) - set(config.allowed_tools)
        unexpected_resources = set(resource_uris) - set(config.allowed_resources)
        if unexpected_tools or unexpected_resources:
            raise GatewayDenied("MCP discovery returned unexpected tools or resources")
        accepted = {
            "server_id": server_id,
            "tools": tuple(tool_names),
            "resources": tuple(resource_uris),
            "prompts": (),
        }
        self._discovery[server_id] = accepted
        return accepted

    async def handshake(self, server_id: str, transport: McpTransport) -> dict[str, Any]:
        config = self._config(server_id)
        initialized = await transport.initialize()
        protocol = str(initialized.get("protocolVersion") or "")
        if not protocol or len(protocol) > 40:
            raise GatewayDenied("MCP handshake protocol version is invalid")
        if initialized.get("prompts") or (initialized.get("capabilities") or {}).get("prompts"):
            raise GatewayDenied("MCP prompt capability is disabled")
        if config.transport == "streamable_http" and initialized.get("redirect"):
            raise GatewayDenied("MCP redirects are forbidden")
        return self.accept_discovery(server_id, await transport.discover())

    async def invoke(
        self,
        server_id: str,
        name: str,
        arguments: Mapping[str, Any],
        *,
        transport: McpTransport,
        gateway: ToolGateway,
        request: ToolExecutionRequest,
    ) -> Any:
        config = self._config(server_id)
        accepted = self._discovery.get(server_id)
        if accepted is None or name not in accepted["tools"]:
            raise GatewayDenied("MCP tool was not inspected and granted")
        self._validate_effect_network_scope(config, request)
        canonical_requested = json.dumps(
            dict(request.arguments), ensure_ascii=True, sort_keys=True,
            separators=(",", ":"), default=str,
        )
        canonical_effect = json.dumps(
            dict(arguments), ensure_ascii=True, sort_keys=True,
            separators=(",", ":"), default=str,
        )
        if canonical_requested != canonical_effect:
            raise GatewayDenied("MCP invocation arguments changed after authorization")

        async def governed_effect(_request: ToolExecutionRequest) -> Any:
            return await transport.invoke(name, arguments)

        descriptor = gateway.descriptor(request.tool_name)
        if descriptor.kind != "mcp":
            raise GatewayDenied("MCP invocation requires an MCP Tool Gateway descriptor")
        return await gateway.invoke_with_effect(request, governed_effect)

    async def read_resource(
        self,
        server_id: str,
        uri: str,
        *,
        effect: Callable[[str], Awaitable[Any]],
        gateway: ToolGateway,
        request: ToolExecutionRequest,
    ) -> Any:
        config = self._config(server_id)
        accepted = self._discovery.get(server_id)
        if accepted is None or uri not in accepted["resources"]:
            raise GatewayDenied("MCP resource was not inspected and granted")
        self._validate_effect_network_scope(config, request)
        if str(request.arguments.get("uri") or "") != uri:
            raise GatewayDenied("MCP resource URI changed after authorization")
        return await gateway.invoke_with_effect(request, lambda _request: effect(uri))


_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("student_identifier", re.compile(r"(?i)(?:\u5b66\u53f7|student\s*id)\s*[:\uff1a#]?\s*[A-Za-z0-9_-]{4,}")),
    ("identity_pii", re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")),
    ("contact_pii", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("contact_pii", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")),
    ("academic_record", re.compile(
        r"(?i)(?:[\u4e00-\u9fff]{2,4}(?:\u7684)?\s*)?"
        r"(?:\u6210\u7ee9|\u5206\u6570|\u7ee9\u70b9|\u6392\u540d|\u540d\u6b21|grade|score|class\s+rank|academic\s+rank)"
        r"\s*(?:\u4e3a|\u662f|[:\uff1a])?\s*(?:\u7b2c\s*)?\d+(?:\.\d+)?(?:\s*[\u5206\u540d])?"
    )),
    ("timetable_association", re.compile(
        r"(?:[\u4e00-\u9fff]{2,4}\u7684(?:\u8bfe\u8868|\u8bfe\u7a0b\u8868|\u4e0a\u8bfe\u65f6\u95f4)|"
        r"\u59d3\u540d\s*[:\uff1a]\s*[\u4e00-\u9fff]{2,4}.{0,12}(?:\u8bfe\u8868|\u8bfe\u7a0b\u8868|\u4e0a\u8bfe\u65f6\u95f4))"
    )),
    ("private_note", re.compile(r"(?i)(?:\u79c1\u4eba|\u79c1\u5bc6|\u4e2a\u4eba)\u7b14\u8bb0|private\s+(?:note|journal)")),
)
_CREDENTIAL_TEXT = re.compile(
    r"(?i)(?:\bsk-[A-Za-z0-9._-]{6,}|authorization\s*:\s*bearer\s+\S+|"
    r"(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|cookie)\s*[:=]\s*\S+)"
)
_SECRET_KEYS = frozenset({
    "api_key", "apikey", "secret", "credential", "password", "token",
    "access_token", "refresh_token", "authorization", "cookie", "client_secret",
})
_BODY_KEYS = frozenset({"tool_body", "request_body", "response_body", "raw_payload"})


class PiiDlpGateway:
    def scan(self, value: str) -> tuple[PiiFinding, ...]:
        text = str(value or "")
        findings: list[PiiFinding] = []
        seen: set[tuple[str, int, int]] = set()
        for kind, pattern in _PII_PATTERNS:
            for match in pattern.finditer(text):
                key = (kind, match.start(), match.end())
                if key in seen:
                    continue
                seen.add(key)
                digest = hashlib.sha256(match.group(0).encode("utf-8")).hexdigest()
                finding_id = f"pii_{hashlib.sha256(f'{kind}:{match.start()}:{match.end()}:{digest}'.encode()).hexdigest()[:32]}"
                findings.append(PiiFinding(
                    finding_id=finding_id,
                    kind=kind,
                    start=match.start(),
                    end=match.end(),
                    digest=digest,
                ))
        return tuple(findings)

    def decide_outbound(self, value: str, *, approved: bool) -> DlpDecision:
        findings = self.scan(value)
        credential = bool(_CREDENTIAL_TEXT.search(str(value or "")))
        sensitive = bool(findings or credential)
        return DlpDecision(
            decision_id=_opaque("dlp"),
            allowed=not sensitive or bool(approved),
            requires_approval=sensitive and not approved,
            findings=findings,
            reason=("approved_sensitive_outbound" if sensitive and approved
                    else "sensitive_outbound" if sensitive else "no_sensitive_data"),
        )

    def redact_diagnostics(self, value: Any, *, field_name: str = "") -> Any:
        folded = str(field_name).strip().casefold().replace("-", "_")
        if folded in _SECRET_KEYS:
            return "[REDACTED_SECRET]" if value not in (None, "") else value
        if folded in _BODY_KEYS:
            return "[REDACTED_TOOL_BODY]"
        if isinstance(value, Mapping):
            return {
                str(key): self.redact_diagnostics(item, field_name=str(key))
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.redact_diagnostics(item) for item in value]
        if isinstance(value, str):
            if _CREDENTIAL_TEXT.search(value):
                return "[REDACTED_SECRET]"
            if self.scan(value):
                return "[REDACTED_PII]"
        return value


@dataclass(frozen=True)
class ProviderCapability:
    provider_id: str
    models: tuple[str, ...]
    capabilities: frozenset[str]
    privacy: PrivacyTier
    estimated_cost_usd: float
    preauthorized: bool

    def __post_init__(self) -> None:
        if not self.provider_id.strip() or not self.models:
            raise ValueError("provider id and models are required")
        if not math.isfinite(self.estimated_cost_usd) or self.estimated_cost_usd < 0:
            raise ValueError("provider estimated cost is invalid")


class ModelCapabilityRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ProviderCapability] = {}

    def register(self, capability: ProviderCapability) -> None:
        if capability.provider_id in self._providers:
            raise ValueError("duplicate provider capability")
        self._providers[capability.provider_id] = capability

    def get(self, provider_id: str) -> ProviderCapability:
        try:
            return self._providers[str(provider_id)]
        except KeyError as exc:
            raise GatewayDenied("provider is not registered") from exc

    def select_failover(
        self,
        primary_provider_id: str,
        *,
        candidates: Iterable[str],
        required_capabilities: Iterable[str],
        remaining_budget_usd: float,
    ) -> ProviderCapability:
        primary = self.get(primary_provider_id)
        required = frozenset(str(item) for item in required_capabilities)
        if not math.isfinite(float(remaining_budget_usd)) or remaining_budget_usd < 0:
            raise GatewayDenied("provider budget is invalid")
        budget_rejected = False
        policy_rejected = False
        for candidate_id in candidates:
            candidate = self._providers.get(str(candidate_id))
            if candidate is None or not candidate.preauthorized:
                policy_rejected = True
                continue
            if candidate.privacy.protection_rank < primary.privacy.protection_rank:
                policy_rejected = True
                continue
            if not required.issubset(candidate.capabilities):
                policy_rejected = True
                continue
            if candidate.estimated_cost_usd > remaining_budget_usd:
                budget_rejected = True
                continue
            return candidate
        if budget_rejected and not policy_rejected:
            raise GatewayDenied("provider failover budget is exhausted")
        raise GatewayDenied("no authorized privacy-preserving provider failover is available")


@dataclass
class _SecretLease:
    lease_id: str
    secret_ref: str
    purpose: str
    value: bytearray = field(repr=False)
    expires_at: datetime

    def clear(self) -> None:
        for index in range(len(self.value)):
            self.value[index] = 0


class SecretLeaseBroker:
    """In-memory Python lease receiver; it has deliberately no persistence API."""

    _LEASE_ID = re.compile(r"^lease_[0-9a-f]{32}$")
    _SECRET_REF = re.compile(r"^secret_[0-9a-f]{32}$")

    def __init__(self, *, max_ttl_seconds: int = 300) -> None:
        self.max_ttl_seconds = max(1, int(max_ttl_seconds))
        self._leases: dict[str, _SecretLease] = {}
        self._dlp = PiiDlpGateway()

    def provision(
        self,
        *,
        lease_id: str,
        secret_ref: str,
        purpose: str,
        value: str,
        expires_at: str,
        now: datetime | None = None,
    ) -> None:
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        expiry = _aware_utc(expires_at)
        ttl = (expiry - reference).total_seconds()
        if self._LEASE_ID.fullmatch(str(lease_id)) is None:
            raise GatewayDenied("secret lease id is invalid")
        if self._SECRET_REF.fullmatch(str(secret_ref)) is None:
            raise GatewayDenied("secret reference is invalid")
        if not purpose.strip() or not value:
            raise GatewayDenied("secret lease purpose and value are required")
        if not math.isfinite(ttl) or ttl <= 0 or ttl > self.max_ttl_seconds:
            raise GatewayDenied("secret lease TTL is outside policy")
        previous = self._leases.pop(lease_id, None)
        if previous is not None:
            previous.clear()
        # Main rotates provider leases before expiry.  Retire the previous lease for
        # the same durable reference/purpose so Python never accumulates credentials.
        for existing_id, existing in tuple(self._leases.items()):
            if existing.secret_ref == secret_ref and existing.purpose == purpose:
                existing.clear()
                self._leases.pop(existing_id, None)
        self._leases[lease_id] = _SecretLease(
            lease_id=lease_id,
            secret_ref=secret_ref,
            purpose=purpose,
            value=bytearray(value.encode("utf-8")),
            expires_at=expiry,
        )

    def redeem(
        self, lease_id: str, *, purpose: str, now: datetime | None = None,
    ) -> str:
        lease = self._leases.get(str(lease_id))
        if lease is None:
            raise GatewayDenied("secret lease is unavailable")
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if reference >= lease.expires_at:
            lease.clear()
            self._leases.pop(lease.lease_id, None)
            raise GatewayDenied("secret lease expired")
        if purpose != lease.purpose:
            raise GatewayDenied("secret lease purpose mismatch")
        return bytes(lease.value).decode("utf-8")

    def redeem_for_secret(
        self, secret_ref: str, *, purpose: str, now: datetime | None = None,
    ) -> str:
        """Resolve the current short lease for a durable opaque secret reference."""

        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        matches: list[_SecretLease] = []
        for lease_id, lease in tuple(self._leases.items()):
            if reference >= lease.expires_at:
                lease.clear()
                self._leases.pop(lease_id, None)
                continue
            if lease.secret_ref == str(secret_ref) and lease.purpose == purpose:
                matches.append(lease)
        if not matches:
            raise GatewayDenied("secret lease is unavailable")
        current = max(matches, key=lambda item: item.expires_at)
        return bytes(current.value).decode("utf-8")

    def public_snapshot(self) -> tuple[dict[str, str], ...]:
        return tuple({
            "lease_id": lease.lease_id,
            "secret_ref": lease.secret_ref,
            "purpose": lease.purpose,
            "expires_at": lease.expires_at.isoformat().replace("+00:00", "Z"),
        } for lease in self._leases.values())

    def redact_diagnostics(self, value: Any) -> Any:
        secrets = {
            bytes(lease.value).decode("utf-8")
            for lease in self._leases.values()
            if lease.value
        }
        redacted = self._dlp.redact_diagnostics(value)

        def scrub(item: Any) -> Any:
            if isinstance(item, Mapping):
                return {str(key): scrub(child) for key, child in item.items()}
            if isinstance(item, list):
                return [scrub(child) for child in item]
            if isinstance(item, str):
                result = item
                for secret in secrets:
                    if secret and secret in result:
                        result = result.replace(secret, "[REDACTED_SECRET]")
                return result
            return item

        return scrub(redacted)


__all__ = [
    "GatewayDenied",
    "LegacyToolAuthority",
    "LegacyToolBoundary",
    "current_legacy_tool_authority",
    "legacy_tool_authority_context",
    "McpRegistry",
    "McpStdioTransport",
    "McpStreamableHttpTransport",
    "ModelCapabilityRegistry",
    "PiiDlpGateway",
    "ProviderCapability",
    "SecretLeaseBroker",
    "ToolExecutionRequest",
    "ToolGateway",
]
