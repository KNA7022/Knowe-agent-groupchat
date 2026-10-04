# Spec Compliance

- ❌ Issues found: the quarantine overflow repair remains incomplete when the first restart-from-head page read fails once. `backend/task_runtime/service.py:1530` passes a one-call cursor override after clearing overflow debt; the error return leaves the old cursor in place, allowing subsequent retries to forget the affected intents. Important I-1 below blocks acceptance of this repair increment.
- ✅ The remaining reviewed changes match the baseline-repair brief, including its explicit disposition to retain fail-closed unowned feedback cards and defer durable card/run ownership. No extra feature, policy bypass, new skip/xfail, collection exclusion, removed test partition, or provider/network effect was added in the diff. Replaced historical assertions have explicit current-policy behavioral replacements: `backend/tests/test_v1017_invariants.py:68`, `backend/tests/test_v1019_external_roots_autoauth.py:12`, `backend/tests/test_zinnia.py:162`, and `backend/tests/test_v22_worker_voice.py:101`.
- ⚠️ Cannot independently verify executed final-suite totals, frozen-state timing, compileall, Node24 checks, diff-check output, or absence of warnings from the diff/report alone. `backend-baseline-report.md:62` records the complete backend gate as 1619 passed, 5 skipped, 10 subtests passed, followed by compileall/frontend checks. Controller should retain the original final command outputs. No reported suite was rerun during review.
- ⚠️ Durable card/run ownership, successful real UI aux adjustment, production Windows sandbox execution, actual-model language compliance, and release packaging are explicitly outside the delivered evidence. The limitations in `backend-baseline-report.md:35` and `backend-baseline-report.md:43` must remain release gaps; this review does not turn them into accepted available features.

# Strengths

- `backend/tests/test_runtime_impl_v10184.py:342` exercises the real external-read handler's protected-root guard, then separately asserts the governed legacy-adapter denial. It does not mistake denial before the handler for evidence of the handler guard.
- `backend/tests/test_v1019_external_roots_autoauth.py:12` gives the structured grant positive case an observable inert filesystem effect and tests missing approval, mismatched task, outside scope, and a fresh revoked-grant branch before any effect. The three prompt-derived grants become negative assertions instead of restoring automatic permission.
- `backend/tests/test_v26_morph.py:339` invokes the actual server feedback command with an inert gateway transport, removes inherited run authority, includes plausible renderer/card run IDs, and checks unchanged pending state, one visible failure, same-card replay and zero egress. This respects the brief's unowned-card disposition. The independent behavior fixture uses a real running service and purpose-bound aux lease; it is not represented as successful UI ownership integration.
- `backend/tests/test_v22_worker_voice.py:116` tests both configured languages and explicit/multilingual overrides through Worker factory, Runtime and a real governed provider client with a mock transport. `backend/worker_gateway_runtime.py:398` and `backend/worker_prompt.md:29` keep one canonical protocol and pass the active configured language without task-language inference or output translation. Raw response passthrough remains asserted.
- `backend/tests/test_task_runtime_quarantine_isolation.py:375` supplies a deterministic regression for overflow entries behind the current page, preserves the fixed cohort bound, and checks exactly one successful quarantine write and an idle retry without duplicate writes. It correctly catches the healthy-read version of the bug.
- `backend/tests/test_zinnia.py:207` covers unauthenticated no-effect and authenticated explicit group creation at the loopback HTTP boundary, then checks canonical group/project identity, chosen workspace, mandatory roles and no invented run. The legacy conversational denial stays explicit; the existing manual approval/card name and rename checks receive actual temporary project directories.
- Mechanical repairs preserve their prior assertions: portable lifecycle command (`backend/tests/test_batch4_terminal.py:225`), pytest-owned temporary directories and UTF-8 memory reads/writes (`backend/tests/test_v012_memory_platform.py:14`), explicit disabled compression (`backend/tests/test_token_usage_m4.py:44`), actual backend source roots, stable coordinator IDs with localized names, and the full tool trace under real authority (`backend/tests/test_history_trace.py:48`).

# Issues

## Critical (Must Fix)

- None found.

## Important (Should Fix)

- **I-1 — A transient read failure during the restarted sweep still loses overflow recovery debt.** `backend/task_runtime/service.py:1530` (with state clearing at `:1508` and the early page-read error return at `:1607`). The retry frees its bounded cohort and clears `_deferred_quarantine_overflow`, then requests `cursor_override=""`. If that single page read raises `RuntimeStoreError`, reconciliation returns `view_unavailable=True` without changing `_deletion_reconcile_cursor`. On the next retry there is no overflow flag and no remaining cohort, so reconciliation resumes from the old tail. Reaching that tail clears `has_more` and the service becomes idle, leaving the overflow intents in `prepared` indefinitely until some independent sweep/restart rediscovers them. This is a remaining defect in the specific overflow repair, not a claim that the original implementation handled read failures. Preserve a pending restart-from-head obligation through read failure and clear it only when the reset sweep has made durable/in-memory cursor progress; keep batch/cohort limits and backoff. Add the single-failure regression below and require finite recovery of every invalid intent and eventual idle state.

## Minor (Nice to Have)

- None raised.

# Focused checks and reproduction

