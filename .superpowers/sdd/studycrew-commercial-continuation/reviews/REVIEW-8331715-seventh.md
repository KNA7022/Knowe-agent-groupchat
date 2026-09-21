# Independent review — 8331715

所有测量都针对 `8331715` 的冻结导出，**不是**移动后的工作树：

```
git archive 8331715 | tar -x -C %TEMP%\sc-review3\new     # backend/commercial_gateway.py sha256 = A6BD81074A075ADE2317AD07F8D5BE20FBF8F5DFF0AFFA7DEDE63628DFACC7CB
git archive 8ae2ba0 | tar -x -C %TEMP%\sc-review3\base    # 基线（本提交只改测试文件 + 台账）
```

导出与提交 blob 逐一核对一致：`git hash-object` → `commercial_gateway.py` = `e42bb208…` = `8331715:backend/commercial_gateway.py`；`test_task2_round2_mcp_security.py` = `8f1ce066…` = `8331715:…`。
工作树未被写入：`git status --porcelain` 为空；所有变异与探针都只在 `%TEMP%\sc-review3\mut`（导出副本）里做。

## Verdict

**FAIL — 0 Critical, 1 Important, 1 Minor**

第六轮的两条 Important 都**确实被锁住了**（变异均 CAUGHT，见"Verified as genuinely closed"），台账这一轮新增的事实性claim我也逐条对照过，没有发现新的假话。判 FAIL 的唯一原因是下面这条 Important 不在第六轮 I-2 的字面范围内、但落在这轮声称"已闭环"的同一条承诺上：终态写入的兜底只覆盖四个类型化异常，而**真实存储故障抛出的不是这四个类型**。

## Findings

### I-1 取消路径的终态兜底挡不住真实存储故障，`CancelledError` 仍被 `sqlite3` 异常替换（两条路径都是）

- Where: `backend/commercial_gateway.py:395-398`（ToolGateway，本轮新测试锁的就是这里）
  ```python
  try:
      self.runtime.store.cancel_tool(invocation.invocation_id)
  except (RuntimeConflictError, RuntimeNotFoundError, RuntimeStoreError, ValueError):
      pass
  raise
  ```
  同一形状的兜底还在 `backend/commercial_gateway.py:2749`（`settle_budget`）、`:2782`（provider 终态记录）、`:2829`（`provider_egress` 事件）。注释里的承诺在 `backend/commercial_gateway.py:2804-2806`："诚实边界：…存储不可用时的表现是'少一条终态记录'"。
- What: 存储层的真实故障不是 `RuntimeStoreError`。`backend/knowe_storage/_sqlite.py:316-326` 的 `transaction()` 只做 `rollback()` 后 `raise` **原样重抛** `sqlite3.*`；`TaskRuntimeStore` 里没有任何把它包成 `RuntimeStoreError` 的地方（`grep sqlite3 backend/task_runtime/store.py` 无命中）。于是 `sqlite3.OperationalError`（`busy_timeout` 默认 1000ms 到期后的 `database is locked`，见 `_sqlite.py:170-180`、`:294-300`）、`sqlite3.ProgrammingError`（runtime 关闭瞬间的 `Cannot operate on a closed database`）都不在 `except` 元组里，会**替换**调用方自己的 `CancelledError`。后果正是这段兜底存在的理由：调用方看到的是数据库异常而不是"已取消"，事务已回滚（`_sqlite.py:322-324` 保证未提交），于是这次 invocation 不会留下 `cancelled` 终态——`_complete_authorized_effect` 的注释说这种情形"表现是少一条终态记录"，但实测表现是**取消语义本身被破坏**。`ToolGateway` 上还有个附带后果：`raise` 不再执行，工具 invocation 停在 `running`，直到重启恢复把它标成 `recovered_blocked` 并吊销 grant——即 M-6 想消除的那种审计失真又回来了（这一句是从回滚语义推出来的，未单独测量）。
- Evidence:
  1. 端到端、**完全不 monkeypatch `cancel_tool`**：effect 里 `runtime.store.db.close()` 让存储真的不可用，然后 `raise asyncio.CancelledError()`。探针 `%TEMP%\sc-review3\mut\backend\tests\test_zz_reviewer_probe.py::test_real_storage_failure_does_not_replace_cancellation`：
     ```
     py -3.12 -m pytest backend/tests/test_zz_reviewer_probe.py -q --continue-on-collection-errors -p no:cacheprovider --tb=line
     E   asyncio.exceptions.CancelledError
     During handling of the above exception, another exception occurred:
     E   sqlite3.ProgrammingError: Cannot operate on a closed database.
     C:\...\knowe_storage\_sqlite.py:323: sqlite3.ProgrammingError: Cannot operate on a closed database.
     FAILED ...::test_real_storage_failure_does_not_replace_cancellation
     2 failed, 1 passed
     ```
     同一文件里的对照 `test_control_runtime_store_error_is_swallowed`（与已合入测试同样的 `RuntimeStoreError`）**passed**，所以这不是探针搭错了。
  2. 同类型检查（`database is locked`）：`test_locked_database_does_not_replace_cancellation` 同样 FAILED，`E sqlite3.OperationalError: database is locked`。
  3. provider 路径同样中招，`%TEMP%\sc-review3\probe_provider_path.py`（复刻已合入的 `test_cancellation_is_not_replaced_by_a_failing_audit_write`，只把假 store 的异常换成 SQLite 的）：`py -3.12 probe_provider_path.py` → `ESCAPED: sqlite3.OperationalError: database is locked (settle_budget)`，即 `aclose()` 抛出的不是干净结束。
