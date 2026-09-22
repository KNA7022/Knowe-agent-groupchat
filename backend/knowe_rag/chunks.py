"""分块：把解析出的块变成可检索、可引用、可增量维护的 chunk。

plan 第 48 行要求 child/parent 两级分块、稳定 ID、去重、增量重建与删除传播。这一层的每个
决定都在回答同一个问题：**改动一份文档之后，哪些引用还有效、哪些向量必须重算。**

- parent 是给模型看的上下文单元（一页、一张表、一个标题下的正文）；
- child 是检索命中的单元，位置指回 parent（`page:3#2`）；
- 只有 child 送 embedding，parent 不送：parent 太长，而且内容已被它的 child 覆盖；
- `chunk_id` 由**文档 + 位置 + 序号**决定，`content_hash` 由**文本**决定。前者决定引用
  能不能对上，后者决定要不要重新调 embedding——两件事分开，才不会为了其中一件牺牲另一件。

id 里带位置是有代价的：在中间插入一段，它下游所有 chunk 的 id 都会变。这条代价写在
`test_inserting_a_paragraph_shifts_the_ids_after_it` 里，而不是假装 id 与顺序无关。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from .parsers import ParsedBlock

__all__ = [
    "CHILD_MAX_CHARS",
    "PARENT_MAX_CHARS",
    "Chunk",
    "EmbeddingRequest",
    "ReindexPlan",
    "build_chunks",
    "embedding_requests",
    "plan_reindex",
]

#: 一个 parent 最多多少字符。没有标题的长文档靠它断开——不断开的话整份 TXT 会变成一个
#: parent，检索给回的"证据"就退化成"整个文件"。
PARENT_MAX_CHARS = 2000

#: 一个 child 最多多少字符。太小会把一句话切碎，太大则一次命中带回太多无关文字，
#: 挤占证据包的名额。
CHILD_MAX_CHARS = 400

#: 这几种块本身就是完整的语义单元，不与邻居合并。
_PROTECTED_KINDS = frozenset({"page", "slide", "sheet", "table", "image_page"})

PARENT = "parent"
CHILD = "child"


@dataclass(frozen=True, slots=True)
class Chunk:
    """一段可检索的文本，以及它在文档里的位置。

    frozen：chunk 是索引、引用、缓存三者的共同键，允许就地改写会让它们各自记住不同的版本。
    """

    chunk_id: str
    #: child 指向它的 parent；parent 指向自己，这样"按 parent_id 分组"是一句话而不是一个分支。
    parent_id: str
    document_id: str
    #: child 的位置是 `<parent 位置>#<序号>`，可以直接回到原文核对。
    location: str
    text: str
    #: 只看文本。相同文字在不同位置共享它，去重就是靠这个省下 embedding 调用。
    content_hash: str
    kind: str
    level: str


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    """一次向量化请求：**去重后**的内容。同一个 content_hash 只会出现一次。"""

    content_hash: str
    text: str


@dataclass(frozen=True, slots=True)
class ReindexPlan:
    """一次重建要做的全部事。

    `removed` 只列**新版本里不再存在**的 chunk_id：位置没变而内容变了的 chunk 沿用同一个
    id，索引层 upsert 就把它替换掉了，不需要先删后加——那道多余的删除会在中途留下一个
    索引里不存在的窗口。
    """

    added: tuple[Chunk, ...]
    removed: tuple[str, ...]
    unchanged: tuple[str, ...]
    embeddings_needed: tuple[EmbeddingRequest, ...]


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunk_id(document_id: str, location: str, ordinal: int) -> str:
    """用 sha256 而不是可读拼接：`document_id` 是调用方给的（可能是一段路径），把它和位置
    直接拼进主键会让分隔符出现在值里，然后两条不同的 chunk 撞成同一个 id。
    """
    raw = f"{document_id}\x00{location}\x00{ordinal}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _group_text(blocks: list[ParsedBlock]) -> str:
    return "\n".join(block.text for block in blocks if block.text)


def _parent_location(blocks: list[ParsedBlock]) -> str:
    first = blocks[0].location
    last = blocks[-1].location
    return first if first == last else f"{first}..{last}"


def _group_into_parents(
    blocks: tuple[ParsedBlock, ...], max_chars: int,
) -> list[list[ParsedBlock]]:
    """按结构分组：标题开启一组、受保护类型自成一组、其余往上加，超过上限就断开。"""
    groups: list[list[ParsedBlock]] = []
    current: list[ParsedBlock] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        if current:
            groups.append(current)
        current = []
        size = 0

    for block in blocks:
        if block.kind == "heading" or block.kind in _PROTECTED_KINDS:
            flush()
            current = [block]
            size = len(block.text)
            if block.kind in _PROTECTED_KINDS:
                flush()
            continue
        # 上限算的是**拼起来之后**的长度，分隔符也算一个字符——否则 parent 会稳定地比上限
        # 多出"块数减一"个字符，而这个偏差会随分块方式变化。
        if current and size + 1 + len(block.text) > max_chars:
            flush()
        if not current:
            current = [block]
            size = len(block.text)
            continue
        current.append(block)
        size += 1 + len(block.text)
    flush()
    return groups


def _split_into_children(text: str, max_chars: int) -> tuple[str, ...]:
    """按**行**累积切分：切点落在行边界上。

    从句子中间切开会让检索命中半句话、引用回显接不上；而且行边界是确定的，字符偏移量不是
    ——同一段文字换个分块参数就会切出不同的句子。

    比上限还长的单行（一张表的 TSV 行、没有换行的长段落）只能硬切，但**一个字符都不能丢**：
    丢掉尾巴意味着检索永远看不到后面的内容，而没有任何地方显示少了东西。
    """
    if not text:
        return ()
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        if len(line) > max_chars:
            if current:
                pieces.append("\n".join(current))
                current = []
                size = 0
            for start in range(0, len(line), max_chars):
                pieces.append(line[start:start + max_chars])
            continue
        if current and size + 1 + len(line) > max_chars:
            pieces.append("\n".join(current))
            current = []
            size = 0
        size += len(line) + (1 if current else 0)
        current.append(line)
    if current:
        pieces.append("\n".join(current))
    return tuple(pieces)


def build_chunks(
    document_id: str,
    blocks: tuple[ParsedBlock, ...],
    *,
    parent_max_chars: int = PARENT_MAX_CHARS,
    child_max_chars: int = CHILD_MAX_CHARS,
) -> tuple[Chunk, ...]:
    """把一份文档的块切成 parent + child，顺序是"每个 parent 后面跟它的 children"。"""
    chunks: list[Chunk] = []
    for group in _group_into_parents(blocks, parent_max_chars):
        location = _parent_location(group)
        text = _group_text(group)
        parent_id = _chunk_id(document_id, location, 0)
        chunks.append(Chunk(
            chunk_id=parent_id,
            parent_id=parent_id,
            document_id=document_id,
            location=location,
            text=text,
            content_hash=_content_hash(text),
            kind=group[0].kind,
            level=PARENT,
        ))
        for ordinal, child_text in enumerate(
            _split_into_children(text, child_max_chars), start=1,
        ):
            chunks.append(Chunk(
                chunk_id=_chunk_id(document_id, location, ordinal),
                parent_id=parent_id,
                document_id=document_id,
                location=f"{location}#{ordinal}",
                text=child_text,
                content_hash=_content_hash(child_text),
                kind=group[0].kind,
                level=CHILD,
            ))
    return tuple(chunks)


def embedding_requests(chunks: tuple[Chunk, ...]) -> tuple[EmbeddingRequest, ...]:
    """要送去向量化的内容，按 content_hash 去重，保持首次出现的顺序。

    去重是纯赚的：页眉页脚、重复的表格标题、同一段话在两页上出现，都会各自变成一次网络
    请求和一份账单。顺序固定则让"重试哪一批"是可复现的。
    """
    seen: set[str] = set()
    requests: list[EmbeddingRequest] = []
    for chunk in chunks:
        if chunk.level != CHILD or not chunk.text:
            continue
        if chunk.content_hash in seen:
            continue
        seen.add(chunk.content_hash)
        requests.append(EmbeddingRequest(content_hash=chunk.content_hash, text=chunk.text))
    return tuple(requests)


def plan_reindex(
    previous: tuple[Chunk, ...], current: tuple[Chunk, ...],
) -> ReindexPlan:
    """比较两个版本，说清哪些要新增向量、哪些要删、哪些原样复用。

    文档被删除时 `current` 是空元组，于是每一个 chunk 都落进 `removed`——删除传播就是
    这条路径，不需要另写一套逻辑。
    """
    previous_by_id = {chunk.chunk_id: chunk for chunk in previous}
    current_by_id = {chunk.chunk_id: chunk for chunk in current}

    added = tuple(
        chunk
        for chunk in current
        if chunk.chunk_id not in previous_by_id
        or previous_by_id[chunk.chunk_id].content_hash != chunk.content_hash
    )
    removed = tuple(sorted(set(previous_by_id) - set(current_by_id)))
    unchanged = tuple(
        chunk.chunk_id
        for chunk in current
        if chunk.chunk_id in previous_by_id
        and previous_by_id[chunk.chunk_id].content_hash == chunk.content_hash
    )
    return ReindexPlan(
        added=added,
        removed=removed,
        unchanged=unchanged,
        embeddings_needed=embedding_requests(added),
    )
