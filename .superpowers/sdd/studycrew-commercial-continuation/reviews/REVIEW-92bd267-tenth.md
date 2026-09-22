# Task 3「加密 RAG」独立复审报告（`899e61e..92bd267`）

复审者：独立 subagent（独立上下文），时间盒一轮。工作树已恢复干净（`git status --porcelain` 无输出）。

**结论摘要**：7 条发现（0 Critical / 7 Important），全部集中在检查 1 与检查 2；检查 3 未穿透。

**环境说明（重要）**：复审进行期间，**另一个 agent 向同一分支提交了 `6748536`（检索评测装置）与 `238dbb5`**，RAG 测试数由 280 变成 300。复审全程排除 `test_rag_evaluation.py` 以保持可比。仓库里那些变化不属于复审者。

固定命令（下文所有 pytest 结果都出自它）：

```powershell
cd D:\agentwork\StudyCrew-worktree
$env:PYTHONPATH = 'D:\agentwork\StudyCrew-worktree;D:\agentwork\StudyCrew-worktree\backend'
$files = (Get-ChildItem backend\tests\test_rag_*.py | Where-Object { $_.Name -ne 'test_rag_evaluation.py' } | ForEach-Object { $_.FullName })
py -3.12 -m pytest @files -q -p no:cacheprovider --no-header      # 基线：300 passed
```

mutation 施加/恢复：`py -3.12 D:\agentwork\mut.py D:\agentwork\mutations.json <i>` → 跑上面的命令 → `git checkout -- backend/commercial_gateway.py`（施加器带锚点断言，行号错了会直接报错，不会静默改错地方）。

---

## 检查 1：授权链有没有"少走一步"

**18 条 mutation：11 条被测试抓住，7 条全绿（发现）。**

先给正向证据（这些步骤是真的有锁，说明这套测试并非空转）：

| 改的地方 | 结果 |
|---|---|
| `embed` 能力要求 `embedding`→`chat`（:3035） | FAILED `test_a_provider_without_the_embedding_capability_is_refused` |
| `embed` 成功路径终态 `_complete_authorized_effect`（:3055-3058） | FAILED `...terminal_audit_event` |
| `embed` 取消路径终态（:3048） | FAILED `...cancelled_not_failed` |
| `embed` DLP payload 清空（:3037）/ DLP 判定被忽略（:2625） | FAILED `...dlp_gateway` |
| `vision` `always_requires_approval=True`→`False`（:2342） | 3 failed（含拒发校验） |
| `vision` `extra_input_tokens=0`（:2331 行的 `_vision_shape` 内，:2343） | FAILED `test_vision_reserves_output_tokens_unlike_embedding` |
| `vision` 能力 `vision`→`chat`（:2331） | 2 failed |
| `vision` payload 去掉提示词（:2335） | FAILED `...keeps_the_prompt_readable_for_dlp` |
| `vision` `reserves_output=False`（:2341） | FAILED |
| `_authorize` 对 embedding/vision 整体短路（:2560 前插入 `return None`） | **11 failed** |
| 共享的 run 绑定校验删除（:2591-2598） | 3 failed |

### F1-1（Important）vision 成功路径的终态审计整段删掉，300 个测试全绿

- **改动**：删 `backend/commercial_gateway.py:3106-3109`
  ```python
  self._complete_authorized_effect(
      authorization, error=None,
      actual_tokens=_provider_reported_total_tokens(result),
  )
  ```
- **命令/输出**：
  ```
  已施加 mutation m13 去掉 vision 成功路径的 _complete_authorized_effect：delete@3106..3109
  ### MUT 12 => 300 passed in 5.69s
  ```
- **为什么测试没抓住**：`test_rag_vision_egress.py` 里**没有任何一次成功的 vision 调用**——每个用例的 handler 都是 `_refuse_to_send`，`_authority(..., approval_id=..., grant_id=...)` 的两个参数从未被传过。也就是说 approval → claim → effect → 终态这条链在视觉路径上零端到端覆盖（embedding 有 `test_embedding_writes_an_authorized_and_a_terminal_audit_event`，vision 没有对应的）。
- **对用户的实际影响**：vision 是"扫描页（可能印着学号、成绩单、家访记录）带着图片离开这台机器"的唯一通道，每次调用都会 prepare + claim 一个 `RiskLevel.HIGH` 的 `provider_egress` invocation。终态缺失时公开审计里只有 `authorized`、没有终态：**这张图到底发出去了没有、什么时候结束、花了多少 token，事后没有任何记录可查**，高风险 invocation 停在 `running`。今天的代码是对的，但下一次有人重构共享的 `_authorize`/终态代码把它删掉时，不会有任何测试响——这条链的完整性目前**无法被测试证明**。

### F1-2（Important）embed 与 vision 的 `_reconcile_provider_usage` 五个分支全删也不被发现（chat 的同一处有专门测试）

