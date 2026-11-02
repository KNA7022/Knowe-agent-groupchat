"""记忆的加密仓库：版本、TTL、墓碑、防重学、级联删除。

plan 第 84、87 行：

> Store all durable memory in StudyCrew's encrypted `MemoryRepository`. ...
> Version edits, enforce TTL, write tombstones, prevent relearning and cascade source/course/group
> deletion through records, candidates, indexes and caches.

**这一层同样不依赖 Mem0。** 记忆存在哪里、什么时候过期、删掉之后怎么防止被重新学回来，
是产品规则；换成别的记忆库时它们不该重写。Mem0（如果最终引入）只做语义与情景抽取、去重和
语义召回，召回**之前**的作用域过滤由这一层自己判——plan 第 86 行明确要求"不能只依赖记忆库的
元数据过滤"，那就意味着这条判断必须存在于我们这一侧。

**明文列留下了什么。** 沿用第十次复审在知识库存储上定下的做法：先说清楚，再写代码。

- `memory_id` —— 随机生成，不含内容。
- `group_digest`/`course_digest`/`content_digest`/`source_digest` —— **绑定主密钥的 HMAC**
  （`_keyed_digest`，按用途做了域分隔）。作用域 id 与来源 id 都是调用方手上的值、照着算得出来，
  所以过滤能力一点没少，而库文件里不会出现"哪个班、哪门课、哪份资料"。
  正文摘要绑密钥的理由与知识库那边相同：这个产品的记忆句子通常很短（"他不擅长因式分解"、
  "数学考了 92 分"），无盐哈希的候选空间小到可以穷举，库文件会变成一台免费的确认机。
- `sensitivity`/`status`/`version` —— 枚举与计数器。它泄漏"这个范围里有多少条敏感记忆、
  其中多少条还在等确认"，但泄漏不了它们是什么。
- `created_at`/`updated_at`/`expires_at` —— 什么时候说的、什么时候改的、什么时候过期。
  与知识库那边同一条取舍：合起来能还原"谁在什么时候学了什么科目"，这是**被记录的取舍**。
- `scope_blob`/`source_blob`/`text` —— 密文；作用域与来源也加密保存，因为召回要能还原出记录
  的原始归属，而摘要不可逆。

**墓碑存两样东西，这不是冗余。** `content_digest`（绑密钥）用来查"这句话被删过吗"，是索引；
`fingerprint`（无盐 sha256，**加密保存**）用来还原一个 `MemoryTombstone` 对象，好让
`MemoryTombstone.blocks()` 在从库里读回来的对象上和内存里造的对象行为完全一致。只存前者的话，
读回来的对象带着一个永远匹配不上的字段——表现是"删除之后确实写不进去了，但挡它的理由是
一个死值"。无盐指纹**不能明文落盘**：那样删除反而把这句话的可验证副本留了下来。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import sys
import uuid

from knowe_rag.crypto import MasterKey, MasterKeyError, field_aad
from knowe_storage._sqlite import SQLiteDatabase

from .contracts import (
    MemoryDecision,
    MemoryRecord,
    MemoryScope,
    MemorySensitivity,
    MemoryStatus,
    MemoryTombstone,
    can_recall,
    classify_write,
    effective_sensitivity,
    is_expired,
    memory_fingerprint,
)

__all__ = [
    "MemoryCascadeReport",
    "MemoryRepository",
    "MemoryRepositoryError",
    "MemoryWriteOutcome",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_meta (
    name TEXT PRIMARY KEY,
    value BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_records (
    memory_id TEXT PRIMARY KEY,
    group_digest TEXT NOT NULL,
    course_digest TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT,
    scope_blob BLOB NOT NULL,
    source_blob BLOB NOT NULL,
    text BLOB NOT NULL
);

-- 去重键里没有敏感度：同一句话不该因为标签不同存成两份。认出它更敏感时就地收紧那一行。
CREATE UNIQUE INDEX IF NOT EXISTS memory_records_by_content
    ON memory_records(group_digest, course_digest, content_digest);
CREATE INDEX IF NOT EXISTS memory_records_by_scope
    ON memory_records(group_digest, course_digest, status);
CREATE INDEX IF NOT EXISTS memory_records_by_source ON memory_records(source_digest);

CREATE TABLE IF NOT EXISTS memory_tombstones (
    memory_id TEXT PRIMARY KEY,
    group_digest TEXT NOT NULL,
    course_digest TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    scope_blob BLOB NOT NULL,
    fingerprint BLOB NOT NULL
);

CREATE INDEX IF NOT EXISTS memory_tombstones_by_content
    ON memory_tombstones(group_digest, course_digest, content_digest);
CREATE INDEX IF NOT EXISTS memory_tombstones_by_group ON memory_tombstones(group_digest);
"""

