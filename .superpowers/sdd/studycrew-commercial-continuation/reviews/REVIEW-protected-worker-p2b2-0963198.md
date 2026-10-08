# Spec Compliance

- **❌ Issues found. Spec verdict: Needs fixes. 0 Critical / 1 Important / 1 Minor.** I1 leaves a canonically mutable owner RUNNING after archive cancels the actual Worker at its initial public phase; this misses the approved failure-finalization/capacity requirement. The other reviewed composition requirements are implemented within the stated scope.
- **Quality verdict: Needs fixes.** Do not close P2b2 / Unit P at this revision. This is a task-scoped review of `25693e0ee1dd9da3ab88d3bd71b90f267fa85a2c..0963198a0f50fbad5320ad99ec9f77f5f6ef6af8`, not a whole-product or commercial-readiness assessment.
- **⚠️ Cannot verify from this diff:** real Coordinator production/pre-run ingress encryption, governed review continuation and usable explicit user recovery remain Unit D; the actual-chain fixture selects planning rather than separately executing knowledge and coach (`backend/tests/test_protected_worker.py:91`, inherited `backend/tests/test_protected_children.py:43`). Abrupt OS kill/power loss, hostile concurrent filesystem replacement, native packaged release and production throughput remain unverified. The nine interruption cases use a `BaseException` plus real close/reopen, not a killed process (`backend/tests/test_protected_worker.py:238`, `:296`). Already delivered bytes and external/OS backups are not revocable by this implementation.

# Strengths

- `backend/protected_worker.py:24` validates exact live same-runtime access, canonical project/workspace and execution authority; `:44` enrolls the child before committing the envelope. `backend/engine.py:3923` selects this route before the legacy attachment/history path, and `backend/worker_gateway_runtime.py:277` preserves the real gag holder with an empty effect registry.
- `backend/protected_worker.py:83` persists the actual run through the completion-aware repository before model activity; `backend/runtime.py:1007` invokes this before STATE_CHANGED/MODEL_CALLED. The real transport assertion checks active attempts and the bound Worker UUID before send (`backend/tests/test_protected_worker.py:128`).
- `backend/knowe_harness/projections.py:224` consumes the real durable event commit result, ACKs local publication, skips optional derivative copies with content-free receipts and retains COORDINATOR unclaimed/unacknowledged. `backend/protected_worker.py:196` commits the encrypted completion/intents, drains the projector, then calls the existing owner finalizer. The retained notification body is not falsely marked reviewed.
- `backend/knowe_adapters/model_adapter.py:92`, `:117`, `:201` restrict native WAITING to one closed question-only control, validate UTF-8 byte length and duplicate JSON fields, and leave ordinary prose as a final answer. `backend/tests/test_protected_worker.py:397`, `:616`, `:631` test malformed, mixed/multiple and prose cases through the inert HTTP transport.
- `backend/worker_gateway_runtime.py:285` stops raw reasoning at its ingress. `backend/tests/test_protected_worker.py:122` asserts useful answer/wait output and scans populated files, decoded gzip/SQLite values, cache and logs with distinct markers. `backend/hub.py:164`, `:207`, `:227` preserve the independent Task Runtime status path after private-history hydration fails, with real bridge/replay assertions at `backend/tests/test_protected_worker.py:361`.
- `backend/tests/test_protected_worker.py:251` enumerates the exact permitted legacy constructor differences instead of masking arbitrary SQL changes; `:296` checks runtime dump equality, zero new Worker/provider/inbox work, retained encrypted pending review and full child SQL equality for pure history reads. `:412` exercises two protected lineages and a legacy attempt in one database.

# Issues

## Critical (Must Fix)

- None identified in this task scope.

## Important (Should Fix)

