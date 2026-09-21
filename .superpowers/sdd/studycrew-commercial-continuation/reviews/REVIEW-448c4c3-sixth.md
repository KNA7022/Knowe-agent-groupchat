# Independent review — 448c4c3

All measurements are against a frozen export of the reviewed revision:

    git archive 448c4c3 | tar -x -C %TEMP%\sc-448      # sha256(backend/commercial_gateway.py) = 0A3CAF641D17178F57F98F492D113590477BA0A8FE9894752EBE51A3CEAC9798
    baseline: git archive e9892ae | tar -x -C %TEMP%\sc-base

Nothing was written inside `D:\agentwork\StudyCrew-worktree`. Note: the worktree HEAD moved
from `448c4c3` to `247c9ae`/`ee85cd6` while this review ran (another writer); every number
below comes from the frozen 448c4c3 export, not from the moving tree.

## Verdict

FAIL — 0 Critical, 2 Important, 2 Minor.

## Findings

### I-1 The fix's own honesty claim about the authority seam is false at 448c4c3
- Where: `backend/commercial_gateway.py:2437-2438` (448c4c3) — "`git grep` 显示**没有任何生产调用点**传这个参数"; same sentence in the commit message of `448c4c3` and in `.superpowers/sdd/studycrew-commercial-continuation/progress.md:53`.
- What: The claim is measurably false. `backend/worker_gateway_runtime.py:332` passes `authority_provider=lambda selected=authority: selected`, and that module is production (`backend/engine.py:98` imports it). Two call sites exist, one of them production. The real constraint is different and weaker-sounding but true: the authority production passes is rebuilt by `ProjectEngine._canonical_legacy_tool_authority` (`backend/engine.py:8298-8305`, `runtime=self._task_runtime_service`), so a real `TaskRuntimeService` always reaches the `isinstance` gate. The gate itself is fine; the stated reason it holds was not.
- Evidence: `git grep -n authority_provider 448c4c3 -- backend` → 19 hits, including `448c4c3:backend/worker_gateway_runtime.py:332`. `Select-String authority_provider sc-448\backend\worker_gateway_runtime.py` → line 332; lines 323-329 show the value comes from `canonicalize(raw_authority)`.
- Severity rationale: It is the second consecutive round in which the boundary claim about this exact seam is false in code comment + commit message + ledger, and the false version points future readers away from the constraint that actually holds. (The parent reports this is corrected in `247c9ae`; I confirmed the corrected paragraph exists there and that no production-call-site claim is made any more. The finding stands against the revision under review.)

### I-2 The new ToolGateway cancellation guard has zero coverage; the ledger presents it as verified
- Where: `backend/commercial_gateway.py:395-398` (448c4c3)
  ```python
  try:
      self.runtime.store.cancel_tool(invocation.invocation_id)
  except (RuntimeConflictError, RuntimeNotFoundError, RuntimeStoreError, ValueError):
      pass
  raise
  ```
- What: Deleting the guard is not caught by any shipped test. The ledger entry added in `448c4c3` (`progress.md:64`) claims "Fixed: cancellation now calls `store.cancel_tool`, and a failing terminal write cannot replace the cancellation" — the first half is covered (`test_cancelled_tool_effect_is_not_left_running`), the second half is not. This is the same "fix without coverage" pattern that made the fifth review's I-3 Important, produced inside the commit that fixed I-3.
- Evidence:
  - Mutation "remove the guard" (patch script `%TEMP%\sc-review2\patch.py unguard_tool_cancel`) → `py -3.12 -m pytest backend/tests/test_task2_round2_mcp_security.py --continue-on-collection-errors -q` → **44 passed** (pristine baseline of the same file: 44 passed).
  - Grep: no other test file references `cancel_tool` — `Select-String backend\tests\*.py -Pattern 'cancel_tool'` returns only `test_task2_round2_mcp_security.py`.
  - The guard is reachable and effective (probe written to `%TEMP%\sc-mut\backend\tests\test_zz_probe_tool_guard.py`, with `runtime.store.cancel_tool` replaced by a raiser `RuntimeStoreError("store is unavailable: cancel_tool")`): pristine → `1 passed` (CancelledError propagates); with the guard removed → `RuntimeStoreError: store is unavailable: cancel_tool` replaces the cancellation. That is exactly the I-2 defect shape, on the tool path.
