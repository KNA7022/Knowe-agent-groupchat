# Task 3 implementation report

## Status

DONE

## Architecture and scope

- Added a new versioned StudyCrew control plane under `backend/task_runtime/` instead of changing the semantics of the historical `backend.runtime.TaskRun` worker-loop record.
- The backend remains the sole authority. SQLite commits each run snapshot and append-only event in one transaction with per-run monotonic `seq` and `version`, stable opaque event IDs, and durable request/idempotency keys.
- Public event payloads and sensitive payloads are stored in separate columns. Public serialization never contains the sensitive column and rejects chain-of-thought, raw reasoning, system prompt, credential, secret, and API-key field names recursively.
- Added exact plan states for TaskRun and AgentRun, explicit transition tables, Verifier-gated terminal success, pause/cancel/resume, approval waits, checkpoints, evidence references, multidimensional budgets, and the plan failure taxonomy.
- Added a deterministic fair scheduler with a persisted group cursor: one executing primary per group, two low-risk/read-only side runs per group, global two primary and two foreground side runs, plus an independent one-job index queue considered after foreground work.
- Restart recovery pauses interrupted routing/running/verifying runs. It inventories incomplete tools but never executes them. High-risk tool records are always non-replayable and require a resolved approval tied to the same group and run.
- Added constrained retry/correction/provider-fallback decision hooks that require same privacy and explicit preauthorization and never expand authority.
- Group archive/delete integration suspends work first. Archived groups retain readable snapshots but cannot submit/resume. Committed group deletion cascades Task Runtime runs, events, approvals, grants, tools, and checkpoints.
- Added HTTP actions under `/task-runtime/*`, a compatible WebSocket `task_runtime` command/result boundary, and authoritative Task Runtime snapshots in the existing `replay_complete` handshake. Both `group_id` and legacy `project_id` are accepted only at the Server transport seam and cross-checked; internal service calls require canonical `group_id`.
- Extended `GroupRuntimeSummary` with primary, queue, side, approval, indexing, and aggregate budget status.
- Added frontend Zod contracts and a Zustand projection that accepts only monotonic contiguous events, refuses cross-group state, marks gaps for resync, and never rolls a newer local projection back with a stale reconnect snapshot.

## Public contracts delivered

- `TaskRun`
- `AgentRun`
- `RunEvent`
- `ApprovalRequest`
- `PermissionGrant`
- `ToolInvocation`
- `Checkpoint`
- `BudgetUsage` / `BudgetLimits`
- `EvidenceRef`
- `GroupRuntimeSummary` runtime fields

All contracts carry schema version 1. Task states and Agent phases match the approved plan verbatim.

## Test evidence

### Backend focused and Group regressions

Command:

`$env:PYTHONPATH='backend'; py -3.12 -m pytest backend/tests/test_task_runtime.py backend/tests/test_groups.py backend/tests/test_group_http.py -q`

Result: `40 passed in 7.33s`

Coverage includes:

- invalid Task and Agent transitions;
- Verifier completion gate;
- durable seq/version, replay, reconnect gap snapshot, and stable idempotent event IDs;
- atomic rollback of snapshot/event/side-record commits under injected failure;
- public/private event separation and chain-of-thought rejection;
- deterministic multidimensional budget exhaustion;
- global/per-group fairness and limits;
- low-risk read-only side isolation and separate lower-priority index capacity;
- idempotent pause/cancel/resume/checkpoint/tool operations;
- approval gating and task-scoped high-risk tool authorization;
- restart pause and high-risk no-replay;
- forged group and cross-group parent rejection;
- archived snapshot/read behavior and resume blocking;
- deletion propagation through all Task Runtime content tables;
- failure recovery decision hooks;
- Server legacy `project_id` compatibility and identity-conflict rejection;
- existing Group domain and HTTP regression suites.

### Frontend focused and Group regressions

Command:

`npx vitest run src/contract/taskRuntime.test.ts src/store/taskRuntime.test.ts src/contract/group.test.ts src/store/groups.test.ts`

Result: `4 files passed, 20 tests passed`

### Targeted TypeScript compilation

Command:

