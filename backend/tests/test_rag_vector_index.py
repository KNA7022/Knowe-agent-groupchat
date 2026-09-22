"""向量索引的测试。

这个类里有三处会**静默**出错的地方，所以测试的重点都在那儿：

1. **末位填补**。删除时把最后一条搬到空出的行上，行号因此移动；记录行号的映射没跟着改的话，
   之后重新加入那条记录会写到一行不属于它的位置——矩阵有空位，所以不越界、不报错，只是
   检索返回别人的向量。
2. **零向量**。余弦对零向量无定义；让它除出 NaN，一条坏数据就会把所有分数变成 NaN，于是
   检索整体给出一个看起来像"没有结果"的答案。
3. **同分定序**。分数相同时不确定的顺序会让两次运行给出不同结果，而"这次第一条和上次不
   一样"会被当成模型不稳定。
"""

from __future__ import annotations

import math

import pytest

from backend.knowe_rag.vector_index import VectorIndex, VectorIndexError


def _index(*rows: tuple[str, list[float]], dimensions: int = 2) -> VectorIndex:
    index = VectorIndex(dimensions=dimensions)
    for chunk_id, vector in rows:
        index.add(chunk_id, vector)
    return index


# ── 打分 ──────────────────────────────────────────────────────────────

def test_the_closest_vector_ranks_first() -> None:
    index = _index(("near", [1.0, 0.0]), ("far", [0.0, 1.0]))

    assert [chunk_id for chunk_id, _ in index.search([1.0, 0.0], limit=2)] == ["near", "far"]


def test_a_vector_scores_one_against_itself() -> None:
    index = _index(("a", [0.6, 0.8]))

    assert dict(index.search([0.6, 0.8], limit=1))["a"] == pytest.approx(1.0)


def test_an_orthogonal_vector_scores_zero() -> None:
    index = _index(("a", [1.0, 0.0]))

    assert dict(index.search([0.0, 1.0], limit=1))["a"] == pytest.approx(0.0)


def test_direction_matters_but_length_does_not() -> None:
    """余弦只看方向。**长度不参与打分**是刻意的：embedding 的模长在不同模型、不同文本长度
    下都会变，把它算进去等于让"这条文本更长"变成一种相关性的来源。"""
    index = _index(("short", [1.0, 0.0]), ("long", [50.0, 0.0]))

    scores = dict(index.search([1.0, 0.0], limit=2))

    assert scores["short"] == pytest.approx(1.0)
    assert scores["long"] == pytest.approx(1.0)


# ── 增删改 ────────────────────────────────────────────────────────────

def test_adding_the_same_id_twice_replaces_it() -> None:
    index = _index(("a", [1.0, 0.0]))

    index.add("a", [0.0, 1.0])

    assert len(index) == 1
    assert dict(index.search([0.0, 1.0], limit=1))["a"] == pytest.approx(1.0)


def test_a_removed_vector_disappears() -> None:
    index = _index(("a", [1.0, 0.0]), ("b", [0.0, 1.0]))

    assert index.remove("a") is True

    assert [chunk_id for chunk_id, _ in index.search([1.0, 0.0], limit=5)] == ["b"]
    assert len(index) == 1


def test_removing_something_that_is_not_there_is_not_an_error() -> None:
    assert _index(("a", [1.0, 0.0])).remove("never-added") is False


def test_removal_keeps_the_moved_entry_pointing_at_its_own_vector() -> None:
    """末位填补：最后一条被搬到空出的行上，它的记录必须跟着走。

    观察方式是删掉一条之后**重新加入被搬走的那条**：记录没更新的话，写入会落到旧行号
    （矩阵有空位，不越界、不报错），而检索读到的仍是搬过来的旧向量——分数不对，但结果
    看起来完全正常。
    """
    index = _index(("a", [1.0, 0.0]), ("b", [0.0, 1.0]), ("c", [0.7, 0.7]))

    index.remove("a")             # c 从末尾被搬到 a 的行
    index.add("c", [0.1, 0.9])    # 更新 c

    scores = dict(index.search([0.1, 0.9], limit=5))
    assert scores["c"] == pytest.approx(1.0), scores
    assert "a" not in index.keys()
    assert len(index) == 2


def test_removing_the_last_entry_does_not_move_anything() -> None:
    index = _index(("a", [1.0, 0.0]), ("b", [0.0, 1.0]))

    index.remove("b")

    assert dict(index.search([1.0, 0.0], limit=5))["a"] == pytest.approx(1.0)