#: 主密钥的自证记录。与知识库存储同一套做法，理由见那边：没有它的时候，"读报错、写静默接管"
#: 会同时成立，而记忆这一侧的静默接管意味着把某人的偏好改写成谁都读不出来的密文。
_KEY_VERIFIER_NAME = "master_key_verifier"
_KEY_VERIFIER_PLAINTEXT = b"studycrew.memory.master-key-verifier.v1"

#: 敏感度从宽到严。只在一个地方用：同一句话被重新标注时**收紧**，不放松。
_SENSITIVITY_ORDER = (
    MemorySensitivity.PUBLIC,
    MemorySensitivity.PERSONAL,
    MemorySensitivity.SENSITIVE,
)


class MemoryRepositoryError(RuntimeError):
    """仓库拒绝这次操作：记录不存在、状态不对、内容被删过，或者钥匙不对。"""


def _master_key_error_types() -> tuple[type[BaseException], ...]:
    """`MasterKeyError` 的**每一种身份**。

    `backend/` 与仓库根同时挂在 PYTHONPATH 上（跑测试时必须如此），于是同一个 `crypto.py`
    会被导入成 `knowe_rag.crypto` 和 `backend.knowe_rag.crypto` **两个模块对象**，而它们的
    `MasterKeyError` 是**两个不同的类**。`except` 按类匹配，所以只写一个名字时，从另一半
    调用方传上来的错误会直接穿过去——表现是"换错钥匙时抛的是 MasterKeyError，不是
    MemoryRepositoryError"，而两个类在源码里看是同一个。

    实测：`py -3.12 -c "import backend.knowe_rag.crypto as a, knowe_rag.crypto as b; print(a is b)"`
    → `False`。

    这里**运行时**求值（`except _master_key_error_types()`），因为另一半可能在这之后才被导入；
    只认已经在 `sys.modules` 里的那些，不为一个异常类去把整个 RAG 包拉进来。
    """
    found: list[type[BaseException]] = []
    for module_name in ("knowe_rag.crypto", "backend.knowe_rag.crypto"):
        module = sys.modules.get(module_name)
        candidate = getattr(module, "MasterKeyError", None) if module is not None else None
        if isinstance(candidate, type) and candidate not in found:
            found.append(candidate)
    return tuple(found) or (MasterKeyError,)


@dataclass(frozen=True, slots=True)
class MemoryWriteOutcome:
    """一次写入尝试的结果。

    `stored` 为 `None` 表示这次没有落库，原因在 `decision.reason` 里；`blocked` 把其中
    "被墓碑挡下"这一种单独标出来——它与"参数有问题"不同，是规则正常工作的结果。
    """

    decision: MemoryDecision
    stored: MemoryRecord | None
    created: bool
    blocked: bool


@dataclass(frozen=True, slots=True)
class MemoryCascadeReport:
    """一次级联删除删掉了什么。

    ⚠ **这里的数字只是仓库这一半。** plan 第 87 行要求级联"穿透 records、candidates、indexes
    与 caches"，而索引与缓存是这一层之外的派生数据（重建自解密后的记录）。仓库删掉了权威
    数据，调用方必须据此丢弃并重建索引——**不重建的话，索引里还留着已经不存在的记忆**，
    而召回会照着索引把它们拿出来。这个报告就是给那一步用的输入。
    """

    group_id: str | None
    course_id: str | None
    source_document_id: str | None
    records: int
    candidates: int
    tombstones: int

    @property
    def total(self) -> int:
        return self.records + self.candidates + self.tombstones


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_moment(moment: str) -> datetime:
    text = str(moment).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MemoryRepositoryError(f"时间戳读不出来：{moment!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _keyed_digest(master_key: MasterKey, *, domain: str, value: str) -> str:
    """绑定主密钥的摘要，**按用途做域分隔**。

    这里不用 `knowe_rag.crypto.keyed_content_hasher`：那个函数刻意不做域分隔，因为它定义的是
    知识库那一侧的"内容同一性"（同一段文字在任何位置都必须得到同一个指纹）。而这一层用同一把
    钥匙摘要四种不同的东西（班、课、正文、来源），不分开的话，一个恰好等于某个课程 id 的正文
    会与那个课程的列摘要相同——列不同所以今天不会撞，但那是靠调用方碰不到才成立的。
    """
    material = f"{domain}\x00{value}".encode("utf-8")
    return hmac.new(master_key.key_material, material, hashlib.sha256).hexdigest()


def _pack_pair(first: str, second: str) -> bytes:
    """两个 id 一起封。JSON 而不是拼接：分隔符出现在值里时拼接会串位。"""
    return json.dumps([first, second], ensure_ascii=False).encode("utf-8")


def _unpack_pair(raw: bytes, *, what: str) -> tuple[str, str]:
    try:
        first, second = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, TypeError) as exc:
        raise MemoryRepositoryError(f"{what} 解出来的不是一对标识：{exc}") from exc
    return str(first), str(second)


