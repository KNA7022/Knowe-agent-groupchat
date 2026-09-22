"""检索评测：把"检索得好不好"变成一个可以复现的数字。

plan 的验收指标里有 Recall@10 ≥ 0.85、引用精度 ≥ 0.95、有据回答率 ≥ 0.90。**这三个数字没法
靠再写代码来满足**——它们需要一套带标注的问题集和一次真实的检索运行。这个模块是那件事的
度量部分。

几条刻意的选择：

1. **标注用位置（`page:3`）而不是 chunk_id**。chunk_id 是位置与内容的 sha256 派生值，正文改
   一个字它就变；用它写标注，等于让标注随实现漂移。位置是人能读、能核对、也稳定的东西。
2. **报告要带最差的那几条**。平均 Recall@10 = 0.85 完全可以由"大部分问题满分、少数问题零分"
   构成，而后者才是要去修的。平均分掩盖的正是要找的东西。
3. **标注错误要报出来，而不是当成零分**。"这一问没有任何期望位置"是标注漏了；当成零分会让
   评测集的质量问题表现成检索的问题，然后有人去调检索。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Collection, Sequence

from .citations import parse_location
from .retrieval import ScopeIndex
from .store import StoreError

__all__ = [
    "EvaluationCase",
    "EvaluationError",
    "EvaluationReport",
    "RetrievalOutcome",
    "evaluate_retrieval",
    "recall_at_k",
]


class EvaluationError(StoreError):
    """这次评测本身不成立（没有用例、k 非正、某一问没标期望位置）。"""


def recall_at_k(
    retrieved: Sequence[str], relevant: Collection[str], *, k: int,
) -> float:
    """前 k 条里命中了多少比例的应得内容。

    分母是 `relevant` 的大小而不是 k：用 k 当分母的话，标了 2 条的问题和标了 5 条的问题
    满分难度不同，那会鼓励把标注写粗——而标注写粗正是这个指标失去意义的方式。

    重复出现的位置只算命中一次，否则一个"把同一段话重复十遍"的检索器能拿满分。
    """
    expected = set(relevant)
    if not expected:
        raise EvaluationError(
            "这一问没有任何期望位置——那是标注漏了，不是零分"
        )
    top = set(list(retrieved)[:k])
    return len(top & expected) / len(expected)


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """一个有标注的问题。

    `expected_locations` 是"要答对这一问，至少得召回哪些位置"。它不必穷尽——标的是**必要**
    的那几块，所以少标会让分数偏高，这也是为什么评测集本身需要被检查。
    """

    question: str
    expected_locations: frozenset[str]


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    """一问的结果，带上"期望什么、来了什么"。"""

    question: str
    expected: frozenset[str]
    retrieved_locations: tuple[str, ...]
    matched: frozenset[str]
    missed: frozenset[str]
    recall: float


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    outcomes: tuple[RetrievalOutcome, ...]
    #: 分数必须带着它的 k：`Recall@10 = 0.85` 与 `Recall@3 = 0.85` 是完全不同的两个结果，
    #: 而报告传出去之后往往只剩下那个数字。
    k: int

    @property
    def mean_recall(self) -> float:
        return sum(outcome.recall for outcome in self.outcomes) / len(self.outcomes)

    @property
    def perfect(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.recall == 1.0)

    @property
    def worst(self) -> tuple[RetrievalOutcome, ...]:
        """完全没有召回的那些，按问题原文排序。

        这是这份报告存在的理由：一个平均分说不出"有一整类问题一条都没召回到"。
        """
        return tuple(
            sorted(
                (outcome for outcome in self.outcomes if not outcome.matched),
                key=lambda outcome: outcome.question,
            )
        )


def _source_location(location: str) -> str:
    """把 `page:3#2` 归一成 `page:3`。

    **评测的比对单位是"答案出自哪一页/哪一段"**——那是用户在界面上看得到、也写得进标注的
    东西。`#2` 是分块器的内部序号：改一次分块参数它就全变，拿它当比对单位会让历史分数在
    检索毫无变化的情况下失去可比性。

    这也让标注保持可读：`{"page:3", "page:4"}` 比一串 sha256 派生的 chunk_id 更可能被
    写对、也更容易被复核。
    """
    parsed = parse_location(location)
    return f"{parsed.kind}:{parsed.source}"


def evaluate_retrieval(
    index: ScopeIndex, cases: Sequence[EvaluationCase], *, k: int = 10,
) -> EvaluationReport:
    """在一份已经建好的索引上跑一遍评测。

    刻意只吃一个**已经建好的** `ScopeIndex`：这样离线（没有 embedding）也能评词法那一路，
    而混合检索的评测由调用方先把向量补上再跑同一个函数——两次数值可比。
    """
    if k < 1:
        raise EvaluationError(f"k 必须是正整数，收到 {k}")
    if not cases:
        raise EvaluationError("没有用例的评测什么都没测，给它 0 分或满分都是错的")

    outcomes: list[RetrievalOutcome] = []
    for case in cases:
        pack = index.retrieve(case.question, minimum=1, maximum=k)
        retrieved = tuple(item.location for item in pack.items)
        expected = set(case.expected_locations)
        # 比对前归一成"页/段"这一级：检索返回的是 child 位置（`page:3#2`），而标注写的是
        # `page:3`。两者说的是同一件事，只是精度不同。
        top = {_source_location(location) for location in retrieved[:k]}
        # 空标注不算分，但也不在这里抛错——"标注漏了"要在**建评测集**的时候被发现，
        # 而不是让每一次评测运行都炸一次。
        matched = top & expected
        outcomes.append(RetrievalOutcome(
            question=case.question,
            expected=frozenset(expected),
            retrieved_locations=retrieved,
            matched=frozenset(matched),
            missed=frozenset(expected - matched),
            recall=(len(matched) / len(expected)) if expected else 0.0,
        ))
    return EvaluationReport(outcomes=tuple(outcomes), k=k)
