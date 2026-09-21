# Independent review — StudyCrew @ 73d7e33 (branch `feature/studycrew-foundation`)

> 报数口径：全部测量针对 **73d7e33**（我被要求评审的 revision），行号与代码引用都取自该 revision 的提交内容（`git show 73d7e33:...`）。父代理告知 HEAD 已前移到 `d9e15e1`，其中 `94479dc` 与 `7cef6b3` 关闭了我下面两条发现（I-2、M-1）；我读了这两个提交的 diff 来确认，但没有对 `d9e15e1` 重新跑测量。工作树未被我修改，所有临时文件都在 `%TEMP%`。

## Verdict
FAIL — 0 Critical, 4 Important, 7 Minor

（若只按 `d9e15e1` 计：I-2、M-1 已由 `94479dc`/`7cef6b3` 关闭，Important = 3（I-1、I-3、I-4），全部是"声明强于代码"与"测试弱于声明"两类，无 Critical。）

## Findings

### I-1 "没有任何配置能让 provider egress 在没有权威 Task Runtime run 的情况下进行"是假的
- Where: `backend/commercial_gateway.py:2286`（`authority_provider` 形参）、`:2317`（`self.authority_provider = authority_provider`）、`:2399-2401`（唯一一次取权威）
- What: `_authorize` 只要求 `authority.runtime` 非空、且该对象的 `store.get_run()` 返回一个 `status is TaskRuntimeStatus.RUNNING`、`group_id`/`task_id` 对得上的 run。它**不检查 `runtime` 是不是 `TaskRuntimeService`**。一个 20 行的普通 Python 假对象就能满足全部条件，于是 egress 全程没有任何 Task Runtime 参与。而 `authority_provider` 正是 `ProviderGatewayClient.__init__` 的一个公开关键字参数，任何构造者都能换成自己的 callable；实例属性也能用 `object.__setattr__` 改。`ebf7feb` 的提交信息与台账第 45 行都写了绝对句："there is no configuration under which a provider egress proceeds without an authoritative Task Runtime run" / "no configuration lets a provider egress proceed without an authoritative Task Runtime run"——实测为假。
- Evidence:
  ```
  py -3.12 %TEMP%\sc-review\exp2.py     # 针对 73d7e33
  == 2a. constructor authority_provider = non-Task-Runtime object ==
      egress result: {"choices": [], "usage": {"total_tokens": 3}}
      trace: ['AUDIT provider_egress authorized', 'SENT POST https://api.deepseek.com/chat/completions', 'AUDIT provider_egress completed']
  == 2b. object.__setattr__ swap on a constructed gateway ==
      egress result: {"choices": [], "usage": {"total_tokens": 3}}
      trace: ['AUDIT provider_egress authorized', 'SENT POST https://api.deepseek.com/chat/completions', 'AUDIT provider_egress completed']
  ```
  假 runtime 只需要 `store.get_run/change_run(**kw)` 与 `consume_budget()` 三个方法。同时确认了**机制那一半是真的**：
  ```
  py -3.12 -c "...ast.walk..."   →  _authorize returns: 1  (line 2502 return _ProviderAuthorization(...))
  inspect.signature(ProviderGatewayClient.__init__)  →  无 **kwargs；_trusted_test_transport /
      allow_ungoverned_test_transport 都抛 TypeError（exp1 A1/A2）
  ```
  诚实边界（我实测过，所以不夸大）：`git grep authority_provider -- backend` 显示**没有任何生产构造点**给 `ProviderGatewayClient` 传 `authority_provider`——`engine.bind_commercial_tool_registry` 和 `worker_gateway_runtime.py:332` 传的是给 `LegacyToolBoundary` 的；生产一律走默认的 ContextVar。所以要利用它需要同进程代码执行能力，而这已经可以直接 `httpx.post()` 出去，因此这不是一个"可利用的越权"。它是**被评审的那句验收声明本身为假**：机械检查（`_authorize` 只有一个 return）通过，但"一个 return"推不出"权威是真实的"。上一轮把同类被证伪的保护性声明定为 Important 并列为终止项，我按同一标准定级。
- Severity rationale: 验收决定建立在这句话上，而它是可测量的假；修法很轻（把 `authority.runtime` 断言为 `TaskRuntimeService` 实例，或让测试走 `probe_authority` 那条路而不是自造 authority），所以不该被软化。

