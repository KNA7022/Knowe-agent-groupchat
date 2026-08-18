# Task 3A implementation report

## Status

DONE

## Production boundary

- `KnoweServer` now owns a thread-safe Task Runtime notification ingress and captures its asyncio loop only after the real WebSocket `serve(...)` lifecycle has entered. Constructor-time and pre-listen commits remain durable and are reconciled by replay/snapshot instead of borrowing an incidental caller loop.
- Synchronous journal listeners only enqueue committed `RunEvent` notifications. A single loop-owned drain reads the authoritative journal, repairs same-run callback inversions through the notification sequence, and serially publishes to the canonical Group Hub.
- Live publication tracks per-run sequence high-water marks and event IDs. Duplicate/idempotent listener notifications do not create a second Hub frame. Cross-run FIFO retains the expiry-to-follow-on-scheduler causal order.
- Shutdown first fences acceptance under the ingress lock, advances the bridge generation, clears queued callbacks, removes the service listener, releases the loop reference, and cancels an in-flight drain before Hub/project shutdown. A racing stale callback cannot publish or mutate Hub state. A later lifecycle registers against the current service and takes a fresh durable baseline.
- Task Runtime store listener registration/removal is now thread-safe and idempotent. No async Hub API is called from Timer or scheduler threads.
- Existing canonical `group_id` resolution, legacy runtime project compatibility, Hub public-text filtering, and `RunEvent.to_public_dict()` redaction remain on the only outbound path.

## Acceptance tests

Added `backend/tests/test_task_runtime_live_event_bridge.py`:

- A real `KnoweServer` on a genuinely running asyncio loop with an actual open Hub `Client` and actual approval `threading.Timer` proves exactly one live `approval_resolved`, followed by ordered `routing` then `running` frames for the next queued run, without a read/snapshot/reconnect. It also proves all socket sends occur on the owning loop thread and the durable journal contains one expiry event.
- A 12-event foreign-thread checkpoint burst proves ordered per-run live delivery. Re-delivering the same committed notification twice proves exactly-once event-ID behavior.
- A delayed Hub boundary races shutdown against a foreign-thread callback. The in-flight frame is cancelled before Hub mutation, later stopped-state commits remain durable only, replay exposes the gap, a fresh lifecycle emits no historical duplicates, and a new post-restart event is live.
- Updated the older direct-transport live-event test to explicitly enter/exit the same bridge lifecycle; unit construction alone no longer captures pytest's incidental loop.

## Exact verification evidence

- Task 1–3 Group/Task Runtime regressions:
  - `py -3.12 -m pytest -q backend/tests/test_groups.py backend/tests/test_group_http.py backend/tests/test_task_runtime.py backend/tests/test_task_runtime_review_fixes.py backend/tests/test_task_runtime_rereview_round2.py backend/tests/test_task_runtime_rereview_round3.py backend/tests/test_task_runtime_rereview_round4.py backend/tests/test_task_runtime_rereview_round5.py backend/tests/test_task_runtime_live_event_bridge.py`
  - Result: `111 passed in 13.51s`.
- Legacy Engine regressions:
  - `py -3.12 -m pytest -q backend/tests/test_worker_completion_v1.py backend/tests/test_runtime_impl_v10184.py backend/tests/test_coordinator_no_reply.py`
  - Result: `26 passed, 10 subtests passed in 1.61s`.
- Frontend Task Runtime/Group regressions:
  - `npm exec vitest run -- src/contract/taskRuntime.test.ts src/store/taskRuntime.test.ts src/contract/group.test.ts src/store/groups.test.ts --pool=forks --poolOptions.forks.singleFork=true`
  - Result: `4 files / 26 tests passed`.
- `py -3.12 -m py_compile` passed for Server, Task Runtime store/service, and the new bridge test.
- Targeted ESLint and strict TypeScript compilation passed with no diagnostics.
- `git diff --check` passed; Git printed only the repository's existing Windows LF-to-CRLF notices.