- **I1 — Startup-phase cancellation bypasses owner finalization and leaves global capacity occupied.** Primary locations: `backend/protected_worker.py:101`, `:122`, `:209`; directly reached caller: `backend/runtime.py:1011`. On the first protected attempt in a Hub with no existing protected event catalog, `phase()` awaits ordinary `Hub.emit`. That reaches awaited socket broadcast because protected authority is only attached later at `backend/protected_worker.py:200` (existing `backend/hub.py:722`, `:754`; real Server initialization binds only an already-existing catalog at `backend/server.py:470`). A slow authenticated client's send lock therefore suspends the first STATE_CHANGED after the durable started journal. If the Group is archived then, `_watch()` cancels the Worker. STATE_CHANGED is before WorkerRuntime's `try` at `backend/runtime.py:1013`, so `CancelledError` escapes its normal CANCELLED result handling. It also bypasses the new `except Exception` in `ProtectedWorker.run`; only cleanup runs, removing the envelope/selection while the canonical owner remains RUNNING with no failure type. The approved immutable failure finalizer is never called. This is an actual lifecycle/slot leak, not merely missing coverage; the reported slow-provider tests wait until transport entry and therefore start after this vulnerable phase (`backend/tests/test_protected_worker.py:500`).
  - **Observed real-fixture result:** `{"outcome":"CancelledError","status":"running","failure_type":"None","provider_sends":0,"selection_closed":true,"envelopes":0,"hub_protection_bound":false}`. The underlying owner remains an executing run even though its selected Worker can no longer finish it. The controller's existing global-capacity regression establishes why a RUNNING archived owner occupies the slot (`backend/tests/test_protected_worker.py:670`).
  - **Required repair:** make startup public phases independent of unbounded client delivery and cover cancellation across the complete selected attempt, including the first phase before the Runtime loop. Use the existing immutable failure-finalization mechanism for archive/revoke where its current guards permit; preserve explicit user cancellation as CANCELLED and preserve PAUSED/terminal/deletion/quarantine/runtime-close behavior. Do not catch every `BaseException` and turn the approved process-interruption policy into false finalization. Add a real slow-client startup regression before first transport send, assert the public truthful terminal status and slot release, plus the user-cancel/shutdown controls relevant to the fix. No private reader or guard relaxation is needed.

## Minor (Nice to Have)

- **M1 — Existing verification-output noise remains.** `.superpowers/sdd/studycrew-commercial-continuation/protected-worker-p2b2-mirrors-final-gate.txt:9` and `:32` contain negative-contract stderr; `protected-worker-p2b2-evidence.md:95` records newline advisories. This is the inherited nonblocking output-hygiene finding, not an additional behavioral regression. Keep it visible in release tracking and make expected diagnostics explicitly asserted/suppressed in their owning tests when that maintenance is undertaken; do not represent these runs as pristine.

# Inspected Risks and Checks

