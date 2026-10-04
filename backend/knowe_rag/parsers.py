"""本地文档解析：把一份文件变成带**结构化位置**的文本块。

Task 3 的"精确引用"建立在这里，所以位置字符串有两个硬要求：

1. 它指向原文里**可复查**的地方（`para:12`、`slide:3`、`sheet:成绩!A1:C4`）；
2. 它对同一份文件**可复现**——解析两次得到完全相同的位置与顺序，否则引用会
   指向别处而依然"合法"。

覆盖七种格式，全部**本地**解析。TXT / Markdown / HTML / DOCX / PPTX / XLSX 只用标准库：
后三种是 ZIP + XML，`zipfile` + `ElementTree` 就够，所以不引入 python-docx / openpyxl /
python-pptx——三个库既给 PyInstaller 产物增重，又只给对象模型、不给稳定位置。PDF 用
`pypdfium2`（体积与许可证测量见 plan 的 "PDF dependency decision"；AGPL 的 PyMuPDF 被排除）。

旧版二进制 Office 格式（`.doc`/`.ppt`/`.xls`）是另一套格式，永远不走这里
（见 `LEGACY_SUFFIXES`）：它们既不是"还没做"，也不该混进"不认识这种后缀"。

页码语义与纯文本段落**故意相反**（`page:N` 按原文页序，没有文本层的页也占号），理由写在
`_parse_pdf` 里：段落号是我们自己编的序号，而页码是用户在阅读器里看得见的。
"""

from __future__ import annotations

from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
import re
import threading
import xml.etree.ElementTree as ElementTree
import zipfile

import pypdfium2 as pdfium

__all__ = [
    "LEGACY_SUFFIXES",
    "SHEET_ROWS_PER_BLOCK",
    "SUPPORTED_SUFFIXES",
    "ParsedBlock",
    "UnsupportedFormat",
    "parse_document",
    "parse_document_bytes",
]


class UnsupportedFormat(ValueError):
    """这份文件不能被解析成块。"""


MAX_EXTRACTED_CHARS = 4_000_000
MAX_PARSE_WORK_CHARS = 8_000_000
MAX_PARSE_BLOCKS = 10_000
MAX_PARSE_CELLS = 100_000
MAX_PARSE_LINES = 100_000


@dataclass
class _ImportBudget:
    work_chars: int = 0
    output_chars: int = 0
    blocks: int = 0
    cells: int = 0

    def work(self, count: int) -> None:
        self.work_chars += count
        if self.work_chars > MAX_PARSE_WORK_CHARS:
            raise UnsupportedFormat("extracted_text_exceeds_import_policy")

    def block(self, count: int) -> None:
        self.blocks += 1
        self.output_chars += count
        if self.blocks > MAX_PARSE_BLOCKS or self.output_chars > MAX_EXTRACTED_CHARS:
            raise UnsupportedFormat("extracted_output_exceeds_import_policy")

    def cell(self) -> None:
        self.cells += 1
        if self.cells > MAX_PARSE_CELLS:
            raise UnsupportedFormat("cell_count_exceeds_import_policy")


_IMPORT_BUDGET: ContextVar[_ImportBudget | None] = ContextVar("rag_import_budget", default=None)


def _work(count: int) -> None:
    budget = _IMPORT_BUDGET.get()
    if budget is not None:
        budget.work(count)


def _cell() -> None:
    budget = _IMPORT_BUDGET.get()
    if budget is not None:
        budget.cell()


def _join(parts: Iterable[str], separator: str = "") -> str:
    """Charge every expanded value and separator before a concatenation allocates output."""
    values = []
    for part in parts:
        _work(len(part) + (len(separator) if values else 0))
        values.append(part)
    return separator.join(values)


def _block(*, text: str, location: str, kind: str) -> ParsedBlock:
    budget = _IMPORT_BUDGET.get()
    if budget is not None:
        budget.block(len(text))
    return ParsedBlock(text=text, location=location, kind=kind)