## Scope boundary

Task 4 status-bar UI and all later gateway/RAG work were not started. The broad repository typecheck baseline was not changed by this remediation; prior reports already document its unrelated legacy failures.

## Independent review fix round 1/5

The first Task 3A review found two Important handoff/resource-policy gaps and one
Minor lifecycle-test gap. This round closes all three without entering Task 4.

### I1 — monotonic per-client replay/live handoff

- Every new `Hub.Client` now starts behind a bounded live-broadcast fence. Point-to-point
  replay frames bypass the fence; broadcasts remain ordered in its queue until
  `replay_complete` has been written. The fence drains to empty while still closed to new
  direct sends, then atomically switches the client to live delivery.
- `Hub.replay_with_cursor()` captures the replay slice and Hub high-water cursor under the
  same sequence lock used by `emit()`. `replay_complete.last_seq` is therefore the cursor
  represented by replay, never a later live event that happens to have incremented the
  Project while snapshot serialization awaits.
- The reconnect response now contains authoritative Task Runtime snapshots for every local
  Group (`task_runtime_groups`) while retaining the current-Group `task_runtime` field for
  compatibility. Events from background Groups can therefore converge without a cold-store
  HTTP fallback.
- The frontend installs every Task Runtime snapshot before processing buffered replay data,
  treats events already represented by that snapshot as duplicates, applies later events
  contiguously, and advances the Hub watermark with `max(local, replay_cursor)` rather than
  rolling it backward. Genuine epoch reset now reconstructs directly from replay/snapshot
  instead of requiring a second snapshot request.
- The configured WebSocket URL is now preserved when adding authentication, allowing the
  real transport regression server to exercise the production socket implementation. The
  existing `stop_worker` command was added to the outbound contract so the changed transport
  passes targeted strict compilation.

### I2 — bounded ownership and finite failure policy

- Removed the redundant forever-growing published-event ID set. Durable per-run sequence
  high-water marks are sufficient for listener duplicate suppression.
- Active cursors are an LRU capped at 2,048 runs. Terminal runs move to a separate 256-entry
  tombstone LRU, and confirmed/reconciled Group deletion removes active, terminal, queued,
  and recovery bookkeeping for that Group.
- Pre-activation notifications are not stored. The durable journal/snapshot is the only
  pre-listen authority.
- Live ingress is capped at 2,048 unique `(run_id, seq)` notifications and coalesces duplicate
  callbacks. Overflow creates one bounded opaque recovery marker per run, with at most 256
  markers, and logs that durable replay is required.
- Journal-read and Hub-broadcast failures retry at most three times with bounded exponential
  delay and go to the same bounded recovery diagnostics. Retries move behind other queued
  work, so one poison run cannot block an unaffected Group. Restart takes a fresh durable
  baseline and replay returns the missed event exactly once.

### Acceptance coverage added

- A blocked real `replay_complete` send races Task Runtime commits in two Groups plus an
  ordinary Hub message. Wire order proves all snapshots precede live deltas, both run cursors
  are contiguous, each racing event appears once, and the next live event applies normally.
- More than 4,000 pre-activation callbacks leave no pending memory. More than 4,000 active
  synthetic notifications cap at the ingress policy, and 300 committed checkpoints keep
  cursor ownership constant without an event-ID cache.
- Permanent journal and broadcast faults each stop after four total attempts while a healthy
  Group continues live. Terminal transition leaves active retention, actual confirmed Group
  deletion removes all related state, and restart/replay obtains missed durable data once.
- A test enters the actual `KnoweServer.run()`/`serve` context, proves the bridge owns that
  server loop, triggers shutdown, and proves listener removal plus loop-reference release.

### Fix-round exact evidence

- Focused Task 3A bridge suite: `6 passed in 5.31s`.
- Combined Group/Task Runtime suites: `114 passed in 15.42s`.
- Legacy Engine suites: `26 passed, 10 subtests passed in 1.66s`.
- Frontend transport wars plus Task Runtime/Group contract-store suites:
  `5 files / 41 tests passed`.
