"""BM25 检索：不依赖词典的中文分词，以及一个可增删的倒排索引。

plan 第 50 行要求 BM25 召回 40 条。这里做的是**本地、零依赖**的那一半——向量那一路走网关
（`36fb324`），两路的结果由 `fusion` 合并。

两件事在这里定死，因为它们决定了索引的含义能不能稳定：

1. **分词**。中文没有空格，用字符二元组（`二次函数` → `二次`/`次函`/`函数`）。这是不依赖
   词典的经典做法：确定、零依赖，而且不会因为分词器换了版本让同一份索引的含义悄悄改变。
   代价是词表更大——但词表大只影响内存，不影响正确性。
2. **可删**。文档删除要真的把词频撤掉，否则它原来那些词仍然虚高着文档频率，IDF 一直算错
   ——而 IDF 错了不会报错，只会让检索结果慢慢变得不对。
"""

from __future__ import annotations

import pytest

from backend.knowe_rag.bm25 import Bm25Index, tokenize


# ── 分词 ──────────────────────────────────────────────────────────────

def test_chinese_is_split_into_character_bigrams() -> None:
    assert tokenize("二次函数") == ("二次", "次函", "函数")


def test_a_single_chinese_character_is_its_own_token() -> None:
    """一个字的词也要能查到。丢掉它意味着"甲"这类短词完全检索不到。"""
    assert tokenize("甲") == ("甲",)


def test_punctuation_and_whitespace_separate_chinese_runs() -> None:
    """标点是分隔符，不是内容。

    把整句当一个连续段去切 bigram，会切出 `，乙` 这种跨标点的词元——它匹配不到任何
    真实查询，只会让每份文档的词表变大、让长度归一化失真。
    """
    assert tokenize("甲，乙。 丙") == ("甲", "乙", "丙")


def test_latin_words_are_kept_whole_and_lowercased() -> None:
    assert tokenize("Quadratic Function") == ("quadratic", "function")


def test_chinese_runs_and_latin_runs_keep_their_relative_order() -> None:
    """词元的顺序不重要（BM25 只看词频），但顺序稳定让失败信息可读。

    注意"第三章"是**一段**汉字，所以按 bigram 切成"第三"/"三章"，而不是三个单字——
    单字切法会让"三章"这种跨字搭配匹配不到任何查询。
    """
    assert tokenize("第三章 3.2 节") == ("第三", "三章", "3", "2", "节")


def test_mixed_text_keeps_both_kinds_of_tokens() -> None:
    tokens = tokenize("学习 RAG 检索")
    assert "rag" in tokens
    assert "学习" in tokens and "检索" in tokens


def test_tokenization_is_deterministic() -> None:
    text = "混合 Mixed 文本 2026"
    assert tokenize(text) == tokenize(text)


# ── 排序 ──────────────────────────────────────────────────────────────

def test_a_document_containing_the_query_outranks_one_that_does_not() -> None:
    index = Bm25Index()
    index.add("hit", "二次函数的图像是一条抛物线")
    index.add("miss", "化学元素周期表")

    results = index.search("二次函数", limit=10)

    assert [doc_id for doc_id, _ in results] == ["hit"]


def test_results_are_ordered_by_score() -> None:
    index = Bm25Index()
    index.add("twice", "二次函数 二次函数 二次函数")
    index.add("once", "二次函数只是顺带提到一次，其余内容与此无关")
    index.add("never", "完全无关的内容")

    results = index.search("二次函数", limit=10)

    assert [doc_id for doc_id, _ in results] == ["twice", "once"]
    scores = [score for _, score in results]
    assert scores == sorted(scores, reverse=True)


def test_a_rare_term_outweighs_a_common_one() -> None:
    """IDF 的作用：到处都有的词不该决定排序。

    没有 IDF 的话，一份把"的"重复很多次的文档会排在真正切题的文档前面——而"的"出现在
    每一份文档里，它不携带任何区分信息。
    """
    index = Bm25Index()
    for number in range(20):
        index.add(f"common{number}", "的 的 的 的 的 的 的 的 的 的")
    index.add("rare", "判别式")

    results = index.search("的 判别式", limit=3)

    assert results[0][0] == "rare", results


def test_a_long_document_does_not_win_just_by_being_long() -> None:
    """长度归一化（b 参数）：长文档命中一个词，不该压过短文档命中同一个词。

    不做归一化时，一篇一万字的文章只要顺带提过一次查询词，就能靠词频压过一句精准的短句。
    """
    index = Bm25Index()
    index.add("short", "判别式")
    index.add("long", "判别式 " + "无关内容 " * 400)

    results = index.search("判别式", limit=10)

    assert results[0][0] == "short", results