`npx tsc --noEmit --target ES2020 --module ESNext --moduleResolution bundler --strict --skipLibCheck --esModuleInterop --jsx react-jsx src/contract/taskRuntime.ts src/contract/envelope.ts src/store/taskRuntime.ts`

Result: pass, no diagnostics.

### Targeted lint

Command:

`npx eslint src/contract/taskRuntime.ts src/contract/taskRuntime.test.ts src/store/taskRuntime.ts src/store/taskRuntime.test.ts src/contract/group.ts src/contract/envelope.ts src/store/groups.test.ts --ext .ts --max-warnings 0`

Result: pass, no diagnostics.

### Python compile and diff hygiene

Commands:

- `py -3.12 -m py_compile backend/task_runtime/contracts.py backend/task_runtime/store.py backend/task_runtime/service.py backend/task_runtime/__init__.py backend/server.py backend/groups.py`
- `git diff --check`

Result: pass. Git only reports the repository's existing Windows line-ending conversion warnings.

## Known pre-existing broad baseline failures

- `npm run typecheck -- --pretty false` still fails in the broad repository baseline with the previously documented unrelated test-fixture, legacy Store, SocketAPI, PDF worker, and other errors. None of the Task 3 files appeared in that diagnostic list; the targeted strict compile above is green.
- Adding the new envelope cases was also run against `src/contract/parity.test.ts`. That suite still has the two pre-existing whitelist-fixture mismatches (its fixtures already omit several current no-seq and structural events). The Task Runtime contract cases themselves compile and validate; this task did not rewrite unrelated historical fixtures.

## Remaining concerns / later-task boundaries

- Task 3 records governed ToolInvocation/PermissionGrant extension points and enforces approval/no-replay invariants. The actual unified Tool Gateway, DLP, MCP transports, shell sandbox, OS secret storage, and provider failover implementation remain Task 5 by plan.
- The scheduler publishes work as `running` when capacity is granted; the later Harness integration can attach actual Agent executors without changing the public state machine.
- Task 4 still needs to bind the new store to the status bar and event drawer. No Task 4 UI was implemented here.
- Task 8 will add deeper checkpoint artifact restoration and fault injection. Task 3 recovery deliberately fences work and exposes explicit resume rather than replaying execution automatically.

## Independent review fix round 1/5

The first independent review reported one Critical, ten Important, and two Minor findings. This round closes every reported item without expanding into the Task 4 status-bar UI or the later Tool Gateway/RAG implementation.

### Deletion authority and crash recovery (C1, I10)

- Group deletion now records a durable, token-validated Group cleanup intent before any Task Runtime mutation. Invalid or stale preview tokens leave the Group repository and every runtime table byte-for-byte/logically unchanged.
- Deletion advances through resumable Group and Task Runtime stages. Startup reconciliation joins both intent journals, so a crash cannot strand a half-deleted group even if it occurs after either subsystem commits.
- The delete response is not successful until runs, private/public events, approvals, grants, tool invocations, checkpoints, verifier decisions, and runtime side records have been removed. A deleted group and its former run snapshots are no longer readable.
- Fault injection covers all six durable boundaries: after Group prepare, Task Runtime intent, runtime cleanup, legacy runtime cleanup, Group commit, and Task Runtime completion.

### Live authority path, concurrency, and scheduling (I1, I2, I3)

- Normal group chat enters authoritative route classification before legacy execution. Direct messages retain explicit legacy DM semantics and do not silently create group TaskRuns. Ambiguous, write-capable, or sensitive outbound work stops in `waiting_approval`.
- HTTP and WebSocket mutations commit the same versioned `RunEvent` journal and publish the same public `task_runtime_event` envelope into the existing Hub ring. Replay and reconnect snapshots therefore use the same authoritative stream.
- Run precondition reads, derived state, budget accounting, version/sequence increments, public/private event writes, and approval/tool/checkpoint side records now commit in one SQLite transaction with durable CAS checks. Concurrent services additionally compete for a durable scheduler lease rather than trusting a process-local lock.
- Primary, side, and index queues persist independent fairness cursors. Repeated turns rotate eligible groups; indexing never advances foreground fairness. Recovery leaves interrupted execution paused for explicit resume while immediately scheduling unrelated eligible queued work.

