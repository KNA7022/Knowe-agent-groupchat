"""检索评测：把"检索得好不好"变成一个可以复现的数字。

plan 的验收指标里有 Recall@10 ≥ 0.85、引用精度 ≥ 0.95、有据回答率 ≥ 0.90。**这三个数字
没法靠再写代码来满足**——它们需要一套带标注的问题集和一次真实的检索运行。这个模块是那件事
的度量部分。

几条刻意的选择：

1. **标注用位置（`page:3`）而不是 chunk_id**。chunk_id 是位置与内容的 sha256 派生值，改一个
   字就变；用它写标注，等于让标注随实现漂移。位置是人能读、能核对、也稳定的东西。
2. **报告要带最差的那几条**。平均 Recall@10 = 0.85 完全可以由"大部分问题满分、少数问题
   零分"构成，而后者才是要去修的。平均分掩盖的正是要找的东西。
3. **标注错误要报出来**。"这一问没有任何期望位置"不是零分，是标注漏了——把它当成零分会
   让评测集的质量问题看起来像检索的问题。
"""

from __future__ import annotations

import pytest

from backend.knowe_rag import ParsedBlock, build_chunks
from backend.knowe_rag.evaluation import (
    EvaluationCase,
    EvaluationError,
    evaluate_retrieval,
    recall_at_k,
)
from backend.knowe_rag.retrieval import ScopeIndex


def _indexed(*pages: str, document_id: str = "doc") -> ScopeIndex:
    index = ScopeIndex()
    for chunk in build_chunks(document_id, tuple(
        ParsedBlock(text=text, location=f"page:{number}", kind="page")
        for number, text in enumerate(pages, start=1)
    )):
        index.add(chunk)
    return index


# ── 召回率 ────────────────────────────────────────────────────────────

def test_recall_is_the_share_of_expected_items_found() -> None:
    assert recall_at_k(["a", "b", "c"], {"a", "b"}, k=3) == 1.0
    assert recall_at_k(["a", "x"], {"a", "b"}, k=3) == 0.5
    assert recall_at_k(["x", "y"], {"a", "b"}, k=3) == 0.0


def test_recall_only_looks_at_the_top_k() -> None:
    """第 k 名之后的命中不算。

    这是 Recall@10 与"最终找到了没有"的区别：模型只看得到前 10 条，第 11 条再相关也进不了
    证据包。
    """
    assert recall_at_k(["a", "b", "c"], {"c"}, k=2) == 0.0
    assert recall_at_k(["a", "b", "c"], {"c"}, k=3) == 1.0


def test_the_denominator_is_how_many_items_were_expected() -> None:
    """分母是标注里"应得"的条数，不是 k。

    用 k 当分母的话，一个问题标了 2 条而另一个标了 5 条，两者的满分难度就不一样了——标注
    得越细的问题分数越低，那会鼓励把标注写粗。
    """
    assert recall_at_k(["a"], {"a", "b", "c", "d"}, k=10) == 0.25


def test_a_case_with_nothing_expected_is_refused() -> None:
    """没有期望位置不是零分，是**标注漏了**。

    当成零分的话，评测集的质量问题会表现成检索的问题，然后有人去调检索——而真正该改的是
    标注。
    """
    with pytest.raises(EvaluationError):
        recall_at_k(["a"], set(), k=10)


def test_repeated_retrievals_do_not_inflate_recall() -> None:
    """同一个位置在结果里出现两次，只算命中一次。

    否则一个"把同一段话重复十遍"的检索器能拿满分。
    """
    assert recall_at_k(["a", "a", "a"], {"a", "b"}, k=10) == 0.5


# ── 整份评测 ──────────────────────────────────────────────────────────

def test_a_perfect_retrieval_scores_one() -> None:
    index = _indexed("二次函数的图像是抛物线", "判别式决定交点个数")

    report = evaluate_retrieval(index, [
        EvaluationCase(question="抛物线的图像", expected_locations=frozenset({"page:1"})),
    ], k=10)

    assert report.mean_recall == 1.0
    assert report.perfect == 1