- Targeted Python compilation, ESLint, strict TypeScript compilation, and
  `git diff --check` passed. Git emitted only the repository's existing Windows
  LF-to-CRLF notices.

## Independent re-review fix round 2/5

The first re-review exposed three remaining production-boundary gaps: bounded
cursor eviction could replay already published RunEvents, terminal live-delivery
failure was not observable to a connected affected Group, and epoch reset erased
the just-replayed roster/activity state. This round closes all three.

### Stable exact-once publication beyond memory cursor capacity

- Every Task Runtime Hub envelope now uses the nested immutable `RunEvent.event_id`
  as its top-level idempotency identity. `Hub.emit_with_status()` distinguishes a
  newly committed envelope from an existing ring/durable envelope, so scanning a
  run journal after an LRU miss advances the bridge cursor without broadcasting
  the old event again.
- Hub's hot idempotency lookup is an explicit 4,096-entry per-project LRU. The ring
  and durable event log remain the authority on cache miss; event-id memory no
  longer grows forever.
- `studycrew_live_publication` stores a durable per-run handoff high-water. A
  successful reconnect snapshot records every represented run, and each successful
  live delivery advances it. Active/terminal in-memory LRUs are therefore only an
  acceleration layer: eviction, delayed callbacks and restart never reinterpret a
  known run as cursor zero. Confirmed Group deletion removes publication state.
- When a client watermark belongs to an older epoch (`since_seq` above the new Hub
  cursor), replay now starts at zero under the same cursor lock. The lower
  `replay_complete` can reconstruct actual new-epoch chat history without an extra
  snapshot request.

### Durable, affected-Group recovery handoff

- Exhaustion and queue overflow upsert one coalesced recovery intent per run in the
  bounded `studycrew_live_recovery` table. Startup reloads the latest 256 intents;
  deletion removes them.
- The loop-owned bridge sends `resync_required` directly to connected clients with
  canonical `project_id`, `group_id` and an opaque recovery token. It does not reuse
  the failed Task Runtime data-broadcast path, does not notify one client twice for
  the same token, and does not clear the intent merely because notification was
  attempted.
- The renderer honors an explicit `resync_required.project_id`, returns the recovery
  token in `request_snapshot`, and does not substitute the active Group. The server
  replies on a direct `task_runtime_result(snapshot)` control path, so Task Runtime
  can converge even while ordinary Hub broadcast remains faulty. Only a successful
  addressed snapshot handoff clears the durable marker.

### Complete epoch reconstruction

- During handshake the transport retains each authoritative `project_created`.
  When a lower epoch cursor is detected it clears old chat and all old Task Runtime
  projections, reapplies new project metadata/roster, applies the authoritative
  activity ledger, drains replayed chat, installs all Runtime snapshots, then drains
  Runtime deltas, advances the new watermark, and only then returns to `live`.
- The app now exposes an explicit Runtime epoch reset action; missing Groups/runs
  from the new authoritative snapshot set cannot survive from the prior server.
- FakeKnoweServer gained injectable replay roster, activity and Runtime snapshots so
  the real WebSocket and real Zustand stores exercise the same ordering as production.

### Round-2 acceptance evidence

- Focused Task 3A bridge suite: `9 passed in 17.62s`.
- Production-cap regressions create more than 2,048 retained runs, evict a real run,
  deliver only its next sequence, ignore a delayed old callback after terminal LRU
  eviction, prove one durable Hub envelope per nested event ID, and prove delete /
  recreate isolation.
- Failure regressions exhaust delivery, keep a healthy Group live, directly converge
  the affected background Group without reconnect or unrelated traffic, acknowledge
  only after snapshot handoff, and reload an unacknowledged token after restart.