### I-2 `_complete_authorized_effect` 的终态记录没有保护，会把 `GeneratorExit` 换成 `RuntimeStoreError`
- Where: `backend/commercial_gateway.py:2719-2728`（`if authorization.invocation_id:` 下的 `store.cancel_tool(...)` / `store.complete_tool(...)`），调用点 `:2815`（`chat_stream` 的 `except (GeneratorExit, asyncio.CancelledError)` 分支内）
- What: 73d7e33 上终态记录**没有** try/except，只有它下面的 `change_run`（`:2743-2752`）有。由于 `:2815` 就在取消分支里，存储抛错会替换掉正在传播的 `GeneratorExit`/`CancelledError`：一次正常 `aclose()` 变成消费者侧的意外异常。我在 `94479dc` 的提交信息里看到这条（父代理也提示了），并独立复现了它的机制。
- Evidence:
  ```
  py -3.12 -c "..._complete_authorized_effect(auth, error=None, cancelled=True)..."   # PYTHONPATH=sc-mut-base(73d7e33)
  tree under test: ...\sc-mut-base\backend\commercial_gateway.py
  terminal record guarded by try/except: False
  RESULT: escaped -> RuntimeStoreError store is unavailable: cancel_tool
  ```
  端到端那条路我没跑成（我的 scratch client 走的不是需要审批的 effect，`invocation_id == ""`，整段被跳过；需要真审批+grant 才能让 `cancel_tool` 被调到），因此这一条的证据是**直接调用层**的，加上 `94479dc` 提交信息里给的红色实测栈（`aclose() -> :2815 -> :2722 -> RuntimeStoreError`）。`git show 94479dc -- backend/commercial_gateway.py` 确实就是给这段加上了与相邻事件写入相同的 `except (RuntimeConflictError, RuntimeNotFoundError, RuntimeStoreError, ValueError)`。
- Severity rationale: 这是 8ae1263 新引入的取消路径上的真实缺陷（旧代码里同一段没有 GeneratorExit 语义可替换），会把"流结束了"变成"存储炸了"；已在 `94479dc` 关闭。

### I-3 非流式 `chat` 的取消分支完全没有测试覆盖
- Where: `backend/commercial_gateway.py:2770-2774`（`chat` 里的 `except (GeneratorExit, asyncio.CancelledError)`）
- What: 把这一整段删掉（等价于把取消重新交给 `except BaseException`，即重新引入 M-6 的"取消被记成失败"），`backend/tests/test_task2_round2_mcp_security.py` 30 个测试**全绿**。宽选集（round2 + provider_secret_gateway + knowe_core + round1 + v06b + audit_redaction，92 个测试）也同样全绿。也就是说 8ae1263 为 `chat` 新增的取消语义没有任何用例守着。
- Evidence:
  ```
  py -3.12 %TEMP%\sc-review\mutate.py      # mutation m6g-chat-path-cancel-not-caught
  {"id": "m6g-chat-path-cancel-not-caught", "status": "TEST-PASSED(BAD)", "tail": "30 passed in 7.69s"}
  ```
  分支本身**是可达且正确的**（我另外实测过：真 Task Runtime 授权 + 挂起的 transport，`task.cancel()` 后审计是 `authorized` → `cancelled`，无 `error_type`）：
  ```
  py -3.12 %TEMP%\sc-review\exp2.py
  == 1. non-streaming chat cancellation (REAL Task Runtime authority) ==
      CancelledError propagated
      provider_egress: {"status": "authorized", ...}
      provider_egress: {"status": "cancelled", "error_type": null, ...}
  ```
- Severity rationale: 新代码 + 零覆盖，且这正是任务书要求"必须靠变异测试证明测试是真的"的那批改动之一。

