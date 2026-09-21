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


class ShellSandboxAttestation:
    """Shell executor 的 sandbox 证明（I-R2）。

    `trusted_sandbox=True` 曾只是把**工具名**放进集合，而 `invoke_with_effect` 之后
    仍可用任意 effect 替换已注册的 executor —— 于是"授权 metadata 里写着受信 sandbox"
    与"真正执行的进程"可以不是同一个东西。

    现在注册 Shell 必须显式给出这份证明，它同时约束：
      · `marker` —— 部署方提供的、非空的 attestation 标识（例如 sandbox 机制名）；
      · `executor` —— **与 descriptor/effect 绑定的那一个** executor 对象。
    运行时不再接受任何 effect override（见 ToolGateway._invoke），因此"换个 effect"
    这条路在 API 层面就消失了。
    """

    __slots__ = ("marker", "executor")

    def __init__(self, marker: str, executor: Any) -> None:
        text = str(marker or "").strip()
        if not text or len(text) > 128:
            raise ValueError("shell sandbox attestation requires a bounded non-empty marker")
        if executor is None:
            raise ValueError("shell sandbox attestation requires the bound executor")
        self.marker = text
        self.executor = executor

    def __repr__(self) -> str:  # pragma: no cover - 诊断用
        return f"ShellSandboxAttestation(marker={self.marker!r})"


