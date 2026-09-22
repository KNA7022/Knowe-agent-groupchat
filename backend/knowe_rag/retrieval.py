"""检索入口：三路召回 → RRF → MMR → 证据包。

plan 第 50 行：BM25 召回 40、向量召回 40、图召回 10，然后 RRF、可选 rerank、MMR、8-12 条
证据。这一层把已经做好的几块（`bm25` / `vector_index` / `fusion`）串起来，并加上作用域。

**作用域隔离靠"一个作用域一个索引"，不靠查询时过滤。** 这不是风格问题：过滤是可以忘记写的，
而一个只装本作用域数据的索引没有"忘记"这个失败模式——跨群组、跨课程的数据看不见，是因为它
根本不在这里。

**图召回补的是上下文，不是相似度。** BM25 与向量都在回答"这块文本本身像不像问题"，而一段
完整论述常常被切成相邻的两块：命中了后半句，前半句在隔壁。兄弟块（同一 parent 下的其它
child）天然就是同一页、同一节的连续内容。

**离线时降级成纯词法检索**，这是 plan 里记下的后果（embedding 在云端），不是故障：没有查询
向量就不走向量那一路，检索仍然给出结果。但**维度不对要报错**——悄悄降级会让"结果变差了"
变成一个查不出来的现象。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .bm25 import Bm25Index, tokenize
from .chunks import CHILD, Chunk
from .fusion import (
    EVIDENCE_MAXIMUM,
    EVIDENCE_MINIMUM,
    MMR_LAMBDA,
    EvidencePack,
    RetrievedChunk,
    maximal_marginal_relevance,
    pack_evidence,
    reciprocal_rank_fusion,
)
from .store import StoreError
from .vector_index import VectorIndex, VectorIndexError

__all__ = [
    "BM25_RECALL",
    "GRAPH_RECALL",
    "VECTOR_RECALL",
    "RetrievalError",
    "ScopeIndex",
]

#: 三路的召回深度（plan 第 50 行）。三路各自多取一些、再由 RRF 合并，比让某一路直接决定
#: 最终名单要好：任一路的头部都可能被它自己的偏差带歪。
BM25_RECALL = 40
VECTOR_RECALL = 40
GRAPH_RECALL = 10

#: 图那一路从多少个种子里扩展。取词法那一路的前几名：它们是"大概率相关"的锚点。
GRAPH_SEEDS = 5


class RetrievalError(StoreError):
    """这次检索请求本身不成立（维度不对、向量属于一个不存在的块）。"""


@dataclass(frozen=True, slots=True)
class _Entry:
    chunk: Chunk
    sources: tuple[str, ...]


class ScopeIndex:
    """一个作用域（群组 × 课程）内的检索索引。

    `dimensions=None` 表示这个索引不做向量检索——离线、或者用户没配置 embedding 服务时就是
    这样。**显式传 None 而不是"等第一个向量来了再定维度"**：维度属于索引的身份，从一个偶然
    先到的向量推断出来，意味着索引的语义由到达顺序决定。
    """

    def __init__(self, *, dimensions: int | None = None) -> None:
        self._bm25 = Bm25Index()
        self._vectors = VectorIndex(dimensions=dimensions) if dimensions else None
        self._entries: dict[str, Chunk] = {}
        self._documents: dict[str, set[str]] = {}
        self._siblings: dict[str, list[str]] = {}
        self._order: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def dimensions(self) -> int | None:
        return self._vectors.dimensions if self._vectors is not None else None

    @property
    def vector_count(self) -> int:
        """向量索引里现有的条目数。

        单独暴露出来是为了让"删除也撤掉向量"这件事**可测**：组装结果时会跳过不在
        `_entries` 里的 id，所以一条孤儿向量不会让已删内容被返回；但向量索引会一直变大，
        而"删除"在两个地方有两个不同的答案——那迟早会咬人。
        """
        return len(self._vectors) if self._vectors is not None else 0

    # ── 写入 ──────────────────────────────────────────────────────────

    def add(self, chunk: Chunk) -> None:
        """加入或替换一个块。

        只有 **child** 进检索索引：parent 是给模型的上下文单元，它太长、而且内容已被自己的
        child 覆盖；两样都收会让同一段文字在结果里出现两次，抢同一个证据名额。
        """
        if chunk.level != CHILD:
            return
        self.remove(chunk.chunk_id)
        self._entries[chunk.chunk_id] = chunk
        self._order[chunk.chunk_id] = len(self._order)
        self._documents.setdefault(chunk.document_id, set()).add(chunk.chunk_id)
        self._siblings.setdefault(chunk.parent_id, []).append(chunk.chunk_id)
        self._bm25.add(chunk.chunk_id, chunk.text)

    def add_vector(self, chunk_id: str, vector: Sequence[float]) -> None:
        """给一个已经加入的块补上向量。

        向量是单独一步，因为算它要过网关（异步、可能失败、可能根本没配置）。这样"离线也能
        建索引"和"补上向量"是两件互不阻塞的事。
        """
        if self._vectors is None:
            raise RetrievalError(
                "这个索引没有维度，不接受向量——构造时传 dimensions 才能做混合检索"
            )
        if chunk_id not in self._entries:
            raise RetrievalError(
                f"向量属于一个不在索引里的块：{chunk_id}。"
                "接受孤儿向量会让它永远检索不到，而没有任何地方报错"
            )
        try:
            self._vectors.add(chunk_id, vector)
        except VectorIndexError as exc:
            raise RetrievalError(str(exc)) from exc

    def remove(self, chunk_id: str) -> bool:
        chunk = self._entries.pop(chunk_id, None)
        if chunk is None:
            return False
        self._order.pop(chunk_id, None)
        self._bm25.remove(chunk_id)
        if self._vectors is not None:
            self._vectors.remove(chunk_id)
        document = self._documents.get(chunk.document_id)
        if document is not None:
            document.discard(chunk_id)
            if not document:
                del self._documents[chunk.document_id]
        siblings = self._siblings.get(chunk.parent_id)
        if siblings is not None:
            siblings.remove(chunk_id)
            if not siblings:
                del self._siblings[chunk.parent_id]
        return True

    def remove_document(self, document_id: str) -> int:
        """撤掉一份文档的全部块，三路一起撤。

        留下一路，已删文档的内容就还能被检索出来——而"我删掉了那份文件"之后答案还能引用它，
        是删除功能最不能出的错。
        """
        removed = 0
        for chunk_id in tuple(self._documents.get(document_id, ())):
            if self.remove(chunk_id):
                removed += 1
        return removed

    # ── 检索 ──────────────────────────────────────────────────────────

    def _graph_neighbours(self, seeds: Sequence[str]) -> tuple[str, ...]:
        """从命中的块出发取兄弟块，保持它们在原始文本里的先后。"""
        picked: list[str] = []
        seen: set[str] = set(seeds)
        for seed in seeds:
            chunk = self._entries.get(seed)
            if chunk is None:
                continue
            order = self._siblings.get(chunk.parent_id, ())
            for sibling in sorted(order, key=lambda item: self._order.get(item, 0)):
                if sibling in seen:
                    continue
                seen.add(sibling)
                picked.append(sibling)
                if len(picked) >= GRAPH_RECALL:
                    return tuple(picked)
        return tuple(picked)

    def retrieve(
        self,
        query: str,
        *,
        query_vector: Sequence[float] | None = None,
        minimum: int = EVIDENCE_MINIMUM,
        maximum: int = EVIDENCE_MAXIMUM,
        lambda_: float = MMR_LAMBDA,
    ) -> EvidencePack:
        """按三路召回、融合、去冗余，返回证据包。"""
        lexical = tuple(
            chunk_id for chunk_id, _ in self._bm25.search(query, limit=BM25_RECALL)
        )
        vector: tuple[str, ...] = ()
        if query_vector is not None:
            if self._vectors is None:
                raise RetrievalError(
                    "收到了查询向量，但这个索引没有维度——"
                    "静默忽略会让向量那一路根本没参与，而结果看起来只是差了一点"
                )
            try:
                vector = tuple(
                    chunk_id
                    for chunk_id, _ in self._vectors.search(query_vector, limit=VECTOR_RECALL)
                )
            except VectorIndexError as exc:
                raise RetrievalError(str(exc)) from exc
        graph = self._graph_neighbours(lexical[:GRAPH_SEEDS])

        channels = {
            "bm25": lexical,
            "vector": vector,
            "graph": graph,
        }
        fused = reciprocal_rank_fusion([lexical, vector, graph])
        scores = dict(fused)
        sources: dict[str, list[str]] = {}
        for name, ranking in channels.items():
            for chunk_id in ranking:
                sources.setdefault(chunk_id, []).append(name)

        candidates: list[RetrievedChunk] = []
        for chunk_id, score in fused:
            chunk = self._entries.get(chunk_id)
            if chunk is None:
                continue
            candidates.append(RetrievedChunk(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                parent_id=chunk.parent_id,
                location=chunk.location,
                text=chunk.text,
                score=score,
                sources=tuple(sources.get(chunk_id, ())),
            ))

        if not candidates:
            return pack_evidence((), minimum=minimum, maximum=maximum)

        tokens = {chunk.chunk_id: set(tokenize(chunk.text)) for chunk in candidates}
        by_id = {chunk.chunk_id: chunk for chunk in candidates}

        def similarity(left: str, right: str) -> float:
            left_tokens, right_tokens = tokens[left], tokens[right]
            union = left_tokens | right_tokens
            return len(left_tokens & right_tokens) / len(union) if union else 0.0

        chosen = maximal_marginal_relevance(
            [(chunk.chunk_id, scores[chunk.chunk_id]) for chunk in candidates],
            similarity=similarity,
            limit=maximum,
            lambda_=lambda_,
        )
        return pack_evidence(
            tuple(by_id[chunk_id] for chunk_id in chosen),
            minimum=minimum,
            maximum=maximum,
        )
