# P2b2a — protected child discovery and cleanup report

Status: implemented and locally verified; independent task gate pending. Base: `ad2105f0d64bb8b6ec4ba7a4bf300919f74f572c`. This is the prerequisite split authorized by the controller, **not completion of P2b2, Unit P, or commercial readiness**.

## Scope and implementation

Added `backend/protected_children.py`, a fixed, encrypted, bounded ownership catalog for the existing selected completion SQLite database and task-envelope directory. Changed only P1's narrow revoked-row cleanup extraction and TaskRuntime startup/deletion/close/recovery coordination, plus `backend/tests/test_protected_children.py` (39 cases). No Worker, Coordinator, Gateway, Engine dispatch, frontend, dependency or skill changes.

The Backend Python skill informed real storage fixtures, explicit lifecycle cleanup and error-path tests. No subagents were spawned. Controller-owned briefs/progress/dependency reports are excluded from this commit.

### Canonical enrollment order

1. Actual canonical Group, TaskRuntime owner and approval cards supply the existing P1 lineage. Test fixtures use those typed APIs, not a fabricated completion/Worker to authorize enrollment.
2. `ProtectedChildren.register(access)` requires the exact live selected `ProtectedAccess` from the same runtime, under the runtime lock and unchanged `guard(write=True)` rules. It verifies the Group/project binding.
3. The verified runtime DB must be `<data-root>/runtime/task-runtime-v1.sqlite3`. Existing `workspace_layout.internal_workspace_for` supplies the same project workspace used by Server `_internal_workspace_for` and Engine construction. Fixed child paths are `<project>/runtime/runtime.sqlite3` and `<project>/runtime/task-envelopes`. Existing helper validation is reused; no second project regex and no caller-supplied path.
4. Store IDs use existing P1 keyed path identity. Domain-separated AEAD/MAC binds encrypted Group/project/task/path/store information to the verified runtime key/store. Task ID is the sole clear catalog identifier. Closed fields, bounded lengths, canonical JSON, a 16 KiB ciphertext ceiling and 10,000-entry limit fail closed. Windows junction/symlink and changed-resolution components are rejected before following locations, including on reopen.
5. Enrollment commits synchronously before child content. It creates no envelope, completion database, record claim, outbox, grant or execution effect. An existing identical enrollment is idempotent. New enrollment after any private record claim is denied: this is not unknown-location backfill. The future Worker composer must call this API before its first child write; this slice does not silently reroute every P1 caller.

### Revoke / cleanup / retirement order

Existing P1 revocation is persisted first. Under the same runtime lock, selected writes and readers continue to obey the original fences. Cleanup uses an authenticated **already-revoked, deletion-only** private primitive; it does not acquire a live reader, restore RUNNING, claim work or weaken terminal guards.

Cleanup authenticates the catalog, then purges accepted P2b1 events/cache, P1 completion/journal/result/outbox/receipt rows, and only claimed envelope files in that opaque task directory. Strict cleanup demands independently authenticated ownership for selected SQL rows; an unclaimed row, unknown envelope file, changed location, orphan SQLite sidecar, missing key or corrupted metadata leaves the catalog obligation intact and raises an explicit recovery error. Shared DB files are not deleted. Unrelated lineage and legacy rows remain usable. Absent artifacts are accepted only at their authenticated fixed locations. Parent directories, unrelated files and unknown paths are not scanned/deleted.

Only after all child classes are confirmed absent is the private path descriptor retired. P1 revocation receipts/claims and accepted content-free event high watermark remain. Fault checkpoints after revoke/events/rows/envelopes and immediately before retirement demonstrate idempotent retries. Runtime DB, child SQLite and filesystem operations are separate commits: this is an ordered durable obligation, not a cross-database transaction.

Actual `TaskRuntimeService` startup retries already revoked obligations, retains unrevoked enrollments without executing/claiming/ACKing, and closes its catalog on shutdown. Existing Group prepare/clean/commit/complete paths invoke the same cleanup. Archive remains a fence, not purge. Invalid-proof quarantine alone is not deletion authority: its unrevoked body remains retained but access denied; an independently durable revoke can be retried while quarantined.

