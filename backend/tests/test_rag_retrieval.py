"""检索入口：三路召回 → RRF → MMR → 证据包。

plan 第 50 行：BM25 召回 40、向量召回 40、图召回 10，然后 RRF、可选 rerank、MMR、8-12 条
证据。这里把已经做好的几块（`bm25` / `vector_index` / `fusion`）串起来，并加上作用域。

**作用域隔离的做法是"一个作用域一个索引"，不是"查询时过滤"。** 这不是风格问题：过滤是可以
忘记写的，而一个只装本作用域数据的索引没有"忘记"这个失败模式。跨群组、跨课程的数据看不见，
是因为它根本不在这里。

**图召回那一路补的是上下文，不是相似度。** BM25 与向量都在回答"这块文本本身像不像问题"，
而一段完整论述常常被切成相邻的两块——命中了后半句，前半句在隔壁。兄弟块（同一个 parent 下
的其它 child）天然就是同一页、同一节的连续内容。
"""

from __future__ import annotations

import pytest

from backend.knowe_rag import Chunk, ParsedBlock, build_chunks
from backend.knowe_rag.retrieval import (
    BM25_RECALL,
    GRAPH_RECALL,
    VECTOR_RECALL,
    ScopeIndex,
    RetrievalError,
)


def _page(document_id: str, *texts: str) -> tuple[Chunk, ...]:
    """每个文本一页，各自成一个 parent 加一个 child。"""
    return build_chunks(document_id, tuple(
        ParsedBlock(text=text, location=f"page:{index}", kind="page")
        for index, text in enumerate(texts, start=1)
    ))


def _multi_child_page(document_id: str, *parts: str) -> tuple[Chunk, ...]:
    """一页里塞进多个 child——图召回要有兄弟块才测得到。

    每段 300 字符，超过 child 上限的一半，所以它们会被切成不同的 child 而仍在同一个 parent
    下。
    """
    text = "\n".join(parts)
    return build_chunks(document_id, (ParsedBlock(text=text, location="page:1", kind="page"),))


def _index(dimensions: int | None = None) -> ScopeIndex:
    return ScopeIndex(dimensions=dimensions)


def _fill(index: ScopeIndex, chunks: tuple[Chunk, ...]) -> None:
    for chunk in chunks:
        index.add(chunk)


def _ids(pack) -> list[str]:
    return [item.chunk_id for item in pack.items]


# ── 三路召回 ──────────────────────────────────────────────────────────

def test_a_lexical_hit_is_returned() -> None:
    index = _index()
    _fill(index, _page("d1", "二次函数的图像是抛物线"))

    pack = index.retrieve("抛物线", minimum=1, maximum=12)

    assert [item.text for item in pack.items] == ["二次函数的图像是抛物线"]


def test_a_vector_hit_is_returned_even_when_no_word_matches() -> None:
    """向量那一路的价值就在这里：问题用的词和原文一个都不一样。

    "这条曲线开口朝哪" 与 "抛物线的方向由二次项系数决定" 没有共同词元，BM25 一条都召不回；
    向量能——因为它是按语义近邻找的。
    """
    index = _index(dimensions=2)
    chunks = _page("d1", "抛物线的方向由二次项系数决定")
    _fill(index, chunks)
    child = next(c for c in chunks if c.level == "child")
    index.add_vector(child.chunk_id, [1.0, 0.0])

    pack = index.retrieve("这条曲线开口朝哪", query_vector=[1.0, 0.0], minimum=1, maximum=12)

    assert [item.chunk_id for item in pack.items] == [child.chunk_id]


def test_a_chunk_found_by_both_channels_outscores_one_found_by_one() -> None:
    """两路都召回比单路更可信，这正是 RRF 存在的前提。

    用同一个查询词让 A 被两路同时命中，B 只被词法命中。
    """
    index = _index(dimensions=2)
    chunks = _page("d1", "判别式", "判别式")
    _fill(index, chunks)
    children = [c for c in chunks if c.level == "child"]
    assert len(children) == 2
    # 只给第二个块向量，让它在向量那一路也出现
    index.add_vector(children[1].chunk_id, [1.0, 0.0])

    pack = index.retrieve("判别式", query_vector=[1.0, 0.0], minimum=2, maximum=12)

    assert pack.items[0].chunk_id == children[1].chunk_id, _ids(pack)


