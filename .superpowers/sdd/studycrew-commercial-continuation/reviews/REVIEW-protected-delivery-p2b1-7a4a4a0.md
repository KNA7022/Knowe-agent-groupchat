# P2b1 independent task gate

## Spec Compliance

- ❌ Issues found: protected authority and final hydration are implemented, but the actual recovery protocol is incomplete. A resync fence prevents the client's prescribed snapshot recovery, and failed protected WS replay still publishes a successful cursor. See I1 and I2 below. Gate: **0 Critical / 2 Important / 1 Minor; do not advance to P2b2 yet**.
- Scope reviewed: BASE `041d033d4f648e4e662c34ff11b9247f82a8058f` → production HEAD `7a4a4a0d473048d924e8073d1ec0ce8a5d41665c`, using the supplied U10 review package. Approved encrypted catalog, HTTP hydration, legacy writer-barrier test correction, and nonzero-cursor authority merge were treated as authorized requirements.
- ⚠️ Cannot verify from this diff: actual Worker/provider → protected completion → event plus notification-intent commit → outbox ACK/finalization remains P2b2, explicitly excluded by `protected-delivery-p2b1-report.md:6` and `:59`. This review does not approve original P2b or Unit P or certify the global Worker/log/export inventory.
- ⚠️ Cannot verify from this diff: hardware power-loss durability, process-kill recovery, hostile concurrent path replacement, and production throughput. The report limits these claims at `protected-delivery-p2b1-report.md:60`; transaction fault and close/reopen evidence is meaningful but not equivalent to those guarantees.

## Strengths

- The authoritative event is committed independently of derived caches, with an explicit typed receipt, SQLite FULL synchronization, keyed identity/content binding, and a preserved high watermark (`backend/protected_events.py:27`, `:127`, `:250`, `:357`). Hub keeps commit and reference enqueue under its sequence gate and does not wait for socket ACK (`backend/hub.py:301`).
- Final projection decrypts only after the Client send lock and rechecks selected lineage. Actual runtime-token admission installs the callback; answer and reasoning markers are distinct in the tests (`backend/hub.py:138`, `backend/server.py:4635`, `backend/protected_events.py:323`, `backend/tests/test_protected_events.py:79`).
- Tests reach real temporary Group/TaskRuntime/completion stores, transaction failures, loopback authentication, actual HTTP projection, current-scope fencing, and PAUSED reopen; the approved nonzero-cursor repair addresses a real durable-tail omission (`backend/tests/test_protected_events.py:133`, `:173`, `:198`, `:434`, `:544`).

## Issues

### Critical

- None found within this task's reviewed boundary.

### Important

- **I1 — Resync permanently suppresses the recovery snapshot and later live events.** `backend/hub.py:161` drops every queued event whose project is in `_protected_resync_projects`; `:183` sets that fence after enqueue/send failure, and `:782` routes snapshots through the same queue. The only clearance is a successful `replay_complete` in `:706`. The actual client responds to `resync_required` with `request_snapshot` (`src/transport/socket.ts:695`), whose ordinary server path invokes `Hub.snapshot` (`backend/server.py:6344`). Established connections cannot use `replay_request` (`backend/server.py:6405`). Consequently the requested snapshot is itself silently dropped and subsequent events stay suppressed until the connection is rebuilt. A focused real Hub/Client probe reproduced only `['resync_required']` after a committed-enqueue fault, a snapshot, and a later legacy message, with the project still fenced and two durable events present. Fix the actual snapshot handoff so recovery can bypass the fence safely and release it only after successful current-scope delivery, preserving ordering and failure fencing. Add a regression through the existing request-snapshot recovery route.

- **I2 — Protected replay failure is acknowledged as a completed replay.** `backend/hub.py:138` returns `False` when final protected hydration fails, but the existing actual WS replay caller ignores the result at `backend/server.py:4776` and sends `replay_complete(last_seq=replay_cursor)` at `:4778` / `:4821`. The new `backend/hub.py:706` then clears the resync fence on that successful control frame. This is newly significant because an authenticated, open socket can now reject one event for scope/key/authority reasons while accepting later frames. The frontend advances its watermark on that completion (`src/transport/socket.ts:689`), making an omitted event appear replayed. A real `_do_replay`/Hub/Client probe after a current-scope change sent a later legacy message and `replay_complete(last_seq=3)` while sending zero protected messages and clearing the fence. Fail the replay explicitly when a required protected send fails: do not publish a success cursor beyond the unsent record or clear recovery state. Cover final hydration denial between replay collection and socket handoff, including a recoverable key/authority failure, with the actual replay caller rather than only `Client.send`.