### Verifier, permissions, tools, and privacy (I4-I8)

- Successful or partial completion requires a persisted decision made by the mandatory Group Verifier Agent through its verifying AgentRun. The decision is bound to group, task, run version, requested outcome, and evidence. Transport input cannot self-attest approval, and any intervening task version invalidates the old decision.
- Approval requests persist the exact capability, tool, normalized operation scope, purpose, and expiry. Resolution can only narrow those fields. Grants bind group, task, run, operation, purpose, and expiry, preventing a message-route approval from laundering authority into a high-risk tool call.
- Tool snapshot, budget reservation, invocation, and public event are atomic. Dispositions distinguish `created`, `requires_new_approval`, and `recovered_blocked`; recovered high-risk prepared work is fenced and needs a new purpose-specific approval.
- Public payloads use event-specific field allowlists plus recursive secret and PII scrubbing, including `token`, `access_token`, `refresh_token`, `password`, `authorization`, and `cookie`. Sensitive message titles/summaries are generic; original data remains only in the sensitive journal column.
- HTTP JSON decoding rejects non-standard `NaN`/`Infinity`, and every floating limit/usage/confidence value is checked with `math.isfinite` before persistence or arithmetic.

### Frontend projection and versioned contracts (I9, M1, M2)

- The frontend strictly parses the complete snapshot and live event envelope, checks envelope/group/run identity, rejects stale equal-version/lower-sequence snapshots, requires contiguous `seq + version`, and requests a resync for gaps or unsupported projections.
- Snapshot ingestion is authoritative replacement, so absent approvals/runs are removed. Every supported event either updates its public projection (including budget and approval resolution) or deliberately forces resync; no chat-text inference is used.
- Shared test builders moved from a test module to `src/contract/taskRuntime.fixture.ts`.
- Backend and frontend versioned contracts reject unsupported schema versions, malformed opaque IDs, forged group identities, nested credentials, and PII-bearing public payloads during construction and hydration.

### Review-specific regression and fault probes

The dedicated `backend/tests/test_task_runtime_review_fixes.py` probes:

- recursive credential rejection for all six required credential names;
- unsupported schema and invalid hydrated identity rejection;
- deterministic-barrier concurrent budget accumulation without a lost update;
- deterministic-barrier concurrent approval requests leaving exactly one valid pending row;
- cross-instance durable scheduler leasing without global overbooking;
- repeated-turn fairness, independent index cursor, and restart dispatch behavior;
- evidence/version-bound Verifier decisions and version-drift rejection;
- crash recovery after each of six deletion transaction boundaries;
- invalid delete-token zero-mutation behavior;
- normal group chat plus HTTP publication of replayable public Hub events, and explicit DM isolation;
- non-finite HTTP JSON rejection and transport-level Verifier-forgery rejection.

Frontend probes cover forged envelopes/snapshots, unsupported versions and IDs, nested secret/PII rejection, stale snapshots, equal-version/lower-sequence rejection, version jumps, budget projection, approval resolution/removal, and gap-driven resynchronization.

### Final focused evidence after all review fixes

- `py -3.12 -m pytest backend/tests/test_task_runtime.py backend/tests/test_task_runtime_review_fixes.py -q` -> `37 passed in 4.22s` during the review loop.
- `py -3.12 -m pytest backend/tests/test_groups.py backend/tests/test_group_http.py backend/tests/test_task_runtime.py backend/tests/test_task_runtime_review_fixes.py -q` -> `61 passed in 7.14s` in the final pre-commit run.
- `npm test -- --run src/contract/taskRuntime.test.ts src/store/taskRuntime.test.ts src/contract/group.test.ts src/store/groupStore.test.ts` -> `3 files passed, 16 tests passed`; `src/store/groupStore.test.ts` does not exist and Vitest simply selected the three matching files.
- `npx eslint src/contract/taskRuntime.ts src/contract/taskRuntime.test.ts src/contract/taskRuntime.fixture.ts src/store/taskRuntime.ts src/store/taskRuntime.test.ts src/contract/envelope.ts src/app/App.tsx src/transport/socket.ts --max-warnings 0` -> pass with no diagnostics.
- `py -3.12 -m py_compile backend/contract.py backend/groups.py backend/server.py backend/task_runtime/contracts.py backend/task_runtime/store.py backend/task_runtime/service.py` -> pass.
- `git diff --check` -> no whitespace errors; Git prints only the repository's Windows LF-to-CRLF conversion notices.

