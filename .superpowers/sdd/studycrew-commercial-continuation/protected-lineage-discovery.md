# Unit P — protected lineage source discovery and P1/P2 handoff

Status: **Discovery complete; controller accepted P1/P2 decomposition; awaiting P1-only brief. No implementation.**

Inspected on 2026-10-04 at accepted dispatch BASE `53efbb109713cbe21635fcfc3fb417238648ae4e`, branch `feature/studycrew-foundation`. No production/test/schema changes, provider requests, key creation, migrations, or Worker execution were performed. The controller's existing `progress.md` change was left untouched. This report is source-inspection evidence, not dynamic sink coverage or security acceptance.

## Why this implementation stopped

The controller has resolved the schema, canonical target, and optional Seen Speech decisions below. There is no outstanding product permission question. I could not confidently implement and verify the entire cross-store change in one pass after tracing its actual boundaries. The brief's requirement to stop rather than improvise a broad Harness change applies here. In particular, the implementation must not hide numerous explicit SQL field boundaries behind a generic JSON or SQLite interceptor.

Suggested decomposition, with the route remaining unavailable throughout intermediate work:

1. P1: private authenticated mapping/codec, TaskEnvelope serialization and explicit Completion SQL/repository adapters, including actual protected start journal persistence. Focused repository tests establish immutable identity and field/row/group AAD boundaries.
2. P2: narrow Engine/factory composition, Hub/persist/client-cache delivery/replay, protected notification handling, deletion integration, and the actual Worker-to-restart acceptance matrix.
3. Only after both, freeze source for the full regression and independently review **the whole original Unit P**. A P1 pass is not Unit P acceptance and cannot enable Unit D.

The controller accepted this decomposition. It does not reduce the original acceptance requirements. Production edits remain paused until the P1-only brief arrives.

## Verified actual execution and persistence selection

| Boundary | Actual source behavior | Required protected adaptation |
|---|---|---|
| `engine.py:9195`, `inject_task_envelope` | Creates/reuses a Worker holder, commits TaskEnvelope, then places the domain object in `_task_envelopes`; it does not itself execute the Worker. | Exact backend canonical binding before holder creation; immutable encrypted commit; retain queue/start identity. |
| `knowe_harness/store.py`, `TaskEnvelopeStore` | Plaintext JSON is serialized before exclusive file creation; duplicate commit parses plaintext and compares envelope digest. Reference reads accept paths without a containment/authority boundary. | Seal before any file/temp write, strict bounded serialization, authenticated reference resolution, stable keyed equality, reject cross-group/path substitution and missing protected binding. |
| `engine.py:3906`, `_run_worker` | Adds turn attachments, `_agent_memory_block`, delivery channel and workspace baseline before execution. | Protected instruction-only branch must exclude all inherited context and recheck the canonical scope. |
| `engine.py:3979`, `_run_worker_attempt` | Adds handoff/title/user-address metadata; prepares context; uses the configured `WorkerRuntimeFactory`; calls `runtime.run`; then commits success/run and drains the completion outbox. | Preserve real factory, Gateway, Runtime and queue; supply explicit protected composition and content-free failures. |
| `worker_gateway_runtime.py:113`, `WorkerRuntimeFactory` | Builds `ProviderModelAdapter` from the real holder, fixed Worker tool registry, process registry and ordinary `backend.runtime.WorkerRuntime`. | Same implementation with protected lineage/key/repository capability injection; no provider/tool grant from registration. |
| `worker_gateway_runtime.py`, `register_active_run` | `on_run_started` **only populates Engine `_worker_runtime_runs`**. Factory does not pass a task-run repository to `WorkerRuntime`. | Protected branch must call existing `SQLiteCompletionStore.create_started_run` before any model send. |
| `runtime.py:989–1009` | Allocates a fresh Worker `TaskRun`, assembles transient initial messages, invokes `on_run_started`, then enters ordinary model loop. | Bind actual Worker run ID during that existing callback; do not conflate it with the Task Runtime owner run or allocate another scheduler. |
| `engine.py:4065–4067` | `SQLiteCompletionStore.commit_success` / `commit_run` is the actual terminal persistence path. | Seal explicit fields inside its existing transactions. |
| `engine.py:3240`, `completion_run_for` | Constructs `CompletionAwareTaskRunRepository(root, self.completion_store).get(task_id)` for reads. | This wrapper/delegate must decode the same protected storage. |
| `knowe_harness/completion.py:3075+` | `CompletionAwareTaskRunRepository` wraps `SQLiteTaskRunRepository`; `create` can call `create_started_run`, but no actual factory caller currently reaches that create path. | Do not cite wrapper-only repository tests as proof of Worker-start integration. |
| `knowe_storage/task_run_repository.py` | `create`/`save` use compressed JSON; ordinary reads use `json_loads`; conflict paths also parse payloads. | Compression is not protection. Cover writes, normal reads, duplicate/create collision, optimistic update and state-list reads. |