- **改动/输出**（每一处单独删、单独跑）：

  | 位置 | 被删的行 | 结果 |
  |---|---|---|
  | :3054 | embed 成功路径 `self._reconcile_provider_usage(authorization, result)` | `### MUT 1 => 300 passed` |
  | :3051 | embed 失败路径 `self._reconcile_provider_usage(authorization, None, failed=True)` | `### MUT 3 => 300 passed` |
  | :3047 | embed 取消路径（同上写法） | `### MUT 4 => 300 passed` |
  | :3105 | vision 成功路径 | `### MUT 10 => 300 passed` |
  | :3102 | vision 失败路径 | `### MUT 11 => 300 passed` |

- **对照实验**（排除"整个仓库根本没测这个"的可能）：删 chat 成功路径的同一行（:3008）后
  ```
  py -3.12 -m pytest backend/tests/test_task2_round2_mcp_security.py -q
  FAILED backend\tests\test_task2_round2_mcp_security.py::test_successful_first_attempt_refunds_the_unused_retry_reservation
  1 failed, 53 passed in 5.92s
  ```
  （该测试断言：预留 3 轮、实际 1 轮时 `budget_usage.model_turns == 1` 且成本只付 1 份。）
  → **同一步骤 chat 有测试锁，embed/vision 没有**。全仓库只有 `test_rag_embedding_egress.py` 与 `test_rag_vision_egress.py` 调用过 `.embed(` / `.vision(`，两者都不检查 effect 之后的账。
- **对用户的实际影响**：预留是上界估计，没有对账就是永久误扣——成功时按 `1+max_retries` 轮与对应成本计；失败时 token 维度 100% 计入（代码注释自己记着上一轮复审的实测："账上 4115 / 真实 0"，22 次调用后 run 被判 `BUDGET_EXHAUSTED` 而实际只用了 27%）；反向 provider 报得比预留多时也不会补扣。向量化是**每次导入资料都会发生**的调用，多扣会迅速把学习任务的额度吃掉，而界面上只会显示"额度用完了"。

### F1-3（Important）`_authorize` 不再把 `shape.extra_input_tokens` 计入预留，也不被发现

- **改动**：`backend/commercial_gateway.py:2650` 行尾去掉 `+ shape.extra_input_tokens`
  ```python
  estimated_input_tokens = (max(1, math.ceil(len(serialized) / 4)))
  ```
- **输出**：`### MUT 17 => 300 passed in 6.10s`
- **为什么没抓住**：vision 的测试只断言 `_vision_shape("读一下这一页", payloads).extra_input_tokens == VISION_TOKENS_PER_IMAGE * 3`（shape **带了值**），没有任何测试断言这个值**真的进了预算预留**。这一条比 embedding 那条更隐蔽：值对了、用法没了。
- **对用户的实际影响**：读一页扫描图只按提示词的几十 token 预留；而补扣上限就是本次预留（`top_up = min(delta, max(0, reserved))`，:2841），图片那几千 token 永远进不了账——:2318 注释里自称"已经修好"的那件事（"预算这一维度对视觉路径基本敞开"）实际没有被测试守住。

---

## 检查 2：加密存储里还有哪些明文，它们泄漏什么

**明文列全集**（`store.py` schema，逐行逐列直读确认）：
`rag_documents(document_id, created_at, updated_at, chunk_count)`、`rag_chunks(chunk_id, document_id, parent_id, location, kind, level, content_hash, sequence)`、`rag_embeddings(model, content_hash, dimensions, created_at)`。**只有 `rag_chunks.text` 与 `rag_embeddings.vector` 是密文 BLOB。**

脚本：`check2_store_plaintext.py`（手工构造文档 + 穷举演示）、`check2b_real_parser.py`（**真实 `parse_document` + 真实源文件路径**，补强版）。

### F2-1（Important）`location` 明文里带 xlsx 工作表名，而工作表名是用户内容——端到端实测

写进去的东西（真实解析器从 `初二(3)班期末成绩单-张三.xlsx` 里解析出来的块）：

```
【真实解析器产出的块】
    kind='sheet' location='sheet:期末成绩单!A1:B2' text='姓名\t数学\n张三\t98'

【无主密钥 sqlite3 直读明文列】
    document_id='C:\\Users\\25977\\AppData\\Local\\Temp\\rag_check2_e2e\\初二(3)班期末成绩单-张三.xlsx'
      location='sheet:期末成绩单!A1:B2' kind='sheet' level='parent'
      text=<BLOB 51 字节（加密）>
```

- 来源：`parsers.py:576-582` 生成 `sheet:{name}!{range}`，`name` 就是工作簿里的工作表名。复审 grep 了 `parsers.py` 全部 12 处 `location=` 生成点，只有 `sheet:` 带用户文字（`para:`/`code:`/`h{level}:{count}`/`table:`/`slide:`/`page:` 都是结构编号，`h1:1` 不含标题文字）。
- **对用户的实际影响**：拿到 `rag.sqlite3` 的人（备份、共用电脑、云同步目录——正是 `crypto.py` 开头写明的威胁模型）**不需要主密钥**就能读到"期末成绩单""家访记录""张三的补考名单"这类工作表名，而单元格内容却打不开。`store.py:7-9` 声称明文"只保留索引运转必需的东西""不泄漏内容"，这条不成立：工作表名是用户自己敲进去的正文，不是索引运转必需。

