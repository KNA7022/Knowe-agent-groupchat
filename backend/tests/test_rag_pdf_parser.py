"""PDF 本地解析：文本层提取。

plan 第 36 行要求 PDF 本地解析。用的是 `pypdfium2`（决定见 plan 的 "PDF dependency decision"：
BSD-3-Clause/Apache-2.0，一个库同时覆盖文本提取与页面渲染，AGPL 的 PyMuPDF 被排除）。

测试用的 PDF 是**就地按字节构造**的，不依赖任何第三方写入库：把它交给真实 PDFium 能读，
这一条本身就被这些测试覆盖（构造器已在 `probe-pdf-extract.py` 里单独验证过一次）。

页码语义与纯文本段落**故意相反**，理由写在 `test_pdf_page_numbers_follow_the_source`：段落号
是我们自己编的序号，而 PDF 页码是原文里真实存在、用户在阅读器里看得见的页号。

测试文本一律用 ASCII：PDF 里的非 ASCII 文本要靠嵌入字体和 ToUnicode CMap 才提取得出，手写的
最小 PDF 这两样都没有，中文会提取成空——那是**构造器**的限制，不是解析器的行为，混在一起会
让测试悄悄变成在测另一件事。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.knowe_rag import UnsupportedFormat, parse_document


def _text_stream(lines: list[str]) -> str:
    """一页的内容流：每行往下挪 30 点。"""
    return "BT /F1 24 Tf 72 700 Td " + " ".join(
        f"({line}) Tj 0 -30 Td" for line in lines
    ) + " ET"


def _build_pdf(pages: list[str]) -> bytes:
    """最小可用 PDF：对象 1 目录、2 页树、3 字体，之后每页两个对象（页 + 内容流）。

    必须带正确的 xref 偏移：PDFium 对坏 xref 有容错，但容错会让测试测的是"容错后的结果"，
    而不是一份正常 PDF 的解析路径。
    """
    font = 3
    kids = " ".join(f"{4 + 2 * index} 0 R" for index in range(len(pages)))
    objects: dict[int, str] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>",
        font: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index, stream in enumerate(pages):
        page_number = 4 + 2 * index
        objects[page_number] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {page_number + 1} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>"
        )
        objects[page_number + 1] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode("latin-1")
    start_xref = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for number in range(1, size):
        out += f"{offsets[number]:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{start_xref}\n%%EOF\n"
    ).encode("latin-1")
    return bytes(out)


def _pdf(path: Path, pages: list[str]) -> Path:
    path.write_bytes(_build_pdf([_text_stream(lines) for lines in pages]))
    return path


# ── 正常路径 ──────────────────────────────────────────────────────────

def test_pdf_pages_become_one_block_each_with_their_page_number(tmp_path: Path) -> None:
    path = _pdf(tmp_path / "two.pdf", [["Page one"], ["Page two"]])

    blocks = parse_document(path)

    assert [(b.text, b.location, b.kind) for b in blocks] == [
        ("Page one", "page:1", "page"),
        ("Page two", "page:2", "page"),
    ]


def test_pdf_page_numbers_follow_the_source(tmp_path: Path) -> None:
    """页码按**原文页序**，中间的空页也占一个号。

    与纯文本段落相反：段落号是我们自己编的序号，跳号只影响我们自己；而 PDF 页码是用户在
    阅读器里看得见的页号。第 2 页是扫描图时把它略过，第 3 页的内容就会被标成 `page:2`，
    用户翻到第 2 页找不到那段话——引用看起来完全合法。
    """
    path = _pdf(tmp_path / "gap.pdf", [["Page one"], [], ["Page three"]])

    blocks = parse_document(path)

    assert [b.location for b in blocks] == ["page:1", "page:2", "page:3"]
    assert [(b.location, b.kind) for b in blocks] == [
        ("page:1", "page"), ("page:2", "image_page"), ("page:3", "page"),
    ]


def test_pdf_page_without_a_text_layer_is_marked_not_dropped(tmp_path: Path) -> None:
    """没有文本层的页（扫描件、照片）要**出现**在结果里，只是不带文本。

    这条是"静默少内容"的反面：直接跳过它，上层看到的就是一份完整、正常、其实缺了几页的
    文档，而缺的是哪几页再也没有人知道。文本为空、`kind` 不同，上层据此知道"这一页有东西，
    只是不是文字"，视线路径（多模态模型读图）落地后由它接手。
    """
    path = _pdf(tmp_path / "scan.pdf", [["Text page"], []])

    blocks = parse_document(path)

    scanned = [b for b in blocks if b.kind == "image_page"]
    assert len(scanned) == 1, [(b.kind, b.location) for b in blocks]
    assert scanned[0].text == ""
    assert scanned[0].location == "page:2"


def test_pdf_page_text_normalises_line_breaks(tmp_path: Path) -> None:
    """PDFium 用 `\\r\\n` 分行，落进索引前要归一成 `\\n`。

    同一个换行在不同解析器里表示不同，位置与文本的对应关系就会随来源变化；比较、切片、
    引用回显都会各错一部分。
    """
    path = _pdf(tmp_path / "lines.pdf", [["First line", "Second line"]])

    blocks = parse_document(path)

    assert blocks[0].text == "First line\nSecond line"
    assert "\r" not in blocks[0].text


def test_pdf_page_text_is_stripped(tmp_path: Path) -> None:
    """页内首尾空白要裁掉。

    PDFium 自己会做一次归一，但只到"首尾各留一个空格"为止——实测 `'  padded  '` 回来是
    `' padded '`。留着它，同一段文字在"页内缩进"与"页首顶格"两种 PDF 里就会带着不同空白
    进索引，去重和引用回显都会跟着分叉。这条断言只有在 `strip()` 真的起作用时才通过，
    去掉它就会失败。

    故意用**单行**：`strip()` 只管整个字符串的首尾，行内的尾随空格它不动，混在一起会让
    这条测试测的变成后一件事。
    """
    path = _pdf(tmp_path / "padded.pdf", [["   hello   "]])

    blocks = parse_document(path)

    assert blocks[0].text == "hello"


# ── 失败要响 ──────────────────────────────────────────────────────────

def test_pdf_that_is_not_a_pdf_fails_loudly(tmp_path: Path) -> None:
    """PDFium 抛的是 `PdfiumError`，泄漏出去上层会当成未知崩溃。

    真实场景很常见：下载中断的半个文件、把别的格式改名成 .pdf。
    """
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"this is not a pdf")

    with pytest.raises(UnsupportedFormat) as caught:
        parse_document(path)

    assert ".pdf" in str(caught.value)


def test_empty_pdf_file_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"")

    with pytest.raises(UnsupportedFormat):
        parse_document(path)


def test_a_pdf_that_does_not_exist_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedFormat):
        parse_document(tmp_path / "nothing-here.pdf")


# ── 确定性 ────────────────────────────────────────────────────────────

def test_pdf_parsing_is_deterministic(tmp_path: Path) -> None:
    path = _pdf(tmp_path / "same.pdf", [["A"], [], ["B", "C"]])

    assert parse_document(path) == parse_document(path)
