"""供 agent 行为测试使用的**真实** provider 授权。

`_trusted_test_transport` 已经删除，所以任何经过 `ProviderGatewayClient` 的测试都必须
拿得出真实的授权上下文——一段 RUNNING 的 Task Runtime run，以及（当 agent 的身份来自
凭据 binding 时）一条真的 provider 凭据 lease。

这个模块把那套样板收在一处，供 `test_deepseek.py` / `test_zinnia.py` /
`test_v21_zinnia.py` 复用。它**不放松任何策略**：registry 登记、DLP、预算预留与
provider_egress 审计全部照常执行，被替换掉的只有最底层的 socket（MockTransport）。
"""

from __future__ import annotations

import contextlib
import contextvars
import itertools
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from backend.commercial_gateway import (
    LegacyToolAuthority,
    SecretLeaseBroker,
    derive_provider_credential_purpose,
    legacy_tool_authority_context,
)
from backend.groups import GroupRepository
from backend.task_runtime.contracts import TaskStatus
from backend.task_runtime.service import TaskRuntimeService

#: 必须与 `production_model_capability_registry()` 登记的三元组一致，否则授权会
#: （正确地）拒绝。改这里之前先改 registry——不是反过来。
DEEPSEEK_ENDPOINT = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"
DEFAULT_TRANSPORT = "openai_chat"

_CURRENT_STORE: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "agent_authority_store", default=None,
)
_STORE_SEQUENCE = itertools.count()


def authority_store() -> Path:
    """当前测试独占的一个空目录（由 conftest 的 autouse fixture 提供）。"""

    base = _CURRENT_STORE.get()
    if base is None:
        raise RuntimeError(
            "authority_store() is only available inside a pytest test; "
            "the conftest fixture supplies the per-test directory"
        )
    return base / f"provider-authority-{next(_STORE_SEQUENCE)}"


def set_authority_store(base: Path | None) -> contextvars.Token[Path | None]:
    """由 conftest 调用；返回的 token 交回 `reset_authority_store`。"""

    return _CURRENT_STORE.set(base)


class AgentProviderAuthority:
    """一个 RUNNING run + 一条真实凭据 lease，供注入假 transport 的 agent 测试使用。"""

    def __init__(
        self,
        store: Path,
        *,
        provider_id: str = "deepseek",
        endpoint: str = DEEPSEEK_ENDPOINT,
        model: str = DEEPSEEK_MODEL,
        transport: str = DEFAULT_TRANSPORT,
        slot: str = "main",
        secret: str = "sk-test-value",
        group_name: str = "agent-authority",
    ) -> None:
        self.provider_id = provider_id
        self.endpoint = endpoint
        self.model = model
        self.transport = transport
        self.slot = slot
        self.secret = secret

        self.groups = GroupRepository(store)
        self.runtime = TaskRuntimeService(store, self.groups)
        self.group = self.groups.create_group(group_name)
        self.runtime.submit(
            self.group.group_id, title=group_name, request_key=f"{group_name}:1",
        )
        self.run = self.runtime.schedule()[0]
        if self.run.status is not TaskStatus.RUNNING:
            raise RuntimeError(f"authority run did not start: {self.run.status}")

        self.purpose = derive_provider_credential_purpose(
            slot=slot, provider_id=provider_id, endpoint=endpoint, transport=transport,
        )
        self.secret_ref = "secret_" + secrets.token_hex(16)
        now = datetime.now(timezone.utc)
        self.broker = SecretLeaseBroker(sweep_interval_seconds=0)
        self.broker.provision(
            lease_id="lease_" + secrets.token_hex(16),
            secret_ref=self.secret_ref,
            slot=slot,
            kind="provider_credential",
            purpose=self.purpose,
            audience="provider_gateway",
            provider_id=provider_id,
            endpoint=endpoint,
            transport=transport,
            value=secret,
            # TTL 必须落在 broker 的 max_ttl_seconds 内，否则 provision 自己就会拒绝。
            expires_at=(now + timedelta(seconds=300)).isoformat(),
            now=now,
        )
        self.binding: dict[str, str] = {
            "provider": provider_id,
            "transport": transport,
            "base_url": endpoint,
            "model": model,
            "secret_ref": self.secret_ref,
            "credential_slot": slot,
            "credential_purpose": self.purpose,
            "credential_kind": "provider_credential",
            "credential_audience": "provider_gateway",
        }
        self.authority = LegacyToolAuthority(
            group_id=self.group.group_id,
            task_id=self.run.task_id,
            run_id=self.run.run_id,
            runtime=self.runtime,
        )

    def configure(self, agent: Any) -> None:
        """把 agent 指向这个已登记的 provider 与这条真实凭据 lease。

        设置的是 agent 本来就有的字段（base_url / model / provider 身份 / 凭据 binding），
        不是给测试新加的开关。
        """

        agent.base_url = self.endpoint
        agent.model = self.model
        agent.api_key = ""
        if hasattr(agent, "provider"):
            agent.provider = self.provider_id
        agent._credential_binding = dict(self.binding)  # noqa: SLF001

    @contextlib.contextmanager
    def active(self) -> Iterator[LegacyToolAuthority]:
        """在真实授权上下文中执行（凭据 lease 也一并装好）。"""

        from backend import runtime_settings

        previous = runtime_settings._secret_lease_broker  # noqa: SLF001
        runtime_settings._secret_lease_broker = self.broker  # noqa: SLF001
        try:
            with legacy_tool_authority_context(self.authority):
                yield self.authority
        finally:
            runtime_settings._secret_lease_broker = previous  # noqa: SLF001

    def close(self) -> None:
        self.broker.close()
        self.runtime.close()
