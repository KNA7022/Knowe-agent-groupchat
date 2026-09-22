"""本地文档解析：把一份文件变成带**结构化位置**的文本块。

Task 3 的"精确引用"建立在这里，所以位置字符串有两个硬要求：

1. 它指向原文里**可复查**的地方（`para:12`、`page:3`、`sheet:Sheet1!A1:C4`）；
2. 它对同一份文件**可复现**——解析两次得到完全相同的位置与顺序，否则引用会
   指向别处而依然"合法"。

本模块只覆盖不需要新依赖的三种格式（TXT / Markdown / HTML，全部标准库）。PDF 与三种
Office 格式需要先决定引入哪些库——那会给 PyInstaller 产物增加体积，是一个要摆到台面上的
取舍，所以它们在这里**被显式拒绝**而不是静默返回空列表（见 `PENDING_SUFFIXES`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
import re

__all__ = [
    "PENDING_SUFFIXES",
    "SUPPORTED_SUFFIXES",
    "ParsedBlock",
    "UnsupportedFormat",
    "parse_document",
]


class UnsupportedFormat(ValueError):
    """这份文件不能被解析成块。"""


@dataclass(frozen=True, slots=True)
class ParsedBlock:
    """一块文本，以及它在原文档里的位置。

    frozen：块身份（位置 + 内容）是引用与索引之间的对应关系，允许就地改写会让两者
    悄悄失配。
    """

    text: str
    location: str
    kind: str


#: 现在能解析的。
SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".markdown", ".html", ".htm"})

#: 规格里要求、但尚未决定依赖的。单独列出来是为了让"还没做"和"不管这种类型"区分开——
#: 混在一起会让人以为 PDF 永远不支持。
PENDING_SUFFIXES = frozenset({".pdf", ".docx", ".pptx", ".xlsx"})


# ── TXT ───────────────────────────────────────────────────────────────

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


def _parse_plain_text(raw: str) -> tuple[ParsedBlock, ...]:
    blocks: list[ParsedBlock] = []
    index = 0
    for chunk in _PARAGRAPH_BREAK.split(raw):
        text = chunk.strip()
        if not text:
            continue
        index += 1
        blocks.append(ParsedBlock(text=text, location=f"para:{index}", kind="paragraph"))
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
            language = fence.group(1) or "text"
            cursor += 1
            body: list[str] = []
            while cursor < len(lines) and not _MD_FENCE.match(lines[cursor]):
                body.append(lines[cursor])
                cursor += 1
            cursor += 1  # closing fence (or end of input)
            code_index += 1
            blocks.append(ParsedBlock(
                text="\n".join(body).strip("\n"),
                location=f"code:{code_index}:{language}",
                kind="code",
            ))
            continue

        heading = _MD_HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            heading_counts[level] = heading_counts.get(level, 0) + 1
            blocks.append(ParsedBlock(
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
            blocks.append(ParsedBlock(
                text="\n".join(rows), location=f"table:{table_index}", kind="table",
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
            blocks.append(ParsedBlock(
                text="\n".join(body), location=f"para:{paragraph_index}", kind="paragraph",
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
            text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
            self._capture = None
            self._buffer = []
            if not text:
                return
            self._counts[kind] = self._counts.get(kind, 0) + 1
            location = (
                f"h{level}:{self._counts[kind]}" if kind == "heading"
                else f"{'list' if kind == 'list' else 'para'}:{self._counts[kind]}"
            )
            self.blocks.append(ParsedBlock(text=text, location=location, kind=kind))

    def handle_data(self, data: str) -> None:
        if self._skip_depth or self._capture is None:
            return
        self._buffer.append(data)


def _parse_html(raw: str) -> tuple[ParsedBlock, ...]:
    extractor = _BlockExtractor()
    extractor.feed(raw)
    extractor.close()
    return tuple(extractor.blocks)


# ── 入口 ──────────────────────────────────────────────────────────────

_PARSERS = {
    ".txt": _parse_plain_text,
    ".md": _parse_markdown,
    ".markdown": _parse_markdown,
    ".html": _parse_html,
    ".htm": _parse_html,
}


def parse_document(path: Path) -> tuple[ParsedBlock, ...]:
    """把一个文件解析成有序的块。

    未知后缀**抛错**而不是返回空元组：空元组会被上层读成"这份文件没有内容"，于是索引里
    静默少一份资料——用户看不出差别，直到答案开始不对。
    """

    suffix = Path(path).suffix.casefold()
    parser = _PARSERS.get(suffix)
    if parser is None:
        if suffix in PENDING_SUFFIXES:
            raise UnsupportedFormat(
                f"{suffix} parsing is not yet supported: it needs a dependency decision "
                "(PyInstaller size) before it can be added"
            )
        raise UnsupportedFormat(f"unsupported document format: {suffix or '(no suffix)'}")
    return parser(Path(path).read_text(encoding="utf-8", errors="replace"))