- Severity rationale: The code is currently correct; the defect is evidential — a protection claimed as fixed in the ledger cannot be shown to exist by the suite, and the mutation standard this round uses for every other claim ("mutation is CAUGHT") fails here.

### M-1 The I-1 red test does not exercise the approval-bearing half of `_authorize`
- Where: `backend/tests/test_task2_round2_mcp_security.py:1414-1491` (the `_FakeRuntime` defines only `store.get_run`, `consume_budget`, `settle_budget`, `cancel`).
- What: The test's payload is ordinary text, so `PiiDlpGateway().decide_outbound` returns `requires_approval=False` and `runtime.prepare_tool` / `store.claim_tool_execution` are never reached. What is pinned is the gate on the non-sensitive path only; a fake stand-in that also implemented `prepare_tool` would never be exercised by this test.
- Evidence: the test passes with a `_FakeRuntime` that has no `prepare_tool` attribute (AttributeError would otherwise surface); `_authorize` only calls `prepare_tool` inside `if decision.requires_approval:` (`commercial_gateway.py:2477`).
- Severity rationale: Minor — scope of a test, not a live defect (the `isinstance` gate sits before all of it).

### M-2 One count claim in the commit message is not reproducible as stated
- Where: commit message of `448c4c3` — "full backend suite 57 failed / 981 passed / 5 skipped / 20 errors".
- What: At the same revision I measure `56 failed, 982 passed, 5 skipped, 20 errors, 10 subtests passed`. The one-test swing is the documented flaky case `test_task_runtime_quarantine_isolation::test_limit_two_overflow_never_starves_valid_confirmed_deletion`, which passed at HEAD in my run and failed in my baseline run. Not a regression; recorded only because count claims keep being quoted.
- Evidence: `py -3.12 -m pytest backend/tests --continue-on-collection-errors -q --tb=no -rf` in `%TEMP%\sc-448` (238.93s) and in `%TEMP%\sc-base` (230.56s).
- Severity rationale: Minor — the failure-**name** diff (below) is clean, which is the standard that matters.

## Verified as genuinely closed