### I-4 M-9 的守卫测试只覆盖 5 个维度中的 1 个，声明却说"每一个维度"
- Where: 生产 `backend/task_runtime/service.py:574-594`；测试 `backend/tests/test_task2_round2_mcp_security.py:656`
- What: `settle_budget` 的释放守卫覆盖 `tokens`/`model_turns`/`tool_calls`（整数）与 `wall_clock_seconds`/`cost_usd`（浮点，容差 `1e-9`）。新测试只用 `tokens`。把 `wall_clock_seconds` 守卫删掉、把 `cost_usd` 守卫删掉、把容差从 `1e-9` 放大到 `1e9`，对应测试全部保持绿色；而 8ae1263 的提交信息写的是"The release is now bounded by the recorded usage in **every dimension**"。
- Evidence:
  ```
  py -3.12 %TEMP%\sc-review\mutate2.py
  {"id":"m9b-wallclock-guard-removed","status":"TEST-PASSED(BAD)","tail":"30 passed in 6.32s"}
  {"id":"m9c-cost-guard-removed",   "status":"TEST-PASSED(BAD)","tail":"30 passed in 4.71s"}
  {"id":"m9d-float-tolerance-unbounded","status":"TEST-PASSED(BAD)","tail":"30 passed in 4.50s"}
  ```
  同一批里**被抓到**的（说明这套测试不是全空转）：`m9a`（删整数守卫）→ `DID NOT RAISE RuntimeConflictError`；`m9e`（删 `actual > reserved` 前置检查）→ `test_settle_budget_refuses_to_add_usage` 失败；`m5`（删 `total_tokens` 优先）与 `m5b`（去掉 bool 排除）→ 都被 `:838`/`:821` 抓到。
- Severity rationale: 按任务书的判据（"corresponding test 必须在变异下失败；留下来的都是 Important 起"）。生产行为本身经我独立实测是正确的，见下面"Verified"一节——缺的是测试，不是实现。

### M-1 前端枚举缺 `tool_cancelled`，`RunEventSchema.parse` 会抛错
- Where: `src/contract/taskRuntime.ts:135`（`publicEventFields`，73d7e33 上 `tool_cancelled` 不存在）、`:182`（`unsupported public event type`）、`src/store/taskRuntime.ts:282`（`RunEventSchema.parse(raw)`）、`src/app/App.tsx:505-513`（onEvent 无 try/catch）、`backend/server.py:2881-2891`（事件不过滤类型直接广播）
- What: 73d7e33 上前端白名单只有 `tool_prepared` 这一种工具事件；`tool_cancelled` 是 8ae1263 新增的后端公开事件类型，前端未登记。我实测 HEAD 的白名单：`provider_egress`/`tool_claimed`/`tool_completed`/`tool_failed`/`tool_cancelled`/`tool_recovered_blocked` **六种全部**被拒（前五种在 8ae1263 之前就缺，属于既有漂移；`tool_cancelled` 是本轮新增的）。
- Evidence:
  ```
  git show 73d7e33:src/contract/taskRuntime.ts > %TEMP%\sc-review\taskRuntime.HEAD.ts
  (Select-String taskRuntime.HEAD.ts -Pattern tool_cancelled).Count → 0
  node .tools\node-v24.9.0-win-x64\node.exe node_modules\vitest\vitest.mjs run --config vitest.probe.config.ts   # 在 %TEMP%\sc-mut-base(=73d7e33) 内
  PROBE tool_cancelled: success=false issues=["unsupported public event type"] | tool_failed: success=false ... |
        tool_completed: success=false ... | tool_recovered_blocked: success=false ... | provider_egress: success=false ...
  ```
- 诚实边界：与 8ae1263 之前相比**边际影响为零**——取消在旧代码里同样写一条 `provider_egress/failed`，而它同样没被登记，同样抛。所以这是"新增事件加入了一条已经坏掉的路径"，不是新故障。已在 `7cef6b3` 修好（并加了 `src/contract/publicEvents.drift.test.ts` 守卫）。
- 附注（评审环境状态，不是代码问题）：我开工时 `src/contract/taskRuntime.ts` 在**工作树里已经是 7cef6b3 的内容**（未提交），`src/contract/publicEvents.drift.test.ts` 是未跟踪文件；我第一次 `git status --porcelain` 只报 `?? skills/`，几分钟后才报出这两项。上面所有测量都以 `git show 73d7e33:...` 的**提交内容**为准。

