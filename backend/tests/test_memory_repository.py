"""记忆的加密仓库：版本、TTL、墓碑、防重学、级联删除，以及它们各自的反面。

plan 第 84、87 行：

> Store all durable memory in StudyCrew's encrypted `MemoryRepository`. Rebuild an ephemeral
> Mem0/USearch index from approved decrypted records with `infer=False` on startup.
>
> Version edits, enforce TTL, write tombstones, prevent relearning and cascade source/course/group
> deletion through records, candidates, indexes and caches.

文件分三组：

* **库文件本身能回答什么问题**——沿用第十次复审在知识库存储上用过的方法：不带主密钥直接读
  库文件。记忆比资料更该过这一关：它记的是"这个学生是谁、他哪里不行"，而这类短句的候选空间
  小到可以穷举。
* **plan 点名的五件事**是否各自成立：版本、TTL、墓碑、防重学、级联。
* **几条容易写反的规则**——被拒绝的推断不该再问第二遍，但用户亲口说的应当压过它；过期不是
  删除；删除之后不能靠"用户再说一次"绕回来。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest

from backend.knowe_memory import (
    MemoryScope,
    MemorySensitivity,
    MemoryStatus,
    MemoryTombstone,
    memory_fingerprint,
)
from backend.knowe_memory.repository import (
    MemoryRepository,
    MemoryRepositoryError,
)

GROUP = "grp_" + "a" * 32
OTHER_GROUP = "grp_" + "b" * 32
COURSE = "course-math"
OTHER_COURSE = "course-physics"

NOW = "2026-09-23T09:00:00Z"
SOON = "2026-09-23T09:30:00Z"
LATER = "2026-09-23T11:00:00Z"

FACT = "二次函数的图像是一条抛物线"
OTHER_FACT = "顶点坐标公式是 (-b/2a, (4ac-b²)/4a)"


def _key():
    from backend.knowe_rag import MasterKey

    return MasterKey(key_material=os.urandom(32))


def _open(path: Path, *, key=None) -> MemoryRepository:
    return MemoryRepository(path, master_key=key or _key())


def _write(
    repository: MemoryRepository,
    text: str,
    *,
    group_id: str = GROUP,
    course_id: str = COURSE,
    sensitivity: MemorySensitivity = MemorySensitivity.PUBLIC,
    inferred: bool = False,
    source: str = "doc_1",
    ttl_seconds: int | None = None,
    now: str = NOW,
):
    return repository.write(
        text=text,
        group_id=group_id,
        course_id=course_id,
        sensitivity=sensitivity,
        inferred=inferred,
        source_document_id=source,
        ttl_seconds=ttl_seconds,
        now=now,
    )


def _recall(
    repository: MemoryRepository,
    *,
    group_id: str = GROUP,
    course_id: str = COURSE,
    sensitivity: MemorySensitivity = MemorySensitivity.PUBLIC,
    now: str = NOW,
    limit: int | None = None,
):
    return repository.recall(
        group_id=group_id,
        course_id=course_id,
        sensitivity=sensitivity,
        now=now,
        limit=limit,
    )


def _on_disk(path: Path) -> bytes:
    """库文件 + WAL + 共享内存。攻击者拿到的是整个目录，不是一个文件。

    只读主库会让"明文没落盘"这类断言**空过**：WAL 里躺着的东西主文件里当然看不到。
    """
    parts = [path.read_bytes()]
    for suffix in ("-wal", "-shm"):
        companion = path.with_name(path.name + suffix)
        if companion.exists():
            parts.append(companion.read_bytes())
    return b"".join(parts)


def _columns(path: Path, sql: str) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


# ══════════════════════════════════════════════════════════════════════
# 库文件本身能回答什么问题
# ══════════════════════════════════════════════════════════════════════

def test_the_memory_text_is_not_in_the_file_without_a_key(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        record = _write(repository, FACT).stored
    assert record is not None

    assert FACT.encode("utf-8") not in _on_disk(path)


def test_the_scope_and_source_identifiers_are_not_in_the_file_either(
    tmp_path: Path,
) -> None:
    """召回要按 group/course 过滤，级联要按来源删——但**过滤不等于明文存储**。

    作用域 id 是可以照算的：调用方手上就有它。所以这里存摘要（绑密钥）而不是原文，
    过滤能力一点没少，而库文件里不会出现"哪个班、哪门课"。
    """
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        _write(repository, FACT, source="doc_初二3班成绩单")

    blob = _on_disk(path)
    assert GROUP.encode("utf-8") not in blob
    assert COURSE.encode("utf-8") not in blob
    assert "doc_初二3班成绩单".encode("utf-8") not in blob


def test_a_deleted_sentence_cannot_be_confirmed_by_hashing_it(tmp_path: Path) -> None:
    """墓碑要挡住重学，但它自己不能变成一台免费的确认机。

    第十次复审在知识库存储上做过的演示：无盐 `sha256(明文)` 落盘之后，短句可以穷举，
    已知文档可以逐句验证"在不在库里"。墓碑恰恰存的就是"某句被删掉的话"的指纹——
    如果那是无盐哈希，删除反而把这句话的可验证副本留了下来。
    """
    path = tmp_path / "memory.db"
    secret = "张三的数学考了 92 分"
    with _open(path) as repository:
        record = _write(
            repository, secret, sensitivity=MemorySensitivity.SENSITIVE,
        ).stored
        assert record is not None
        repository.delete(memory_id=record.memory_id, now=SOON)
        repository.checkpoint()

    blob = _on_disk(path)
    unsalted = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    assert unsalted.encode("ascii") not in blob
    assert memory_fingerprint(secret).encode("ascii") not in blob


def test_deleting_then_checkpointing_leaves_no_copy_of_the_text(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        record = _write(repository, FACT).stored
        assert record is not None
        repository.delete(memory_id=record.memory_id, now=SOON)
        repository.checkpoint()

    # 密文本身也应当不在了：删除是删行，不是把行标成"已删除"留着。
    assert _columns(path, "SELECT COUNT(*) FROM memory_records")[0][0] == 0


def test_a_different_master_key_refuses_rather_than_returning_nothing(
    tmp_path: Path,
) -> None:
    """换了钥匙要**报错**，不能安静地返回空。

    返回空的表现是"记忆都在，只是都想不起来了"——用户看到的是功能坏了，而不是数据没了。
    与知识库存储同一套做法：开库就判，判不过就不打开（那里的教训是"读报错、写静默接管"，
    见 `reviews/REVIEW-8a77adb-t3-rereview.md` 的 Important-1）。
    """
    path = tmp_path / "memory.db"
    key = _key()
    with _open(path, key=key) as repository:
        record = _write(repository, FACT).stored
    assert record is not None

    with pytest.raises(MemoryRepositoryError):
        MemoryRepository(path, master_key=_key())

    # 关键的另一半：拒绝之后，原来那把钥匙还读得回来。
    with _open(path, key=key) as repository:
        loaded = repository.record(memory_id=record.memory_id)
    assert loaded is not None and loaded.text == FACT


def test_a_ciphertext_cannot_be_moved_to_another_record(tmp_path: Path) -> None:
    """AAD 绑到"哪一行的哪个字段"，所以对调两行的密文会被拒。

    没有这层绑定，能写数据库的人把两条记忆的正文对调，两边都还能正常解开——而用户永远
    不会发现"记的是对的、挂在了错的门课上"。
    """
    path = tmp_path / "memory.db"
    key = _key()
    with _open(path, key=key) as repository:
        first = _write(repository, FACT).stored
        second = _write(repository, OTHER_FACT).stored
    assert first is not None and second is not None

    connection = sqlite3.connect(path)
    try:
        rows = dict(connection.execute("SELECT memory_id, text FROM memory_records"))
        assert len(rows) == 2, "这条测试需要两行，否则它什么都没测"
        connection.execute(
            "UPDATE memory_records SET text=? WHERE memory_id=?",
            (rows[second.memory_id], first.memory_id),
        )
        connection.execute(
            "UPDATE memory_records SET text=? WHERE memory_id=?",
            (rows[first.memory_id], second.memory_id),
        )
        connection.commit()
    finally:
        connection.close()

    with _open(path, key=key) as repository:
        with pytest.raises(MemoryRepositoryError):
            repository.record(memory_id=first.memory_id)


# ══════════════════════════════════════════════════════════════════════
# 写入：什么直接生效、什么先进候选
# ══════════════════════════════════════════════════════════════════════

def test_a_direct_fact_is_recallable_immediately(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        outcome = _write(repository, FACT)
        assert outcome.created is True
        assert outcome.blocked is False
        assert outcome.stored is not None
        assert outcome.stored.scope.status is MemoryStatus.ACTIVE

        recalled = _recall(repository)
        assert [record.text for record in recalled] == [FACT]


def test_an_inferred_preference_is_only_a_candidate(tmp_path: Path) -> None:
    """模型推断出来的偏好可能是错的，而写进记忆之后会一路影响后面的回答。"""
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        outcome = _write(repository, "这个学生喜欢用图像法解题", inferred=True)
        assert outcome.stored is not None
        assert outcome.stored.scope.status is MemoryStatus.CANDIDATE

        assert _recall(repository) == ()
        assert [
            record.text for record in repository.candidates(group_id=GROUP, course_id=COURSE)
        ] == ["这个学生喜欢用图像法解题"]


def test_a_grade_in_the_text_is_a_candidate_even_when_labelled_public(
    tmp_path: Path,
) -> None:
    """调用方忘记标注是最常见的失败方式，规则不该依赖它记得。"""
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        outcome = _write(repository, "这次数学测验考了 92 分")
        assert outcome.stored is not None
        assert outcome.stored.scope.status is MemoryStatus.CANDIDATE
        assert outcome.decision.reason
        assert _recall(repository) == ()


def test_the_same_text_twice_is_one_record_with_one_version(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        first = _write(repository, FACT)
        second = _write(repository, FACT, now=SOON)

    assert first.stored is not None and second.stored is not None
    assert second.created is False
    assert second.stored.memory_id == first.stored.memory_id
    assert second.stored.version == 1
    assert len(_columns(path, "SELECT memory_id FROM memory_records")) == 1


def test_two_different_facts_in_one_scope_are_two_records(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        _write(repository, FACT)
        _write(repository, OTHER_FACT)
        assert len(_recall(repository)) == 2


def test_relabelling_the_same_text_as_sensitive_tightens_the_stored_copy(
    tmp_path: Path,
) -> None:
    """同一句话先被当成公开事实、后来才认出是敏感内容：**收紧必须写回原来那一行**。

    去重键里不含敏感度（同一句话不该因为标签不同存成两份），所以升级只能落在已有行上。
    不这么做的话，"先存的那一份"会继续以公开身份被召回，而调用方以为自己已经把它收成了
    候选——这类"以为关掉了"的漏洞正是 plan 第 85 行要防的东西。
    """
    path = tmp_path / "memory.db"
    tightened_key = _key()
    with _open(path, key=tightened_key) as repository:
        _write(repository, FACT)
        assert [record.text for record in _recall(repository)] == [FACT]

        tightened = _write(repository, FACT, sensitivity=MemorySensitivity.SENSITIVE, now=SOON)

    assert tightened.stored is not None
    assert tightened.stored.scope.sensitivity is MemorySensitivity.SENSITIVE
    assert tightened.stored.scope.status is MemoryStatus.CANDIDATE
    assert len(_columns(path, "SELECT memory_id FROM memory_records")) == 1

    # 用同一把钥匙重新打开（`_open` 不传 key 会生成新钥匙，所以这里必须把库建在同一进程里
    # 的那个 key 上——这也是上面那个 with 结束后还要再确认一次的原因）。
    with _open(path, key=tightened_key) as repository:
        assert _recall(repository) == (), "收紧之后它不该还能以公开身份被想起来"


# ══════════════════════════════════════════════════════════════════════
# 召回：作用域在查之前和查之后各判一次
# ══════════════════════════════════════════════════════════════════════

def test_recall_is_filtered_by_all_four_dimensions(tmp_path: Path) -> None:
    """plan 第 86 行：group/course/sensitivity/status 四个维度一个都不能少。

    少一个就是一条真实的越界路径，所以四个方向各写一条反例。
    """
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        _write(repository, FACT)
        _write(repository, OTHER_FACT, course_id=OTHER_COURSE)
        _write(repository, "在别的班的课上说的", group_id=OTHER_GROUP)
        # 非公开内容一律先进候选（`classify_write` 的规则），所以要**先批准**它才谈得上召回
        # ——否则这条测试证明的是"候选不会被召回"，而不是"敏感度是一维过滤条件"。
        personal = _write(
            repository,
            "他自己说的一个私人偏好",
            sensitivity=MemorySensitivity.PERSONAL,
        ).stored
        assert personal is not None
        repository.approve(memory_id=personal.memory_id, now=SOON)
        _write(repository, "推断出来的东西", inferred=True)

        assert [record.text for record in _recall(repository)] == [FACT]
        assert [
            record.text
            for record in _recall(repository, sensitivity=MemorySensitivity.PERSONAL)
        ] == ["他自己说的一个私人偏好"]
        assert [
            record.text
            for record in _recall(repository, course_id=OTHER_COURSE)
        ] == [OTHER_FACT]
        assert _recall(repository, group_id=OTHER_GROUP) != ()


def test_recall_drops_an_expired_record_without_sweeping_first(tmp_path: Path) -> None:
    """过期判定在**读出来之后**还要再判一次，不能只靠 SQL 的 where。

    这一条盯的是 plan 那句"before and after every query"：SQL 过滤的是列，而过期是一条
    规则（没有 `expires_at` 表示永不过期、时间戳读不出来时怎么办）。同一个规则写两遍就会
    漂移，所以库里那一行仍是 ACTIVE，而它出不出现由 `can_recall` 说了算。
    """
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        _write(repository, FACT, ttl_seconds=3600)

        assert [record.text for record in _recall(repository, now=NOW)] == [FACT]
        assert _recall(repository, now=LATER) == ()
        assert _columns(
            path, "SELECT status FROM memory_records"
        )[0][0] == MemoryStatus.ACTIVE.value, "没有 sweep 之前它仍然是 ACTIVE"


def test_recall_is_newest_first_and_refuses_a_nonsense_limit(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        _write(repository, FACT, now="2026-09-23T09:00:00Z")
        _write(repository, OTHER_FACT, now="2026-09-23T09:01:00Z")
        _write(repository, "第三条", now="2026-09-23T09:02:00Z")

        assert [record.text for record in _recall(repository, limit=2)] == [
            "第三条", OTHER_FACT,
        ]
        with pytest.raises(MemoryRepositoryError):
            _recall(repository, limit=0)


# ══════════════════════════════════════════════════════════════════════
# TTL
# ══════════════════════════════════════════════════════════════════════

def test_a_memory_without_a_ttl_never_expires(tmp_path: Path) -> None:
    """没有 `expires_at` 表示**永不过期**，不是"立即过期"。"""
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        _write(repository, FACT, ttl_seconds=None)
        assert _recall(repository, now="2099-01-01T00:00:00Z") != ()


def test_sweeping_marks_expired_rows_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        _write(repository, FACT, ttl_seconds=60)
        _write(repository, OTHER_FACT)

        assert repository.sweep_expired(now=NOW) == 0, "还没到期就不该动它"
        assert repository.sweep_expired(now=LATER) == 1
        assert repository.sweep_expired(now=LATER) == 0, "第二次没有可扫的了"

    statuses = dict(
        _columns(path, "SELECT memory_id, status FROM memory_records")
    )
    assert sorted(statuses.values()) == [MemoryStatus.ACTIVE.value, MemoryStatus.EXPIRED.value]


def test_an_expired_memory_comes_back_when_it_is_stated_again(tmp_path: Path) -> None:
    """过期是"这条不再自动想起来"，不是"这句话被禁止了"。"""
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        first = _write(repository, FACT, ttl_seconds=60)
        repository.sweep_expired(now=LATER)
        assert _recall(repository, now=LATER) == ()

        again = _write(repository, FACT, ttl_seconds=None, now=LATER)

    assert first.stored is not None and again.stored is not None
    assert again.stored.memory_id == first.stored.memory_id, "还是同一条，不是又插一行"
    assert again.stored.version == 2
    assert again.stored.scope.status is MemoryStatus.ACTIVE
    assert again.stored.expires_at is None


# ══════════════════════════════════════════════════════════════════════
# 审批
# ══════════════════════════════════════════════════════════════════════

def test_approving_a_candidate_makes_it_recallable(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        candidate = _write(repository, "他不擅长因式分解", inferred=True).stored
        assert candidate is not None
        assert _recall(repository) == ()

        approved = repository.approve(memory_id=candidate.memory_id, now=SOON)

        assert approved.scope.status is MemoryStatus.ACTIVE
        assert approved.version == candidate.version + 1
        assert _recall(repository) == ()
        assert [record.text for record in _recall(repository, sensitivity=MemorySensitivity.PERSONAL)] == ["他不擅长因式分解"]


def test_a_rejected_candidate_is_not_asked_about_twice(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        candidate = _write(repository, "他不擅长因式分解", inferred=True).stored
        assert candidate is not None
        repository.reject(memory_id=candidate.memory_id, now=SOON)

        again = _write(repository, "他不擅长因式分解", inferred=True, now=LATER)

        assert again.created is False
        assert again.stored is not None
        assert again.stored.scope.status is MemoryStatus.REJECTED
        assert repository.candidates(group_id=GROUP, course_id=COURSE) == ()
        assert _recall(repository) == ()


def test_a_rejected_inference_yields_to_the_user_saying_it_directly(
    tmp_path: Path,
) -> None:
    """用户亲口说的比"模型猜的、用户否掉的"更新。否掉的是那次推断，不是这句话。"""
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        candidate = _write(repository, "他喜欢用图像法解题", inferred=True).stored
        assert candidate is not None
        repository.reject(memory_id=candidate.memory_id, now=SOON)

        told = _write(repository, "他喜欢用图像法解题", inferred=False, now=LATER)

    assert told.stored is not None
    assert told.stored.memory_id == candidate.memory_id
    assert told.stored.scope.status is MemoryStatus.ACTIVE
    assert told.stored.version == candidate.version + 2


def test_approve_and_reject_refuse_a_record_that_is_not_a_candidate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        record = _write(repository, FACT).stored
        assert record is not None
        with pytest.raises(MemoryRepositoryError):
            repository.approve(memory_id=record.memory_id, now=SOON)
        with pytest.raises(MemoryRepositoryError):
            repository.reject(memory_id=record.memory_id, now=SOON)


def test_acting_on_a_memory_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        for action in ("approve", "reject", "delete", "revise"):
            with pytest.raises(MemoryRepositoryError):
                if action == "revise":
                    repository.revise(memory_id="mem_missing", text=FACT, now=SOON)
                else:
                    getattr(repository, action)(memory_id="mem_missing", now=SOON)


# ══════════════════════════════════════════════════════════════════════
# 版本
# ══════════════════════════════════════════════════════════════════════

def test_revising_bumps_the_version_and_changes_the_text(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        original = _write(repository, FACT).stored
        assert original is not None
        revised = repository.revise(
            memory_id=original.memory_id, text="二次函数的图像是一条抛物线（修订）", now=SOON,
        )

        assert revised.memory_id == original.memory_id
        assert revised.version == original.version + 1
        assert revised.created_at == original.created_at, "创建时间不该被改写"
        assert [record.text for record in _recall(repository)] == [
            "二次函数的图像是一条抛物线（修订）"
        ]

    # 旧正文不该留在库里：记忆是"现在相信什么"，不是一份保留历史的文档。
    assert FACT.encode("utf-8") not in _on_disk(path)


def test_revising_onto_a_deleted_sentence_is_refused(tmp_path: Path) -> None:
    """否则墓碑就有一条绕过路径：把别的记忆改成那句话。"""
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        doomed = _write(repository, "他上课总是走神").stored
        keeper = _write(repository, FACT).stored
        assert doomed is not None and keeper is not None
        repository.delete(memory_id=doomed.memory_id, now=SOON)

        with pytest.raises(MemoryRepositoryError):
            repository.revise(memory_id=keeper.memory_id, text="他上课总是走神", now=LATER)


# ══════════════════════════════════════════════════════════════════════
# 墓碑与防重学
# ══════════════════════════════════════════════════════════════════════

def test_deleting_leaves_a_tombstone_and_no_record(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        record = _write(repository, FACT).stored
        assert record is not None
        tombstone = repository.delete(memory_id=record.memory_id, now=SOON)

        assert isinstance(tombstone, MemoryTombstone)
        assert tombstone.memory_id == record.memory_id
        assert tombstone.deleted_at == SOON
        assert repository.record(memory_id=record.memory_id) is None
        assert _recall(repository) == ()
        assert len(repository.tombstones(group_id=GROUP, course_id=COURSE)) == 1


def test_a_deleted_sentence_cannot_be_learned_again(tmp_path: Path) -> None:
    """plan 第 87 行的 "prevent relearning"。

    没有墓碑的话，"删除"只在用户点它的那一刻有效：下一次同一份资料被导入，那句话就回来了。
    """
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        record = _write(repository, FACT).stored
        assert record is not None
        repository.delete(memory_id=record.memory_id, now=SOON)

        outcome = _write(repository, FACT, now=LATER)

        assert outcome.blocked is True
        assert outcome.created is False
        assert outcome.stored is None
        assert _recall(repository) == ()
        assert len(_columns(path, "SELECT memory_id FROM memory_records")) == 0


def test_a_deleted_sentence_cannot_be_stated_again_either(tmp_path: Path) -> None:
    """连"用户再说一次"也不放行——这是有意的不对称，不是漏掉的分支。

    重新导入同一份资料时抽取出来的事实走的也是非推断路径，所以如果只有推断写入被墓碑挡住，
    "删除"就挡不住最常见的那条回归路径。代价是解除删除需要一条单独的、面向用户的确认路径，
    而这条路径现在还不存在——它必须是产品决定，不能是一个悄悄放行的仓库方法。
    """
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        record = _write(repository, FACT).stored
        assert record is not None
        repository.delete(memory_id=record.memory_id, now=SOON)

        assert _write(repository, FACT, inferred=False, now=LATER).blocked is True


def test_a_tombstone_only_blocks_its_own_course(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        record = _write(repository, FACT).stored
        assert record is not None
        repository.delete(memory_id=record.memory_id, now=SOON)

        elsewhere = _write(repository, FACT, course_id=OTHER_COURSE, now=LATER)

    assert elsewhere.blocked is False
    assert elsewhere.stored is not None


def test_a_loaded_tombstone_still_matches_its_text(tmp_path: Path) -> None:
    """从库里读回来的墓碑必须与内存里造的**行为完全一样**。

    契约层用无盐指纹比对（`MemoryTombstone.blocks`）。如果仓库为了防确认机而只存一个绑定
    密钥的摘要，读回来的对象就带着一个永远匹配不上的字段——表现是"删除之后再写就被挡住了，
    但挡它的理由是一个死值"。所以落地时两者都存：绑定密钥的摘要用来查，指纹本身加密保存
    用来还原对象。
    """
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        record = _write(repository, FACT).stored
        assert record is not None
        repository.delete(memory_id=record.memory_id, now=SOON)
        loaded = repository.tombstones(group_id=GROUP, course_id=COURSE)[0]

    assert loaded.fingerprint == memory_fingerprint(FACT)
    assert loaded.blocks(text=FACT, scope=MemoryScope(
        group_id=GROUP,
        course_id=COURSE,
        sensitivity=MemorySensitivity.PUBLIC,
        status=MemoryStatus.ACTIVE,
    ))
    assert not loaded.blocks(text=OTHER_FACT, scope=MemoryScope(
        group_id=GROUP,
        course_id=COURSE,
        sensitivity=MemorySensitivity.PUBLIC,
        status=MemoryStatus.ACTIVE,
    ))


def test_is_blocked_answers_before_anything_is_written(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        assert repository.is_blocked(text=FACT, group_id=GROUP, course_id=COURSE) is False
        record = _write(repository, FACT).stored
        assert record is not None
        repository.delete(memory_id=record.memory_id, now=SOON)

        assert repository.is_blocked(text=FACT, group_id=GROUP, course_id=COURSE) is True
        assert repository.is_blocked(
            text=FACT, group_id=GROUP, course_id=OTHER_COURSE
        ) is False


# ══════════════════════════════════════════════════════════════════════
# 级联删除
# ══════════════════════════════════════════════════════════════════════

def test_cascade_by_course_removes_records_candidates_and_tombstones(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        kept = _write(repository, FACT).stored
        _write(repository, OTHER_FACT, course_id=OTHER_COURSE)
        _write(repository, "推断出来的", inferred=True)
        doomed = _write(repository, "这门课里被删过的一句").stored
        assert doomed is not None
        repository.delete(memory_id=doomed.memory_id, now=SOON)

        report = repository.cascade_delete(course_id=COURSE, now=LATER)

        assert report.records == 1
        assert report.candidates == 1
        assert report.tombstones == 1
        assert report.total == 3
        assert [record.text for record in _recall(repository, course_id=OTHER_COURSE)] == [
            OTHER_FACT
        ]
        assert kept is not None and repository.record(memory_id=kept.memory_id) is None
        assert repository.is_blocked(
            text="这门课里被删过的一句", group_id=GROUP, course_id=COURSE
        ) is False, "整门课都没了，它的墓碑也没有意义了"


def test_cascade_by_group_covers_every_course_in_it(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        _write(repository, FACT)
        _write(repository, OTHER_FACT, course_id=OTHER_COURSE)
        survivor = _write(repository, "别的班的事", group_id=OTHER_GROUP).stored

        report = repository.cascade_delete(group_id=GROUP, now=LATER)

        assert report.total == 2
        assert survivor is not None
        assert repository.record(memory_id=survivor.memory_id) is not None


def test_cascade_by_source_keeps_tombstones(tmp_path: Path) -> None:
    """删掉一份资料 ≠ 解除用户删过的那些话。

    来源级联回答的是"这份文件不再是我们记忆的依据"；而"这句话我不要你记着"是用户对内容本身
    的决定，它不该因为换了一份资料就被撤销。
    """
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        doomed = _write(repository, "从这份资料里学的第一句", source="doc_A").stored
        assert doomed is not None
        repository.delete(memory_id=doomed.memory_id, now=SOON)
        _write(repository, "从这份资料里学的第二句", source="doc_A")
        kept = _write(repository, "从另一份资料里学的", source="doc_B").stored

        report = repository.cascade_delete(source_document_id="doc_A", now=LATER)

        assert report.records == 1
        assert report.tombstones == 0
        assert repository.is_blocked(
            text="从这份资料里学的第一句", group_id=GROUP, course_id=COURSE
        ) is True
        assert kept is not None
        assert repository.record(memory_id=kept.memory_id) is not None


def test_cascade_without_a_selector_is_refused(tmp_path: Path) -> None:
    """没有选择器的级联就是"删掉全部"，而调用方多半是漏传了参数。"""
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        _write(repository, FACT)
        with pytest.raises(MemoryRepositoryError):
            repository.cascade_delete(now=LATER)
        assert _recall(repository) != ()


def test_cascade_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        _write(repository, FACT)
        assert repository.cascade_delete(course_id=COURSE, now=LATER).total == 1
        assert repository.cascade_delete(course_id=COURSE, now=LATER).total == 0


# ══════════════════════════════════════════════════════════════════════
# 落盘格式：明文列到底留下了什么
# ══════════════════════════════════════════════════════════════════════

def test_the_plaintext_columns_hold_no_content(tmp_path: Path) -> None:
    """逐列点名：这四类列是明文的，它们各自只留"不透明的东西"。

    这条测试的作用不是"证明安全"，而是让**下一列明文**必须显式地加进这个清单里——
    第十次复审在知识库存储上找到的两条泄漏，都是"多加了一列没人重读的明文"。
    """
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        _write(repository, FACT, source="doc_初二3班成绩单")

    columns = {
        str(row[1]) for row in _columns(path, "PRAGMA table_info(memory_records)")
    }
    assert columns == {
        "memory_id", "group_digest", "course_digest", "content_digest",
        "source_digest", "sensitivity", "status", "version", "created_at",
        "updated_at", "expires_at", "scope_blob", "source_blob", "text",
    }

    blob = _on_disk(path)
    row = _columns(
        path,
        "SELECT group_digest, course_digest, content_digest, source_digest, scope_blob,"
        " source_blob, text FROM memory_records",
    )[0]
    for value in row[:4]:
        assert isinstance(value, str) and len(value) == 64, "摘要都是 64 位十六进制"
    for value in row[4:]:
        assert isinstance(value, bytes) and value, "内容是密文"

    for plaintext in (FACT, "doc_初二3班成绩单", COURSE, GROUP):
        assert plaintext.encode("utf-8") not in blob


def test_the_two_digest_kinds_are_not_interchangeable(tmp_path: Path) -> None:
    """绑定密钥的摘要 ≠ 无盐指纹：两者用途不同，绝不能互换。"""
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        _write(repository, FACT)

    stored = str(
        _columns(path, "SELECT content_digest FROM memory_records")[0][0]
    )
    assert stored != memory_fingerprint(FACT)


def test_the_scope_blob_round_trips_the_original_identifiers(tmp_path: Path) -> None:
    """摘要用来查，密文用来还原——所以读回来的记录里必须还是原来那个班、那门课。"""
    path = tmp_path / "memory.db"
    key = _key()
    with _open(path, key=key) as repository:
        record = _write(repository, FACT, source="doc_初二3班成绩单").stored
    assert record is not None

    with _open(path, key=key) as repository:
        loaded = repository.record(memory_id=record.memory_id)

    assert loaded is not None
    assert loaded.scope.group_id == GROUP
    assert loaded.scope.course_id == COURSE
    assert loaded.source_document_id == "doc_初二3班成绩单"
    assert loaded.text == FACT


def test_a_memory_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    key = _key()
    with _open(path, key=key) as repository:
        record = _write(repository, FACT, ttl_seconds=3600).stored
    assert record is not None

    with _open(path, key=key) as repository:
        recalled = _recall(repository)

    assert [item.text for item in recalled] == [FACT]
    assert recalled[0].memory_id == record.memory_id
    assert recalled[0].expires_at == record.expires_at
    assert recalled[0].scope.sensitivity is MemorySensitivity.PUBLIC


def test_the_scope_blob_is_not_a_plaintext_json_pair(tmp_path: Path) -> None:
    """作用域密文不能是"看得见的 JSON"——那等于没加密，只是换了个列名。"""
    path = tmp_path / "memory.db"
    with _open(path) as repository:
        _write(repository, FACT)

    blob = bytes(
        _columns(path, "SELECT scope_blob FROM memory_records")[0][0]
    )
    assert json.dumps([GROUP, COURSE], ensure_ascii=False).encode("utf-8") not in blob
    assert GROUP.encode("utf-8") not in blob