def test_a_missed_question_scores_zero_and_shows_up_in_the_worst_list() -> None:
    """最差清单是这份报告存在的理由。

    平均分 0.85 可以由"四个满分、一个零分"构成，而有问题的正是那一个——只看平均分的人
    会以为"还差一点点"，实际是"有一整类问题完全没被召回"。
    """
    index = _indexed("二次函数的图像是抛物线", "判别式决定交点个数")

    report = evaluate_retrieval(index, [
        EvaluationCase(question="抛物线", expected_locations=frozenset({"page:1"})),
        EvaluationCase(question="完全无关的词汇xyzzy", expected_locations=frozenset({"page:2"})),
    ], k=10)

    assert report.mean_recall == 0.5
    assert [outcome.question for outcome in report.worst] == ["完全无关的词汇xyzzy"]
    assert report.worst[0].missed == frozenset({"page:2"})


def test_the_report_says_which_locations_were_expected_and_which_arrived() -> None:
    """一条失败的评测要能直接读出"期望什么、来了什么"。

    只给一个分数的话，要复现就得自己去翻检索结果——而评测的意义就是省掉那一步。
    """
    index = _indexed("二次函数的图像是抛物线")

    report = evaluate_retrieval(index, [
        EvaluationCase(question="抛物线", expected_locations=frozenset({"page:1"})),
    ], k=10)

    outcome = report.outcomes[0]
    assert outcome.expected == frozenset({"page:1"})
    assert outcome.matched == frozenset({"page:1"})
    # `retrieved_locations` 保留检索给出的**原始**位置（带 child 序号）：诊断时要看到底命中
    # 了哪一块，而不只是哪一页。比对用的是归一化后的那一级。
    assert outcome.retrieved_locations == ("page:1#1",)


def test_a_child_location_matches_its_page_in_the_annotation() -> None:
    """检索返回 `page:1#1`，标注写 `page:1`，两者算命中。

    比对单位是"哪一页/哪一段"——用户看得到、也写得进标注；`#1` 是分块器的内部序号，改一次
    分块参数它就全变。拿它当比对单位，历史分数会在检索毫无变化的情况下失去可比性。
    """
    index = _indexed("二次函数的图像是抛物线")

    report = evaluate_retrieval(index, [
        EvaluationCase(question="抛物线", expected_locations=frozenset({"page:1"})),
    ], k=10)

    assert report.outcomes[0].recall == 1.0
    assert report.outcomes[0].retrieved_locations[0].startswith("page:1")


def test_an_empty_case_list_is_refused() -> None:
    """没有用例的评测给出 0 分或满分都是错的——它什么都没测。"""
    with pytest.raises(EvaluationError):
        evaluate_retrieval(_indexed("内容"), [], k=10)


def test_a_non_positive_k_is_refused() -> None:
    with pytest.raises(EvaluationError):
        evaluate_retrieval(
            _indexed("内容"),
            [EvaluationCase(question="内容", expected_locations=frozenset({"page:1"}))],
            k=0,
        )


def test_the_k_used_is_recorded_in_the_report() -> None:
    """分数必须带着它的 k。

    一个"Recall@10 = 0.85"和一个"Recall@3 = 0.85"是完全不同的两个结果，而报告传出去之后
    只剩下那个数字。
    """
    report = evaluate_retrieval(
        _indexed("内容"),
        [EvaluationCase(question="内容", expected_locations=frozenset({"page:1"}))],
        k=3,
    )

    assert report.k == 3


def test_evaluation_is_deterministic() -> None:
    index = _indexed("甲内容", "乙内容")
    cases = [EvaluationCase(question="内容", expected_locations=frozenset({"page:1"}))]

    assert evaluate_retrieval(index, cases, k=5) == evaluate_retrieval(index, cases, k=5)


def test_a_case_whose_expected_locations_are_absent_from_the_library_still_scores() -> None:
    """标注了一个库里根本没有的位置：那是 0 分，不是错误。

    它和"标注为空"不同——空是忘了写，指到一个不存在的位置是写错了，但两者都该如实记零分
    而不是让评测崩掉：一份标注有瑕疵的评测集仍然比没有评测强，只要分数如实。
    """
    report = evaluate_retrieval(
        _indexed("内容"),
        [EvaluationCase(question="内容", expected_locations=frozenset({"page:99"}))],
        k=10,
    )

    assert report.mean_recall == 0.0
    assert report.worst[0].missed == frozenset({"page:99"})
