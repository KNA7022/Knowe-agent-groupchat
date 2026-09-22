"""引用校验与弃答。

plan 第 51 行：验证权限、版本、课程隔离与**精确引用**，对没有依据的校内事实**弃答**。

这一层回答的是"这个答案能不能交出去"，而不是"检索得好不好"：

* **位置必须能被解析**。`page:3#2` 既是给用户看的，也是回原文核对的入口；解析不了的位置
  意味着用户点不过去，那条引用就只是装饰。
* **答案里引用的每一个位置都必须在证据包里**。引用了证据里没有的位置就是**幻觉引用**——
  它比"没有引用"更可信，因为它长得像一条出处。
* **弃答有两个不同的原因**，混在一起用户就分不清该怎么办：证据不够（该补充资料）还是引用了
  不存在的位置（该重问或上报）。这个产品里这两件事是完全不同的信号。

作用域隔离不在这里：那是 `retrieval.py` 里"一个作用域一个索引"保证的，这一层只校验引用与
证据之间的对应关系。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Sequence

from .fusion import EvidencePack

__all__ = [
    "CitationReport",
    "Grounding",
    "GroundingVerdict",
    "LocationError",
    "ParsedLocation",
    "parse_location",
    "sort_locations",
    "verify_citations",
]

#: 位置的形式是 `<类型>:<来源>`，可选再跟 `#<序号>`。
#:
#: 类型是解析器产出的那几种（`parsers.py` 的 `kind` 与位置前缀一致）。**来源部分不做任何
#: 字符限制**：`sheet:成绩!A1:C4` 里有 `!` 和 `:`，`code:1:python` 里有 `:`——按 `:` 分割
#: 会把它们切碎。
_LOCATION = re.compile(r"^(?P<kind>para|page|slide|table|code|sheet|h[1-6]):(?P<body>.+)$")

#: child 序号。**只有 `#` 后面全是数字且直到结尾**才算序号：`sheet:Sheet#1!A1` 里的 `#`
#: 属于工作表名，不能被当成序号——把工作表名的后半截当序号，会让引用指向一块不存在的区域。
_ORDINAL = re.compile(r"^(?P<source>.+?)#(?P<ordinal>\d+)$")


class LocationError(ValueError):
    """这个位置解析不了。"""


@dataclass(frozen=True, slots=True)
class ParsedLocation:
    kind: str
    source: str
    ordinal: int | None
    raw: str

    @property
    def _numeric(self) -> tuple[int, str]:
        """纯数字的来源按数值排，其余按文本排。

        按字符串排会得到 `page:10` < `page:2`——引用列表的顺序就和原文顺序不一样了，而
        "按原文顺序"正是排序存在的全部理由。
        """
        return (int(self.source), "") if self.source.isdigit() else (0, self.source)

    @property
    def sort_key(self) -> tuple:
        return (self.kind, *self._numeric, self.ordinal or 0)


def parse_location(value: str) -> ParsedLocation:
    """把位置字符串解析成结构化形式；解析不了就抛 `LocationError`。

    **不静默接受**。一个解析不了的位置意味着用户点不过去，而那条引用看上去完全正常——
    比没有引用更糟，因为它长得像一条出处。
    """
    text = str(value or "").strip()
    match = _LOCATION.match(text)
    if match is None:
        raise LocationError(f"位置解析不了：{value!r}")
    body = match.group("body")
    ordinal: int | None = None
    ordinal_match = _ORDINAL.match(body)
    if ordinal_match is not None:
        body = ordinal_match.group("source")
        ordinal = int(ordinal_match.group("ordinal"))
        if ordinal < 1:
            raise LocationError(f"child 序号必须是正整数：{value!r}")
    elif "#" in body:
        # `#` 出现了却不成序号，几乎一定是把序号写错了。放它过去就等于承认一个错位置合法。
        raise LocationError(f"位置里的 # 后面不是序号：{value!r}")
    return ParsedLocation(
        kind=match.group("kind"), source=body, ordinal=ordinal, raw=text,
    )


def sort_locations(locations: Sequence[str]) -> tuple[str, ...]:
    """把一组位置按原文顺序排好。

    模型引用证据的顺序是随机的，而人核对时是顺着原文看的。
    """
    parsed = sorted((parse_location(value) for value in locations), key=lambda item: item.sort_key)
    return tuple(item.raw for item in parsed)


@dataclass(frozen=True, slots=True)
class CitationReport:
    """答案的引用与证据之间的对应关系。"""

    #: 答案引用且**确实在证据里**的位置。
    supported: tuple[str, ...]
    #: 答案引用了但证据里没有的位置——幻觉引用。
    unsupported: tuple[str, ...]
    #: 找到了却没被答案引用的证据。它是"答案可能漏了什么"的唯一线索。
    uncited: tuple[str, ...]

    @property
    def is_grounded(self) -> bool:
        """有引用、且引用的每一条都在证据里。

        没有任何引用时为 False：在校内事实这种场景里，"我没找到依据"和"我找到了但没说来自
        哪里"必须能被区分开，否则"有据回答率"这个指标没有意义。
        """
        return bool(self.supported) and not self.unsupported


def verify_citations(cited: Sequence[str], pack: EvidencePack) -> CitationReport:
    """把答案里引用的位置与证据包对上。"""
    parsed = [parse_location(value) for value in cited]
    known = {item.location for item in pack.items}
    referred = {item.raw for item in parsed}
    return CitationReport(
        supported=sort_locations(referred & known),
        unsupported=sort_locations(referred - known),
        uncited=sort_locations(known - referred),
    )


class GroundingVerdict(Enum):
    """这个答案能不能交出去。"""

    ANSWER = "answer"
    #: 证据条数不够——该补充资料。
    ABSTAIN_INSUFFICIENT = "abstain_insufficient"
    #: 引用对不上（含"根本没有引用"）——该重问或上报。
    ABSTAIN_UNSUPPORTED_CITATION = "abstain_unsupported_citation"


@dataclass(frozen=True, slots=True)
class Grounding:
    verdict: GroundingVerdict
    report: CitationReport
    required_evidence: int
    available_evidence: int

    @property
    def should_answer(self) -> bool:
        return self.verdict is GroundingVerdict.ANSWER

    @staticmethod
    def judge(*, pack: EvidencePack, cited: Sequence[str]) -> "Grounding":
        """判定能不能作答。

        **"证据不够"先判**：证据本来就不够时，引用对不对得上已经不是主要问题了——那时候
        该做的是去补充资料，而不是去查模型引用得对不对。
        """
        report = verify_citations(cited, pack)
        if not pack.is_sufficient:
            verdict = GroundingVerdict.ABSTAIN_INSUFFICIENT
        elif report.unsupported or not report.supported:
            verdict = GroundingVerdict.ABSTAIN_UNSUPPORTED_CITATION
        else:
            verdict = GroundingVerdict.ANSWER
        return Grounding(
            verdict=verdict,
            report=report,
            required_evidence=pack.minimum,
            available_evidence=len(pack.items),
        )
