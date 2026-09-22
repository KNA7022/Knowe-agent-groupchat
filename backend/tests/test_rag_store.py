"""加密知识库存储。

这里要证明的不是"写进去能读出来"，而是一串反面属性：

- **数据库文件里搜不到明文**。这条一旦不成立，整套加密就只是改了扩展名；
- **密文不能换行**。把 A 行的密文粘到 B 行必须读不出来，否则能写文件的人可以把两段正文
  对调，"这条结论出自原文第 3 页"就不再可信；
- **删除之后那一行真的没了**，而不是留在空闲页里等着被复用。

验收指标里的"删除不可恢复"在存储层就是最后那一条。
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import struct

import pytest

from backend.knowe_rag import (Chunk, MasterKey, ParsedBlock, build_chunks, embedding_requests)
from backend.knowe_rag.store import EncryptedKnowledgeStore, StoreError


def _key() -> MasterKey:
    return MasterKey(key_material=os.urandom(32))


def _blocks(*texts: str) -> tuple[ParsedBlock, ...]:
    return tuple(
        ParsedBlock(text=text, location=f"page:{index}", kind="page")
        for index, text in enumerate(texts, start=1)
    )


def _chunks(*texts: str, document_id: str = "doc") -> tuple[Chunk, ...]:
    return build_chunks(document_id, _blocks(*texts))


def _database_bytes(path: Path) -> bytes:
    """数据库本体加上它的 WAL——内容可能还在 WAL 里没落盘。"""
    collected = b""
    for suffix in ("", "-wal"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            collected += candidate.read_bytes()
    return collected


def _stored_text_blob(path: Path) -> bytes:
    """直接从数据库里取一条 chunk 的密文，用于之后确认它是否还留在文件里。"""
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT text FROM rag_chunks LIMIT 1").fetchone()
    finally:
        connection.close()
    return bytes(row[0]) if row else b""


@pytest.fixture()
def store(tmp_path: Path):
    with EncryptedKnowledgeStore(tmp_path / "rag.db", master_key=_key()) as opened:
        yield opened


# ── 往返 ──────────────────────────────────────────────────────────────

def test_written_chunks_read_back_unchanged(store: EncryptedKnowledgeStore) -> None:
    written = _chunks("甲", "乙")

    store.write_document("doc", written)

    assert store.read_chunks("doc") == written


def test_a_second_store_on_the_same_files_reads_the_same_data(tmp_path: Path) -> None:
    """重启之后资料还在——这正是主密钥必须持久且不变的原因。"""
    path = tmp_path / "rag.db"
    key = _key()
    written = _chunks("甲", "乙")
    with EncryptedKnowledgeStore(path, master_key=key) as first:
        first.write_document("doc", written)

    with EncryptedKnowledgeStore(path, master_key=key) as second:
        assert second.read_chunks("doc") == written


def test_the_chunk_order_is_preserved(store: EncryptedKnowledgeStore) -> None:
    """顺序是引用的一部分：读回来次序变了，`page:1..page:3` 这类区间位置就对不上了。"""
    written = _chunks("甲", "乙", "丙")

    store.write_document("doc", written)

    assert [c.location for c in store.read_chunks("doc")] == [c.location for c in written]


def test_reading_a_document_that_was_never_written_returns_nothing(
    store: EncryptedKnowledgeStore,
) -> None:
    assert store.read_chunks("no-such-document") == ()


# ── 加密真的生效 ──────────────────────────────────────────────────────

def test_the_database_never_holds_the_plaintext(tmp_path: Path) -> None:
    """数据库文件（含 WAL）里不能出现原文。

    这是整个加密层的底线测试：不加密的话它立刻失败，而"加密了但忘了某个字段"这类漏网
    也会在这里露出来——比逐个字段检查更可靠，因为它看的是文件的实际字节。
    """
    path = tmp_path / "rag.db"
    secret = "这是一段独一无二的学习笔记 ZQXJ-4471"
    with EncryptedKnowledgeStore(path, master_key=_key()) as opened:
        opened.write_document("doc", _chunks(secret))

    raw = _database_bytes(path)

    assert secret.encode("utf-8") not in raw
    assert b"ZQXJ-4471" not in raw


def test_the_embedding_vector_is_encrypted_too(tmp_path: Path) -> None:
    """向量也要加密。

    向量不是"随机数字"：它可以从公开的 embedding 服务反推出大致内容，而且是这份资料
    最贵的部分。只加密正文、把向量留成明文是这类实现常见的半吊子做法。
    """
    path = tmp_path / "rag.db"
    vector = [0.125, -0.5, 0.75, 0.0625]
    with EncryptedKnowledgeStore(path, master_key=_key()) as opened:
        opened.store_embedding("hash-1", vector, model="text-embedding-v4")

    raw = _database_bytes(path)

    assert struct.pack("<4f", *vector) not in raw


# ── 完整性：密文不能换行 ──────────────────────────────────────────────

def test_a_chunk_row_moved_onto_another_row_is_rejected(tmp_path: Path) -> None:
    """把 A 行的密文粘到 B 行，读取时必须失败。

    没有这条，能写数据库的人可以把两段正文对调：两边都正常解密，位置与内容却对不上，
    于是引用指向的原文是假的——比读不出来危险得多，因为它看起来一切正常。
    """
    path = tmp_path / "rag.db"
    key = _key()
    with EncryptedKnowledgeStore(path, master_key=key) as opened:
        written = _chunks("甲", "乙")
        opened.write_document("doc", written)
        first, second = written[0].chunk_id, written[1].chunk_id

    import sqlite3

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE rag_chunks SET text = (SELECT text FROM rag_chunks WHERE chunk_id = ?)"
        " WHERE chunk_id = ?",
        (first, second),
    )
    connection.commit()
    connection.close()

    with EncryptedKnowledgeStore(path, master_key=key) as opened:
        with pytest.raises(StoreError):
            opened.read_chunks("doc")


def test_a_tampered_ciphertext_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "rag.db"
    key = _key()
    with EncryptedKnowledgeStore(path, master_key=key) as opened:
        opened.write_document("doc", _chunks("甲"))

    import sqlite3

    connection = sqlite3.connect(path)
    row = connection.execute("SELECT chunk_id, text FROM rag_chunks").fetchone()
    damaged = bytearray(row[1])
    damaged[-1] ^= 0x01
    connection.execute("UPDATE rag_chunks SET text = ? WHERE chunk_id = ?", (bytes(damaged), row[0]))
    connection.commit()
    connection.close()

    with EncryptedKnowledgeStore(path, master_key=key) as opened:
        with pytest.raises(StoreError):
            opened.read_chunks("doc")


def test_a_database_written_with_another_key_cannot_be_read(tmp_path: Path) -> None:
    path = tmp_path / "rag.db"
    with EncryptedKnowledgeStore(path, master_key=_key()) as opened:
        opened.write_document("doc", _chunks("甲"))

    with EncryptedKnowledgeStore(path, master_key=_key()) as other:
        with pytest.raises(StoreError):
            other.read_chunks("doc")


# ── 增量写入 ──────────────────────────────────────────────────────────

def test_rewriting_an_unchanged_document_reports_no_work(store: EncryptedKnowledgeStore) -> None:
    written = _chunks("甲", "乙")

    store.write_document("doc", written)
    plan = store.write_document("doc", written)

    assert plan.added == ()
    assert plan.removed == ()
    assert plan.embeddings_needed == ()


def test_a_changed_page_is_reported_as_needing_a_new_embedding(
    store: EncryptedKnowledgeStore,
) -> None:
    store.write_document("doc", _chunks("甲", "乙"))

    plan = store.write_document("doc", _chunks("甲", "乙改"))

    assert [r.text for r in plan.embeddings_needed] == ["乙改"]
    assert plan.removed == ()


def test_a_removed_page_is_deleted_from_storage(store: EncryptedKnowledgeStore) -> None:
    store.write_document("doc", _chunks("甲", "乙"))

    plan = store.write_document("doc", _chunks("甲"))

    assert plan.removed
    assert all(not chunk.location.startswith("page:2") for chunk in store.read_chunks("doc"))


# ── 删除传播 ──────────────────────────────────────────────────────────

def test_a_deleted_chunk_leaves_no_ciphertext_behind(tmp_path: Path) -> None:
    """删除之后，那一行的**密文**也不能留在文件里。

    只检查"行没了"是不够的：SQLite 默认把删掉的内容留在空闲页里等着复用，于是"删除"在
    界面上生效、在文件里没生效——谁拿到这个目录都能用普通工具把它捞出来。验收指标里的
    "删除不可恢复"在存储层就是这一条。

    先断言密文**确实在**文件里，否则这条测试可能只是碰巧成立，什么也没测到。
    """
    path = tmp_path / "rag.db"
    key = _key()
    with EncryptedKnowledgeStore(path, master_key=key) as opened:
        opened.write_document("doc", _chunks("独一无二的句子 ZQXJ-4471"))
        opened.checkpoint()

    sealed = _stored_text_blob(path)
    assert sealed, "前提不成立：数据库里没有读到密文"
    assert sealed in _database_bytes(path), "前提不成立：密文本来就不在文件里，这条测试测不到东西"

    with EncryptedKnowledgeStore(path, master_key=key) as opened:
        opened.delete_document("doc")
        opened.checkpoint()

    assert sealed not in _database_bytes(path), "删掉的密文还留在文件的空闲页里"


def test_deleting_a_document_removes_every_row(store: EncryptedKnowledgeStore) -> None:
    store.write_document("doc", _chunks("甲", "乙"))

    removed = store.delete_document("doc")

    assert removed == len(_chunks("甲", "乙"))
    assert store.read_chunks("doc") == ()
    assert store.document_ids() == ()


def test_deleting_one_document_leaves_the_others_alone(store: EncryptedKnowledgeStore) -> None:
    """"删掉这份没用的"不该顺手带走别的东西。"""
    kept = build_chunks("keep", _blocks("保留"))
    store.write_document("keep", kept)
    store.write_document("drop", build_chunks("drop", _blocks("删掉")))

    store.delete_document("drop")

    assert store.document_ids() == ("keep",)
    # 拿整份比对，而不是只比文本：parent 与 child 的文本相同，只比文本时"少了一条"看不出来。
    assert store.read_chunks("keep") == kept


def test_deleting_a_document_that_does_not_exist_is_not_an_error(
    store: EncryptedKnowledgeStore,
) -> None:
    """重复删除是正常的（用户在界面上点两次），不该报错。"""
    assert store.delete_document("never-existed") == 0


# ── embedding 去重 ────────────────────────────────────────────────────

def test_an_embedding_is_stored_once_per_content_hash(store: EncryptedKnowledgeStore) -> None:
    """去重的落地：两页文字相同，只留一份向量，但两页都读得出自己的向量。

    这是分块层"按 content_hash 去重"省下来的钱真正兑现的地方——如果这里按 chunk_id 存，
    前面的去重就白做了。
    """
    written = _chunks("重复的页眉", "重复的页眉")
    store.write_document("doc", written)
    hash_value = written[1].content_hash

    store.store_embedding(hash_value, [1.0, 2.0], model="m")
    store.store_embedding(hash_value, [1.0, 2.0], model="m")

    assert store.embedding_hashes(model="m") == (hash_value,)
    assert store.load_embedding(hash_value, model="m") == (1.0, 2.0)


def test_embeddings_do_not_mix_between_models(store: EncryptedKnowledgeStore) -> None:
    """同一段文字在不同模型下的向量不能互相顶替——维度和语义都不一样。

    混用不会报错，只会让检索结果悄悄变差，而这种退化没有任何地方会显示出来。
    """
    store.store_embedding("hash-1", [1.0, 2.0], model="model-a")
    store.store_embedding("hash-1", [3.0, 4.0, 5.0], model="model-b")

    assert store.load_embedding("hash-1", model="model-a") == (1.0, 2.0)
    assert store.load_embedding("hash-1", model="model-b") == (3.0, 4.0, 5.0)
    assert store.load_embedding("hash-1", model="model-c") is None


def test_a_missing_embedding_reads_as_none(store: EncryptedKnowledgeStore) -> None:
    """没有向量不是错误：文档刚导入、还没调用 embedding 服务时就是这样。"""
    assert store.load_embedding("nope", model="m") is None


def test_missing_embeddings_lists_only_the_ones_without_a_vector(
    store: EncryptedKnowledgeStore,
) -> None:
    """已经算过的向量不再请求。

    这是"同一份资料导入两次不该付两次钱"落地的地方：导入流程拿到要算的清单、发现只剩新
    内容，才谈得上省钱。全部重算一遍既不报错也不慢到会被注意到，只是账单翻倍。
    """
    chunks = _chunks("甲", "乙")
    store.write_document("doc", chunks)
    already = next(r for r in embedding_requests(chunks) if r.text == "甲")
    store.store_embedding(already.content_hash, [1.0], model="m")

    assert [r.text for r in store.missing_embeddings(chunks, model="m")] == ["乙"]


def test_missing_embeddings_is_empty_when_everything_is_stored(
    store: EncryptedKnowledgeStore,
) -> None:
    chunks = _chunks("甲", "乙")
    store.write_document("doc", chunks)
    for request in embedding_requests(chunks):
        store.store_embedding(request.content_hash, [1.0], model="m")

    assert store.missing_embeddings(chunks, model="m") == ()


def test_missing_embeddings_ignores_vectors_from_another_model(
    store: EncryptedKnowledgeStore,
) -> None:
    """别的模型算过不算数：维度与语义都不同，拿旧向量顶替只会让检索悄悄变差。"""
    chunks = _chunks("甲")
    store.write_document("doc", chunks)
    for request in embedding_requests(chunks):
        store.store_embedding(request.content_hash, [1.0], model="old-model")

    assert [r.text for r in store.missing_embeddings(chunks, model="new-model")] == ["甲"]


def test_embeddings_round_trip_at_float32_precision(store: EncryptedKnowledgeStore) -> None:
    """向量按 float32 二进制往返，中途不经过十进制文本。

    embedding 服务给的就是 float32，`struct` 保证同样的字节进、同样的字节出；绕一趟
    `str()` / `float()` 会引入十进制舍入，让相似度以一个没人会去查的方式慢慢偏离。

    期望值由 float32 自己算出来，所以这条断言测的是"往返无损"，而不是"float64 精度"——
    后者在这里永远不可能成立，写成那样只会让测试看起来在测一件它测不了的事。
    """
    vector = [0.1, -1e-7, 3.141592653589793, 0.0, 1.0]
    expected = struct.unpack("<5f", struct.pack("<5f", *vector))

    store.store_embedding("hash-1", vector, model="m")

    assert store.load_embedding("hash-1", model="m") == tuple(expected)
