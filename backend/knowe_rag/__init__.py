"""本地优先的检索与知识层（Task 3）。

目前只有解析器。按计划，这一层还要长成：加密存储（AES-256-GCM）、云端 embedding 的
网关调用路径、BM25 + 向量混合检索、以及引用与权限校验。每块都按同一流程推进——
红测 → 实现 → 独立复审达到 0 Critical / 0 Important。
"""

from __future__ import annotations

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
from .parsers import (
    LEGACY_SUFFIXES,
    SHEET_ROWS_PER_BLOCK,
    SUPPORTED_SUFFIXES,
    ParsedBlock,
    UnsupportedFormat,
    parse_document,
)
from .store import EncryptedKnowledgeStore, StoreError

__all__ = [
    "CHILD_MAX_CHARS",
    "LEGACY_SUFFIXES",
    "PARENT_MAX_CHARS",
    "SHEET_ROWS_PER_BLOCK",
    "SUPPORTED_SUFFIXES",
    "Chunk",
    "EmbeddingRequest",
    "EncryptedKnowledgeStore",
    "MasterKey",
    "MasterKeyError",
    "ParsedBlock",
    "ReindexPlan",
    "StoreError",
    "UnsupportedFormat",
    "build_chunks",
    "diff_fingerprints",
    "embedding_requests",
    "field_aad",
    "parse_document",
    "plan_reindex",
]
