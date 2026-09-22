"""OOXML 三种格式的本地解析：DOCX / PPTX / XLSX。

plan 第 36 行要求这三种格式**本地**解析。它们都是 ZIP + XML，`zipfile` + `ElementTree`
就够用，所以这里不引入 `python-docx` / `openpyxl` / `python-pptx`：三个库既给 PyInstaller
产物增重，又拿不到本项目要的那种稳定位置（库给的是对象模型，位置要自己造）。

位置必须指向原文里**可复查**的地方，所以每个位置都带结构信息——段落序号、幻灯片编号、
工作表名加单元格范围——而不是"第几个块"。引用一个 Excel 单元格区域和引用"某张表"不是
同一件事。

测试用的包是就地构造的最小 OOXML（只有解析器真正需要的成员）。这本身是一句断言：
解析器不依赖与内容无关的成员（`[Content_Types].xml`、`docProps/*`）。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from backend.knowe_rag import ParsedBlock, UnsupportedFormat, parse_document
from backend.knowe_rag.parsers import SHEET_ROWS_PER_BLOCK

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"

_XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def _package(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return path


# ── DOCX 构造 ─────────────────────────────────────────────────────────

def _p(text: str = "", style: str | None = None, runs: str | None = None) -> str:
    properties = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    if runs is None:
        runs = f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>' if text else ""
    return f"<w:p>{properties}{runs}</w:p>"


def _tbl(rows: list[list[str]]) -> str:
    body = "".join(
        "<w:tr>" + "".join(f"<w:tc>{_p(cell)}</w:tc>" for cell in row) + "</w:tr>"
        for row in rows
    )
    return f"<w:tbl>{body}</w:tbl>"


def _docx(path: Path, body: str, members: dict[str, str] | None = None) -> Path:
    package = {
        "word/document.xml": f'{_XML_HEAD}<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>',
    }
    package.update(members or {})
    return _package(path, package)


# ── PPTX 构造 ─────────────────────────────────────────────────────────

def _slide(*paragraphs: str) -> str:
    runs = "".join(f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" for text in paragraphs)
    return (
        f'{_XML_HEAD}<p:sld xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree>'
        f"<p:sp><p:txBody>{runs}</p:txBody></p:sp>"
        "</p:spTree></p:cSld></p:sld>"
    )


def _pptx(path: Path, slides: dict[int, str]) -> Path:
    members = {f"ppt/slides/slide{number}.xml": body for number, body in slides.items()}
    return _package(path, members)


# ── XLSX 构造 ─────────────────────────────────────────────────────────

def _xlsx(
    path: Path,
    sheets: dict[str, str],
    shared: list[str] | None = None,
) -> Path:
    workbook_sheets = "".join(
        f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheets, start=1)
    )
    relationships = "".join(
        f'<Relationship Id="rId{index}" Type="{R}/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, len(sheets) + 1)
    )
    members = {
        "xl/workbook.xml": (
            f'{_XML_HEAD}<workbook xmlns="{S}" xmlns:r="{R}">'
            f"<sheets>{workbook_sheets}</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            f'{_XML_HEAD}<Relationships xmlns="{PR}">{relationships}</Relationships>'
        ),
    }
    for index, body in enumerate(sheets.values(), start=1):
        members[f"xl/worksheets/sheet{index}.xml"] = (
            f'{_XML_HEAD}<worksheet xmlns="{S}"><sheetData>{body}</sheetData></worksheet>'
        )
    if shared is not None:
        items = "".join(f"<si><t>{text}</t></si>" for text in shared)
        members["xl/sharedStrings.xml"] = (
            f'{_XML_HEAD}<sst xmlns="{S}" count="{len(shared)}" '
            f'uniqueCount="{len(shared)}">{items}</sst>'
        )
    return _package(path, members)


def _row(number: int, cells: str) -> str:
    return f'<row r="{number}">{cells}</row>'


def _number_cell(ref: str, value: str) -> str:
    return f'<c r="{ref}"><v>{value}</v></c>'


def _shared_cell(ref: str, index: int) -> str:
    return f'<c r="{ref}" t="s"><v>{index}</v></c>'


def _inline_cell(ref: str, value: str) -> str:
    return f'<c r="{ref}" t="inlineStr"><is><t>{value}</t></is></c>'


# ── DOCX ──────────────────────────────────────────────────────────────

def test_docx_paragraphs_get_sequence_locations(tmp_path: Path) -> None:
    path = _docx(tmp_path / "a.docx", _p("第一段。") + _p("第二段。"))

    blocks = parse_document(path)

    assert [(b.text, b.location, b.kind) for b in blocks] == [
        ("第一段。", "para:1", "paragraph"),
        ("第二段。", "para:2", "paragraph"),
    ]


def test_docx_heading_styles_become_headings_with_their_level(tmp_path: Path) -> None:
    """标题层级写在样式里，位置里就要带上层级。

    引用"1.1 小节"和引用"第一章"不是同一件事；把两者都记成 `para:N` 会让引用看起来
    精确、实际上指不到层级。
    """
    path = _docx(
        tmp_path / "b.docx",
        _p("第一章", style="Heading1") + _p("正文。") + _p("1.1 小节", style="Heading2"),
    )

    blocks = parse_document(path)

    assert [(b.location, b.kind) for b in blocks] == [
        ("h1:1", "heading"),
        ("para:1", "paragraph"),
        ("h2:1", "heading"),
    ]


def test_docx_table_is_one_block_with_rows_and_cells(tmp_path: Path) -> None:
    path = _docx(
        tmp_path / "c.docx",
        _p("下表：") + _tbl([["项目", "值"], ["甲", "1"], ["乙", "2"]]),
    )

    blocks = parse_document(path)

    tables = [b for b in blocks if b.kind == "table"]
    assert len(tables) == 1, [b.kind for b in blocks]
    assert tables[0].location == "table:1"
    # 单元格要留在同一行里，否则"第几行第几列"从文本上就读不出来了。
    assert [line.split("\t") for line in tables[0].text.splitlines()] == [
        ["项目", "值"], ["甲", "1"], ["乙", "2"],
    ]


def test_docx_a_table_paragraph_does_not_also_become_a_paragraph_block(tmp_path: Path) -> None:
    """表格单元格里的段落不能再单独出一个块。

    否则同一段文字在索引里出现两次、位置还不同，引用会指向其中一份而读者看到的是另一份。
    """
    path = _docx(tmp_path / "d.docx", _tbl([["甲", "乙"]]))

    blocks = parse_document(path)

    assert [b.kind for b in blocks] == ["table"]
    assert "para:1" not in [b.location for b in blocks]


def test_docx_runs_tabs_and_breaks_survive_inside_a_paragraph(tmp_path: Path) -> None:
    """一个段落的文本分散在多个 run 里，制表符和换行是独立元素而不是字符。

    拼接时丢掉它们会把"姓名<TAB>分数"读成"姓名分数"，表格化段落的列就没了。
    """
    runs = (
        "<w:r><w:t>姓名</w:t></w:r>"
        "<w:r><w:tab/></w:r>"
        "<w:r><w:t>分数</w:t></w:r>"
        "<w:r><w:br/></w:r>"
        "<w:r><w:t>甲</w:t></w:r>"
        "<w:r><w:tab/></w:r>"
        "<w:r><w:t>90</w:t></w:r>"
    )
    path = _docx(tmp_path / "e.docx", _p(runs=runs))

    blocks = parse_document(path)

    assert blocks[0].text == "姓名\t分数\n甲\t90"


def test_docx_blank_paragraphs_do_not_take_a_number(tmp_path: Path) -> None:
    """空段落不占编号——与纯文本解析同一条规矩。

    编号只由内容决定，否则在文档开头敲一下回车，后面所有引用的位置都会平移。
    """
    path = _docx(tmp_path / "f.docx", _p() + _p("甲") + _p() + _p() + _p("乙"))

    blocks = parse_document(path)

    assert [(b.text, b.location) for b in blocks] == [("甲", "para:1"), ("乙", "para:2")]


# ── PPTX ──────────────────────────────────────────────────────────────

def test_pptx_each_slide_is_one_block_numbered_by_its_file(tmp_path: Path) -> None:
    path = _pptx(tmp_path / "a.pptx", {1: _slide("封面"), 2: _slide("要点一", "要点二")})

    blocks = parse_document(path)

    assert [(b.location, b.kind) for b in blocks] == [("slide:1", "slide"), ("slide:2", "slide")]
    assert blocks[1].text.splitlines() == ["要点一", "要点二"]


def test_pptx_slides_are_ordered_numerically_not_lexically(tmp_path: Path) -> None:
    """幻灯片顺序按编号，不按文件名字符串。

    字典序会把 slide10 排在 slide2 前面，于是第 2 页的引用指向第 10 页的内容——而且
    位置看起来完全合法。
    """
    slides = {number: _slide(f"第{number}页") for number in (1, 2, 10)}
    path = _pptx(tmp_path / "b.pptx", slides)

    blocks = parse_document(path)

    assert [(b.location, b.text) for b in blocks] == [
        ("slide:1", "第1页"), ("slide:2", "第2页"), ("slide:10", "第10页"),
    ]


def test_pptx_empty_slides_yield_no_block(tmp_path: Path) -> None:
    """整页没有文字时不出块，但**不能**因此让后面页的编号移位。"""
    path = _pptx(tmp_path / "c.pptx", {1: _slide("有字"), 2: _slide(), 3: _slide("也有字")})

    blocks = parse_document(path)

    assert [(b.location, b.text) for b in blocks] == [("slide:1", "有字"), ("slide:3", "也有字")]


# ── XLSX ──────────────────────────────────────────────────────────────

def test_xlsx_position_names_the_sheet_and_the_cell_range(tmp_path: Path) -> None:
    """位置要能让人直接跳到那几格去核对。

    只写工作表名的话，一张几千行的表里"这个说法出自哪里"就没有答案了。
    """
    path = _xlsx(
        tmp_path / "a.xlsx",
        {"成绩": _row(1, _number_cell("A1", "90")) + _row(2, _number_cell("B2", "80"))},
    )

    blocks = parse_document(path)

    assert [(b.location, b.kind) for b in blocks] == [("sheet:成绩!A1:B2", "sheet")]


def test_xlsx_resolves_shared_and_inline_strings_and_numbers(tmp_path: Path) -> None:
    path = _xlsx(
        tmp_path / "b.xlsx",
        {
            "Sheet1": _row(
                1,
                _shared_cell("A1", 0)
                + _inline_cell("B1", "内联")
                + _number_cell("C1", "42"),
            )
        },
        shared=["共享"],
    )

    blocks = parse_document(path)

    assert blocks[0].text == "共享\t内联\t42"


def test_xlsx_keeps_columns_aligned_inside_the_range(tmp_path: Path) -> None:
    """位置声明了列范围，文本就必须逐列对得上——中间空格要用制表符占位。

    把空单元格压掉会让"B 列是空的"变成"这一行只有两列"，于是引用里的列号和文本错位。
    """
    path = _xlsx(
        tmp_path / "c.xlsx",
        {"Sheet1": _row(1, _shared_cell("A1", 0) + _number_cell("C1", "1"))},
        shared=["甲"],
    )

    blocks = parse_document(path)

    assert blocks[0].text == "甲\t\t1"
    assert blocks[0].location == "sheet:Sheet1!A1:C1"


def test_xlsx_infers_cell_position_when_the_reference_is_missing(tmp_path: Path) -> None:
    """单元格可以不写 `r`（有的生成器只在跳列时才写）。

    不推断的话要么整份文件读不出来，要么格子位置全挤到第一列——后者的引用会指向错的格子。
    """
    body = _row(1, '<c t="inlineStr"><is><t>甲</t></is></c><c t="inlineStr"><is><t>乙</t></is></c>')
    path = _xlsx(tmp_path / "d.xlsx", {"Sheet1": body})

    blocks = parse_document(path)

    assert blocks[0].text == "甲\t乙"
    assert blocks[0].location == "sheet:Sheet1!A1:B1"


def test_xlsx_tall_sheet_is_split_into_row_bands(tmp_path: Path) -> None:
    """长表要按行分块，且每块的位置写清自己那一段的行范围。

    整张表做成一个块，检索命中时给出的证据会是"这张表"，引用也就落不到具体几行上。
    """
    rows = "".join(
        _row(number, _number_cell(f"A{number}", str(number)))
        for number in range(1, SHEET_ROWS_PER_BLOCK + 2)
    )
    path = _xlsx(tmp_path / "e.xlsx", {"Sheet1": rows})

    blocks = parse_document(path)

    assert [b.location for b in blocks] == [
        f"sheet:Sheet1!A1:A{SHEET_ROWS_PER_BLOCK}",
        f"sheet:Sheet1!A{SHEET_ROWS_PER_BLOCK + 1}:A{SHEET_ROWS_PER_BLOCK + 1}",
    ]
    assert sum(len(b.text.splitlines()) for b in blocks) == SHEET_ROWS_PER_BLOCK + 1


def test_xlsx_empty_sheet_yields_no_block(tmp_path: Path) -> None:
    path = _xlsx(tmp_path / "f.xlsx", {"空表": "", "有表": _row(1, _number_cell("A1", "1"))})

    blocks = parse_document(path)

    assert [b.location for b in blocks] == ["sheet:有表!A1:A1"]


def test_xlsx_blank_rows_are_dropped_from_the_band(tmp_path: Path) -> None:
    """全空行不进块：它在原文里没有任何可引用的内容，留着只会让文本与范围对不上。"""
    body = _row(1, _number_cell("A1", "1")) + _row(2, "") + _row(3, _number_cell("A3", "3"))
    path = _xlsx(tmp_path / "g.xlsx", {"Sheet1": body})

    blocks = parse_document(path)

    assert len(blocks) == 2
    assert [b.location for b in blocks] == ["sheet:Sheet1!A1:A1", "sheet:Sheet1!A3:A3"]


# ── 失败要响 ──────────────────────────────────────────────────────────

def test_a_required_ooxml_member_missing_fails_loudly(tmp_path: Path) -> None:
    """包里没有正文成员时必须报错，而且要说清是**缺件**。

    返回空块列表会被上层读成"这份文件没有内容"，于是索引里静默少一份资料。

    断言不能只看成员名：成员名也出现在"成员内容坏了"那条消息里，于是"文件不是 OOXML"
    和"OOXML 损坏了"两件对用户不同的事会一起通过同一个断言。
    """
    path = _package(
        tmp_path / "a.docx",
        {"docProps/app.xml": f'{_XML_HEAD}<Properties xmlns="x"/>'},
    )

    with pytest.raises(UnsupportedFormat) as caught:
        parse_document(path)

    message = str(caught.value)
    assert "word/document.xml" in message
    assert "缺少" in message


def test_an_office_file_that_is_not_a_zip_fails_loudly(tmp_path: Path) -> None:
    """把旧格式改名成 .docx 是很常见的操作，这时要给一句能行动的话。

    泄漏 `zipfile.BadZipFile` 会让上层把它当成一次未知崩溃，而不是"这份文件读不了"。
    """
    path = tmp_path / "renamed.docx"
    path.write_bytes("这不是一个 ZIP 包".encode("utf-8"))

    with pytest.raises(UnsupportedFormat) as caught:
        parse_document(path)

    assert ".docx" in str(caught.value)


def test_legacy_office_suffixes_get_an_actionable_message(tmp_path: Path) -> None:
    """.doc / .ppt / .xls 是另一套二进制格式，永远不会用这套解析器支持。

    它们既不是"还没做"，也不该混进"不认识这种后缀"：用户手里就有这些文件，能行动
    的答复是"先另存为新格式"。
    """
    for name in ("a.doc", "a.ppt", "a.xls"):
        path = tmp_path / name
        path.write_bytes(b"\xd0\xcf\x11\xe0")
        with pytest.raises(UnsupportedFormat) as caught:
            parse_document(path)
        assert "另存" in str(caught.value) or "save as" in str(caught.value).lower()


# ── 确定性 ────────────────────────────────────────────────────────────

def test_office_parsing_is_deterministic(tmp_path: Path) -> None:
    docx = _docx(tmp_path / "d.docx", _p("标题", style="Heading1") + _p("正文。"))
    pptx = _pptx(tmp_path / "d.pptx", {1: _slide("甲"), 2: _slide("乙")})
    xlsx = _xlsx(tmp_path / "d.xlsx", {"Sheet1": _row(1, _number_cell("A1", "1"))})

    for path in (docx, pptx, xlsx):
        assert parse_document(path) == parse_document(path)
        assert all(isinstance(block, ParsedBlock) for block in parse_document(path))