- Severity rationale: 兜底存在的唯一目的就是"存储坏掉时不要弄坏取消语义"，而它恰好漏掉了存储坏掉时真正会抛的异常类型；代码注释与台账把这描述成"最多少一条审计记录"，实测是取消被替换。这与本项目反复出现的"claim 强于 code"是同一形态，只不过这次差一层：测试锁住的是 `RuntimeStoreError` 这一个形状，注释描述的是"存储不可用"这整个场景。（`ToolGateway._invoke` docstring 里"该兜底是这条承诺的**全部实现**"作为"实现位置"是准确的，我按准确理解处理，没有据此单独记一条。）修法很小（`except Exception`），但按规则不因修得便宜而降级。

### M-1 套件里还有文档未列的 flaky 名字，计数在三轮运行间持续漂移

- Where: `backend/tests/test_task_runtime_rereview_round4.py::test_expired_approval_read_sweep_terminalizes_continuation_and_releases_queue`（环境说明只列了三个 flaky）。
- What: 基线树重跑时它失败、新树通过；第一轮两棵树的运行里它都没失败。所以它是第四个 flaky 名字，而不是本轮引入的失败。计数漂移也比我预期的更大：基线 `57 failed / 982 passed` → 重跑 `59 failed / 980 passed`；新树 `58 failed / 983 passed` → 重跑 `59 failed / 982 passed`。
- Evidence: 两次全量运行的 `-rf` 名单对比（`%TEMP%\sc-review3\base-run.txt`、`%TEMP%\sc-review3\new-run.txt`）：
  ```
  === only in NEW ===  test_task_runtime_quarantine_isolation.py::test_limit_two_overflow_never_starves_valid_confirmed_deletion
                       test_task_runtime_recovery_ownership.py::test_stalled_client_is_fenced_before_dirty_log_exceeds_budget[1000]
  === only in BASE === test_task_runtime_recovery_ownership.py::test_stalled_client_is_fenced_before_dirty_log_exceeds_budget[200]
                       test_task_runtime_rereview_round4.py::test_expired_approval_read_sweep_terminalizes_continuation_and_releases_queue
  ```
- Severity rationale: 只是把"计数会漂"这条已记录的 Minor 从三个名字扩到四个，不影响验收；但"failure 名字是标准"这条纪律需要一个准确的 flaky 清单才成立。

## Verified as genuinely closed

第六轮 I-2（`ToolGateway` 终态兜底零覆盖）——**已闭环**：
- 变异"去掉 395-398 的 try/except"（`mutate.py drop_tool_cancel_guard`）→ `test_tool_cancellation_survives_a_failing_terminal_write` **FAILED**（`1 failed, 46 deselected`），且 traceback 落在守卫行本身：`commercial_gateway.py:395 in _invoke → self.runtime.store.cancel_tool(invocation.invocation_id)`，抛出的是 `RuntimeStoreError`——证明这行真的被走到、`invocation_id` 是真的（`tool_ced23b0b3f4042fe9b581e25c1964503`）。
- 不是"因为错误的原因通过"：`CancelledError` 来自 effect 分支（`cancel_tool` 在 `_invoke` 里只出现在这一个分支；若 effect 不抛，`complete_tool` 会走通、`pytest.raises` 会 DID NOT RAISE），不是 pytest 机制。`store` 是普通属性（`service.py:133`），monkeypatch 生效。
- 未变异时 round-2 文件 **47 passed**，与提交信息"Round-2 acceptance file: 47 passed"一致。

第六轮 M-1（红测只走非敏感路径）——**已闭环**：
- 变异"去掉 `isinstance`"（`mutate.py drop_isinstance`）→ 两条 duck-typed 测试都 FAILED，新的那条以 `AssertionError: prepare_tool must not be reached` 失败，且 traceback 显示假 runtime 的 `prepare_tool` 被调用，参数 `tool_name='provider_egress'`、`risk=HIGH`——说明审批分支（`_authorize:2489-2510`）真的被走到，这条测试确实覆盖敏感路径。
- 前置条件是真闸门而不是装饰：`commercial_gateway.py:2474` 用 `PiiDlpGateway().decide_outbound(serialized, approved=False)`，测试用同一份 `client._serialized_request(messages, None, None)` 断言 `requires_approval`，与 `_authorize` 里的计算逐字相同；变异运行证明该分支为真。

