"""Task 3 解析器的红测。

规格（`.superpowers/plans/studycrew-commercial-continuation.md` 的 Task 3）：

    Parse PDF, DOCX, PPTX, XLSX, Markdown, TXT and HTML locally with **structural locations**.

这一批只覆盖**不需要新依赖**的三种（TXT / Markdown / HTML，全用标准库）。PDF 与三种 Office
格式需要先决定引入哪些库（PyInstaller 体积有代价），所以那部分单独一轮，这里先把**契约**
钉住：块、位置、确定性。

位置字符串是 RAG 引用精度的地基：`page:3` / `para:12` / `sheet:Sheet1!A1:C4` 这类。
它必须**稳定**——同一份文件解析两次得到完全相同的位置与顺序，否则"引用"就不可复现。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.knowe_rag.parsers import ParsedBlock, UnsupportedFormat, parse_document


# ── 契约 ──────────────────────────────────────────────────────────────

def test_block_carries_text_location_and_kind() -> None:
    block = ParsedBlock(text="第一段。", location="para:1", kind="paragraph")
    assert block.text == "第一段。"
    assert block.location == "para:1"
    assert block.kind == "paragraph"
    # 不可变：引用与索引都依赖块身份稳定，允许就地改写会毁掉两者的对应关系。
    with pytest.raises(Exception):
        block.text = "改了"  # type: ignore[misc]


# ── TXT ───────────────────────────────────────────────────────────────

def test_plain_text_paragraphs_get_sequence_locations(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("第一段。\n\n第二段。\n\n\n第三段。\n", encoding="utf-8")

    blocks = parse_document(path)

    assert [b.text for b in blocks] == ["第一段。", "第二段。", "第三段。"]
    assert [b.location for b in blocks] == ["para:1", "para:2", "para:3"]
    assert {b.kind for b in blocks} == {"paragraph"}


def test_plain_text_locations_number_only_paragraphs_that_carry_text(tmp_path: Path) -> None:
    """文件首尾的空白不占编号：位置只由**内容**决定。

    如果空白也占号，同一段文字的位置会随着"文件开头多了一个空行"而改变，引用就指向别处
    却依然"合法"。所以这里特意让首尾都有空白段——中间连续的换行会被分隔正则一次吃掉，
    只有首尾的空白能真正分出一个空段来。
    """
    path = tmp_path / "sparse.txt"
    path.write_text("\n\n甲\n\n\n\n乙\n\n\n", encoding="utf-8")

    blocks = parse_document(path)

    assert [(b.text, b.location) for b in blocks] == [("甲", "para:1"), ("乙", "para:2")]


def test_empty_file_yields_no_blocks(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("", encoding="utf-8")
    assert parse_document(path) == ()


# ── Markdown ──────────────────────────────────────────────────────────

def test_markdown_headings_become_their_own_blocks_with_levels(tmp_path: Path) -> None:
    path = tmp_path / "lesson.md"
    path.write_text(
        "# 第一章\n\n导语。\n\n## 1.1 小节\n\n正文一。\n",
        encoding="utf-8",
    )

    blocks = parse_document(path)

    assert [b.kind for b in blocks] == ["heading", "paragraph", "heading", "paragraph"]
    assert [b.text for b in blocks] == ["第一章", "导语。", "1.1 小节", "正文一。"]
    # 标题层级要留在位置里：引用一个二级标题与引用一级标题不是同一件事。
    assert [b.location for b in blocks] == ["h1:1", "para:1", "h2:1", "para:2"]


def test_markdown_code_fence_is_one_block_and_keeps_its_language(tmp_path: Path) -> None:
    path = tmp_path / "code.md"
    path.write_text(
        "说明。\n\n```python\nprint(1)\nprint(2)\n```\n\n收尾。\n",
        encoding="utf-8",
    )

    blocks = parse_document(path)

    fence = next(b for b in blocks if b.kind == "code")
    assert "print(1)" in fence.text and "print(2)" in fence.text
    assert fence.location.startswith("code:")
    # 语言标注要留下：检索时"这段是不是代码"和"是什么语言的代码"用途不同。
    assert "python" in fence.location


def test_markdown_table_is_one_block_not_one_per_row(tmp_path: Path) -> None:
    path = tmp_path / "table.md"
    path.write_text(
        "| 项目 | 值 |\n| --- | --- |\n| 甲 | 1 |\n| 乙 | 2 |\n",
        encoding="utf-8",
    )

    blocks = parse_document(path)

    tables = [b for b in blocks if b.kind == "table"]
    assert len(tables) == 1, [b.kind for b in blocks]
    assert "甲" in tables[0].text and "乙" in tables[0].text


# ── HTML ──────────────────────────────────────────────────────────────

def test_html_script_and_style_never_reach_the_blocks(tmp_path: Path) -> None:
    """脚本与样式不是内容。

    它们进块之后既污染检索，又可能把 `<script>` 里的字符串当成"证据"引用出来。
    """
    path = tmp_path / "page.html"
    path.write_text(
        "<html><head><title>标题</title>"
        "<style>body{color:red}</style>"
        "<script>var secret = '不该出现';</script>"
        "</head><body><p>正文。</p></body></html>",
        encoding="utf-8",
    )

    blocks = parse_document(path)
    joined = "\n".join(b.text for b in blocks)

    assert "正文。" in joined
    assert "不该出现" not in joined
    assert "color:red" not in joined


def test_html_block_content_excludes_nested_script_and_style(tmp_path: Path) -> None:
    """块**内部**的脚本与样式同样不是内容。

    只挡住顶层 `<script>` 是不够的：内联脚本通常就写在正文元素里面，它会跟着所在段落
    一起被切进同一个块，于是追踪脚本里的字符串就成了一段"可引用的证据"。
    """
    path = tmp_path / "inline.html"
    path.write_text(
        '<p>价格是 <span id="p">10</span> 元'
        "<script>track('不该出现');</script>。</p>"
        "<ul><li>条目<style>.不该出现{color:red}</style></li></ul>",
        encoding="utf-8",
    )

    blocks = parse_document(path)
    joined = "\n".join(b.text for b in blocks)

    assert "价格是" in joined and "元" in joined
    assert "不该出现" not in joined


def test_html_structure_is_reflected_in_kind_and_location(tmp_path: Path) -> None:
    path = tmp_path / "structured.html"
    path.write_text(
        "<html><body><h1>大标题</h1><p>一段。</p><ul><li>甲</li><li>乙</li></ul></body></html>",
        encoding="utf-8",
    )

    blocks = parse_document(path)

    kinds = [b.kind for b in blocks]
    assert "heading" in kinds and "paragraph" in kinds and "list" in kinds
    assert all(b.location for b in blocks)


# ── 未支持的格式与确定性 ──────────────────────────────────────────────

def test_unsupported_suffix_fails_loudly(tmp_path: Path) -> None:
    """未知后缀必须报错，而不是返回空块列表。

    空列表会被上层当成"这份文件没有内容"，于是索引里**静默少一份资料**——用户看不出
    差别，直到答案开始不对。
    """
    path = tmp_path / "data.bin"
    path.write_bytes(b"\x00\x01\x02")

    with pytest.raises(UnsupportedFormat):
        parse_document(path)


def test_pdf_is_named_as_not_yet_supported(tmp_path: Path) -> None:
    """PDF **必须**被明确列为"还没做"，而不是走"未知后缀"。

    两者的运维含义不同：一个是"我们还没做"，另一个是"这个文件类型我们不管"。混在一起
    会让人以为 PDF 永远不支持。PDF 要等一个库的取舍决定；三种 Office 格式已经用标准库
    支持了（见 `test_rag_office_parsers.py`）。
    """
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.7 stub")
    with pytest.raises(UnsupportedFormat) as caught:
        parse_document(path)
    assert "还没做" in str(caught.value)


def test_parsing_is_deterministic(tmp_path: Path) -> None:
    path = tmp_path / "same.md"
    path.write_text("# 标题\n\n正文。\n", encoding="utf-8")
    first = parse_document(path)
    second = parse_document(path)
    assert [(b.text, b.location, b.kind) for b in first] == [
        (b.text, b.location, b.kind) for b in second
    ]