### F2-2（Important）`document_id` 明文 = 调用方给的值，`chunks.py:99` 自己承认"可能是一段路径"

check2b 直读结果里 `document_id='C:\Users\...\初二(3)班期末成绩单-张三.xlsx'` —— 整个路径、文件名（往往等于文档标题，含姓名与班级）都是明文。本地优先应用里最自然的 `document_id` 就是源文件路径；而 `rag_chunks` / `rag_documents` 的所有查询只把它当不透明键，`_chunk_id` 也只是把它哈希进去，所以换成 `sha256(path)` 一切照常。目前全仓库**没有 document_id 生成器**（Task 3 没有生产调用方），所以这是接线前的契约缺口：今天没有任何东西阻止调用方把路径直接写进去。

### F2-3（Important）`content_hash` 是无盐 `sha256(明文)`，可确认、可穷举

`store.py:7-9` 写"`content_hash` 会泄漏结构与'两行文字相同'这件事，但不泄漏内容"。实测两种利用：

```
【利用 1：短文本穷举】content_hash 是 sha256(明文)，无盐、无密钥
    穷举 314160 次后命中：明文 = '314159'（原文档 C:\Users\张三\Documents\我的密码本.txt）

【利用 2：已知文本确认】攻击者猜一句原文，就能确认它是否在库里
    sha256('姓名\t数学\t语文\t备注\n张三\t98\t91\t家访记录…') 命中库中 content_hash
    sha256('家长联系电话 13800138000，请勿外传。') 命中库中 content_hash
    sha256('任意别的文本') 不在库里
```

- **对用户的实际影响**：库文件把"这段具体文字在不在库里"变成一次免费查询。这个产品的块经常就是短的（姓名+学号+电话、一行成绩、一句评语），候选空间很小，能被直接还原。加密保护的正是这些数据，而指纹把其中可猜的那部分变回了明文。

### F2-4（Minor）其余明文列

`created_at`/`updated_at`（他每天几点在学习、哪天导入的）、`chunk_count`、`sequence`、`kind`/`level`（哪几页是扫描件、文档有多少页）、`model`/`dimensions`（用的哪个模型、多少块）。单独看信息量小，但与 F2-1/F2-3 合起来足以还原"谁、什么时候、导入了哪份资料、大概多少内容"。

---

## 检查 3：作用域隔离能不能被穿透

**未发现问题。** 8 条尝试全部没有让作用域 A 的检索返回作用域 B 的内容：

| # | 尝试的路子 | 结果 |
|---|---|---|
| A | 两个索引里出现**同一个 chunk_id**（不同内容） | B 完全看不见 A；A 返回的是调用方亲手 `add` 进去的那块 |
| B | `add_vector` 给一个只属于 B 的块 | 被拒：`RetrievalError: 向量属于一个不在索引里的块…` |
| C | `remove_document` 之后再 `add` / `add_vector` | 删除后检索为空、`vector_count=0`；重导入正常，无残留 |
| D | 把 B 的 `Chunk` 对象加进索引 A（document_id 与索引不一致） | A 能检索到它——但这是**调用方把数据交给了 A**；索引层没有任何 API 会自己从别处取块 |
| E | 手工把 `_siblings` / `_documents` 弄成与 `_entries` 不一致 | 检索为空：候选一律从 `_entries` 取，孤儿 id 进不了结果 |
| F | 向量索引 swap-remove 之后 id 与向量是否错位 | 无错位：删中间一条后用它的向量检索，top1 是剩下的对应块 |
| G | 同一个 frozen `Chunk` 加进两个索引，再从其中一个删除 | 两个索引互不影响 |
| H | 实例间共享可变状态（类属性/同一底层对象） | 无：六个内部容器都不是同一对象，类上无可变属性 |

**能力边界（复审者自述）**：`ScopeIndex` 目前**没有任何生产调用方**，所以只能对类本身做黑箱尝试；真正会出事的场景——"装配层把同一个索引实例喂给两个作用域""从共享库里按 document_id 取块时忘了按作用域分流"——在这里无法证伪。另外 `EncryptedKnowledgeStore` 的 schema 里**完全没有作用域列**，隔离 100% 依赖未来调用方为每个作用域各建一个索引，这一点在接线时必须被设计复查。

---

## 总体判断（复审者原文）

最有信心的是**检查 1**：结论可复核（18 条 mutation、11 条被抓住、7 条全绿并附原始输出），而且用 chat 的对照实验把 F1-2 从"整个仓库都没测对账"收窄成了"chat 测了、两种新调用类型没测"。**做得最浅的是检查 3**：没有生产调用方，只能测类本身，没覆盖持久化/多进程/装配层错用，所以"未穿透"的外推能力有限；检查 2 覆盖了 xlsx 与 txt 的全流程并核对了全部 `location` 生成点，但没有为七种解析器各建一份文档逐一确证明文列。

证据文件（均在 `D:\agentwork\`，不在仓库内）：`check2_output.txt`、`check2b_output.txt`、`check3_output.txt`、`check2_store_plaintext.py`、`check2b_real_parser.py`、`check3_scope_isolation.py`、`mut.py`、`mutations.json`。
