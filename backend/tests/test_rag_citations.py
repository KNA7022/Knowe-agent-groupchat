"""引用校验与弃答。

plan 第 51 行：验证权限、版本、课程隔离与**精确引用**，对没有依据的校内事实**弃答**。

这一层回答的是"这个答案能不能交出去"，而不是"检索得好不好"：

* **位置必须能被解析**。`page:3#2` 这种位置既是给用户看的，也是回原文核对的入口；解析不了
  的位置意味着用户点不过去，那条引用就是装饰。
* **答案里引用的每一个位置都必须在证据包里**。引用了证据包里没有的位置就是**幻觉引用**
  ——它看起来比没有引用更可信，因为它长得像一条出处。
* **弃答有两个不同的原因**，混在一起会让用户分不清该怎么办：证据不够（该补充资料），
  还是引用了不存在的位置（该重问或报告问题）。

作用域隔离不在这里：那是"一个作用域一个索引"在 `retrieval.py` 里保证的，这一层只校验
引用与证据的对应关系。
"""

from __future__ import annotations

import pytest

from backend.knowe_rag.citations import (
    Grounding,
    GroundingVerdict,
    LocationError,
    parse_location,
    sort_locations,
    verify_citations,
)
from backend.knowe_rag.fusion import EvidencePack, RetrievedChunk


def _chunk(location: str, *, number: int = 1, text: str = "内容") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"chunk-{number}",
        document_id="doc",
        parent_id=f"parent-{number}",
        location=location,
        text=text,
        score=1.0,
        sources=("bm25",),
    )


def _pack(*locations: str, minimum: int = 1) -> EvidencePack:
    return EvidencePack(
        items=tuple(_chunk(location, number=index) for index, location in enumerate(locations, 1)),
        minimum=minimum,
    )


# ── 位置解析 ──────────────────────────────────────────────────────────

def test_a_page_location_parses() -> None:
    parsed = parse_location("page:3")

    assert parsed.kind == "page"
    assert parsed.source == "3"
    assert parsed.ordinal is None


def test_a_child_location_keeps_its_ordinal() -> None:
    """`page:3#2` 里的 `#2` 是 child 在 parent 内的序号。

    丢掉它，一条指向"第 3 页第 2 段"的引用就退化成"第 3 页"——核对的人得自己找是哪一段。
    """
    parsed = parse_location("page:3#2")

    assert parsed.kind == "page"
    assert parsed.source == "3"
    assert parsed.ordinal == 2


def test_a_paragraph_location_parses() -> None:
    parsed = parse_location("para:12")

    assert (parsed.kind, parsed.source, parsed.ordinal) == ("para", "12", None)


def test_a_heading_location_keeps_the_level() -> None:
    """`h2:1` 与 `h1:1` 是不同的位置：引用一个二级标题和引用一级标题不是同一件事。"""
    parsed = parse_location("h2:1")

    assert parsed.kind == "h2"
    assert parsed.source == "1"


def test_a_sheet_location_keeps_the_name_and_the_range() -> None:
    """工作表名与单元格范围都要留下，否则引用只说得出"在某张表里"。"""
    parsed = parse_location("sheet:成绩!A1:C4")

    assert parsed.kind == "sheet"
    assert parsed.source == "成绩!A1:C4"


def test_a_slide_location_parses() -> None:
    assert parse_location("slide:10").source == "10"


def test_a_code_block_location_parses() -> None:
    """位置现在只留序号（`code:1`）；解析层不假设来源里有什么字符。

    语言标注已挪进块文本，所以 `code:1:python` 不再是产出形态。这里保留它是因为
    `parse_location` 的契约是"来源部分不做字符限制"——**已落库的旧引用**仍然要能解析，
    而这个契约正是靠"来源里带 `:` 也能过"来体现的。
    """
    parsed = parse_location("code:1:python")

    assert parsed.kind == "code"
    assert parsed.source == "1:python"
    assert parse_location("code:1").source == "1"


@pytest.mark.parametrize("value", ["", "page", "page:", ":3", "para:abc#x", "page:3#0"])
def test_a_malformed_location_is_rejected(value: str) -> None:
    """解析不了的位置必须报错。

    悄悄接受它意味着用户的"点这里回原文"什么也不会发生，而那条引用看上去完全正常——
    比没有引用更糟，因为它长得像一条出处。
    """
    with pytest.raises(LocationError):
        parse_location(value)


def test_a_child_ordinal_must_be_positive() -> None:
    with pytest.raises(LocationError):
        parse_location("page:1#0")