本轮台账（`.superpowers/sdd/studycrew-commercial-continuation/progress.md` 新增的第六轮小节）逐条对照结果：**未发现新的假 claim**。
- "sixth review verdict: FAIL (0 Critical / 2 Important / 2 Minor)" 与 `%TEMP%\sc-review2\REVIEW-448c4c3.md` 的 Verdict 及 I-1/I-2/M-1/M-2 标题一致。
- "`worker_gateway_runtime.py:332` 是生产调用点"：`backend/worker_gateway_runtime.py:332` 仍是 `authority_provider=lambda selected=authority: selected`。
- "第六轮 verified as genuinely closed" 列表与 `REVIEW-448c4c3.md` 的 Verified 小节逐项对得上（isinstance 闸门、两条 abort 子句、五个 settle_budget 维度、provider_egress 终态兜底、前端 drift guard 三种失败方式、`cancelled` 状态一致性、conftest broker reset、无新增失败名）。
- 台账说 I-2/M-1 的变异 CAUGHT——我独立复现，成立。
- 台账说"`incomplete_tools()` 只读 prepared/running"：`backend/task_runtime/store.py:1283` 确为 `status IN ('prepared','running')`。
- 台账说 `tool_cancelled` 与后端白名单、前端镜像一致：`contracts.py:82-85` 的 10 个字段 == `src/contract/taskRuntime.ts:175-178` 的 10 个字段（不仅子集，完全相等）；`_transition_tool_with_event` 生成的 payload（`store.py:1351-1365`）是它的子集；`src/contract/publicEvents.drift.test.ts:101-106` 用的样例 `status: 'cancelled'`。

`cancelled` 终端状态的完整面（第六轮未查的部分）——**测量结果：吸收态，无矛盾**。探针 `%TEMP%\sc-review3\probe_cancelled.py`（纯 store/service API）：
- `cancel_tool` 对非 `running` 是**静默 no-op**并返回未改动的 invocation（`cancel(prepared)` → 仍 `prepared`，无异常）；`expected_statuses` 只有 `running`（`store.py:1422`），`mark_tool_recovered_blocked` 只有 `prepared/running`（`:1452`），`complete_tool` 只有 `running`（`:1403`）。
- 进入 `cancelled` 后：第二次 `cancel` → 无变化；`claim_tool_execution` → `(cancelled, claimed=False)`；`mark_tool_recovered_blocked` → 无变化；`complete_tool` → 无变化；`incomplete_tools()` → False；`recover_after_restart()` → `high_risk_blocked=0, replayable_pending=0`，状态不变。
- **不能被重放**：同 `idempotency_key` 再次 `prepare_tool` 返回既有 invocation（`disposition='created'`, `status='cancelled'`），而两个 Gateway 调用点都要求 `status == "prepared"`（`commercial_gateway.py:377`、`:2505`）→ `GatewayDenied`。`error_type` 保持空（`store.py:1420-1426` 不写 error_type）。
- 后端没有别处把工具终态枚举成 `{completed, failed}` 而漏掉 `cancelled`（唯一的工具状态枚举点是上面三处 + `incomplete_tools`）；前端 `src/store/taskRuntime.ts:57` 的 `TERMINAL` 是 **run** 状态，与工具无关。

独立 "零新增失败" 检查——**通过（除已记录的 flaky）**：`git archive 8331715` vs `git archive 8ae2ba0`，两棵树各跑一次全量 `py -3.12 -m pytest backend/tests -q --tb=no -rf --continue-on-collection-errors -p no:cacheprovider`，按 **failure 名字** diff：新树独有的名字只有 `test_limit_two_overflow_never_starves_valid_confirmed_deletion` 与 `test_stalled_client_is_fenced_before_dirty_log_exceeds_budget[1000]`，两者都属于环境说明里已记录的 flaky（后者是同一 flaky 用例的另一个参数）；反向（仅基线）是 `…exceeds_budget[200]` 和 `test_expired_approval_read_sweep…`。**没有任何新测试失败**，两条新测试在两轮运行里都通过。

## Claims I could not verify

- 第六轮遗留的"`MagicMock(spec=TaskRuntimeService)` / `__class__` 撒谎是否也能骗过 `isinstance`"仍未测（本轮时间盒内未做，与第六轮同样悬置）。
- `database is locked` 在生产里的**发生频率**：我只测到"该异常类型不被兜底捕获"，没有做并发/负载实验去测它多久出现一次；关闭数据库那条探针则不需要竞争条件就复现了。
- 工作树在 `8331715` 之后的三个提交（`350beaf`/`4877985`/`214dc24`）不在本次审查范围内，其中台账里的逐条变异表与 `models=("*",)` 的新测试我都没有重跑（父代理已说明它们不改行为）。
- 前端套件本轮**未运行**（`npx vitest run` 未执行）；`8331715` 不含前端改动，前端镜像一致性是我直接读 `src/contract/taskRuntime.ts` 与后端白名单比对的，drift guard 本身的失败行为依据第六轮的测量。
- 未测：`ToolGateway` 路径上"存储故障后 invocation 停在 `running`"这一后果（由 `_sqlite.py:322-324` 的回滚语义推出，未单独测量）。