### Minor

- **M1 — Validation output contains line-ending warnings.** `protected-delivery-p2b1-evidence.txt:121` and `:187` preserve repeated LF-to-CRLF Git warnings. These are honestly disclosed and do not invalidate the green pytest run; however the review rubric requires recording non-pristine validation output. Align the scoped validation command/environment with repository newline policy when convenient, without rewriting unrelated files or changing existing encodings.

## Focused checks and evidence

- Read the requirements, implementer report, and supplied complete review package. An initial combined tool response truncated the first package chunk; only that unavailable portion was recovered. No Git diff/log was regenerated and no production files, index, or HEAD were modified. A later mechanical package pass located exact new-file line numbers for citations, without re-reviewing source.
- Named risk: cross-thread Store reads versus authority connection cleanup and FULL configuration. Inspected unchanged `backend/knowe_storage/_sqlite.py` transaction/close serialization and connection initialization. Its RLock serializes individual SQLite transactions; no additional confirmed issue is asserted from this check.
- Named risk: new send-failure/fence semantics versus existing recovery callers. Inspected only `Client.release_live_replay_fence`, `Hub.replay_with_cursor`, server handshake/replay and ordinary snapshot/replay-request callers, and the frontend `resync_required`/cursor handlers. These checks produced I1 and I2. The unchanged fixture `world`/`seed` was inspected solely to construct the isolated reproduction. No broader repository crawl was performed.
- Did not rerun the full or focused suites. Reviewed recorded frozen full result at `protected-delivery-p2b1-evidence.txt:182`: **1875 passed, 5 pre-existing skipped, 10 subtests passed**; recorded focused result is **57 passed**. These establish the reported regression state, not coverage of I1/I2.
- Additional execution: one in-memory Python 3.12 probe, `PYTHONUTF8=1`, `PYTHONDONTWRITEBYTECODE=1`, invoked as a PowerShell literal here-string piped to `py -3.12 -B -`. It imported existing `world`, `setup_delivery`, `admitted`, and `drain` fixtures and used an isolated `TemporaryDirectory`. The producer is the task's approved completion fixture; GroupRepository, TaskRuntime, SQLite authority, Hub, Client, token-admission helper, and final hydration were real. No Worker/provider or external service was called.
- Probe procedure: release the Client's initial replay fence; inject `protected_enqueue_fault` after authority commit; drain resync; call real `Hub.snapshot`; emit/drain a later legacy message; flush Store. Then invoke actual `KnoweServer._do_replay` after a real Group rename. Only unrelated project resolution/UI metadata helpers were stubbed (`_canonical_project_id_from_request`, directory-validity check, conversation display state, activity metadata, and snapshot-publication bookkeeping); protected group admission, authority, replay collection, Client send and hydration were unchanged.
- Corrected probe exit **0**, exact output:

```text
RESYNC_PROBE {'frames': ['resync_required'], 'fenced': True, 'durable_events': 2}
REPLAY_PROBE {'frames': [('message', None, 3), ('replay_complete', 3, None)], 'protected_messages': 0, 'fenced': False}
```

- The first probe already reproduced I1, then failed due to the test harness omitting `server.store`; that is a harness error, not a product finding. Its generator was closed without explicitly closing Runtime, so automatic temporary-directory cleanup hit an open SQLite handle. The corrected probe explicitly closes Store, Runtime, and registered temporary SQLite connections and cleaned its independent directory successfully.
- Cleanup limitation: scratch directory `C:/Users/25977/AppData/Local/Temp/p2b1-review-ripyf6w7` may remain from the first probe. An exact-target PowerShell cleanup attempt was rejected by automatic approval/tool policy with **“blocked by policy”**; no more specific reason was supplied. No alternate deletion mechanism was attempted. The controller was informed.

## Assessment

- **Spec compliance: Needs fixes. Task quality: Needs fixes.** Authority persistence and privacy boundaries are carefully implemented, but the new recovery fence and send-result semantics are not integrated with the actual existing WS recovery protocol. I1 and I2 are independently reproduced delivery failures and block the required 0 Critical / 0 Important gate.
- Review methods used the Python backend skill's async-boundary and real-fixture guidance. Only this UTF-8 report is a retained workspace write; source, tests, skills, unrelated controller edits, index and HEAD were preserved.