- Combined Group/Task Runtime suites: `116 passed in 29.44s`.
- Legacy Engine suites: `26 passed, 10 subtests passed in 1.38s`.
- Frontend real transport plus Task Runtime/Group contract-store suites:
  `5 files / 43 tests passed`.
- Targeted Python compilation, ESLint, strict TypeScript compilation and
  `git diff --check` passed. The broad TypeScript baseline still reports the
  previously documented unrelated legacy failures. The legacy E2E file remains
  blocked in its pre-existing fixture because it creates projects without the now
  mandatory project directory (`25 passed, 16 fixture errors` when audited alongside
  Hub unit tests).

## Independent re-review fix round 3/5

The second re-review found that recovery was acknowledged at the socket-write
boundary, was keyed and capped per Run, and could leave a renderer permanently in
`resync`. This round replaces that provisional behavior with a renderer-acknowledged,
Group-coalesced durable protocol.

### End-to-end renderer acknowledgement

- A recovery notice now carries one Group token plus a monotonic generation. A
  Runtime-only snapshot response binds canonical project, Group, token, generation,
  and a deterministic snapshot cursor. The server retains the durable intent after
  sending the response.
- `task_runtime_recovery_ack` is accepted only on the authenticated connection that
  was issued the matching snapshot cursor. Forged project/Group/token/generation or
  cursor values cause zero durable mutation. If a new failure advances the generation
  after snapshot issue, the stale ACK cannot clear it and the server advertises the
  remaining generation.
- The renderer treats request suppression as connection-scoped outstanding work.
  Disconnect, reconnect and epoch reset allow the same durable token to retry. A
  recovery response passes a strict schema, matching-address checks, and authoritative
  store projection before the renderer clears its outstanding attempt, moves from
  `resync` to `live`, and sends the ACK. Malformed or rejected snapshots remain in
  `resync` and send no ACK.
- Duplicate notices in one connection produce one concurrent request. If another
  failed Run advances the Group while that request is outstanding, the same response
  carries the latest generation rather than opening a second parallel handshake.

### Lossless Group-coalesced recovery

- `studycrew_live_recovery` is now keyed by canonical `group_id`. It retains one token,
  monotonic generation, failure count, high-water metadata and a chained cause digest
  for every affected Group. The previous Run-scoped schema migrates transactionally
  and coalesces existing rows.
- The old global `DELETE ... OFFSET 256` retention was removed. Durable correctness
  work is bounded semantically by existing Groups and is never discarded. The live
  dispatcher reads fixed 256-Group pages, yields fairly, and keeps only a 256-entry hot
  cache; a restart test retains and notifies all 257 Groups, including the oldest.
- Two failed Runs in one Group retain one row/token and one outstanding client frame;
  the authoritative snapshot covers both Runs. Acknowledging one Group never clears
  another. Temporary recovery-table write failure still has a bounded live-control
  fallback while committed RunEvents remain the restart authority.

### Atomic deletion lifecycle

- `TaskRuntimeStore.delete_group()` deletes `studycrew_live_recovery` and
  `studycrew_live_publication` inside the same transaction as Runs, events, approvals,
  tools and checkpoints. Both tables are included in `group_content_counts()` and the
  completion residual assertion.
- A fault injected at the final Run deletion proves the entire transaction, including
  both bridge tables, rolls back and the deletion intent is not marked complete. The
  successful retry leaves every content count at zero before recording completion.

### Round-3 acceptance evidence

- Focused recovery protocol, real WebSocket/store, migration, paging and deletion
  suite: `5 passed in 1.48s`.
- Bridge plus recovery suites: `14 passed` (the final combined run below includes both).
- Task 1–3 Group/Task Runtime regressions: `122 passed in 28.10s`.
- Legacy Engine regressions: `26 passed, 10 subtests passed in 1.68s`.
- Frontend real transport/store and Task Runtime/Group suites:
  `5 files / 44 tests passed`.
