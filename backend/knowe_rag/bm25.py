"""BM25：不依赖词典的本地词法检索。

plan 第 50 行要求 BM25 召回 40 条。这里做的是**本地、零依赖**的那一路；向量那一路走网关
（`36fb324`），两路的结果由 `fusion` 合并。

两件事在这里定死，因为它们决定索引的含义能不能稳定：

1. **分词**：中文没有空格，用字符二元组（`二次函数` → `二次`/`次函`/`函数`）。不依赖词典的
   经典做法——确定、零依赖，也不会因为分词器换了版本让同一份索引的含义悄悄改变。代价是
   词表更大，而词表只影响内存。
2. **可删**：删除要把词频真的撤掉。只把文档从结果里藏起来是不够的——它原来那些词仍然记在
   文档频率里，于是留下来的文档继续享受一个被稀释过的 IDF。那不会报错，只会让分数从此
   不对，而没有任何地方显示这件事。
"""

from __future__ import annotations

from collections import Counter
import math
import re

__all__ = [
    "Bm25Index",
    "tokenize",
]

#: 一段拉丁字母/数字，或者一段汉字。标点与空白天然成为分隔符——把整句连起来切 bigram 会
#: 切出 `，乙` 这种跨标点的词元，它匹配不到任何真实查询，只让词表变大、长度归一化失真。
_TOKEN = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

#: BM25 的两个标准参数：`k1` 控制词频饱和（出现 10 次不等于相关 10 倍），`b` 控制长度
#: 归一化的强度（0 = 不归一化，1 = 完全按长度折算）。用原论文的默认值而不是自己调——
#: 调参要有评测集，而评测集是 Task 6 的事。
DEFAULT_K1 = 1.2
DEFAULT_B = 0.75


def tokenize(text: str) -> tuple[str, ...]:
    """把文本切成检索用的词元，保持出现顺序。

    汉字段按 **bigram** 切：`二次函数` → `二次`、`次函`、`函数`；只有一个字时它自己就是
    一个词元（否则"甲"这类短词永远检索不到）。
    """
    tokens: list[str] = []
    for match in _TOKEN.finditer(str(text or "").casefold()):
        run = match.group(0)
        if run[0].isascii():
            tokens.append(run)
        elif len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
    return tuple(tokens)


class Bm25Index:
    """可增删的倒排索引。

    倒排表（词元 → 文档集合）是必要的，不是优化：只按词频表算分的话，每个查询词都要遍历
    **每一份**文档，一个几万块的索引每次检索都要走一遍全量。
    """

    def __init__(self, *, k1: float = DEFAULT_K1, b: float = DEFAULT_B) -> None:
        self._k1 = float(k1)
        self._b = float(b)
        self._frequencies: dict[str, Counter[str]] = {}
        self._lengths: dict[str, int] = {}
        self._document_frequency: Counter[str] = Counter()
        self._postings: dict[str, set[str]] = {}

    def __len__(self) -> int:
        return len(self._frequencies)

    def add(self, doc_id: str, text: str) -> None:
        """加入或**替换**一份文档。

        先撤掉旧的那一份：累加会让一份文档凭"被导入过两次"排到前面，而用户只是重新导入了
        同一份文件。
        """
        self.remove(doc_id)
        tokens = tokenize(text)
        if not tokens:
            # 纯标点的文档进索引只会在结果里占一个名额，却永远匹配不到任何查询。
            return
        counts = Counter(tokens)
        self._frequencies[doc_id] = counts
        self._lengths[doc_id] = len(tokens)
        for token in counts:
            self._document_frequency[token] += 1
            self._postings.setdefault(token, set()).add(doc_id)

    def remove(self, doc_id: str) -> bool:
        """移出一份文档；它本来就不在索引里时返回 False（删除是幂等的）。"""
        counts = self._frequencies.pop(doc_id, None)
        if counts is None:
            return False
        self._lengths.pop(doc_id, None)
        for token in counts:
            remaining = self._document_frequency[token] - 1
            if remaining > 0:
                self._document_frequency[token] = remaining
            else:
                self._document_frequency.pop(token, None)
            postings = self._postings.get(token)
            if postings is not None:
                postings.discard(doc_id)
                if not postings:
                    self._postings.pop(token, None)
        return True

    def search(self, query: str, *, limit: int) -> tuple[tuple[str, float], ...]:
        """按 BM25 打分，返回 `(doc_id, score)` 降序；分数相同按 doc_id 升序。"""
        if limit <= 0 or not self._frequencies:
            return ()
        tokens = set(tokenize(query))
        if not tokens:
            return ()
        total = len(self._frequencies)
        average_length = sum(self._lengths.values()) / total
        scores: dict[str, float] = {}
        for token in tokens:
            frequency = self._document_frequency.get(token, 0)
            if not frequency:
                continue
            idf = math.log(1.0 + (total - frequency + 0.5) / (frequency + 0.5))
            for doc_id in self._postings.get(token, ()):
                count = self._frequencies[doc_id][token]
                length = self._lengths[doc_id]
                denominator = count + self._k1 * (
                    1.0 - self._b + self._b * length / average_length
                )
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * (count * (self._k1 + 1.0)) / denominator
        # 分数相同时按 doc_id：否则同样一次检索在两次运行里给出不同顺序，而"这次第一条和
        # 上次不一样"会被当成模型不稳定，实际只是字典迭代顺序。
        ranked = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
        return tuple(ranked[:limit])