| Named risk | Exact inspection and result |
| --- | --- |
| Storage capability accidentally authorizes provider execution | Inspected changed `ProtectedWorker.authority` and agent/factory wiring, plus the directly called `ProviderGatewayClient._authorize` at `backend/commercial_gateway.py:2549`. It reobtains canonical RUNNING owner/group/task, real TaskRuntime type, capability/DLP and budget (`:2591`, `:2600`, `:2670`). The selected live guard rechecks current card/scope at `backend/protected_storage.py:272`. No provider bypass was added. |
| Holder identity / unsupported scope / child location | Changed admission/factory checks at `backend/protected_worker.py:24`, `:39`, `backend/worker_gateway_runtime.py:277`; selected live guard at `backend/protected_storage.py:275` rejects source/course and revalidates current card. The generic role validator is accepted inherited behavior, not newly claimed three-role dynamic coverage. Wrong task/runtime/path and missing/revoked/paused selection tests are at `backend/tests/test_protected_worker.py:443`. |
| Started journal or delivery identity occurs too late | Followed `on_run_started` at `backend/runtime.py:1007` into `backend/knowe_harness/completion.py:3276`, `:1288`: actual run and QUEUED/STARTED journal persist before model activity. The new codec-derived callback runs before final RuntimeResult persistence (`backend/runtime.py:1258`, `backend/protected_worker.py:89`). |
| Native control normalization silently accepts duplicate/mixed controls | Completed the diff-cut `_stream_step` function and its immediately called merge/finalize helpers in `backend/knowe_adapters/model_adapter.py:134`, `:241`, `:266`. Finalization propagates malformed-call errors; new strict JSON and closed-argument checks run before WAITING translation. Inspected the actual malformed/mixed/prose transport regressions, without rerunning them. |
| ACK/notification ordering or false review consumption | Inspected all new protected projector code at `backend/knowe_harness/projections.py:224` and finalizer sequence at `backend/protected_worker.py:193`. DELIVERY uses SQLite commit identity; COORDINATOR retains its only encrypted body and has no receipt/ACK. No notification JSON writer, SeenSpeech writer or `_submit_internal` occurs on that branch. Success/wait and real restart assertions retain the body (`backend/tests/test_protected_worker.py:143`, `:340`). |
| Failure bridge hidden by denied private refs | Inspected all Client queue/fence changes and actual bridge test at `backend/tests/test_protected_worker.py:361`. `task_runtime_event` survives the private-history fence without decrypting failed/cancelled completion content. No new failed/cancelled reader exists in the diff. |
| Recovery accidentally replays protected work | Completed the diff-cut Engine `start` recovery closure (`backend/engine.py:2078`) and read the explicit legacy selector change at `:2633`. The genuine reopen tests assert no selected workers/agents/envelopes/notifications/provider calls and unchanged claims/runtime SQL; no new startup authority is fabricated. |
| Failure finalizer can settle a different owner or bypass quarantine | Inspected changed immutable tuple and `fail_closed`, then the exact unchanged `_owned_run` / `finalize_trusted_execution` contracts (`backend/task_runtime/service.py:230`, `:494`). Existing group mutation and Verifier rules remain; canonical RUNNING and original task checks prevent the tested mutable-view redirect (`backend/tests/test_protected_worker.py:670`). I1 identifies the separate path that never reaches this finalizer. |
| Async cleanup/cancellation at new composition boundaries | Read complete new `ProtectedWorker`, relevant Runtime start/cancellation/cleanup boundaries, Hub phase publication and Server initial binding. Ran only the bounded startup slow-client probe below. Existing slow-provider/replay tests do not cover that startup await; I1 is reproduced. |

# Verification Evidence and Boundaries

- Read the current controller brief including startup-fence/immutable-finalizer additions, complete P2b/P2 real-chain requirements, implementer report/evidence, accepted P2a/P2b1/child API reports and child fix acceptance. Used the SDD reviewer template and Backend Python Expert async/testing references; did not invoke the excluded code-review skill or delegate further.
- Read the complete supplied 3,451-line review package in bounded pieces, including all eight production files, the new 759-line test module and evidence/documentation. Did not regenerate Git diff/log or crawl unrelated production code. Changed-file supplementary reads were limited to diff-cut functions required for the named caller/contract risks above; unchanged-code reads are listed in that table.
- The supplied final logs report **1983 passed / 5 existing skipped / 10 subtests** (`protected-worker-p2b2-backend-final-gate.txt:28`), **110 focused passed / 10 subtests** (`protected-worker-p2b2-focused-final-gate.txt:3`), **25 mirrored tests** and **33 foundation tests**. These are implementer runs reviewed for correspondence, not reviewer reruns. Compileall/diff-check are recorded as exit 0 in the evidence ledger. No suite was rerun.
- Independently computed SHA256 for all nine listed source/test files; all match `protected-worker-p2b2-freeze-final-gate.txt` and the post-gate file. No source/test/index/HEAD mutation occurred during this review. The sole retained workspace write is this UTF-8 review file via `apply_patch`.
- One startup cancellation experiment was executed, after an initial import-only invocation failed because standalone Python lacked pytest's backend import path. The corrected in-memory invocation adds `backend` to `sys.path`, reuses real canonical fixtures and actual Engine/Gateway/Worker/Hub/Client, and makes no provider request or external network connection. It reached the failing behavior below before teardown. No package/race/full test run, dependency action or cleanup of earlier scratch was performed.