The comments on Engine `_completion_components` imply WorkerRuntimeFactory already writes the shared runtime database; the inspected factory/runtime implementation does not establish that. Test the actual callback path rather than relying on that comment.

## SQL sink inventory to complete explicitly

The following are source-confirmed writers/readers, **not a claim they were dynamically reached in this task**:

- `SQLiteCompletionStore._upsert_task_run`: `task_runs.payload_json`, plus any content-bearing provenance fields. Keep opaque task/run IDs, state, version and timestamps queryable.
- `_insert_journal_entry`: both `task_journal_v1` and `task_attempts_v1`, including `reason` scalars and `payload_json`; this helper is currently static.
- Completion commit paths: `completion_events_v2`, `task_results_v1`, `agent_completion_outcomes_v2`; the outcomes table also stores clear `terminal_reason`.
- Outbox creation/update/retry: `completion_outbox_v2.payload_json`, clear `last_error`, and projection receipts. `acknowledge_outbox` currently hashes effect payload with unkeyed SHA-256.
- Wait creation/update/resume/resolve: `wait_tokens_v2`, including content-bearing question/dependency fields and payloads. Preserve worker/task/attempt and open/resuming filters.
- Delivery insertion: `deliveries` plus `delivery_artifacts` payloads; preserve transaction and immutable-delivery semantics.
- Decision/history paths: `decision_events_v2` and `task_run_history_v2`; history contains clear `reason` and serialized run content.
- Readers include `get`, `active_for`, task/active lists, `task_result`, journal/attempt readers, pending/claim/ack/fail outbox, wait lookup/resume/resolve, decisions, history/resume and terminal reconciliation. Recovery cannot silently skip unreadable protected ciphertext.

The existing database has many content-bearing SQL scalars beside JSON. Encrypting only `payload_json` would leave markers in durable storage. Table/field/row identity must be supplied explicitly to a shared codec at each relevant boundary.

### Explicit SQL adapter map for P1