- **Named risk: recovery cursor restart semantics under transient store unavailability.** Inspected the continuation of the cut-off changed retry function, `_reconcile_group_deletions_locked`, and its cursor/error handling in `backend/task_runtime/service.py:1451`, `:1497`, `:1558`. The healthy reset remains bounded (`:1577`) and quarantined rows are not rewritten (`:1623`); the error path does not preserve the restart obligation. The one focused probe below confirms I-1. The first probe invocation failed at import because standalone execution lacked the backend source path; the successful invocation adds it explicitly. No pytest suite was run.
- **Named risk: fabricated fixture authority or grant-policy bypass.** Inspected `backend/tests/support_authority.py:73` and the two imported helpers at `backend/tests/test_commercial_security_gateway.py:38` and `:49`. They create real durable groups/runs, schedule through Task Runtime, provision a purpose-bound credential lease, and resolve a structured approval through the actual service. New provider transports in the diff return in-memory HTTP responses. The aux control-command regression explicitly clears authority rather than treating the behavior fixture's independent run as card ownership.
- **Named risk: global configuration leakage or user credentials behind repaired fixtures.** Inspected `backend/tests/conftest.py:21` and `:65`, plus the cut-off setup of `backend/tests/test_token_usage_m4.py:20`. The autouse baseline resets all Config fields before/after each case, uses a fixed test runtime token, defaults to fake agents and clears provider credentials and runtime settings. Consequently the new explicit compression setting does not leak into later tests, and the new loopback creation test uses a fixture credential rather than a user's credential.
- **Scope and evidence check:** read the task brief, implementation report, supplied BASE-to-HEAD diff and triage/baseline failure summary. Review range: `71998357e9b8380d2d5f34c846cb24e4d3f1188d` → `9e5264cf94d5455d3b7886a4e51478476274b87c`. No checkout/index/branch mutation, code edit, provider call, dependency installation or suite rerun. Only this requested report was written to the checkout.

The successful probe ran from `D:/agentwork/StudyCrew-worktree` with `PYTHONDONTWRITEBYTECODE=1` and `PYTHONIOENCODING=utf-8`, piping this script to `py -3.12 -`. Temporary runtime data was owned and cleaned by `TemporaryDirectory`; timer scheduling alone was held inert.

```python
import sys, tempfile
sys.path.insert(0, "backend")
from pathlib import Path
from backend.groups import GroupRepository
from backend.task_runtime.service import TaskRuntimeService
from backend.task_runtime.store import RuntimeStoreError

TaskRuntimeService.DEFERRED_QUARANTINE_LIMIT = 2
TaskRuntimeService.DELETION_RECONCILE_BATCH_SIZE = 2
TaskRuntimeService._arm_deferred_quarantine_retry_locked = lambda self: None
with tempfile.TemporaryDirectory(prefix="review-quarantine-") as directory:
    root = Path(directory)
    service = TaskRuntimeService(root, GroupRepository(root))
    try:
        ids = ["grp_" + digit + "0" * 31 for digit in ("0", "1", "e", "f")]
        tail = "grp_" + "f" * 32
        for i, gid in enumerate([*ids, tail]):
            service.store.prepare_group_deletion(
                gid, f"gdt_probe_{i}", group_proof_digest=f"{i+1:064x}",
                confirmation_token=f"fixture-{i}", runtime_project_id=f"project_probe_{i}",
            )
        quarantine = service.store.quarantine_group_deletion
        quarantine(tail, "already_quarantined")
        def unavailable(*args, **kwargs):
            raise RuntimeStoreError("inert transient failure")
        service.store.quarantine_group_deletion = unavailable
        service.reconcile_group_deletions()
        service.reconcile_group_deletions()
        service.store.quarantine_group_deletion = quarantine
        page = service.store.group_deletion_intents_page
        service.store.group_deletion_intents_page = unavailable
        service._retry_deferred_quarantines()
        print("after one failed reset scan:", service._deferred_quarantine_overflow,
              service._deletion_reconcile_has_more, service._deletion_reconcile_cursor)
        service.store.group_deletion_intents_page = page
        for _ in range(5):
            service._retry_deferred_quarantines()
        print("stages:", [service.store.group_deletion_intent(gid)["stage"] for gid in ids])
        print("remaining debt:", service._deferred_quarantines,
              service._deferred_quarantine_overflow, service._deletion_reconcile_has_more,
              service._deletion_reconcile_cursor)
    finally:
        service.close()
```

Observed successful probe output (exit 0):

```text
after one failed reset scan: False True grp_f0000000000000000000000000000000
stages: ['quarantined', 'quarantined', 'prepared', 'prepared']
remaining debt: {} False False
```

Expected: after the transient page failure is removed, bounded retries must revisit the head and reach the overflow entries, yielding four `quarantined` stages before all cohort/overflow/page debt is cleared. The current terminal state proves there is no scheduled retry obligation left; additional idle retries cannot fix it.

# Assessment

**Task quality: Needs fixes.** Counts: 0 Critical, 1 Important, 0 Minor.

**Reasoning:** The Windows/fixture repair and current-policy test replacements are focused and substantively preserve intent. The one production recovery correction needs a failure-path completion before this increment can be trusted; the language fix and explicitly fail-closed aux disposition otherwise satisfy the reviewed scope.
