# [v1.0.13][R1] Model apply acknowledgements, readiness state and idempotent wake signals.
"""
runtime_settings.py — [v0.44] 设置面板下发的**运行时设置**（Harness 层的唯一权威）

摆放位置：backend/runtime_settings.py（与 config.py 同包）

── 为什么单独一个模块 ──
config.py 是「装机常量」：进程起来就定死（frozen dataclass）。设置面板要的是
「运行中可改、重启后还在」——两种生命周期硬塞一起会互相打架。于是：

    CONFIG.approval_timeout_s  ← 改成了 property，转发到本模块（config.py [v0.44]）
    engine._new_agent(...)     ← 先问本模块要模型绑定，问不到再落回 CONFIG.deepseek_*
    server.py /settings        ← HTTP 入口，收到就 apply() + 落盘

── 三条硬规矩（README §2.3 / §3.2 / §3.3）──
 1. 全局覆盖个性：apply() 里只要检测到 main_model 变了 → 清空全部 per-Agent 绑定；
    approval_timeout_s 变了 → 清空全部群级超时。前端也做同样的清空，但**这里是兜底
    权威**——哪怕来包的是个老版本前端，规矩也立得住。
 2. 审批超时按「群级 > 全局」解析，群从 contextvar 里拿（engine._loop 进场即登记，
    propose 都发生在 _loop 的后代任务里，contextvar 自动继承——gate.py 一行不用改）。
 3. 「永不超时」在权威状态中就是 None；只有展示层需要时才使用远期日期。
    前端下发 null / 0 都归一到 None，Gate 不创建自动 timeout 任务。

── 凭据边界 ──
Python 配置只保存 Electron safeStorage 的 opaque SecretRef 与 main 派生的绑定元数据。
Provider 调用点只能兑换 purpose-bound 短租；dotenv、环境变量和 settings.json 明文均不是
凭据来源。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Mapping

from knowe_core.provider_identity import provider_target
from .i18n_backend import msg

log = logging.getLogger("knowe.settings")

_SECRET_REF_RE = re.compile(r"^secret_[0-9a-f]{32}$")
_CREDENTIAL_PURPOSE_RE = re.compile(r"^provider:[0-9a-f]{64}$")
_CREDENTIAL_SLOT_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_secret_lease_broker: Any | None = None


def set_secret_lease_broker(broker: Any | None) -> None:
    """Inject the server-owned in-memory lease receiver; never a credential value."""

    global _secret_lease_broker
    _secret_lease_broker = broker
    _signal_model_waiters()


def resolve_provider_credential(raw: Any) -> str:
    """Resolve one purpose-bound short lease at the provider request boundary."""

    binding = _norm_binding(raw)
    broker = _secret_lease_broker
    if binding is None or broker is None:
        raise RuntimeError("provider credential lease is unavailable")
    secret_ref = binding.get("secret_ref", "")
    purpose = binding.get("credential_purpose", "")
    slot = binding.get("credential_slot", "")
    if (
        not secret_ref
        or not purpose
        or not slot
        or binding.get("credential_kind") != "provider_credential"
        or binding.get("credential_audience") != "provider_gateway"
    ):
        raise RuntimeError("provider credential lease is unavailable")
    try:
        from .commercial_gateway import derive_provider_credential_purpose

        expected = derive_provider_credential_purpose(
            slot=slot,
            provider_id=binding["provider"],
            endpoint=binding["base_url"],
            transport=binding["transport"],
        )
        if not hmac.compare_digest(purpose, expected):
            raise RuntimeError("provider credential purpose is invalid")
        value = broker.redeem_provider_credential(
            secret_ref,
            slot=slot,
            provider_id=binding["provider"],
            endpoint=binding["base_url"],
            transport=binding["transport"],
            expected_purpose=expected,
        )
    except Exception as exc:
        raise RuntimeError("provider credential lease is unavailable") from exc
    if not value:
        raise RuntimeError("provider credential lease is unavailable")
    return str(value)


class SettingsApplyConflict(ValueError):
    """The tested binding is no longer the binding being applied."""

#: 「永不超时」的实际秒数：100 年。够所有实际用途当作永不；又是普通 float，
#: gate 里任何 `asyncio.wait_for(..., timeout=...)` / `now + timeout` 的算术都装得下。
NEVER_TIMEOUT_S: float = 3_153_600_000.0

#: 出厂默认：审批等待没有自动墙钟截止，由用户或外部取消显式落定。
DEFAULT_APPROVAL_TIMEOUT_S: float | None = None

# ═══════════════════════════════════════════════════════════════
# 当前项目 contextvar（engine._loop 进场登记；propose 在其后代任务里自动继承）
# ═══════════════════════════════════════════════════════════════

_current_project: ContextVar[str | None] = ContextVar("knowe_current_project", default=None)


def set_current_project(project_id: str | None) -> object:
    """登记当前任务树属于哪个项目。返回 token，可用于 reset。"""
    return _current_project.set(project_id)


def reset_current_project(token: object) -> None:
    try:
        _current_project.reset(token)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — token 跨任务失效时静默（兜底路径）
        pass


@contextmanager
def project_context(project_id: str | None) -> Iterator[None]:
    """with 版：`with project_context(pid): ...`（Engine.__init__ 造 Gate 时用）。"""
    token = _current_project.set(project_id)
    try:
        yield
    finally:
        try:
            _current_project.reset(token)
        except Exception:  # noqa: BLE001
            pass


# ═══════════════════════════════════════════════════════════════
# 状态 + 持久化
# ═══════════════════════════════════════════════════════════════

def _default_state() -> dict[str, Any]:
    return {
        # [v1.0.13 R1] Persisted revision and per-install HMAC salt.  The salt never
        # leaves this module; clients receive only opaque ``sha256:<digest>`` values.
        "settings_revision": 0,
        "fingerprint_salt": secrets.token_hex(32),
        "active_model_fingerprint": None,
        "user_name": "",                    # 空 = 前端还没下发过，取用方自兜底「用户」
        "language": "zh",                   # [v1.0.21.3] 主要语言：'zh' | 'en'（界面 + 提示词语言）
        # Credential plaintext is owned by Electron safeStorage.  Bindings retain only
        # an opaque SecretRef and its authorized lease purpose.
        "main_model": None,                 # {provider, model, secret_ref, credential_purpose, ...}
        "aux_model": None,                  # 同上（前端已把「跟随主模型便宜档」派生好再发）
        "agent_models": {},                 # {"<project_id>::<agent_id>": binding}
        "approval_timeout_s": DEFAULT_APPROVAL_TIMEOUT_S,   # None = 永不
        "group_approval_timeouts": {},      # {project_id: seconds | None}
    }


_state: dict[str, Any] = _default_state()
_loaded = False
_model_waiters: set[asyncio.Event] = set()


def _settings_path() -> Path | None:
    """settings.json 的落点：CONFIG.data_dir 下。纯内存模式（data_dir 空）→ 不落盘。"""
    from .config import CONFIG  # 局部 import：config.property → 本模块，避免环
    raw = (CONFIG.data_dir or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser() / "settings.json"


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    path = _settings_path()
    if path is None or not path.exists():
        _loaded = True
        return
    legacy_plaintext = False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            legacy_plaintext = any(
                isinstance(binding, dict) and bool(str(binding.get("api_key") or ""))
                for binding in (
                    raw.get("main_model"),
                    raw.get("aux_model"),
                    *((raw.get("agent_models") or {}).values()
                      if isinstance(raw.get("agent_models"), dict) else ()),
                )
            )
            base = _default_state()
            base.update({k: raw[k] for k in base.keys() & raw.keys()})
            if not str(base.get("fingerprint_salt") or "").strip():
                base["fingerprint_salt"] = secrets.token_hex(32)
            try:
                base["settings_revision"] = max(0, int(base.get("settings_revision") or 0))
            except (TypeError, ValueError):
                base["settings_revision"] = 0
            # Legacy settings.json files may contain plaintext API keys.  They are
            # untrusted input after this migration: normalize bindings into opaque
            # handles only and never keep the old value in Python memory.
            base["main_model"] = _norm_binding(base.get("main_model"))
            base["aux_model"] = _norm_binding(base.get("aux_model"))
            agents = base.get("agent_models")
            base["agent_models"] = {
                str(key): binding
                for key, value in (agents.items() if isinstance(agents, dict) else ())
                if (binding := _norm_binding(value)) is not None
            }
            # Upgrade compatibility: a pre-v1.0.13 persisted main binding was already
            # effective in production, so activate it once during migration.  A truly
            # new install starts without a binding and still requires test→apply.
            if raw.get("main_model") and "active_model_fingerprint" not in raw:
                base["active_model_fingerprint"] = None
            if legacy_plaintext:
                # Successful load is also the one-way disk migration.  Keeping the
                # plaintext in an old file would remain a credential bypass even if
                # the current Python process had already dropped it from memory.
                _persist(base)
            _state.clear()
            _state.update(base)
            _loaded = True
            log.info(msg("runtime_settings.py.001"), path)
        else:
            _loaded = True
    except Exception as exc:
        if legacy_plaintext:
            # Loading an in-memory redacted copy while leaving plaintext durable
            # would preserve a credential bypass.  Keep the authority unloaded and
            # block provider readiness until the old file can be cleaned/quarantined.
            _state.clear()
            _state.update(_default_state())
            _loaded = False
            raise RuntimeError(
                "legacy plaintext provider settings could not be removed safely"
            ) from exc
        _loaded = True
        log.warning(msg("runtime_settings.py.002"), exc)


def _persist(candidate: Mapping[str, Any] | None = None) -> None:
    """Atomically persist a candidate state; write failures are part of apply's result."""
    path = _settings_path()
    if path is None:
        return
    source = _state if candidate is None else candidate
    value = json.loads(json.dumps(source, ensure_ascii=False))
    value["main_model"] = _norm_binding(value.get("main_model"))
    value["aux_model"] = _norm_binding(value.get("aux_model"))
    agents = value.get("agent_models")
    value["agent_models"] = {
        str(key): binding
        for key, raw in (agents.items() if isinstance(agents, dict) else ())
        if (binding := _norm_binding(raw)) is not None
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# ═══════════════════════════════════════════════════════════════
# 归一化小工具
# ═══════════════════════════════════════════════════════════════

def _norm_binding(raw: Any) -> dict[str, str] | None:
    """校验并收窄模型绑定；credential plaintext is never retained."""
    if not isinstance(raw, dict):
        return None
    provider = str(raw.get("provider") or "").strip()
    model = str(raw.get("model") or "").strip()
    if not provider or not model:
        return None
    secret_ref = str(raw.get("secret_ref") or "").strip()
    purpose = str(raw.get("credential_purpose") or raw.get("purpose") or "").strip()
    slot = str(raw.get("credential_slot") or raw.get("slot") or "").strip()
    kind = str(raw.get("credential_kind") or raw.get("kind") or "").strip()
    audience = str(raw.get("credential_audience") or raw.get("audience") or "").strip()
    base_url = str(raw.get("base_url") or "").rstrip("/")
    transport = str(raw.get("transport") or "openai_chat")
    if secret_ref and _SECRET_REF_RE.fullmatch(secret_ref) is None:
        secret_ref = ""
    if purpose and _CREDENTIAL_PURPOSE_RE.fullmatch(purpose) is None:
        purpose = ""
    if slot and _CREDENTIAL_SLOT_RE.fullmatch(slot) is None:
        slot = ""
    if secret_ref:
        try:
            from .commercial_gateway import (
                _canonical_provider_endpoint,
                derive_provider_credential_purpose,
            )

            canonical_endpoint = _canonical_provider_endpoint(base_url)
            expected_purpose = derive_provider_credential_purpose(
                slot=slot,
                provider_id=provider,
                endpoint=canonical_endpoint,
                transport=transport,
            )
            if (
                canonical_endpoint != base_url
                or kind != "provider_credential"
                or audience != "provider_gateway"
                or not hmac.compare_digest(purpose, expected_purpose)
            ):
                raise ValueError
        except (ValueError, RuntimeError, PermissionError):
            secret_ref = ""
            purpose = ""
            slot = ""
            kind = ""
            audience = ""
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "transport": transport,
        "secret_ref": secret_ref,
        "credential_purpose": purpose,
        "credential_slot": slot,
        "credential_kind": kind,
        "credential_audience": audience,
    }