def test_an_unknown_location_kind_is_rejected() -> None:
    """只有解析器真正会产出的那几种前缀算位置。

    接受任意前缀意味着 `foo:1` 也能通过引用校验，而它指向不了任何原文——一条点不过去的
    引用在界面上和真引用长得一模一样。
    """
    with pytest.raises(LocationError):
        parse_location("foo:1")


def test_a_hash_that_is_not_an_ordinal_is_rejected() -> None:
    """`#` 出现了却不成序号，几乎一定是把序号写错了。

    放它过去等于承认一个错位置合法：`para:abc#x` 会被当成来源是 `abc#x` 的位置，而它对应的
    原文位置根本不存在。
    """
    with pytest.raises(LocationError):
        parse_location("para:abc#x")


# ── 引用校验 ──────────────────────────────────────────────────────────

def test_a_cited_location_that_is_in_the_pack_is_supported() -> None:
    report = verify_citations(["page:1"], _pack("page:1", "page:2"))

    assert report.supported == ("page:1",)
    assert report.unsupported == ()
    assert report.is_grounded is True


def test_a_cited_location_that_is_not_in_the_pack_is_unsupported() -> None:
    """这是这一层存在的理由：答案引用了一条**不存在于证据里**的位置。

    它比"没有引用"更可信，因为它长得像一条出处；而用户点过去会发现原文里没有这段话——
    如果那个位置连存在都不存在，问题就更严重了。
    """
    report = verify_citations(["page:9"], _pack("page:1", "page:2"))

    assert report.unsupported == ("page:9",)
    assert report.is_grounded is False


def test_evidence_that_was_not_cited_is_reported() -> None:
    """没被引用的证据要如实列出来。

    它是"答案可能漏掉了什么"的唯一线索：证据找到了却没进答案，要么是模型没用上，
    要么是用上了却没说来自哪里——两种都值得知道。
    """
    report = verify_citations(["page:1"], _pack("page:1", "page:2", "page:3"))

    assert report.uncited == ("page:2", "page:3")


def test_a_citation_must_match_exactly_including_the_child_ordinal() -> None:
    """`page:1` 与 `page:1#2` 不是同一个位置。

    把 parent 的位置当成 child 的位置接受，会让引用指向一整页——核对的人还是不知道该看
    哪一段。
    """
    report = verify_citations(["page:1#2"], _pack("page:1"))

    assert report.unsupported == ("page:1#2",)


def test_an_answer_with_no_citations_is_not_grounded() -> None:
    """没有任何引用的答案不算"有据"。

    在校内事实这种场景里，"我没找到依据"和"我找到了但懒得说"必须能被区分开，否则
    "有据回答率"这个指标就没有意义。
    """
    report = verify_citations([], _pack("page:1"))

    assert report.supported == ()
    assert report.is_grounded is False


def test_an_empty_pack_cannot_support_anything() -> None:
    report = verify_citations(["page:1"], EvidencePack(items=(), minimum=1))

    assert report.is_grounded is False
    assert report.unsupported == ("page:1",)


def test_a_location_that_cannot_be_parsed_is_reported_not_ignored() -> None:
    """答案里出现一个解析不了的位置，要**报出来**而不是跳过。

    跳过的话，一次格式错误会表现成"这条引用没被计入"，而 grounded 判断可能因此反而通过。
    """
    with pytest.raises(LocationError):
        verify_citations(["不是位置"], _pack("page:1"))


def test_repeated_citations_are_collapsed() -> None:
    """同一位置被引用两次只算一条：否则"引用了几条证据"这个数字可以靠重复刷高。"""
    report = verify_citations(["page:1", "page:1"], _pack("page:1"))

    assert report.supported == ("page:1",)


def test_citation_order_follows_the_reading_order_of_the_source() -> None:
    """报告里的顺序按原文位置，不按模型说话的先后。

    模型引用顺序是随机的，而人核对时是顺着原文看的。
    """
    report = verify_citations(["page:10", "page:2"], _pack("page:2", "page:10"))

    assert report.supported == ("page:2", "page:10")


# ── 弃答判断 ──────────────────────────────────────────────────────────

def test_grounding_succeeds_when_every_citation_is_backed() -> None:
    verdict = Grounding.judge(pack=_pack("page:1", minimum=1), cited=["page:1"])

    assert verdict.verdict is GroundingVerdict.ANSWER
    assert verdict.report.is_grounded is True