- Targeted Python compilation, ESLint, strict TypeScript compilation and
  `git diff --check` all passed; Git emitted only the repository's existing Windows
  LF-to-CRLF notices.

## Independent re-review fix round 4/5

The third re-review exposed a generation/snapshot TOCTOU, first-ACK global
retirement, unbounded per-client recovery fan-out, and a renderer status that could
hide a second recovering Group. This round closes those production boundaries.

### Stable generation-bound snapshot capture

- The durable Group recovery marker now retains bounded latest-cause coverage:
  affected Run ID, sequence, version, stable token, monotonic generation, failure
  count and chained cause digest. Metadata remains constant-size regardless of the
  number of failed Runs in a Group.
- Recovery capture reads the marker before materializing the Group snapshot, verifies
  the same token/generation/digest afterwards, proves the snapshot covers the marker's
  Run sequence/version, then performs a final marker comparison while holding the
  same bridge ingress lock used by production failure recording. The issued cursor
  hashes both the complete snapshot Run cursors and exact marker identity/coverage.
- A generation advance at any boundary rebuilds the snapshot. A bounded retry
  exhaustion leaves the durable generation outstanding. The regression injects a
  checkpoint and recovery generation during the first `group_snapshot()` call and
  proves the response contains generation 2 with the Run's authoritative sequence 2;
  only that exact cursor can clear generation 2.

### Multi-client ACK obligations and durable disconnect retry

- A recovery notice reserves its per-client obligation before the socket send await,
  so an ACK racing another client's in-flight notice cannot retire the Group intent.
  Global retirement occurs only after the ACKing connection applied its issued cursor
  and no other live connection retains the same notified/issued generation.
- A socket that closes with an unacknowledged obligation records one durable
  fresh-connection retry fence on the Group row. An older still-connected window
  cannot consume it; a connection established after the loss must apply and ACK the
  authoritative snapshot. Forged, stale or cross-Group ACKs still mutate nothing.
- Both direct Client tests and two genuinely connected WebSocket clients prove A's ACK
  leaves B valid, B can request/apply/ACK without reconnect, and only the final valid
  obligation retires. A disconnect/B-ACK/replacement-connection sequence proves the
  durable retry fence survives the lost window.

### Bounded fair recovery paging and global renderer status

- Each Client owns a four-Group recovery window, one opaque SQLite paging cursor and
  fixed revision metadata. Notification fills only free slots and never constructs a
  full-table Group set. ACK completion advances the next page; all correctness rows
  remain losslessly in SQLite. The existing bridge hot cache remains capped at 256 and
  temporary recovery-table write failures can use only free window slots.
- A 513-Group regression proves the initial send and both independent clients' maps
  stop at four, every subsequent completion advances fairly, all 513 Groups including
  the oldest arrive exactly once, and per-client memory never exceeds the configured
  window. Restart reconstructs delivery from the still-durable rows rather than hot
  client state.
- The renderer now returns the global connection to `live` only when its complete
  per-project recovery-attempt map is empty. A real socket/store test starts two
  background Group recoveries, proves the first applied+ACKed snapshot remains
  `resync`, and proves only the second completion returns to `live`.

### Round-4 acceptance evidence

- Task 1–3 Group/Task Runtime regressions, including recovery and real bridge suites:
  `125 passed in 28.73s`.
- Focused bridge/recovery suites after final bounded-marker refinement:
  `17 passed in 17.81s` (`8` recovery protocol tests plus `9` bridge tests).
- Legacy Engine regressions: `26 passed, 10 subtests passed in 1.33s`.
- Frontend real transport/store plus Task Runtime/Group suites:
  `5 files / 45 tests passed in 12.66s`.
- Targeted Python compilation, changed-file ESLint, strict compilation of
  `src/transport/socket.ts`, and `git diff --check` passed. The broad repository
  `npm run typecheck` continues to report its documented unrelated legacy fixture,
  component and store errors; it reported no new error in `src/transport/socket.ts`.