def _binding_for_apply(raw: Any, previous: Any) -> dict[str, str] | None:
    """Normalize handle-only binding and reject renderer-to-Python plaintext."""
    if isinstance(raw, dict) and str(raw.get("api_key") or ""):
        raise ValueError("provider credentials must enter through Electron secret leases")
    binding = _norm_binding(raw)
    if binding is None:
        return None
    if isinstance(raw, dict) and raw.get("secret_ref") and not binding["secret_ref"]:
        raise ValueError("provider credential binding metadata is invalid")
    prior = _norm_binding(previous)
    clear_requested = isinstance(raw, dict) and any(
        raw.get(key) is True for key in ("clear_api_key", "api_key_clear", "clearApiKey")
    )
    if clear_requested:
        binding["secret_ref"] = ""
        binding["credential_purpose"] = ""
        binding["credential_slot"] = ""
        binding["credential_kind"] = ""
        binding["credential_audience"] = ""
        return binding
    if (
        not binding["secret_ref"]
        and prior is not None
        and _same_credential_scope(binding, prior)
    ):
        binding["secret_ref"] = prior["secret_ref"]
        binding["credential_purpose"] = prior["credential_purpose"]
        binding["credential_slot"] = prior["credential_slot"]
        binding["credential_kind"] = prior["credential_kind"]
        binding["credential_audience"] = prior["credential_audience"]
    return binding


