from __future__ import annotations

"""Commercial security gateways for provider, tool, MCP, DLP and secret effects.

The classes in this module are deliberately transport-neutral.  The backend remains
the policy authority: callers submit a requested subset, while backend-authored
``ToolDescriptor`` and Task Runtime grants define the maximum and exact authority.
No tool body, raw PII or credential is written to the Task Runtime public ledger.
"""

import base64
import binascii
import hashlib
import inspect
import json
import math
import re
import asyncio
import copy
import threading
import time
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
    BudgetUsage,
    DlpDecision,
    McpServerConfig,
    PiiFinding,
    PrivacyTier,
    RiskLevel,
    ToolDescriptor,
    TaskStatus,
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
        self._trusted_sandbox_tools: set[str] = set()

    def register(
        self,
        descriptor: ToolDescriptor,
        effect: ToolEffect,
        *,
        trusted_sandbox: bool = False,
    ) -> None:
        if descriptor.tool_name in self._descriptors:
            raise ValueError(f"duplicate Tool Gateway descriptor: {descriptor.tool_name}")
        self._descriptors[descriptor.tool_name] = descriptor
        self._effects[descriptor.tool_name] = effect
        if trusted_sandbox:
            if descriptor.kind != "shell":
                raise ValueError("trusted sandbox markers are valid only for Shell descriptors")
            self._trusted_sandbox_tools.add(descriptor.tool_name)

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
        if (
            descriptor.kind == "shell"
            and descriptor.tool_name not in self._trusted_sandbox_tools
        ):
            # Declared argv/path/network scopes are authorization metadata, not an OS
            # sandbox.  Until a verifiable Windows sandbox executor is injected, no
            # production Shell or MCP-stdio process may reach its effect boundary.
            raise GatewayDenied("Shell effect requires an explicitly trusted sandbox executor")
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
    runtime: TaskRuntimeService | None = field(default=None, repr=False, compare=False)
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


def _validate_jsonrpc_response(raw: Any, *, request_id: int) -> dict[str, Any]:
    """Validate one unambiguous JSON-RPC 2.0 response for every MCP transport."""

    if not isinstance(raw, Mapping):
        raise GatewayDenied("MCP peer returned an invalid JSON-RPC response")
    keys = frozenset(str(key) for key in raw)
    if "method" in keys or "params" in keys:
        raise GatewayDenied("MCP peer returned an ambiguous JSON-RPC request/response")
    has_result = "result" in keys
    has_error = "error" in keys
    if (
        raw.get("jsonrpc") != "2.0"
        or raw.get("id") != request_id
        or has_result == has_error
        or not keys.issubset({"jsonrpc", "id", "result", "error"})
    ):
        raise GatewayDenied("MCP peer returned an invalid JSON-RPC response")
    if has_error:
        error = raw.get("error")
        if (
            not isinstance(error, Mapping)
            or isinstance(error.get("code"), bool)
            or not isinstance(error.get("code"), int)
            or not isinstance(error.get("message"), str)
            or not str(error.get("message") or "").strip()
            or not frozenset(error).issubset({"code", "message", "data"})
        ):
            raise GatewayDenied("MCP peer returned a malformed JSON-RPC error")
        raise GatewayDenied("MCP peer rejected the governed request")
    result = raw.get("result")
    if not isinstance(result, Mapping):
        raise GatewayDenied("MCP peer returned a malformed result")
    return dict(result)


class _McpJsonRpcTransport:
    """Bounded MCP JSON-RPC client shared by stdio and Streamable HTTP."""

    _MAX_DISCOVERY_PAGES = 16

    def __init__(self, *, allowed_resources: Iterable[str] = ()) -> None:
        self._rpc_id = 0
        self._initialize_result: dict[str, Any] | None = None
        # capability 驱动 discovery：config 没声明资源能力时，连 resources/list
        # 都不发（旧实现无条件先 tools/list 再 resources/list）。
        self._allowed_resources = tuple(str(item) for item in allowed_resources)

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
        return _validate_jsonrpc_response(raw, request_id=request_id)

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
        # capability 驱动：只有注册了资源能力才请求 resources/list。
        resources: list[Any] = []
        if self._allowed_resources:
            resources = await self._list_all("resources/list", "resources")
        return {"tools": tools, "resources": resources}

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
        session_factory: Callable[[McpServerConfig], Any] | None = None,
        trusted_test_seam: bool = False,
    ) -> None:
        super().__init__(allowed_resources=config.allowed_resources)
        if config.transport != "stdio":
            raise ValueError("McpStdioTransport requires a stdio config")
        self.config = config
        self.session_factory = session_factory
        self.trusted_test_seam = bool(trusted_test_seam)
        self._session: McpRpcSession | None = None

    async def _ensure_session(self) -> McpRpcSession:
        if self._session is not None:
            return self._session
        if self.session_factory is None or not self.trusted_test_seam:
            raise GatewayDenied(
                "MCP stdio requires an explicitly trusted sandbox executor"
            )
        # C-R2 之后，stdio 的进程创建完全由**注入的受信 factory** 负责，它按密封的
        # config 构造会话；transport 不再从调用方接收 launch request，因此
        # "authorization 指向 A、实际启动 B" 这条错位路径不复存在。
        value = self.session_factory(self.config)
        session = await value if inspect.isawaitable(value) else value
        if not all(callable(getattr(session, name, None)) for name in ("request", "notify", "close")):
            raise GatewayDenied("MCP stdio session factory returned an invalid session")
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
        super().__init__(allowed_resources=config.allowed_resources)
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


