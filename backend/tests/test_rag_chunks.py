"""分块：把解析出的块变成可检索、可引用、可增量维护的 chunk。

plan 第 48 行要求 child/parent 两级分块、稳定 ID、去重、增量重建与删除传播。这一层的每个
决定都在回答同一个问题：**改动一份文档之后，哪些引用还有效、哪些向量必须重算。**

- parent 是给模型看的上下文单元（一页、一张表、一个标题下的正文）；
- child 是检索命中的单元，位置指回 parent（`page:3#2`）；
- child 才送去 embedding，parent 不送——parent 通常太长，且已被它的 child 覆盖；
- id 由**文档 + 位置 + 序号**决定，内容指纹单独存：位置决定引用能不能对上，指纹决定要不
  要重新调 embedding。

id 里带位置是有代价的（在中间插入一段会让它下游的 id 全变），这条代价写在
`test_inserting_a_paragraph_shifts_the_ids_after_it` 里，而不是假装 id 与顺序无关。
"""

from __future__ import annotations

import pytest

from backend.knowe_rag import ParsedBlock
from backend.knowe_rag.chunks import (
    CHILD_MAX_CHARS,
    PARENT_MAX_CHARS,
    EmbeddingRequest,
    Chunk,
    build_chunks,
    embedding_requests,
    plan_reindex,
)


def _block(text: str, location: str, kind: str = "paragraph") -> ParsedBlock:
    return ParsedBlock(text=text, location=location, kind=kind)


def _page(number: int, lines: list[str]) -> ParsedBlock:
    return ParsedBlock(text="\n".join(lines), location=f"page:{number}", kind="page")


def _children(chunks: tuple[Chunk, ...]) -> tuple[Chunk, ...]:
    return tuple(chunk for chunk in chunks if chunk.level == "child")


def _parents(chunks: tuple[Chunk, ...]) -> tuple[Chunk, ...]:
    return tuple(chunk for chunk in chunks if chunk.level == "parent")


def _id_of(chunks: tuple[Chunk, ...], text: str, level: str = "child") -> str:
    for chunk in chunks:
        if chunk.text == text and chunk.level == level:
            return chunk.chunk_id
    raise AssertionError(f"no {level} chunk with text {text!r} in {[c.text for c in chunks]}")


# ── 分组 ──────────────────────────────────────────────────────────────

def test_a_standalone_block_becomes_one_parent_with_one_child() -> None:
    chunks = build_chunks("doc", (_page(1, ["甲"]), _page(2, ["乙"])))

    parents = _parents(chunks)
    assert [p.location for p in parents] == ["page:1", "page:2"]
    assert [p.kind for p in parents] == ["page", "page"]
    assert [c.location for c in _children(chunks)] == ["page:1#1", "page:2#1"]


def test_markdown_headings_group_the_blocks_under_them() -> None:
    """标题是分组边界：它下面的正文属于它，而不是各成一个 parent。

    否则"这一节讲了什么"在索引里根本不是一个可检索的单位——每个段落各成一块，节标题
    和它的正文之间没有任何关系被记录下来。
    """
    blocks = (
        _block("第一章", "h1:1", "heading"),
        _block("导语。", "para:1"),
        _block("更多。", "para:2"),
        _block("1.1 小节", "h2:1", "heading"),
        _block("正文。", "para:3"),
    )

    chunks = build_chunks("doc", blocks)

    parents = _parents(chunks)
    assert [p.location for p in parents] == ["h1:1..para:2", "h2:1..para:3"]
    assert [p.text for p in parents] == ["第一章\n导语。\n更多。", "1.1 小节\n正文。"]
    assert [p.kind for p in parents] == ["heading", "heading"]


