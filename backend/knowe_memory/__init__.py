"""受治理的记忆层（Task 4）。

与 `knowe_rag` 平行的另一层：知识库回答"这份资料里写了什么"，记忆层回答"这个学生是谁、
他学到哪了"。两者的共同前提是**本地优先**，所以记忆也只落在加密存储里。

目前只有契约与门控（`contracts.py`）——什么能直接写、什么必须先进候选、什么能被召回。
这一层刻意不依赖任何记忆库：plan 自己要求"不能只依赖记忆库的元数据过滤"，那就意味着这条
判断必须存在于我们这一侧。
"""

from __future__ import annotations

from .contracts import (
    MemoryDecision,
    MemoryRecord,
    MemoryScope,
    MemorySensitivity,
    MemoryStatus,
    MemoryTombstone,
    can_recall,
    classify_write,
    is_expired,
    memory_fingerprint,
)

__all__ = [
    "MemoryDecision",
    "MemoryRecord",
    "MemoryScope",
    "MemorySensitivity",
    "MemoryStatus",
    "MemoryTombstone",
    "can_recall",
    "classify_write",
    "is_expired",
    "memory_fingerprint",
]
