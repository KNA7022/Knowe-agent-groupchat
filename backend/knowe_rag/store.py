"""加密的知识库存储。

plan 第 49 行：源文件、抽取文本、chunk、向量与缓存都用 AES-256-GCM 加密，只留不透明的元数据
明文。这里把分块层和加密层接起来，落在应用已有的 `SQLiteDatabase` 事务边界上。

**明文保留的东西，以及它们各自泄漏什么。**

⚠ 这一段在第十次复审之后重写过。原来的说法是「`location` 与 `content_hash` 不泄漏内容」，
那句话**不成立**：复审无密钥直读数据库拿到了 `sheet:期末成绩单!A1:B2`，并用重算哈希确认了
任意选定句子是否在库里。逐项如实列出：

- `document_id` —— 调用方给的。本地优先应用里最自然的做法就是传文件路径，而文件名往往等于
  文档标题（含姓名与班级）。**必须**用 `document_identifier()` 生成不透明标识；它是约定而
  不是强制（`write_document` 不会拒绝一个像路径的 id，那会误伤合法用途）。
- `location` —— 指向原文的结构位置。`para:`/`page:`/`h1:`/`slide:`/`code:` 只含编号；xlsx 那一支
  用**表在工作簿里的序号**，因为工作表名是用户敲进去的正文，已挪进块文本（加密那一侧）。
  markdown 围栏的 info string 同理（第十一次复验的 Important-3：` ```期末成绩单 ` 这类围栏很
  常见，而它当时既在明文列里、又不在块文本里——泄漏且检索不到）。
- `content_hash` —— **绑定主密钥**的 HMAC（`crypto.keyed_content_hasher`）。无盐 sha256 会让
  库文件变成一台免费的确认机：这个产品的块经常很短（姓名+学号+电话、一行成绩），候选空间
  小到可以穷举，一份已知文档也能被直接确认为在库里。
  ⚠ 它**仍然泄漏一件事**：同一份库里文字相同的两个位置共享同一个指纹（去重就是靠这个），
  所以数一数指纹能知道"这几处文字一样"。这是去重的代价，不是可以顺手去掉的东西。
- `chunk_id`/`parent_id` —— 位置与文档 id 的 sha256 派生值，不含原文。
  ⚠ 它们**不绑定主密钥**：同一个文件导入两个不同主密钥的库，chunk_id 序列完全相同。所以
  "换钥匙必须重建索引"这句话对指纹成立，对 chunk_id 不成立——跨库关联性没有被这层覆盖。
- 其余 —— `kind`/`level`（哪些页是扫描件、文档有多少页）、`sequence`、`chunk_count`、
  `created_at`/`updated_at`（几点在学习、哪天导入的）、`rag_embeddings` 的
  `model`/`dimensions`（用了哪个模型、多少块）。

这些合起来仍然足以还原「谁在什么时候导入了哪份资料、大概多少内容」。这是**被记录的取舍**，
不是「已经解决了」。

**连接一律设 `PRAGMA secure_delete=ON`**：SQLite 默认把删掉的内容留在空闲页里等着复用，
谁拿到这个文件都能用普通工具捞出来。验收指标里的"删除不可恢复"在存储层就是这一条加上
"那一行真的被删了"。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import struct

from knowe_storage._sqlite import SQLiteDatabase

from .chunks import (
    CHILD,
    PARENT,
    Chunk,
    EmbeddingRequest,
    ReindexPlan,
    diff_fingerprints,
    embedding_requests,
)
from .crypto import (
    ContentHasher,
    MasterKey,
    MasterKeyError,
    field_aad,
    keyed_content_hasher,
)

__all__ = [
    "EncryptedKnowledgeStore",
    "StoreError",
    "document_identifier",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rag_meta (
    name TEXT PRIMARY KEY,
    value BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS rag_documents (
    document_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rag_chunks (
    chunk_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    location TEXT NOT NULL,
    kind TEXT NOT NULL,
    level TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    text BLOB NOT NULL,
    FOREIGN KEY (document_id) REFERENCES rag_documents(document_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS rag_chunks_by_document ON rag_chunks(document_id);
CREATE INDEX IF NOT EXISTS rag_chunks_by_hash ON rag_chunks(content_hash);

CREATE TABLE IF NOT EXISTS rag_embeddings (
    model TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (model, content_hash)
);

CREATE TABLE IF NOT EXISTS rag_sources (
    document_id TEXT PRIMARY KEY,
    metadata BLOB NOT NULL,
    original BLOB NOT NULL,
    FOREIGN KEY (document_id) REFERENCES rag_documents(document_id) ON DELETE CASCADE
);
"""