| Table | Explicit payload/scalar handling | Identity and query constraints | Existing entry points |
|---|---|---|---|
| `task_runs` | Seal `payload_json`; use keyed `payload_sha256` where present. Validate provenance as bounded backend schema or seal any content-bearing extension. | Primary `task_id`, unique `run_id`; version CAS, state and updated-time ordering stay queryable. Protected decoder must cross-check clear task/run/version/state against authenticated payload. | Completion `_upsert_task_run`; wrapper `create` resume replacement and optimistic-success `save` fallback; delegate create/save/get/get_by_run_id/list_by_state. |
| `task_journal_v1` | Seal payload and `reason`. | `journal_entry_id`; unique task/attempt/sequence/state/runtime_state; preserve ordered journal reads. | `_insert_journal_entry`, `journal_for`; started/progress and final result paths. |
| `task_attempts_v1` | Seal payload and `reason`. | Composite task/attempt identity; update only when current terminal=0 or new terminal=1. Active filters use worker/project/terminal. | `_insert_journal_entry` upsert, `record_progress`, `attempt_snapshot`, `active_attempts`. |
| `completion_events_v2` | Seal payload. Authenticate `active`, status, version and supersession identity; an active-bit update must also reseal payload. | Primary completion ID, unique idempotency key, unique active task/attempt; active/terminal indexes remain intact. | `_insert_event` duplicate and active replacement branches; commit/transition; all completion getters/lists. |
| `task_results_v1` | Seal payload. | Primary result ID and unique completion ID; task/attempt/completion and project/worker queries. | `_insert_task_result` duplicate/get paths, `_ensure_result_commit`, `task_result`. |
| `agent_completion_outcomes_v2` | Seal payload and `terminal_reason`; provenance rule as above. | Outcome ID, unique completion ID; task/attempt and project/worker indexes. | `_insert_agent_completion_outcome` within commit transaction. |
| `completion_outbox_v2` | Seal payload and `last_error`. Route key is only safe if constructed from validated opaque channel/project/worker identifiers; reject arbitrary content in protected route. | Outbox ID; unique completion/kind/route; preserve state/attempts and priority ordering. | `_insert_outbox`, `pending_outbox`, `claim_outbox`, `fail_outbox`, `acknowledge_outbox`. |
| `completion_projection_receipts_v2` | Seal payload and any non-opaque `effect_ref`; keyed `effect_sha256`. | Effect ID and unique outbox ID; receipt survives outbox deletion. | `acknowledge_outbox`, `projection_receipt`. |
| `wait_tokens_v2` | Seal payload, `question`, `dependencies_json`, `answer`, `answer_ref`; do not treat reference text as authorization. | Wait token ID; unique active task/attempt for open/resuming; worker/status/time filters. | `_wait_token_for_event` insert and reuse; `_resolve_wait_rows`; `_insert_event` close-waits loop; active/get readers; `resume_wait`, `resolve_wait`. |
| `deliveries` | Seal payload; validate provenance. | Delivery ID, unique idempotency key, unique task/run. Both duplicate lookup forms must authenticate before returning a domain object. | `_insert_delivery`; `reconcile_terminal_runs` delivery recovery. |
| `delivery_artifacts` | Payload and path/media/ownership/hash can all disclose content. Source-free instruction-only protected route should reject artifacts/file claims; future support needs explicit keyed/path adapter, not plaintext columns. | Existing primary key `(delivery_id,path,sha256)` and FK cascade must not be silently changed. | `_insert_delivery` artifact loop. |
| `decision_events_v2` | Seal payload; `actor` must be validated opaque/system identity or sealed. | Decision ID, task/attempt/time index, completion relation. | `_insert_decision`, `record_decision`, `decisions_for_task`, `list_decisions`, transition-with-decision. |
| `task_run_history_v2` | Seal payload and `reason`; do not derive clear unkeyed stable history ID from arbitrary reason text. | History ID and explicit task/attempt/run mapping; repeat archive idempotency. | `archive_run_for_resume`; wrapper resume path. |

Reader queries frequently select **only** `payload_json`; they need independently selected row identity for field/row AAD. Taking row identity only from inside an encrypted envelope would allow swapping the entire envelope between rows. Use clear primary identity from the query result or exact caller key, then cross-check the decrypted domain object against it. A protected payload's stored schema/store/lineage metadata must not become caller-chosen authority.

`_payload_provenance` also parses arbitrary stored JSON; direct usages should be traced before replacing JSON handling. Static `_insert_journal_entry`, `_insert_task_result`, `_resolve_wait_rows` need an explicit repository codec argument or instance-bound method, not ambient identity.

### Transaction and retry constraints