- **I-1 (the defect)**: removing the `isinstance(runtime, TaskRuntimeService)` check (`commercial_gateway.py:2439-2442`) → `test_a_duck_typed_runtime_cannot_authorize_provider_egress` **FAILED** (`1 failed, 43 passed`).
- **I-3 / M-4, both paths**: narrowing `chat`'s clause (`:2823`) back to `(GeneratorExit, asyncio.CancelledError)` → `test_aborted_chat_is_audited_as_cancelled_not_failed[KeyboardInterrupt]` and `[SystemExit]` **FAILED** (`2 failed, 42 passed`); narrowing `chat_stream`'s clause (`:2864`) identically → `test_aborted_stream_is_audited_as_cancelled_not_failed[KeyboardInterrupt]`/`[SystemExit]` **FAILED** (`2 failed, 42 passed`). All three exception types are covered on both paths.
- **I-4**: dropping the `wall_clock_seconds` guard (`service.py:581-588`) → parameter `[wall_clock_seconds-1.0-60.0]` **FAILED**; dropping `cost_usd` (`:589-594`) → `[cost_usd-0.01-1.0]` **FAILED**; widening the tolerances to `1e9` (`:584`, `:590`) → the corresponding parameter **FAILED** in each of the two runs. All five dimensions are genuinely parameterised.
- **I-2 (provider path)**: the guard is present (`commercial_gateway.py:2761-2781`) and removing just the `try/except` around the terminal record → `test_cancellation_is_not_replaced_by_a_failing_audit_write` **FAILED** (`1 failed, 43 passed`).
- **Defeating the `isinstance` gate**: a subclass of `TaskRuntimeService` with a fake `store.get_run` returning `TaskRuntimeStatus.RUNNING` defeats it — I patched the duck-typed test's `_FakeRuntime` into `class _FakeRuntime(TaskRuntimeService)` and the assertion failed with `Failed: DID NOT RAISE GatewayDenied` (`1 failed`), i.e. authorization passed and `client.chat()` returned normally (MockTransport, no real socket). So the code comment's stated boundary ("stops a hand-written stand-in, not a determined caller") is accurate; a `MagicMock(spec=TaskRuntimeService)` case was **not** measured.
- **Drift guard is not vacuous**: in a copy with a junctioned `node_modules`, `npx vitest run src/contract/publicEvents.drift.test.ts` → `2 passed`. Mutation "drop `classification` from `run_queued`" → FAILED (`expected [ 'classification', 'kind', …(3) ] to deeply equal [ 'kind', 'route_label', …(2) ]`); mutation "add `brand_new_event`" → FAILED (18 vs 17 names); mutation "rename `_PUBLIC_EVENT_FIELDS`" → FAILED closed with `could not parse _PUBLIC_EVENT_FIELDS ... a guard that silently passes is worse than none`. `src/contract/publicEvents.drift.test.ts:31-37` is real.
- **ToolGateway's new `cancelled` terminal state is consistent**: `store.cancel_tool` (`store.py:1410-1426`) only accepts `running`; `store.incomplete_tools()` (`:1280-1285`) filters `status IN ('prepared','running')`, so a cancelled invocation is not re-labelled `recovered_blocked` by restart recovery; the emitted `tool_cancelled` payload keys (`store.py:1351-1365`) are exactly the whitelist entry (`contracts.py:82-85`, `src/contract/taskRuntime.ts:175-178`). No recovery/frontend/contract inconsistency found.
- **conftest `_secret_lease_broker` reset**: `runtime_settings._secret_lease_broker` is assigned only by `server.py:466` (inside server construction) and by the test helper `support_authority.py:160-166`, which is a context manager that saves/restores; the autouse per-test reset therefore cannot strand a broker. No isolation problem found.
- **Zero new failures (independent)**: HEAD `56 failed / 982 passed / 5 skipped / 20 errors` vs baseline `e9892ae` `57 failed / 963 passed / 5 skipped / 20 errors`; failure-**name** diff contains **no name present only in the new tree**; the only asymmetric name is the documented flaky `test_limit_two_overflow_never_starves_valid_confirmed_deletion` (failed in the baseline run, passed at HEAD).

## Claims I could not verify

- Whether `MagicMock(spec=TaskRuntimeService)` / a `__class__` lie also defeats the `isinstance` gate (mechanism obvious, end-to-end **not** measured; only the subclass route was).
- The ledger's "the review reproduced it [the `_secret_lease_broker` leak] on the `e9892ae` baseline too" (`progress.md:62`) — not re-measured.
- `support_authority.py`'s docstring claim that the fifth review measured a forwarding `client_factory` handler reaching a real socket — accepted as a quotation, not re-measured.
- Whether any test outside `test_task2_round2_mcp_security.py` covers the I-2 tool-path guard: I did not run the full suite under that mutation; the grep over `backend/tests/*.py` for `cancel_tool` returns only that file, which is strong but indirect evidence.
- `247c9ae` / `b0defa8` / `ee85cd6` were not reviewed (outside the requested revision); I only confirmed that `247c9ae` replaces the false sentence with the store-rebuild argument.
- Not re-derived, per instructions: M-5/M-6/M-9 implementations, the named test seam, the authority fixture, and the `7cef6b3` frontend whitelist fix.
```