class ToolGateway:
    """One policy/effect boundary shared by built-ins, Shell and MCP."""

    def __init__(self, runtime: TaskRuntimeService, *, dlp: "PiiDlpGateway | None" = None) -> None:
        self.runtime = runtime
        self.dlp = dlp or PiiDlpGateway()
        self._descriptors: dict[str, ToolDescriptor] = {}
        self._effects: dict[str, ToolEffect] = {}
        self._shell_attestations: dict[str, ShellSandboxAttestation] = {}

    def register(
        self,
        descriptor: ToolDescriptor,
        effect: ToolEffect,
        *,
        attestation: ShellSandboxAttestation | None = None,
    ) -> None:
        if descriptor.tool_name in self._descriptors:
            raise ValueError(f"duplicate Tool Gateway descriptor: {descriptor.tool_name}")
        if attestation is not None:
            if descriptor.kind != "shell":
                raise ValueError("shell sandbox attestations are valid only for Shell descriptors")
            # 证明必须绑定**这一个** executor：换 effect 就是换进程边界。
            if attestation.executor is not effect:
                raise ValueError(
                    "shell sandbox attestation must be bound to the registered effect executor"
                )
            self._shell_attestations[descriptor.tool_name] = attestation
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
        if descriptor.kind == "shell" and descriptor.tool_name not in self._shell_attestations:
            # Declared argv/path/network scopes are authorization metadata, not an OS
            # sandbox.  Until a verifiable Windows sandbox executor is registered with an
            # explicit attestation, no production Shell or MCP-stdio process may reach
            # its effect boundary.
            raise GatewayDenied("Shell effect requires an explicitly trusted sandbox executor")
        if effect_override is not None and descriptor.kind == "shell":
            # I-R2：Shell 的 effect 不可替换。attestation 绑定的是**某一个** executor，
            # 允许 override 就等于允许在通过 marker 之后换掉进程边界。
            raise GatewayDenied("Shell effects cannot be replaced after registration")
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
        except (GeneratorExit, asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # M-6 的同一条纪律，应用到工具路径：取消不是 effect 失败。此前这里只有
            # `except Exception`，而这三个都不是 `Exception` 的子类——于是 effect 被取消时
            # `complete_tool` 根本不会被调用，invocation 一直停在 `running`，直到重启恢复
            # 把它标成 `recovered_blocked`。审计里一次"用户中止"看起来像"工具还在跑"。
            #
            # 终态记录自己失败时也不能替换掉取消（与 `_complete_authorized_effect` 同样的
            # 理由）：调用方要看到的是它自己的取消，不是一个数据库异常。
            try:
                self.runtime.store.cancel_tool(invocation.invocation_id)
            except (RuntimeConflictError, RuntimeNotFoundError, RuntimeStoreError, ValueError):
                pass
            raise
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


def _is_textual_mime(mime_type: Any) -> bool:
    """MCP resource 的这段内容是否应当按文本过 prompt/DLP 策略。

    旧实现只看"有没有 `text` 字段"，于是 `mimeType: text/plain` + `blob`（base64）
    的注入文本完全绕过了策略——复审实测过。这里改成按 MIME 判定文本性：
    `text/*`、JSON、XML（含 `+json` / `+xml`）都算文本，解码后一律扫描。
    """

    if not isinstance(mime_type, str) or not mime_type:
        # 没声明类型时保守按文本处理：宁可多扫一次，也不要漏掉注入内容。
        return True
    normalized = mime_type.split(";", 1)[0].strip().casefold()
    if normalized.startswith("text/"):
        return True
    return normalized in {"application/json", "application/xml"} or normalized.endswith(
        ("+json", "+xml")
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
        sealed_config = getattr(transport, "config", None)
        if sealed_config is None:
            raise GatewayDenied("MCP sealed transport has no bound config")
        # I7：比较**规范化 endpoint**而不是 URL。旧实现比 `config.url`，而 stdio 的
        # `McpServerConfig` 强制 url 为空 —— 于是两个空串永远相等，真正要启动的
        # command/args **从未被比较**（复审实测：换掉可执行文件后 seal 仍然通过）。
        # `_canonical_mcp_endpoint` 对 stdio 也会产出可比较的标识，register 时已经在用它。
        if _canonical_mcp_endpoint(sealed_config) != _canonical_mcp_endpoint(config):
            raise GatewayDenied("MCP sealed transport target changed after registration")
        # 顺带核对 server_id 与 transport 类型，避免"同 endpoint 但换了 peer 类型"。
        # 用 isinstance 而不是比较 `type().__name__`（复审实测后者双向不可靠：
        # 合法子类会被误拒，而伪造 `__name__` 的任意对象能通过）。
        if sealed_config.server_id != config.server_id:
            raise GatewayDenied("MCP sealed transport is bound to a different server")
        expected_type = (
            McpStreamableHttpTransport if config.transport == "streamable_http"
            else McpStdioTransport
        )
        if not isinstance(transport, expected_type):
            raise GatewayDenied("MCP sealed transport type does not match its config")
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
        # I-R4：返回**深拷贝**而不是内部对象。旧实现 `return accepted` 把内部
        # records（含 schema 与 identity）直接交给调用方，于是
        # `accepted["tool_records"][name]["schema"]` 被改写后，后续参数校验用的是
        # 被改过的 schema，而 `identity.digest` 一字未变——授权校验照样通过。
        # 深拷贝让"调用方看到的"与"registry 依赖的"彻底分离。
        return copy.deepcopy(accepted)

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
                textual = text
            else:
                try:
                    body_bytes = base64.b64decode(blob, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise GatewayDenied("MCP resource blob encoding is invalid") from exc
                # 旧实现只对 `text` 分支做 prompt/DLP 检查，于是
                # `mimeType: text/plain` + base64 的注入文本可以绕过（复审实测：
                # "ignore all previous instructions…" 被原样接受）。现在按 MIME 判定：
                # 文本类内容一律解码后过同一套策略。
                textual = body_bytes.decode("utf-8", errors="replace") if _is_textual_mime(
                    mime_type,
                ) else None
            total += len(body_bytes)
            if total > 1_048_576:
                raise GatewayDenied("MCP resource contents exceed the size limit")
            if textual is not None and (
                _PROMPT_INJECTION.search(textual)
                or PiiDlpGateway().decide_outbound(textual, approved=False).requires_approval
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

    def provider_ids(self) -> tuple[str, ...]:
        """已注册的 provider 列表（只读快照，便于调用方/测试枚举）。"""

        return tuple(sorted(self._providers))

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
    """生产 provider 能力表。

    ⚠ **RAG 能力（embedding / vision / rerank）是保守声明，不是营销清单**（I-R1）：
    旧实现给每个 provider 只注册 `{"chat","stream","tools"}`，于是 Task 3 的本地优先
    RAG 要按隐私与成本预授权 cloud embedding 或 vision OCR 时**没有槽位可挂**，只剩
    "绕过 registry"一条路——那正是这套网关要防的事。这里为已知具备对应公开 API 的
    provider 显式加上槽位；未列出的 provider 一律不具备该能力（fail closed）。

    新增 provider 时必须**核实**它是否真的提供该能力的公开 API 再登记：把没验证过的
    能力写进来，就等于让 registry 为一次并不受支持的调用背书。登记 RAG 能力**不会**
    降低 chat 路径的隐私要求（privacy 仍按 provider 单独声明）。
    """

    # (embedding, vision, rerank) —— 仅列已核实的公开 API 能力。
    rag_capabilities: dict[str, tuple[bool, bool, bool]] = {
        "openai-api": (True, True, False),
        "gemini": (True, True, False),
        "alibaba": (True, True, True),
        "zai": (True, True, False),
        "deepseek": (False, False, False),
        "openrouter": (False, True, False),
        "anthropic": (False, True, False),
        "xai": (False, True, False),
        "minimax": (False, True, False),
        "minimax-cn": (False, True, False),
        "kimi-coding": (False, True, False),
        "kimi-coding-cn": (False, True, False),
        "stepfun": (False, True, False),
        "nvidia": (True, False, True),
        "huggingface": (True, False, False),
        "novita": (True, False, False),
        "arcee": (False, False, False),
        "gmi": (False, False, False),
        "copilot": (False, True, False),
    }

    registry = ModelCapabilityRegistry()
    for provider_id, (endpoints, transports) in _PRODUCTION_PROVIDER_ENDPOINTS.items():
        embedding, vision, rerank = rag_capabilities.get(provider_id, (False, False, False))
        capabilities = {"chat", "stream", "tools"}
        if embedding:
            capabilities.add("embedding")
        if vision:
            capabilities.add("vision")
        if rerank:
            capabilities.add("rerank")
        registry.register(ProviderCapability(
            provider_id=provider_id,
            # I-R1 的**剩余部分**，写在这里以免下一个人以为它是随手写的通配符：
            # `authorize()` 只在 `"*" not in capability.models` 时才核对模型名，所以这
            # 一行等于"这个 provider 的任何模型名都通过"，于是 `privacy` 与
            # `estimated_cost_usd` 实际上都是**按 provider**声明、不按模型。
            #
            # 为什么不收紧：项目里确实有一份带来源 URL 的逐模型价格表
            # (`backend/token_pricing.py`)，但它 (a) 对 `copilot` / `gmi` /
            # `huggingface` / `kimi-coding` / `kimi-coding-cn` 五个 provider 一条都没有，
            # 收紧会让这五个的模型集为空（fail closed，直接不可用），且会拒绝用户配置
            # 的任何新模型；(b) **完全不含隐私数据**——"逐模型隐私"根本推不出来。
            #
            # 所以这是一个产品取舍，不是遗漏：要么 fail closed 于一份经过核实的清单，
            # 要么保持通配符并明说隐私/价格是按 provider 的。现在是后者。台账里有完整的
            # 实测依据与建议（隐私才是该逐模型化的那一项，价格可以留在 provider 级）。
            models=("*",),
            capabilities=frozenset(capabilities),
            privacy=PrivacyTier.STANDARD,
            estimated_cost_usd=0.01,
            preauthorized=True,
            endpoint_authorities=endpoints,
            transports=transports,
        ))
    return registry


@dataclass
class _ProviderAuthorization:
    """一次 provider egress 的授权结果（I-R1）。

    旧实现只把 `(runtime, invocation_id)` 传出去，于是 effect 之后**没有**任何信息
    可以用来做 usage 对账：既不知道预留了多少 token，也拿不到 capability 的单价。
    这个对象把对账需要的三项一起带出来。
    """

    runtime: TaskRuntimeService | None
    invocation_id: str
    reserved_tokens: int
    capability: ProviderCapability
    run_id: str
    group_id: str
    #: 预留的完整明细（结算时按维度退回差额）。
    reserved_usage: BudgetUsage = None  # type: ignore[assignment]
    #: 其中用于**输入**估算的那部分：结算时输入维度不退（估算保守），
    #: 只把输出预留按实际上限退回。
    estimated_input_tokens: int = 0
    #: 授权时刻（单调时钟），用于终态的 duration_ms（I-R5）。
    started_at: float = 0.0
    #: 授权时的 capability 摘要，终态事件要带上以便与 authorized 配对（I-R5）。
    capability_digest: str = ""


#: `max_tokens` 未显式给出时施加的 backend 输出上限。
#:
#: 旧实现 `reserved_output_tokens = max(0, int(max_tokens or 0))` 在 None 时预留 0，
#: 而**生产 Worker 恰好就传 None**（worker_gateway_runtime.py），于是输出用量完全不在
#: 预算里——"预算耗尽会拦住调用"这句话就没有依据。这里给一个保守上限，并按剩余额度
#: 收敛；真正准确的对账由 effect 后的 usage 补偿完成。
_DEFAULT_OUTPUT_TOKEN_CAP = 4096


def _provider_reported_total_tokens(response: Any) -> int | None:
    """从 provider 响应里取本次使用的 token 总数（拿不到就返回 None）。

    I3：**优先复用 `knowe_core.provider_client.normalize_usage_buckets`**，而不是自己
    再写一套窄解析。复审实测过窄解析的后果：`anthropic_messages` 与 Codex/Responses
    形状（`{"usage": {"input_tokens": …, "output_tokens": …}}`）全部返回 None，于是
    那些 provider **静默不对账** —— 而 registry 里 minimax/anthropic/openai-api 正是
    这些 transport。`normalize_usage_buckets` 已经处理 OpenAI / DeepSeek / Anthropic /
    Gemini 以及多层 envelope 展开。

    保留 `total_tokens` 回退：少数兼容实现只报这一个字段。
    usage 是不可信输入，所以只接受非负整数并显式拒绝 bool。
    """

    if not isinstance(response, Mapping):
        return None
    # M-5：`total_tokens` 若存在优先采用。旧实现先跑 buckets，非空就**忽略** total——
    # 复审实测 `{"total_tokens":42,"prompt_tokens":10}` 被算成 10（少算 32）。
    usage = response.get("usage")
    if isinstance(usage, Mapping):
        total = usage.get("total_tokens")
        if not isinstance(total, bool) and isinstance(total, int) and total >= 0:
            return total
    try:
        from knowe_core.provider_client import normalize_usage_buckets
    except ImportError:  # pragma: no cover - 仅打包异常时
        normalize_usage_buckets = None  # type: ignore[assignment]
    if normalize_usage_buckets is not None:
        buckets = normalize_usage_buckets(response)
        if buckets:
            total = sum(
                int(value) for value in buckets.values()
                if isinstance(value, int) and not isinstance(value, bool)
            )
            if total > 0:
                return total
    return None


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
    ) -> None:
        """唯一的生产模型出口。

        C-R1：这里曾有 `allow_ungoverned_test_transport`，一个构造期布尔量即可让
        `_authorize` 直接 `return None` —— 整体跳过 authority、registry、DLP、
        预算、审批与审计。

        该参数的**前身**改名成 `_trusted_test_transport` 活了一段时间，而且它的
        "物理上出不了网"的说辞被复审证伪：一层 wrapper、或一个 handler 会转发的
        `MockTransport`，都能真的建立 socket。那样的东西不是"已知限制"，是一条被
        洗白的后门，所以它现在**已删除**——本类不再有任何跳过授权的构造参数。

        押注在"下一层拦得住"是错的：这一层必须自己就是边界。测试要注入假 transport
        时走的是 `client_factory`，把 client 放进一个真实的短命 Task Runtime run 里
        （`settings_probe` 用的就是这条路径）。

        ⚠ 下面两句曾被我自己写在这里、又被独立复审实测打掉，一并更正（`ebf7feb` 的
        提交信息里还留着第一句，提交信息改不了，只能在此更正）：

          · "没有任何配置能让 provider egress 在没有权威 Task Runtime run 的情况下
            进行" —— **不成立**。`authority_provider` 是公开构造参数，一个鸭子类型的
            假 runtime 曾经就能让整段授权放行并真的发出请求；`_authorize` 现在加了
            `isinstance(runtime, TaskRuntimeService)`，但那只挡住"随手写个假对象"。
          · "`client_factory` 只替换发送通道" —— **过强**。它注入的是测试提供的
            transport，而那个 transport 的 handler 想连哪里就连哪里：授权核对的是
            `base_url`，不是 handler 实际连上的地址。不转发是**测试的承诺**，不是
            代码的保证。
        """
        from knowe_core.provider_client import ProviderClient

        self.base_url = str(base_url)
        self.model = str(model)
        self.provider = str(provider)
        self.transport = str(transport or "openai_chat")
        self.registry = registry or production_model_capability_registry()
        self.authority_provider = authority_provider
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
        )

    #: 允许从底层 transport 读取的**只读**属性。
    #: C-R1 之前这里是 `__getattr__` 全量代理，于是 `gateway._request`、
    #: `gateway._build_body` 等内部发送路径对任何持有者可达。
    _DELEGATED_READS = ("base_url", "model", "provider", "timeout", "max_retries")

    #: 这些名字**不允许**从实例上取到。
    #:
    #: 允许从底层 transport 读取的**只读**元数据。
    _DELEGATED_READS = ("base_url", "model", "provider", "timeout", "max_retries")

    def __getattr__(self, name: str) -> Any:
        """只放行元数据读取；其余一律 AttributeError。

        ⚠ **这不是安全边界，别把它当边界**（第四轮复审实测）：底层对象 `_transport`
        是普通实例属性，`gateway.__dict__["_transport"]`、`vars(gateway)`、
        `object.__getattribute__` 都能取到它，取到后 `.chat()` / `._request()` 就能
        直接出网。曾试图用 `__getattribute__` 只挡 `_transport` 这一个字面名，结果
        既挡不住 `__dict__`，又因为配套的 `_sealed_transport()` 访问器把同一个对象
        交了出去而完全失效——**那种"补一层"的做法只是虚假的安全感**，已删除。

        这里的价值仅在于：让 `gateway._request` / `gateway._build_body` 这类**不存在
        的属性**不会意外代理到底层，从而让"这个类对外能做什么"一眼可读。

        真正的边界只能是"不把 transport 放在这个对象上"（例如放在模块级闭包或
        `WeakKeyDictionary` 里）。当前未做，已登记为遗留。
        """

        if name in ProviderGatewayClient._DELEGATED_READS:
            transport = object.__getattribute__(self, "_transport")
            return getattr(transport, name)
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}; "
            "provider egress goes through ProviderGatewayClient.chat/chat_stream only"
        )

    async def aclose(self) -> None:
        """关闭底层 transport（只关闭连接，不绕过任何策略）。"""

        transport = object.__getattribute__(self, "_transport")
        await transport.aclose()

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
    ) -> "_ProviderAuthorization | None":
        # C-R1：这里曾有 `if self._allow_ungoverned_test_transport: return None, ""`，
        # 一个构造期布尔量即可整体跳过 authority、registry、DLP、预算、审批与审计。
        # 后来它改名成 `_trusted_test_transport` 继续短路（同样的东西、更明确的名字），
        # 现在**已彻底删除**：`_authorize` 没有任何提前返回，每一次 provider egress
        # 都必须带着真实的 Task Runtime 授权走完全程。
        authority = self.authority_provider()
        if authority is None or authority.runtime is None:
            raise GatewayDenied("authoritative Task Runtime context is unavailable for provider egress")
        runtime = authority.runtime
        # I-1（独立复审实测）：此前这一段只做**鸭子类型**检查，而 `authority_provider`
        # 是公开构造参数。一个二十行的假对象——`store.get_run` 返回 `status=RUNNING` 的
        # 假 run，再加 `consume_budget`/`settle_budget`——就能让整段授权全绿并发起真实
        # egress，全程没有 Task Runtime（复审的 trace：`authorized` → `SENT POST` →
        # `completed`）。所以这里要求 runtime 真的是 `TaskRuntimeService`。
        #
        # ⚠ 诚实边界：`isinstance` 不是密码学边界——子类、或一个真实的
        # `TaskRuntimeService` 实例，仍然由持有者决定。它挡住的是"随手写个假对象就成了"
        # （也就是复审证伪的那条路），不是"铁了心要绕过的调用方"。
        #
        # 【第二次更正】我在这里先写过"`git grep` 显示没有任何生产调用点传这个参数"——
        # **那句是错的**（写的时候并没有真去 grep）。实际有两个调用点，而且其中一个就是
        # 生产路径：
        #   · `worker_gateway_runtime.py:332` 传 `authority_provider=lambda: authority`；
        #   · 那个 `authority` 来自 `engine._canonical_legacy_tool_authority(...)`，它
        #     **不接受 mapping 作为授权**：它拿三个 id 回 Task Runtime store 查 run、
        #     核 group/task 归属、核 approval/grant，然后用 `runtime=self._task_runtime_service`
        #     重建 authority（也就是 engine 自己的真实服务实例）。
        #   · `engine.bind_commercial_tool_registry` 也接受同一个参数，默认 `None` → 回落到
        #     ContextVar。
        # 所以真实的论证不是"没人传"，而是"**传进来的 authority 是由 store 重建的**"：
        # 假 mapping 换不来授权，且重建出的 runtime 一定是真实实例，因而必然通过下面的
        # isinstance。这段注释存在的意义就是别再让第三种说法冒出来。
        if not isinstance(runtime, TaskRuntimeService):
            raise GatewayDenied(
                "provider egress requires a real Task Runtime, not a stand-in"
            )
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
        # I-R1：`max_tokens=None`（生产 Worker 的值）时不再预留 0，而是施加一个保守
        # 上限并受剩余额度收敛；准确用量由 effect 后的 usage 对账补齐。
        output_limit = self._effective_output_limit(
            authority, run=run, requested=max_tokens,
        )
        reserved_output_tokens = output_limit
        # I3：预留与 consume 必须用**同一份** BudgetUsage。旧实现声明
        # `model_turns = 1 + max_retries` 却只 consume 1 轮/1 份成本，于是 settle 的
        # 差值恒为 0、重试计费从未进账（复审实测：真重试 3 次，账上仍是 1 轮）。
        reserved_usage = self._reserved_usage(
            capability=capability,
            tokens=estimated_input_tokens + reserved_output_tokens,
        )
        budgeted = runtime.consume_budget(
            authority.group_id,
            authority.run_id,
            reserved_usage,
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
        return _ProviderAuthorization(
            runtime=runtime,
            invocation_id=invocation_id,
            reserved_tokens=estimated_input_tokens + reserved_output_tokens,
            capability=capability,
            run_id=authority.run_id,
            group_id=authority.group_id,
            # 结算时按维度退回差额，所以带上**与 consume 完全相同**的那一份预留明细
            # （由 `_reserved_usage` 统一构造，避免两处各写一遍而漂移——这正是 I3
            #  "声明 3 轮却只扣 1 轮" 的成因）。
            reserved_usage=reserved_usage,
            estimated_input_tokens=estimated_input_tokens,
            started_at=time.monotonic(),
            capability_digest=capability_digest,
        )

    def _reserved_usage(self, *, capability: Any, tokens: int) -> BudgetUsage:
        """本次调用的预留用量（consume 与 settle 共用同一份定义）。

        `model_turns` 与 `cost_usd` 按**最大尝试次数**计（`1 + max_retries`）：一次授权
        最多会发出这么多次 provider 调用。effect 之后由 `_reconcile_provider_usage`
        按**实际**尝试次数结算退回，所以两边都不会偏。
        """

        attempts = self._max_attempts()
        return BudgetUsage(
            model_turns=attempts,
            tokens=int(tokens),
            cost_usd=capability.estimated_cost_usd * attempts,
        )

    def _actual_attempts(self) -> int:
        """上一次 provider 调用**实际**发出的次数（含重试），拿不到就按 1 计。

        由 `ProviderClient.last_attempts` 提供；它记录每次网络尝试。拿不到时按 1 计是
        保守的：宁可少退（多扣一点）也不要按 0 计而把轮次全退掉。
        """

        transport = object.__getattribute__(self, "_transport")
        attempts = int(getattr(transport, "last_attempts", 0) or 0)
        return max(1, attempts)

    def _settle_attempt_dimensions(
        self, authorization: "_ProviderAuthorization", tokens: int | None,
    ) -> None:
        """只结算轮次/成本维度（provider 未上报 usage 时用）。"""

        if authorization.runtime is None:
            return
        attempts = self._actual_attempts()
        actual_usage = BudgetUsage(
            tokens=(
                authorization.reserved_usage.tokens if tokens is None else max(0, int(tokens))
            ),
            model_turns=attempts,
            cost_usd=authorization.capability.estimated_cost_usd * attempts,
            wall_clock_seconds=authorization.reserved_usage.wall_clock_seconds,
        )
        if actual_usage == authorization.reserved_usage:
            return
        try:
            authorization.runtime.settle_budget(
                authorization.group_id,
                authorization.run_id,
                actual=actual_usage,
                reserved=authorization.reserved_usage,
                request_key=f"provider-settle:{uuid.uuid4().hex}",
            )
        except (RuntimeConflictError, RuntimeNotFoundError, RuntimeStoreError, ValueError):
            return

    def _max_attempts(self) -> int:
        """一次授权可能触发的 provider 调用次数（1 + 重试）。"""

        transport = object.__getattribute__(self, "_transport")
        retries = int(getattr(transport, "max_retries", 0) or 0)
        return max(1, retries + 1)

    @staticmethod
    def _effective_output_limit(
        authority: LegacyToolAuthority, *, run: Any, requested: int | None,
    ) -> int:
        """本次调用要预留多少输出 token（I-R1）。

        - 显式给出 max_tokens：尊重它（连通性探针传 1）。
        - 未给出（生产 Worker 的路径）：用 `_DEFAULT_OUTPUT_TOKEN_CAP`，并按 run 的
          剩余 token 额度收敛，避免预留本身就超预算。
        """

        if requested is not None and int(requested) > 0:
            return int(requested)
        cap = _DEFAULT_OUTPUT_TOKEN_CAP
        limits = getattr(run, "budget_limits", None)
        used = getattr(run, "budget_usage", None)
        total = int(getattr(limits, "tokens", 0) or 0)
        consumed = int(getattr(used, "tokens", 0) or 0)
        remaining = total - consumed
        if remaining <= 0:
            # 额度已尽：不预留任何输出，让下游的 budget 检查立刻拒绝。
            return 0
        return max(1, min(cap, remaining))

    def _reconcile_provider_usage(
        self,
        authorization: "_ProviderAuthorization | None",
        response: Any,
        *,
        failed: bool = False,
    ) -> None:
        """按 provider 上报的 usage 对账（I-R1）。

        预留是**上界估计**（输入按字符数/4 估、输出按上限估），所以两个方向都要处理：

        · 实际 **高于**预留 → 补扣差额（估算偏低）。
        · 实际 **低于**预留 → 通过 `settle_budget` 把差额退回。**这一步是复审要求补的**：
          旧实现只补不退，复审实测每次调用永久多扣约 3.3 倍（真实 1300 vs 预留 4615），
          22 次后 run 被判 BUDGET_EXHAUSTED 而实际只用了 27%，`BudgetLimits(tokens=100000)`
          的实际语义变成约 27000 —— "预算"这个数字不再可信。

        I1 残余（第四轮复审）：**调用失败**时旧实现压根不调用本方法，于是预留被 100%
        计入（实测账上 4115 / 真实 0）。失败时 token 维度保持预留是**故意保守**的
        （失败前可能已经产生了输入消耗，无法从错误里得知），但轮次与成本维度会按实际
        尝试次数结算——模型轮次是已知量，没有理由按最坏情况收费。

        usage 是不可信输入：只接受非负整数（见 `_provider_reported_total_tokens`），
        且补扣量以本次预留为上限，否则一个乱报用量的 provider 可以一次把预算扣穿。
        """

        if authorization is None or authorization.runtime is None:
            return
        if failed:
            self._settle_attempt_dimensions(authorization, None)
            return
        actual = _provider_reported_total_tokens(response)
        if actual is None:
            # provider 没报 usage：token 维度保持预留（保守），但轮次/成本仍按**实际
            # 尝试次数**结算——否则未重试的成功调用会一直按最坏情况付模型轮次。
            self._settle_attempt_dimensions(authorization, None)
            return
        reserved = authorization.reserved_tokens
        delta = actual - reserved
        if delta > 0:
            # 估算偏低：补扣，但以本次预留为上限（防 peer 用假 usage 扣穿预算）。
            top_up = min(delta, max(0, reserved))
            if top_up > 0:
                try:
                    authorization.runtime.consume_budget(
                        authorization.group_id,
                        authorization.run_id,
                        BudgetUsage(tokens=top_up),
                        request_key=f"provider-usage:{uuid.uuid4().hex}",
                    )
                    reserved += top_up
                except (RuntimeConflictError, RuntimeNotFoundError, RuntimeStoreError, ValueError):
                    # 对账失败不能把一次已成功的调用变成失败；下一次调用会重新校验余额。
                    return
        # 统一结算：tokens 按实际（或保持预留），轮次/成本按**实际尝试次数**（I3）。
        # 预留是按最坏情况（1 + max_retries）扣的，没重试的部分必须退回去，否则
        # 每次成功调用都多付 (max_retries) 轮与对应成本。
        attempts = self._actual_attempts()
        actual_usage = BudgetUsage(
            tokens=max(0, reserved),
            model_turns=attempts,
            cost_usd=authorization.capability.estimated_cost_usd * attempts,
            wall_clock_seconds=authorization.reserved_usage.wall_clock_seconds,
        )
        if actual_usage == authorization.reserved_usage:
            return
        # settle 要求 actual <= reserved：预留里 token 维度可能已被补差抬高，
        # 把它一起抬到实际值再结算（结算后的账就是 actual_usage）。
        settled_reserved = replace(
            authorization.reserved_usage, tokens=max(0, reserved),
        )
        try:
            authorization.runtime.settle_budget(
                authorization.group_id,
                authorization.run_id,
                actual=actual_usage,
                reserved=settled_reserved,
                request_key=f"provider-settle:{uuid.uuid4().hex}",
            )
        except (RuntimeConflictError, RuntimeNotFoundError, RuntimeStoreError, ValueError):
            return

    @staticmethod
    def _complete_authorized_effect(
        authorization: "_ProviderAuthorization | None",
        *,
        error: BaseException | None,
        actual_tokens: int | None = None,
        cancelled: bool = False,
    ) -> None:
        """把 effect 的终态写回 Task Runtime。

        I-R5：旧实现只对**需要审批**的请求写 `complete_tool` 终态；普通请求
        （`invocation_id` 为空）直接 return，于是公开审计里只有 effect 前的
        `authorized`，**无法证明请求是否执行、何时结束、耗时多久、实际用了多少 token**。
        现在普通请求也会写一条 `provider_egress` 终态事件。

        M-6：`cancelled` 必须与 `error` 分开。消费者离开流式响应（`aclose`）或任务被
        取消时抛出的是 `GeneratorExit` / `CancelledError`——**控制流信号，不是运行
        错误**。旧实现把它们塞进 `except BaseException`，于是 `type(error).__name__`
        把 `GeneratorExit` 写成了错误类型，审计里就留下一次假的 `failed`。取消现在是
        独立的 `cancelled` 终态，且**不写** `error_type`。

        终态 payload 只含 opaque id 与度量：status / capability_digest / privacy /
        duration_ms / actual_tokens / typed error——没有 body、凭据或 PII。
        """

        if cancelled and error is not None:
            raise ValueError("a cancelled effect cannot also carry an error")
        if authorization is None or authorization.runtime is None:
            return
        if cancelled:
            status = "cancelled"
        else:
            status = "failed" if error is not None else "completed"
        if authorization.invocation_id:
            store = authorization.runtime.store
            try:
                if cancelled:
                    store.cancel_tool(authorization.invocation_id)
                else:
                    store.complete_tool(
                        authorization.invocation_id,
                        status=status,
                        error_type=type(error).__name__ if error is not None else "",
                    )
            except (RuntimeConflictError, RuntimeNotFoundError, RuntimeStoreError, ValueError):
                # 终态记录失败不能改变 effect 的结局。**取消路径上这一条尤其要紧**：本
                # 方法就在 `except GeneratorExit` 分支里，这里抛出的任何异常都会替换掉
                # `GeneratorExit`，于是一次正常取消变成 `aclose()` 的意外异常——消费者
                # （真实 UI 每一帧都在 `async for ... break`）看到的是"存储炸了"，而不是
                # "流结束了"。下面的事件写入一直有这个保护，终态记录此前没有，这个不对称
                # 就是本段存在的原因。
                #
                # 诚实边界：这里**静默**吞掉，本模块没有 logger，所以存储不可用时的表现是
                # "少一条终态记录"（连下面的 provider_egress 事件通常也一起失败）。这次修复
                # 只保证它不再破坏取消语义，不保证审计不丢。
                pass
        duration_ms = max(
            0, int((time.monotonic() - (authorization.started_at or time.monotonic())) * 1000),
        )
        payload: dict[str, Any] = {
            "status": status,
            "capability_digest": authorization.capability_digest,
            "privacy": authorization.capability.privacy.value,
            "duration_ms": duration_ms,
            "approval_required": bool(authorization.invocation_id),
        }
        if error is not None:
            payload["error_type"] = type(error).__name__
        if actual_tokens is not None and actual_tokens >= 0:
            payload["actual_tokens"] = int(actual_tokens)
        try:
            authorization.runtime.store.change_run(
                authorization.run_id,
                event_type="provider_egress",
                public_payload=payload,
                idempotency_key=f"provider-terminal:{uuid.uuid4().hex}",
            )
        except (RuntimeConflictError, RuntimeNotFoundError, RuntimeStoreError, ValueError):
            # 终态审计失败不能把一次已完成的调用变成失败。
            return

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        authorization = self._authorize(
            messages=messages, tools=tools, extra_body=extra_body,
            max_tokens=max_tokens, stream=False,
        )
        try:
            result = await object.__getattribute__(self, "_transport").chat(
                messages, tools, temperature, max_tokens, extra_body,
            )
        except (GeneratorExit, asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # M-6：取消不是失败。终态记 `cancelled`，不写 error_type。
            # M-4（独立复审实测）：`KeyboardInterrupt` / `SystemExit` 同样不是 provider
            # 故障——它们与 `CancelledError` 一样是"调用方中止"，此前会记成
            # `failed / KeyboardInterrupt`。两者仍然重新抛出，调用方要看到它们。
            self._reconcile_provider_usage(authorization, None, failed=True)
            self._complete_authorized_effect(authorization, error=None, cancelled=True)
            raise
        except BaseException as exc:
            # I1 残余：失败的调用也要结算**轮次/成本**维度，否则预留被 100% 计入
            # （复审实测：失败时账上 4115 / 真实 0）。token 维度保持预留是故意的——
            # 失败前可能已产生输入消耗，从错误里无法得知。
            self._reconcile_provider_usage(authorization, None, failed=True)
            self._complete_authorized_effect(authorization, error=exc)
            raise
        # I-R1：预留是上界估计，effect 之后按 provider 上报的 usage 补差。
        self._reconcile_provider_usage(authorization, result)
        # I-R5：终态审计带上本次实际用量（拿不到就是 None）。
        self._complete_authorized_effect(
            authorization, error=None,
            actual_tokens=_provider_reported_total_tokens(result),
        )
        return result

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ):
        authorization = self._authorize(
            messages=messages, tools=tools, extra_body=extra_body,
            max_tokens=max_tokens, stream=True,
        )
        try:
            async for event in object.__getattribute__(self, "_transport").chat_stream(
                messages, tools, temperature, max_tokens, extra_body,
            ):
                yield event
        except (GeneratorExit, asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # M-6：消费者提前 break / `aclose()`（或任务被取消）会让 `GeneratorExit`
            # 从下面的 `yield` 处抛出。它是控制流信号，不是 provider 故障——旧实现把它
            # 交给 `except BaseException`，于是审计里出现一次假的 `failed/GeneratorExit`。
            # `GeneratorExit` 必须重新抛出，否则 async generator 会报
            # "async generator ignored GeneratorExit"。`KeyboardInterrupt` /
            # `SystemExit` 同理（M-4，独立复审实测它们此前记成 `failed`）。
            #
            # ⚠ M-5 已知限制（复审实测，本段修不掉）：这段清理代码**不保证执行**。消费者
            # 若既不 `aclose()` 也不让事件循环 `shutdown_asyncgens()`（例如直接在已关闭的
            # 循环上丢弃生成器），终态事件可以是**空的**——审计里连 `cancelled` 都没有。
            # 这是 async generator 的终结语义，不是这里能补的；要彻底解决得让调用方持有
            # 生命周期（或在 effect 外层记录"开始过"，由恢复流程收敛）。
            self._reconcile_provider_usage(authorization, None, failed=True)
            self._complete_authorized_effect(authorization, error=None, cancelled=True)
            raise
        except BaseException as exc:
            self._reconcile_provider_usage(authorization, None, failed=True)
            self._complete_authorized_effect(authorization, error=exc)
            raise
        # ⚠ I-R1 未尽之处（已记入台账）：流式路径**不做 usage 对账**，因为 usage 只出现在
        # SSE 的最后一个 chunk 里，而这里拿到的是生成器，逐个 yield 时看不到"最终用量"。
        # 轮次/成本仍按实际尝试次数结算（下面这次调用），所以预算是被约束的，只是
        # token 维度没有按实际用量做二次校正。
        self._reconcile_provider_usage(authorization, None)
        self._complete_authorized_effect(authorization, error=None)


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