- `commit_success` intends one transaction for `_upsert_task_run`, `_insert_delivery`, `_insert_event`, agent outcome; `_insert_event` calls `_ensure_result_commit` for result/journal/outbox and closes prior waits.
- `commit_run` intends the same atomic run/event/result/journal/outbox/outcome set, plus the WAITING token when needed.
- `_insert_event` idempotent replay repairs missing result/journal/outbox, so duplicate tests must cover repaired subordinate records without new completion identity or fresh Worker execution.
- `acknowledge_outbox` inserts the receipt and **deletes the outbox row** in the same transaction. The receipt's authentication/mapping cannot depend on the later existence of its outbox row. Recommit may recreate an outbox intent that is reconciled through the retained receipt.
- `CompletionAwareTaskRunRepository.create` WAITING resume archives the old run, then replaces task_runs with a new Worker run ID, then marks started. Do not assume all three operations are currently one transaction; protected scope must either support this exact lifecycle with an explicit protocol or fail closed until a later authorized resume adapter exists.
- Source callbacks and lifecycle guards must be acquired before SQL. Cross-store mapping/completion writes cannot be represented as one SQLite transaction when they use separate files.
- **Potential existing atomicity defect to verify in P1:** `_ensure_runtime_tables(conn)` calls `conn.executescript` and is called from inside `create_started_run`, `_upsert_task_run`, `_insert_delivery`, and some readers. Standard sqlite3 `executescript` can implicitly commit a pending transaction. Verify actual `SQLiteDatabase` behavior with a fault test before claiming the existing commit/outbox transaction is atomic. Prefer moving idempotent schema initialization before transactions or individual statements if confirmed; do not hide it with a new store.
- **Potential existing delegate defect:** `SQLiteTaskRunRepository.create/save` refer to `compress_json_dumps` and `payload_sha256` but the inspected import list does not import those functions. Actual Engine terminal writes bypass these methods; P1 adapter tests must distinguish a legacy NameError from encryption behavior and handle only the relevant scoped issue.

No dynamic writer instrumentation, SQL trace or rollback fault injection was run during discovery; those remain acceptance work.

## Projection, event and cache sinks

| Sink | Source evidence | Scoped disposition |
|---|---|---|
| Projector task-state pointer JSON | `_task_state` uses `_atomic_json`. | Opaque metadata may remain clear; verify all values. |
| Projector report | `_report` calls `handoff.write_report`; report/audit files can duplicate final output. | Explicit metadata-only skipped receipt for protected branch. |
| Projector coordinator pointer JSON | `_coordinator` constructs a content-bearing notification message then writes its SHA-256 to pointer metadata. | Use keyed equality or encrypted payload; avoid unkeyed instruction/output equality metadata. |
| Projector memory/worklog | `_memory` writes projection JSON, Markdown and state JSON. | Explicit metadata-only skipped receipt. |
| Projector knowledge | `_knowledge` may cause legacy side jobs. | Explicit skipped receipt, no side jobs. |
| Projector UI | `_ui` emits reason/gaps/actions and correlated stable IDs to Engine. | Encrypt before durable/cache boundary; retain useful authorized transient views. |
| Engine completion result | `_emit_completion_visible` publishes full result and reasoning through Hub. | Required useful result; cannot disable it to satisfy marker scans. |
| Engine Seen Speech | `seen_speech.py` appends exact visible text to JSONL and caches plaintext `VisibleSpeech` in `_rows`. | Controller explicitly permits a metadata-only skipped receipt for protected lineage. Never call its raw record path for protected text. |
| Engine coordinator notification | `_write_completion_notification` writes content/error/structured notification JSON via temp + replace. `_recover_completion_notifications` currently reads files and requeues unfinished work automatically. | Preserve encrypted pending/reconciled truth; protected recovery must not autoqueue a coordinator/provider turn or mark review completed. |
| Hub ring/idempotency | `Hub` caches committed events in project ring and `_idempotent_events`. | Ciphertext/opaque authenticated historical envelopes only. |
| Client replay handoff | `Client.send_live` copies events into `_live_replay_buffer`; `release_live_replay_fence` later sends those cached copies. | Cache protected envelope and hydrate at authorized send boundary; hydrating before buffering is insufficient. |
| Persistent events | `persist.Store.append_event`, archive gzip rotation, `load_events`/`load_all_events`. | Seal before normal JSONL/archive writers; ordinary replay must distinguish persistent envelope from transient view. |
| Engine reasoning stream | Factory `_relay_reasoning` calls `_fire` with task/attempt/scope IDs; Runtime also accumulates reasoning in run metadata. | Bind this actual event lane as well as final completion lane; no raw persistent/cache copy. |

## Controller decisions received before edits