def validate_parsed_output(blocks: tuple[ParsedBlock, ...]) -> None:
    """Keep a final output guard independent of format-specific extraction accounting."""
    if len(blocks) > MAX_PARSE_BLOCKS or sum(len(block.text) for block in blocks) > MAX_EXTRACTED_CHARS:
        raise UnsupportedFormat("extracted_output_exceeds_import_policy")


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    """一块文本，以及它在原文档里的位置。

    frozen：块身份（位置 + 内容）是引用与索引之间的对应关系，允许就地改写会让两者
    悄悄失配。
    """

    text: str
    location: str
    kind: str


#: 现在能解析的。规格里要求的七种全在这里——"待做"集合已经删掉，因为 PDF 的依赖定下来了，
#: 一个永远为空的集合只会让人以为还有别的东西在排队。
SUPPORTED_SUFFIXES = frozenset({
    ".docx", ".htm", ".html", ".markdown", ".md", ".pdf", ".pptx", ".txt", ".xlsx",
})

#: 旧版二进制 Office 格式。它们**不是**"还没做"：这套解析器永远不支持它们，能行动的
#: 答复是"先另存为新格式"，所以错误消息里直接给出目标后缀。
LEGACY_SUFFIXES = {".doc": ".docx", ".ppt": ".pptx", ".xls": ".xlsx"}


# ── TXT ───────────────────────────────────────────────────────────────

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def _parse_plain_text(raw: str) -> tuple[ParsedBlock, ...]:
    blocks: list[ParsedBlock] = []
    index = 0

    def paragraphs():
        start = 0
        for match in _PARAGRAPH_BREAK.finditer(raw):
            yield raw[start:match.start()]
            start = match.end()
        yield raw[start:]

    for chunk in paragraphs():
        text = chunk.strip()
        if not text:
            continue
        index += 1
        blocks.append(_block(text=text, location=f"para:{index}", kind="paragraph"))
    return tuple(blocks)


# ── Markdown ──────────────────────────────────────────────────────────

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_FENCE = re.compile(r"^```(\S*)\s*$")
_MD_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_MD_TABLE_RULE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_MD_BLOCK_START = (_MD_HEADING, _MD_FENCE, _MD_TABLE_ROW)


def _starts_a_block(line: str) -> bool:
    return any(pattern.match(line) for pattern in _MD_BLOCK_START)


def _parse_markdown(raw: str) -> tuple[ParsedBlock, ...]:
    blocks: list[ParsedBlock] = []
    heading_counts: dict[int, int] = {}
    paragraph_index = 0
    table_index = 0
    code_index = 0
    lines = raw.splitlines()
    cursor = 0

    while cursor < len(lines):
        line = lines[cursor]

        fence = _MD_FENCE.match(line)
        if fence:
            # 围栏后面那一串（info string）**是用户敲进去的正文**：```期末成绩单、```英语单词、
            # ```第一章 都很常见，而它可以是任何非空白 token。放进 location 就是明文泄漏，
            # 而围栏正文从下一行才开始，所以它同时还检索不到——两头都错。
            # 修法与 xlsx 表名相同：location 只留序号，标注挪进块文本的第一行（于是它能被搜到）。
            info = fence.group(1).strip()
            cursor += 1
            body: list[str] = []
            while cursor < len(lines) and not _MD_FENCE.match(lines[cursor]):
                body.append(lines[cursor])
                cursor += 1
            cursor += 1  # closing fence (or end of input)
            code_index += 1
            content = _join(body, "\n").strip("\n")
            blocks.append(_block(
                text=f"{info}\n{content}" if info else content,
                location=f"code:{code_index}",
                kind="code",
            ))
            continue

        heading = _MD_HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            heading_counts[level] = heading_counts.get(level, 0) + 1
            blocks.append(_block(
                text=heading.group(2).strip(),
                location=f"h{level}:{heading_counts[level]}",
                kind="heading",
            ))
            cursor += 1
            continue

        if _MD_TABLE_ROW.match(line):
            rows: list[str] = []
            while cursor < len(lines) and _MD_TABLE_ROW.match(lines[cursor]):
                # 分隔行（| --- | --- |）是排版，不是内容。
                if not _MD_TABLE_RULE.match(lines[cursor]):
                    rows.append(lines[cursor].strip())
                cursor += 1
            table_index += 1
            blocks.append(_block(
                text=_join(rows, "\n"), location=f"table:{table_index}", kind="table",
            ))
            continue

        if line.strip():
            body = [line.strip()]
            cursor += 1
            while (
                cursor < len(lines)
                and lines[cursor].strip()
                and not _starts_a_block(lines[cursor])
            ):
                body.append(lines[cursor].strip())
                cursor += 1
            paragraph_index += 1
            blocks.append(_block(
                text=_join(body, "\n"), location=f"para:{paragraph_index}", kind="paragraph",
            ))
            continue

        cursor += 1

    return tuple(blocks)


