"""记忆的契约与门控：什么能直接写、什么必须先进候选、什么能被召回。

plan 第 79、80 行：

> Direct, non-sensitive facts may be written after deterministic checks. Inferred preferences,
> weak points, grades and sensitive facts enter an isolated Candidate namespace and cannot be
> recalled until approved.
>
> Enforce group/course/sensitivity/status scope before and after every Mem0 query; never rely
> solely on Mem0 metadata filters.

这一层**不依赖 Mem0**：它是"什么东西允许被记住、允许被想起来"的规则本身。把它写成纯逻辑有
两个好处——它可以被逐条钉住，而且换掉底层记忆库时这些规则不用重写。

两处容易搞错、也最容易造成真实伤害的地方：

* **"推断出来的"要单独判**。模型从行为里推断出的偏好可能是错的，而写进记忆之后它会一路影响
  后面的回答——用户既看不到它、也没有机会纠正它。
* **召回要在我们这一侧再判一次**。plan 说"never rely solely on Mem0 metadata filters"：
  过滤是库的行为，而我们自己的判断必须独立成立，否则换一个库、或者它的过滤有个 bug，
  跨课程的记忆就会漏出去。
"""

from __future__ import annotations

import pytest

from backend.knowe_memory import (
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

GROUP = "grp_" + "a" * 32
OTHER_GROUP = "grp_" + "b" * 32
COURSE = "course-math"
OTHER_COURSE = "course-physics"


def _scope(**overrides) -> MemoryScope:
    values = {
        "group_id": GROUP,
        "course_id": COURSE,
        "sensitivity": MemorySensitivity.PUBLIC,
        "status": MemoryStatus.ACTIVE,
    }
    values.update(overrides)
    return MemoryScope(**values)  # type: ignore[arg-type]


def _record(**overrides) -> MemoryRecord:
    values = {
        "memory_id": "mem_1",
        "scope": _scope(),
        "text": "二次函数的图像是抛物线",
        "source_document_id": "doc_1",
        "version": 1,
        "created_at": "2026-09-23T00:00:00Z",
        "expires_at": None,
    }
    values.update(overrides)
    return MemoryRecord(**values)  # type: ignore[arg-type]


# ── 写入门控 ──────────────────────────────────────────────────────────

def test_a_direct_non_sensitive_fact_may_be_written_immediately() -> None:
    """用户亲口说的、非敏感的事实可以直接记下。

    这正是"记忆有用"的那一半：把每一句"我周四有考试"都送去审批，用户第一次用就会关掉这个
    功能。
    """
    decision = classify_write(
        sensitivity=MemorySensitivity.PUBLIC, inferred=False,
    )

    assert decision.status is MemoryStatus.ACTIVE
    assert decision.needs_approval is False


@pytest.mark.parametrize("sensitivity", [
    MemorySensitivity.PERSONAL, MemorySensitivity.SENSITIVE,
])
def test_a_sensitive_fact_must_be_approved_first(sensitivity: MemorySensitivity) -> None:
    """个人与敏感内容先进候选，审批之后才能被想起来。"""
    decision = classify_write(sensitivity=sensitivity, inferred=False)

    assert decision.status is MemoryStatus.CANDIDATE
    assert decision.needs_approval is True


def test_an_inferred_fact_must_be_approved_even_when_it_is_not_sensitive() -> None:
    """**推断出来的东西要单独判**，哪怕它不敏感。

    模型从行为里推断出的偏好可能是错的，而写进记忆之后它会一路影响后面的回答——用户既看不到
    它、也没有机会纠正它。所以"推断"本身就是进候选的理由。
    """
    decision = classify_write(
        sensitivity=MemorySensitivity.PUBLIC, inferred=True,
    )

    assert decision.status is MemoryStatus.CANDIDATE
    assert decision.needs_approval is True


def test_a_grade_is_treated_as_sensitive_without_being_told_so() -> None:
    """成绩是 plan 点名的那一类，不该靠调用方记得把它标成 sensitive。

    调用方忘了标，成绩就会以"公开事实"的身份直接写进记忆——而它是最不该被随便想起来的东西
    之一。
    """
    decision = classify_write(
        sensitivity=MemorySensitivity.PUBLIC, inferred=False, text="这次数学考了 98 分",
    )

    assert decision.needs_approval is True
    assert decision.reason


def test_a_contact_detail_is_treated_as_sensitive() -> None:
    decision = classify_write(
        sensitivity=MemorySensitivity.PUBLIC, inferred=False,
        text="家长联系电话 13800138000",
    )

    assert decision.needs_approval is True


def test_the_decision_says_why() -> None:
    """门控要给出理由。

    界面上"这条要你确认一下"如果不说明为什么，用户只会机械地点同意——而那正是审批要防的事。
    """
    inferred = classify_write(sensitivity=MemorySensitivity.PUBLIC, inferred=True)
    graded = classify_write(
        sensitivity=MemorySensitivity.PUBLIC, inferred=False, text="期末成绩 92",
    )

    assert inferred.reason and graded.reason
    assert inferred.reason != graded.reason


# ── 召回门控 ──────────────────────────────────────────────────────────

def test_an_active_record_in_the_same_scope_can_be_recalled() -> None:
    assert can_recall(_record(), scope=_scope(), now="2026-09-23T01:00:00Z") is True


@pytest.mark.parametrize("field,other", [
    ("group_id", OTHER_GROUP),
    ("course_id", OTHER_COURSE),
])
def test_a_record_from_another_scope_cannot_be_recalled(field: str, other: str) -> None:
    """跨群组、跨课程都不能召回。

    plan 要求"在每一次查询前后都检查"，而不是只靠库的元数据过滤：过滤是库的行为，而我们自己
    的判断必须独立成立——否则换一个库、或者它的过滤有个 bug，别的课程的记忆就漏出去了。
    """
    assert can_recall(
        _record(), scope=_scope(**{field: other}), now="2026-09-23T01:00:00Z",
    ) is False


def test_a_candidate_cannot_be_recalled() -> None:
    """候选**不可被召回**——这正是 plan 第 79 行那句"cannot be recalled until approved"。

    候选能召回的话，"先进候选"就只是一个标签：信息照样影响回答，而用户从没同意过。
    """
    candidate = _record(scope=_scope(status=MemoryStatus.CANDIDATE))

    assert can_recall(candidate, scope=_scope(), now="2026-09-23T01:00:00Z") is False


@pytest.mark.parametrize("status", [
    MemoryStatus.REJECTED, MemoryStatus.EXPIRED, MemoryStatus.DELETED,
])
def test_a_non_active_record_cannot_be_recalled(status: MemoryStatus) -> None:
    assert can_recall(
        _record(scope=_scope(status=status)), scope=_scope(), now="2026-09-23T01:00:00Z",
    ) is False


def test_a_recall_scope_must_match_sensitivity_too() -> None:
    """敏感度也是作用域的一维：一次"只要公开内容"的召回不该带回个人记忆。"""
    personal = _record(scope=_scope(sensitivity=MemorySensitivity.PERSONAL))

    assert can_recall(
        personal, scope=_scope(sensitivity=MemorySensitivity.PUBLIC),
        now="2026-09-23T01:00:00Z",
    ) is False


def test_an_expired_record_cannot_be_recalled() -> None:
    expired = _record(expires_at="2026-09-22T00:00:00Z")

    assert can_recall(expired, scope=_scope(), now="2026-09-23T01:00:00Z") is False
    assert can_recall(
        expired, scope=_scope(), now="2026-09-21T00:00:00Z",
    ) is True


def test_is_expired_distinguishes_a_record_with_no_expiry() -> None:
    """没有 expires_at 表示**永不过期**，不是"立即过期"。"""
    assert is_expired(_record(expires_at=None), now="2030-01-01T00:00:00Z") is False
    assert is_expired(
        _record(expires_at="2026-09-22T00:00:00Z"), now="2026-09-23T00:00:00Z",
    ) is True


# ── 墓碑：挡住"重新学回来" ────────────────────────────────────────────

def test_a_tombstone_blocks_the_same_content_from_being_learned_again() -> None:
    """删掉之后，同样的内容不该被重新学回来。

    plan 第 81 行的 "prevent relearning"。没有这一条，"删除"只在用户点它的那一刻有效：下一次
    同一份资料被导入（或者模型又推断出同一件事），那条记忆就回来了——而用户会以为它已经没了。
    """
    original = _record()
    tombstone = MemoryTombstone(
        memory_id=original.memory_id, scope=original.scope,
        fingerprint=memory_fingerprint(original.text), deleted_at="2026-09-23T02:00:00Z",
    )

    assert tombstone.blocks(text=original.text, scope=original.scope) is True
    assert tombstone.blocks(text="完全不同的内容", scope=original.scope) is False


def test_a_tombstone_does_not_block_content_in_another_scope() -> None:
    """墓碑也是作用域内的：同一句话在另一门课里出现，不该被这门课的删除挡住。"""
    tombstone = MemoryTombstone(
        memory_id="mem_1", scope=_scope(),
        fingerprint=memory_fingerprint("二次函数的图像是抛物线"),
        deleted_at="2026-09-23T02:00:00Z",
    )

    assert tombstone.blocks(
        text="二次函数的图像是抛物线", scope=_scope(course_id=OTHER_COURSE),
    ) is False


def test_the_fingerprint_is_stable_and_content_only() -> None:
    """指纹只看内容：同一句话在任何作用域里都得到同一个指纹，否则墓碑匹配不上。"""
    assert memory_fingerprint("同一段话") == memory_fingerprint("同一段话")
    assert memory_fingerprint("同一段话") != memory_fingerprint("另一段话")


# ── 契约的形状 ────────────────────────────────────────────────────────

def test_a_record_carries_its_source_so_a_memory_can_be_traced_back() -> None:
    """每条记忆要能说清它是从哪份资料来的。

    一条说不出来源的记忆，用户没法核对它是否属实——而"它记错了我说的话"是这类功能最常见的
    抱怨。
    """
    record = _record()

    assert record.source_document_id
    assert record.version >= 1
    assert record.created_at


def test_the_decision_type_is_frozen_and_self_contained() -> None:
    """`MemoryDecision` 要带着结论与理由，调用方不需要自己再判一遍。"""
    decision = classify_write(sensitivity=MemorySensitivity.PUBLIC, inferred=False)

    assert isinstance(decision, MemoryDecision)
    assert decision.status is MemoryStatus.ACTIVE
    assert isinstance(decision.reason, str)
