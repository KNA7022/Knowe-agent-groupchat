"""把多路检索结果合成一个证据包：RRF、MMR、打包。

plan 第 50 行：BM25 召回 40、向量召回 40、图召回 10，然后 RRF、可选 rerank、MMR，
最后 8-12 条证据。

这里每一层都在对付一种"看起来对、实际错"的结果：

- **RRF 用名次而不是分数**：BM25 的分数无界，余弦在 [-1,1]，两路相加时尺度大的那一路会
  主导结果；
- **MMR 要归一化分数**：不归一化时 `λ*score` 压过多样性项，MMR 退化成"按分数排序"，而它
  看起来还在正常运行；
- **打包不能凑数**：只有 3 条就给 3 条并如实报告不够。把不相关的段落填进证据包，模型会
  照着它们编答案，而"我们其实没找到"这个事实被抹掉了。
"""

from __future__ import annotations

import pytest

from backend.knowe_rag.fusion import (
    EvidencePack,
    RetrievedChunk,
    maximal_marginal_relevance,
    pack_evidence,
    reciprocal_rank_fusion,
)


def _similarity(left: str, right: str) -> float:
    """词元重合度。真实的检索层会用向量余弦，但那要过网关；这里要测的是 MMR 的**选择
    逻辑**，一个确定的相似度就够，而确定的相似度让断言不会因为模型换版本而漂移。"""
    left_tokens, right_tokens = set(left.split()), set(right.split())
    union = left_tokens | right_tokens
    return len(left_tokens & right_tokens) / len(union) if union else 0.0


TEXTS = {
    "same-1": "二次函数 图像 抛物线",
    "same-2": "二次函数 图像 抛物线",
    "same-3": "二次函数 图像 抛物线",
    "other": "判别式 决定 交点",
}


def _by_text(a: str, b: str) -> float:
    return _similarity(TEXTS[a], TEXTS[b])


def _chunk(number: int, *, document_id: str = "doc", score: float | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"chunk-{number}",
        document_id=document_id,
        parent_id=f"parent-{number}",
        location=f"page:{number}#1",
        text=f"第 {number} 段内容",
        score=float(number if score is None else score),
        sources=("bm25",),
    )


# ── RRF ───────────────────────────────────────────────────────────────

def test_agreement_between_two_rankers_beats_a_single_first_place() -> None:
    """两路都排在前面，胜过只有一路排第一。

    这正是 RRF 存在的理由：一个结果被两路独立召回，比被一路排到第一更可信。
    `a` 是第 2 + 第 2，`b` 是第 1 + 缺席——前者更大。
    """
    fused = reciprocal_rank_fusion([["b", "a"], ["c", "a"]])

    assert fused[0][0] == "a", fused


def test_rrf_fuses_ranks_not_scores() -> None:
    """输入是**名次**。类型上就没有分数可传——这是刻意的。

    BM25 的分数是无界的（一个词命中多次可以到几十），余弦在 [-1,1]。把两路归一化再相加，
    尺度大的那一路会主导结果；名次天然可比，而且对离群分数不敏感：一个异常大的 BM25 分数
    只影响它自己那一名的位置。
    """
    fused = reciprocal_rank_fusion([["only"], ["also-only"]])

    assert dict(fused)["only"] == pytest.approx(1 / 61)
    assert dict(fused)["also-only"] == pytest.approx(1 / 61)


def test_a_document_missing_from_one_ranking_still_scores() -> None:
    """缺席不等于零分之外的惩罚：它只少了那一路的贡献。"""
    fused = dict(reciprocal_rank_fusion([["a", "b"], ["a"]]))

    assert fused["a"] == pytest.approx(1 / 61 + 1 / 61)
    assert fused["b"] == pytest.approx(1 / 62)


def test_rrf_order_is_deterministic_for_ties() -> None:
    """同分按 chunk_id 排序，否则两次运行给出不同顺序。"""
    first = reciprocal_rank_fusion([["a", "b"], ["b", "a"]])

    assert [chunk_id for chunk_id, _ in first] == ["a", "b"]
    assert reciprocal_rank_fusion([["a", "b"], ["b", "a"]]) == first


def test_rrf_returns_nothing_for_no_rankings() -> None:
    assert reciprocal_rank_fusion([]) == ()
    assert reciprocal_rank_fusion([[], []]) == ()


def test_rrf_ignores_a_repeated_document_within_one_ranking() -> None:
    """同一路里重复出现不该被算两次：那等于让一路给自己投票。

    真实情况下这不该发生，但一旦发生（上游拼接出错），它会让那一路的结果整体上浮，
    而排序看起来仍然"正常"。
    """
    fused = dict(reciprocal_rank_fusion([["a", "a", "b"]]))

    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["b"] == pytest.approx(1 / 62)


# ── MMR ───────────────────────────────────────────────────────────────

def test_mmr_prefers_a_diverse_second_choice_over_a_near_duplicate() -> None:
    """第一条选定之后，第二条要选"与它不像"的那个。

    没有这一步，证据包里会出现三条几乎相同的话：它们各自都很相关，合起来却只说明了一件事，
    而真正能补充的那一条被挤掉了。
    """
    candidates = [("same-1", 1.0), ("same-2", 0.99), ("other", 0.8)]

    picked = maximal_marginal_relevance(
        candidates, similarity=_by_text, limit=2, lambda_=0.5,
    )

    assert picked == ("same-1", "other")