def test_the_result_reports_which_channels_found_each_chunk() -> None:
    """`sources` 是事后唯一能解释排序的东西：只有词法命中的和两路都命中的，可信程度不同。"""
    index = _index(dimensions=2)
    chunks = _page("d1", "判别式", "判别式")
    _fill(index, chunks)
    children = [c for c in chunks if c.level == "child"]
    index.add_vector(children[0].chunk_id, [1.0, 0.0])

    pack = index.retrieve("判别式", query_vector=[1.0, 0.0], minimum=2, maximum=12)

    by_id = {item.chunk_id: item for item in pack.items}
    assert set(by_id[children[0].chunk_id].sources) == {"bm25", "vector"}
    assert by_id[children[1].chunk_id].sources == ("bm25",)


def test_hybrid_finds_what_neither_channel_finds_alone() -> None:
    """混合检索的意义：两路合起来覆盖的比任何一路都多。

    这一条同时是"两路都要真的接上"的证明——只接一路时，另一路独有的那块内容不会出现。
    """
    index = _index(dimensions=2)
    chunks = _page("d1", "抛物线", "完全无关的另一个主题")
    _fill(index, chunks)
    children = [c for c in chunks if c.level == "child"]
    index.add_vector(children[1].chunk_id, [0.0, 1.0])

    lexical_only = index.retrieve("抛物线", minimum=1, maximum=12)
    hybrid = index.retrieve("抛物线", query_vector=[0.0, 1.0], minimum=2, maximum=12)

    assert {item.chunk_id for item in lexical_only.items} == {children[0].chunk_id}
    assert {item.chunk_id for item in hybrid.items} == {children[0].chunk_id, children[1].chunk_id}


# ── 图召回 ────────────────────────────────────────────────────────────

def test_graph_recall_brings_a_sibling_of_a_hit() -> None:
    """命中一块之后，同 parent 下的兄弟块要跟进来。

    一段完整论述被切成相邻两块时，只给模型后半句等于让它半句话里编答案。
    """
    index = _index()
    chunks = _multi_child_page("d1", "甲" * 300, "乙" * 300)
    _fill(index, chunks)
    children = [c for c in chunks if c.level == "child"]
    assert len(children) == 2, [c.text[:10] for c in chunks]
    first, second = children

    pack = index.retrieve(first.text, minimum=1, maximum=12)

    assert second.chunk_id in {item.chunk_id for item in pack.items}, _ids(pack)


def test_graph_recall_does_not_repeat_the_seed() -> None:
    """种子块自己不能被图那一路再"召回"一次。

    它会让种子在 RRF 里凭空多得一份票，把一个只是被词法命中的块抬成"两路都命中"——而
    `sources` 是这个系统里唯一能事后解释排序的东西，它一旦不实，解释就是错的。

    断言不能只看"证据包里没有重复"：`pack_evidence` 本来就会按 id 去重，那样测的是打包
    层，不是图这一路。
    """
    index = _index()
    chunks = _multi_child_page("d1", "甲" * 300, "乙" * 300)
    _fill(index, chunks)
    first = next(c for c in chunks if c.level == "child")

    pack = index.retrieve(first.text, minimum=1, maximum=12)

    seed = next(item for item in pack.items if item.chunk_id == first.chunk_id)
    assert seed.sources == ("bm25",), seed.sources
    assert "graph" not in seed.sources


def test_a_chunk_with_no_siblings_does_not_break_graph_recall() -> None:
    index = _index()
    _fill(index, _page("d1", "独立的一页"))

    pack = index.retrieve("独立", minimum=1, maximum=12)

    assert len(pack.items) == 1


# ── 作用域隔离 ────────────────────────────────────────────────────────

def test_only_the_chunks_in_this_index_are_visible() -> None:
    """隔离靠"一个作用域一个索引"，不靠查询时过滤。

    过滤是可以忘记写的；一个只装本作用域数据的索引没有"忘记"这个失败模式。这里把两个索引
    各装一份内容，断言谁也看不见谁。
    """
    mine = _index()
    _fill(mine, _page("d-mine", "本课程的内容"))
    theirs = _index()
    _fill(theirs, _page("d-theirs", "别的课程的内容"))

    assert [item.text for item in mine.retrieve("内容", minimum=1, maximum=12).items] == ["本课程的内容"]
    assert [item.text for item in theirs.retrieve("内容", minimum=1, maximum=12).items] == ["别的课程的内容"]


def test_removing_a_document_removes_it_from_every_channel() -> None:
    """删除要从三路一起撤掉。

    结果组装会跳过不在 `_entries` 里的 id，所以留下一路**不会**让已删内容被检索出来——
    这也是为什么单看检索结果测不出这个缺陷。真正会出问题的是向量索引一直变大，以及
    "删除"在两个地方有两个不同的答案。
    """
    index = _index(dimensions=2)
    chunks = _page("d1", "要删掉的内容", "保留的内容")
    _fill(index, chunks)
    children = [c for c in chunks if c.level == "child"]
    index.add_vector(children[0].chunk_id, [1.0, 0.0])
    assert index.vector_count == 1

    index.remove_document("d1")

    assert index.vector_count == 0, "向量索引里还留着已删文档的条目"
    assert index.retrieve("内容", query_vector=[1.0, 0.0], minimum=1, maximum=12).items == ()


