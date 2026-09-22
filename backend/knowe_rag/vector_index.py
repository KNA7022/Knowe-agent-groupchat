"""向量检索：精确余弦扫描。

plan 第 50 行写的是 USearch。**这里没有用它**，理由是测量出来的而不是偏好：

* `usearch` 的 wheel 只有 0.33 MB，但它 `assert isinstance(vectors, np.ndarray)`——numpy
  是两种方案共同的前提（实测冻结增量 **+14.92 MB**，usearch 只多 0.33 MB）；
* 于是 usearch 多买到的只有**近似**，而近似的代价是召回率。验收指标 Recall@10 ≥ 0.85 在
  精确扫描下是免费的；
* 在本产品的规模下（个人知识库，几千到几万块），精确扫描一次查询是几十毫秒，而一次 LLM
  调用是秒级——省下的那点时间买不回来任何东西。

要换成 ANN 的话，加一个后端类 + 一个依赖，`search` 的契约不变。

维度是**构造参数**而不是从第一个向量推断：维度属于索引的身份（plan 里"模型 id 与维度
都是索引身份的一部分"），悄悄接受一个不同维度的向量意味着索引里混着两个模型的语义。
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from .store import StoreError

__all__ = [
    "VectorIndex",
    "VectorIndexError",
]


class VectorIndexError(StoreError):
    """这次向量操作不能做：维度不对，或者索引不处于能接受它的状态。"""


class VectorIndex:
    """一个可增删的精确余弦索引。

    内部是一块预分配的 `(capacity, dimensions)` float32 矩阵加一个 id 列表。预分配而不是
    每次 `np.vstack`：后者每加一条就复制整个矩阵，导入一份几万块的知识库会退化成 O(n²)。
    """

    def __init__(self, *, dimensions: int) -> None:
        if dimensions < 1:
            raise VectorIndexError(f"维度必须是正整数，收到 {dimensions}")
        self._dimensions = int(dimensions)
        self._capacity = 0
        self._count = 0
        self._matrix = np.zeros((0, self._dimensions), dtype="float32")
        self._keys: list[str] = []
        self._rows: dict[str, int] = {}

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def __len__(self) -> int:
        return self._count

    def _ensure(self, needed: int) -> None:
        if needed <= self._capacity:
            return
        grown = max(needed, max(8, self._capacity * 2))
        block = np.zeros((grown, self._dimensions), dtype="float32")
        if self._count:
            block[: self._count] = self._matrix[: self._count]
        self._matrix = block
        self._capacity = grown

    def add(self, chunk_id: str, vector: Sequence[float]) -> None:
        """加入或**替换**一条向量。

        `NaN` 与无穷被拒绝：它们会让**整行**比较失效——一条 NaN 向量不会只影响自己，它
        会让所有余弦分数变成 NaN，于是检索整体返回一个看起来"没有结果"的答案。
        """
        values = np.asarray(vector, dtype="float32")
        if values.shape != (self._dimensions,):
            raise VectorIndexError(
                f"向量维度是 {values.shape}，索引期望 ({self._dimensions},)"
            )
        if not np.isfinite(values).all():
            raise VectorIndexError("向量里有 NaN 或无穷，它会污染整次检索的分数")
        row = self._rows.get(chunk_id)
        if row is None:
            self._ensure(self._count + 1)
            row = self._count
            self._keys.append(chunk_id)
            self._rows[chunk_id] = row
            self._count += 1
        self._matrix[row] = values

    def remove(self, chunk_id: str) -> bool:
        """移出一条向量；它本来就不在索引里时返回 False。

        用**末位填补**而不是 `np.delete`：后者每删一条复制整个矩阵，删除一份文档的全部
        块会退化成 O(n²)。填补的代价是行号会移动，所以最后那条记录的 `_rows` 也必须跟着
        改——不改的话，被搬走的那条从此指向别人的向量，而检索照常返回结果。
        """
        row = self._rows.pop(chunk_id, None)
        if row is None:
            return False
        last = self._count - 1
        if row != last:
            self._matrix[row] = self._matrix[last]
            moved = self._keys[last]
            self._keys[row] = moved
            self._rows[moved] = row
        self._keys.pop()
        self._count = last
        return True

    def search(
        self, vector: Sequence[float], *, limit: int,
    ) -> tuple[tuple[str, float], ...]:
        """按余弦相似度返回 `(chunk_id, score)` 降序；同分按 chunk_id 升序。

        排序在 Python 里做而不是 `np.argsort`：同分时要按 id 定序，而 numpy 的排序不保证
        稳定，`argsort` 加上稳定的 `kind` 也只保证"输入顺序"，不保证"id 顺序"。对个人知识
        库的规模，这点开销远小于一次网络调用。

        维度校验放在空索引判断**之前**：反过来的话，空索引 + 错误维度会静默返回空结果，
        调用方看到的是"库里没有相关内容"——一个维度错误伪装成了一次没有命中。
        """
        query = np.asarray(vector, dtype="float32")
        if query.shape != (self._dimensions,):
            raise VectorIndexError(
                f"查询向量维度是 {query.shape}，索引期望 ({self._dimensions},)"
            )
        if limit <= 0 or self._count == 0:
            return ()
        block = self._matrix[: self._count]
        query_norm = float(np.linalg.norm(query))
        norms = np.linalg.norm(block, axis=1) * query_norm
        dots = block @ query
        # 零向量（或零查询）没有方向，余弦无定义。给 0 分而不是让它除出 NaN/inf：一条
        # 零向量不该让整次检索的分数全部污染。
        similarities = np.divide(
            dots, norms, out=np.zeros_like(dots), where=norms > 0,
        )
        ranked = sorted(
            (
                (self._keys[index], float(similarities[index]))
                for index in range(self._count)
            ),
            key=lambda pair: (-pair[1], pair[0]),
        )
        return tuple(ranked[:limit])

    def keys(self) -> tuple[str, ...]:
        """索引里现有的 id（顺序不保证，仅用于诊断与测试）。"""
        return tuple(self._keys)