def test_a_parent_stops_growing_at_the_size_limit() -> None:
    """没有标题的长文档也要在超过上限时断开。

    不断开的话整份 TXT 会变成一个 parent，"证据"就退化成"整个文件"，引用也就指不到
    具体位置了。
    """
    piece = "x" * (PARENT_MAX_CHARS // 3)
    blocks = tuple(_block(piece, f"para:{n}") for n in range(1, 5))

    parents = _parents(build_chunks("doc", blocks))

    assert len(parents) > 1
    assert all(len(p.text) <= PARENT_MAX_CHARS for p in parents)


def test_a_parent_that_is_exactly_at_the_limit_is_not_split() -> None:
    """上限是"不超过"，不是"小于"：恰好等于上限时不该再断开。

    差一个字符就切开，块的大小会随实现里"分隔符算不算一个字符"这种细节漂移，于是同一份
    文档在两版实现里得出不同的分块，增量重建全部失效。
    """
    head = (PARENT_MAX_CHARS - 1) // 2
    blocks = (_block("y" * head, "para:1"), _block("y" * (PARENT_MAX_CHARS - 1 - head), "para:2"))

    parents = _parents(build_chunks("doc", blocks))

    assert len(parents) == 1
    assert len(parents[0].text) == PARENT_MAX_CHARS


# ── child 切分 ────────────────────────────────────────────────────────

def test_a_short_parent_has_exactly_one_child_holding_the_same_text() -> None:
    chunks = build_chunks("doc", (_page(1, ["很短的一页"]),))

    children = _children(chunks)
    assert len(children) == 1
    assert children[0].text == "很短的一页"


def test_long_text_is_split_into_children_only_at_line_boundaries() -> None:
    """切点必须落在行边界上。

    从句子中间切开会让检索命中一段半句话，引用回显也接不上；而且行边界是确定性的，字符
    偏移量不是——同一段文字在不同的分块参数下会切出不同的句子。
    """
    lines = [f"第{index}行" + "z" * 60 for index in range(1, 40)]
    chunks = build_chunks("doc", (_page(1, lines),))

    children = _children(chunks)
    assert len(children) > 1
    for child in children:
        assert set(child.text.splitlines()) <= set(lines), child.text
        assert len(child.text) <= CHILD_MAX_CHARS


def test_a_single_line_longer_than_the_child_limit_is_cut_and_kept_whole() -> None:
    """一行比 child 上限还长（比如一张表的 TSV 行）时必须硬切，且**不能丢字符**。

    不切的话这个 child 会比上限大出任意多；丢掉尾巴则是最坏的一种——检索永远看不到
    后面的内容，而没有任何地方显示少了东西。
    """
    line = "q" * (CHILD_MAX_CHARS * 2 + 7)
    chunks = build_chunks("doc", (_page(1, [line]),))

    children = _children(chunks)
    assert len(children) == 3, [len(child.text) for child in children]
    assert all(len(child.text) <= CHILD_MAX_CHARS for child in children)
    assert "".join(child.text for child in children) == line


def test_a_parent_with_no_text_has_no_children() -> None:
    """没有文本层的页（扫描件）不能产生 child。

    空文本的 child 会被送去 embedding，那是一次没有内容的网络请求；更糟的是它会在索引里
    留下一条永远匹配不上、也永远删不干净的记录。
    """
    chunks = build_chunks("doc", (ParsedBlock(text="", location="page:1", kind="image_page"),))

    parents = _parents(chunks)
    assert len(parents) == 1 and parents[0].location == "page:1"
    assert _children(chunks) == ()


# ── 位置 ──────────────────────────────────────────────────────────────

def test_a_child_location_points_back_at_its_parent() -> None:
    lines = ["w" * 80] * 30
    chunks = build_chunks("doc", (_page(7, lines),))

    parent = _parents(chunks)[0]
    for child in _children(chunks):
        assert child.location.startswith(parent.location + "#")
        assert child.parent_id == parent.chunk_id


# ── 稳定 ID 与内容指纹 ────────────────────────────────────────────────

def test_chunk_ids_are_stable_across_rebuilds() -> None:
    blocks = (_page(1, ["甲"]), _page(2, ["乙"]))

    assert build_chunks("doc", blocks) == build_chunks("doc", blocks)


def test_two_documents_with_the_same_text_get_different_ids() -> None:
    """同一个位置、同一段文字，来自两份文档时必须是两条记录。

    否则删除其中一份文档会把另一份的内容一起带走——而且是在"我删的是那份没用的"这种
    完全合理的操作之后发生的。
    """
    blocks = (_page(1, ["相同内容"]),)

    first = build_chunks("doc-a", blocks)
    second = build_chunks("doc-b", blocks)

    assert {c.chunk_id for c in first}.isdisjoint({c.chunk_id for c in second})


def test_identical_text_at_two_locations_shares_one_content_hash() -> None:
    """内容指纹只看文本，不看位置：这是"去重"能省下 embedding 调用的全部依据。"""
    chunks = build_chunks("doc", (_page(1, ["重复的页眉"]), _page(2, ["重复的页眉"])))

    children = _children(chunks)
    assert len(children) == 2
    assert children[0].chunk_id != children[1].chunk_id
    assert children[0].content_hash == children[1].content_hash


def test_inserting_a_page_shifts_the_ids_after_it() -> None:
    """在中间插入一页，它下游所有 chunk 的 id 都会变——这是**已知代价**，不是 bug。

    id 里带位置，是为了让"引用 → 原文位置"这条链路不依赖任何外部映射表；只按内容生成 id
    则两处相同文字会撞成同一个。代价是编辑会让下游重新 embedding，如实写在这里，而不是
    假装 id 与顺序无关。

    用 `page` 而不是 `paragraph` 做夹具：段落会合并进同一个 parent，就不是"两个独立单元
    相对移位"这件事了。
    """
    before = build_chunks("doc", (_page(1, ["甲"]), _page(2, ["丙"])))
    after = build_chunks("doc", (_page(1, ["甲"]), _page(2, ["乙"]), _page(3, ["丙"])))

    # 甲 还在第 1 页，它的引用继续有效；
    assert _id_of(before, "甲") == _id_of(after, "甲")
    # 丙 被挤到第 3 页，引用必须换成新 id——旧 id 在新版本里不再指向任何东西。
    assert _id_of(before, "丙") != _id_of(after, "丙")


# ── 去重 ──────────────────────────────────────────────────────────────

def test_identical_text_is_embedded_once() -> None:
    chunks = build_chunks("doc", (_page(1, ["重复"]), _page(2, ["重复"]), _page(3, ["独特"])))

    requests = embedding_requests(chunks)

    assert [r.text for r in requests] == ["重复", "独特"]


def test_parents_are_never_embedded() -> None:
    """parent 不进向量索引：它太长，而且内容已被它的 child 覆盖。

    送进去会让每次查询都为同一段文字多付一次钱，还会让 parent 和 child 在同一次检索里
    互相竞争，挤掉本该由别处提供的那条证据。

    夹具必须让 parent 的文本**不等于**任何单个 child：只有一个 child 时两者文本相同、
    指纹也相同，那种情况下"排除了 parent"和"去重了 child"在结果上完全一样，测不出东西。
    """
    lines = ["p" * 300, "q" * 300]
    chunks = build_chunks("doc", (_page(1, lines),))

    parents = _parents(chunks)
    children = _children(chunks)
    assert len(children) == 2, [c.text for c in chunks]

    embedded = {r.content_hash for r in embedding_requests(chunks)}

    assert embedded == {c.content_hash for c in children}
    assert parents[0].content_hash not in embedded


def test_pages_without_text_are_not_embedded() -> None:
    chunks = build_chunks("doc", (
        _page(1, ["有字"]),
        ParsedBlock(text="", location="page:2", kind="image_page"),
    ))

    assert [r.text for r in embedding_requests(chunks)] == ["有字"]


def test_an_empty_child_is_never_embedded() -> None:
    """手工构造一条空文本 child：分块器现在不会产出它，但重建索引时可能从**更早版本写下
    的记录**里读到一条。

    空文本送向量化是一次没有内容的网络请求，而且它会在索引里留下一条永远匹配不上、也
    永远说不清该不该删的记录。所以这条保证写在 `embedding_requests` 自己的契约里，不能
    只依赖"上游不会那样构造"。
    """
    empty = Chunk(
        chunk_id="deadbeef", parent_id="deadbeef", document_id="doc",
        location="page:1#1", text="", content_hash="0" * 64, kind="page", level="child",
    )

    assert embedding_requests((empty,)) == ()


def test_embedding_requests_keep_the_order_of_first_appearance() -> None:
    """顺序决定批量请求长什么样，也就决定重试要重发哪一批；乱序会让两次运行的结果不可比。"""
    chunks = build_chunks("doc", (_page(1, ["丙"]), _page(2, ["甲"]), _page(3, ["丙"])))

    assert [r.text for r in embedding_requests(chunks)] == ["丙", "甲"]


# ── 增量重建与删除传播 ────────────────────────────────────────────────

def test_an_unchanged_document_needs_no_embedding_work() -> None:
    blocks = (_page(1, ["甲"]), _page(2, ["乙"]))
    before = build_chunks("doc", blocks)

    plan = plan_reindex(before, build_chunks("doc", blocks))

    assert plan.added == ()
    assert plan.removed == ()
    assert plan.embeddings_needed == ()
    assert len(plan.unchanged) == len(before)


def test_a_changed_paragraph_is_re_embedded_and_nothing_else_is() -> None:
    before = build_chunks("doc", (_page(1, ["甲"]), _page(2, ["乙"])))
    after = build_chunks("doc", (_page(1, ["甲"]), _page(2, ["乙改"])))

    plan = plan_reindex(before, after)

    assert [r.text for r in plan.embeddings_needed] == ["乙改"]
    assert plan.removed == ()
    still_here = [c.chunk_id for c in before if c.location == "page:1#1"]
    assert still_here[0] in plan.unchanged


def test_removing_a_document_removes_every_chunk_it_had() -> None:
    """删除传播：索引里不能留下任何指向已删文档的 chunk。

    留下的那一条依然带着原文文字，于是"我删掉了那份文件"之后，答案还能把它引出来——
    这是删除功能最不能出的错。
    """
    before = build_chunks("doc", (_page(1, ["甲"]), _page(2, ["乙"])))

    plan = plan_reindex(before, ())

    assert set(plan.removed) == {c.chunk_id for c in before}
    assert plan.added == ()
    assert plan.embeddings_needed == ()
    assert plan.unchanged == ()


def test_a_removed_page_is_reported_as_removed_and_not_only_as_missing() -> None:
    before = build_chunks("doc", (_page(1, ["甲"]), _page(2, ["乙"])))
    after = build_chunks("doc", (_page(1, ["甲"]),))

    plan = plan_reindex(before, after)

    page_two = [c.chunk_id for c in before if c.location.startswith("page:2")]
    assert page_two, "the fixture must actually contain page 2"
    assert set(page_two) <= set(plan.removed)
    assert plan.embeddings_needed == ()


def test_removal_is_decided_by_identity_not_by_content() -> None:
    """删除要按 chunk 的**身份**判断，不能按内容判断。

    同一段文字出现在多处是常事（页眉、引用、同一道题出现在两套卷子里）。按内容判断会同时
    犯两个方向相反的错：把"位置没变、内容改了"的记录当成删除（它其实只需就地替换），又把
    "这一页已经删了、但同样一句话在别处还有"的记录留下——后者正是删除传播最不能出的错，
    留下那一条，答案还能把已删文件里的文字引出来。

    夹具必须让内容与身份**分叉**：一份文档里"甲"改成"乙"、"乙"那一页整页消失。如果被删的
    内容恰好是唯一内容，两种判断会给出同样的答案，测不出区别。
    """
    before = build_chunks("doc", (_page(1, ["甲"]), _page(2, ["乙"])))
    after = build_chunks("doc", (_page(1, ["乙"]),))

    plan = plan_reindex(before, after)

    page_two = {c.chunk_id for c in before if c.location.startswith("page:2")}
    assert page_two, "the fixture must actually contain page 2"
    assert page_two <= set(plan.removed), plan.removed

    page_one = {c.chunk_id for c in before if c.location.startswith("page:1")}
    assert page_one.isdisjoint(plan.removed), plan.removed


def test_an_empty_document_has_no_chunks_at_all() -> None:
    assert build_chunks("doc", ()) == ()
    assert plan_reindex((), ()) == plan_reindex((), ())


def test_the_plan_reports_added_chunks_with_their_text() -> None:
    """增量重建要能直接喂给 embedding：只给 id 的话调用方还得回去查文本。"""
    plan = plan_reindex((), build_chunks("doc", (_page(1, ["新内容"]),)))

    assert [r.text for r in plan.embeddings_needed] == ["新内容"]
    assert all(isinstance(r, EmbeddingRequest) for r in plan.embeddings_needed)
    assert [c.text for c in plan.added if c.level == "child"] == ["新内容"]


# ── 确定性 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["page", "sheet", "slide", "table"])
def test_chunking_is_deterministic_for_every_standalone_kind(kind: str) -> None:
    blocks = (
        ParsedBlock(text="甲", location=f"{kind}:1", kind=kind),
        ParsedBlock(text="乙", location=f"{kind}:2", kind=kind),
    )

    assert build_chunks("doc", blocks) == build_chunks("doc", blocks)