### Broad baseline checks (pre-existing failures, not attributed to Task 3)

- `py -3.12 -m pytest backend/tests -q` still stops during collection because four legacy suites import missing `ACTION_CONTRACT` from `backend.engine`: `test_v10182_runtime_ui.py`, `test_v22_dispatch_reliability.py`, `test_v27_weld.py`, and `test_v28_weld.py`.
- Broad `npm test` completed with `19 files / 333 tests passed` and `17 files / 105 tests failed`. The failures are existing UI/legacy contract drift and socket-wars environment failures; the Task Runtime focused suites above are green.
- Broad `npm run typecheck` retains existing unrelated diagnostics. Filtering the changed files showed only already-present `App.tsx` `calibrateActivity`, `socket.ts` unused `url`, and the historical `stop_worker` union mismatch; no new Task Runtime contract/store type diagnostic was introduced.

### Remaining bounded integration concerns

- The authoritative chat route is now ahead of legacy execution and its events are broadcast/replayable, but the historical worker engine is not yet converted into a step-by-step Task Runtime executor. That later Harness integration must consume the existing state-machine interfaces rather than bypass them.
- The App consumes public events and reconnect snapshots and resynchronizes gaps. The visual Agent status bar/event drawer remains intentionally deferred to Task 4.
- The current SQLite transaction/CAS and scheduler lease are correct for the single-desktop deployment. A future multi-process or remote deployment would need a database/lease design appropriate to that topology; multi-tenant deployment is explicitly outside the approved product scope.

## Independent re-review fix round 2/5

The first re-review found one remaining Critical and seven Important issues. This round closes all eight while preserving the previously accepted deletion-token, Verifier, approval-scope, finite-number, versioned-contract, and live transport behavior.

### Durable deletion mutation fence (C1)

- A `studycrew_group_deletions` row is now a permanent SQLite mutation fence. `create_run()` and every run mutation inspect it inside their write transaction, closing the cleanup-to-Group-commit race even across independent service instances.
- Service entry points additionally reject a prepared GroupRepository delete transaction before the Task Runtime intent has been copied. Submit, route, approval, grant revocation, tool, checkpoint, Verifier, resume/control, scheduler dispatch, and Agent updates cannot mutate a fenced Group.
- Scheduler candidate selection excludes either Group or runtime deletion intent, while the transactional fence still wins if deletion begins after selection.
- Cleanup and both service/server startup reconciliation re-sweep every deletion intent, including a previously marked-complete intent. Residue written by an older binary or pre-fence race is removed rather than bricking startup; canonical deleted content remains unreadable.

### Stable idempotency winners (I2, I6)

- Approval requests, approval resolution/grants, checkpoints, Verifier decisions, and both authorized/blocked tool preparations reload and return the committed side record after the atomic event transaction.
- A loser that observes the winning `(run_id, idempotency_key)` event can no longer return its locally generated approval, grant, checkpoint, decision, or invocation identity.
- Deterministic two-service barriers force both callers past the initial empty lookup. All callers receive the same durable ID/payload; tool budget/audit is recorded exactly once.

### Crash-held scheduler lease recovery (I3)

- A failed startup lease acquisition reads the durable expiry and arms a daemon retry timer for just after that boundary. If another process renews the lease, `schedule()` arms the next bounded retry.
- The retry runs under the service lifecycle lock. `close()` cancels the timer, fences callbacks, conditionally releases its own lease, and closes the store without a watcher/use-after-close race.
- A short-expiry crash-owner probe verifies that an eligible queued run starts without any external message or mutation.

### Centralized public-value DLP (I7)