#: 向量的落盘精度。embedding 服务给的就是 float32，用二进制原样存取；绕一趟十进制文本会在
#: 没人会去查的地方引入舍入，然后相似度慢慢偏离。
_VECTOR_FORMAT = "<{}f"

#: 主密钥的自证记录。用主密钥把一句常数封起来，能解开就说明这个库属于这把钥匙。
#:
#: 没有它的时候，三处表现互不一致（第十一次复验实测）：`read_chunks` 逐个字段报认证失败、
#: `missing_embeddings` 静默报告"全都缺向量"、而 `write_document` **静默成功**——因为指纹绑了
#: 密钥，换一把钥匙之后差异判定变成"全部新增"，于是它按 chunk_id 就地覆盖，把原来那把钥匙
#: 的密文和指纹一起改写掉。写入那一刻没有任何报错，而旧密钥此后再也读不回原文。
#:
#: 所以判断只留一处，而且放在打开时：库属于谁，不该由每个方法各自猜。
_KEY_VERIFIER_NAME = "master_key_verifier"

#: 被封印的常数本身没有含义，只用来证明"这把钥匙能解开这个库"。
_KEY_VERIFIER_PLAINTEXT = b"studycrew.rag.master-key-verifier.v1"


class StoreError(RuntimeError):
    """存储层拒绝这次操作：数据解不开、对不上，或者不属于这里。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pack_vector(vector) -> bytes:
    values = tuple(float(value) for value in vector)
    return struct.pack(_VECTOR_FORMAT.format(len(values)), *values)


def _unpack_vector(blob: bytes) -> tuple[float, ...]:
    if len(blob) % 4:
        raise StoreError(f"向量字节数 {len(blob)} 不是 4 的倍数，它已经损坏")
    return struct.unpack(_VECTOR_FORMAT.format(len(blob) // 4), blob)


class EncryptedKnowledgeStore:
    """一份加密的知识库。用 `with` 打开，或在结束时显式 `close()`。"""

    def __init__(self, path: str | Path, *, master_key: MasterKey) -> None:
        self._key = master_key
        # 落盘的指纹必须绑定主密钥。无盐 sha256 让库文件变成一台免费的确认机：
        # 短块（姓名+学号+电话、一行成绩）候选空间小到可以穷举，而一份已知文档
        # 也能被直接确认为「在库里」。
        self._content_hasher: ContentHasher = keyed_content_hasher(master_key)
        self.path = Path(path)
        self._database = SQLiteDatabase(self.path)
        # 连接级设置，每次打开都要重新声明，所以放在这里而不是建表脚本里。
        self._database.initialize("PRAGMA secure_delete=ON;")
        self._database.initialize(_SCHEMA)
        try:
            self._verify_master_key()
        except BaseException:
            self._database.close()
            raise

    # ── 身份 ──────────────────────────────────────────────────────────

    def _verify_master_key(self) -> None:
        """这个库是不是这把钥匙的。**打开时就判，判不过就不打开。**

        两种拒绝，理由不同：

        * 校验记录解不开 —— 库是别人的钥匙建的。继续用下去不会立刻出错，而是把旧资料就地
          改写成新钥匙的密文，旧钥匙的原文永久不可读。
        * 库里有资料却没有校验记录 —— 那是本功能之前建的库，或者是别人的库。这里**不替你
          认领**：认领错了就是把上面那件事做一遍，而认领对了也只是省一次手动操作。
        """
        aad = field_aad("rag_meta", _KEY_VERIFIER_NAME)
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT value FROM rag_meta WHERE name=?", (_KEY_VERIFIER_NAME,),
            ).fetchone()
            if row is None:
                occupied = connection.execute(
                    "SELECT (SELECT COUNT(*) FROM rag_documents)"
                    "     + (SELECT COUNT(*) FROM rag_chunks)"
                    "     + (SELECT COUNT(*) FROM rag_embeddings) AS total"
                ).fetchone()["total"]
                if occupied:
                    raise StoreError(
                        "这个知识库里已经有资料，却没有主密钥校验记录：它可能是更早的版本"
                        "建的，也可能不是这个库。这里不会替你认领它，因为认错了会把里面的"
                        "资料改写成谁都读不出来的密文"
                    )
                connection.execute(
                    "INSERT INTO rag_meta(name, value) VALUES (?, ?)",
                    (_KEY_VERIFIER_NAME, self._key.seal(_KEY_VERIFIER_PLAINTEXT, aad=aad)),
                )
                return
        try:
            opened = self._key.open(row["value"], aad=aad)
        except MasterKeyError as exc:
            raise StoreError(
                "这个知识库不属于当前主密钥（校验记录解不开）。拒绝打开：继续写下去会把"
                "里面的资料静默改写成另一把钥匙的密文，而原来那把钥匙再也读不回来"
            ) from exc
        if opened != _KEY_VERIFIER_PLAINTEXT:
            raise StoreError("主密钥校验记录的内容不对：这个库已经损坏或被替换过")

    # ── 生命周期 ──────────────────────────────────────────────────────

    def close(self) -> None:
        self._database.close()

    def checkpoint(self) -> None:
        """把 WAL 合并回主库并截断它。

        删除之后需要它来兑现"删干净了"：内容可能还躺在 WAL 里，而 WAL 是同一个目录下的普通
        文件，谁拿到这个目录都能读。不调用的话，结果只是"迟早会落盘"，不是"现在就没有了"。
        """
        self._database.initialize("PRAGMA wal_checkpoint(TRUNCATE);")

    def __enter__(self) -> "EncryptedKnowledgeStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ── 加解密 ────────────────────────────────────────────────────────

    def _seal(self, record_id: str, field: str, plaintext: bytes) -> bytes:
        return self._key.seal(plaintext, aad=field_aad(record_id, field))

    def _open(self, record_id: str, field: str, blob: bytes) -> bytes:
        try:
            return self._key.open(blob, aad=field_aad(record_id, field))
        except MasterKeyError as exc:
            raise StoreError(f"{field} 读不出来（记录 {record_id}）：{exc}") from exc

    def _open_text(self, chunk_id: str, blob: bytes) -> str:
        raw = self._open(chunk_id, "text", blob)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StoreError(f"chunk {chunk_id} 的正文不是合法的 UTF-8：{exc}") from exc

    @property
    def content_hasher(self) -> ContentHasher:
        """落盘指纹用的函数（绑定主密钥的那一版）。

        暴露出来是给调用方建块时用的：`build_chunks(..., content_hasher=store.content_hasher)`
        能让内存里的指纹与库里的从一开始就一致。**不传也不会错**——`write_document` 会在
        写入前统一替换——但传了可以省掉一次重算。
        """
        return self._content_hasher

    def sealed_chunk(self, chunk: Chunk) -> Chunk:
        """把块的指纹换成绑定密钥的那一版。

        调用方给的块来自 `build_chunks`，默认指纹是无盐 sha256；而库里存的是绑定密钥的那
        一版。不统一的话两边永远不相等，于是「每次写入都认为全变了」——不报错，只是每次都
        重算全部向量。
        """
        sealed = self._content_hasher(chunk.text)
        return chunk if sealed == chunk.content_hash else replace(
            chunk, content_hash=sealed,
        )

    # ── 读 ────────────────────────────────────────────────────────────

    def read_chunks(self, document_id: str) -> tuple[Chunk, ...]:
        with self._database.transaction(immediate=False) as connection:
            rows = connection.execute(
                "SELECT chunk_id, parent_id, location, kind, level, content_hash, text"
                "  FROM rag_chunks WHERE document_id=? ORDER BY sequence",
                (document_id,),
            ).fetchall()
        # 解密放在事务外：真正的解密是 CPU 工作，没有理由握着数据库锁做。
        return tuple(
            Chunk(
                chunk_id=row["chunk_id"],
                parent_id=row["parent_id"],
                document_id=document_id,
                location=row["location"],
                text=self._open_text(row["chunk_id"], row["text"]),
                content_hash=row["content_hash"],
                kind=row["kind"],
                level=row["level"],
            )
            for row in rows
        )

    def document_ids(self) -> tuple[str, ...]:
        with self._database.transaction(immediate=False) as connection:
            rows = connection.execute(
                "SELECT document_id FROM rag_documents ORDER BY document_id"
            ).fetchall()
        return tuple(row["document_id"] for row in rows)

    def write_source(self, document_id: str, metadata: dict, original: bytes) -> None:
        """Persist filenames, course attribution and original bytes only as authenticated ciphertext."""
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO rag_sources(document_id, metadata, original) VALUES (?, ?, ?)",
                (document_id, self._seal(document_id, "metadata", json.dumps(metadata, ensure_ascii=False).encode("utf-8")),
                 self._seal(document_id, "original", original)),
            )

    def source_metadata(self, document_id: str) -> dict | None:
        with self._database.transaction(immediate=False) as connection:
            row = connection.execute("SELECT metadata FROM rag_sources WHERE document_id=?", (document_id,)).fetchone()
        return json.loads(self._open(document_id, "metadata", row["metadata"]).decode("utf-8")) if row else None

    def read_original(self, document_id: str) -> bytes | None:
        with self._database.transaction(immediate=False) as connection:
            row = connection.execute("SELECT original FROM rag_sources WHERE document_id=?", (document_id,)).fetchone()
        return self._open(document_id, "original", row["original"]) if row else None

    def _stored_fingerprints(self, document_id: str) -> dict[str, str]:
        """只读 id 与指纹：算一次差异不需要把整份文档解密出来。"""
        with self._database.transaction(immediate=False) as connection:
            rows = connection.execute(
                "SELECT chunk_id, content_hash FROM rag_chunks WHERE document_id=?",
                (document_id,),
            ).fetchall()
        return {row["chunk_id"]: row["content_hash"] for row in rows}

    # ── 写 ────────────────────────────────────────────────────────────

    def write_document(self, document_id: str, chunks: tuple[Chunk, ...]) -> ReindexPlan:
        """写入一份文档的这一版，返回与上一版之间的差异。

        差异由 `diff_fingerprints` 判定——和 `plan_reindex` 用的是同一个函数，所以"哪些要
        重新算向量"只有一个答案。两边各写一遍的话，不一致时的表现是"有些 chunk 永远没有
        向量"：检索不到它，也没有任何地方报错。
        """
        sealed = tuple(self.sealed_chunk(chunk) for chunk in chunks)
        previous = self._stored_fingerprints(document_id)
        current = {chunk.chunk_id: chunk.content_hash for chunk in sealed}
        added_ids, removed, unchanged = diff_fingerprints(previous, current)
        added = tuple(chunk for chunk in sealed if chunk.chunk_id in added_ids)

        now = _utc_now()
        with self._database.transaction() as connection:
            # 文档行必须先落地：chunk 的外键指向它，顺序反了会在第一次写入时就撞上
            # FOREIGN KEY constraint failed。
            connection.execute(
                "INSERT INTO rag_documents(document_id, created_at, updated_at, chunk_count)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(document_id) DO UPDATE SET"
                "  updated_at=excluded.updated_at, chunk_count=excluded.chunk_count",
                (document_id, now, now, len(sealed)),
            )
            for chunk_id in removed:
                connection.execute("DELETE FROM rag_chunks WHERE chunk_id=?", (chunk_id,))
            for sequence, chunk in enumerate(sealed):
                if chunk.chunk_id not in added_ids:
                    continue
                connection.execute(
                    "INSERT INTO rag_chunks("
                    "  chunk_id, document_id, parent_id, location, kind, level,"
                    "  content_hash, sequence, text"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(chunk_id) DO UPDATE SET"
                    "  document_id=excluded.document_id, parent_id=excluded.parent_id,"
                    "  location=excluded.location, kind=excluded.kind, level=excluded.level,"
                    "  content_hash=excluded.content_hash, sequence=excluded.sequence,"
                    "  text=excluded.text",
                    (
                        chunk.chunk_id,
                        chunk.document_id,
                        chunk.parent_id,
                        chunk.location,
                        chunk.kind,
                        chunk.level,
                        chunk.content_hash,
                        sequence,
                        self._seal(chunk.chunk_id, "text", chunk.text.encode("utf-8")),
                    ),
                )

        return ReindexPlan(
            added=added,
            removed=removed,
            unchanged=unchanged,
            embeddings_needed=embedding_requests(added),
        )

    def delete_document(self, document_id: str) -> int:
        """删除一份文档的全部 chunk，返回删掉的行数。

        重复删除不报错：用户在界面上点两次是正常的。级联外键是第二道保险，真正的删除在
        这里显式做——否则一个忘了开 `foreign_keys` 的连接就会留下孤儿行，而孤儿行里还带着
        已经"删掉"的资料的密文。
        """
        with self._database.transaction() as connection:
            hashes = connection.execute("SELECT DISTINCT content_hash FROM rag_chunks WHERE document_id=?", (document_id,)).fetchall()
            cursor = connection.execute(
                "DELETE FROM rag_chunks WHERE document_id=?", (document_id,)
            )
            connection.execute("DELETE FROM rag_sources WHERE document_id=?", (document_id,))
            connection.execute("DELETE FROM rag_documents WHERE document_id=?", (document_id,))
            connection.executemany(
                "DELETE FROM rag_embeddings WHERE content_hash=? AND content_hash NOT IN (SELECT content_hash FROM rag_chunks)",
                [(row["content_hash"],) for row in hashes],
            )
            return cursor.rowcount

    # ── 向量 ──────────────────────────────────────────────────────────

    @staticmethod
    def _embedding_record_id(model: str, content_hash: str) -> str:
        return f"{model}\x00{content_hash}"

    def store_embedding(self, content_hash: str, vector, *, model: str) -> None:
        """按 `(模型, 内容指纹)` 存一份向量。

        主键里带模型：同一段文字在不同模型下的向量维度与语义都不同，混用不会报错，只会让
        检索结果悄悄变差。按内容指纹而不是 chunk id 存，才是分块层那次去重真正省下钱的地方。
        """
        values = tuple(float(value) for value in vector)
        blob = self._seal(
            self._embedding_record_id(model, content_hash), "vector", _pack_vector(values)
        )
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO rag_embeddings(model, content_hash, dimensions, vector, created_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(model, content_hash) DO UPDATE SET"
                "  dimensions=excluded.dimensions, vector=excluded.vector,"
                "  created_at=excluded.created_at",
                (model, content_hash, len(values), blob, _utc_now()),
            )

    def load_embedding(self, content_hash: str, *, model: str) -> tuple[float, ...] | None:
        with self._database.transaction(immediate=False) as connection:
            row = connection.execute(
                "SELECT vector, dimensions FROM rag_embeddings"
                " WHERE model=? AND content_hash=?",
                (model, content_hash),
            ).fetchone()
        if row is None:
            return None
        raw = self._open(
            self._embedding_record_id(model, content_hash), "vector", row["vector"]
        )
        values = _unpack_vector(raw)
        if len(values) != int(row["dimensions"]):
            raise StoreError(
                f"向量长度与记录不符：存的是 {row['dimensions']}，解出 {len(values)}"
            )
        return values

    def embedding_hashes(self, *, model: str) -> tuple[str, ...]:
        with self._database.transaction(immediate=False) as connection:
            rows = connection.execute(
                "SELECT content_hash FROM rag_embeddings WHERE model=? ORDER BY content_hash",
                (model,),
            ).fetchall()
        return tuple(row["content_hash"] for row in rows)

    def embedding_requests(
        self, chunks: tuple[Chunk, ...],
    ) -> tuple[EmbeddingRequest, ...]:
        """这些块里要向量化的内容，**指纹与库里一致**。

        调用方应该用这一个，而不是模块级的 `chunks.embedding_requests`：后者算的是无盐指纹，
        与库里的对不上，于是 `store_embedding` 写进去的行与 `missing_embeddings` 查的行是两套
        ——表现是"每次都缺向量"，每次导入都重新算一遍、重复计费，而结果看起来完全正常。
        """
        return embedding_requests(tuple(self.sealed_chunk(chunk) for chunk in chunks))

    def missing_embeddings(
        self, chunks: tuple[Chunk, ...], *, model: str,
    ) -> tuple[EmbeddingRequest, ...]:
        """还缺哪些向量。已经算过的不再请求——这是重复导入同一份资料时不重复付费的那一步。"""
        wanted = self.embedding_requests(chunks)
        if not wanted:
            return ()
        known = set(self.embedding_hashes(model=model))
        return tuple(request for request in wanted if request.content_hash not in known)


def document_identifier(source: str | Path) -> str:
    r"""给一份源文件算一个**不透明**的 `document_id`。

    `document_id` 是明文存储的，而本地优先应用里最自然的做法就是直接传文件路径——文件名往往
    等于文档标题（含姓名与班级）。复审实测库里出现的是
    `C:\Users\...\初二(3)班期末成绩单-张三.xlsx`。

    这个 helper 把它换成一个不含任何原文的标识。路径先解析成绝对路径，所以同一份文件从不同
    工作目录打开会得到同一个 id；在 Windows 上再做一次 casefold，让大小写不同的同一路径也
    落到一起。

    它是**约定**而不是强制：`write_document` 不会拒绝一个看起来像路径的 id（那会误伤合法
    用途）。接线时用它，就是这条契约的全部执行方式。
    """
    resolved = Path(source).expanduser().resolve(strict=False)
    digest = hashlib.sha256(str(resolved).casefold().encode("utf-8")).hexdigest()[:32]
    return f"doc_{digest}"
