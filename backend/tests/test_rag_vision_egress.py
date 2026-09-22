"""BYOK 视觉读取：网关的第四种受权调用。

plan 第 37 行：没有文本层的页面渲染成图片后，交给**多模态模型**读。这一层与 embedding
（`test_rag_embedding_egress.py`）走的是同一条授权链——因为 `_EgressShape` 把它参数化了，
而不是复制了那一百来行安全代码。

vision 相对 embedding 只有两处新增行为，而它们都来自同一个事实：

**DLP 只能扫文本，扫不了图片。** 一段提示词可以是干净的，但附上的那张扫描页上可能印着
学号、成绩单或者家访记录——正则看不见它。所以：

1. 能力要求是 `vision`（不是 `chat`，也不是 `embedding`）；
2. **无论提示词多干净，vision 一律要求审批**。这是"用审批代替扫描"：当一层防线看不见
   出站内容时，就不该由它来放行。

这一条对应 plan 第 40 行记下的后果——"每一张扫描页都会离开这台机器（在 DLP 授权之下）"。
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import uuid

import httpx
import pytest

from backend.commercial_gateway import (
    GatewayDenied,
    LegacyToolAuthority,
    ProviderGatewayClient,
    build_vision_messages,
    legacy_tool_authority_context,
)
from backend.groups import GroupRepository
from backend.knowe_core.provider_client import ProviderClient
from backend.task_runtime.contracts import RiskLevel, TaskStatus
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


def _authority(runtime, group, run, *, approval_id: str = "", grant_id: str = ""):
    return LegacyToolAuthority(
        group_id=group.group_id,
        task_id=run.task_id,
        run_id=run.run_id,
        runtime=runtime,
        approval_id=approval_id,
        grant_id=grant_id,
    )


def _client(
    handler,
    *,
    provider: str = "alibaba",
    base_url: str = ALIBABA,
    model: str = "qwen-vl-max",
    max_retries: int = 0,
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
            max_retries=max_retries,
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            _gateway_managed=True,
        ),
    )
    return client


def _refuse_to_send(request: httpx.Request) -> httpx.Response:  # pragma: no cover
    raise AssertionError(f"这一请求不该发出去：{request.url}")


_PNG_STUB = b"\x89PNG\r\n\x1a\n" + b"stub-image-bytes"


def _page_image(tmp_path: Path) -> bytes:
    """真的渲染一页出来，而不是塞一段假字节。

    这条路径的两半（渲染与视觉）是同一个功能，用真渲染出来的 PNG 测，等于顺手确认了
    "渲染器的输出能被视觉路径接受"。
    """
    from backend.knowe_rag.rendering import render_page

    source = tmp_path / "page.pdf"
    source.write_bytes(_minimal_pdf())
    return render_page(source, 1, scale=1).image


def _minimal_pdf() -> bytes:
    stream = "BT /F1 24 Tf 72 700 Td (scan) Tj ET"
    font = 3
    objects = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        font: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
           f"/Contents 5 0 R /Resources << /Font << /F1 {font} 0 R >> >> >>",
        5: f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
    }
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode("latin-1")
    start = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for number in range(1, size):
        out += f"{offsets[number]:010d} 00000 n \n".encode("latin-1")
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode("latin-1")
    return bytes(out)


def _egress_statuses(runtime, run) -> list[str]:
    return [
        str(event.public_payload.get("status"))
        for event in runtime.store.events_after(run.run_id, 0)
        if getattr(event, "event_type", "") == "provider_egress"
    ]


# ── 消息构造（纯函数，不需要授权） ────────────────────────────────────

def test_the_prompt_is_the_text_part_of_the_message() -> None:
    messages = build_vision_messages("这一页讲了什么？", [_PNG_STUB])

    assert messages[0]["role"] == "user"
    assert messages[0]["content"][0] == {"type": "text", "text": "这一页讲了什么？"}


def test_each_image_becomes_a_data_url(tmp_path: Path) -> None:
    """图片按 data URL 内联发送。

    不内联的话就得先传到某个地方再给 URL——那等于把用户的扫描页放到了这台机器之外的第二处，
    而这一路径的整个前提是"图片只在这一次请求里离开机器"。
    """
    image = _page_image(tmp_path)

    messages = build_vision_messages("读一下", [image])

    part = messages[0]["content"][1]
    assert part["type"] == "image_url"
    url = part["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == image


def test_several_images_keep_their_order(tmp_path: Path) -> None:
    """多页的先后不能乱：位置是按页给的，顺序错了引用就指向别页。"""
    first = _page_image(tmp_path)
    second = _PNG_STUB + b"second-page"

    messages = build_vision_messages("读", [first, second])

    parts = [part for part in messages[0]["content"] if part["type"] == "image_url"]
    assert len(parts) == 2
    assert base64.b64decode(parts[0]["image_url"]["url"].split(",", 1)[1]) == first
    assert base64.b64decode(parts[1]["image_url"]["url"].split(",", 1)[1]) == second


def test_no_images_means_no_image_parts() -> None:
    messages = build_vision_messages("只有文字", [])

    assert [part["type"] for part in messages[0]["content"]] == ["text"]


# ── 能力 ──────────────────────────────────────────────────────────────

def test_a_provider_without_the_vision_capability_is_refused(tmp_path: Path) -> None:
    """`deepseek` 在能力表里没有 vision。

    用它发图片必须被拒：否则"这个 provider 能不能读图"这个判断等于不存在，用户拿到的是
    provider 的一个 400，而网关这边一切正常。
    """
    runtime, group, run = _runtime_with_run(tmp_path, "novision")
    client = _client(
        _refuse_to_send, provider="deepseek", base_url=DEEPSEEK, model="deepseek-chat",
    )

    async def call():
        with legacy_tool_authority_context(_authority(runtime, group, run)):
            await client.vision([_PNG_STUB], prompt="读一下")

    # 断言**原因**：只写 pytest.raises(GatewayDenied) 的话，一个"能力要求写成了 chat"
    # 的实现也会通过——它会走到稍后的审批闸门再被挡下，拒绝理由完全不同。
    with pytest.raises(GatewayDenied, match="capability registry"):
        asyncio.run(call())
    runtime.close()


# ── 审批（这一层最重要的行为） ────────────────────────────────────────

def test_vision_requires_approval_even_for_a_completely_clean_prompt(tmp_path: Path) -> None:
    """提示词干净也照样要审批。

    DLP 看不见图片里的东西——一张扫描页上可以印着学号、成绩单、家访记录，而正则一个都
    匹配不到。所以这一条路不能靠"扫过了，没问题"放行。

    对照：同一段干净提示词走 embedding 时不要求审批（见
    `test_rag_embedding_egress.py::test_a_clean_embedding_is_not_held_up_by_the_dlp_gateway`）。
    两个测试一起才说明"vision 的审批要求是它自己多出来的"，而不是"什么都要求审批"。
    """
    runtime, group, run = _runtime_with_run(tmp_path, "clean-but-image")
    client = _client(_refuse_to_send)

    async def call():
        with legacy_tool_authority_context(_authority(runtime, group, run)):
            await client.vision([_PNG_STUB], prompt="第三章讲了什么？")

    with pytest.raises(GatewayDenied, match="approval"):
        asyncio.run(call())
    runtime.close()


def test_a_clean_prompt_alone_would_not_have_needed_approval(tmp_path: Path) -> None:
    """对照的另一半：同一个 client、同一句提示词，走 embedding 不要求审批。

    没有这一条，"vision 要求审批"可能只是因为提示词里有敏感内容——那样测的就不是图片这条
    规则了。
    """
    from backend.commercial_gateway import PiiDlpGateway

    clean = build_vision_messages("第三章讲了什么？", [])[0]["content"][0]["text"]
    decision = PiiDlpGateway().decide_outbound(
        json.dumps({"input": [clean]}, ensure_ascii=False), approved=False,
    )

    assert decision.requires_approval is False


def test_vision_without_an_authority_is_refused(tmp_path: Path) -> None:
    client = _client(_refuse_to_send)

    with pytest.raises(GatewayDenied, match="authoritative Task Runtime"):
        asyncio.run(client.vision([_PNG_STUB], prompt="读一下"))


def test_an_empty_image_list_is_refused(tmp_path: Path) -> None:
    """没有图片的 vision 调用是调用方的错误。

    放它过去会变成一次普通的文本请求，而调用方以为自己在读图——结果里出现的"描述"其实是
    模型凭空写的。
    """
    runtime, group, run = _runtime_with_run(tmp_path, "noimage")
    client = _client(_refuse_to_send)

    async def call():
        with legacy_tool_authority_context(_authority(runtime, group, run)):
            await client.vision([], prompt="读一下")

    with pytest.raises(GatewayDenied, match="at least one image"):
        asyncio.run(call())
    runtime.close()


# ── 与 embedding 的另两处差异 ─────────────────────────────────────────

def test_vision_is_refused_before_any_request_is_sent(tmp_path: Path) -> None:
    """被拒时一个字节都不能出去——handler 里挂了断言来保证这件事。"""
    runtime, group, run = _runtime_with_run(tmp_path, "nosend")
    client = _client(_refuse_to_send)

    async def call():
        with legacy_tool_authority_context(_authority(runtime, group, run)):
            await client.vision([_PNG_STUB], prompt="读一下")

    with pytest.raises(GatewayDenied):
        asyncio.run(call())
    # 连 effect 前的 authorized 审计都不该写：这一次请求没有通过授权。
    assert _egress_statuses(runtime, run) == []
    runtime.close()


def test_vision_reserves_output_tokens_unlike_embedding() -> None:
    """视觉模型**会**生成 token，所以它必须预留输出——这与 embedding 正好相反。

    断言的是 `_vision_shape` 这个**真实函数**的产物，而不是测试自己刚构造的一个对象。写成
    后者的话（`shape = _EgressShape(reserves_output=True)` 然后断言它等于 True），测的只是
    "我刚设的值就是我刚设的值"，而它会永远通过——这一版最初就是这么写的，被 mutation 当场
    指了出来。
    """
    from backend.commercial_gateway import VISION_TOKENS_PER_IMAGE, _vision_shape

    payloads = [_PNG_STUB] * 3
    shape = _vision_shape("读一下这一页", payloads)

    assert shape.reserves_output is True
    assert shape.always_requires_approval is True
    assert shape.required == {"vision"}
    # 图片的 token 成本不在 payload 里（它只记了一个长度），必须显式补上；否则一次读图只按
    # 提示词那几十个 token 预留，预算这一维度对视觉路径基本是敞开的。
    assert shape.extra_input_tokens == VISION_TOKENS_PER_IMAGE * 3


def test_the_vision_shape_keeps_the_prompt_readable_for_dlp() -> None:
    """payload 里必须留着提示词本身。

    DLP 扫的就是它。把提示词也换成摘要，文本这一侧的敏感内容就一起失去了检查——而图片那侧
    本来就已经只能靠审批了。
    """
    from backend.commercial_gateway import _vision_shape

    shape = _vision_shape("学号：2026123456", [_PNG_STUB])

    assert "2026123456" in json.dumps(shape.payload, ensure_ascii=False)


def test_the_built_messages_contain_the_image_so_dlp_cannot_read_it(tmp_path: Path) -> None:
    """这条测试记录一个**限制**，不是行为。

    DLP 扫的是文本；图片经 base64 之后是一串它读不懂的字符。把这一点写成断言，是为了让
    "为什么 vision 必须走审批"有一个可以被读到的地方——而不是只存在于注释里。
    """
    from backend.commercial_gateway import PiiDlpGateway

    image = _page_image(tmp_path)
    serialized = json.dumps(build_vision_messages("学号：2026123456", [image]), ensure_ascii=False)

    decision = PiiDlpGateway().decide_outbound(serialized, approved=False)

    # 提示词里的学号被扫到了；
    assert decision.requires_approval is True
    # 但图片本身没有被理解——它只是一段 base64。这就是审批必须存在的原因。
    assert base64.b64encode(image).decode("ascii") in serialized


# ── 图片格式 ──────────────────────────────────────────────────────────

def test_an_image_of_an_unknown_format_is_refused(tmp_path: Path) -> None:
    """认不出来的格式直接拒绝，而不是按 PNG 发出去。

    按 PNG 发出去的话，provider 回的错误会指向"图片损坏"或者"不支持的格式"，而问题在调用方
    给的那串字节上——排查会从一个错误的线索开始。
    """
    runtime, group, run = _runtime_with_run(tmp_path, "badimage")
    client = _client(_refuse_to_send)

    async def call():
        with legacy_tool_authority_context(_authority(runtime, group, run)):
            await client.vision([b"definitely-not-an-image"], prompt="读一下")

    with pytest.raises(GatewayDenied, match="图片格式"):
        asyncio.run(call())
    runtime.close()


def test_a_jpeg_is_accepted_as_well_as_a_png(tmp_path: Path) -> None:
    """相机与扫描仪的常见输出就是 JPEG，所以它必须与 PNG 一样被接受。

    只认 PNG 会让"用手机拍的一页"在最外层就被拒，而错误信息说着"格式不认识"——用户手里
    明明是一张很正常的照片。
    """
    messages = build_vision_messages("读", [b"\xff\xd8\xff\xe0jpeg-bytes"])

    url = messages[0]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")


# ── 成功路径（第十次复审的三条发现都指向这里） ───────────────────────
#
# 这一节之前**完全不存在**：文件里每一次调用都是"被拒"，`_authority(..., approval_id=...)`
# 的两个参数从没被传过。于是 approval → claim → effect → 终态 → 对账这条链在 vision 上零
# 端到端覆盖，第十次复审实测：把这条链上的任何一步删掉，27 个测试全绿。
#
# 审批准入为什么可行：`_authorize` 把自己的 `operation_scope` 交给 `prepare_tool`，而 grant
# 的 scope 是审批时给的——两者不必逐字相同（实测：空 scope 的 grant 也能放行），所以测试
# 不需要预知运行时算出的那串摘要。

def _operation_scope_for(client, runtime, group, run, images, prompt) -> dict[str, str]:
    """读出真正的 `_authorize` 会用的 `operation_scope`。

    做法：给一对**格式合法但不存在**的 approval/grant，让流程走到 `prepare_tool` 再被拒——
    那一步会把 scope 交出来。

    为什么不在测试里把那六个 sha256 复刻一遍：`task_runtime/service.py:1101` 要求
    `grant.operation_scope` 与请求的 scope **完全相等**，而复刻件一旦与实现漂移，失败会出现在
    一个与原因无关的地方。从真实调用里读，漂移不可能发生。
    """
    captured: dict[str, str] = {}
    original = runtime.prepare_tool

    def spy(*args, **kwargs):
        captured.update(kwargs.get("operation_scope") or {})
        return original(*args, **kwargs)

    runtime.prepare_tool = spy
    try:
        authority = LegacyToolAuthority(
            group_id=group.group_id, task_id=run.task_id, run_id=run.run_id,
            runtime=runtime,
            approval_id="apr_" + "0" * 32, grant_id="grant_" + "0" * 32,
        )

        async def attempt():
            with legacy_tool_authority_context(authority):
                await client.vision(images, prompt=prompt)

        with pytest.raises(GatewayDenied):
            asyncio.run(attempt())
    finally:
        runtime.prepare_tool = original
    assert captured, "没有捕获到 operation_scope——授权流程没有走到 prepare_tool"
    return captured


def _approve_provider_egress(runtime, group, run, scope: dict[str, str]) -> tuple[str, str]:
    """按给定的 scope 建一条真实的 provider_egress 审批，返回 `(approval_id, grant_id)`。"""
    approval = runtime.request_approval(
        group.group_id, run.run_id, kind="tool", summary="Allow one provider egress",
        risk=RiskLevel.HIGH, request_key=f"approve-{uuid.uuid4().hex}",
        requested_capabilities=("provider_egress",), tool_name="provider_egress",
        operation_scope=scope, purpose="provider_egress",
    )
    _resolved, grant, _owner = runtime.resolve_approval(
        group.group_id, approval.approval_id, approved=True,
        capabilities=("provider_egress",), request_key=f"resolve-{uuid.uuid4().hex}",
    )
    assert grant is not None
    return approval.approval_id, grant.grant_id


def _vision_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": "这一页讲的是二次函数"}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 30},
        },
    )


def test_a_successful_vision_call_reaches_the_provider(tmp_path: Path) -> None:
    """先证明这条链**能走通**，否则下面几条断言的成功路径等于没被执行过。"""
    runtime, group, run = _runtime_with_run(tmp_path, "vision-ok")
    probe = _client(_vision_ok)
    scope = _operation_scope_for(probe, runtime, group, run, [_PNG_STUB], "这一页讲了什么")
    approval_id, grant_id = _approve_provider_egress(runtime, group, run, scope)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _vision_ok(request)

    client = _client(handler)
    authority = _authority(runtime, group, run, approval_id=approval_id, grant_id=grant_id)

    async def call():
        with legacy_tool_authority_context(authority):
            return await client.vision([_PNG_STUB], prompt="这一页讲了什么")

    result = asyncio.run(call())

    assert len(calls) == 1
    assert result["choices"][0]["message"]["content"] == "这一页讲的是二次函数"
    runtime.close()


def test_a_successful_vision_call_writes_a_terminal_audit_event(tmp_path: Path) -> None:
    """F1-1：成功路径必须写终态。

    没有它，公开审计里只有 `authorized`——**这张图到底发出去了没有、什么时候结束、花了多少
    token，事后没有任何记录可查**，而每次 vision 都会 prepare + claim 一个 HIGH risk 的
    invocation。第十次复审实测：把这一段整段删掉，27 个测试全绿。这条测试就是为了让那种删除
    立刻变红。
    """
    runtime, group, run = _runtime_with_run(tmp_path, "vision-terminal")
    client = _client(_vision_ok)
    scope = _operation_scope_for(client, runtime, group, run, [_PNG_STUB], "这一页讲了什么")
    approval_id, grant_id = _approve_provider_egress(runtime, group, run, scope)
    authority = _authority(runtime, group, run, approval_id=approval_id, grant_id=grant_id)

    async def call():
        with legacy_tool_authority_context(authority):
            await client.vision([_PNG_STUB], prompt="这一页讲了什么")

    asyncio.run(call())

    statuses = _egress_statuses(runtime, run)
    assert "authorized" in statuses, statuses
    assert "completed" in statuses, statuses
    terminal = next(
        event for event in runtime.store.events_after(run.run_id, 0)
        if getattr(event, "event_type", "") == "provider_egress"
        and event.public_payload.get("status") == "completed"
    )
    assert "duration_ms" in terminal.public_payload
    assert terminal.public_payload.get("actual_tokens") == 1230
    runtime.close()


def test_a_successful_vision_call_reconciles_the_reservation(tmp_path: Path) -> None:
    """F1-2：成功后要按 provider 上报的用量对账。

    预留是上界估计（按 `1 + max_retries` 轮计），不退回就永久多扣。第十次复审用 chat 做了
    对照：删 chat 的同一行会让 `test_successful_first_attempt_refunds_the_unused_retry_reservation`
    失败，删 embed/vision 的**五个分支**则全绿——同一步骤，两种调用类型一个有锁一个没有。

    ⚠ **第一版这条测试是空转的，第十一次复验抓到了。** 它当时用默认的 `max_retries=0`：
    预留 `1+0 = 1` 轮，而 `_reconcile_provider_usage` 在"实际 == 预留"时直接返回，于是成功
    调用**根本不产生 settle 事件**；断言命中的是授权期那一次 `consume_budget`，删掉对账照样
    为真。复验实测：`max_retries=0` 时只有一条 `model_turns=1`，换成 3 就只剩 `model_turns=4`
    那一条、断言立刻为假。embed 那条测试的 docstring 自己记着同一个坑，vision 这条当时没跟上。
    """
    runtime, group, run = _runtime_with_run(tmp_path, "vision-settle")
    # 预留按 `1 + max_retries` 轮计。不设重试的话预留与实际都是 1 轮，**删掉对账也看不出差别**。
    client = _client(_vision_ok, max_retries=3)
    scope = _operation_scope_for(client, runtime, group, run, [_PNG_STUB], "这一页讲了什么")
    approval_id, grant_id = _approve_provider_egress(runtime, group, run, scope)
    authority = _authority(runtime, group, run, approval_id=approval_id, grant_id=grant_id)

    async def call():
        with legacy_tool_authority_context(authority):
            await client.vision([_PNG_STUB], prompt="这一页讲了什么")

    asyncio.run(call())

    # 结算发生在 run 的预算上；这里用**公开事件**观察，因为它就是界面上能看到的那个数字。
    budgeted = [
        event for event in runtime.store.events_after(run.run_id, 0)
        if getattr(event, "event_type", "") == "budget_updated"
    ]
    assert budgeted, "一次成功的调用之后没有任何预算事件——对账没有被执行"
    # 只有预留那一条时轮次是 4；对账执行之后才会出现实际轮次 1 的那一条。
    turns = [
        event.public_payload.get("usage", {}).get("model_turns") for event in budgeted
    ]
    assert 1 in turns, turns
    runtime.close()


def test_each_image_adds_its_estimated_tokens_to_the_reservation(tmp_path: Path) -> None:
    """F1-3：每多一张图，预留就该多一份图片估算。

    **不能只断言"预留 ≥ 一张图的估算"**——预留里还有输出那一份（保守上限，上千），它本来就把
    任何图片估算盖过去了；实测那一版断言在 `extra_input_tokens` 被整个删掉之后照样通过。
    指向这一项的只有差值。

    这一条比 embedding 那条更隐蔽：值对了、用法没了。
    """
    from backend.commercial_gateway import VISION_TOKENS_PER_IMAGE

    def reserved_for(count: int, name: str) -> int:
        runtime, group, run = _runtime_with_run(tmp_path, name)
        client = _client(_vision_ok)
        images = [_PNG_STUB] * count
        scope = _operation_scope_for(client, runtime, group, run, images, "读")
        approval_id, grant_id = _approve_provider_egress(runtime, group, run, scope)
        authority = _authority(runtime, group, run, approval_id=approval_id, grant_id=grant_id)

        async def call():
            with legacy_tool_authority_context(authority):
                await client.vision(images, prompt="读")

        asyncio.run(call())
        authorized = next(
            event for event in runtime.store.events_after(run.run_id, 0)
            if getattr(event, "event_type", "") == "provider_egress"
            and event.public_payload.get("status") == "authorized"
        )
        reserved = int(authorized.public_payload["usage"]["tokens"])
        runtime.close()
        return reserved

    single = reserved_for(1, "vision-one-image")
    triple = reserved_for(3, "vision-three-images")

    difference = triple - single
    # 两张图的差就是两份估算；payload 里图片只记 mime 与长度，序列化本身的增长可以忽略。
    assert difference >= 2 * VISION_TOKENS_PER_IMAGE - 200, (
        f"一张图预留 {single}、三张图 {triple}，差值 {difference} 明显小于两份图片估算"
        f"（{2 * VISION_TOKENS_PER_IMAGE}）——图片 token 没有被计入预留"
    )

def test_a_failed_vision_call_reconciles_the_attempt_dimensions(tmp_path: Path) -> None:
    """失败时同样要结算轮次/成本——这是复审所列五个分支里 vision 的最后那一个。

    至此五个分支各自有测试：embed 成功/失败/取消、vision 成功/失败。
    """
    runtime, group, run = _runtime_with_run(tmp_path, "vision-fail-settle")
    client = _client(
        lambda request: httpx.Response(400, json={"error": "bad"}),
        max_retries=3,
    )
    scope = _operation_scope_for(client, runtime, group, run, [_PNG_STUB], "读")
    approval_id, grant_id = _approve_provider_egress(runtime, group, run, scope)
    authority = _authority(runtime, group, run, approval_id=approval_id, grant_id=grant_id)

    async def call():
        with legacy_tool_authority_context(authority):
            await client.vision([_PNG_STUB], prompt="读")

    with pytest.raises(Exception):
        asyncio.run(call())

    budgeted = [
        event for event in runtime.store.events_after(run.run_id, 0)
        if getattr(event, "event_type", "") == "budget_updated"
    ]
    assert budgeted, "失败的 vision 调用之后没有任何预算事件——失败路径没有结算"
    assert any(
        event.public_payload.get("usage", {}).get("model_turns") == 1
        for event in budgeted
    ), [event.public_payload for event in budgeted]
    runtime.close()