### M-2 `ProviderClient._gateway_managed` 的说法与代码矛盾；该守卫零测试
- Where: `backend/knowe_core/provider_client.py:239-244`（守卫）、`:310-339`（`_get_client` 的 MockTransport 检查）
- What: 注释说"这个布尔唯一做的事，是拒绝'绕过 Gateway 直接构造 transport 并发请求'"。实测：它是调用者自己传的构造参数，想绕过的人传 `True` 即可；再配一个没有 `_transport` 属性的 wrapper 就完全通过。台账第 46 行说"the surrounding comment says exactly that rather than implying it is an active control"——注释原文并没有这句，它把它写成了一个正在生效的拒绝动作。另外：全仓库**没有任何测试**断言该构造期 `PermissionError`（`git grep "Production provider egress requires the Provider Gateway" -- backend/tests` → 0 命中）。
- Evidence:
  ```
  py -3.12 %TEMP%\sc-review\exp1_bypass.py
  [OK ] B1 direct ProviderClient(_gateway_managed=True) egressed: {"choices":[{"message":{"content":"ok"}}]}
       real HTTP hits on local socket: 1
  ```
  （变体 seam-c：把 `if client_factory is not None and not self._gateway_managed` 改成 `... and False`，6 个相关测试文件 92 个测试全绿。）
- Severity rationale: 生产无调用点传 `_gateway_managed`，所以不是漏洞；问题是"看起来在生效、其实是死的"守卫 + 与之矛盾的说明，正是这个项目反复出现的失败模式。

### M-3 `client_factory` 不是"只替换 socket"：授权/审计的 endpoint 与真实目的地可以不一致
- Where: `backend/tests/support_authority.py:8-9`（"被替换掉的只有最底层的 socket（MockTransport）"）、`backend/commercial_gateway.py:2306-2308`（"client_factory（只替换发送通道，授权/预算/审计照旧）"）、`:2414-2420`（registry 用 `self.base_url` 做准入）
- What: registry 准入、DLP、`capability_digest`、审计全部基于 `self.base_url`；真正发出去的 URL 由注入的 transport 决定。一个改写目的地的 `MockTransport` handler 就能让"已授权给 `https://api.deepseek.com`"的字节落到别处，而审计里一切照旧。
- Evidence:
  ```
  py -3.12 %TEMP%\sc-review\exp2.py
  == 2c. registered endpoint vs actual destination (client_factory rewrites URL) ==
      gateway thinks endpoint   : https://api.deepseek.com
      real socket received      : ['/exfil']
      audit trace               : ['AUDIT provider_egress authorized', 'AUDIT provider_egress completed']
  ```
- Severity rationale: 同样需要同进程代码执行，生产无 `client_factory` 调用点；但 agent 测试现在**以这条通道为官方做法**，而它的说明把风险描述得比实际窄（endpoint 绑定也被它绕开了）。

### M-4 M-6 的控制流信号枚举漏了 `KeyboardInterrupt` / `SystemExit`
- Where: `backend/commercial_gateway.py:2808`（`except (GeneratorExit, asyncio.CancelledError)`）
- What: M-6 的论点是"控制流信号不是运行错误"。`KeyboardInterrupt`/`SystemExit` 属同一类，但仍落到下面的 `except BaseException` → `type(error).__name__` 被写进审计。
- Evidence:
  ```
  py -3.12 %TEMP%\sc-review\exp4.py
  == E. transport raises KeyboardInterrupt (BaseException, not cancel) ==
      raised: KeyboardInterrupt
      terminal statuses: ['failed']
      full provider_egress: [('authorized', None), ('failed', 'KeyboardInterrupt')]
  ```

### M-5 效果可以"完全没有终态记录"
- Where: `backend/commercial_gateway.py:2791-2826`（`chat_stream` 的终态路径全在生成器体里）
- What: 终态审计依赖 async generator 被 finalize。正常 `asyncio.run`/事件循环的 asyncgen 终结钩子会补上（实测 A 用例写了 `cancelled`），但如果生成器始终没被终结（进程退出、或 loop 未做 `shutdown_asyncgens` 就关闭），终态审计**一条都没有**。这解释了"会不会漏记"这一问的答案是"会"。
- Evidence:
  ```
  py -3.12 %TEMP%\sc-review\exp4.py
  == A. consumer breaks out and drops the generator (asyncio.run finalises) ==   terminal statuses: ['cancelled']
  == B. same, but the loop is closed WITHOUT shutdown_asyncgens ==               terminal statuses: []
  == C. aclose() twice ==                                                        terminal statuses: ['cancelled']   (只一条，无重复)
  == D. full consumption, then aclose() ==                                        terminal statuses: ['completed']   (只一条，无重复)
  ```

