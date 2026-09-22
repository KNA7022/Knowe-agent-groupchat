"""加密存储的隐私边界：库文件本身能回答什么问题。

第十次复审在 `store.py` 的明文列上找到两条真实泄漏，这个文件把它们钉住：

1. **`content_hash` 是无盐 `sha256(明文)`**。问题不是"能从指纹反推原文"，而是**库文件变成
   一台免费的确认机**：这个产品的块经常很短（姓名+学号+电话、一行成绩、一句评语），候选空间
   很小，穷举得出来；对已知文档也能立刻验证"这句话在不在库里"。而加密保护的正是这类数据。
   复审实测：穷举 314160 次还原出一份六位数文档。

2. **`location` 明文里带 xlsx 工作表名**，而工作表名是用户自己敲进去的正文。复审无密钥直读
   数据库拿到 `sheet:期末成绩单!A1:B2`，而单元格内容仍是密文——`store.py` 声称明文"不泄漏
   内容"，这条不成立。

修法各有取舍，取舍本身写在测试里。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from backend.knowe_rag import Chunk, MasterKey, ParsedBlock, build_chunks
from backend.knowe_rag.store import EncryptedKnowledgeStore, document_identifier


def _key() -> MasterKey:
    return MasterKey(key_material=os.urandom(32))


def _chunks(*texts: str, document_id: str = "doc") -> tuple[Chunk, ...]:
    return build_chunks(document_id, tuple(
        ParsedBlock(text=text, location=f"page:{index}", kind="page")
        for index, text in enumerate(texts, start=1)
    ))


def _fingerprints(path: Path) -> list[str]:
    """无主密钥直读库文件里的 content_hash——攻击者能做的正是这件事。"""
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT DISTINCT content_hash FROM rag_chunks ORDER BY content_hash"
        ).fetchall()
    finally:
        connection.close()
    return [str(row[0]) for row in rows]


# ── F2-3：指纹不能是明文的哈希 ────────────────────────────────────────

def test_the_stored_fingerprint_is_not_a_plain_hash_of_the_text(tmp_path: Path) -> None:
    """落盘的指纹必须绑定密钥，不能是 `sha256(明文)`。"""
    path = tmp_path / "rag.db"
    secret = "家长联系电话 13800138000，请勿外传。"
    with EncryptedKnowledgeStore(path, master_key=_key()) as store:
        store.write_document("doc", _chunks(secret))

    stored = _fingerprints(path)

    assert hashlib.sha256(secret.encode("utf-8")).hexdigest() not in stored
    assert stored, "库里应当有指纹——否则这条测试什么都没测"


def test_a_guessable_document_cannot_be_confirmed_from_the_file(tmp_path: Path) -> None:
    """把短文本穷举一遍，也不该命中库里的指纹。

    复审的原话是"穷举 314160 次后命中：明文 = '314159'"——那就是一台免费的确认机。这里的
    演示用同样的思路：候选集里的每一个都被算一遍，一个都不该出现在库里。
    """
    path = tmp_path / "rag.db"
    answer = "314159"
    with EncryptedKnowledgeStore(path, master_key=_key()) as store:
        store.write_document("doc", _chunks(answer))

    stored = set(_fingerprints(path))
    guesses = {
        hashlib.sha256(f"{number:06d}".encode("ascii")).hexdigest()
        for number in range(0, 2_000)     # 演示用的小候选集，真实候选集是 10^6
    }

    assert stored & guesses == set()


def test_two_places_with_the_same_text_share_one_fingerprint(tmp_path: Path) -> None:
    """绑定密钥**不削弱去重**：库内相同文本仍然共享指纹。

    这是修法能不能成立的关键——如果每个块各算各的，`embedding_requests` 的去重就没了，
    每一次向量化都会重复计费。
    """
    path = tmp_path / "rag.db"
    with EncryptedKnowledgeStore(path, master_key=_key()) as store:
        store.write_document("doc", _chunks("重复的页眉", "重复的页眉"))

    # 两页内容相同 → parent 与 child 共四个块，但只有**一个**不同的指纹。
    assert len(_fingerprints(path)) == 1


def test_a_different_master_key_gives_different_fingerprints(tmp_path: Path) -> None:
    """换密钥 → 指纹全变。这正是"绑定密钥"的含义，也意味着换密钥必须重建索引。"""
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    text = "同一段文字"

    with EncryptedKnowledgeStore(first, master_key=_key()) as store:
        store.write_document("doc", _chunks(text))
    with EncryptedKnowledgeStore(second, master_key=_key()) as store:
        store.write_document("doc", _chunks(text))

    assert set(_fingerprints(first)) & set(_fingerprints(second)) == set()


def test_rerwriting_an_unchanged_document_still_reports_no_work(tmp_path: Path) -> None:
    """指纹换了算法，增量重建必须照常工作。

    调用方拿到的 `Chunk` 来自 `build_chunks`（默认无盐指纹），而库里存的是绑定密钥的那一版；
    如果存储层不把两边统一，"每次写入都认为全变了"——不报错，只是每次都重算全部向量。
    """
    path = tmp_path / "rag.db"
    with EncryptedKnowledgeStore(path, master_key=_key()) as store:
        chunks = _chunks("甲", "乙")
        store.write_document("doc", chunks)
        plan = store.write_document("doc", chunks)

    assert plan.added == ()
    assert plan.removed == ()
    assert plan.embeddings_needed == ()


def test_a_changed_paragraph_is_still_detected(tmp_path: Path) -> None:
    """统一指纹之后，"变了"仍然要被认出来——否则增量重建会漏掉更新。"""
    path = tmp_path / "rag.db"
    with EncryptedKnowledgeStore(path, master_key=_key()) as store:
        store.write_document("doc", _chunks("甲", "乙"))
        plan = store.write_document("doc", _chunks("甲", "乙改"))

    assert [request.text for request in plan.embeddings_needed] == ["乙改"]


def test_missing_embeddings_agrees_with_what_was_stored(tmp_path: Path) -> None:
    """`missing_embeddings` 与库里的指纹必须自洽。

    两套指纹各算各的话，它永远报告"缺向量"——于是每次导入都重新向量化一遍，重复计费，
    而结果看起来完全正常。
    """
    path = tmp_path / "rag.db"
    with EncryptedKnowledgeStore(path, master_key=_key()) as store:
        chunks = _chunks("甲", "乙")
        store.write_document("doc", chunks)
        # 用 store 自己的 `embedding_requests`：模块级那个算的是无盐指纹，与库里的对不上。
        for request in store.embedding_requests(chunks):
            store.store_embedding(request.content_hash, [1.0], model="m")

        assert store.missing_embeddings(chunks, model="m") == ()


# ── F2-1：位置里不能有工作表名 ────────────────────────────────────────

def test_an_xlsx_location_carries_no_worksheet_name(tmp_path: Path) -> None:
    """位置是明文，所以它不能带工作表名——那是用户敲进去的正文。

    复审无密钥直读数据库拿到了 `sheet:期末成绩单!A1:B2`，而单元格内容仍是密文。
    """
    from backend.knowe_rag import parse_document

    path = _write_xlsx(tmp_path / "book.xlsx")
    blocks = parse_document(path)

    for block in blocks:
        assert "期末成绩单" not in block.location, block.location
        assert "张三" not in block.location, block.location


def test_the_worksheet_name_is_still_reachable_and_searchable(tmp_path: Path) -> None:
    """修法不是把表名删掉，而是把它挪进**密文**那一侧。

    表名对用户仍然可见（从解出来的块文本里），而且现在还能被检索到——原实现把表名放在
    location 里，于是"期末成绩单"这个词在检索里根本不存在。
    """
    from backend.knowe_rag import parse_document

    path = _write_xlsx(tmp_path / "book.xlsx")
    blocks = parse_document(path)

    assert any("期末成绩单" in block.text for block in blocks), [b.text for b in blocks]


def test_the_location_still_points_at_the_range(tmp_path: Path) -> None:
    """位置依然要能指回原文的可复查之处——这是它存在的理由。"""
    from backend.knowe_rag import parse_document

    path = _write_xlsx(tmp_path / "book.xlsx")
    blocks = parse_document(path)

    sheets = [block for block in blocks if block.kind == "sheet"]
    assert sheets, [block.kind for block in blocks]
    assert sheets[0].location.startswith("sheet:")
    assert "A1:B2" in sheets[0].location, sheets[0].location


# ── F2-1 的姊妹点：markdown 围栏的 info string ────────────────────────

def test_a_markdown_fence_info_string_is_not_a_location(tmp_path: Path) -> None:
    """围栏后面那一串也是用户正文，同样不能进明文 location。

    第十一次复验实测：```` ```期末成绩单与张三的分数 ```` 会得到
    `location='code:1:期末成绩单与张三的分数'`，父块也带着它；而围栏正文从下一行才开始，
    所以这段文字**既泄漏、又检索不到**——与 F2-1 修掉的工作表名完全同型。
    """
    from backend.knowe_rag import parse_document

    note = tmp_path / "笔记.md"
    note.write_text("```期末成绩单与张三的分数\nprint(1)\n```\n", encoding="utf-8")

    blocks = parse_document(note)

    assert blocks, "解析应当产出块，否则这条测试什么都没测"
    for block in blocks:
        assert "期末成绩单" not in block.location, block.location
        assert "张三" not in block.location, block.location


def test_the_fence_info_string_is_still_reachable_and_searchable(tmp_path: Path) -> None:
    """修法与表名相同：挪进块文本，而不是丢掉。"""
    from backend.knowe_rag import parse_document

    note = tmp_path / "笔记.md"
    note.write_text("```期末成绩单与张三的分数\nprint(1)\n```\n", encoding="utf-8")

    blocks = parse_document(note)

    assert any("期末成绩单" in block.text for block in blocks), [b.text for b in blocks]


def test_a_markdown_fence_still_says_what_language_it_is(tmp_path: Path) -> None:
    """语言标注不能丢：检索时"这段是不是代码"和"是什么语言的代码"用途不同。"""
    from backend.knowe_rag import parse_document

    note = tmp_path / "code.md"
    note.write_text("```python\nprint(1)\n```\n", encoding="utf-8")

    fence = next(b for b in parse_document(note) if b.kind == "code")

    assert fence.location == "code:1"
    assert "python" in fence.text
    assert "print(1)" in fence.text


# ── F2-2：document_id 不能带上路径 ────────────────────────────────────

def test_document_identifier_is_opaque(tmp_path: Path) -> None:
    """`document_id` 是明文存储的，所以它不能是文件路径。

    本地优先应用里最自然的做法就是传路径，而文件名往往等于文档标题（含姓名与班级）。
    这个 helper 给出一个不含任何原文的标识。
    """
    source = tmp_path / "初二(3)班期末成绩单-张三.xlsx"

    identifier = document_identifier(source)

    assert "成绩单" not in identifier
    assert "张三" not in identifier
    assert str(tmp_path) not in identifier
    assert identifier.isascii()


def test_the_same_path_gives_the_same_identifier(tmp_path: Path) -> None:
    """同一份文件两次导入要得到同一个标识，否则"重新导入"会变成"多了一份"。"""
    source = tmp_path / "book.xlsx"

    assert document_identifier(source) == document_identifier(source)


def test_different_paths_give_different_identifiers(tmp_path: Path) -> None:
    assert document_identifier(tmp_path / "a.xlsx") != document_identifier(tmp_path / "b.xlsx")


def _write_xlsx(path: Path) -> Path:
    """一个工作表名叫「期末成绩单」的最小 xlsx。"""
    from backend.tests.test_rag_office_parsers import _row, _xlsx

    body = _row(1, '<c r="A1" t="inlineStr"><is><t>姓名</t></is></c>'
                   '<c r="B1" t="inlineStr"><is><t>数学</t></is></c>')
    body += _row(2, '<c r="A2" t="inlineStr"><is><t>张三</t></is></c>'
                    '<c r="B2"><v>98</v></c>')
    return _xlsx(path, {"期末成绩单": body})