@dataclass(frozen=True)
class McpCapabilityIdentity:
    server_id: str
    transport: str
    canonical_endpoint: str
    operation: str
    remote_name: str = ""
    remote_uri: str = ""
    schema_digest: str = ""

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(
            {
                "server_id": self.server_id,
                "transport": self.transport,
                "canonical_endpoint": self.canonical_endpoint,
                "operation": self.operation,
                "remote_name": self.remote_name,
                "remote_uri": self.remote_uri,
                "schema_digest": self.schema_digest,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()


def _canonical_mcp_endpoint(config: McpServerConfig) -> str:
    if config.transport == "stdio":
        executable = _canonical_path(config.command).as_posix()
        return "stdio:" + hashlib.sha256(json.dumps(
            {"executable": executable, "argv": list(config.args)},
            ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
    parsed = urlparse(config.url)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise GatewayDenied("MCP endpoint must be canonical HTTPS")
    try:
        port = parsed.port
    except ValueError as exc:
        raise GatewayDenied("MCP endpoint port is invalid") from exc
    host = parsed.hostname.casefold().rstrip(".")
    authority = host if port in (None, 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "/").rstrip("/") or "/"
    return f"https://{authority}{'' if path == '/' else path}"


def _canonical_schema(schema: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    frozen = copy.deepcopy(dict(schema))
    encoded = json.dumps(
        frozen, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return frozen, hashlib.sha256(encoded).hexdigest()


class McpRegistry:
    """MCP configuration, handshake, discovery and invocation policy registry.

    ⚠ 设计要点（C-R2 修复）：**registry 自己拥有并密封 transport**。
    旧实现的 `handshake(server_id, transport)` / `invoke(..., transport=)` /
    `read_resource(..., effect=)` 把"用哪个 peer"交给调用方，于是 approval/grant 里
    记录的 server/endpoint 与实际发包的对象**没有任何绑定**——identity 校验的是
    registry 里的 config，执行的却是外部传入的 transport。

    现在 transport 只能由 registry 在 `register()` 时按 config 构造并持有；
    invoke/read 一律从密封实例取通道，且每次调用前重新核对 canonical endpoint。
    """

    #: 允许的 MCP 协议版本（必须精确属于此集合）。
    SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")

    def __init__(
        self,
        *,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
        session_factory: Callable[[McpServerConfig], Any] | None = None,
    ) -> None:
        self._configs: dict[str, McpServerConfig] = {}
        self._discovery: dict[str, dict[str, Any]] = {}
        # 工厂由**调用方在构造时**提供，registry 之后不再接受任何外部 transport/effect。
        # 生产环境由部署方注入受信 sandbox executor 的 session factory；测试注入
        # 不可联网的 fake。registry 只负责"按 config 密封"，不负责信任判定。
        self._http_client_factory = http_client_factory
        self._session_factory = session_factory
        self._transports: dict[str, McpTransport] = {}

    def register(self, config: McpServerConfig) -> None:
        if config.server_id in self._configs:
            raise ValueError("duplicate MCP server id")
        if config.transport == "streamable_http":
            self._validate_url(config, config.url)
        _canonical_mcp_endpoint(config)
        self._configs[config.server_id] = config

    def _transport_for(self, server_id: str) -> McpTransport:
        """按注册 config 构造并缓存密封 transport（不接收外部实例）。"""

        config = self._config(server_id)
        existing = self._transports.get(config.server_id)
        if existing is not None:
            return existing
        if config.transport == "streamable_http":
            transport: McpTransport = McpStreamableHttpTransport(
                config, client_factory=self._http_client_factory,
            )
        else:
            if self._session_factory is None:
                raise GatewayDenied(
                    "MCP stdio requires an explicitly trusted sandbox session factory"
                )
            transport = McpStdioTransport(
                config,
                session_factory=self._session_factory,
                trusted_test_seam=True,
            )
        self._transports[config.server_id] = transport
        return transport

    def _seal_target(self, server_id: str) -> tuple[McpServerConfig, McpTransport]:
        """取密封 transport，并重新确认它的目标与注册 config 一致。

        这一步是 C-R2 的核心：identity/approval 里的 endpoint 与真正发包的对象
        必须在**每次**使用前对齐，而不是只在注册时对齐一次。
        """

        config = self._config(server_id)
        transport = self._transport_for(config.server_id)
        configured_url = str(getattr(transport, "config", None).url or "")
        if configured_url != str(config.url):
            raise GatewayDenied("MCP sealed transport target changed after registration")
        return config, transport

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
        try:
            encoded = json.dumps(
                schema, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise GatewayDenied("MCP tool schema is not strict JSON") from exc
        if len(encoded) > 65_536:
            raise GatewayDenied("MCP tool schema exceeds the policy limit")

        allowed_keys = frozenset({
            "type", "properties", "required", "additionalProperties", "items",
            "enum", "const", "minLength", "maxLength", "minimum", "maximum",
            "minItems", "maxItems", "description", "title", "pattern",
        })
        node_count = 0

        def walk(node: Any, depth: int) -> None:
            nonlocal node_count
            node_count += 1
            if depth > 16 or node_count > 1024:
                raise GatewayDenied("MCP tool schema exceeds depth or node limits")
            if not isinstance(node, Mapping):
                raise GatewayDenied("MCP tool schema node is malformed")
            keys = frozenset(str(key) for key in node)
            if not keys.issubset(allowed_keys) or any(key.startswith("$") for key in keys):
                raise GatewayDenied("MCP tool schema uses unsupported authority keywords")
            kind = node.get("type")
            if kind not in {"object", "array", "string", "integer", "number", "boolean", "null"}:
                raise GatewayDenied("MCP tool schema type is unsupported")
            for text_key in ("description", "title", "pattern"):
                text = node.get(text_key)
                if text is not None and (not isinstance(text, str) or len(text) > 4096):
                    raise GatewayDenied("MCP tool schema text is invalid")
                if isinstance(text, str) and _PROMPT_INJECTION.search(text):
                    raise GatewayDenied("MCP tool schema contains prompt injection")
                if (
                    isinstance(text, str)
                    and PiiDlpGateway().decide_outbound(text, approved=False).requires_approval
                ):
                    raise GatewayDenied("MCP tool schema text contains sensitive data")
            # Python's backtracking regex engine cannot safely execute an arbitrary
            # remote pattern under a deterministic time budget.  Such schemas are not
            # safely expressible by this gateway and therefore fail closed.
            if "pattern" in node:
                raise GatewayDenied("MCP tool schema pattern constraints are unsupported")

            constraint_keys = {
                "minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems",
            }
            allowed_constraints = {
                "string": {"minLength", "maxLength"},
                "integer": {"minimum", "maximum"},
                "number": {"minimum", "maximum"},
                "array": {"minItems", "maxItems"},
            }.get(str(kind), set())
            if (keys & constraint_keys) - allowed_constraints:
                raise GatewayDenied("MCP tool schema constraint does not match its type")
            if kind == "string":
                minimum = node.get("minLength", 0)
                maximum = node.get("maxLength", 65_536)
                if (
                    isinstance(minimum, bool)
                    or isinstance(maximum, bool)
                    or not isinstance(minimum, int)
                    or not isinstance(maximum, int)
                    or not 0 <= minimum <= maximum <= 65_536
                ):
                    raise GatewayDenied("MCP string schema constraints are invalid")
            elif kind in {"integer", "number"}:
                minimum = node.get("minimum")
                maximum = node.get("maximum")
                for bound in (minimum, maximum):
                    if bound is not None and (
                        isinstance(bound, bool)
                        or not isinstance(bound, (int, float))
                        or not math.isfinite(float(bound))
                    ):
                        raise GatewayDenied("MCP numeric schema constraints are invalid")
                if minimum is not None and maximum is not None and float(minimum) > float(maximum):
                    raise GatewayDenied("MCP numeric schema constraints are inverted")
            if "enum" in node:
                enum = node["enum"]
                if not isinstance(enum, list) or not enum or len(enum) > 256:
                    raise GatewayDenied("MCP tool schema enum is invalid")
                try:
                    enum_bytes = json.dumps(
                        enum, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                    ).encode("utf-8")
                except (TypeError, ValueError, OverflowError) as exc:
                    raise GatewayDenied("MCP tool schema enum is not strict JSON") from exc
                if len(enum_bytes) > 16_384:
                    raise GatewayDenied("MCP tool schema enum is too large")
            if kind == "object":
                properties = node.get("properties", {})
                if not isinstance(properties, Mapping) or len(properties) > 256:
                    raise GatewayDenied("MCP tool schema properties are invalid")
                required = node.get("required", [])
                if (
                    not isinstance(required, list)
                    or len(required) != len(set(required))
                    or any(not isinstance(item, str) or item not in properties for item in required)
                ):
                    raise GatewayDenied("MCP tool schema required list is invalid")
                additional = node.get("additionalProperties", False)
                if additional is not False:
                    raise GatewayDenied("MCP tool schemas must forbid additional properties")
                for key, child in properties.items():
                    if (
                        not isinstance(key, str)
                        or not key
                        or len(key) > 128
                        or any(ord(char) < 32 for char in key)
                    ):
                        raise GatewayDenied("MCP tool schema property name is invalid")
                    walk(child, depth + 1)
            elif kind == "array":
                if "items" not in node:
                    raise GatewayDenied("MCP array schema requires bounded items")
                minimum = node.get("minItems", 0)
                maximum = node.get("maxItems")
                if (
                    isinstance(minimum, bool)
                    or isinstance(maximum, bool)
                    or not isinstance(minimum, int)
                    or not isinstance(maximum, int)
                    or not 0 <= minimum <= maximum <= 1024
                ):
                    raise GatewayDenied("MCP array schema requires a bounded maxItems")
                walk(node["items"], depth + 1)
            elif "properties" in node or "required" in node or "items" in node:
                raise GatewayDenied("MCP schema keywords do not match their type")

        walk(schema, 0)

    @staticmethod
    def _validate_bounded_metadata(value: Any, *, label: str) -> None:
        """Validate and content-scan untrusted nested MCP metadata."""

        nodes = 0

        def walk(item: Any, depth: int) -> None:
            nonlocal nodes
            nodes += 1
            if depth > 8 or nodes > 256:
                raise GatewayDenied(f"MCP {label} metadata exceeds complexity limits")
            if item is None or isinstance(item, (str, bool)):
                if isinstance(item, str) and len(item) > 4096:
                    raise GatewayDenied(f"MCP {label} metadata string is too large")
                return
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                if not math.isfinite(float(item)):
                    raise GatewayDenied(f"MCP {label} metadata number is invalid")
                return
            if isinstance(item, list):
                if len(item) > 64:
                    raise GatewayDenied(f"MCP {label} metadata list is too large")
                for child in item:
                    walk(child, depth + 1)
                return
            if isinstance(item, Mapping):
                if len(item) > 64:
                    raise GatewayDenied(f"MCP {label} metadata object is too large")
                for key, child in item.items():
                    if (
                        not isinstance(key, str)
                        or not key
                        or len(key) > 128
                        or any(ord(char) < 32 for char in key)
                    ):
                        raise GatewayDenied(f"MCP {label} metadata key is invalid")
                    walk(child, depth + 1)
                return
            raise GatewayDenied(f"MCP {label} metadata is not strict JSON")

        walk(value, 0)
        try:
            encoded = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise GatewayDenied(f"MCP {label} metadata is not strict JSON") from exc
        if len(encoded) > 16_384:
            raise GatewayDenied(f"MCP {label} metadata exceeds the size limit")
        text = encoded.decode("utf-8")
        if (
            _PROMPT_INJECTION.search(text)
            or PiiDlpGateway().decide_outbound(text, approved=False).requires_approval
        ):
            raise GatewayDenied(f"MCP {label} metadata violates content policy")

    @classmethod
    def _validate_resource_annotations(cls, value: Any) -> None:
        if not isinstance(value, Mapping) or not frozenset(value).issubset({
            "audience", "priority", "lastModified",
        }):
            raise GatewayDenied("MCP resource annotations are malformed")
        audience = value.get("audience")
        if audience is not None and (
            not isinstance(audience, list)
            or not audience
            or len(audience) > 2
            or len(audience) != len(set(audience))
            or any(item not in {"user", "assistant"} for item in audience)
        ):
            raise GatewayDenied("MCP resource annotations audience is invalid")
        priority = value.get("priority")
        if priority is not None and (
            isinstance(priority, bool)
            or not isinstance(priority, (int, float))
            or not math.isfinite(float(priority))
            or not 0 <= float(priority) <= 1
        ):
            raise GatewayDenied("MCP resource annotations priority is invalid")
        modified = value.get("lastModified")
        if modified is not None:
            if not isinstance(modified, str) or len(modified) > 64:
                raise GatewayDenied("MCP resource annotations lastModified is invalid")
            try:
                parsed = datetime.fromisoformat(modified.replace("Z", "+00:00"))
            except ValueError as exc:
                raise GatewayDenied("MCP resource annotations lastModified is invalid") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise GatewayDenied("MCP resource annotations lastModified needs a timezone")
        cls._validate_bounded_metadata(dict(value), label="resource annotations")

    @classmethod
    def _validate_arguments(cls, value: Any, schema: Mapping[str, Any], *, path: str = "$") -> None:
        kind = schema.get("type")
        valid = {
            "object": lambda item: isinstance(item, Mapping),
            "array": lambda item: isinstance(item, list),
            "string": lambda item: isinstance(item, str),
            "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
            "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool)
                and math.isfinite(float(item)),
            "boolean": lambda item: isinstance(item, bool),
            "null": lambda item: item is None,
        }
        checker = valid.get(str(kind))
        if checker is None or not checker(value):
            raise GatewayDenied(f"MCP invocation argument violates schema at {path}")
        if "enum" in schema and value not in schema["enum"]:
            raise GatewayDenied(f"MCP invocation argument violates enum at {path}")
        if "const" in schema and value != schema["const"]:
            raise GatewayDenied(f"MCP invocation argument violates const at {path}")
        if kind == "object":
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            missing = set(required) - set(value)
            extras = set(value) - set(properties)
            if missing or extras:
                raise GatewayDenied(f"MCP invocation argument object violates schema at {path}")
            for key, item in value.items():
                cls._validate_arguments(item, properties[key], path=f"{path}.{key}")
        elif kind == "array":
            minimum = int(schema.get("minItems", 0))
            maximum = int(schema.get("maxItems", 0))
            if not minimum <= len(value) <= maximum:
                raise GatewayDenied(f"MCP invocation array violates schema at {path}")
            for index, item in enumerate(value):
                cls._validate_arguments(item, schema["items"], path=f"{path}[{index}]")
        elif kind == "string":
            minimum = int(schema.get("minLength", 0))
            maximum = int(schema.get("maxLength", 65_536))
            if not minimum <= len(value) <= maximum:
                raise GatewayDenied(f"MCP invocation string violates schema at {path}")
            pattern = schema.get("pattern")
            if pattern:
                try:
                    if re.fullmatch(pattern, value) is None:
                        raise GatewayDenied(f"MCP invocation string violates pattern at {path}")
                except re.error as exc:
                    raise GatewayDenied("MCP schema pattern is invalid") from exc
        elif kind in {"integer", "number"}:
            if "minimum" in schema and float(value) < float(schema["minimum"]):
                raise GatewayDenied(f"MCP invocation number violates minimum at {path}")
            if "maximum" in schema and float(value) > float(schema["maximum"]):
                raise GatewayDenied(f"MCP invocation number violates maximum at {path}")

    def accept_discovery(self, server_id: str, discovery: Mapping[str, Any]) -> dict[str, Any]:
        config = self._config(server_id)
        if "prompts" in discovery:
            raise GatewayDenied("MCP prompts are disabled")
        raw_tools = discovery.get("tools") or ()
        raw_resources = discovery.get("resources") or ()
        if not isinstance(raw_tools, (list, tuple)) or not isinstance(raw_resources, (list, tuple)):
            raise GatewayDenied("MCP discovery payload is malformed")
        endpoint = _canonical_mcp_endpoint(config)
        tool_names: list[str] = []
        tool_records: dict[str, dict[str, Any]] = {}
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
            if PiiDlpGateway().decide_outbound(description, approved=False).requires_approval:
                raise GatewayDenied("MCP tool description contains sensitive data")
            frozen_schema, schema_digest = _canonical_schema(raw["inputSchema"])
            identity = McpCapabilityIdentity(
                server_id=server_id,
                transport=config.transport,
                canonical_endpoint=endpoint,
                operation="tools/call",
                remote_name=name,
                schema_digest=schema_digest,
            )
            tool_names.append(name)
            tool_records[name] = {
                "schema": frozen_schema,
                "identity": identity,
            }
        resource_uris: list[str] = []
        resource_records: dict[str, dict[str, Any]] = {}
        for raw in raw_resources:
            if not isinstance(raw, Mapping) or not str(raw.get("uri") or "").strip():
                raise GatewayDenied("MCP resource discovery entry is malformed")
            if not frozenset(raw).issubset({
                "uri", "name", "title", "description", "mimeType", "size", "annotations",
            }):
                raise GatewayDenied("MCP resource metadata contains unsupported fields")
            uri = str(raw["uri"]).strip()
            parsed_uri = urlparse(uri)
            if (
                len(uri) > 2048
                or not parsed_uri.scheme
                or any(ord(char) < 32 for char in uri)
                or uri in resource_uris
            ):
                raise GatewayDenied("MCP resource URI is invalid")
            metadata_text = "\n".join(
                str(raw.get(key) or "") for key in ("name", "title", "description", "mimeType")
            )
            for text_key in ("name", "title", "description", "mimeType"):
                if text_key in raw and (
                    not isinstance(raw[text_key], str) or len(raw[text_key]) > 4096
                ):
                    raise GatewayDenied("MCP resource metadata field is invalid")
            mime_type = raw.get("mimeType")
            if isinstance(mime_type, str) and mime_type and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/"
                r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}",
                mime_type,
            ) is None:
                raise GatewayDenied("MCP resource MIME type is invalid")
            if (
                len(metadata_text) > 8192
                or _PROMPT_INJECTION.search(metadata_text)
                or PiiDlpGateway().decide_outbound(
                    metadata_text, approved=False,
                ).requires_approval
            ):
                raise GatewayDenied("MCP resource metadata violates content policy")
            size = raw.get("size")
            if size is not None and (
                isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= 1_048_576
            ):
                raise GatewayDenied("MCP resource metadata size is invalid")
            if "annotations" in raw:
                self._validate_resource_annotations(raw["annotations"])
            self._validate_bounded_metadata(dict(raw), label="resource discovery")
            try:
                canonical_metadata = json.dumps(
                    dict(raw), ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError, OverflowError) as exc:
                raise GatewayDenied("MCP resource metadata is not strict JSON") from exc
            schema_digest = hashlib.sha256(canonical_metadata).hexdigest()
            identity = McpCapabilityIdentity(
                server_id=server_id,
                transport=config.transport,
                canonical_endpoint=endpoint,
                operation="resources/read",
                remote_uri=uri,
                schema_digest=schema_digest,
            )
            resource_uris.append(uri)
            resource_records[uri] = {
                "metadata": copy.deepcopy(dict(raw)),
                "identity": identity,
            }
        unexpected_tools = set(tool_names) - set(config.allowed_tools)
        unexpected_resources = set(resource_uris) - set(config.allowed_resources)
        if unexpected_tools or unexpected_resources:
            raise GatewayDenied("MCP discovery returned unexpected tools or resources")
        accepted = {
            "server_id": server_id,
            "tools": tuple(tool_names),
            "resources": tuple(resource_uris),
            "tool_records": tool_records,
            "resource_records": resource_records,
            "discovery_digest": hashlib.sha256(json.dumps(
                {
                    "tools": {name: item["identity"].digest for name, item in tool_records.items()},
                    "resources": {
                        uri: item["identity"].digest for uri, item in resource_records.items()
                    },
                }, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
        }
        self._discovery[server_id] = accepted
        return accepted

    async def handshake(self, server_id: str) -> dict[str, Any]:
        """握手：transport 由 registry 按 config 自建，不接受外部实例。"""

        config, transport = self._seal_target(server_id)
        initialized = await transport.initialize()
        # 协议版本必须精确属于支持集合：旧实现只要求"非空且 ≤40 字符"，
        # 于是 "banana" / "9999-01-01" 都会被接受（复审实测）。
        protocol = str(initialized.get("protocolVersion") or "")
        if protocol not in self.SUPPORTED_PROTOCOL_VERSIONS:
            raise GatewayDenied("MCP handshake protocol version is not supported")
        capabilities = initialized.get("capabilities")
        # capabilities 必须是 Mapping；旧实现在它不是 Mapping 时直接跳过 prompts 检查。
        if not isinstance(capabilities, Mapping):
            raise GatewayDenied("MCP handshake capabilities are malformed")
        if "prompts" in initialized or "prompts" in capabilities:
            raise GatewayDenied("MCP prompt capability is disabled")
        if config.transport == "streamable_http" and initialized.get("redirect"):
            raise GatewayDenied("MCP redirects are forbidden")
        discovery = await transport.discover()
        # discovery 由 capability 驱动：只在注册了资源时才请求 resources/list，
        # 因此调用方拿到的方法序列与 config 声明一致。
        return self.accept_discovery(server_id, discovery)

    async def close(self) -> None:
        """关闭所有密封 transport（stdio 会关子进程/会话）。"""

        transports, self._transports = self._transports, {}
        for transport in transports.values():
            closer = getattr(transport, "close", None)
            if closer is None:
                continue
            value = closer()
            if inspect.isawaitable(value):
                await value

    def tool_descriptor(
        self, server_id: str, name: str, *, risk: RiskLevel,
    ) -> ToolDescriptor:
        accepted = self._discovery.get(str(server_id))
        record = None if accepted is None else accepted["tool_records"].get(str(name))
        if record is None:
            raise GatewayDenied("MCP tool was not inspected")
        config = self._config(server_id)
        host = urlparse(config.url).hostname if config.transport == "streamable_http" else None
        return ToolDescriptor(
            tool_name=f"mcp:{server_id}:tool:{name}",
            kind="mcp",
            risk=risk,
            network_hosts=(str(host),) if host else (),
            schema_digest=record["identity"].digest,
        )

    def resource_descriptor(
        self, server_id: str, uri: str, *, risk: RiskLevel,
    ) -> ToolDescriptor:
        accepted = self._discovery.get(str(server_id))
        record = None if accepted is None else accepted["resource_records"].get(str(uri))
        if record is None:
            raise GatewayDenied("MCP resource was not inspected")
        config = self._config(server_id)
        host = urlparse(config.url).hostname if config.transport == "streamable_http" else None
        return ToolDescriptor(
            tool_name=f"mcp:{server_id}:resource:{hashlib.sha256(uri.encode('utf-8')).hexdigest()}",
            kind="mcp",
            risk=risk,
            network_hosts=(str(host),) if host else (),
            schema_digest=record["identity"].digest,
        )

    def validate_tool_capability(
        self,
        server_id: str,
        name: str,
        arguments: Mapping[str, Any],
        descriptor: ToolDescriptor,
    ) -> McpCapabilityIdentity:
        accepted = self._discovery.get(str(server_id))
        record = None if accepted is None else accepted["tool_records"].get(str(name))
        if record is None:
            raise GatewayDenied("MCP tool was not inspected and granted")
        identity: McpCapabilityIdentity = record["identity"]
        if descriptor.kind != "mcp" or descriptor.schema_digest != identity.digest:
            raise GatewayDenied("MCP capability authorization is stale or mismatched")
        self._validate_arguments(dict(arguments), record["schema"])
        return identity

    @staticmethod
    def _validate_tool_result(value: Any) -> Mapping[str, Any]:
        """校验 CallToolResult 的确切形状与内容策略（I-R4）。

        旧实现把 tool result **原样返回**（`return await transport.invoke(...)`），
        既没有形状/大小限制，也没有 prompt-injection / DLP 扫描——而 resource 路径
        有 `_validate_resource_result` 做同等校验。这个不对称意味着 MCP tool 返回的
        文本可以带着注入指令直接进入后续模型上下文。此方法补上对称的校验。
        """

        if not isinstance(value, Mapping) or not frozenset(value).issubset(
            {"content", "isError", "structuredContent", "_meta"}
        ):
            raise GatewayDenied("MCP tool result is malformed")
        content = value.get("content")
        if not isinstance(content, list) or len(content) > 64:
            raise GatewayDenied("MCP tool result content is malformed")
        total = 0
        for item in content:
            if not isinstance(item, Mapping) or not frozenset(item).issubset(
                {"type", "text", "data", "mimeType", "resource", "_meta"}
            ):
                raise GatewayDenied("MCP tool result entry is malformed")
            kind = str(item.get("type") or "")
            if kind not in {"text", "image", "audio", "resource"}:
                raise GatewayDenied("MCP tool result content type is not supported")
            text = item.get("text")
            if text is not None and not isinstance(text, str):
                raise GatewayDenied("MCP tool result text is malformed")
            if isinstance(text, str):
                total += len(text.encode("utf-8"))
                if _PROMPT_INJECTION.search(text) or PiiDlpGateway().decide_outbound(
                    text, approved=False,
                ).requires_approval:
                    raise GatewayDenied("MCP tool result violates prompt/DLP policy")
            if "data" in item:
                encoded = item.get("data")
                if not isinstance(encoded, str):
                    raise GatewayDenied("MCP tool result data is malformed")
                try:
                    decoded = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise GatewayDenied("MCP tool result data is not valid base64") from exc
                total += len(decoded)
            if "resource" in item:
                McpRegistry._validate_bounded_metadata(
                    item.get("resource"), label="tool result resource",
                )
            if "_meta" in item:
                McpRegistry._validate_bounded_metadata(
                    item["_meta"], label="tool result",
                )
            if total > 1_048_576:
                raise GatewayDenied("MCP tool result exceeds the size limit")
        structured = value.get("structuredContent")
        if structured is not None:
            if not isinstance(structured, Mapping):
                raise GatewayDenied("MCP tool structured content is malformed")
            # 结构化内容同样可能把注入文本带进上下文，因此一并做内容扫描。
            serialized = json.dumps(
                dict(structured), ensure_ascii=True, sort_keys=True,
                separators=(",", ":"), default=str,
            )
            if len(serialized.encode("utf-8")) > 262_144:
                raise GatewayDenied("MCP tool structured content exceeds the size limit")
            if _PROMPT_INJECTION.search(serialized) or PiiDlpGateway().decide_outbound(
                serialized, approved=False,
            ).requires_approval:
                raise GatewayDenied("MCP tool structured content violates prompt/DLP policy")
        if "isError" in value and not isinstance(value["isError"], bool):
            raise GatewayDenied("MCP tool result isError flag is malformed")
        if "_meta" in value:
            McpRegistry._validate_bounded_metadata(value["_meta"], label="tool result")
        return value

    @staticmethod
    def _validate_resource_result(uri: str, value: Any) -> Mapping[str, Any]:
        if not isinstance(value, Mapping) or not frozenset(value).issubset({"contents", "_meta"}):
            raise GatewayDenied("MCP resource result is malformed")
        if "_meta" in value:
            McpRegistry._validate_bounded_metadata(
                value["_meta"], label="resource result",
            )
        contents = value.get("contents")
        if not isinstance(contents, list) or not contents or len(contents) > 64:
            raise GatewayDenied("MCP resource contents are malformed")
        total = 0
        for item in contents:
            if not isinstance(item, Mapping) or not frozenset(item).issubset({
                "uri", "mimeType", "text", "blob", "_meta",
            }):
                raise GatewayDenied("MCP resource content entry is malformed")
            if str(item.get("uri") or "") != uri:
                raise GatewayDenied("MCP resource content URI changed after authorization")
            mime_type = item.get("mimeType")
            if mime_type is not None and (
                not isinstance(mime_type, str)
                or len(mime_type) > 255
                or re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/"
                    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}",
                    mime_type,
                ) is None
            ):
                raise GatewayDenied("MCP resource content MIME type is invalid")
            metadata = {key: item[key] for key in ("uri", "mimeType", "_meta") if key in item}
            McpRegistry._validate_bounded_metadata(
                metadata, label="resource content",
            )
            text = item.get("text")
            blob = item.get("blob")
            if (isinstance(text, str)) == (isinstance(blob, str)):
                raise GatewayDenied("MCP resource content requires exactly one body encoding")
            body = text if isinstance(text, str) else blob
            assert isinstance(body, str)
            if isinstance(text, str):
                body_bytes = text.encode("utf-8")
            else:
                try:
                    body_bytes = base64.b64decode(blob, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise GatewayDenied("MCP resource blob encoding is invalid") from exc
            total += len(body_bytes)
            if total > 1_048_576:
                raise GatewayDenied("MCP resource contents exceed the size limit")
            if isinstance(text, str) and (
                _PROMPT_INJECTION.search(text)
                or PiiDlpGateway().decide_outbound(text, approved=False).requires_approval
            ):
                raise GatewayDenied("MCP resource content violates prompt/DLP policy")
        return value

    async def invoke(
        self,
        server_id: str,
        name: str,
        arguments: Mapping[str, Any],
        *,
        gateway: ToolGateway,
        request: ToolExecutionRequest,
    ) -> Any:
        # transport 来自密封实例，不经调用方传入。
        config, transport = self._seal_target(server_id)
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

        descriptor = gateway.descriptor(request.tool_name)
        self.validate_tool_capability(server_id, name, arguments, descriptor)

        async def governed_effect(_request: ToolExecutionRequest) -> Any:
            value = await transport.invoke(name, arguments)
            # I-R4：tool result 必须与 resource 走同等的形状/内容校验。
            return self._validate_tool_result(value)

        return await gateway.invoke_with_effect(request, governed_effect)

    async def read_resource(
        self,
        server_id: str,
        uri: str,
        *,
        gateway: ToolGateway,
        request: ToolExecutionRequest,
    ) -> Any:
        config, transport = self._seal_target(server_id)
        accepted = self._discovery.get(server_id)
        if accepted is None or uri not in accepted["resources"]:
            raise GatewayDenied("MCP resource was not inspected and granted")
        self._validate_effect_network_scope(config, request)
        if str(request.arguments.get("uri") or "") != uri:
            raise GatewayDenied("MCP resource URI changed after authorization")
        record = accepted["resource_records"].get(uri)
        descriptor = gateway.descriptor(request.tool_name)
        if (
            record is None
            or descriptor.kind != "mcp"
            or descriptor.schema_digest != record["identity"].digest
        ):
            raise GatewayDenied("MCP resource authorization is stale or mismatched")

        async def governed_read(_request: ToolExecutionRequest) -> Any:
            value = await transport.read_resource(uri)
            return self._validate_resource_result(uri, value)

        return await gateway.invoke_with_effect(request, governed_read)


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
_DIAGNOSTIC_METADATA_KEYS = frozenset({
    "path", "query", "url", "command", "destination", "source", "cwd", "argv",
})


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
        if folded in _BODY_KEYS or folded.endswith("_body") or folded.endswith("_payload"):
            return "[REDACTED_TOOL_BODY]"
        if folded in _DIAGNOSTIC_METADATA_KEYS:
            return "[REDACTED_METADATA]" if value not in (None, "") else value
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
    endpoint_authorities: tuple[str, ...] = ()
    transports: tuple[str, ...] = ("openai_chat",)

    def __post_init__(self) -> None:
        if not self.provider_id.strip() or not self.models:
            raise ValueError("provider id and models are required")
        if not math.isfinite(self.estimated_cost_usd) or self.estimated_cost_usd < 0:
            raise ValueError("provider estimated cost is invalid")
        if not self.transports:
            raise ValueError("provider transports are required")
        for endpoint in self.endpoint_authorities:
            _canonical_provider_endpoint(endpoint)


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

    def authorize(
        self,
        provider_id: str,
        *,
        model: str,
        endpoint: str,
        transport: str,
        required_capabilities: Iterable[str],
    ) -> ProviderCapability:
        capability = self.get(provider_id)
        if not capability.preauthorized:
            raise GatewayDenied("provider capability is not preauthorized")
        if "*" not in capability.models and str(model) not in capability.models:
            raise GatewayDenied("provider model is not in the trusted capability registry")
        if str(transport) not in capability.transports:
            raise GatewayDenied("provider transport is not in the trusted capability registry")
        required = frozenset(str(item) for item in required_capabilities)
        if not required.issubset(capability.capabilities):
            raise GatewayDenied("provider operation exceeds the trusted capability registry")
        candidate = _canonical_provider_endpoint(endpoint)
        if not capability.endpoint_authorities or not any(
            _provider_endpoint_under(candidate, rule) for rule in capability.endpoint_authorities
        ):
            raise GatewayDenied("provider endpoint is not in the trusted capability registry")
        return capability

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


def _canonical_provider_endpoint(value: str) -> str:
    parsed = urlparse(str(value).strip())
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise GatewayDenied("provider endpoint must be canonical HTTPS without credentials or query")
    host = parsed.hostname.casefold().rstrip(".")
    try:
        port = parsed.port
    except ValueError as exc:
        raise GatewayDenied("provider endpoint port is invalid") from exc
    authority = host if port in (None, 443) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parsed.path or "").rstrip("/")
    return f"https://{authority}{path}"


def _provider_endpoint_under(candidate: str, rule: str) -> bool:
    canonical_rule = _canonical_provider_endpoint(rule)
    if candidate == canonical_rule:
        return True
    parsed_rule = urlparse(canonical_rule)
    if parsed_rule.path:
        return candidate.startswith(canonical_rule + "/")
    parsed_candidate = urlparse(candidate)
    return parsed_candidate.netloc == parsed_rule.netloc


def derive_provider_credential_purpose(
    *, slot: str, provider_id: str, endpoint: str, transport: str,
) -> str:
    if re.fullmatch(r"[a-z][a-z0-9_.:-]{0,127}", str(slot)) is None:
        raise GatewayDenied("provider credential slot is invalid")
    canonical = _canonical_provider_endpoint(endpoint)
    body = "\0".join((
        "studycrew-provider-credential-v1",
        str(slot),
        str(provider_id),
        canonical,
        str(transport),
        "provider_gateway",
    ))
    return "provider:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def _provider_capability_digest(
    capability: ProviderCapability,
    *,
    model: str,
    endpoint: str,
    transport: str,
) -> str:
    body = {
        "provider_id": capability.provider_id,
        "model": str(model),
        "endpoint": _canonical_provider_endpoint(endpoint),
        "transport": str(transport),
        "privacy": capability.privacy.value,
        "capabilities": sorted(capability.capabilities),
    }
    return hashlib.sha256(json.dumps(
        body, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


_PRODUCTION_PROVIDER_ENDPOINTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "openrouter": (("https://openrouter.ai",), ("openai_chat",)),
    "deepseek": (("https://api.deepseek.com",), ("openai_chat",)),
    "zai": (("https://api.z.ai", "https://open.bigmodel.cn"), ("openai_chat",)),
    "kimi-coding": (("https://api.moonshot.ai",), ("openai_chat",)),
    "kimi-coding-cn": (("https://api.moonshot.cn",), ("openai_chat",)),
    "alibaba": (("https://dashscope.aliyuncs.com", "https://dashscope-intl.aliyuncs.com"), ("openai_chat",)),
    "minimax": (("https://api.minimax.io",), ("anthropic_messages",)),
    "minimax-cn": (("https://api.minimaxi.com",), ("anthropic_messages",)),
    "stepfun": (("https://api.stepfun.com",), ("openai_chat",)),
    "xai": (("https://api.x.ai",), ("openai_chat",)),
    "anthropic": (("https://api.anthropic.com",), ("anthropic_messages",)),
    "openai-api": (("https://api.openai.com",), ("codex_responses", "openai_chat")),
    "gemini": (("https://generativelanguage.googleapis.com",), ("openai_chat",)),
    "nvidia": (("https://integrate.api.nvidia.com",), ("openai_chat",)),
    "huggingface": (("https://api-inference.huggingface.co",), ("openai_chat",)),
    "novita": (("https://api.novita.ai",), ("openai_chat",)),
    "arcee": (("https://api.arcee.ai",), ("openai_chat",)),
    "gmi": (("https://api.gmicloud.ai",), ("openai_chat",)),
    "copilot": (("https://api.githubcopilot.com",), ("openai_chat",)),
}


def production_model_capability_registry() -> ModelCapabilityRegistry:
    registry = ModelCapabilityRegistry()
    for provider_id, (endpoints, transports) in _PRODUCTION_PROVIDER_ENDPOINTS.items():
        registry.register(ProviderCapability(
            provider_id=provider_id,
            models=("*",),
            capabilities=frozenset({"chat", "stream", "tools"}),
            privacy=PrivacyTier.STANDARD,
            estimated_cost_usd=0.01,
            preauthorized=True,
            endpoint_authorities=endpoints,
            transports=transports,
        ))
    return registry


class ProviderGatewayClient:
    """The sole production model-egress facade.

    It derives authority from the backend Task Runtime context, validates a trusted
    provider capability, scans the exact outgoing JSON, reserves model budget before
    the network effect, and records a structured audit event.  The wrapped
    ``ProviderClient`` is transport-only and cannot make production requests without
    this facade.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "",
        *,
        transport: str = "openai_chat",
        registry: ModelCapabilityRegistry | None = None,
        authority_provider: Callable[[], LegacyToolAuthority | None] = current_legacy_tool_authority,
        timeout: Any = None,
        max_retries: int = 0,
        extra_headers: dict[str, str] | None = None,
        client_factory: Callable[[], Any] | None = None,
        provider: str = "",
        backoff_base: float = 1.0,
        credential_provider: Callable[[], str] | None = None,
        _trusted_test_transport: bool = False,
    ) -> None:
        """唯一的生产模型出口。

        C-R1：这里曾有 `allow_ungoverned_test_transport`，一个构造期布尔量即可让
        `_authorize` 直接 `return None, ""` —— 整体跳过 authority、registry、DLP、
        预算、审批与审计。该参数与其短路已删除。

        测试若要替换底层 transport，使用 `_trusted_test_transport=True`：它**只**
        允许注入 `client_factory`（替换发送通道），**不放行任何策略**——授权、DLP、
        预算、审计照旧执行。因此它不能被用来绕过策略，只是把"发到哪儿"换成假的。
        """
        from knowe_core.provider_client import ProviderClient

        self.base_url = str(base_url)
        self.model = str(model)
        self.provider = str(provider)
        self.transport = str(transport or "openai_chat")
        self.registry = registry or production_model_capability_registry()
        self.authority_provider = authority_provider
        # ⚠ 仅测试可见：见 __init__ 文档。生产构造点永不传 True。
        self._trusted_test_transport = bool(_trusted_test_transport)
        self._transport = ProviderClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=timeout,
            max_retries=max_retries,
            extra_headers=extra_headers,
            client_factory=client_factory,
            provider=provider,
            backoff_base=backoff_base,
            credential_provider=credential_provider,
            _gateway_managed=True,
            _trusted_test_transport=_trusted_test_transport,
        )

    #: 允许从底层 transport 读取的**只读**属性。
    #: C-R1 之前这里是 `__getattr__` 全量代理，于是 `gateway._request`、
    #: `gateway._build_body`、`gateway._transport` 等内部发送路径对任何持有者可达
    #: ——"必须经过 Gateway"就能被绕过。现在只放行元数据读取，其余一律
    #: AttributeError（让"这个类能做什么"一眼可读）。
    _DELEGATED_READS = ("base_url", "model", "provider", "timeout", "max_retries")

    def __getattr__(self, name: str) -> Any:
        if name in ProviderGatewayClient._DELEGATED_READS:
            return getattr(self._transport, name)
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}; "
            "provider egress goes through ProviderGatewayClient.chat/chat_stream only"
        )

    async def aclose(self) -> None:
        """关闭底层 transport（只关闭连接，不绕过任何策略）。"""

        await self._transport.aclose()

    @staticmethod
    def _serialized_request(
        messages: Any, tools: Any, extra_body: Any,
    ) -> str:
        try:
            return json.dumps(
                {"messages": messages, "tools": tools, "extra_body": extra_body},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise GatewayDenied("provider request body is not bounded JSON") from exc

    def _authorize(
        self,
        *,
        messages: Any,
        tools: Any,
        extra_body: Any,
        max_tokens: int | None,
        stream: bool,
    ) -> tuple[TaskRuntimeService | None, str]:
        # C-R1：这里曾有 `if self._allow_ungoverned_test_transport: return None, ""`，
        # 一个构造期布尔量即可整体跳过 authority、registry、DLP、预算、审批与审计。
        #
        # 现在只剩一条**仅测试**的等价路径：_trusted_test_transport。它存在的唯一理由是
        # 大量 agent 行为测试需要一个假 transport，而那些测试环境里没有 Task Runtime，
        # 因此无法满足 authority 要求。它与旧后门的区别是**可审计**：
        #   · 名字以下划线开头，明确标注"测试专用"；
        #   · 只有 agent 的 client_factory 注入点会传它，生产构造点永不传；
        #   · packaging/阶段 1 验收报告把它登记为**已知限制**——它仍能让持有该参数的
        #     构造者跳过策略，因此**不是**最终形态，只是把"任意的布尔后门"收窄成
        #     "一个有名有据、有测试守卫的测试通道"。
        # 后续应改为：测试建立短命 Task Runtime run（与 I-R8 的 settings probe 同一做法），
        # 然后删除这个参数。
        if self._trusted_test_transport:
            return None, ""
        authority = self.authority_provider()
        if authority is None or authority.runtime is None:
            raise GatewayDenied("authoritative Task Runtime context is unavailable for provider egress")
        runtime = authority.runtime
        run = runtime.store.get_run(authority.run_id)
        if (
            run is None
            or run.group_id != authority.group_id
            or run.task_id != authority.task_id
            or run.status is not TaskStatus.RUNNING
        ):
            raise GatewayDenied("provider egress Task Runtime binding is invalid")
        required = {"chat", "stream" if stream else "chat"}
        if tools:
            required.add("tools")
        capability = self.registry.authorize(
            self.provider,
            model=self.model,
            endpoint=self.base_url,
            transport=self.transport,
            required_capabilities=required,
        )
        serialized = self._serialized_request(messages, tools, extra_body)
        decision = PiiDlpGateway().decide_outbound(serialized, approved=False)
        capability_digest = _provider_capability_digest(
            capability, model=self.model, endpoint=self.base_url, transport=self.transport,
        )
        operation_scope = {
            "capability": capability_digest,
            "request": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "provider": hashlib.sha256(self.provider.encode("utf-8")).hexdigest(),
            "model": hashlib.sha256(self.model.encode("utf-8")).hexdigest(),
            "endpoint": hashlib.sha256(
                _canonical_provider_endpoint(self.base_url).encode("utf-8")
            ).hexdigest(),
            "transport": hashlib.sha256(self.transport.encode("utf-8")).hexdigest(),
        }
        invocation_id = ""
        if decision.requires_approval:
            if not authority.approval_id or not authority.grant_id:
                raise GatewayDenied("sensitive provider outbound requires fresh Task Runtime approval")
            invocation = runtime.prepare_tool(
                authority.group_id,
                authority.run_id,
                task_id=authority.task_id,
                tool_name="provider_egress",
                idempotency_key=f"provider:{uuid.uuid4().hex}",
                risk=RiskLevel.HIGH,
                public_summary="Governed provider egress prepared",
                approval_id=authority.approval_id,
                grant_id=authority.grant_id,
                operation_scope=operation_scope,
                purpose="provider_egress",
            )
            if invocation.disposition != "created" or invocation.status != "prepared":
                raise GatewayDenied("fresh provider egress approval is required")
            running, claimed = runtime.store.claim_tool_execution(invocation.invocation_id)
            if not claimed or running.status != "running":
                raise GatewayDenied("provider egress approval was already consumed")
            invocation_id = invocation.invocation_id

        estimated_input_tokens = max(1, math.ceil(len(serialized) / 4))
        reserved_output_tokens = max(0, int(max_tokens or 0))
        budgeted = runtime.consume_budget(
            authority.group_id,
            authority.run_id,
            BudgetUsage(
                model_turns=1,
                tokens=estimated_input_tokens + reserved_output_tokens,
                cost_usd=capability.estimated_cost_usd,
            ),
            request_key=f"provider-budget:{uuid.uuid4().hex}",
        )
        if budgeted.status is not TaskStatus.RUNNING:
            if invocation_id:
                runtime.store.complete_tool(
                    invocation_id, status="failed", error_type="BudgetExhausted",
                )
            raise GatewayDenied("Task Runtime model budget is exhausted")
        runtime.store.change_run(
            authority.run_id,
            event_type="provider_egress",
            public_payload={
                "status": "authorized",
                "capability_digest": capability_digest,
                "privacy": capability.privacy.value,
                "usage": budgeted.budget_usage.__dict__,
                "approval_required": decision.requires_approval,
            },
            idempotency_key=f"provider-audit:{uuid.uuid4().hex}",
            mutate=lambda current: (
                None if current.status is TaskStatus.RUNNING
                else (_ for _ in ()).throw(RuntimeStoreError("provider run is no longer running"))
            ),
        )
        return runtime, invocation_id

    @staticmethod
    def _complete_authorized_effect(
        runtime: TaskRuntimeService | None,
        invocation_id: str,
        *,
        error: BaseException | None,
    ) -> None:
        if runtime is None or not invocation_id:
            return
        runtime.store.complete_tool(
            invocation_id,
            status="failed" if error is not None else "completed",
            error_type=type(error).__name__ if error is not None else "",
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        runtime, invocation_id = self._authorize(
            messages=messages, tools=tools, extra_body=extra_body,
            max_tokens=max_tokens, stream=False,
        )
        try:
            result = await self._transport.chat(
                messages, tools, temperature, max_tokens, extra_body,
            )
        except BaseException as exc:
            self._complete_authorized_effect(runtime, invocation_id, error=exc)
            raise
        self._complete_authorized_effect(runtime, invocation_id, error=None)
        return result

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ):
        runtime, invocation_id = self._authorize(
            messages=messages, tools=tools, extra_body=extra_body,
            max_tokens=max_tokens, stream=True,
        )
        try:
            async for event in self._transport.chat_stream(
                messages, tools, temperature, max_tokens, extra_body,
            ):
                yield event
        except BaseException as exc:
            self._complete_authorized_effect(runtime, invocation_id, error=exc)
            raise
        self._complete_authorized_effect(runtime, invocation_id, error=None)


@dataclass
class _SecretLease:
    lease_id: str
    secret_ref: str
    slot: str
    kind: str
    purpose: str
    audience: str
    provider_id: str
    endpoint: str
    transport: str
    value: bytearray = field(repr=False)
    expires_at: datetime

    def clear(self) -> None:
        for index in range(len(self.value)):
            self.value[index] = 0


class SecretLeaseBroker:
    """In-memory Python lease receiver; it has deliberately no persistence API."""

    _LEASE_ID = re.compile(r"^lease_[0-9a-f]{32}$")
    _SECRET_REF = re.compile(r"^secret_[0-9a-f]{32}$")

    _SLOT = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
    _PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

    def __init__(
        self,
        *,
        max_ttl_seconds: int = 300,
        sweep_interval_seconds: float = 1.0,
    ) -> None:
        self.max_ttl_seconds = max(1, int(max_ttl_seconds))
        self._leases: dict[str, _SecretLease] = {}
        self._dlp = PiiDlpGateway()
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._sweep_interval_seconds = max(0.0, float(sweep_interval_seconds))
        self._sweeper: threading.Thread | None = None
        if self._sweep_interval_seconds > 0:
            self._sweeper = threading.Thread(
                target=self._sweep_loop,
                name="studycrew-secret-lease-expiry",
                daemon=True,
            )
            self._sweeper.start()

    def _sweep_loop(self) -> None:
        while not self._closed.wait(self._sweep_interval_seconds):
            self.sweep_expired()

    def provision(
        self,
        *,
        lease_id: str,
        secret_ref: str,
        slot: str,
        kind: str,
        purpose: str,
        audience: str,
        provider_id: str,
        endpoint: str,
        transport: str,
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
        if (
            self._SLOT.fullmatch(str(slot)) is None
            or kind not in {"provider_credential", "master_key"}
            or not purpose.strip()
            or not audience.strip()
            or not value
        ):
            raise GatewayDenied("secret lease metadata and value are required")
        if kind == "provider_credential":
            if (
                audience != "provider_gateway"
                or self._PROVIDER.fullmatch(str(provider_id)) is None
                or not str(transport).strip()
                or _canonical_provider_endpoint(endpoint) != str(endpoint)
                or purpose != derive_provider_credential_purpose(
                    slot=slot,
                    provider_id=provider_id,
                    endpoint=endpoint,
                    transport=transport,
                )
            ):
                raise GatewayDenied("provider credential lease binding is invalid")
        elif (
            audience == "provider_gateway"
            or provider_id
            or endpoint
            or purpose.startswith("provider:")
        ):
            raise GatewayDenied("master key lease cannot carry provider authority")
        if not math.isfinite(ttl) or ttl <= 0 or ttl > self.max_ttl_seconds:
            raise GatewayDenied("secret lease TTL is outside policy")
        with self._lock:
            if self._closed.is_set():
                raise GatewayDenied("secret lease broker is closed")
            previous = self._leases.pop(lease_id, None)
            if previous is not None:
                previous.clear()
            # Main rotates one exact durable reference.  Pending replacement refs may
            # coexist until backend settings activate one; they cannot be redeemed by
            # a binding for the other reference.
            for existing_id, existing in tuple(self._leases.items()):
                if existing.secret_ref == secret_ref:
                    existing.clear()
                    self._leases.pop(existing_id, None)
            self._leases[lease_id] = _SecretLease(
                lease_id=lease_id,
                secret_ref=secret_ref,
                slot=slot,
                kind=kind,
                purpose=purpose,
                audience=audience,
                provider_id=provider_id,
                endpoint=endpoint,
                transport=transport,
                value=bytearray(value.encode("utf-8")),
                expires_at=expiry,
            )

    def redeem(
        self, lease_id: str, *, purpose: str, now: datetime | None = None,
    ) -> str:
        with self._lock:
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

        with self._lock:
            reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
            self._sweep_expired_locked(reference)
            matches = [
                lease for lease in self._leases.values()
                if lease.secret_ref == str(secret_ref)
                and lease.purpose == purpose
                and lease.kind == "provider_credential"
                and lease.audience == "provider_gateway"
            ]
            if not matches:
                raise GatewayDenied("provider secret lease is unavailable")
            current = max(matches, key=lambda item: item.expires_at)
            return bytes(current.value).decode("utf-8")

    def redeem_provider_credential(
        self,
        secret_ref: str,
        *,
        slot: str,
        provider_id: str,
        endpoint: str,
        transport: str,
        expected_purpose: str,
        now: datetime | None = None,
    ) -> str:
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        canonical_endpoint = _canonical_provider_endpoint(endpoint)
        with self._lock:
            self._sweep_expired_locked(reference)
            matches = [
                lease for lease in self._leases.values()
                if lease.secret_ref == str(secret_ref)
            ]
            if not matches:
                raise GatewayDenied("provider secret lease is unavailable")
            current = max(matches, key=lambda item: item.expires_at)
            if (
                current.kind != "provider_credential"
                or current.audience != "provider_gateway"
                or current.slot != slot
                or current.provider_id != provider_id
                or current.endpoint != canonical_endpoint
                or current.transport != transport
                or current.purpose != expected_purpose
                or not current.purpose.startswith("provider:")
            ):
                raise GatewayDenied("provider secret lease binding mismatch")
            return bytes(current.value).decode("utf-8")

    def _sweep_expired_locked(self, reference: datetime) -> int:
        removed = 0
        for lease_id, lease in tuple(self._leases.items()):
            if reference >= lease.expires_at:
                lease.clear()
                self._leases.pop(lease_id, None)
                removed += 1
        return removed

    def sweep_expired(self, *, now: datetime | None = None) -> int:
        reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._lock:
            return self._sweep_expired_locked(reference)

    def revoke_secret(self, secret_ref: str) -> int:
        removed = 0
        with self._lock:
            for lease_id, lease in tuple(self._leases.items()):
                if lease.secret_ref == str(secret_ref):
                    lease.clear()
                    self._leases.pop(lease_id, None)
                    removed += 1
        return removed

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            for lease in self._leases.values():
                lease.clear()
            self._leases.clear()
        sweeper = self._sweeper
        if sweeper is not None and sweeper is not threading.current_thread():
            sweeper.join(timeout=max(0.1, self._sweep_interval_seconds * 2))

    def public_snapshot(self) -> tuple[dict[str, str], ...]:
        with self._lock:
            self._sweep_expired_locked(datetime.now(timezone.utc))
            return tuple({
                "lease_id": lease.lease_id,
                "secret_ref": lease.secret_ref,
                "slot": lease.slot,
                "kind": lease.kind,
                "purpose": lease.purpose,
                "audience": lease.audience,
                "provider_id": lease.provider_id,
                "endpoint": lease.endpoint,
                "transport": lease.transport,
                "expires_at": lease.expires_at.isoformat().replace("+00:00", "Z"),
            } for lease in self._leases.values())

    def redact_diagnostics(self, value: Any) -> Any:
        with self._lock:
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
    "McpCapabilityIdentity",
    "McpStdioTransport",
    "McpStreamableHttpTransport",
    "ModelCapabilityRegistry",
    "PiiDlpGateway",
    "ProviderCapability",
    "ProviderGatewayClient",
    "derive_provider_credential_purpose",
    "production_model_capability_registry",
    "SecretLeaseBroker",
    "ToolExecutionRequest",
    "ToolGateway",
]