### M-6 `runtime_settings._secret_lease_broker` 的跨测试全局泄漏（既有，非新夹具造成）
- Where: `backend/tests/conftest.py:46-55`（`_reset_runtime_settings` 不重置该全局）、`backend/tests/support_authority.py:148-159`（新夹具的存取）
- What: 新夹具本身是干净的（正常退出/异常退出都还原，见下），但 `test_secret_lease_ingress.py` 与 `test_zinnia.py` 会把全局 broker 留在那儿，而 conftest 不清理它 → 测试顺序会改变后续用例看到的全局状态。
- Evidence:
  ```
  py -3.12 -m pytest backend/tests/test_secret_lease_ingress.py backend/tests/leakcheck.py
  LEAKCHECK broker=<...SecretLeaseBroker object...>   → 1 failed, 6 passed
  同一命令在 %TEMP%\sc-base-e9892ae（基线）上同样失败 → 既有问题，不是 ebf7feb 引入
  ```
  新夹具自身的测量（`probe_fixture_globals.py`）：`PROBE-A broker at test entry: None`（单独跑 agent 测试时）、`PROBE-B/C restored ...: True`（正常与异常路径）、`PROBE-D distinct dirs: True`、`PROBE-E wrong-endpoint binding rejected`。

### M-7 台账/提交信息里的"零新增失败"证据句与实测不符（结论仍成立）
- Where: `.superpowers/sdd/studycrew-commercial-continuation/progress.md:45`
- What: 该行写"full suite 57 failed / 967 passed ... the 57 failure names identical to the baseline apart from the two known flaky cases (`test_persist::test_hub_emit_writes_through_to_disk` appears, `test_task_runtime_quarantine_isolation::...` does not)"。我独立 export `8ae1263^`(=e9892ae) 并全量跑了两次：基线是 **56 failed / 964 passed**，HEAD 是 **57 / 967**；失败**名单**只差一个，且方向与该句相反——`test_task_runtime_quarantine_isolation::test_limit_two_overflow_never_starves_valid_confirmed_deletion` 只在 HEAD 出现，`test_hub_emit_writes_through_to_disk` 两边都没出现。两个都是已知 flaky，我在两棵树上各单独跑 6 次复现了摇摆（HEAD：quarantine 3 失败/3 通过、hub_emit 2 失败/4 通过；基线：2/4 与 1/5），所以**"零新增失败"这个结论是对的**，错的是它引用为证据的那句话。
- Evidence:
  ```
  py -3.12 -m pytest backend/tests --continue-on-collection-errors -q --tb=no -rfE -p no:cacheprovider
  HEAD(73d7e33): 57 failed, 967 passed, 5 skipped, 20 errors
  BASE(e9892ae): 56 failed, 964 passed, 5 skipped, 20 errors
  Compare-Object(FAILED|ERROR 名单) → only in HEAD: ...test_limit_two_overflow_never_starves_valid_confirmed_deletion
                                     only in BASE: (空)
  ```

## Verified as genuinely fixed

