"""云端 embedding 的受权调用：Task 3 的第三种 provider egress。

plan 第 46 行：网关此前只有 `chat` 与 `chat_stream`，所以云端 embedding **不是**"换个模型名"
就能做成的事——它需要第三种受权调用：出站文本过 DLP、预留预算、写 `provider_egress` 审计。
这个文件盯的是"这条链一步都没少"，而不是"能不能拿到向量"。

embedding 与 chat 有三处实质差异，每一处都是可以悄悄做错的地方：

1. 能力要求是 `embedding`，不是 `chat`。用 chat 的能力去发 embedding 请求，等于"这个
   provider 到底提不提供该能力"这个判断根本不存在；
2. **没有输出 token**。embedding 不生成 token，把输出上限一起预留会让预算按一个不存在的
   量被扣掉，而且不报错；
3. 出站正文是一个**文本数组**，不是 messages。DLP 必须扫到它——扫不到就意味着学生的手机号
   可以一路绿灯送去向量化。

测试走真实授权路径，只在最底层把发送通道换成 `MockTransport`（Task 2 建立的模式）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from backend.commercial_gateway import (
    GatewayDenied,
    LegacyToolAuthority,
    ProviderGatewayClient,
    legacy_tool_authority_context,
)
from backend.groups import GroupRepository
from backend.knowe_core.provider_client import ProviderClient
from backend.task_runtime.contracts import TaskStatus
from backend.task_runtime.service import TaskRuntimeService

ALIBABA = "https://dashscope.aliyuncs.com"
DEEPSEEK = "https://api.deepseek.com"


def _runtime_with_run(tmp_path: Path, name: str):
    groups = GroupRepository(tmp_path)
    runtime = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group(name)
    run = runtime.submit(group.group_id, title=name, request_key=f"req-{name}")
    run = runtime.schedule()[0]
    assert run.status is TaskStatus.RUNNING
    return runtime, group, run


def _client(
    handler,
    *,
    provider: str = "alibaba",
    base_url: str = ALIBABA,
    model: str = "text-embedding-v4",
) -> ProviderGatewayClient:
    client = ProviderGatewayClient(
        base_url=base_url, api_key="k", model=model, provider=provider,
    )
    object.__setattr__(
        client,
        "_transport",
        ProviderClient(
            base_url=base_url,
            api_key="k",
            model=model,
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            _gateway_managed=True,
        ),
    )
    return client


def _vectors(*rows: list[float]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": [{"index": index, "embedding": row} for index, row in enumerate(rows)],
            "usage": {"prompt_tokens": 7, "total_tokens": 7},
        },
    )


def _recorded(handler_calls: list[httpx.Request]) -> list[dict]:
    return [json.loads(request.content.decode("utf-8")) for request in handler_calls]


def _egress_events(runtime, run) -> list:
    return [
        event
        for event in runtime.store.events_after(run.run_id, 0)
        if getattr(event, "event_type", "") == "provider_egress"
    ]


def _statuses(runtime, run) -> list[str]:
    return [
        str(event.public_payload.get("status"))
        for event in _egress_events(runtime, run)
    ]


# ── 正常路径 ──────────────────────────────────────────────────────────

def test_embedding_is_authorized_and_reaches_the_provider(tmp_path: Path) -> None:
    runtime, group, run = _runtime_with_run(tmp_path, "embed")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _vectors([0.5, -0.25])

    client = _client(handler)
    authority = LegacyToolAuthority(
        group_id=group.group_id, task_id=run.task_id, run_id=run.run_id, runtime=runtime,
    )

    async def call():
        with legacy_tool_authority_context(authority):
            return await client.embed(["一段学习笔记"])

    result = asyncio.run(call())

    assert len(calls) == 1
    assert str(calls[0].url).endswith("/embeddings"), calls[0].url
    assert _recorded(calls) == [{"model": "text-embedding-v4", "input": ["一段学习笔记"]}]
    assert result["data"][0]["embedding"] == [0.5, -0.25]
    runtime.close()


def test_embedding_writes_an_authorized_and_a_terminal_audit_event(tmp_path: Path) -> None:
    """I-R5 立的规矩对第三种调用同样成立。

    只有 effect 前的 `authorized`，就无法回答"请求到底发出去了没有、用了多少 token"——
    而这正是审计存在的理由。
    """
    runtime, group, run = _runtime_with_run(tmp_path, "audit")

    client = _client(lambda request: _vectors([0.1]))
    authority = LegacyToolAuthority(
        group_id=group.group_id, task_id=run.task_id, run_id=run.run_id, runtime=runtime,
    )

    async def call():
        with legacy_tool_authority_context(authority):
            await client.embed(["甲"])

    asyncio.run(call())

    statuses = _statuses(runtime, run)
    assert "authorized" in statuses, statuses
    assert "completed" in statuses, statuses
    terminal = next(
        event for event in _egress_events(runtime, run)
        if event.public_payload.get("status") == "completed"
    )
    assert "duration_ms" in terminal.public_payload
    assert terminal.public_payload.get("actual_tokens") == 7
    # 审计里不能出现出站正文。
    assert not ({"input", "messages", "body"} & set(terminal.public_payload))
    runtime.close()


# ── 能力 ──────────────────────────────────────────────────────────────

def test_a_provider_without_the_embedding_capability_is_refused(tmp_path: Path) -> None:
    """`deepseek` 在能力表里没有 embedding（`production_model_capability_registry`）。

    用它发 embedding 必须被拒：否则"这个 provider 提不提供该能力"这个判断等于不存在，
    用户拿到的是一个 provider 的 404，而网关这边一切正常。
    """
    runtime, group, run = _runtime_with_run(tmp_path, "nocap")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("被拒绝的请求不该发出去")

    client = _client(
        handler, provider="deepseek", base_url=DEEPSEEK, model="deepseek-chat",
    )
    authority = LegacyToolAuthority(
        group_id=group.group_id, task_id=run.task_id, run_id=run.run_id, runtime=runtime,
    )

    async def call():
        with legacy_tool_authority_context(authority):
            await client.embed(["甲"])

    with pytest.raises(GatewayDenied):
        asyncio.run(call())
    runtime.close()


def test_embedding_without_a_task_runtime_authority_is_refused(tmp_path: Path) -> None:
    """没有授权上下文时不能发 embedding——与 chat 同一条规矩。

    这条同时证明了 embed 走的是**同一个** `_authorize`：如果它自带一套宽松的检查，
    这里就会放行。
    """
    client = _client(lambda request: _vectors([0.1]))  # pragma: no cover - 不该被调用

    with pytest.raises(GatewayDenied, match="authoritative Task Runtime"):
        asyncio.run(client.embed(["甲"]))


# ── DLP ───────────────────────────────────────────────────────────────

def test_embedding_text_goes_through_the_dlp_gateway(tmp_path: Path) -> None:
    """出站文本里的 PII 必须触发审批要求。

    这是 embedding 相对 chat 最容易漏掉的一处：chat 扫的是 messages，而 embedding 的出站
    是一个文本数组。新加一条 egress 路径却忘了把它接进 DLP，等于学生的手机号可以一路绿灯
    送去向量化——而且从外面看不出任何异常。
    """
    runtime, group, run = _runtime_with_run(tmp_path, "dlp")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("需要审批的请求不该在拿到审批前发出去")

    client = _client(handler)
    authority = LegacyToolAuthority(
        group_id=group.group_id, task_id=run.task_id, run_id=run.run_id, runtime=runtime,
    )

    async def call():
        with legacy_tool_authority_context(authority):
            await client.embed(["家长联系电话 13800138000"])

    with pytest.raises(GatewayDenied, match="approval"):
        asyncio.run(call())
    runtime.close()


def test_a_clean_embedding_is_not_held_up_by_the_dlp_gateway(tmp_path: Path) -> None:
    """反过来也要成立：普通文本不该被 DLP 拦下。

    一条"永远要求审批"的 DLP 是安全的，但会让向量化完全不可用——那样这条路径等于没做。
    """
    runtime, group, run = _runtime_with_run(tmp_path, "clean")

    client = _client(lambda request: _vectors([0.1]))
    authority = LegacyToolAuthority(
        group_id=group.group_id, task_id=run.task_id, run_id=run.run_id, runtime=runtime,
    )

    async def call():
        with legacy_tool_authority_context(authority):
            return await client.embed(["第三章 二次函数的图像"])

    assert asyncio.run(call())["data"][0]["embedding"] == [0.1]
    runtime.close()


# ── 预算 ──────────────────────────────────────────────────────────────

def test_embedding_reserves_input_tokens_only(tmp_path: Path) -> None:
    """embedding 不生成 token，预留里就不能有输出那一份。

    输入只有几十个字符。如果实现把"输出上限"也算进来（chat 是需要那么做的），预留会跳到
    上千——预算是按这个数字扣的，等于每次向量化都白扣一大笔，而且不报错、只是额度悄悄
    用光。
    """
    runtime, group, run = _runtime_with_run(tmp_path, "budget")

    client = _client(lambda request: _vectors([0.1]))
    authority = LegacyToolAuthority(
        group_id=group.group_id, task_id=run.task_id, run_id=run.run_id, runtime=runtime,
    )

    async def call():
        with legacy_tool_authority_context(authority):
            await client.embed(["很短的一段"])

    asyncio.run(call())

    authorized = next(
        event for event in _egress_events(runtime, run)
        if event.public_payload.get("status") == "authorized"
    )
    reserved = authorized.public_payload["usage"]["tokens"]
    assert reserved < 200, reserved
    assert reserved > 0, reserved
    runtime.close()


# ── 失败与取消 ────────────────────────────────────────────────────────

def test_a_failed_embedding_is_audited_as_failed(tmp_path: Path) -> None:
    runtime, group, run = _runtime_with_run(tmp_path, "fail")

    client = _client(lambda request: httpx.Response(400, json={"error": "bad input"}))
    authority = LegacyToolAuthority(
        group_id=group.group_id, task_id=run.task_id, run_id=run.run_id, runtime=runtime,
    )

    async def call():
        with legacy_tool_authority_context(authority):
            await client.embed(["甲"])

    with pytest.raises(Exception) as caught:
        asyncio.run(call())
    assert type(caught.value).__name__ == "ProviderError", type(caught.value).__name__

    statuses = _statuses(runtime, run)
    assert "failed" in statuses, statuses
    assert "completed" not in statuses, statuses
    runtime.close()


def test_a_cancelled_embedding_is_audited_as_cancelled_not_failed(tmp_path: Path) -> None:
    """取消不是 provider 故障（M-6）。第三种调用类型同样要守住这条线。

    记成 `failed` 会让一次正常的"用户走开了"看起来像 provider 崩了，而这正是审计要能
    分辨的事情。
    """
    runtime, group, run = _runtime_with_run(tmp_path, "cancel")

    def handler(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError()

    client = _client(handler)
    authority = LegacyToolAuthority(
        group_id=group.group_id, task_id=run.task_id, run_id=run.run_id, runtime=runtime,
    )

    async def call():
        with legacy_tool_authority_context(authority):
            await client.embed(["甲"])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(call())

    statuses = _statuses(runtime, run)
    assert "cancelled" in statuses, statuses
    assert "failed" not in statuses, statuses
    runtime.close()


# ── 不能把 chat 弄坏 ──────────────────────────────────────────────────

def test_the_embeddings_endpoint_is_built_from_a_chat_base_url() -> None:
    """`base_url` 允许自带 `/chat/completions`（构造器的既有行为）。

    不把那一段剥掉就会拼出 `.../chat/completions/embeddings`——一个不存在的路径。provider
    回 404，而错误看起来像"这个模型不支持向量化"，于是人会去查模型而不是查 URL。
    """
    plain = ProviderClient(base_url=ALIBABA, api_key="k", model="m", _gateway_managed=True)
    with_chat = ProviderClient(
        base_url=f"{ALIBABA}/v1/chat/completions", api_key="k", model="m", _gateway_managed=True,
    )
    already = ProviderClient(
        base_url=f"{ALIBABA}/v1/embeddings", api_key="k", model="m", _gateway_managed=True,
    )

    assert plain._embeddings_endpoint == f"{ALIBABA}/embeddings"
    assert with_chat._embeddings_endpoint == f"{ALIBABA}/v1/embeddings"
    assert already._embeddings_endpoint == f"{ALIBABA}/v1/embeddings"


def test_chat_still_reserves_output_room(tmp_path: Path) -> None:
    """反向锁：chat 的输出预留**没有**被这次重构弄丢。

    "embedding 只预留输入"必须与"chat 预留输入加输出"**并存**才算做对了。如果两边都变小，
    看起来也能通过 embedding 那条测试，实际却是 I-R1 修好的东西（`max_tokens=None` 时不再
    预留 0）被撤掉了——预算是被约束的这件事就没了。
    """
    runtime, group, run = _runtime_with_run(tmp_path, "chat-budget")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    client = _client(
        handler, provider="openai-api", base_url="https://api.openai.com/v1", model="m",
    )
    authority = LegacyToolAuthority(
        group_id=group.group_id, task_id=run.task_id, run_id=run.run_id, runtime=runtime,
    )

    async def call():
        with legacy_tool_authority_context(authority):
            await client.chat([{"role": "user", "content": "很短"}])

    asyncio.run(call())

    authorized = next(
        event for event in _egress_events(runtime, run)
        if event.public_payload.get("status") == "authorized"
    )
    reserved = authorized.public_payload["usage"]["tokens"]
    # 输入只有几个字；如果输出预留还在，这个数字会被那个保守上限顶到上千。
    assert reserved > 200, reserved
    runtime.close()


def test_chat_still_works_after_authorization_learned_a_second_shape(tmp_path: Path) -> None:
    """`_authorize` 改成服务多种调用类型之后，chat 的既有行为必须原样保留。

    这里只是冒烟：一次普通 chat 仍然被授权、仍然到达 provider、仍然写两段审计。chat 的
    细则由 Task 2 的测试文件覆盖，它们必须与本文件一起通过——重构授权链的风险不在新功能
    上，而在旧路径上。
    """
    runtime, group, run = _runtime_with_run(tmp_path, "chat-smoke")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            },
        )

    client = _client(
        handler, provider="openai-api", base_url="https://api.openai.com/v1", model="m",
    )
    authority = LegacyToolAuthority(
        group_id=group.group_id, task_id=run.task_id, run_id=run.run_id, runtime=runtime,
    )

    async def call():
        with legacy_tool_authority_context(authority):
            return await client.chat([{"role": "user", "content": "hi"}])

    result = asyncio.run(call())

    assert len(calls) == 1
    assert str(calls[0].url).endswith("/chat/completions"), calls[0].url
    assert result["choices"][0]["message"]["content"] == "ok"
    assert "authorized" in _statuses(runtime, run)
    assert "completed" in _statuses(runtime, run)
    runtime.close()
