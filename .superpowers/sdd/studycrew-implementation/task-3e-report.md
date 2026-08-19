# Task 3E implementation report

## Status

DONE

## Exact red reproduction

- Added the real two-event-loop reproduction before changing production code.
- Loop 1 bound the Server recovery owner, failed once, and armed a real 60-second
  `TimerHandle`. The loop was then closed without normal Server shutdown.
- On loop 2, all three source variants (`canonical`, `runtime`, and `both`) failed at
  `old_handle.cancelled()`: the old handle remained live, its tokens survived, and
  later insertion wakes could not publish a replacement owner.
- Red result: `3 failed, 57 deselected`.

## Design

### Atomic lifecycle retirement and publication

- Loop binding now accepts a distinct predecessor only when that loop is stopped or
  closed; a concurrently running foreign loop is still rejected.
- Under the existing lifecycle lock, rebind first increments the lifecycle generation,
  snapshots durable invalidation debt, clears the old owner identity, retry handle and
  both retry tokens, resets process-local backoff, and cancels the retired handle.
- A done owner has its exception retrieved. A pending owner on a stopped reusable loop
  is cancelled for later drain; a pending owner on a closed loop is never passed to an
  unsafe loop operation and is detached behind the generation fence.
- Only after the old resources are retired is the new loop published. The preserved
  invalidation is consumed by the foreground reconciliation entry point or by exactly
  one new-generation owner.

### Generation-fenced callbacks

- Canonical and Runtime insertion listeners are now per-generation closures. A listener
  captured from loop 1 returns before changing invalidation epoch, wake ownership,
  backoff or scheduling after loop 2 is published.
- Timer firing validates stop/shutdown, lifecycle generation, loop identity and both
  retry tokens before clearing any handle or token. A stale timer callback is therefore
  strict zero-mutation.
- The loop-entry callback no longer clears current wake ownership when it runs on the
  wrong lifecycle. Owner settlement already retrieves exceptions before checking its
  full task/loop/generation/token identity and remains zero-mutation when stale.
- Handle cancellation is contained for already-cancelled, fired, closed-loop and
  inaccessible cleanup boundaries.

## Focused acceptance coverage

- Closed loop plus live 60-second retry: canonical-only, Runtime-intent-only, and
  both-source insertions automatically commit on loop 2. Old handle and tokens are
  absent, and old listener/timer callbacks cannot alter loop-2 state.
- Stopped loop plus pending owner: loop 2 cancels and detaches the owner, handles 100
  and 1,000 real foreign-thread Runtime intent insertions with
  `active owner + active handle <= 1`, commits the valid canonical work, quarantines
  every missing-authority intent, and remains unchanged when loop 1 later drains the
  cancelled owner.
- Loop 2 to loop 3 migration proves a stopped clean loop can be rebound again; loop-2
  listeners and loop-1 owner callbacks are stale no-ops. Immediate loop-3 shutdown
  leaves no owner or retry resource.
- Cancelled handles paired with successful-done and exception-done owners are cleared;
  exceptions are retrieved and later manual settlement is a no-op.
- All seven Task 3E exact cases pass with real stopped/closed loops and durable stores.

## Verification evidence

- Final quarantine/fairness/crash/generation/wake/error/lifecycle/rebind suite:
  `64 passed in 30.47s`.
- Task 3E exact subset after upgrading the stress cases to real durable insertions:
  `7 passed in 15.11s`.
- Complete Task 1-3 Group/HTTP/Task Runtime/live bridge/recovery/deletion suites:
  `232 passed in 71.41s`. Two preceding attempts exposed the existing Windows pressure
  behavior (one quarantine Timer timeout and one transient `PermissionError` causing a
  safe idempotent replay); both exact cases passed in isolated/focused reruns before the
  clean complete run.
- Legacy Engine suites: `26 passed, 10 subtests passed in 1.97s`.
- Frontend Group/Task Runtime contracts, stores and real socket transport:
  `5 files / 45 tests passed`.