### Narrow recovery correction (approved escalation)

The original RUNNING + invalid-proof quarantine + real restart test exposed `recover_after_restart` attempting the forbidden PAUSED mutation, crashing the constructor. The original regression remains, rather than modifying its fixture to PAUSED. Both executing-run recovery and queued-continuation recovery now skip only positively identified deletion/quarantine fences. Their original status remains unchanged and is not reported as recovered/paused/succeeded.

Existing `service.recovery_report` exposes `fenced_runs: [{run_id, reason}]`, with only `group_quarantine_fence` / `group_deletion_fence`. A fixed backend warning contains the already contract-validated opaque run ID and closed reason, no Group name/path/body/raw exception. Tests cover report/caplog, malformed IDs, healthy Group recovery and a newly committed fence race; unknown storage/codec errors still propagate. Mutation guards remain unchanged. This is a backend diagnostic only: **no new UI alert or API surface is claimed**.

## Verification

Raw command/result excerpts and original RED are in `protected-child-cleanup-p2b2a-evidence.md`.

| Gate | Actual result |
|---|---|
| New child matrix | 39 passed, 9.77s |
| P1/P2a/P2b1/child/quarantine focused matrix | 274 passed, 81.00s |
| Frozen Python 3.12 complete backend | 1926 passed, 5 existing skipped, 10 subtests passed, 216.66s |
| Python 3.12 compileall backend | Exit 0 |
| Node 24.9.0 wrapper: parity/publicEvents/taskRuntime mirrors | 3 files / 25 tests passed, 3.53s |
| Diff check | Exit 0; existing LF→CRLF advisory warnings |
| Additional real two-Group deletion probe | Selected started lineage cleaned; other Group body readable and catalog retained; exit 0 after probe handle cleanup correction |

Full backend is baseline 1887 + 39 cases, no new skip/xfail/exclusion. No TS source changed. Controller owns this round's separate full frontend 588/55-file, typecheck/lint and foundation 33 evidence; those are not represented as re-run by this implementer. Source/tests were frozen for the full gate and subsequent probes; hashes recorded in evidence.

Coverage includes no-content enrollment, envelope-only, started-only, completed and event-present states; selected raw marker scans; repeated retry/delete; real close/reopen with unchanged child rows/claims; COORDINATOR fixture ACK receipt and pending DELIVERY ownership; real Hub event hydration and rejection after revoke; two lineages in one DB plus legacy; deleting highest protected sequence 3 and reopening to emit 4; Group deletion and unrelated Group retention; forged encrypted descriptors/claims; missing/wrong key; real Windows junctions; partial schemas/orphan sidecars; deterministic late writer after revoke; revoked and unrevoked quarantine; healthy recovery; unknown error propagation.

Fixture mistakes encountered during iteration (enum SQL literal, missing typed completion evidence/envelope IDs and expected wrapped error) were corrected and are **not product RED evidence**. The genuine quarantine startup RED is retained in the evidence, along with its approved fix. A first supplementary probe passed its behavioral assertions but failed disposable directory cleanup because test-only native `sqlite3` context management does not immediately close the connection; rerunning with explicit garbage collection after runtime close completed. A second invocation initially omitted test PYTHONPATH and failed import before execution. Neither was a backend full-suite failure or a source change. Output is not claimed pristine: negative-contract stderr and Git line-ending warnings are documented.

## Earlier P2b2 probes and remaining boundary

Before the split, real P2b1 store/Hub probes established: crash before/after DELIVERY ACK leaves existing durable answer available through accepted paused history after actual runtime reopen; owner becomes PAUSED, no new claims/ACKs/Worker/provider, and trusted finalization refuses PAUSED. Controller selected explicit pending user recovery, no late bookkeeping. Successful live flow's existing Verifier finalization and actual Engine notification enumeration remain P2b2 work.