def _credential_scope(raw: Any) -> tuple[str, str, str] | None:
    binding = _norm_binding(raw)
    if binding is None:
        return None
    return (
        binding["provider"].strip().casefold(),
        binding["base_url"].strip().rstrip("/").casefold(),
        binding["transport"].strip().casefold(),
    )


def _same_credential_scope(left: Any, right: Any) -> bool:
    left_scope = _credential_scope(left)
    return left_scope is not None and left_scope == _credential_scope(right)


def _matching_previous(raw: Any, *candidates: Any) -> Any:
    """Return the first predecessor whose credential scope matches ``raw`` exactly."""
    for candidate in candidates:
        if _same_credential_scope(raw, candidate):
            return candidate
    return None


def binding_for_test(target: str, raw: Any) -> dict[str, str] | None:
    """Fill a redacted renderer test binding from persisted credentials when safe.

    GET /settings exposes only ``has_api_key``.  A subsequent connection test therefore sends
    an empty key; the backend may borrow its persisted value only when provider, base URL and
    transport still match.  A newly typed key always wins and is never copied back to the client.
    """

    _ensure_loaded()
    binding = _norm_binding(raw)
    if binding is None:
        return binding
    clear_requested = isinstance(raw, dict) and any(
        raw.get(key) is True for key in ("clear_api_key", "api_key_clear", "clearApiKey")
    )
    if isinstance(raw, dict) and str(raw.get("api_key") or ""):
        raise ValueError("provider credentials must enter through Electron secret leases")
    if clear_requested:
        binding["secret_ref"] = ""
        binding["credential_purpose"] = ""
        binding["credential_slot"] = ""
        binding["credential_kind"] = ""
        binding["credential_audience"] = ""
        return binding
    if binding["secret_ref"]:
        return binding
    prior: Any
    if str(target).strip().lower() == "aux":
        # Prefer an explicit/effective aux credential, then the persisted main credential for a
        # renderer-derived cheap-tier binding.  Scope matching prevents cross-provider reuse.
        prior = _matching_previous(raw, _state.get("aux_model"), _state.get("main_model"))
    else:
        prior = _matching_previous(raw, _state.get("main_model"))
    if prior is not None:
        previous = _norm_binding(prior)
        if previous is not None:
            binding["secret_ref"] = previous["secret_ref"]
            binding["credential_purpose"] = previous["credential_purpose"]
            binding["credential_slot"] = previous["credential_slot"]
            binding["credential_kind"] = previous["credential_kind"]
            binding["credential_audience"] = previous["credential_audience"]
    return binding


def _binding_complete(binding: dict[str, str] | None) -> bool:
    return bool(
        binding
        and binding.get("provider")
        and binding.get("model")
        and binding.get("secret_ref")
        and binding.get("credential_purpose")
        and binding.get("credential_slot")
        and binding.get("credential_kind") == "provider_credential"
        and binding.get("credential_audience") == "provider_gateway"
        and binding.get("base_url")
    )


def binding_fingerprint(raw: Any) -> str | None:
    """Return an opaque, stable per-install HMAC fingerprint for one binding.

    The covered fields are the exact apply/test transaction boundary requested by the
    first-run gate.  Only the opaque SecretRef is covered; credential plaintext never
    enters this state or fingerprint computation.
    """

    _ensure_loaded()
    binding = _norm_binding(raw)
    if binding is None:
        return None
    canonical = "|".join((
        binding["provider"].strip().casefold(),
        binding["model"].strip(),
        binding["base_url"].strip().rstrip("/").casefold(),
        binding["transport"].strip().casefold(),
        binding["secret_ref"],
        binding["credential_purpose"],
        binding["credential_slot"],
        binding["credential_kind"],
        binding["credential_audience"],
    )).encode("utf-8")
    salt = str(_state.get("fingerprint_salt") or "").encode("utf-8")
    digest = hmac.new(salt, canonical, hashlib.sha256).hexdigest()
    return f"sha256:{digest}"


def public_binding(raw: Any) -> dict[str, Any] | None:
    """Return a redacted binding suitable for local HTTP acknowledgements."""

    binding = _norm_binding(raw)
    if binding is None:
        return None
    return {
        "provider": binding["provider"],
        "model": binding["model"],
        "transport": binding["transport"],
        "base_url": binding["base_url"],
        "has_api_key": bool(binding["secret_ref"]),
        "secret_ref": binding["secret_ref"],
        "credential_purpose": binding["credential_purpose"],
        "credential_slot": binding["credential_slot"],
        "credential_kind": binding["credential_kind"],
        "credential_audience": binding["credential_audience"],
    }