## Independent re-review fix round 5/5 — final

The fourth re-review found two remaining interleavings: an ACK for a newer Group
generation ignored another window's older notice, and overlapping fill coroutines
could exceed the four-slot cap while a single latest-Group hint lost earlier late
inserts. This final round closes both without changing the public transport envelope.

### Cross-generation multi-client obligations

- Recovery ownership is now compared by canonical Group plus its stable token across
  every generation. A valid old notice remains requestable while the durable row
  advances; snapshot capture upgrades that connection to the current generation.
- A successfully applied snapshot becomes a bounded connection-local applied handoff
  while another window still owns an unresolved notice or issued cursor. Same-current
  applied handoffs do not block final retirement; older applied handoffs do block and
  are upgraded/resend when a newer generation appears. Final retirement removes the
  settled handoff from every live connection.
- Disconnect retention resolves an old notice/cursor against the row's current
  generation and records durable fresh-connection retry debt. A current applied
  handoff does not create false debt, while an applied older generation does.
- ACK completion refills every live connection, not only the sender, so a generation
  that advances during another client's snapshot immediately upgrades any older
  applied handoff. Cursor/token/project/Group validation remains connection-bound;
  stale or forged ACKs cannot mutate the durable row.
- Real A/B WebSockets now start with generation 1, advance to generation 2 before A's
  request, retain the row after A applies/ACKs generation 2, allow B's original
  generation-1 notice to receive generation 2, and retire only after B's ACK. A
  three-client regression injects generation 3 before B's generation-2 ACK and proves
  all older obligations upgrade before retirement. Cross-generation disconnect and
  replacement-connection retry are also covered.

### Serialized bounded pager and lossless revision rescan

- Every Client owns an explicit asyncio fill lock. All slot reservations repeat the
  capacity check under the bridge ingress lock before an await, and failed sends roll
  back their reservation. Ten arbitrary concurrent fill calls therefore retain and
  emit at most four unresolved frames.
- The lossy single latest-Group hint was removed. The production recovery recorder
  advances a monotonic process revision under the bridge lock. A client observing any
  unseen revision resets only its opaque SQLite cursor/exhausted flag; ACKed rows are
  absent from SQLite, while its current unresolved/applied handoffs are the only
  bounded exclusion. Multiple changes between fills are rediscovered by ordinary
  fixed-size pages.
- Different clients fill concurrently, each behind its own lock. A slow/unresponsive
  window keeps its four obligations without blocking a fast window from applying all
  later Groups. Applied rows retained solely for the slow cohort do not consume the
  fast client's unresolved window.
- Regressions cover 513 durable Groups, 100 production-recorded inserts after cursor
  exhaustion, restart in the middle of a page, two independent clients, a slow cohort,
  and ten simultaneous notifier tasks. Every durable Group is eventually observed,
  the oldest survives, and unresolved notified/snapshot state never exceeds four.

### Final-round acceptance evidence

- Focused live bridge and recovery protocol suites: `21 passed in 18.91s`.
- Task 1–3 Group/Task Runtime regressions: `129 passed in 29.63s`.
- Legacy Engine regressions: `26 passed, 10 subtests passed in 1.41s`.
- Frontend real transport/store and Task Runtime/Group suites:
  `5 files / 45 tests passed in 12.86s`.
- Targeted Python compilation, changed-file ESLint, strict compilation of
  `src/transport/socket.ts`, and `git diff --check` passed. No frontend envelope or
  schema changed in this round. The previously documented broad TypeScript baseline
  remains outside Task 3A and has no new error from this final fix.

### Final audit

Exact-once event identity/publication, stable marker/snapshot capture, durable Group
recovery, atomic Group deletion, replay/epoch reconstruction, real Timer expiry order,
shutdown loop fencing, canonical Group isolation and public redaction retain their
combined passing coverage. No residual Task 3A correctness issue is known. Task 4 and
later status-bar/RAG work remain untouched.