def test_only_documents_containing_a_query_token_are_returned() -> None:
    index = Bm25Index()
    index.add("a", "甲")
    index.add("b", "乙")

    assert [doc_id for doc_id, _ in index.search("乙", limit=10)] == ["b"]


# ── 边界 ──────────────────────────────────────────────────────────────

def test_an_empty_query_returns_nothing() -> None:
    """空查询不能返回"所有文档"——那会让一次输入事故变成一次全库泄露。"""
    index = Bm25Index()
    index.add("a", "甲")

    assert index.search("", limit=10) == ()
    assert index.search("   ，。", limit=10) == ()


def test_an_empty_index_returns_nothing() -> None:
    assert Bm25Index().search("甲", limit=10) == ()


def test_a_zero_limit_returns_nothing() -> None:
    index = Bm25Index()
    index.add("a", "甲")

    assert index.search("甲", limit=0) == ()


def test_the_limit_is_respected() -> None:
    index = Bm25Index()
    for number in range(20):
        index.add(f"doc{number}", "共同词 各自内容")

    assert len(index.search("共同词", limit=5)) == 5


def test_ties_are_broken_deterministically() -> None:
    """分数相同时按 doc_id 排序。

    否则同样一次检索在两次运行里给出不同顺序，而"为什么这次的第一条和上次不一样"会被
    当成模型不稳定——实际只是集合的迭代顺序。

    用六个文档而不是三个：不排序时结果的顺序取决于集合迭代，三个元素碰巧排对的概率是
    1/6，那会让这条测试偶尔放过一个真实的缺陷。
    """
    index = Bm25Index()
    for name in ("f", "c", "a", "e", "b", "d"):
        index.add(name, "完全相同的内容")

    first = index.search("完全相同", limit=10)

    assert [doc_id for doc_id, _ in first] == ["a", "b", "c", "d", "e", "f"]
    assert index.search("完全相同", limit=10) == first


# ── 删除 ──────────────────────────────────────────────────────────────

def test_a_removed_document_disappears_from_results() -> None:
    index = Bm25Index()
    index.add("gone", "二次函数")
    index.add("kept", "二次函数")

    assert index.remove("gone") is True

    assert [doc_id for doc_id, _ in index.search("二次函数", limit=10)] == ["kept"]


def test_removing_a_document_that_is_not_there_is_not_an_error() -> None:
    """删除是幂等的：用户在界面上点两次、或删除流程重跑一次，都不该炸。"""
    index = Bm25Index()

    assert index.remove("never-added") is False


def test_removing_a_document_moves_the_idf_of_its_terms() -> None:
    """删掉文档后，它带给其它文档的 IDF 增益必须消失。

    这是"可删"的真正内容：只把文档从结果里藏起来是不够的——它原来那些词仍然记在文档频率
    里，于是还留在索引里的文档会继续享受一个被稀释过的 IDF。结果不会报错，只是分数从此
    不对，而没有任何地方显示这件事。
    """
    index = Bm25Index()
    index.add("target", "判别式")
    for number in range(9):
        index.add(f"filler{number}", "判别式 无关")

    with_filler = index.search("判别式", limit=10)
    score_before = dict(with_filler)["target"]

    for number in range(9):
        index.remove(f"filler{number}")

    score_after = dict(index.search("判别式", limit=10))["target"]
    assert score_after > score_before, (score_before, score_after)


def test_adding_the_same_document_twice_replaces_it() -> None:
    """同一个 id 再加一次是**替换**，不是累加。

    累加会让一份文档的每个词都记两遍，于是它凭"被导入过两次"排到前面——而用户只是重新
    导入了同一份文件。观察方式是两种做法给出同一个分数：累加的话词频从 1 变 2，分数必然
    不同。
    """
    once = Bm25Index()
    once.add("doc", "判别式")
    twice = Bm25Index()
    twice.add("doc", "判别式")
    twice.add("doc", "判别式")

    assert twice.search("判别式", limit=10) == once.search("判别式", limit=10)


def test_a_document_with_no_indexable_text_is_not_stored() -> None:
    """纯标点的文档进索引只会在结果里占一个名额，却永远匹配不到任何查询。"""
    index = Bm25Index()
    index.add("empty", "，。！？")
    index.add("real", "甲")

    assert [doc_id for doc_id, _ in index.search("甲", limit=10)] == ["real"]


def test_removing_a_document_that_had_no_text_is_not_an_error() -> None:
    index = Bm25Index()
    index.add("empty", "，。")

    assert index.remove("empty") is False