- Backend and frontend public contracts now classify credential values (Authorization/Bearer, API keys, tokens, passwords, cookies) plus contact/identity PII, student identifiers, numeric grades/ranks/GPA, detectable name/course-schedule associations, and identifiable private-note markers.
- Public TaskRun titles/summaries, Agent status, approval summary, checkpoint label, tool summary, nested event values, and migration output use the same classification boundary. Normal message routing never copies user text into public status; raw content remains only in the sensitive event column.
- TaskRun and route/run events expose only a fixed-label `data_classification`/`classification` list. Arbitrary classification strings cannot be used to smuggle content.
- Exact probes `张三成绩95分，排名第2` and `Authorization: Bearer sk-secret` are rejected by public contracts and absent from title, snapshot, replayable events, and public Hub payloads.

### Replay identity and cursor binding (I9)

- `RuntimeReplaySchema` binds its snapshot run and every event to the outer run/group, binds snapshot cursors to both its nested run and replay tail, requires a snapshot only for a gap, and validates contiguous non-gap event sequence/tail.
- The store repeats identity and local-cursor checks before any mutation. Forged gap snapshots and non-gap events reject with the requested projection unchanged and no cross-run map entry.

### Forward migration (N1)

- Runtime schema initialization now creates base tables first, then performs one transactional/idempotent v1 migration before creating the pending-approval and idempotency indexes.
- Existing approvals, grants, tools, TaskRuns, Agent public statuses, and public events receive safe purpose/scope/disposition/classification defaults and public-value scrubbing before hydration.
- Multiple legacy pending approvals are ordered deterministically by `(run_id, created_at, approval_id)`. The first remains pending; each loser becomes rejected and receives an opaque tombstone containing loser ID, winner ID, run ID, resolution, and migration time—never the private request body.
- A crash rolls the migration back; a crash after migration but before index creation is safe to rerun. Seeded prior-v1 rows open successfully on two consecutive restarts.

### Trusted production completion bridge (N2)

- `ProjectEngine.submit_tracked()` attaches a trusted Future to the actual queued Engine turn. The Engine loop resolves it only after the Coordinator/Agent turn exits and classifies success, cancellation, execution exception, or executor-precondition failure.
- Server bridge tasks translate that trusted outcome into `begin_verification`, the mandatory Group Verifier AgentRun, an evidence/version-bound decision, and terminal completion. No client-controlled Verifier approval is introduced.
- Successful and partial results bind an opaque Engine completion evidence reference. Failed execution is verified as failed without fabricating positive evidence. Directory, workspace, attachment, submission, and Engine exceptions no longer strand running capacity.
- Stable executor-derived keys make duplicate lifecycle notifications no-ops. Terminal completion calls the scheduler, allowing the next queued primary to start.

### Round-2 regression evidence

- `py -3.12 -m pytest backend/tests/test_task_runtime_rereview_round2.py backend/tests/test_task_runtime.py backend/tests/test_task_runtime_review_fixes.py -q` -> `47 passed in 6.63s`.
- `py -3.12 -m pytest backend/tests/test_groups.py backend/tests/test_group_http.py backend/tests/test_task_runtime.py backend/tests/test_task_runtime_review_fixes.py backend/tests/test_task_runtime_rereview_round2.py -q` -> `71 passed in 9.84s`.
- `npm test -- --run src/contract/taskRuntime.test.ts src/store/taskRuntime.test.ts src/contract/group.test.ts` -> `3 files passed, 19 tests passed`.
- `npx tsc --noEmit --target ES2020 --module ESNext --moduleResolution bundler --strict --skipLibCheck --esModuleInterop --jsx react-jsx src/contract/taskRuntime.ts src/contract/envelope.ts src/store/taskRuntime.ts` -> pass.
- Focused ESLint over the Task Runtime contract/store, App/transport integration, fixtures, and tests -> pass with no diagnostics.
- Python compilation of Engine, Server, Task Runtime contracts/store/service, and round-2 probes -> pass.
- `git diff --check` -> no whitespace errors; only the repository's Windows LF-to-CRLF notices.

