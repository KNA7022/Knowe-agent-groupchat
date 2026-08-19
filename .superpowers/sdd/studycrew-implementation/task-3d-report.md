# Task 3D implementation report

## Status

DONE

## Red reproduction

- Added the exact reviewer probe with `DELETION_RECONCILE_BATCH_SIZE=2`: two
  permanent Task Runtime `complete` receipts plus one canonical-only `prepared`
  transaction.
- Before the fix, six foreground reconciliation passes left the canonical Group
  present and fenced. The focused command failed in `0.87s` with
  `canonical-only prepared deletion was starved`.
- The same probe now passes for both batch sizes `1` and `2`, completes the
  canonical deletion in bounded turns, and proves the owned continuation task
  terminates.

## Design

### Independent durable source ownership

- Server reconciliation no longer gives the Task Runtime page first claim on a
  shared candidate budget. Runtime intents and canonical Group transactions receive
  independent non-zero quotas whose sum never exceeds the configured batch.
- The next source turn and each source's sweep state are persisted in the Task Runtime
  scheduler table. Batch size `1` uses durable round-robin arbitration; larger batches
  split capacity and rotate the odd slot.
- The Server now owns a distinct `server_task_runtime_source` cursor. The Task Runtime
  quarantine Timer retains `task_runtime_service`. This removes the race where one
  owner could reset the shared cursor while the other was paging, which previously
  caused a batch-1 continuation loop.

### Terminal lane, retry sweeps, and backoff

- Permanent `complete` runtime receipts prune only bounded in-memory bridge state and
  never consume actionable candidate capacity.
- Source state follows `pending -> failed -> retry -> done`. A failed identity remains
  represented by its durable canonical/runtime row; the stable cursor continues to
  later pages before a retry sweep wraps. Only a complete failure-free sweep reaches
  `done`.
- A permanent candidate therefore cannot pin healthy entries before or after it, and
  is not silently forgotten when the cursor reaches the end.
- Continuations with no successful destructive progress use bounded exponential idle
  backoff (`50ms` to `1s`) instead of the fixed `10ms` active cadence. Successful
  progress resets the backoff.

### Bounds and preserved invariants

- Runtime page sample, canonical page, candidate map, diagnostics, and per-turn work
  remain capped by the configured batch. Both runtime and canonical 10,000-row probes
  materialize only two visible rows per page.
- Canonical proof validation, per-Group quarantine checks, authenticated deletion,
  recovery debt, idempotent receipts, crash replay, and privacy-safe diagnostics remain
  unchanged. Logs contain only opaque Group IDs and exception type names.

## Focused acceptance coverage

- Batch `1` and `2`: terminal runtime receipts cannot starve a canonical-only prepared
  transaction; both continuations terminate.
- Reverse skew: permanently blocked canonical pages cannot starve an actionable runtime
  intent.
- Multiple pages on both sources with a restart between pages: the Server cursor is
  unchanged by Task Runtime constructor/Timer work, resumes strictly after its durable
  position, and every remaining actionable deletion executes once.
- A permanent failed candidate before healthy later pages remains retryable, later pages
  complete, and the poison-only sweep enters idle backoff.
- Terminal-only pages use idle backoff and keep total quota/candidate/report collections
  within the configured limit.
- 10,000 runtime intents and 10,000 canonical transactions retain two-row page/sample
  bounds and monotonic cursors.

## Verification evidence

- `python -m pytest backend/tests/test_task_runtime_quarantine_isolation.py -q`
  -> `14 passed in 6.03s`.
- Complete Task 1-3 Group/HTTP/Task Runtime/recovery/deletion focused suites
  -> `182 passed in 50.24s`.
- Legacy Engine regressions
  -> `26 passed, 10 subtests passed in 1.91s`.
- Frontend Group/Task Runtime contracts, stores, and real socket transport
  -> `5 files / 45 tests passed in 12.72s`.
- Node `v24.19.0`, Python `3.12.10`; `npm run test:foundation` passed Python command
  tests and the StudyCrew runtime/Windows packaging verifier.
- `npm run lint` passed without warnings.
- Targeted Python compilation and `git diff --check` passed. Git emitted only the
  repository's existing Windows LF-to-CRLF notices.