# ── HTML ──────────────────────────────────────────────────────────────

#: 这些标签里的字符不是内容。放进块里既污染检索，也可能把 `<script>` 里的字符串
#: 当成"证据"引用出来。
_HTML_NEVER_CONTENT = frozenset({"script", "style", "noscript", "template", "head"})

_HTML_BLOCK_TAGS: dict[str, tuple[str, int]] = {
    "h1": ("heading", 1), "h2": ("heading", 2), "h3": ("heading", 3),
    "h4": ("heading", 4), "h5": ("heading", 5), "h6": ("heading", 6),
    "p": ("paragraph", 0),
    "li": ("list", 0),
}


class _BlockExtractor(HTMLParser):
    """抓最外层的块级标签。

    只抓最外层（`_capture is None` 时才开新的），所以 `<li><p>x</p></li>` 产出**一个**
    list 块而不是两个重叠的块——重叠的块会让引用指向同一段文字两次。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[ParsedBlock] = []
        self._skip_depth = 0
        self._capture: tuple[str, int] | None = None
        self._buffer: list[str] = []
        self._counts: dict[str, int] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HTML_NEVER_CONTENT:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if self._capture is None and tag in _HTML_BLOCK_TAGS:
            self._capture = _HTML_BLOCK_TAGS[tag]
            self._buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _HTML_NEVER_CONTENT:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if self._capture is not None and tag in _HTML_BLOCK_TAGS:
            kind, level = self._capture
            text = re.sub(r"\s+", " ", _join(self._buffer)).strip()
            self._capture = None
            self._buffer = []
            if not text:
                return
            self._counts[kind] = self._counts.get(kind, 0) + 1
            location = (
                f"h{level}:{self._counts[kind]}" if kind == "heading"
                else f"{'list' if kind == 'list' else 'para'}:{self._counts[kind]}"
            )
            self.blocks.append(_block(text=text, location=location, kind=kind))

    def handle_data(self, data: str) -> None:
        if self._skip_depth or self._capture is None:
            return
        self._buffer.append(data)


def _parse_html(raw: str) -> tuple[ParsedBlock, ...]:
    extractor = _BlockExtractor()
    extractor.feed(raw)
    extractor.close()
    return tuple(extractor.blocks)


# ── OOXML 公共部分（DOCX / PPTX / XLSX）───────────────────────────────
#
# 三种格式都是 ZIP + XML，共用的只有"包怎么打开、成员怎么读、XML 怎么解析"。

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _open_package(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        # 把 .doc 直接改名成 .docx 是很常见的操作，那时文件根本不是 ZIP。泄漏
        # BadZipFile 会让上层把它当成一次未知崩溃，而不是"这份文件读不了"。
        raise UnsupportedFormat(
            f"{Path(path).suffix} 不是一个有效的 OOXML 包"
            f"（把旧格式改名过来就会是这样）：{exc}"
        ) from exc


def _package_member(archive: zipfile.ZipFile, name: str, suffix: str) -> bytes:
    try:
        return archive.read(name)
    except KeyError as exc:
        raise UnsupportedFormat(f"{suffix} 包里缺少 {name}，无法解析正文") from exc


def _parse_xml(raw: bytes, name: str, suffix: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise UnsupportedFormat(f"{suffix} 包里的 {name} 不是可解析的 XML：{exc}") from exc


# ── DOCX ──────────────────────────────────────────────────────────────

#: Word 的标题是样式，不是标记。样式 ID 在英文版是 `Heading1`，中文版也是 `Heading1`
#: （界面显示"标题 1"），个别生成器写成 `heading 1`，所以匹配留出空格并忽略大小写。
_DOCX_HEADING_STYLE = re.compile(r"^heading\s*([1-6])$", re.IGNORECASE)


def _docx_paragraph_text(paragraph: ElementTree.Element) -> str:
    """拼一个段落的文本。

    段落文本分散在多个 run 里，制表符和换行是**独立元素**而不是字符：丢掉它们会把
    "姓名<TAB>分数"读成"姓名分数"，表格化段落的列就没了。
    """
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_W}t":
            parts.append(node.text or "")
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    return _join(parts).strip()


def _docx_heading_level(paragraph: ElementTree.Element) -> int | None:
    properties = paragraph.find(f"{_W}pPr")
    if properties is None:
        return None
    style = properties.find(f"{_W}pStyle")
    if style is None:
        return None
    match = _DOCX_HEADING_STYLE.match((style.get(f"{_W}val") or "").strip())
    return int(match.group(1)) if match else None


def _docx_table_text(table: ElementTree.Element) -> str:
    lines: list[str] = []
    for row in table.findall(f"{_W}tr"):
        cells = []
        for cell in row.findall(f"{_W}tc"):
            _cell()
            cells.append(_join((
                text
                for text in (_docx_paragraph_text(p) for p in cell.iter(f"{_W}p"))
                if text
            ), " "))
        if any(cells):
            lines.append(_join(cells, "\t"))
    return _join(lines, "\n")


def _parse_docx_package(archive: zipfile.ZipFile, suffix: str) -> tuple[ParsedBlock, ...]:
    name = "word/document.xml"
    root = _parse_xml(_package_member(archive, name, suffix), name, suffix)
    body = root.find(f"{_W}body")
    if body is None:
        raise UnsupportedFormat(f"{suffix} 包里的 {name} 没有 w:body")

    blocks: list[ParsedBlock] = []
    paragraph_index = 0
    table_index = 0
    heading_counts: dict[int, int] = {}

    # 只遍历 body 的**直接子元素**：表格单元格里的段落由表格那一支负责，否则同一段文字
    # 会在索引里出现两次、位置还不一样，引用指向其中一份而读者看到的是另一份。
    for child in body:
        if child.tag == f"{_W}p":
            text = _docx_paragraph_text(child)
            if not text:
                continue
            level = _docx_heading_level(child)
            if level is None:
                paragraph_index += 1
                blocks.append(_block(
                    text=text, location=f"para:{paragraph_index}", kind="paragraph",
                ))
            else:
                heading_counts[level] = heading_counts.get(level, 0) + 1
                blocks.append(_block(
                    text=text, location=f"h{level}:{heading_counts[level]}", kind="heading",
                ))
        elif child.tag == f"{_W}tbl":
            text = _docx_table_text(child)
            if not text:
                continue
            table_index += 1
            blocks.append(_block(text=text, location=f"table:{table_index}", kind="table"))
    return tuple(blocks)


# ── PPTX ──────────────────────────────────────────────────────────────

_PPTX_SLIDE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")


def _parse_pptx_package(archive: zipfile.ZipFile, suffix: str) -> tuple[ParsedBlock, ...]:
    # 按**编号**排序，不按文件名字符串：字典序会把 slide10 排在 slide2 前面，于是第 2 页的
    # 引用指向第 10 页的内容，而位置看起来完全合法。
    slides = sorted(
        (int(match.group(1)), name)
        for name in archive.namelist()
        if (match := _PPTX_SLIDE.match(name))
    )
    if not slides:
        raise UnsupportedFormat(f"{suffix} 包里没有任何 ppt/slides/slideN.xml")

    blocks: list[ParsedBlock] = []
    for number, name in slides:
        root = _parse_xml(_package_member(archive, name, suffix), name, suffix)
        paragraphs = [
            text
            for paragraph in root.iter(f"{_A}p")
            if (text := _join(node.text or "" for node in paragraph.iter(f"{_A}t")).strip())
        ]
        if paragraphs:
            blocks.append(_block(
                text=_join(paragraphs, "\n"), location=f"slide:{number}", kind="slide",
            ))
    return tuple(blocks)


# ── XLSX ──────────────────────────────────────────────────────────────

#: 一张表切成多少行一块。整张表做成一个块，检索命中时给出的证据会是"这张表"，引用也就
#: 落不到具体几行上；太小又会把一段连续数据切碎。200 行是中小学一张成绩表/课表的量级。
SHEET_ROWS_PER_BLOCK = 200

_XLSX_CELL_REFERENCE = re.compile(r"^([A-Za-z]{1,3})(\d+)$")


def _xlsx_column_index(reference: str) -> int | None:
    """`C12` -> 2（0 基列号）。认不出来就返回 None，由调用方决定怎么退。"""
    match = _XLSX_CELL_REFERENCE.match(reference)
    if not match:
        return None
    index = 0
    for letter in match.group(1).upper():
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return index - 1


def _xlsx_column_name(index: int) -> str:
    name = ""
    position = index + 1
    while position:
        position, remainder = divmod(position - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def _xlsx_workbook_sheets(
    archive: zipfile.ZipFile, suffix: str,
) -> list[tuple[str, str]]:
    """工作表名与它在包里对应的成员路径。

    名字要**来自 workbook.xml**，不能用 `sheet1.xml` 推：位置里写的是用户看得见的那张表，
    而文件编号与显示顺序无关（挪动工作表不会重命名文件）。
    """
    workbook_name = "xl/workbook.xml"
    root = _parse_xml(_package_member(archive, workbook_name, suffix), workbook_name, suffix)
    relationships_name = "xl/_rels/workbook.xml.rels"
    relationships = _parse_xml(
        _package_member(archive, relationships_name, suffix), relationships_name, suffix,
    )
    targets = {
        relationship.get("Id"): relationship.get("Target")
        for relationship in relationships
    }

    container = root.find(f"{_S}sheets")
    if container is None:
        raise UnsupportedFormat(f"{suffix} 包里的 {workbook_name} 没有 sheets")

    sheets: list[tuple[str, str]] = []
    for sheet in container.findall(f"{_S}sheet"):
        name = sheet.get("name") or ""
        target = targets.get(sheet.get(f"{_R}id") or "")
        if not name or not target:
            raise UnsupportedFormat(
                f"{suffix} 的工作表 {name or '(无名)'} 没有可读的正文成员"
            )
        # Target 可以是相对 `xl/` 的路径，也可以是以 / 开头的包内绝对路径。
        member = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
        sheets.append((name, member))
    if not sheets:
        raise UnsupportedFormat(f"{suffix} 包里没有任何工作表")
    return sheets


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    # 没有共享字符串表是合法的（整张表只用内联字符串或数字），不是缺件。
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = _parse_xml(raw, "xl/sharedStrings.xml", ".xlsx")
    return [
        _join(node.text or "" for node in item.iter(f"{_S}t"))
        for item in root.findall(f"{_S}si")
    ]


def _xlsx_cell_value(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "s":
        raw = cell.find(f"{_S}v")
        index = (raw.text or "").strip() if raw is not None and raw.text else ""
        if not index.isdigit() or int(index) >= len(shared):
            return ""
        return shared[int(index)]
    if kind == "inlineStr":
        return _join(node.text or "" for node in cell.iter(f"{_S}t")).strip()
    if kind == "b":
        raw = cell.find(f"{_S}v")
        return "TRUE" if (raw is not None and (raw.text or "").strip() == "1") else "FALSE"
    # 数字、`t="str"`（公式的字符串结果）、`t="e"`（错误码）都直接取 <v> 的原文。
    # 日期不做转换：那需要先读 styles.xml 的 numFmt，见模块末尾的已知限制。
    raw = cell.find(f"{_S}v")
    return (raw.text or "").strip() if raw is not None and raw.text else ""


def _xlsx_sheet_rows(
    sheet_root: ElementTree.Element, shared: list[str],
) -> list[tuple[int, list[tuple[int, str]]]]:
    """(行号, [(0 基列号, 值)])——只保留有值的单元格，全空行不出现在结果里。"""
    data = sheet_root.find(f"{_S}sheetData")
    if data is None:
        return []
    rows: list[tuple[int, list[tuple[int, str]]]] = []
    current_row = 0
    for row_element in data.findall(f"{_S}row"):
        declared = row_element.get("r")
        current_row = int(declared) if declared and declared.isdigit() else current_row + 1
        column = -1
        values: list[tuple[int, str]] = []
        for cell in row_element.findall(f"{_S}c"):
            _cell()
            # 单元格可以不写 `r`（有的生成器只在跳列时才写）。不推断的话格子会全挤到第一
            # 列，引用就指向错的格子；所以缺 `r` 时按同行上一个单元格右移一格。
            reference = cell.get("r")
            if reference:
                resolved = _xlsx_column_index(reference)
                column = resolved if resolved is not None else column + 1
            else:
                column += 1
            value = _xlsx_cell_value(cell, shared)
            # Shared-string references count each time, even though they reuse one Python string.
            _work(len(value))
            if value:
                values.append((column, value))
        if values:
            rows.append((current_row, values))
    return rows


def _xlsx_bands(
    rows: list[tuple[int, list[tuple[int, str]]]],
) -> list[list[tuple[int, list[tuple[int, str]]]]]:
    """把行切成块：连续的行归一块，且不超过 `SHEET_ROWS_PER_BLOCK` 行。

    不连续的行必须**断开**：位置声明的范围里每一行都要在文本里，否则"A1:A3"配两行文本，
    引用能对上位置却对不上内容。
    """
    bands: list[list[tuple[int, list[tuple[int, str]]]]] = []
    current: list[tuple[int, list[tuple[int, str]]]] = []
    previous: int | None = None
    for row in rows:
        if current and (row[0] != previous + 1 or len(current) >= SHEET_ROWS_PER_BLOCK):
            bands.append(current)
            current = []
        current.append(row)
        previous = row[0]
    if current:
        bands.append(current)
    return bands


def _parse_xlsx_package(archive: zipfile.ZipFile, suffix: str) -> tuple[ParsedBlock, ...]:
    shared = _xlsx_shared_strings(archive)
    blocks: list[ParsedBlock] = []
    for sheet_number, (name, member) in enumerate(
        _xlsx_workbook_sheets(archive, suffix), start=1,
    ):
        sheet_root = _parse_xml(_package_member(archive, member, suffix), member, suffix)
        for band in _xlsx_bands(_xlsx_sheet_rows(sheet_root, shared)):
            first_row, last_row = band[0][0], band[-1][0]
            first_column = min(column for _, values in band for column, _ in values)
            last_column = max(column for _, values in band for column, _ in values)
            # Sparse-cell padding can amplify tiny cell values into very wide TSV rows.
            _work(len(band) * (last_column - first_column))
            # 位置声明了列范围，文本就必须逐列对得上，所以空单元格用制表符占位——
            # 压掉它会让"B 列是空的"变成"这一行只有两列"，引用里的列号就错位了。
            lines = []
            for _, values in band:
                row_values = dict(values)
                lines.append(_join((
                    row_values.get(column, "")
                    for column in range(first_column, last_column + 1)
                ), "\t"))
            # 表名进**文本**而不是位置：它在密文里，同时「期末成绩单」这样的词从此可被
            # 检索（原实现把它放在 location，于是检索里根本不存在这个词）。
            blocks.append(_block(
                text=_join([name, *lines], "\n"),
                # 位置是**明文**存储的，所以它不能带工作表名——那是用户敲进去的正文。
                # 复审实测：无密钥直读数据库能拿到 `sheet:期末成绩单!A1:B2`，而单元格
                # 内容仍是密文。表名改放进块文本（加密的那一侧），于是它既能被检索，
                # 又不留在文件里。
                location=(
                    f"sheet:{sheet_number}!"
                    f"{_xlsx_column_name(first_column)}{first_row}:"
                    f"{_xlsx_column_name(last_column)}{last_row}"
                ),
                kind="sheet",
            ))
    return tuple(blocks)


# ── PDF ───────────────────────────────────────────────────────────────

def _normalise_pdf_text(raw: str) -> str:
    """PDFium 用 `\\r\\n` 分行、页末附一个换行。两者都要处理。

    同一个换行在不同解析器里表示不同，位置与文本的对应关系就会随来源变化，比较、切片和
    引用回显会各错一部分。
    """
    return raw.replace("\r\n", "\n").replace("\r", "\n").strip()


def _parse_pdf(path: Path | bytes) -> tuple[ParsedBlock, ...]:
    """一页一块，位置是 `page:N`。

    页码按**原文页序**，没有文本层的页也占一个号——这与纯文本"/O 跳过空段"是**故意相反**的
    规矩，因为两种编号的性质不同：段落号是我们自己编的序号，跳号只影响我们自己；页码是用户
    在阅读器里看得见的页号。略过第 2 页，第 3 页的内容就会被标成 `page:2`，用户翻到第 2 页
    找不到那段话——而引用看起来完全合法。

    没有文本层的页（扫描件、照片）产出一个文本为空、`kind="image_page"` 的块，**不是**直接
    丢掉：丢掉之后上层看到的是一份完整、正常、其实缺了几页的文档，而缺的是哪几页再没有人
    知道。文本为空保证它不会被当成证据，`kind` 让上层知道"这一页有东西，只是不是文字"——
    本地渲染 + 多模态读图那条路径落地后由它接手。
    """
    suffix = ".pdf" if isinstance(path, bytes) else Path(path).suffix.casefold()
    try:
        document = pdfium.PdfDocument(path)
    except (pdfium.PdfiumError, OSError) as exc:
        # 下载中断的半份文件、把别的格式改名成 .pdf、文件被删掉，都会走到这里。
        # 泄漏 PdfiumError（甚至 ctypes 层的异常）会让上层把它当成一次未知崩溃。
        raise UnsupportedFormat(f"{suffix} 打不开：{exc}") from exc

    blocks: list[ParsedBlock] = []
    try:
        if isinstance(path, bytes) and len(document) > 1000:
            raise UnsupportedFormat("pdf_exceeds_page_policy")
        for index in range(len(document)):
            page = document[index]
            try:
                textpage = page.get_textpage()
                try:
                    # Check native character count before requesting a potentially expanded buffer.
                    _work(textpage.count_chars())
                    text = _normalise_pdf_text(textpage.get_text_range())
                finally:
                    textpage.close()
            finally:
                page.close()
            blocks.append(_block(
                text=text,
                location=f"page:{index + 1}",
                kind="page" if text else "image_page",
            ))
    finally:
        document.close()
    return tuple(blocks)


# ── 入口 ──────────────────────────────────────────────────────────────

def _read_text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _parse_ooxml(path: Path, parse_package) -> tuple[ParsedBlock, ...]:
    suffix = Path(path).suffix.casefold()
    with _open_package(path) as archive:
        return parse_package(archive, suffix)


_PARSERS = {
    ".txt": lambda path: _parse_plain_text(_read_text(path)),
    ".md": lambda path: _parse_markdown(_read_text(path)),
    ".markdown": lambda path: _parse_markdown(_read_text(path)),
    ".html": lambda path: _parse_html(_read_text(path)),
    ".htm": lambda path: _parse_html(_read_text(path)),
    ".docx": partial(_parse_ooxml, parse_package=_parse_docx_package),
    ".pptx": partial(_parse_ooxml, parse_package=_parse_pptx_package),
    ".xlsx": partial(_parse_ooxml, parse_package=_parse_xlsx_package),
    ".pdf": _parse_pdf,
}


def parse_document(path: Path) -> tuple[ParsedBlock, ...]:
    """把一个文件解析成有序的块。

    未知后缀**抛错**而不是返回空元组：空元组会被上层读成"这份文件没有内容"，于是索引里
    静默少一份资料——用户看不出差别，直到答案开始不对。
    """

    path = Path(path)
    suffix = path.suffix.casefold()
    parser = _PARSERS.get(suffix)
    if parser is None:
        if suffix in LEGACY_SUFFIXES:
            raise UnsupportedFormat(
                f"{suffix} 是旧版 Office 二进制格式，本产品不解析它："
                f"请先另存为 {LEGACY_SUFFIXES[suffix]} 再导入"
            )
        raise UnsupportedFormat(f"unsupported document format: {suffix or '(no suffix)'}")
    return parser(path)


MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
_PDF_IMPORT_LOCK = threading.Lock()


def _parse_document_bytes(raw: bytes, suffix: str) -> tuple[ParsedBlock, ...]:
    """Parse renderer-selected bytes in memory; never resolve a renderer filesystem path."""
    suffix = suffix.casefold()
    if len(raw) > MAX_IMPORT_BYTES:
        raise UnsupportedFormat("document_too_large")
    text_parsers = {
        ".txt": _parse_plain_text, ".md": _parse_markdown, ".markdown": _parse_markdown,
        ".html": _parse_html, ".htm": _parse_html,
    }
    if suffix in text_parsers:
        decoded = raw.decode("utf-8", errors="replace")
        _work(len(decoded))
        if decoded.count("\n") > MAX_PARSE_LINES:
            raise UnsupportedFormat("line_count_exceeds_import_policy")
        return text_parsers[suffix](decoded)
    if suffix == ".pdf":
        # PDFium is not thread-safe. Concurrent HTTP imports use worker threads.
        with _PDF_IMPORT_LOCK:
            return _parse_pdf(raw)
    packages = {".docx": _parse_docx_package, ".pptx": _parse_pptx_package, ".xlsx": _parse_xlsx_package}
    if suffix not in packages:
        raise UnsupportedFormat("unsupported_document_format")
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            members = archive.infolist()
            if (len(members) > 4096 or sum(member.file_size for member in members) > MAX_PACKAGE_BYTES
                    or any(member.file_size > 16 * 1024 * 1024 or member.flag_bits & 1 for member in members)):
                raise UnsupportedFormat("package_exceeds_import_policy")
            return packages[suffix](archive, suffix)
    except (zipfile.BadZipFile, RuntimeError, ValueError) as exc:
        raise UnsupportedFormat("invalid_document_package") from exc


def parse_document_bytes(raw: bytes, suffix: str) -> tuple[ParsedBlock, ...]:
    token = _IMPORT_BUDGET.set(_ImportBudget())
    try:
        blocks = _parse_document_bytes(raw, suffix)
        validate_parsed_output(blocks)
        return blocks
    finally:
        _IMPORT_BUDGET.reset(token)