def _zinnia_usable(raw: Any) -> bool:
    binding = _norm_binding(raw)
    return bool(
        _binding_complete(binding)
        and binding is not None
        and binding.get("transport", "openai_chat") in {"openai_chat", "codex_responses"}
    )


def zinnia_binding_status() -> tuple[bool, str, dict[str, str] | None]:
    """Resolve the same persisted binding precedence used by Zinnia."""

    _ensure_loaded()
    agents = _state.get("agent_models") or {}
    own = agents.get("__platform__::zinnia") if isinstance(agents, dict) else None
    main = own or _state.get("main_model")
    if _zinnia_usable(main):
        return True, "agent" if own else "main", dict(main)
    aux = _state.get("aux_model")
    if main and _zinnia_usable(aux):
        return True, "aux", dict(aux)
    return False, "incompatible" if main else "missing", dict(main) if isinstance(main, dict) else None


def model_ready_for(project_id: str, agent_id: str) -> bool:
    """Runtime readiness barrier predicate for one Agent turn.

    A binding is usable when complete: provider/model/base_url/api_key all present
    (per-Agent override first, then global main).  The old test→apply fingerprint
    ceremony was removed — saving a complete binding is the user's explicit
    confirmation, so no fingerprint handshake is required.
    """

    if project_id == "__platform__" and agent_id == "zinnia":
        return zinnia_binding_status()[0]
    _ensure_loaded()
    agents = _state.get("agent_models") or {}
    own = agents.get(f"{project_id}::{agent_id}") if isinstance(agents, dict) else None
    if own is not None:
        return _binding_complete(_norm_binding(own))
    main = _state.get("main_model")
    return bool(_binding_complete(_norm_binding(main)))


def _signal_model_waiters() -> None:
    # apply() runs on the server event-loop thread.  Avoid depending on asyncio.Event's
    # private ``_loop`` attribute; a vanished/cancelled waiter must never abort apply.
    for waiter in tuple(_model_waiters):
        try:
            waiter.set()
        except Exception:  # noqa: BLE001 — defensive wake-up path
            pass


async def wait_for_model_ready(
    project_id: str,
    agent_id: str,
    *,
    timeout: float | None = None,
) -> bool:
    """Wait for an apply transaction to make an Agent binding usable."""

    if model_ready_for(project_id, agent_id):
        return True
    waiter = asyncio.Event()
    _model_waiters.add(waiter)
    try:
        # Close the check/register race before awaiting.
        if model_ready_for(project_id, agent_id):
            return True
        while not model_ready_for(project_id, agent_id):
            waiter.clear()
            if timeout is None:
                await waiter.wait()
            else:
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    return model_ready_for(project_id, agent_id)
        return True
    finally:
        _model_waiters.discard(waiter)


def _norm_timeout(raw: Any) -> float | None:
    """秒数归一：None / 0 / 负数 / 超过 NEVER → None（永不）；其余 → float。"""
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_APPROVAL_TIMEOUT_S
    if v <= 0 or v >= NEVER_TIMEOUT_S:
        return None
    return v


def core_base_url(base: str) -> str:
    """
    [v0.44.3 场景2 修复] 目录口径 → knowe_core 口径的**唯一**换算点（片段保护版）
    （engine._core_base_url 与下面 test_binding 的 Agent 路径探测都走这里）。

    目录/测试/aux/知知用行业标准口径：base = 端点前缀（自带版本段，如 …/v1、…/paas/v4、
    …/v1beta/openai），请求层只追加 `/chat/completions`。所以任何厂商的**正确端点**永远是
    `{目录 base}/chat/completions`——知知就是这么打的（zinnia.py），全厂商通用、已验证可用。

    历史病根：knowe_core 的 ProviderClient 曾在 else 分支自补 `/v1/chat/completions`，对
    不以 /v1 结尾的厂商拼出 …/v4/v1/chat/completions 而 404（项目经理/Worker 走 core，知知不走
    core，所以只有他俩挂）。ProviderClient 的根源已同步修正（只追 /chat/completions、尊重
    base 版本段，见 knowe_core/provider_client）。

    这里再加一道**与 core 内部拼接实现解耦**的保护：把 base 换算成
    `{目录 base}/chat/completions#`。交给 core 后，无论 core 往后追加什么（新实现的
    /chat/completions、或历史上的 /v1/chat/completions），都落进 URL fragment；HTTP 层
    按 RFC 3986 §3.5 不发 fragment，实际请求恰好命中 `{目录 base}/chat/completions`——与
    知知/aux/目录**完全同址**。这样即便 core 侧回退到旧拼接口径，Agent 也照样打对地址。

    空 base 透传空串（保留下游对空 base 的兜底）；已是片段保护形态则幂等返回。
    """
    b = (base or "").rstrip("/")
    if not b or b.endswith("#"):          # 空串透传；已片段保护则幂等
        return b
    endpoint = b if b.endswith("/chat/completions") else f"{b}/chat/completions"
    return f"{endpoint}#"


# ═══════════════════════════════════════════════════════════════
# 对外：apply / 查询
# ═══════════════════════════════════════════════════════════════