- Broad `npm run typecheck` still reports the documented pre-existing UI/legacy fixture
  baseline (missing `Conv.unread`, stale fake bridge/socket interfaces, legacy ES lib
  targets, and unrelated component errors). Task 3D changes no TypeScript files and all
  focused frontend suites remain green.

## Changed files

- `backend/server.py`
- `backend/task_runtime/service.py`
- `backend/tests/test_task_runtime_quarantine_isolation.py`
- `.superpowers/sdd/studycrew-implementation/task-3d-report.md`

## Residual risks

- `GroupRepository` remains the existing single-user JSON snapshot and therefore loads
  the registry itself as a whole. The reconciliation page and all derived collections
  are bounded, but replacing the underlying registry with a database is outside Task 3D.
- Runtime/canonical cursors, both source states, and the next turn now commit in one
  SQLite transaction after candidate effects. A crash may replay a bounded page, but
  immutable proofs and idempotent deletion receipts prevent duplicate destructive
  effects.
- A permanent storage/permission failure intentionally remains fail-closed and retries
  with capped backoff until storage is repaired; it is never auto-cleared.

## Commit

This report is included in the single focused Task 3D commit. Its immutable Git hash is
reported in the handoff because a commit cannot embed its own content hash.

## Independent review fix round 1/5

### Status

DONE

### Effect-before-advance and atomic source checkpoint

- The Server asks `TaskRuntimeService` to reconcile its page without persisting the
  Server-owned cursor. Runtime cleanup may complete idempotently, but the durable
  Server frontier stays at the old page until every candidate effect either succeeds
  or is classified into durable retry debt.
- Runtime cursor, canonical cursor, both sweep states, and next-source turn now publish
  in one SQLite transaction after candidate processing. `CancelledError` at any prior
  await leaves the complete page replayable after restart.
- A page whose first candidate committed and second candidate was cancelled replays
  safely: the committed canonical transaction is excluded, remaining candidates each
  execute once, and no destructive effect is duplicated.
- Checkpoint failure both before the SQLite transaction and after an ambiguous committed
  transaction is safe. The owned loop remains pending, converges automatically, and
  the authenticated deletion effect executes once.

### Authoritative-view retry debt

- `(blocked=True, view_unavailable=True)` now marks the canonical source failed before
  the page checkpoint. On the last page this becomes `retry`, not `done`, and the owned
  continuation observes storage restoration without a foreground call.
- Runtime `view_unavailable`, non-empty `last_error`, and bounded `deferred` reports all
  carry the same durable failure debt through later pages and a retry sweep.
- Permanent outages remain fail-closed and use the existing capped exponential idle
  backoff. A clean sweep after recovery is the only transition to `done`.

### Exact boundary coverage

- The two independent reviewer probes first failed exactly as reported: the cancelled
  candidate remained `prepared` after restart, and the one-shot authority outage had no
  owned continuation. Both now pass.
- Parameterized crash probes cover every deletion boundary: Group prepare, runtime
  suspend, runtime intent, runtime cleanup, legacy runtime deletion, Group commit, and
  runtime completion. Every restart converges to canonical `committed` plus runtime
  `complete` with the pre-effect cursor unchanged.
- Mixed-page cancellation, checkpoint pre/post-commit uncertainty, canonical exact-view
  outage, runtime `deferred`, and runtime `last_error` all recover autonomously.

### Verification evidence

- Final Task 3D quarantine/fairness/crash suite: `28 passed in 7.87s`.
- Complete Task 1-3 Group/HTTP/Task Runtime/recovery/deletion suites:
  `196 passed in 53.62s`.
- Legacy Engine suites: `26 passed, 10 subtests passed in 1.84s`.
- Frontend Group/Task Runtime contracts, stores, and real socket transport:
  `5 files / 45 tests passed in 12.56s`.
- Node `v24.19.0`, Python `3.12.10`; foundation/runtime verification, ESLint,
  targeted Python compilation, and `git diff --check` passed.
- The documented pre-existing broad TypeScript baseline is unchanged; this round
  modifies no TypeScript files.