FAILED/CANCELLED finalization correctly revokes private body reads. Controller selected true terminal state via existing public TaskRuntime fixed status/closed failure type, without exposing private failure text or faking PAUSED. Actual bridge/snapshot/replay and Client unhydrated behavior still require P2b2 verification; this slice does not claim them.

P2b1's completion-derived publication authority remains unchanged. The new child catalog is not publication authority. No actual inject→factory→ProviderModelAdapter→ProviderGatewayClient→Worker→completion/outbox→Hub/Client chain was added or tested here. Actual Coordinator production and pre-run user/chat encryption remain Unit D; Worker-only protection would not protect all chat. No real provider or model calls/downloads occurred.

## Self-review and limitations

Initial self-review found no unresolved issue, but independent review subsequently found I1/I2 below; that review supersedes the initial assessment. The large pre-existing service received only lifecycle and approved recovery coordination changes, not a rewrite. Catalog corruption/key loss/path changes deliberately fail recovery and may require operator repair; there is no relocation fallback. Cross-process malicious filesystem replacement, external copied files/backups, OS/WAL forensic remnants and plaintext already handed to a caller are not revocable guarantees. SQLite logical deletion/secure-delete is not advertised as physical forensic erasure. Catalog capacity overflow is explicit, not silent truncation. Future composition must use the enrollment order before the first private child write.

## Review fix round 1

Fix base `97332fbe689289018e54e1a5fc292fd89d5e9f0d`; intervening controller documentation at `6e41feb` preserved. Full independent review: `reviews/REVIEW-protected-child-cleanup-p2b2a-97332fb.md` (0 Critical / 2 Important / 2 Minor).

- I1: confirmed real composition bug. When startup retried one legitimately revoked child, opening existing event authority globally revoked another Group merely because any deletion intent existed. Invalid-proof quarantine must never authorize deletion. The only production change is in `ProtectedEvents.retry_cleanup`: it consumes already authenticated durable revoked rows only, with no inferred revocation. Existing Runtime Group reconciliation remains the sole canonical-proof validation route before its authorized revocation/cleanup. No event publication, reader, crypto, Worker or terminal guard changes.
- Added three real two-Group restart variants: empty event catalog + interrupted durable revoke; pending real protected event + interrupted durable revoke; pending event + actual canonical prepared deletion. All three failed before the fix with the other Group unexpectedly `revoked`; all three pass after it. They assert retained encrypted SQL/envelopes/record claims and descriptor unchanged, owner remains RUNNING but access denied, legitimate selected child cleanup completes, event hydration denied and sequence head retained. Actual Hub publication is used for event variants; only failure injection at the post-durable-revoke cleanup boundary is patched.
- I2: replaced the username-bearing diagnostic prefix with `<TEMP>` while retaining WinError 32, synthetic suffix and honest failed teardown. Scanned both slice report/evidence for profile identifiers; no remaining matches. This changes current evidence only, not Git history; public-release/history sanitation remains separate.
- M2: reused native SQLite contexts in this test file now use `closing(connection)` plus the original connection transaction context. Commit/rollback semantics are preserved before deterministic close; no GC workaround is required for these helpers. New read-only snapshots also close explicitly. No broad hygiene sweep.
- M1 remains disclosed/nonblocking: expected negative-contract stderr and existing Git newline advisories. No waiver of correctness checks or new exclusions.

TDD and verification details are appended to the same evidence file. Source/tests froze after 3-case GREEN and self-review; associated child/event/Group deletion/quarantine matrix passed 179 tests in 62.01s. Node 24.9.0 mirrored contracts passed 25 tests / 3 files in 2.79s; compileall exited 0. The single frozen full Python 3.12 regression for this fix passed **1929 tests / 5 existing skips / 10 subtests in 220.76s**, with no new exclusions. Diff-check passed with disclosed newline advisories. Independent scoped rereview remains required; this does not mark Unit P complete.