The round-2 backend probes include deterministic late-submit/side-record/dispatch deletion races, post-commit residue restart cleanup, two-service idempotency winners, crash-lease automatic retry and watcher shutdown, exact value-DLP strings, prior-v1 migration/deduplication, trusted live Engine completion, duplicate notification idempotency, next-run scheduling, and verified partial/failed outcomes. Frontend probes cover forged nested gap snapshots and non-gap events with zero projection mutation.

### Baseline note and remaining planned boundary

- `backend/tests/test_worker_completion_v1.py` remains green. In the adjacent `test_engine_display_name.py` run, three existing locale expectations still fail because production returns `Leader` while the tests expect `Coordinator`; this is unrelated to the tracked-turn bridge and was not changed.
- The previous report's statement that legacy execution had no terminal bridge is superseded by this round. The Engine is now connected at its real turn-completion boundary, while deeper per-step Agent/Tool execution projection remains a later Harness integration.
- Task 4 visual status bar/event drawer and Task 5 full Tool Gateway/DLP/MCP internals remain intentionally outside this patch.

## Independent re-review fix round 3/5

The second re-review left exactly two Important findings: unsafe legacy public-event
upgrade and an incomplete governed production lifecycle. This round fixes both without
changing the Task 4 UI or implementing the Task 5 Tool Gateway.

### N1 — allowlisted legacy RunEvent migration

- The v1 migration no longer recursively redacts an arbitrary public dictionary while
  retaining its keys. It now rebuilds every reachable event type from the current
  event-specific allowlist, drops forbidden and unknown outer keys, filters nested
  budget and approval shapes, rejects non-finite legacy budget values, and replaces
  sensitive allowed values with opaque status text.
- Dropped credential/PII fields contribute only fixed classification labels for the
  event types that support classification. Neither the field name nor its value is
  retained in public JSON.
- Event identity, run/group binding, sequence, version, timestamp, sensitivity,
  idempotency key, and audit ordering are untouched. Migration remains one SQLite
  transaction and is deterministic/idempotent on consecutive opens.
- The exact prior-v1 `run_queued` payload containing
  `access_token: legacy-secret`, an unknown `extra` object, and
  `Authorization: Bearer sk-secret` now hydrates/replays after the first and second
  open. The public SQLite JSON contains none of those keys/values and the normalized
  row metadata/bytes are stable on the second open. Parameterized probes cover all 12
  reachable public event types and malformed extra keys.

### N2A/D — durable private approval continuation

- Group-chat routing can atomically persist a private executor continuation beside the
  route approval before returning `waiting_approval`. It is bound to group, task, run,
  approval, route request/idempotency key, route version, risk, execution key, and the
  exact private request envelope (message, attachments, and forwarding metadata).
- Approval resolution and continuation readiness/rejection commit in the same run CAS
  transaction. The server claims `ready -> claimed` with a SQLite compare-and-swap
  before invoking the Engine. Duplicate/replayed resolutions observe the durable
  winner and cannot invoke the Engine twice.
- Cancellation terminalizes continuations in the same transaction as the cancelled
  TaskRun. Rejection/expiry terminalize with approval resolution. Group deletion now
  counts and deletes continuations before completion, so private request content is
  unreadable after deletion.
- Restart reconciliation never replays a claimed request. High-risk ready work
  (including sensitive outbound and write-capable routes) becomes
  `recovered_blocked` and its queued run is paused for explicit approval. Only a
  never-claimed, non-high-risk ready request can be resumed after the project Engine is
  awake.
- A real `_cmd_user_message` regression proves: ambiguous message -> waiting with zero
  Engine calls; approval -> exactly one call containing the original private text;
  governed Verifier completion -> terminal; duplicate approval -> still one call. The
  original text is absent from public RunEvents/snapshots.

### N2B — whole-task ProjectEngine completion scope

- `submit_tracked()` now creates a unique governed scope rather than resolving a
  per-inbox-turn future directly. Coordinator turns, every Worker task spawned from the
  governed context, and Worker-produced Coordinator aggregation notifications retain
  that scope explicitly.
- The trusted Future resolves only when the scope has no pending Coordinator turns and
  no attached Worker task. A concurrent turn cannot attach to or complete another
  scope. Coordinator aggregation can delegate further scoped Workers without
  cross-turn contamination.
