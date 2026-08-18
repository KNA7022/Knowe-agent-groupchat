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
