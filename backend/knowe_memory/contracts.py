"""记忆的契约与门控：什么能直接写、什么必须先进候选、什么能被召回。

plan 第 75 行要求这一组类型，第 79-81 行给出规则：

> Direct, non-sensitive facts may be written after deterministic checks. Inferred preferences,
> weak points, grades and sensitive facts enter an isolated Candidate namespace and cannot be
> recalled until approved.
>
> Enforce group/course/sensitivity/status scope before and after every Mem0 query; never rely
> solely on Mem0 metadata filters.
>
> Version edits, enforce TTL, write tombstones, prevent relearning and cascade deletion.

**这一层不依赖 Mem0，也不依赖任何库。** 它是"什么东西允许被记住、允许被想起来"的规则本身，
写成纯逻辑有两个好处：它可以被逐条钉住；换掉底层记忆库时这些规则不用重写——而"不能只依赖
记忆库的元数据过滤"正是 plan 自己提的要求，那就意味着这条判断必须存在于**我们这一侧**。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import re

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


class MemorySensitivity(Enum):
    """这条记忆有多敏感。"""

    PUBLIC = "public"
    """课程内容、公开事实。"""

    PERSONAL = "personal"
    """偏好、薄弱点——描述的是这个人。"""

    SENSITIVE = "sensitive"
    """成绩、评语、联系方式——泄漏一次就收不回来。"""


class MemoryStatus(Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    REJECTED = "rejected"
    EXPIRED = "expired"
    DELETED = "deleted"


#: 这些内容即使被标成 PUBLIC 也要走审批。plan 第 79 行点名了成绩与敏感事实；这里用模式把它
#: 变成**不依赖调用方记得标注**的规则——调用方忘记标，成绩就会以"公开事实"的身份直接进记忆，
#: 而它恰恰是最不该被随便想起来的东西之一。
_ALWAYS_SENSITIVE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("grade", re.compile(
        r"(?i)(?:成绩|分数|得分|考了|考到|绩点|排名|名次|grade|score|rank)"
        r"\s*(?:是|为|:|：)?\s*(?:第\s*)?\d+"
    )),
    ("contact", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("contact", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")),
    ("identifier", re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")),
    ("weakness", re.compile(r"(?i)(?:薄弱|不擅长|总是错|老是错|学不会|听不懂)")),
)


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """一条记忆属于哪里，以及它现在处于什么状态。

    四个维度**都要**在召回时匹配。`sensitivity` 也是其中一维：一次"只要公开内容"的召回不该
    带回个人记忆。
    """

    group_id: str
    course_id: str
    sensitivity: MemorySensitivity
    status: MemoryStatus


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """一条被记住的东西。

    `source_document_id` 与 `version` 是必需字段而不是可选项：一条说不出来源的记忆，用户没法
    核对它是否属实——而"它记错了我说的话"是这类功能最常见的抱怨。
    """

    memory_id: str
    scope: MemoryScope
    text: str
    source_document_id: str
    version: int
    created_at: str
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryDecision:
    """这条内容该以什么状态写进去，以及**为什么**。

    带上理由是因为界面上"这条要你确认一下"如果不说明原因，用户只会机械地点同意——而那正是
    审批要防的事。
    """

    status: MemoryStatus
    reason: str

    @property
    def needs_approval(self) -> bool:
        return self.status is MemoryStatus.CANDIDATE


@dataclass(frozen=True, slots=True)
class MemoryTombstone:
    """一条被删除的记忆留下的标记。

    它的作用不是记录"删过什么"，而是**让同样的内容不能被重新学回来**（plan 第 81 行的
    "prevent relearning"）。没有它，"删除"只在用户点它的那一刻有效：下一次同一份资料被导入，
    或者模型又推断出同一件事，那条记忆就回来了——而用户以为它已经没了。
    """

    memory_id: str
    scope: MemoryScope
    fingerprint: str
    deleted_at: str

    def blocks(self, *, text: str, scope: MemoryScope) -> bool:
        """这份内容在这个作用域里是不是已经被删过了。

        只看 group 与 course：敏感度和状态是"这条记忆现在算什么"，而墓碑要挡的是"同一门课里
        同一句话"。用敏感度一起判会让一句被删掉的公开事实在它被重新判定为个人内容时溜回来。
        """
        if scope.group_id != self.scope.group_id or scope.course_id != self.scope.course_id:
            return False
        return memory_fingerprint(text) == self.fingerprint


def memory_fingerprint(text: str) -> str:
    """墓碑用的内容指纹。**只看文本**，所以同一句话在任何作用域里都得到同一个值。

    这里用无盐哈希是**有意的**：墓碑只在库内比对，不落盘成"这句话在不在库里"的查询接口——
    墓碑表本身与记忆表一样受存储层加密保护。加密存储那一侧另有一套绑定密钥的指纹（见
    `knowe_rag.crypto.keyed_content_hasher`），两者用途不同，不要互相替换。
    """
    return hashlib.sha256(str(text).strip().encode("utf-8")).hexdigest()


def classify_write(
    *, sensitivity: MemorySensitivity, inferred: bool, text: str = "",
) -> MemoryDecision:
    """这条内容该直接生效，还是先进候选。

    三条规则，按"用户能不能自己发现它"排序：

    1. **推断出来的进候选，哪怕不敏感。** 模型从行为里推断的偏好可能是错的，而写进记忆之后它
       会一路影响后面的回答——用户既看不到它、也没有机会纠正它。
    2. **敏感内容进候选。** 成绩、评语、联系方式，泄漏一次收不回来。
    3. **内容里出现的敏感特征也进候选**，不管调用方标了什么。调用方忘记标是最常见的失败方式，
       而规则不该依赖它记得。
    """
    if inferred:
        return MemoryDecision(
            status=MemoryStatus.CANDIDATE,
            reason="这条是从行为里推断出来的，可能不对——先让用户确认",
        )
    if sensitivity is not MemorySensitivity.PUBLIC:
        return MemoryDecision(
            status=MemoryStatus.CANDIDATE,
            reason=f"内容是{sensitivity.value}级别，需要用户确认后才能被想起来",
        )
    matched = _sensitive_feature(text)
    if matched is not None:
        return MemoryDecision(
            status=MemoryStatus.CANDIDATE,
            reason=f"内容里出现了{matched}，即使被标成公开也要先确认",
        )
    return MemoryDecision(
        status=MemoryStatus.ACTIVE,
        reason="用户直接说的、非敏感的事实",
    )


def _sensitive_feature(text: str) -> str | None:
    for label, pattern in _ALWAYS_SENSITIVE:
        if pattern.search(str(text or "")):
            return label
    return None


def effective_sensitivity(text: str, requested: MemorySensitivity) -> MemorySensitivity:
    """A caller's public label cannot downgrade detected private evidence."""
    feature = _sensitive_feature(text)
    if feature in {'grade', 'contact', 'identifier'}:
        return MemorySensitivity.SENSITIVE
    if feature == 'weakness' and requested is MemorySensitivity.PUBLIC:
        return MemorySensitivity.PERSONAL
    return requested


def _parse(moment: str | None) -> datetime | None:
    if not moment:
        return None
    text = str(moment).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_expired(record: MemoryRecord, *, now: str) -> bool:
    """过期了没有。**没有 `expires_at` 表示永不过期**，不是"立即过期"。"""
    expires = _parse(record.expires_at)
    if expires is None:
        return False
    current = _parse(now) or datetime.now(timezone.utc)
    return current >= expires


def can_recall(record: MemoryRecord, *, scope: MemoryScope, now: str) -> bool:
    """这条记忆能不能出现在这个作用域的召回里。

    plan 要求"在每一次查询前后都检查"，而不是只靠记忆库的元数据过滤：过滤是库的行为，而我们
    自己的判断必须独立成立——否则换一个库、或者它的过滤有个 bug，跨课程的记忆就漏出去了。

    四个维度逐一比对，**顺序无关但一个都不能少**：少一个就是一个真实的越界路径。
    """
    if record.scope.status is not MemoryStatus.ACTIVE:
        return False
    if record.scope.group_id != scope.group_id:
        return False
    if record.scope.course_id != scope.course_id:
        return False
    if record.scope.sensitivity is not scope.sensitivity:
        return False
    return not is_expired(record, now=now)
