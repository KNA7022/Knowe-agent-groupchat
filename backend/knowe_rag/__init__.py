"""本地优先的检索与知识层（Task 3）。

已经落地的部分：七种格式的本地解析、分块与增量重建、DPAPI 主密钥与 AES-256-GCM 存储、
云端 embedding 的受权调用、BM25 与融合（RRF / MMR / 证据打包）。

还差：向量索引（USearch）与图召回、权限与课程隔离校验、精确引用与弃答。每块都按同一流程
推进——红测 → 实现 → 独立复审达到 0 Critical / 0 Important。
"""

from __future__ import annotations

from .bm25 import Bm25Index, tokenize
from .chunks import (
    CHILD_MAX_CHARS,
    PARENT_MAX_CHARS,
    Chunk,
    EmbeddingRequest,
    ReindexPlan,
    build_chunks,
    diff_fingerprints,
    embedding_requests,
    plan_reindex,
)
from .crypto import MasterKey, MasterKeyError, field_aad
from .fusion import (
    EVIDENCE_MAXIMUM,
    EVIDENCE_MINIMUM,
    EvidencePack,
    RetrievedChunk,
    maximal_marginal_relevance,
    pack_evidence,
    reciprocal_rank_fusion,
)
from .parsers import (
    LEGACY_SUFFIXES,
    SHEET_ROWS_PER_BLOCK,
    SUPPORTED_SUFFIXES,
    ParsedBlock,
    UnsupportedFormat,
    parse_document,
)
from .retrieval import (
    BM25_RECALL,
    GRAPH_RECALL,
    VECTOR_RECALL,
    RetrievalError,
    ScopeIndex,
)
from .store import EncryptedKnowledgeStore, StoreError
from .vector_index import VectorIndex, VectorIndexError

__all__ = [
    "BM25_RECALL",
    "CHILD_MAX_CHARS",
    "EVIDENCE_MAXIMUM",
    "EVIDENCE_MINIMUM",
    "GRAPH_RECALL",
    "LEGACY_SUFFIXES",
    "PARENT_MAX_CHARS",
    "SHEET_ROWS_PER_BLOCK",
    "SUPPORTED_SUFFIXES",
    "VECTOR_RECALL",
    "Bm25Index",
    "Chunk",
    "EmbeddingRequest",
    "EncryptedKnowledgeStore",
    "EvidencePack",
    "MasterKey",
    "MasterKeyError",
    "ParsedBlock",
    "ReindexPlan",
    "RetrievalError",
    "RetrievedChunk",
    "ScopeIndex",
    "StoreError",
    "UnsupportedFormat",
    "VectorIndex",
    "VectorIndexError",
    "build_chunks",
    "diff_fingerprints",
    "embedding_requests",
    "field_aad",
    "maximal_marginal_relevance",
    "pack_evidence",
    "parse_document",
    "plan_reindex",
    "reciprocal_rank_fusion",
    "tokenize",
]