- Future cancellation propagates to all scoped Worker tasks; Engine shutdown joins
  Workers and returns a trusted cancelled outcome for any remaining scope. Server run
  cancellation cancels its bridge, which cancels the tracked Future and therefore its
  Worker subtree.
- The real-Engine probe holds a Worker pending after the root Coordinator turn returns:
  the trusted Future and Task Runtime capacity remain nonterminal. Releasing the Worker
  queues a scoped aggregation turn; only after that Coordinator turn finishes does the
  Future resolve. A separate probe verifies cancellation drains the scoped Worker and
  scope ledger.

### N2C/D — truthful executor and Verifier provenance

- Trusted Engine results now preserve `succeeded`, `partial`, `failed`, and `cancelled`
  outcomes plus error provenance. Parameter, provider/model/timeout, permission,
  budget, generic executor, and cancellation categories map to distinct public
  Task Runtime truth; `executor_failure` was added to the versioned backend/frontend
  failure enum.
- Failed executor work records an approved, task/version/evidence-bound mandatory Group
  Verifier decision meaning “the failure classification is verified,” then completes
  with the actual executor failure type. A rejected Verifier decision instead produces
  `verification_failed`; those states can no longer be conflated.
- Successful/partial execution still requires bound evidence, partial remains a
  truthful terminal outcome, and cancellation uses the cancellation transition rather
  than fabricating Verifier failure. No transport endpoint accepts a client-supplied
  Verifier decision.
- Stable execution keys make duplicate completion callbacks no-ops. Continuation state
  and Task Runtime terminal state survive restart, while any uncertain crossed
  executor boundary remains blocked rather than replayed.

### Round-3 exact regression evidence

- `python -m pytest -q backend/tests/test_task_runtime_rereview_round3.py` ->
  `20 passed in 1.33s`.
- `python -m pytest -q backend/tests/test_task_runtime.py backend/tests/test_task_runtime_review_fixes.py backend/tests/test_task_runtime_rereview_round2.py backend/tests/test_task_runtime_rereview_round3.py`
  -> `67 passed in 7.69s`.
- `python -m pytest -q backend/tests/test_groups.py backend/tests/test_group_http.py backend/tests/test_task_runtime.py backend/tests/test_task_runtime_review_fixes.py backend/tests/test_task_runtime_rereview_round2.py backend/tests/test_task_runtime_rereview_round3.py`
  -> `91 passed in 11.42s`.
- `python -m pytest -q backend/tests/test_worker_completion_v1.py backend/tests/test_runtime_impl_v10184.py backend/tests/test_coordinator_no_reply.py`
  -> `26 passed, 10 subtests passed in 2.42s`.
- `npx vitest run src/contract/taskRuntime.test.ts src/store/taskRuntime.test.ts src/contract/group.test.ts src/store/groups.test.ts --pool=forks --poolOptions.forks.singleFork=true`
  -> `4 files / 24 tests passed`.
- Targeted strict TypeScript compilation and ESLint for
  `src/contract/taskRuntime.ts` and `src/store/taskRuntime.ts` passed without diagnostics.
- Python compilation of Engine, Server, Task Runtime contracts/store/service, and the
  round-3 probe module passed. `git diff --check` passed with only the repository's
  existing Windows LF-to-CRLF notices.

### Broad baseline note and remaining planned boundary

- Broad `npm run typecheck` still reports the already documented unrelated repository
  baseline errors (`Conv.unread` fixtures, `calibrateActivity`, old SocketAPI/Bridge
  fakes, PDF worker typing, legacy Store strictness, and similar files). The changed
  Task Runtime contract/store pass the targeted strict compile and lint.
- The continuation record intentionally remains a private Task Runtime side record;
  it is not a new public/frontend contract. Task 5 still owns encrypted vault storage,
  the unified Tool Gateway, MCP transports, DLP gateway, and shell/network policy.
- Task 4 remains responsible for rendering the authoritative run/Agent state in the
  visual status bar and event drawer. No visual UI was added in this round.