def test_insufficient_evidence_abstains_with_its_own_reason() -> None:
    """证据条数不够是一种弃答，引用对不上是**另一种**。

    混成一句"无法回答"会让用户不知道该补充资料还是该重问；而这两种情况对这个产品来说
    是完全不同的信号。
    """
    verdict = Grounding.judge(pack=_pack("page:1", minimum=8), cited=["page:1"])

    assert verdict.verdict is GroundingVerdict.ABSTAIN_INSUFFICIENT


def test_an_unsupported_citation_abstains_even_when_evidence_is_plentiful() -> None:
    """引用了一条不在证据里的位置——这是最该拒的一种情况。

    证据明明够，但答案里出现了没有依据的出处；放它过去，等于让幻觉引用冒充有据回答。
    """
    pack = _pack(*[f"page:{number}" for number in range(1, 10)], minimum=3)

    verdict = Grounding.judge(pack=pack, cited=["page:1", "page:99"])

    assert verdict.verdict is GroundingVerdict.ABSTAIN_UNSUPPORTED_CITATION
    assert verdict.report.unsupported == ("page:99",)


def test_evidence_that_is_sufficient_but_uncited_still_answers() -> None:
    """证据够、引用也都对得上，只是还有没被引用的证据——这是正常的，不是弃答理由。

    一份答案不需要把检索到的每一条都用上。
    """
    verdict = Grounding.judge(pack=_pack("page:1", "page:2", minimum=1), cited=["page:1"])

    assert verdict.verdict is GroundingVerdict.ANSWER
    assert verdict.report.uncited == ("page:2",)


def test_an_answer_with_no_citations_abstains() -> None:
    verdict = Grounding.judge(pack=_pack("page:1", minimum=1), cited=[])

    assert verdict.verdict is GroundingVerdict.ABSTAIN_UNSUPPORTED_CITATION


def test_insufficient_evidence_wins_over_a_citation_problem() -> None:
    """两种弃答同时成立时报"证据不够"。

    用户看到"引用了不存在的位置"会去重问，而真正该做的是补充资料——先报哪一条决定了他
    下一步做什么，所以这个优先级是有意义的，不是随手排的。
    """
    verdict = Grounding.judge(pack=_pack("page:1", minimum=8), cited=["page:99"])

    assert verdict.verdict is GroundingVerdict.ABSTAIN_INSUFFICIENT


# ── 位置排序 ──────────────────────────────────────────────────────────

def test_locations_sort_by_number_then_ordinal_within_a_kind() -> None:
    """同一个类型内先按数字、再按 child 序号。

    **跨类型比较没有意义**：`page:` 来自 PDF、`para:` 来自纯文本，同一份文档里不会同时出现
    这两种位置。类型之间按名字排只是为了让结果稳定，不是一条语义规则——把它写成断言，等于
    给一个巧合赋予含义。
    """
    ordered = sort_locations(["page:10", "page:2", "page:2#2", "page:2#1"])

    assert ordered == ("page:2", "page:2#1", "page:2#2", "page:10")


def test_the_same_input_always_sorts_the_same_way() -> None:
    """跨类型时至少要稳定：两次调用给出同一个顺序，否则引用列表会无缘无故地变。"""
    values = ["page:2", "para:1", "slide:3"]

    assert sort_locations(values) == sort_locations(list(reversed(values)))


def test_numeric_locations_do_not_sort_like_strings() -> None:
    """`page:10` 要排在 `page:2` 后面。

    按字符串排序会得到 `page:10` < `page:2`，于是引用列表的顺序和原文顺序不一样——
    而"按原文顺序"正是这个函数存在的全部理由。
    """
    assert sort_locations(["page:10", "page:9", "page:100"]) == ("page:9", "page:10", "page:100")


def test_sorting_an_empty_list_is_empty() -> None:
    assert sort_locations([]) == ()


def test_sorting_rejects_an_unparseable_location() -> None:
    with pytest.raises(LocationError):
        sort_locations(["page:1", "nonsense"])


# ── 报告的形状 ────────────────────────────────────────────────────────

def test_the_verdict_carries_the_report_so_the_reason_is_inspectable() -> None:
    """弃答要能说清**为什么**——这是界面能给出下一步提示的前提。"""
    verdict = Grounding.judge(pack=_pack("page:1", minimum=8), cited=["page:1"])

    assert verdict.report.supported == ("page:1",)
    assert verdict.required_evidence == 8
    assert verdict.available_evidence == 1
