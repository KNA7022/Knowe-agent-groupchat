# P2b1 fix round 2 scoped re-review

## Finding Verdicts

- **I3 — Queue overflow during a recovery handoff leaves an invisible permanent fence: ADDRESSED.** `backend/hub.py:202` now captures the pending snapshot identity removed by a new invalidation. When such an identity is invalidated, `:212` requests one fresh `resync_required`, and `:213` coalesces an already queued same-project notice. The notice remains behind the in-flight handoff in the existing ordered drain. Repeated invalidations cannot multiply notices after the identity has been removed. The client can therefore observe the newer recovery requirement after a stale snapshot succeeds, rather than seeing a successful snapshot followed by silent suppression.
- **I3 safety condition — No authorization-denial retry storm: ADDRESSED.** A current snapshot's own failed final handoff passes `failed_handoff=True` at `backend/hub.py:198`. This occurs only after the exact identity check at `:192`; an older handoff returning after a newer overflow exits through that check and cannot suppress the newer notice. The failed-handoff flag is local to this call, retains the fence, and suppresses automatic notices for repeated hydration denial without permanently disabling later invalidation notifications.
- **Original I1 and I2: remain ADDRESSED** as established in the prior scoped re-review. This fix does not remove snapshot admission/current-success fence release or the server's failed-replay early return. They were not reopened as fresh review targets.

## New Breakage in the Fix Diff

- None found. The production change is limited to distinguishing invalidation of an existing recovery identity from that identity's own failed handoff and coalescing the resulting notice (`backend/hub.py:198`, `:202`, `:212`). It adds no authority writes, plaintext cache, network await under a lifecycle lock, or alternate authorization path.
- The new regressions check both stale socket success and stale socket failure after overflow, observe the fresh notice before issuing the next request, verify bounded coalescing, recover every durable successor once in sequence, and verify resumed live delivery (`backend/tests/test_protected_events.py:813`). The separate repeated-scope-denial regression verifies three failed requests leave only the original notice and no queued automatic retry (`:873`).

## Checks and Evidence

- Applied the existing scoped re-review template and retained P2b1 brief. Reviewed the supplied fix package once for FIX_BASE `4247eea45a7ffca0840aea153e5537a38ef904af` → HEAD `49f9c89eb440f951f2f7e52fc6356d55be21729c`, including the appended fix report, raw evidence, complete production hunk and new tests. A later mechanical package pass located exact citation line numbers. No Git diff/log was regenerated and no untouched production files were reopened.
- The regression directly reproduces the earlier I3 barrier: snapshot waiting in socket send, subsequent queue overflow, then stale handoff completion. Instance capacity 1 reaches the same production overflow branch as capacity 4096 without a high-count loop. Both parameter variants require the new signal before the next actual `_on_frame(request_snapshot)` call (`backend/tests/test_protected_events.py:813`). This answers the specific previously open defect rather than assuming the client retries spontaneously.
- Raw RED evidence records **1 failed / 1 passed**, with the missing fresh-notice assertion failing before production changes (`protected-delivery-p2b1-evidence.txt:389`). The covering command names the six relevant test files and its final run records **117 passed** (`:406`); frozen full regression records **1887 passed / 5 pre-existing skipped / 10 subtests passed** (`:452`). Compileall and scoped diff-check report exit 0. The displayed pre/post hashes for `backend/hub.py` and `backend/tests/test_protected_events.py` match (`:408`, `:454`).
- No tests or probes were rerun: the fix diff and its actual new regressions answer the concrete I3 success/failure, ordering and retry-loop doubts. No new test exclusions, skips or xfails appear in this fix.
- Production, tests, index, HEAD, skills, controller files and prior scratch directories were left untouched. Only this UTF-8 report is a retained workspace write.

## Out-of-Scope Observations

- None newly identified. Prior M1 line-ending validation notices remain a separately carried, non-blocking Minor; no encoding/EOL rewrite was requested or performed.
- Actual Worker/provider/notification-intent composition remains P2b2. This delivery-only gate does not approve original P2b/Unit P, certify hardware power-loss behavior, or remove the previously documented broader composition/performance limits.

## Verdict

- **Fix round: All findings addressed, no new Critical/Important breakage.** P2b1 independent task gate is **0 Critical / 0 Important**; prior M1 remains non-blocking. The narrowed protected-delivery prerequisite may advance to P2b2 under its separate implementation and acceptance requirements.