1. Use one narrowly scoped private mapping table in the **existing Task Runtime SQLite database**. It records authenticated opaque lineage/protection/revocation state, not a second approval registry. Canonical approval/run/card generation remains admission authority.
2. Keep Harness task/attempt IDs, Task Runtime owner task/run IDs, Worker run IDs, completion IDs and event IDs distinct. Bind actual IDs where allocated; no source callbacks under SQL.
3. Successful owner terminal denies further effects while allowing already completed same-group historical reads through retained authenticated lineage. Group/source deletion must erase content and leave only content-free opaque deletion receipt. Do not claim a generic tombstone makes retained decryptable bodies acceptable.
4. Wrong/missing binding/key must fail closed. Removing a clear marker must never convert protected data to legacy plaintext.
5. Add protected-only start-journal wiring through the existing callback; no second scheduler.
6. Minimal backend canonical Worker composition may move from Unit D into P. Initially support only optional builtins `knowledge`, `planning`, `coach`. Deny coordinator, verifier, expert and legacy_expert without separately supported constrained executor profile.
7. `register_protected_worker(group_id, canonical_agent_id)` must verify exact current Group roster identity under the real lifecycle guard and use the **same** `gag_…` ID in Engine. No guessing a legacy ID from role/name, no Group membership mutation, no renderer mutation route.
8. Existing holder collision must deny unless exact canonical group/ID/role matches. Registration makes no provider request and grants no tools. Do not invoke raw `_write_agent_profile` or name/history persistence as an incidental side effect.
9. Seen Speech is optional memory duplication for this route and may produce an explicit metadata-only skip receipt; required Hub/result/completion truth stays useful.
10. Protected coordinator notification stores encrypted pending/reconciled truth without automatic provider/coordinator execution on original completion or restart. Do not discard pending review as completed; Unit D must later resume under legitimate authorization.

## Canonical composition finding

`Engine._get_or_create_worker` accepts arbitrary IDs, creates a real provider holder, then persists roster/name and writes an agent profile. `server.py` `/groups/roster-update` updates GroupRepository only. No supported durable `gag_…` to legacy Worker mapping was found in the inspected route. A raw call to `_get_or_create_worker` would therefore both bypass explicit canonical mapping and introduce further plaintext profile/name copies. The controller-approved same-ID private composition above is the required new seam.

`ProviderModelAdapter.from_legacy` consumes the holder's real `ProviderGatewayClient`. Worker factory explicitly canonicalizes tool authority metadata but its Provider Gateway ordinarily uses the ambient authority provider. Tests must prove explicit real canonical provider authority propagation; an enclosing test `.active()` context would hide the missing propagation.

## Proposed transaction protocol — not yet implemented

- Register immutable owner/card/generation and allocated Harness task/attempt plus canonical Worker identity under runtime → Group admission guard → runtime SQL order, using the verified existing MasterKey capability and bounded authenticated payload schema.
- Only after committed registration may encrypted TaskEnvelope serialization occur. Duplicate commit compares canonical keyed equality and exact identity, not randomized ciphertext.
- Existing Runtime `on_run_started` binds its actual run ID and commits the protected started journal before model invocation. Cross-database crash outcome must remain explicitly uncertain; restart reconciles stored identities without requeueing the Worker.
- Completion/outbox remain in the existing shared completion transaction. Bind completion/event identities with explicit idempotency and retain the historical generation for replay.
- Hydrate only for authorized transient use; store/cache writers always receive ciphertext or an authenticated versioned reference.
- Implement and test lifecycle deletion across the private runtime mapping and per-project completion/event/envelope stores. Physical retention limits, backups and WAL/checkpoint behavior must be reported honestly.

No cross-store atomicity claim is justified by current code. The exact final bind/commit crash protocol still requires implementation tests.

## Verification status and raw evidence

No RED/GREEN tests ran. There are no new tests, skips, xfails or exclusions. No full backend/frontend regression ran because no implementation exists to freeze. The parent's prior regression evidence is not presented as evidence for Unit P.

Raw final read-only terminal evidence is in `protected-lineage-discovery-evidence.txt`. It records BASE, branch, working-tree status and the source references proving selected start/commit/read paths. The report and evidence file are uncommitted for controller handoff; no implementation commit was created.

Unsupported/release state: **all protected dispatch remains unimplemented and unavailable**. Unit D must not start based on this report.