# ── 离线与降级 ────────────────────────────────────────────────────────

def test_retrieval_without_a_query_vector_still_works() -> None:
    """embedding 是云端的，所以离线时检索会降级成纯词法——这是 plan 里记下的后果，不是
    故障。降级要有结果，而不是报错或返回空。"""
    index = _index(dimensions=2)
    chunks = _page("d1", "二次函数")
    _fill(index, chunks)
    child = next(c for c in chunks if c.level == "child")
    index.add_vector(child.chunk_id, [1.0, 0.0])

    pack = index.retrieve("二次函数", minimum=1, maximum=12)

    assert [item.chunk_id for item in pack.items] == [child.chunk_id]


def test_a_query_vector_of_the_wrong_dimension_is_an_error_not_a_silent_downgrade() -> None:
    """维度不对必须报错。

    悄悄退回纯词法检索的话，用户看到的是"结果变差了"，而没有任何地方显示向量那一路根本没
    参与——那正是最难查的一类问题。
    """
    index = _index(dimensions=3)
    _fill(index, _page("d1", "内容"))

    with pytest.raises(RetrievalError):
        index.retrieve("内容", query_vector=[1.0, 0.0], minimum=1, maximum=12)


def test_adding_a_vector_to_an_index_without_dimensions_is_an_error() -> None:
    index = _index()
    chunks = _page("d1", "内容")
    _fill(index, chunks)
    child = next(c for c in chunks if c.level == "child")

    with pytest.raises(RetrievalError):
        index.add_vector(child.chunk_id, [1.0, 0.0])


def test_adding_a_vector_for_an_unknown_chunk_is_an_error() -> None:
    """向量必须属于索引里已有的块。接受孤儿向量会让它永远检索不到，也不报错。"""
    index = _index(dimensions=2)

    with pytest.raises(RetrievalError):
        index.add_vector("no-such-chunk", [1.0, 0.0])


# ── 证据包与边界 ──────────────────────────────────────────────────────

def test_every_evidence_item_carries_a_resolvable_location() -> None:
    """模型引用了一条证据、界面却指不回原文时，那条引用就是不可核对的。"""
    index = _index()
    _fill(index, _page("d1", "甲"))

    pack = index.retrieve("甲", minimum=1, maximum=12)

    item = pack.items[0]
    assert item.location == "page:1#1"
    assert item.document_id == "d1"
    assert item.chunk_id


def test_an_empty_index_yields_an_insufficient_pack_rather_than_an_error() -> None:
    """库是空的不是错误，是"没有依据"——弃答判断建立在这个事实上。"""
    pack = _index().retrieve("任何问题", minimum=8, maximum=12)

    assert pack.items == ()
    assert pack.is_sufficient is False


def test_a_thin_library_is_reported_as_insufficient_not_padded() -> None:
    index = _index()
    _fill(index, _page("d1", "只有一条"))

    pack = index.retrieve("只有一条", minimum=8, maximum=12)

    assert len(pack.items) == 1
    assert pack.is_sufficient is False


def test_the_evidence_limits_are_respected() -> None:
    index = _index()
    _fill(index, _page("d1", *[f"第{n}条 共同词" for n in range(20)]))

    pack = index.retrieve("共同词", minimum=3, maximum=5)

    assert len(pack.items) == 5


def test_the_recall_depths_match_the_plan() -> None:
    """三路的召回深度是 plan 写明的一组常量（40/40/10），不是随手取的数。

    把它们钉在这里，是为了让"有人顺手把它调小"变成一次失败的测试，而不是一次没人注意的
    检索质量变化。
    """
    assert (BM25_RECALL, VECTOR_RECALL, GRAPH_RECALL) == (40, 40, 10)


def test_retrieval_is_deterministic() -> None:
    index = _index(dimensions=2)
    chunks = _page("d1", "甲内容", "乙内容", "丙内容")
    _fill(index, chunks)
    for position, child in enumerate(c for c in chunks if c.level == "child"):
        index.add_vector(child.chunk_id, [1.0, float(position)])

    first = index.retrieve("内容", query_vector=[1.0, 0.5], minimum=2, maximum=12)
    second = index.retrieve("内容", query_vector=[1.0, 0.5], minimum=2, maximum=12)

    assert first == second