- Node `v24.19.0`, Python `3.12.10`; `npm run test:foundation` passed command tests and
  the StudyCrew Windows runtime/package verifier.
- ESLint, targeted Python compilation and `git diff --check` passed. Git emitted only
  the repository's existing Windows LF-to-CRLF notices.

## Changed files

- `backend/server.py`
- `backend/tests/test_task_runtime_quarantine_isolation.py`
- `.superpowers/sdd/studycrew-implementation/task-3e-report.md`

## Residual risks

- A closed event loop cannot safely execute cancellation for a Task that its caller
  abandoned while pending. Rebind now detaches that Task from every Server lifecycle
  resource, publishes a distinct reconciliation lock, suppresses the generic CPython
  destruction warning in favor of a bounded opaque diagnostic, and generation-fences
  every continuation. Durable source revisions and authenticated prepared records
  remain the recovery authority.
- The existing Windows filesystem/Timer pressure probes remain timing-sensitive. Their
  failure modes are fail-closed and idempotent, and both passed in the final focused
  and complete suites after isolated reruns.

## Commit

This report is included in the focused Task 3E commit. The immutable commit hash is
reported in the handoff because a commit cannot embed its own hash.

## Independent review fix round 1/5

### Status

DONE

### Lifecycle-owned reconciliation serialization

- The independent held-lock probe was admitted first. A real recovery owner suspended
  inside `_run_confirmed_group_delete` while holding the production reconciliation lock.
  Loop 2 published successfully but timed out on the inherited lock for both stopped
  and closed loop-1 variants. Red result: `4 failed, 64 deselected`.
- Every loop lifecycle now owns a distinct `asyncio.Lock`. Rebind increments generation,
  retires owner/retry identities, then atomically publishes the new lock, loop and
  generation-scoped listeners under the lifecycle lock. Loop 2 can never wait on a
  primitive retained by loop 1.
- Each reconciliation captures an immutable `(loop, lifecycle generation, lock)` fence.
  It validates that fence after lock acquisition, after every deletion await, before
  and after durable effects, before and after the atomic source checkpoint, and before
  publishing process-local recovery state. A late old-generation continuation raises
  `CancelledError` before it can mutate new-generation state.
- A stopped-loop Task receives cancellation and may be drained later on its original
  loop; its settlement is stale and releases only the retired lock. A closed-loop Task
  is never passed to an unsafe cancellation operation. It is detached without retaining
  the Task/content, the new generation receives a fresh lock, and an opaque diagnostic
  records only retired/active generation numbers and lifecycle status.

### Exact acceptance coverage

- Parameterized real-path probes stop or close loop 1 at `after_group_prepare` and
  `after_runtime_intent`, covering both sides of the idempotent Runtime-intent effect.
  All four now converge on loop 2 without running loop 1.
- The stopped variants drain loop 1 only after loop 2 commits and assert that the late
  cancellation/settlement changes no loop-2 owner, token, handle, backoff, epoch or
  canonical transaction state.
- Both loop exception handlers are captured and garbage collection is forced. No
  pending-Task destruction or un-retrieved exception context is emitted.
- Prior loop2/loop3 migration, same-loop owner/retry mutual exclusion, 100/1,000 real
  foreign-thread insertions, durable checkpoint/crash replay and idempotent deletion
  tests remain green.

### Verification evidence

- Held-lock stopped/closed exact probes: `4 passed, 64 deselected in 1.96s`.
- Complete quarantine/fairness/crash/generation/wake/error/lifecycle/rebind suite:
  `68 passed in 31.46s` on the final post-diagnostic rerun.
- Complete Task 1-3 Group/HTTP/Task Runtime/live bridge/recovery/deletion suites:
  `236 passed in 74.24s`.
- Legacy Engine suites: `26 passed, 10 subtests passed in 1.18s`.
- Frontend Group/Task Runtime contracts, stores and real socket transport:
  `5 files / 45 tests passed in 12.94s`.
- Node 24/Python 3.12 foundation and Windows package verification passed.