## Focused I1 Reproduction

Run in the worktree with `py -3.12 -B -X utf8 -c $probe`, where `$probe` is this UTF-8 PowerShell single-quoted here-string body. Only the lowest HTTP transport is replaced by the existing inert fixture; the Client send-lock barrier is the actual lock used by production send.

```python
import asyncio, json, tempfile, sys
from pathlib import Path
sys.path.insert(0, str(Path('backend').resolve()))
from pytest import MonkeyPatch
from backend.tests.test_protected_children import world
from backend.tests.test_protected_worker import provider, Transport, compose
from backend.tests.test_protected_events import admitted

async def main():
    with tempfile.TemporaryDirectory(prefix='p2b2-startup-review-') as temp:
        root = Path(temp)
        patch = MonkeyPatch()
        wg = world.__wrapped__(root); w = next(wg)
        pg = provider.__wrapped__(patch, root); next(pg)
        transport = Transport()
        engine, store, access, envelope, owner, group = compose(w, transport)
        selected = engine._protected_workers[envelope.task_id]
        server, client = await admitted(w, engine.hub, patch)
        await client.release_live_replay_fence()
        await client._send_lock.acquire()
        worker = asyncio.create_task(engine._run_worker(envelope, envelope.worker_id))
        try:
            async with asyncio.timeout(3):
                while not selected._active_published:
                    await asyncio.sleep(0)
            assert selected.store.active_attempts()
            w[1].archive(group.group_id)
            await asyncio.sleep(.15)
            client._send_lock.release()
            outcome = await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 3)
            run = w[2].store.get_run(owner.run_id)
            print(json.dumps({'outcome': type(outcome[0]).__name__, 'status': run.status.value,
                'failure_type': str(run.failure_type), 'provider_sends': len(transport.requests),
                'selection_closed': selected.closed, 'envelopes': len(engine._task_envelopes),
                'hub_protection_bound': engine.hub.protected_events is not None}))
        finally:
            if client._send_lock.locked(): client._send_lock.release()
            if not worker.done(): worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            await engine.stop(); store.close()
            pg.close(); wg.close(); patch.undo()

asyncio.run(main())
```

Observed behavior, printed before temporary-directory teardown:

```json
{"outcome": "CancelledError", "status": "running", "failure_type": "None", "provider_sends": 0, "selection_closed": true, "envelopes": 0, "hub_protection_bound": false}
```

The process then exited 1 because automatic temporary-directory teardown encountered `PermissionError: [WinError 32]` on its synthetic runtime SQLite file. This does not negate the recorded behavioral result, and is not reported as a passing test. A read-only post-exit inventory found only `settings.json` (834 bytes) and `runtime/task-runtime-v1.sqlite3` (290,816 bytes) beneath `<TEMP>/p2b2-startup-review-a133cpr3`. The exact resolved new directory was validated against its expected full path/name and reparse attribute before an attempted native PowerShell recursive removal. **Automatic approval review rejected that cleanup before execution with `blocked by policy`, without a more specific reason.** No alternate cleanup or bypass was attempted; this newly created synthetic residue remains. Account-bearing path prefixes are deliberately absent from this report. The controller/user may remove that exact owned directory later; earlier scratch was untouched.

# Assessment

- **Task quality: Needs fixes.** Normal publication/retention/restart ordering is well grounded in actual production paths and meaningful storage tests. The startup cancellation gap in I1 still violates lifecycle completion and capacity release despite the passing frozen suite; fix it and run the required focused/frozen gate before requesting the next scoped review.
- **Remaining counts: 0 Critical / 1 Important / 1 Minor.** M1 is the existing nonblocking verification-noise carry-forward. No Unit P completion or whole-product/commercial claim follows from this review.