def test_lambda_one_is_pure_relevance() -> None:
    """λ=1 时多样性项被完全关掉——这是"可选的 rerank"该有的样子：能关，且关得干净。"""
    candidates = [("same-1", 1.0), ("same-2", 0.99), ("other", 0.8)]

    picked = maximal_marginal_relevance(
        candidates, similarity=_by_text, limit=2, lambda_=1.0,
    )

    assert picked == ("same-1", "same-2")


def test_mmr_normalises_scores_so_scale_does_not_swamp_diversity() -> None:
    """BM25 的分数是无界的；不归一化的话 `λ*score` 会压过多样性项。

    那时 MMR 退化成"按分数排序"，而它看起来还在正常运行——证据包里全是同一段话的复述，
    没有任何地方显示多样化这一步实际上没起作用。
    """
    candidates = [("same-1", 42.0), ("same-2", 41.6), ("other", 33.6)]

    picked = maximal_marginal_relevance(
        candidates, similarity=_by_text, limit=2, lambda_=0.5,
    )

    assert picked == ("same-1", "other"), picked


def test_mmr_returns_at_most_the_limit_and_no_more_than_it_has() -> None:
    candidates = [("same-1", 1.0), ("same-2", 0.9)]

    assert len(maximal_marginal_relevance(
        candidates, similarity=_by_text, limit=5, lambda_=0.5,
    )) == 2
    assert maximal_marginal_relevance(
        candidates, similarity=_by_text, limit=0, lambda_=0.5,
    ) == ()


def test_mmr_handles_an_empty_candidate_list() -> None:
    assert maximal_marginal_relevance(
        [], similarity=_by_text, limit=5, lambda_=0.5,
    ) == ()


# ── 证据打包 ──────────────────────────────────────────────────────────

def test_packing_keeps_the_relevance_order_and_the_locations() -> None:
    """证据必须带着位置。

    模型引用了一条证据、界面却指不回原文，那条引用就是不可核对的——而"可核对"正是采集位置
    的全部理由。
    """
    chunks = tuple(_chunk(number, score=10.0 - number) for number in range(1, 6))

    pack = pack_evidence(chunks, minimum=3, maximum=12)

    assert [item.chunk_id for item in pack.items] == [c.chunk_id for c in chunks]
    assert all(item.location.startswith("page:") for item in pack.items)
    assert all(item.document_id for item in pack.items)


def test_packing_does_not_pad_when_there_is_too_little() -> None:
    """只有 3 条就给 3 条，并如实报告不够。

    凑数是这里最容易做、也最坏的一件事：把不相关的段落填进证据包，模型会照着它们编答案，
    而"我们其实没找到"这个事实被抹掉了。
    """
    pack = pack_evidence(tuple(_chunk(n) for n in range(1, 4)), minimum=8, maximum=12)

    assert len(pack.items) == 3
    assert pack.is_sufficient is False


def test_packing_is_sufficient_at_the_minimum() -> None:
    pack = pack_evidence(tuple(_chunk(n) for n in range(1, 9)), minimum=8, maximum=12)

    assert len(pack.items) == 8
    assert pack.is_sufficient is True


def test_packing_caps_at_the_maximum() -> None:
    """证据包有上限：给模型的上下文不是越多越好，多出来的相关段落会挤掉真正关键的那条，
    也让"这几条是依据"这件事变得模糊。"""
    pack = pack_evidence(tuple(_chunk(n) for n in range(1, 31)), minimum=8, maximum=12)

    assert len(pack.items) == 12
    assert pack.is_sufficient is True


def test_packing_drops_a_repeated_chunk_id() -> None:
    """同一个 chunk 被两路召回，在证据包里也只能占一个名额。"""
    repeated = tuple([_chunk(1), _chunk(1), _chunk(2)])

    pack = pack_evidence(repeated, minimum=1, maximum=12)

    assert [item.chunk_id for item in pack.items] == ["chunk-1", "chunk-2"]


def test_packing_an_empty_candidate_list_is_insufficient_not_an_error() -> None:
    pack = pack_evidence((), minimum=8, maximum=12)

    assert pack.items == ()
    assert pack.is_sufficient is False
    assert isinstance(pack, EvidencePack)


def test_packing_keeps_the_sources_so_an_answer_can_be_explained() -> None:
    """"这条是从哪一路找出来的"要留下来：只有 BM25 召回的条目和两路都召回的条目，
    可信程度不一样，而把这件事记下来是唯一能事后解释排序的方式。"""
    chunk = RetrievedChunk(
        chunk_id="c", document_id="d", parent_id="p", location="page:1#1",
        text="甲", score=1.0, sources=("bm25", "vector"),
    )

    pack = pack_evidence((chunk,), minimum=1, maximum=12)

    assert pack.items[0].sources == ("bm25", "vector")


@pytest.mark.parametrize("minimum,maximum", [(0, 12), (8, 0), (13, 12)])
def test_an_impossible_range_is_refused(minimum: int, maximum: int) -> None:
    """上下限写反或为负是调用方的错误，不能悄悄按某一个生效——那样"最少 8 条"这条承诺
    会在没人知道的情况下变成"最少 0 条"。"""
    with pytest.raises(ValueError):
        pack_evidence((), minimum=minimum, maximum=maximum)
