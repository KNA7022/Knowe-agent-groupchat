# 独立复验：第十次复审 7 条的修复（`1a82b2b` + `e95f423`）

- 覆盖修订：`1a82b2b`（F2 四条修复）、`e95f423`（台账）
- 复验时的 HEAD：`8a77adb`（复验期间另一个 agent 在同一棵树上提交了 Task 4 的起点）
- 结论：**0 Critical / 3 Important / 4 Minor → Task 3 不能记为验收通过**
- 复验脚本（`D:\agentwork\`）：`attack_f2.py`、`check_xlsx_raw.py`、`check2.py`、`check_tail.py`、
  `check3_mutation.py`、`m2_probe.py`
- 环境事实：基线 `py -3.12 -m pytest`（17 个 RAG 文件）→ `335 passed in 7.42s`；复验结束时同一命令
  仍 `335 passed`，`git status --porcelain` 无输出（mutation 全部从内存快照恢复并逐一核验）

> ⚠ **并发写入**：复验期间另一个 agent 在同一棵树上把 HEAD 从 `e95f423` 推到 `8a77adb`（Task 4
> 起点，新增 `backend/knowe_memory/` 与 `test_memory_contracts.py`）。复验者核对过
> `git diff e95f423 8a77adb`：没有触碰 `commercial_gateway.py` 或 `knowe_rag/*`，所以全部结果对
> 当前 HEAD 同样成立。**这次是运气**——见本轮台账里为此记下的流程改动。

---

## 检查 1：F2 四条是否真的消除（攻击者视角，非读 diff）

做法：`MasterKey(key_material=os.urandom(32))` + `EncryptedKnowledgeStore` 真建库 →
`build_chunks` 写入特征中文 → 另用 `parse_document` 解析真实 xlsx → 关闭后用 `sqlite3`
无主密钥直读**每一个明文列**。

**F2-3 指纹：已消除。** `[A1] sha256 穷举 400000 次命中：None`、`[A2] 已知文档 sha256 命中：False`、
`[A5] 两把密钥的 HMAC 相同：False`。
残余（Minor-1）：同库同文本共享指纹，实测 `54c545a03b72 -> ['page:4','page:4#1','page:5','page:5#1']`
——泄漏"这两页文字一样"。

**F2-1 location：xlsx 那一支已消除。**

```
sheet    sheet:1!A1:B2        '初二(3)班期末成绩单'
  location='sheet:1!A1:B2'  content_hash='2b95171e…'  text='<BLOB 79>'
  整个库文件的字节里出现 '期末成绩单'：False
  整个库文件里出现 '张三'：False / '初二(3)班'：False
  密文侧确实带着表名（解出来才看得到）：True
```

**location 生成点重数**：`parsers.py` 恰好 **12 处**（L84 纯文本、L125 markdown code、L136 h、
L151 table、L167 para、L229-232 HTML h/list/para、L358/363/370 docx、L400 pptx、L586 xlsx、L642 pdf），
另有 `chunks.py` 的 `_parent_location`（`first..last`）与 child `#n` 后缀；全仓库只有这两处产生
location。**其中 L125 仍把用户正文写进明文 location → Important-3。**

**F2-2 document_id：没有消除**，但台账已明写"约定而非强制"，不计新账。实测用
`C:\Users\25977\Desktop\初二(3)班期末成绩单-张三.xlsx` 当 `document_id` 写入**成功、无任何报错**，
`rag_documents.document_id` 就是该路径。新信息见 Minor-2。

**F2-4**：docstring 现在如实列出每个明文列，但清单漏了"同库同文本共享指纹"这一条（Minor-1）。

---

## Important-1 换主密钥时 `write_document` 静默毁库（`1a82b2b` 引入的回归）

密钥 A 建库写入 → 关闭 → 密钥 B 打开同一库：

```
[B 打开] read_chunks         -> StoreError: text 读不出来（记录 141b3653…）：密文没有通过认证
[B 打开] missing_embeddings  -> 静默成功：<tuple len=2>
[B 打开] write_document(同内容) -> 静默成功：ReindexPlan(added=(Chunk(chunk_id='141b3653…'), …))
>>> B 写完之后库里的 (chunk_id, 指纹前12)：141b3653 83fd318f / e503af8f 83fd318f / …
[A 重新打开] read_chunks -> StoreError: text 读不出来（记录 141b3653…）
```

- `read_chunks` 报错（正确），`missing_embeddings` 静默说谎，`write_document` **静默成功并按
  chunk_id 就地重写**全部 4 行的 `content_hash` 与密文 → A 的原文**不可恢复**。
- 不是"第三套指纹"：chunk_id 由 `(document_id, location, 序号)` 决定、与密钥无关，所以是**就地
  覆盖**（行数不变）；两套并存只发生在 `rag_embeddings`（PK=(model,content_hash)，两把钥匙各写
  一遍 → 2 段文字变成 4 行）。
- **是回归**：旧实现 `current = {chunk.chunk_id: chunk.content_hash for chunk in chunks}` 用的是
  调用方的**无盐**指纹，换钥匙后 diff 全是"未变"→ 不写、不破坏；绑密钥后 diff 全变成"新增"→
  全量重写。
- 正确行为是**开库即校验主密钥**。当时是读报错、写静默接管、盘点静默说谎，三处互不一致。

**用户影响**：主密钥轮换或丢失后重新生成密钥、再导入一次资料，就会把旧库彻底洗掉，且在写入
那一刻没有任何报错。

## Important-2 `test_a_successful_vision_call_reconciles_the_reservation` 是空转的

`check3_mutation.py`：对 7 个点做精确文本删除，**每次在子进程里**跑
`test_rag_vision_egress.py` + `test_rag_embedding_egress.py`（35 个测试），跑完从内存快照恢复。

| # | mutation | 结果 |
|---|---|---|
| M1 | vision 成功 `_complete_authorized_effect` | ✅ 抓到 |
| **M2** | **vision 成功 `_reconcile_provider_usage`** | ❌ **`35 passed` —— 空转** |
| M3 | vision 失败 `_reconcile_provider_usage` | ✅ 抓到 |
| M4 | embed 成功 `_reconcile_provider_usage` | ✅ 抓到 |
| M5 | embed 失败 `_reconcile_provider_usage` | ✅ 抓到 |
| M6 | embed 取消 `_reconcile_provider_usage` | ✅ 抓到 |
| M7 | `+ shape.extra_input_tokens`（差值断言） | ✅ 抓到 |

机制（`m2_probe.py`，mutation 必须落在 import 之前）：

```
--- vision 成功路径的原样写法（max_retries=0）---
    budget_updated usage = {'model_turns': 1, …}
    >>> 断言 any(model_turns == 1) = True          ← 恒真
--- 若设 max_retries=3 ---
    budget_updated usage = {'model_turns': 4, …}   ← 只剩预留那一条，settle 没了
    >>> 断言 any(model_turns == 1) = False         ← 会咬住
```

原因：`_client` 默认 `max_retries=0` → 预留 `1+0 = 1`，而 `_reconcile_provider_usage` 在
`actual_usage == reserved_usage` 时直接 return → 成功调用根本不产生 settle 事件，断言命中的是
授权期那次 `consume_budget`。embed 成功那条测试的 docstring 自己记着同一个坑（已用
`max_retries=3`），**vision 这条没跟上**。

生产影响：`CONFIG.provider_max_retries` 默认 2 → 视觉调用预留 3 轮/3 份成本，这一行将来被删或
挪走，每次成功调用永久多扣 3 倍而测试不会响。

## Important-3 markdown 围栏的 info string 仍把用户正文写进明文 location

输入 `笔记.md`：```` ```期末成绩单与张三的分数 ```` + 内容：

```
kind=code  location='code:1:期末成绩单与张三的分数'
>>> 「期末成绩单」出现在块文本里吗：False
>>> 无密钥直读到的 location 列：['code:1:期末成绩单与张三的分数..code:2:python', …]（父块也带）
```

`_MD_FENCE = r"^```(\S*)\s*$"` 的 group(1) 可以是任何非空白 token；围栏正文从下一行才开始，
所以这段文字**既在明文列里泄漏、又永远检索不到**——与 F2-1 修掉的缺陷完全同型。
`parse_location('code:1:期末成绩单与张三的分数')` 照常放行，没有任何一层会拦。

---

## Minor（4 条）

- **Minor-1** 同库内相同文本共享指纹 → 泄漏"这些位置文字一样"（去重必需；建议补进 store.py 的泄漏清单）。
- **Minor-2** `document_identifier()` 自己是**无盐、无密钥**的 sha256 前缀，仍是一台针对路径的
  确认机：用一份候选路径清单命中 `doc_5fb69fc6… -> C:\Users\25977\Desktop\初二(3)班期末成绩单-张三.xlsx`。
  另外 chunk_id/parent_id 与密钥无关：同一文件导入两个不同主密钥的库，chunk_id 序列完全相同，
  而 content_hash 不相交。
- **Minor-3** `knowe_rag/__init__.py:21,120` 把**模块级** `embedding_requests` 导出成包级 API
  （`default_content_hash` 也在 `__all__`），恰好引导调用方用错那一个：用包顶层那个存完向量后
  `missing_embeddings` 永远报缺 2 个，`rag_embeddings` 变成 4 行（真实内容 2 段）→ 每次导入重复
  向量化、重复计费，而结果看起来完全正常。
- **Minor-4** xlsx 序号是位置性的：两表对调后 `sheet:1!A1:B2` 指向另一张表；三表合并后编号也会
  错位。表名已进密文可作二次校验、且精确引用尚未落地，故判 Minor。

## 未发现问题的两项（负面证据）

- **`dataclasses.replace` 对 `@dataclass(frozen=True, slots=True)` 正确**：`__slots__` 八项齐、
  类型仍是 `Chunk`、其余字段逐一相等、二次 seal 幂等。
- **往返一致性**：`read_chunks(` 在生产代码里**零调用点**；10 处调用全在 `test_rag_store.py`，
  逐条判断后没有一处依赖"读回来的等于写进去的"。

## 信心与浅度

- 最有信心：**检查 3**（7 个点全部实际删除并在新进程里跑过测试，M2 的机制用独立探针定量证明）。
- 做得最浅：检查 1 里"还有别的 location 生成点带用户文字吗"的**完备性**——只覆盖了 `parsers.py`
  的 12 处与 `chunks.py` 的两处构造点，没有穷举七种格式各自的正则/属性边界（例如 HTML 属性、
  docx 样式名是否可能渗进 location）。检查 2 的换密钥场景只测了"同一路径同一内容"，没测"用错
  钥匙写另一份文档"。

## 建议的最小修复集

1. 开库校验主密钥并拒绝写（Important-1）
2. 围栏 info string 挪进块文本（Important-3）
3. vision 成功那条测试加 `max_retries=3`（Important-2）
4. 顺带把 Minor-3 的包级导出收口