def test_the_index_grows_past_its_initial_capacity() -> None:
    """扩容路径要被走到，而且扩容不能弄丢或错位已有向量。

    预分配的好处只有真的扩容时才体现。每个向量与查询的夹角都不同，所以四十个分数两两不等
    ——扩容时如果某一行写错或被覆盖，分数就会重复，而那是"检索结果看着正常但内容是错的"
    这类问题里最难发现的一种。
    """
    index = VectorIndex(dimensions=4)
    for number in range(40):
        index.add(f"c{number}", [1.0, number / 40.0, 0.0, 0.0])

    assert len(index) == 40
    hits = index.search([1.0, 0.0, 0.0, 0.0], limit=40)

    assert len(hits) == 40
    assert hits[0][0] == "c0"
    assert hits[0][1] == pytest.approx(1.0)
    scores = [score for _, score in hits]
    assert scores == sorted(scores, reverse=True)
    assert len(set(scores)) == 40, "两个条目的分数相同，说明扩容时有行被覆盖或错位"


# ── 维度 ──────────────────────────────────────────────────────────────

def test_a_vector_of_the_wrong_dimension_is_refused() -> None:
    """维度对不上必须报错，不能截断或补零。

    悄悄接受意味着索引里混进了另一个 embedding 模型的语义——而向量之间的分数仍然算得出来，
    于是检索结果从此没有任何意义，也没有任何地方显示这件事。
    """
    index = VectorIndex(dimensions=3)

    with pytest.raises(VectorIndexError):
        index.add("a", [1.0, 0.0])
    with pytest.raises(VectorIndexError):
        index.add("a", [1.0, 0.0, 0.0, 0.0])


def test_a_query_of_the_wrong_dimension_is_refused() -> None:
    index = _index(("a", [1.0, 0.0]))

    with pytest.raises(VectorIndexError):
        index.search([1.0, 0.0, 0.0], limit=1)


def test_a_wrong_dimension_query_is_refused_even_on_an_empty_index() -> None:
    """空索引也要校验维度。

    校验写在"索引为空就返回空"之后的话，一个维度错误会伪装成一次没有命中：调用方看到的是
    "库里没有相关内容"，于是去查库、查数据，而问题在参数上。
    """
    with pytest.raises(VectorIndexError):
        VectorIndex(dimensions=3).search([1.0, 0.0], limit=1)


def test_a_zero_dimension_index_is_refused() -> None:
    with pytest.raises(VectorIndexError):
        VectorIndex(dimensions=0)


# ── 坏数据 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_vector_is_refused(bad: float) -> None:
    """NaN 或无穷会污染**整次**检索，而不是只影响自己。

    一条 NaN 向量参与点积之后，所有分数都变成 NaN，排序给出一个空结果——看起来像"库里没有
    相关内容"，实际是数据结构坏了。
    """
    index = VectorIndex(dimensions=2)

    with pytest.raises(VectorIndexError):
        index.add("a", [bad, 0.0])


def test_a_zero_vector_does_not_produce_nan_scores() -> None:
    """零向量没有方向，余弦无定义。给它 0 分，而不是让它把整列分数变成 NaN。"""
    index = _index(("zero", [0.0, 0.0]), ("real", [1.0, 0.0]))

    scores = dict(index.search([1.0, 0.0], limit=5))

    assert scores["zero"] == 0.0
    assert not math.isnan(scores["real"])
    assert scores["real"] == pytest.approx(1.0)


def test_a_zero_query_does_not_produce_nan_scores() -> None:
    index = _index(("a", [1.0, 0.0]))

    scores = dict(index.search([0.0, 0.0], limit=5))

    assert scores["a"] == 0.0


# ── 边界与确定性 ──────────────────────────────────────────────────────

def test_an_empty_index_returns_nothing() -> None:
    assert VectorIndex(dimensions=2).search([1.0, 0.0], limit=5) == ()


def test_a_zero_or_negative_limit_returns_nothing() -> None:
    index = _index(("a", [1.0, 0.0]))

    assert index.search([1.0, 0.0], limit=0) == ()
    assert index.search([1.0, 0.0], limit=-1) == ()


def test_the_limit_is_respected() -> None:
    index = _index(*((f"c{n}", [1.0, 0.0]) for n in range(10)))

    assert len(index.search([1.0, 0.0], limit=3)) == 3


def test_ties_are_broken_by_chunk_id() -> None:
    """同分按 id 定序：否则同样一次检索在两次运行里给出不同顺序，而"这次第一条和上次
    不一样"会被当成模型不稳定。用六个条目而不是三个，让偶然排对的概率降到 1/720。"""
    index = _index(*((name, [1.0, 0.0]) for name in ("f", "c", "a", "e", "b", "d")))

    first = index.search([1.0, 0.0], limit=10)

    assert [chunk_id for chunk_id, _ in first] == ["a", "b", "c", "d", "e", "f"]
    assert index.search([1.0, 0.0], limit=10) == first


def test_search_is_deterministic_across_rebuilds() -> None:
    rows = (("a", [0.1, 0.9]), ("b", [0.9, 0.1]), ("c", [0.5, 0.5]))
    query = [0.6, 0.4]

    assert _index(*rows).search(query, limit=3) == _index(*rows).search(query, limit=3)
