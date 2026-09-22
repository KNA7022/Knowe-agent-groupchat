"""把多路检索结果合成一个证据包。

plan 第 50 行的后半段：RRF → 可选 rerank → MMR → 8-12 条证据。这一层不碰网络、不碰数据库，
输入输出都是普通对象，因此它的每条规则都能被单独钉住。

三层各自对付一种"看起来对、实际错"的结果：

* **RRF 用名次而不是分数**——BM25 的分数无界，余弦在 [-1,1]；两路相加时尺度大的那一路会
  主导结果，而排出来的顺序看起来完全正常。
* **MMR 要先把分数归一化**——不归一化时 `λ*score` 压过多样性项，MMR 退化成"按分数排序"，
  而它看起来还在运行。
* **打包不能凑数**——不足 8 条就给不足的数量并如实报告。把不相关的段落填进证据包，模型会
  照着它们编答案，而"其实没找到"这个事实被抹掉了。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

__all__ = [
    "DEFAULT_RRF_K",
    "EVIDENCE_MAXIMUM",
    "EVIDENCE_MINIMUM",
    "EvidencePack",
    "MMR_LAMBDA",
    "RetrievedChunk",
    "maximal_marginal_relevance",
    "pack_evidence",
    "reciprocal_rank_fusion",
]

#: RRF 的排名平滑常数，取原论文的 60。它压住头部：k 越大，前几名之间的差距越小。这里沿用
#: 而不是自己调——调参要有评测集，而评测集是 Task 6 的事。
DEFAULT_RRF_K = 60

#: MMR 的默认 λ：0.7 表示"七分看相关性、三分看多样性"。偏向相关性是刻意的——多样性是
#: 防止证据包退化成同一段话的复述，不是让不相关的内容挤进来。
MMR_LAMBDA = 0.7

#: 证据包的条数区间（plan 第 50 行的 8-12）。
EVIDENCE_MINIMUM = 8
EVIDENCE_MAXIMUM = 12


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """一条检索结果。

    `location` 与 `document_id` 是**引用**所需的全部信息：模型引用了一条证据、界面却指不回
    原文时，那条引用就是不可核对的。`sources` 记下它由哪几路召回——只有 BM25 召回和两路都
    召回的可信程度不同，而这是事后唯一能解释排序的东西。
    """

    chunk_id: str
    document_id: str
    parent_id: str
    location: str
    text: str
    score: float
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvidencePack:
    """一次打包的结果，以及"够不够"这个事实。

    把"够不够"做成一个属性而不是让调用方自己比大小：弃答判断就落在这里，而写成一目了然的
    一个布尔值，比散落在调用方的 `len(items) < 8` 更难漏掉。
    """

    items: tuple[RetrievedChunk, ...]
    minimum: int

    @property
    def is_sufficient(self) -> bool:
        return len(self.items) >= self.minimum


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[str]], *, k: int = DEFAULT_RRF_K,
) -> tuple[tuple[str, float], ...]:
    """把多路**名次**合并成一个排序，返回 `(chunk_id, score)` 降序。

    输入是名次序列，没有分数可传；这是刻意的（见模块文档）。同分按 `chunk_id` 排序，
    否则同样一次检索在两次运行里给出不同顺序——而"这次第一条和上次不一样"会被当成模型
    不稳定，实际只是字典迭代顺序。

    同一路里重复出现的 id 只算第一次，且**名次按去重后重新计数**：重复项如果也让名次往前
    走，它后面那些真正不同的结果会被整体低估——那一路的影响力被一个上游拼接错误悄悄稀释，
    而排序看起来仍然正常。
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen: set[str] = set()
        for chunk_id in ranking:
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            rank = len(seen)
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return tuple(sorted(scores.items(), key=lambda pair: (-pair[1], pair[0])))


def maximal_marginal_relevance(
    candidates: Sequence[tuple[str, float]],
    *,
    similarity: Callable[[str, str], float],
    limit: int,
    lambda_: float = MMR_LAMBDA,
) -> tuple[str, ...]:
    """在相关性与多样性之间取平衡，返回选中的 chunk_id 顺序。

    `similarity(a, b)` 由调用方给：文本可以用词元重合，向量可以用余弦。这一层不预设是哪
    一种，因为检索层同时有这两样东西。

    分数在这里**按最大值归一化**，所以调用方不必关心尺度——BM25 的分数是无界的，不归一化
    会让第一项盖过第二项，MMR 就变成了"按分数排序"而看不出来。
    """
    if limit <= 0 or not candidates:
        return ()
    unique: dict[str, float] = {}
    for chunk_id, score in candidates:
        unique.setdefault(chunk_id, float(score))
    ceiling = max(unique.values())
    relevance = {
        chunk_id: (score / ceiling if ceiling > 0 else 0.0)
        for chunk_id, score in unique.items()
    }

    selected: list[str] = []
    remaining = dict(relevance)
    while remaining and len(selected) < limit:
        best_id = ""
        best_value = 0.0
        for chunk_id, score in remaining.items():
            overlap = max(
                (similarity(chunk_id, chosen) for chosen in selected),
                default=0.0,
            )
            value = lambda_ * score - (1.0 - lambda_) * overlap
            # 严格大于：平手时保留先出现的那个，顺序由输入决定而不是字典。
            if not best_id or value > best_value:
                best_id, best_value = chunk_id, value
        selected.append(best_id)
        remaining.pop(best_id)
    return tuple(selected)


def pack_evidence(
    candidates: Sequence[RetrievedChunk],
    *,
    minimum: int = EVIDENCE_MINIMUM,
    maximum: int = EVIDENCE_MAXIMUM,
) -> EvidencePack:
    """把候选整理成证据包：去重、按相关性保留、封顶。

    **不补足**。不足 `minimum` 时返回多少就是多少，由 `is_sufficient` 如实报告——弃答判断
    建立在这个事实之上，而凑数会让它失效。
    """
    if minimum < 1 or maximum < 1 or minimum > maximum:
        # 悄悄按其中一个生效的话，"最少 8 条"这条承诺会在没人知道的情况下变成"最少 0 条"。
        raise ValueError(
            f"证据条数区间不合法：minimum={minimum}, maximum={maximum}"
        )
    seen: set[str] = set()
    items: list[RetrievedChunk] = []
    for chunk in candidates:
        if chunk.chunk_id in seen:
            continue
        seen.add(chunk.chunk_id)
        items.append(chunk)
        if len(items) >= maximum:
            break
    return EvidencePack(items=tuple(items), minimum=minimum)