class MemoryRepository:
    """一份加密的记忆库。用 `with` 打开，或在结束时显式 `close()`。"""

    def __init__(self, path: str | Path, *, master_key: MasterKey) -> None:
        self._key = master_key
        self.path = Path(path)
        self._database = SQLiteDatabase(self.path)
        self._database.initialize("PRAGMA secure_delete=ON;")
        self._database.initialize(_SCHEMA)
        self._verify_master_key()

    # ── 生命周期 ──────────────────────────────────────────────────────

    def close(self) -> None:
        self._database.close()

    def checkpoint(self) -> None:
        """把 WAL 合并回主库并截断它——删除之后靠它兑现"现在就没了"，而不是"迟早会没"。"""
        self._database.initialize("PRAGMA wal_checkpoint(TRUNCATE);")

    def __enter__(self) -> "MemoryRepository":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _verify_master_key(self) -> None:
        aad = field_aad("memory_meta", _KEY_VERIFIER_NAME)
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT value FROM memory_meta WHERE name=?", (_KEY_VERIFIER_NAME,),
            ).fetchone()
            if row is None:
                occupied = connection.execute(
                    "SELECT (SELECT COUNT(*) FROM memory_records)"
                    "     + (SELECT COUNT(*) FROM memory_tombstones) AS total"
                ).fetchone()["total"]
                if occupied:
                    raise MemoryRepositoryError(
                        "这份记忆库里已经有内容，却没有主密钥校验记录：它可能是别的钥匙建的。"
                        "这里不会替你认领——认错了会把里面的记忆改写成谁都读不出来的密文"
                    )
                connection.execute(
                    "INSERT INTO memory_meta(name, value) VALUES (?, ?)",
                    (_KEY_VERIFIER_NAME, self._key.seal(_KEY_VERIFIER_PLAINTEXT, aad=aad)),
                )
                return
        try:
            opened = self._key.open(row["value"], aad=aad)
        except _master_key_error_types() as exc:
            raise MemoryRepositoryError(
                "这份记忆库不属于当前主密钥（校验记录解不开）。拒绝打开：这里的每一条都记着"
                "这个学生是谁"
            ) from exc
        if opened != _KEY_VERIFIER_PLAINTEXT:
            raise MemoryRepositoryError("主密钥校验记录的内容不对：这份记忆库已经损坏或被替换过")

    # ── 加解密 ────────────────────────────────────────────────────────

    def _seal(self, memory_id: str, field: str, plaintext: bytes) -> bytes:
        return self._key.seal(plaintext, aad=field_aad(memory_id, field))

    def _open(self, memory_id: str, field: str, blob: bytes) -> bytes:
        try:
            return self._key.open(blob, aad=field_aad(memory_id, field))
        except _master_key_error_types() as exc:
            raise MemoryRepositoryError(
                f"{field} 读不出来（记录 {memory_id}）：{exc}"
            ) from exc

    def _digest(self, *, domain: str, value: str) -> str:
        return _keyed_digest(self._key, domain=domain, value=value)

    # ── 行 ↔ 记录 ─────────────────────────────────────────────────────

    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        memory_id = str(row["memory_id"])
        group_id, course_id = _unpack_pair(
            self._open(memory_id, "memory.record.scope", row["scope_blob"]), what="作用域",
        )
        source = self._open(memory_id, "memory.record.source", row["source_blob"])
        text = self._open(memory_id, "memory.record.text", row["text"])
        try:
            decoded = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MemoryRepositoryError(
                f"记忆 {memory_id} 的正文不是合法的 UTF-8：{exc}"
            ) from exc
        return MemoryRecord(
            memory_id=memory_id,
            scope=MemoryScope(
                group_id=group_id,
                course_id=course_id,
                sensitivity=MemorySensitivity(str(row["sensitivity"])),
                status=MemoryStatus(str(row["status"])),
            ),
            text=decoded,
            source_document_id=source.decode("utf-8"),
            version=int(row["version"]),
            created_at=str(row["created_at"]),
            expires_at=row["expires_at"],
        )

    _COLUMNS = (
        "memory_id, group_digest, course_digest, content_digest, source_digest,"
        " sensitivity, status, version, created_at, updated_at, expires_at,"
        " scope_blob, source_blob, text"
    )

    def _fetch(self, memory_id: str) -> sqlite3.Row | None:
        with self._database.transaction(immediate=False) as connection:
            return connection.execute(
                f"SELECT {self._COLUMNS} FROM memory_records WHERE memory_id=?",
                (memory_id,),
            ).fetchone()

    def _require(self, memory_id: str) -> sqlite3.Row:
        row = self._fetch(memory_id)
        if row is None:
            raise MemoryRepositoryError(f"没有这条记忆：{memory_id}")
        return row

    # ── 读 ────────────────────────────────────────────────────────────

    def record(self, *, memory_id: str) -> MemoryRecord | None:
        row = self._fetch(memory_id)
        return None if row is None else self._row_to_record(row)

    def recall(
        self,
        *,
        group_id: str,
        course_id: str,
        sensitivity: MemorySensitivity,
        now: str | None = None,
        limit: int | None = None,
    ) -> tuple[MemoryRecord, ...]:
        """这个作用域里现在能被想起来的记忆，最新的在前。

        **作用域判两次**（plan 第 86 行的 before and after）：SQL 先按摘要过滤，解密之后再让
        `can_recall` 逐维重判一次。这不是重复劳动——过期判定是一条规则（没有 `expires_at` 表示
        永不过期、时间戳读不出来时怎么办），把它抄进 SQL 就会有两份实现，而两份实现漂移的表现
        是"某条记忆有时候出得来、有时候出不来"。所以 SQL 只管列，规则只管 `can_recall`。

        参数是三个 id 而不是一个 `MemoryScope`：召回的作用域是"我要找什么"，而 `MemoryScope`
        是"这条记忆属于哪"，两者的 `status` 含义不同（前者的 status 在这里没有意义）。
        """
        if limit is not None and limit < 1:
            raise MemoryRepositoryError(f"limit 必须至少是 1，收到 {limit}")
        moment = now or _utc_now()
        where = MemoryScope(
            group_id=group_id, course_id=course_id,
            sensitivity=sensitivity, status=MemoryStatus.ACTIVE,
        )
        with self._database.transaction(immediate=False) as connection:
            rows = connection.execute(
                f"SELECT {self._COLUMNS} FROM memory_records"
                " WHERE group_digest=? AND course_digest=? AND sensitivity=? AND status=?"
                " ORDER BY created_at DESC, memory_id ASC",
                (
                    self._digest(domain="memory.group", value=group_id),
                    self._digest(domain="memory.course", value=course_id),
                    sensitivity.value,
                    MemoryStatus.ACTIVE.value,
                ),
            ).fetchall()
        records = tuple(
            record
            for record in (self._row_to_record(row) for row in rows)
            if can_recall(record, scope=where, now=moment)
        )
        return records if limit is None else records[:limit]

    def candidates(
        self, *, group_id: str, course_id: str, now: str | None = None,
    ) -> tuple[MemoryRecord, ...]:
        """等用户确认的那些。它们**不在召回里**，只有这条路径能把它们取出来。"""
        moment = now or _utc_now()
        with self._database.transaction(immediate=False) as connection:
            rows = connection.execute(
                f"SELECT {self._COLUMNS} FROM memory_records"
                " WHERE group_digest=? AND course_digest=? AND status=?"
                " ORDER BY created_at ASC, memory_id ASC",
                (
                    self._digest(domain="memory.group", value=group_id),
                    self._digest(domain="memory.course", value=course_id),
                    MemoryStatus.CANDIDATE.value,
                ),
            ).fetchall()
        return tuple(
            record
            for record in (self._row_to_record(row) for row in rows)
            if not is_expired(record, now=moment)
        )

    def tombstones(self, *, group_id: str, course_id: str) -> tuple[MemoryTombstone, ...]:
        with self._database.transaction(immediate=False) as connection:
            rows = connection.execute(
                "SELECT memory_id, deleted_at, scope_blob, fingerprint"
                "  FROM memory_tombstones WHERE group_digest=? AND course_digest=?"
                " ORDER BY deleted_at ASC, memory_id ASC",
                (
                    self._digest(domain="memory.group", value=group_id),
                    self._digest(domain="memory.course", value=course_id),
                ),
            ).fetchall()
        result: list[MemoryTombstone] = []
        for row in rows:
            memory_id = str(row["memory_id"])
            found_group, found_course = _unpack_pair(
                self._open(memory_id, "memory.tombstone.scope", row["scope_blob"]),
                what="墓碑作用域",
            )
            fingerprint = self._open(
                memory_id, "memory.tombstone.fingerprint", row["fingerprint"],
            ).decode("ascii")
            result.append(MemoryTombstone(
                memory_id=memory_id,
                scope=MemoryScope(
                    group_id=found_group,
                    course_id=found_course,
                    # 墓碑不记敏感度与状态（见 `MemoryTombstone.blocks`）：它只挡"同一门课里
                    # 同一句话"。这两个字段填什么都不会参与比对，所以填一个明确的常量，
                    # 而不是从行里读一个不存在的列。
                    sensitivity=MemorySensitivity.PUBLIC,
                    status=MemoryStatus.DELETED,
                ),
                fingerprint=fingerprint,
                deleted_at=str(row["deleted_at"]),
            ))
        return tuple(result)

    def is_blocked(self, *, text: str, group_id: str, course_id: str) -> bool:
        """这句话在这个作用域里是不是被删过。

        按**绑定密钥的摘要**查，不按无盐指纹查：后者要给每一行解密，而它是明文落盘时才会变成
        确认机的东西。
        """
        with self._database.transaction(immediate=False) as connection:
            row = connection.execute(
                "SELECT 1 FROM memory_tombstones"
                " WHERE group_digest=? AND course_digest=? AND content_digest=? LIMIT 1",
                (
                    self._digest(domain="memory.group", value=group_id),
                    self._digest(domain="memory.course", value=course_id),
                    self._digest(domain="memory.content", value=text),
                ),
            ).fetchone()
        return row is not None

    # ── 写 ────────────────────────────────────────────────────────────

    def write(
        self,
        *,
        text: str,
        group_id: str,
        course_id: str,
        sensitivity: MemorySensitivity,
        inferred: bool,
        source_document_id: str,
        ttl_seconds: int | None = None,
        now: str | None = None,
    ) -> MemoryWriteOutcome:
        """把一条内容写进记忆，或者说明为什么没有写。

        `sensitivity` 与 `inferred` 是**必填**的：两者的默认值都会往危险的方向倒——默认
        "公开"会让敏感内容直接生效，默认"不是推断的"会让模型猜的东西绕过审批。少填一个参数
        就会立刻报错，比给一个看起来合理的默认值安全。

        顺序是有意的：**墓碑先判**，"删过就别再学回来"压过其它一切；然后才轮到去重与分类。
        """
        moment = now or _utc_now()
        sensitivity = effective_sensitivity(text, sensitivity)
        decision = classify_write(sensitivity=sensitivity, inferred=inferred, text=text)

        if self.is_blocked(text=text, group_id=group_id, course_id=course_id):
            return MemoryWriteOutcome(
                decision=MemoryDecision(
                    status=MemoryStatus.DELETED,
                    reason="这句话在这个作用域里被删除过；按「删掉就别再学回来」的规则，"
                           "这次写入被拒绝",
                ),
                stored=None,
                created=False,
                blocked=True,
            )

        group_digest = self._digest(domain="memory.group", value=group_id)
        course_digest = self._digest(domain="memory.course", value=course_id)
        content_digest = self._digest(domain="memory.content", value=text)
        existing = self._find(group_digest, course_digest, content_digest)
        if existing is not None:
            return self._write_existing(
                existing, decision=decision, requested=sensitivity,
                ttl_seconds=ttl_seconds, moment=moment,
            )

        memory_id = f"mem_{uuid.uuid4().hex}"
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO memory_records("
                "  memory_id, group_digest, course_digest, content_digest, source_digest,"
                "  sensitivity, status, version, created_at, updated_at, expires_at,"
                "  scope_blob, source_blob, text"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    memory_id,
                    group_digest,
                    course_digest,
                    content_digest,
                    self._digest(domain="memory.source", value=source_document_id),
                    sensitivity.value,
                    decision.status.value,
                    1,
                    moment,
                    moment,
                    self._expiry(ttl_seconds, moment),
                    self._seal(memory_id, "memory.record.scope", _pack_pair(group_id, course_id)),
                    self._seal(
                        memory_id, "memory.record.source", source_document_id.encode("utf-8"),
                    ),
                    self._seal(memory_id, "memory.record.text", text.encode("utf-8")),
                ),
            )
        row = self._require(memory_id)
        return MemoryWriteOutcome(
            decision=decision, stored=self._row_to_record(row), created=True, blocked=False,
        )

    def _find(
        self, group_digest: str, course_digest: str, content_digest: str,
    ) -> sqlite3.Row | None:
        with self._database.transaction(immediate=False) as connection:
            return connection.execute(
                f"SELECT {self._COLUMNS} FROM memory_records"
                " WHERE group_digest=? AND course_digest=? AND content_digest=?",
                (group_digest, course_digest, content_digest),
            ).fetchone()

    def _write_existing(
        self,
        row: sqlite3.Row,
        *,
        decision: MemoryDecision,
        requested: MemorySensitivity,
        ttl_seconds: int | None,
        moment: str,
    ) -> MemoryWriteOutcome:
        """同一句话又来了：它可能是重复、是收紧、是翻案，也可能只是不该再问第二遍。

        三条规则，都来自"更新的信息应当赢，但只有用户能推翻用户"：

        * **已生效或已在等确认** —— 按"只收紧"的规则处理；没有可收紧的地方就什么都不做。
          重复导入同一份资料是最常见的输入，每次都插一行会让记忆里堆满同一句话。
        * **上次被否掉** —— 再推断一次不该重新问（用户已经答过这个问题了），但用户**亲口说的**
          要能翻案：否掉的是那次推断，不是这句话。
        * **上次过期了** —— 过期是"不再自动想起来"，不是"这句话被禁止了"，所以重新说出来
          就该回来。
        """
        stored = self._row_to_record(row)
        status = stored.scope.status

        if status in (MemoryStatus.ACTIVE, MemoryStatus.CANDIDATE):
            tightened = self._tighten(stored, requested=requested, decision=decision)
            if tightened is not None:
                return tightened
            return MemoryWriteOutcome(
                decision=MemoryDecision(
                    status=status, reason="这条内容已经在这个作用域里了，没有变化",
                ),
                stored=stored,
                created=False,
                blocked=False,
            )

        if status is MemoryStatus.REJECTED and decision.status is MemoryStatus.CANDIDATE:
            return MemoryWriteOutcome(
                decision=MemoryDecision(
                    status=MemoryStatus.REJECTED,
                    reason="用户已经否掉过这条推断，不再重复询问",
                ),
                stored=stored,
                created=False,
                blocked=False,
            )

        memory_id = stored.memory_id
        current = stored.scope.sensitivity
        more_sensitive = _SENSITIVITY_ORDER.index(requested) > _SENSITIVITY_ORDER.index(current)
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE memory_records SET status=?, sensitivity=?, version=version+1,"
                " updated_at=?, expires_at=? WHERE memory_id=?",
                (
                    decision.status.value,
                    requested.value if more_sensitive else current.value,
                    moment,
                    self._expiry(ttl_seconds, moment),
                    memory_id,
                ),
            )
        return MemoryWriteOutcome(
            decision=decision,
            stored=self._row_to_record(self._require(memory_id)),
            created=False,
            blocked=False,
        )

    def _tighten(
        self,
        stored: MemoryRecord,
        *,
        requested: MemorySensitivity,
        decision: MemoryDecision,
    ) -> MemoryWriteOutcome | None:
        """同一句话被重新标注得**更严**时就地收紧；不需要收紧就返回 None。

        去重键里不含敏感度，所以"先以公开身份存下、后来才认出是敏感内容"只能改在原来那一行
        上。不改的话，先存的那一份会继续被召回，而调用方以为自己已经关掉了它。

        只收紧、不放松：一次标成公开的重复写入不该把已经认作敏感的记忆放出去。
        """
        current = stored.scope.sensitivity
        more_sensitive = _SENSITIVITY_ORDER.index(requested) > _SENSITIVITY_ORDER.index(current)
        needs_approval = (
            decision.status is MemoryStatus.CANDIDATE
            and stored.scope.status is MemoryStatus.ACTIVE
        )
        if not (more_sensitive or needs_approval):
            return None

        memory_id = stored.memory_id
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE memory_records SET sensitivity=?, status=?, updated_at=?"
                " WHERE memory_id=?",
                (
                    requested.value if more_sensitive else current.value,
                    MemoryStatus.CANDIDATE.value,
                    _utc_now(),
                    memory_id,
                ),
            )
        return MemoryWriteOutcome(
            decision=MemoryDecision(
                status=MemoryStatus.CANDIDATE,
                reason="这条内容已经在库里了，但这次它被标得更严（更敏感或来自推断），"
                       "所以就地收成了候选",
            ),
            stored=self._row_to_record(self._require(memory_id)),
            created=False,
            blocked=False,
        )

    def approve(self, *, memory_id: str, now: str | None = None) -> MemoryRecord:
        return self._decide(memory_id, target=MemoryStatus.ACTIVE, now=now)

    def reject(self, *, memory_id: str, now: str | None = None) -> MemoryRecord:
        """否决一条候选。

        **不写墓碑。** 用户说的是"这条不对"，不是"别再让我看到这句话"——所以同一句话再次被
        推断出来时仍然可以成为候选（只是去重会把完全相同的重复挡在门外）。墓碑留给"删除"。
        """
        return self._decide(memory_id, target=MemoryStatus.REJECTED, now=now)

    def _decide(
        self, memory_id: str, *, target: MemoryStatus, now: str | None,
    ) -> MemoryRecord:
        row = self._require(memory_id)
        stored = self._row_to_record(row)
        if stored.scope.status is not MemoryStatus.CANDIDATE:
            raise MemoryRepositoryError(
                f"只有候选需要{'批准' if target is MemoryStatus.ACTIVE else '否决'}，"
                f"而这条记忆的状态是 {stored.scope.status.value}"
            )
        if is_expired(stored, now=now or _utc_now()):
            raise MemoryRepositoryError("过期候选不能批准或否决")
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE memory_records SET status=?, updated_at=?, version=version+1 WHERE memory_id=?",
                (target.value, now or _utc_now(), memory_id),
            )
        return self._row_to_record(self._require(memory_id))

    def revise(
        self, *, memory_id: str, text: str, now: str | None = None,
    ) -> MemoryRecord:
        """改写一条记忆的正文，版本号 +1。**创建时间保持原样**。

        改写而不是追加历史：记忆是"现在相信什么"，不是一份保留旧版本的文档。留着旧正文等于
        把一句已经不再成立的话继续存在盘上，而用户以为自己改掉了它。

        新正文被墓碑挡下时**报错**而不是安静返回旧值：调用方正在把一句话改成另一句话，
        而那句话是用户删掉的——这可能是一次真实的越权写入，不该看起来像成功。
        """
        row = self._require(memory_id)
        stored = self._row_to_record(row)
        if stored.scope.status not in (MemoryStatus.ACTIVE, MemoryStatus.CANDIDATE):
            raise MemoryRepositoryError(
                f"状态是 {stored.scope.status.value} 的记忆不能改写"
            )
        if self.is_blocked(
            text=text, group_id=stored.scope.group_id, course_id=stored.scope.course_id,
        ):
            raise MemoryRepositoryError("这句话在这个作用域里被删除过，不能改写成它")

        group_digest = str(row["group_digest"])
        course_digest = str(row["course_digest"])
        content_digest = self._digest(domain="memory.content", value=text)
        clash = self._find(group_digest, course_digest, content_digest)
        if clash is not None and str(clash["memory_id"]) != memory_id:
            raise MemoryRepositoryError(
                "这个作用域里已经有一条内容相同的记忆了，改写会造成两条一样的"
            )

        with self._database.transaction() as connection:
            sensitivity = effective_sensitivity(text, stored.scope.sensitivity)
            decision = classify_write(sensitivity=sensitivity, inferred=False, text=text)
            status = MemoryStatus.CANDIDATE if decision.needs_approval else stored.scope.status
            connection.execute(
                "UPDATE memory_records SET content_digest=?, text=?, version=version+1,"
                " updated_at=?, status=?, sensitivity=? WHERE memory_id=?",
                (
                    content_digest,
                    self._seal(memory_id, "memory.record.text", text.encode("utf-8")),
                    now or _utc_now(),
                    status.value,
                    sensitivity.value,
                    memory_id,
                ),
            )
        return self._row_to_record(self._require(memory_id))

    def delete(self, *, memory_id: str, now: str | None = None) -> MemoryTombstone:
        """删掉一条记忆，留下一个墓碑。

        删的是**行**，不是把状态改成"已删除"：留在盘上的密文只差一把钥匙就能读回来，而用户
        对"删除"的理解是它不在了。墓碑只留指纹——够挡住重学，读不出原文。

        `MemoryStatus.DELETED` 因此不会出现在这张表里，它是给**持有旧对象的调用方**用的标签：
        他们手上那条 `MemoryRecord` 已经不在库里了，这个值说明为什么。
        """
        row = self._require(memory_id)
        stored = self._row_to_record(row)
        moment = now or _utc_now()
        group_digest = str(row["group_digest"])
        course_digest = str(row["course_digest"])

        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO memory_tombstones("
                "  memory_id, group_digest, course_digest, content_digest, deleted_at,"
                "  scope_blob, fingerprint"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(memory_id) DO UPDATE SET"
                "  content_digest=excluded.content_digest, deleted_at=excluded.deleted_at,"
                "  scope_blob=excluded.scope_blob, fingerprint=excluded.fingerprint",
                (
                    memory_id,
                    group_digest,
                    course_digest,
                    str(row["content_digest"]),
                    moment,
                    self._seal(
                        memory_id,
                        "memory.tombstone.scope",
                        self._open(memory_id, "memory.record.scope", row["scope_blob"]),
                    ),
                    self._seal(
                        memory_id,
                        "memory.tombstone.fingerprint",
                        memory_fingerprint(stored.text).encode("ascii"),
                    ),
                ),
            )
            connection.execute("DELETE FROM memory_records WHERE memory_id=?", (memory_id,))

        return MemoryTombstone(
            memory_id=memory_id,
            scope=MemoryScope(
                group_id=stored.scope.group_id,
                course_id=stored.scope.course_id,
                sensitivity=stored.scope.sensitivity,
                status=MemoryStatus.DELETED,
            ),
            fingerprint=memory_fingerprint(stored.text),
            deleted_at=moment,
        )

    def sweep_expired(self, *, now: str | None = None) -> int:
        """把已经过期的记忆标成过期，返回标了几条。

        标状态而不是删除：过期说的是"不再自动想起来"，而用户可能还想知道自己说过这句话。
        真正的删除是用户点的那个动作。

        ⚠ 这只处理**已经存下来的**那一批。过期判定本身在每次召回时都要做（`can_recall`），
        否则"刚过期的记忆在下次扫描之前仍然出得来"——扫描是定时的，而读取是随时发生的。
        """
        moment = now or _utc_now()
        candidates = self._expired_ids(moment)
        if not candidates:
            return 0
        with self._database.transaction() as connection:
            for memory_id in candidates:
                connection.execute(
                    "UPDATE memory_records SET status=?, updated_at=? WHERE memory_id=?",
                    (MemoryStatus.EXPIRED.value, moment, memory_id),
                )
        return len(candidates)

    def _expired_ids(self, moment: str) -> tuple[str, ...]:
        """按 `expires_at` 找过期的行——**这里可以只看列**，因为 `expires_at` 是库里的值，
        而"读不出来的时间戳算不算过期"那条规则仍然只由 `is_expired` 回答（它把读不出来的
        时间当成没有，于是这里也不会误标）。"""
        current = _parse_moment(moment)
        with self._database.transaction(immediate=False) as connection:
            rows = connection.execute(
                "SELECT memory_id, expires_at FROM memory_records"
                " WHERE expires_at IS NOT NULL AND status IN (?, ?)",
                (MemoryStatus.ACTIVE.value, MemoryStatus.CANDIDATE.value),
            ).fetchall()
        expired: list[str] = []
        for row in rows:
            try:
                expires = _parse_moment(str(row["expires_at"]))
            except MemoryRepositoryError:
                continue
            if current >= expires:
                expired.append(str(row["memory_id"]))
        return tuple(expired)

    # ── 级联 ──────────────────────────────────────────────────────────

    def cascade_delete(
        self,
        *,
        group_id: str | None = None,
        course_id: str | None = None,
        source_document_id: str | None = None,
        now: str | None = None,
    ) -> MemoryCascadeReport:
        """按班 / 课 / 来源删除，返回删掉了什么。

        至少要给一个选择器：一个参数都不给的级联就是"删掉全部"，而调用方多半是漏传了参数。

        **墓碑的两种待遇是有意的。**
        * 按班或课删 —— 墓碑一起删。用户说的是"这一整门课不要了"，把它的墓碑留着等于继续
          替一门不存在的课记着"某些话被删过"。
        * 按来源删 —— 墓碑留着。来源级联回答的是"这份文件不再是我们记忆的依据"，而"这句话我
          不要你记着"是用户对内容本身的决定，不该因为换了一份资料就被撤销。

        ⚠ 这里删的是**权威数据**。派生出来的索引与缓存不在这一层（plan 第 87 行要求级联穿透
        它们），调用方拿到报告后必须据此重建——见 `MemoryCascadeReport` 的说明。
        """
        if group_id is None and course_id is None and source_document_id is None:
            raise MemoryRepositoryError("级联删除至少要给一个选择器（班 / 课 / 来源）")

        clauses: list[str] = []
        values: list[str] = []
        if group_id is not None:
            clauses.append("group_digest=?")
            values.append(self._digest(domain="memory.group", value=group_id))
        if course_id is not None:
            clauses.append("course_digest=?")
            values.append(self._digest(domain="memory.course", value=course_id))
        where = " AND ".join(clauses)

        source_clauses = list(clauses)
        source_values = list(values)
        if source_document_id is not None:
            source_clauses.append("source_digest=?")
            source_values.append(self._digest(domain="memory.source", value=source_document_id))
        source_where = " AND ".join(source_clauses)

        counts: dict[str, int] = {}
        with self._database.transaction() as connection:
            for status in (MemoryStatus.ACTIVE, MemoryStatus.CANDIDATE):
                counts[status.value] = int(connection.execute(
                    f"SELECT COUNT(*) AS n FROM memory_records WHERE {source_where} AND status=?",
                    (*source_values, status.value),
                ).fetchone()["n"])
            others = int(connection.execute(
                f"SELECT COUNT(*) AS n FROM memory_records WHERE {source_where}"
                "  AND status NOT IN (?, ?)",
                (*source_values, MemoryStatus.ACTIVE.value, MemoryStatus.CANDIDATE.value),
            ).fetchone()["n"])
            connection.execute(
                f"DELETE FROM memory_records WHERE {source_where}", tuple(source_values),
            )
            tombstones = 0
            if group_id is not None or course_id is not None:
                tombstones = int(connection.execute(
                    f"SELECT COUNT(*) AS n FROM memory_tombstones WHERE {where}", tuple(values),
                ).fetchone()["n"])
                connection.execute(
                    f"DELETE FROM memory_tombstones WHERE {where}", tuple(values),
                )

        # 过期/被否掉的行也算"删掉的记录"：报告说清总数，不然界面上的数字会对不上。
        return MemoryCascadeReport(
            group_id=group_id,
            course_id=course_id,
            source_document_id=source_document_id,
            records=counts[MemoryStatus.ACTIVE.value] + others,
            candidates=counts[MemoryStatus.CANDIDATE.value],
            tombstones=tombstones,
        )

    # ── 小工具 ────────────────────────────────────────────────────────

    @staticmethod
    def _expiry(ttl_seconds: int | None, moment: str) -> str | None:
        if ttl_seconds is None:
            return None
        if ttl_seconds <= 0:
            raise MemoryRepositoryError(f"ttl_seconds 必须大于 0，收到 {ttl_seconds}")
        deadline = _parse_moment(moment) + timedelta(seconds=ttl_seconds)
        return deadline.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
