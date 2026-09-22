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