def _revision_payload(state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    source = _state if state is None else state
    return {
        key: value
        for key, value in source.items()
        if key not in {"settings_revision", "fingerprint_salt"}
    }

def apply(payload: dict[str, Any], *, canonical_pid: Any = None) -> dict[str, Any]:
    """
    整包应用前端下发的设置（POST /settings 的处理核心）。

    优先级规矩（README §3.2 / §3.3）在两层落实：
      · **解析层**（真正的 Harness 保证）：effective_approval_timeout / model_binding_for
        永远按「个性化 > 全局 > 出厂」裁决——不管状态是怎么进来的。
      · **应用层**：前端的 saveMainModel / setApprovalTimeout 已经在发包前把对应的
        个性化表清空（全局覆盖个性），所以**完整包直接照单全收**；只有当包里
        全局值变了、却**没带**对应的个性化表（老客户端 / 手写请求）时，才在这里
        兜底清空——决不能因为「开机对账」这类正常同步吃掉用户的群级/Agent 级设置。

    canonical_pid：server 传进来的「项目 id 归一函数」（临时 id → 权威 id），
    群级超时与 per-Agent 键都过一遍，防止前端拿着握手前的临时 id 存出孤儿键。

    返回：应用后的生效摘要（回给前端做确认展示）。
    """
    _ensure_loaded()

    expected_revision = payload.get("expected_revision")
    if expected_revision not in (None, ""):
        try:
            expected_value = int(expected_revision)
        except (TypeError, ValueError) as exc:
            raise SettingsApplyConflict(msg("runtime_settings.py.003")) from exc
        current_revision = int(_state.get("settings_revision") or 0)
        if expected_value != current_revision:
            raise SettingsApplyConflict(
                msg("runtime_settings.py.004", expected_value=expected_value, current_revision=current_revision)
            )

    candidate = json.loads(json.dumps(_state, ensure_ascii=False))
    candidate_main = _binding_for_apply(payload.get("main_model"), candidate.get("main_model"))

    before = json.dumps(_revision_payload(candidate), ensure_ascii=False, sort_keys=True)

    norm = canonical_pid if callable(canonical_pid) else (lambda pid: pid)

    # ── 1. 主模型 / 辅助模型 ──
    new_main = candidate_main
    old_main = candidate.get("main_model")
    main_changed = bool(new_main) and new_main != old_main

    candidate["main_model"] = new_main if new_main else old_main
    # A derived auxiliary binding and a newly introduced per-Agent override may intentionally
    # omit the secret after GET /settings.  When there is no binding-specific predecessor, the
    # newly resolved main binding is the only safe fallback, and _binding_for_apply still requires
    # an exact provider/base_url/transport scope match before borrowing its key.
    aux_raw = payload.get("aux_model")
    aux_previous = _matching_previous(aux_raw, candidate.get("aux_model"), new_main)
    aux = _binding_for_apply(aux_raw, aux_previous)
    if aux is not None or "aux_model" in payload:
        candidate["aux_model"] = aux

    # ── 2. per-Agent 绑定 ──
    incoming_agents = payload.get("agent_models")
    if isinstance(incoming_agents, dict):
        # 完整包：前端已按「全局覆盖个性」把表清好了——照单全收（含空表）。
        cleaned: dict[str, dict[str, str]] = {}
        previous_agents = candidate.get("agent_models") or {}
        for key, raw in incoming_agents.items():
            raw_key = str(key)
            if "::" not in raw_key:
                continue
            pid, _, aid = raw_key.partition("::")
            canonical_key = f"{norm(pid)}::{aid}"
            previous = None
            if isinstance(previous_agents, dict):
                previous = previous_agents.get(canonical_key, previous_agents.get(raw_key))
            b = _binding_for_apply(raw, _matching_previous(raw, previous, new_main))
            if b is not None:
                cleaned[canonical_key] = b
        candidate["agent_models"] = cleaned
    elif main_changed:
        # 兜底：包里换了主模型却没带个性化表 → 按规矩清空，别让旧表压过新全局。
        candidate["agent_models"] = {}
        log.info(msg("runtime_settings.py.005"))

    # ── 3. 审批超时 ──
    timeout_changed = False
    if "approval_timeout_s" in payload:
        new_timeout = _norm_timeout(payload.get("approval_timeout_s"))
        timeout_changed = new_timeout != candidate.get("approval_timeout_s")
        candidate["approval_timeout_s"] = new_timeout
    incoming_groups = payload.get("group_approval_timeouts")
    if isinstance(incoming_groups, dict):
        # 完整包照单全收（前端改全局时已把群表清空后随包发来）。
        candidate["group_approval_timeouts"] = {
            str(norm(pid)): _norm_timeout(v) for pid, v in incoming_groups.items()
        }
    elif timeout_changed:
        candidate["group_approval_timeouts"] = {}
        log.info(msg("runtime_settings.py.006"),
                 msg("runtime_settings.py.007") if candidate["approval_timeout_s"] is None
                 else f"{candidate['approval_timeout_s']:g}s")

    # ── 4. 称呼 ──
    if "user_name" in payload:
        candidate["user_name"] = str(payload.get("user_name") or "").strip()

    # ── 4.1 主要语言 [v1.0.21.3] ──
    if "language" in payload:
        lang = str(payload.get("language") or "zh").strip().lower()
        candidate["language"] = lang if lang in ("zh", "en") else "zh"

    # v1.0.19.5: 保存完整绑定即视为用户确认（指纹闸门废除）。指纹仅作诊断记录，
    # 与「测试→应用」仪式完全解耦——用户填好 key 确认保存的那一刻，Agent 即可用。
    if candidate.get("main_model"):
        candidate["active_model_fingerprint"] = (
            binding_fingerprint(candidate.get("main_model")) or None
        )
    after = json.dumps(_revision_payload(candidate), ensure_ascii=False, sort_keys=True)
    if after != before:
        candidate["settings_revision"] = int(candidate.get("settings_revision") or 0) + 1
    # Publish only after the exact candidate is durably replaced on disk.
    _persist(candidate)
    _state.clear()
    _state.update(candidate)
    _signal_model_waiters()
    return snapshot()


def snapshot() -> dict[str, Any]:
    """当前生效设置的进程内完整快照；HTTP 边界必须改用 api_snapshot()."""
    _ensure_loaded()
    return json.loads(json.dumps(_state, ensure_ascii=False))


# Diagnostic/audit exports cross a different trust boundary from the localhost
# settings editor.  Keep one recursive scrubber here so evidence bundles never
# accidentally serialize credentials while ordinary non-secret values remain exact.
_SECRET_FIELD_NAMES = frozenset({
    "api_key",
    "authorization",
    "auth_token",
    "access_token",
    "refresh_token",
    "bearer_token",
    "token",
    "cookie",
    "cookies",
    "password",
    "passwd",
    "client_secret",
    "secret",
    "secret_key",
    "private_key",
    "fingerprint_salt",
})
_REDACTED_PLACEHOLDER = "[REDACTED]"


def _normalise_secret_field_name(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(".", "_").replace(" ", "_")


def _is_secret_field_name(value: Any) -> bool:
    name = _normalise_secret_field_name(value)
    if name in _SECRET_FIELD_NAMES:
        return True
    return any(
        name.endswith(suffix)
        for suffix in (
            "_api_key",
            "_authorization",
            "_auth_token",
            "_access_token",
            "_refresh_token",
            "_bearer_token",
            "_cookie",
            "_password",
            "_client_secret",
            "_secret_key",
            "_private_key",
            "_fingerprint_salt",
        )
    )


def _secret_is_unset(value: Any) -> bool:
    return value is None or value == "" or value == b""


def redact_secrets(value: Any, *, field_name: Any = None) -> Any:
    """Return a deep copy with credential-bearing fields replaced exactly once.

    Empty credentials stay empty so diagnostics can still distinguish an unset key
    from a configured-but-redacted key.  This function deliberately filters only
    structured credential fields; it never rewrites ordinary natural-language text.
    """

    if field_name is not None and _is_secret_field_name(field_name):
        return value if _secret_is_unset(value) else _REDACTED_PLACEHOLDER
    if isinstance(value, Mapping):
        return {key: redact_secrets(item, field_name=key) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    return value


def redacted_snapshot() -> dict[str, Any]:
    """Credential-safe settings projection for diagnostics and audit artifacts."""

    value = redact_secrets(snapshot())
    return dict(value) if isinstance(value, Mapping) else {}


def settings_revision() -> int:
    _ensure_loaded()
    return max(0, int(_state.get("settings_revision") or 0))


def model_apply_ack(*, welcome_state: str = "unknown") -> dict[str, Any]:
    """Redacted ModelApplyAckV1 shared by GET and POST /settings."""

    _ensure_loaded()
    compatible, source, _binding = zinnia_binding_status()
    main = _state.get("main_model")
    current = binding_fingerprint(main)
    # v1.0.19.5: 保存即确认——当前配置的指纹就是已激活指纹，无需事务比对。
    acknowledged = current
    return {
        "settings_revision": settings_revision(),
        "applied_fingerprint": acknowledged,
        "effective": {"main_model": public_binding(main)},
        "zinnia_compatible": compatible,
        "zinnia_binding_source": source,
        "welcome_state": welcome_state,
    }


def api_snapshot(*, welcome_state: str = "unknown") -> dict[str, Any]:
    """Public settings snapshot plus readiness ack; never serialize credential values."""

    value = snapshot()
    # The per-install HMAC salt is process-private key material.  The renderer
    # needs only the opaque applied fingerprint returned by model_apply_ack().
    value.pop("fingerprint_salt", None)
    value.pop("active_model_fingerprint", None)
    value["main_model"] = public_binding(value.get("main_model"))
    value["aux_model"] = public_binding(value.get("aux_model"))
    raw_agents = value.get("agent_models")
    value["agent_models"] = {
        str(key): public
        for key, raw in (raw_agents.items() if isinstance(raw_agents, dict) else [])
        if (public := public_binding(raw)) is not None
    }
    value.update(model_apply_ack(welcome_state=welcome_state))
    return value


def user_name(default: str | None = None) -> str:
    """全局对用户的称呼（README §2.1：改名后所有上下文同步）。"""
    _ensure_loaded()
    return str(_state.get("user_name") or "").strip() or (default if default is not None else msg("runtime_settings.py.017"))


def language(default: str = "zh") -> str:
    """当前主要语言（'zh' | 'en'）。PromptResolver / 前端对账都读这里。"""
    _ensure_loaded()
    lang = str(_state.get("language") or "").strip().lower()
    return lang if lang in ("zh", "en") else default


def set_approval_timeout_compat(value: Any) -> float | None:
    """Set the process-local global timeout for legacy callers and tests.

    Before runtime settings became the authority, callers occasionally used
    ``object.__setattr__(CONFIG, "approval_timeout_s", value)``.  The compatibility
    property setter delegates here so that those callers update the same authoritative
    in-memory state instead of shadowing it on the frozen Config instance.  This path is
    intentionally non-persistent; durable settings still go through :func:`apply`.
    """

    _ensure_loaded()
    normalized = _norm_timeout(value)
    _state["approval_timeout_s"] = normalized
    _state["group_approval_timeouts"] = {}
    return None if normalized is None else float(normalized)


def effective_approval_timeout(project_id: str | None = None) -> float | None:
    """
    审批卡超时的**最终裁决**（CONFIG.approval_timeout_s property 的真身）：

        群级覆盖（project_id 或 contextvar 里的当前项目） > 全局值 > 出厂永不
        None 表示 Gate 直接等待显式 resolution，不创建自动超时。

    gate.py 每次读 CONFIG.approval_timeout_s 都会走到这里——群在哪儿来？
    engine._loop 进场时把项目 id 登进 contextvar，propose 全部发生在 _loop 的
    后代任务里，asyncio 的上下文继承把 id 一路带过去。
    """
    _ensure_loaded()
    pid = project_id if project_id is not None else _current_project.get()
    if pid:
        groups = _state.get("group_approval_timeouts") or {}
        if pid in groups:
            v = groups[pid]
            return None if v is None else float(v)
    v = _state.get("approval_timeout_s", DEFAULT_APPROVAL_TIMEOUT_S)
    return None if v is None else float(v)


def model_binding_for(project_id: str, agent_id: str) -> dict[str, str] | None:
    """
    某个 Agent 的生效模型绑定：个性化（"pid::aid"） > 全局主模型 > None。
    None = 用户没在设置面板配过；provider 出口必须 fail closed。
    """
    _ensure_loaded()
    agents = _state.get("agent_models") or {}
    own = agents.get(f"{project_id}::{agent_id}")
    if own:
        return dict(own)
    main = _state.get("main_model")
    return dict(main) if main else None


#: 各厂商便宜档（backend 兜底副本——正常情况下前端把派生辅助绑定发过来了；
#: 只有「前端只发了 main、没发 aux」的旧包才会用到这张表）。
_CHEAP_TIER: dict[str, str] = {
    "openrouter": "deepseek/deepseek-v4-flash",
    "deepseek": "deepseek-v4-flash",
    "zai": "glm-4.5-flash",
    "kimi-coding": "kimi-k2.5",
    "kimi-coding-cn": "kimi-k2.5",
    "alibaba": "qwen3.5-plus",
    "minimax": "MiniMax-M2",
    "minimax-cn": "MiniMax-M2",
    "stepfun": "step-3.5-flash",
    "tencent-tokenhub": "hy3-preview",
    "xai": "grok-4.20-0309-non-reasoning",
    "anthropic": "claude-haiku-4-5-20251001",
    "openai-api": "gpt-5.4-nano",
    "gemini": "gemini-3.6-flash",
    "nvidia": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "huggingface": "XiaomiMiMo/MiMo-V2-Flash",
    "novita": "deepseek/deepseek-v3-0324",
    "arcee": "trinity-mini",
    "gmi": "google/gemini-3.1-flash-lite-preview",
    "copilot": "gpt-4o-mini",
    "opencode-zen": "gpt-5-nano",
}


def aux_effective() -> dict[str, str] | None:
    """
    辅助模型（摘要/翻译/知识蒸馏）的生效绑定：
        显式 aux > 由主模型派生（同厂商同 SecretRef，模型换便宜档） > None（fail closed）。
    """
    _ensure_loaded()
    aux = _state.get("aux_model")
    if aux:
        return dict(aux)
    main = _state.get("main_model")
    if not main:
        return None
    derived = dict(main)
    # [customAPI] custom 等非官方 provider 不在 _CHEAP_TIER → 回退主模型名 = 完整跟随语义
    # （有意为之：自定义端点无便宜档，唯一合理语义是复用主模型配置，非漏网）。
    derived["model"] = _CHEAP_TIER.get(main.get("provider", ""), main.get("model", ""))
    return derived





# ═══════════════════════════════════════════════════════════════
# 连接测试（POST /settings/test 的真身 —— 真发请求，禁 mock）
# ═══════════════════════════════════════════════════════════════

_TEST_TIMEOUT_S = 15

#: Settings 连接测试专用的持久 group 名（单本地用户，一个就够）。
_PROBE_GROUP_NAME = "settings-probe"
#: 探针 run 的标题。
_PROBE_TITLE = "Settings provider probe"
#: 探针 run 的有界保留数（I5）：每次点击建一个短命 run，超出部分清理，避免无界增长。
_PROBE_RUN_RETENTION = 20


def probe_authority(
    groups: Any, runtime: Any,
) -> tuple[Any, str]:
    """为一个 Settings 探针建立**短命但有据**的 Task Runtime authority（I-R8）。

    旧实现直接用普通 `ProviderGatewayClient` 且不带 authority，于是 Gateway 在联网前
    就以 `GatewayDenied` 拒绝——Settings 的"连接测试"功能**必然失败**，而且既没有红测
    也没有绿测记录这个事实。

    这里不再走"打开 ungoverned seam"那条路（那正是 C-R1 修掉的洞），而是建立
    backend-authored 的短命 run：固定 group、单回合、低风险、只读。

    ⚠ I5 修复：旧实现用**固定 request_key** 复用同一个 run 且从不收尾。复审实测后果：
    第 21 次点击时该 run 撞上默认 `model_turns=20` 被判 `BUDGET_EXHAUSTED`，随后
    `probe_authority` 抛 `RuntimeError`（而它在 `_probe_once` 的 try 之外），
    **连接测试从此永久失效**，还留下一个永不结束的 RUNNING run。

    现在每次调用都用唯一 request_key 建新 run，并把 run_id 交回调用方，由
    `_probe_once` 在探针结束后立即收尾——"short-lived" 因此名副其实。
    """

    from .commercial_gateway import LegacyToolAuthority
    from .task_runtime.contracts import RiskLevel, TaskStatus

    group = None
    for candidate in groups.list_groups():
        if getattr(candidate, "name", "") == _PROBE_GROUP_NAME:
            group = candidate
            break
    if group is None:
        group = groups.create_group(_PROBE_GROUP_NAME)

    def _running_probe_run() -> Any:
        """找到一个仍处于 RUNNING 的探针 run（没有就 None）。

        为什么是"复用仍存活的、终结后换新"而不是每次新建：调度器有并发上限，
        新提交的 run 不保证立刻被调度到 RUNNING（实测：已有 run 占位时
        `schedule()` 不会把它推起来），而 Gateway 的 authority 校验要求 RUNNING。
        复用则天然满足，且只在旧 run 终结（例如撞上 model_turns 上限）时换新——
        这同时解决了 I5 的"永久失效"。
        """

        try:
            runs = runtime.store.list_runs(
                group_id=group.group_id, statuses={TaskStatus.RUNNING},
            )
        except Exception:
            return None
        for run in runs:
            if getattr(run, "title", "") == _PROBE_TITLE:
                return run
        return None

    run = _running_probe_run()
    if run is not None:
        # 复用一个仍存活的探针 run（并发下避免互相 cancel，见 _running_probe_run 注释）。
        prune_probe_runs(groups, runtime, group.group_id)
        return (
            LegacyToolAuthority(
                group_id=group.group_id,
                task_id=run.task_id,
                run_id=run.run_id,
                runtime=runtime,
            ),
            run.run_id,
        )

    run = runtime.submit(
        group.group_id,
        title=_PROBE_TITLE,
        request_key=f"settings-probe:{secrets.token_hex(16)}",
        # 只读 + 低风险：它不写任何用户工作区，也不碰高风险副作用。
        read_only=True,
        risk=RiskLevel.LOW,
        public_summary="Settings connection test",
    )
    # 只有 RUNNING 的 run 才能通过 Gateway 的 authority 校验。
    if getattr(run, "status", None) is not TaskStatus.RUNNING:
        for scheduled in runtime.schedule():
            if scheduled.run_id == run.run_id:
                run = scheduled
                break
    if getattr(run, "status", None) is not TaskStatus.RUNNING:
        raise RuntimeError("settings probe run is not running")
    # I5：建完新 run 后把该 group 里多余的终态 run 清掉，避免随点击次数无界增长。
    prune_probe_runs(groups, runtime, group.group_id)
    return (
        LegacyToolAuthority(
            group_id=group.group_id,
            task_id=run.task_id,
            run_id=run.run_id,
            runtime=runtime,
        ),
        run.run_id,
    )


def prune_probe_runs(groups: Any, runtime: Any, group_id: str) -> int:
    """清理探针 group 里多余的终态 run（I5），返回删除条数。

    只作用于该 group、只删终态 run，动作很窄；失败不影响探针本身。
    """

    if groups is None or runtime is None:
        return 0
    try:
        return int(
            runtime.store.prune_terminal_runs(
                group_id=group_id, keep=_PROBE_RUN_RETENTION,
            )
        )
    except Exception:
        return 0


def _finish_probe_run(groups: Any, runtime: Any, run_id: str) -> None:
    """收尾探针 run：探针结束即**取消**它，不留 RUNNING 状态（I5）。

    为什么用 `cancel` 而不是 `complete`：`TaskRuntimeService.complete` 要求 run 先进入
    `VERIFYING` 并且有一份 task-bound Verifier 决定——探针既没有验证者也不产出可验收
    结果，套那条路径是硬塞。探针 run 的生命周期就是"一次连通性验证"，用完取消是正确
    语义，也正好满足 `probe_authority` 里"只复用 RUNNING run"的前提。
    """

    if groups is None or runtime is None or not run_id:
        return
    try:
        stored = runtime.store.get_run(run_id)
        group_id = stored.group_id if stored is not None else ""
    except Exception:
        return
    if not group_id:
        return
    try:
        runtime.cancel(
            group_id,
            run_id,
            request_key=f"settings-probe-cancel:{secrets.token_hex(16)}",
        )
    except Exception:
        # 收尾失败不影响探针结论；下一次点击会用新的 run，不会永久损坏。
        return


def _probe_once(
    binding: Mapping[str, Any], *, groups: Any = None, runtime: Any = None,
) -> tuple[bool, str]:
    """Run the settings probe through the same governed provider boundary as Agents."""

    from .commercial_gateway import (
        ProviderGatewayClient,
        legacy_tool_authority_context,
    )

    selected = dict(binding)
    run_id = ""
    try:
        authority, run_id = (
            probe_authority(groups, runtime)
            if groups is not None and runtime is not None
            else (None, "")
        )
    except Exception as exc:
        # I5：authority 建立失败必须变成 typed category，而不是逃出到 HTTP handler
        # （旧实现让它抛 RuntimeError 出界）。
        return False, type(exc).__name__

    async def run_probe() -> None:
        client = ProviderGatewayClient(
            base_url=selected["base_url"],
            api_key="",
            model=selected["model"],
            provider=selected["provider"],
            transport=selected["transport"],
            timeout=_TEST_TIMEOUT_S,
            max_retries=0,
            credential_provider=(
                lambda exact=selected: resolve_provider_credential(exact)
            ),
        )
        try:
            await client.chat(
                [{"role": "user", "content": "ping"}], max_tokens=1,
            )
        finally:
            await client.aclose()

    try:
        with legacy_tool_authority_context(authority):
            asyncio.run(run_probe())
        return True, ""
    except Exception as exc:  # settings response exposes only a typed category
        return False, type(exc).__name__
    finally:
        # 探针结束即收尾：不留 RUNNING run，也就不会积累到撞上限而永久失效。
        _finish_probe_run(groups, runtime, run_id)


def _test_binding_impl(
    binding: dict[str, Any], *, groups: Any = None, runtime: Any = None,
) -> dict[str, Any]:
    """
    对一份绑定真发**最小请求**（max_tokens=1），验证接入点 + Key + 模型三件事。
    同步阻塞实现（urllib）——server 用 asyncio.to_thread 包着调。

    [v0.44.3 场景2] 还会**再探一条 Agent 实际路径**（所有 transport 都探）：
    knowe_core 的 ProviderClient 按 OpenAI 兼容方式使用 Bearer，并尊重 base 自带的版本段，
    最终请求 ``{base}/chat/completions``。标准探测与 Agent 路径不一定使用同一协议
    （例如 Anthropic messages），所以两条都要核对：前者验证 Key/模型/厂商，后者验证
    引擎真正会打的路径；后者 404/405 → 如实报不兼容。

    返回 {"ok": bool, "message": str, "latency_ms": int | None}。
    """
    b = _norm_binding(binding)
    if b is None:
        return {"ok": False, "message": msg("event.test.binding.impl.01"), "latency_ms": None}
    base = b["base_url"]
    target = provider_target(b["provider"], base, b["model"])
    if not base:
        return {
            "ok": False,
            "message": msg("runtime_settings.py.010", target=target),
            "latency_ms": None,
        }

    ok, failure_type = _probe_once(b, groups=groups, runtime=runtime)
    if not ok:
        return {
            "ok": False,
            "message": f"{target}: governed Provider probe unavailable ({failure_type})",
            "latency_ms": None,
        }
    return {
        "ok": True,
        "message": msg("runtime_settings.py.016", target=target),
        "latency_ms": None,
    }


def test_binding(
    binding: dict[str, Any], *, groups: Any = None, runtime: Any = None,
) -> dict[str, Any]:
    """Run the real probe and bind the result to the exact tested configuration.

    I-R8：传入 `groups`/`runtime` 时，探针会在一个 backend-authored 的短命 Task Runtime
    run 内执行 —— 这是它唯一能通过 Gateway authority 的方式，也是"连接测试"真正可用的
    前提。不传则保持旧行为（探针会在联网前被 Gateway 拒绝，返回 typed category）。
    """

    result = _test_binding_impl(binding, groups=groups, runtime=runtime)
    result["tested_fingerprint"] = binding_fingerprint(binding)
    result["zinnia_compatible"] = _zinnia_usable(binding)
    return result