- **命名的后门确实没了**：`ProviderGatewayClient.__init__` 无 `**kwargs`，`_trusted_test_transport` / `allow_ungoverned_test_transport` 都抛 `TypeError`；AST 计数 `_authorize` 恰好 1 个 `return`（`:2502`）；`ProviderClient` 侧的同名参数也没了；三个 agent 调用点（`deepseek.py`/`knowe_agent.py`/`zinnia.py`）的实参在 `8ae1263^..ebf7feb` 的 diff 里被删除。把参数加回去，`test_injecting_a_transport_no_longer_bypasses_authorization` 报 `DID NOT RAISE TypeError`；把 `_authorize` 头部插一个 `return None`，它报 `DID NOT RAISE GatewayDenied`。
- **M-6 流式取消**：`test_abandoned_stream_is_audited_as_cancelled_not_failed`（`:1160`）在"删掉取消分支"的变异下失败，报错正是提交信息声称的修复前形态 —— `{'status':'failed', 'error_type':'GeneratorExit'}`。`test_cancel_tool_is_a_distinct_terminal_state`（`:1197`）在 `cancel_tool` 改回 `failed/tool_failed`、以及偷偷塞 `error_type="GeneratorExit"` 两种变异下都失败；删掉 `contracts.py` 的 `tool_cancelled` 登记会让 `RunEvent.__post_init__` 抛 `ValueError: unsupported public event type: tool_cancelled`（说明 `tool_cancelled` 确实过公开 payload 契约）。`test_completing_an_authorized_effect_distinguishes_cancel_from_failure`（`:1253`）在"取消走 `complete_tool`"和"payload status 写回 failed"两种变异下都失败。
- **M-5**：删掉 total 优先块 → `test_usage_parsing_understands_non_openai_dialects` 报 `assert 10 == 42`；去掉 bool 排除 → `test_provider_reported_usage_is_parsed_strictly` 报 `assert True is None`。
- **M-9 实现本身**：基线复现了缺陷（`usage 100 → settle(reserved=1000, actual=0) 被接受 → usage 0`），73d7e33 上 `RuntimeConflictError: released tokens exceeds the recorded usage`，且 `cost_usd` / `wall_clock_seconds` 两个浮点维度也各自被拒（我用 `exp3.py` 直接构造了这两维的超额释放）。幂等（同 `request_key` 重放不改数）、并发（4 线程同时 settle 只有 1 个成功，其余 `RuntimeConflictError`，余额不会变负）、`consume_budget` 与 settle 混跑都不会把用量inflate成负数——见 `exp3.py` 输出。
- **"零新增失败"**：以失败**名单**（不是计数）比对，HEAD 相对基线只多一个已知 flaky 名字，且该名字在基线单独重跑时也会摇（3 失败/3 通过）。三名 agent 测试文件的独立比对：两边都是 **10 failed / 55 passed，失败名单完全相同**（与台账第 45 行的说法一致）。
- **新授权夹具真的建立了真东西**：真 `TaskRuntimeService` + RUNNING run（`PROBE-F run status=TaskStatus.RUNNING group_match=True task_match=True`）；真 `SecretLeaseBroker` lease，且 `resolve_provider_credential` 的 purpose 绑定仍生效（`PROBE-E`：换 endpoint 的 binding 被拒，同一 binding 在 TTL 内可重复兑换——这是设计如此）；夹具不注册/不放宽 capability registry —— 把 registry 里 `deepseek` 的 endpoint 键改名后 `test_deepseek.py` 从"0 失败"变成 **17 failed**，即失败是响亮的，不是静默通过（台账第 50 行的声明成立）。全局状态：`PROBE-A/B/C` 证明新夹具在正常与异常路径都还原 `_secret_lease_broker`，`_CURRENT_STORE` 由 conftest 用 token reset，每次 `authority_store()` 给不同子目录。

## Claims I could not verify

- **`d9e15e1` 上的实测**：I-2/M-1 是否真的在 `94479dc`/`7cef6b3` 后关闭，我只读了 diff 与提交信息，没有在新 HEAD 上重跑变异/全量。`7cef6b3` 的内容与我开工时工作树里那份未提交改动一致，因此我倾向认为 M-1 已闭合，但这是推断而非测量。
- **I-2 的端到端复现**：我只证明了"终态记录未加保护，`cancel_tool` 抛错会逃出 `_complete_authorized_effect`"。要让 `aclose()` 真的抛，需要一条**需要审批**的效果（只有它才带 `invocation_id`，`:2437-2458`），而那需要真 approval+grant；我的 scratch client 没走到那条路，所以那一跳引用的是 `94479dc` 提交信息里的栈，不是我自己的输出。
- **`ToolGateway._invoke`（`:383-392`）的取消语义**：它 `except Exception`（不含 `CancelledError`），所以取消时工具 effect 既不记 failed 也不记 cancelled，invocation 会停在 `running` 直到重启恢复（`service.py:2123-2131`）。这是 8ae1263 之前就存在、且不在本次三个提交的 diff 里的不对称，我只做了代码阅读，没有执行测量，因此不作为 finding，只作为观察记录。
- **`_DELEGATED_READS` 在 `commercial_gateway.py:2335` 与 `:2340` 重复定义**（同名类属性写了两遍，第一段注释成了孤儿）。不影响行为，不属于本次改动，未列为 finding。
- **`settle_budget` 整数维度的截断**：`int(reserved.tokens) - int(actual.tokens)` 对小数预留会截断（`reserved=10.999, usage=10` → 释放 10，被接受并抹平 0.999），我实测到了，但 `BudgetUsage` 语义是整数 token，量级 <1，未列为 finding。
