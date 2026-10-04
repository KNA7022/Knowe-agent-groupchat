# P1 protected storage implementation report

Scope: storage prerequisite only, based on `7ddd4f930d8d3138fdb0b6ffe23e8940f4d8f473`. Production dispatch remains disabled. No Engine, factory, Hub, Client, SeenSpeech, projector, notification, provider, scheduler, frontend or public mutation-route changes. This report does not claim Unit P or the real approved Worker path is complete. P2 and independent review remain gates.

## Implemented boundaries

`ProtectedLineages` uses the existing TaskRuntime database and verified card MasterKey/sentinel. Its dedicated private mapping is not an approval registry. Registration checks the real current canonical card generation/digest, owner task/run, Group scope revision and exact roster target under service → Group → SQLite locking. It supports only optional builtin knowledge/planning/coach; coordinator/verifier are already excluded by canonical CardProposal, and expert is explicitly denied here. Sources and courses remain denied, including when a custom scope validator exists. Registration creates no grant and leaves the approval owner waiting.

The encrypted mapping binds independently allocated Harness task/attempt, canonical Group/Worker/project, owner task/run, approval generation/digest, scope revision, keyed instruction digest and, on its first real task-run storage write, the actual Worker run UUID. Distinct ID domains are not guessed from names or roles. Retained authenticated binding permits same-scope completed-owner history reads after succeeded/partial owner termination, but every new seal denies terminal-owner writes. Cancel, fail, scope changes, archive, removal and explicit revoke deny subsequent reads/writes. No execution authority follows from historical reads.

TaskEnvelopeStore receives an explicit capability. It validates the exact instruction/identity and source-free envelope, rejects metadata/context/attachment/file-bearing inputs, seals before exclusive file creation, and authenticates existing bytes before immutable retry comparison. Duplicate equality is keyed. Ref traversal/symlinks are rejected. No protected-format or missing-key fallback to plaintext exists. The only shared legacy path correction is containment.

SQLiteCompletionStore, CompletionAwareTaskRunRepository and SQLiteTaskRunRepository receive explicit capability adapters. Table selection is made at each concrete reader/writer, not by a global SQLite or JSON interceptor:

| Table | Protected body and scalar treatment |
| --- | --- |
| task_runs | Full envelope/run including reasoning/metadata/provenance sealed; clear provenance metadata replaced by content-free reference; keyed payload digest |
| task_journal_v1 | Full journal sealed; reason scalar replaced by authenticated reference |
| task_attempts_v1 | Full snapshot sealed; reason reference |
| completion_events_v2 | Full completion/reasoning/intent/provenance sealed; supersession and active-state changes resealed |
| task_results_v1 | Useful result body sealed, normal joined reads retain independent result identity |
| agent_completion_outcomes_v2 | Full outcome sealed; terminal_reason reference |
| completion_outbox_v2 | Full entry/payload/error sealed; last_error reference; state/attempt mutations resealed |
| completion_projection_receipts_v2 | Effect body and effect_ref sealed; clear effect_ref reference and keyed effect digest; independently bound receipt survives outbox deletion |
| wait_tokens_v2 | Full wait sealed; question/dependencies_json/answer/answer_ref references; resolve/resume status changes resealed |
| deliveries | Text/reasoning/provenance sealed; file artifacts rejected before commit |
| decision_events_v2 | Reason/payload/actor sealed; actor reference |
| task_run_history_v2 | Run/history reason sealed; reason reference and normal history reader |

The thirteenth discovered table, delivery_artifacts, is not admitted by this instruction-only branch. No file-manifest writer is reached for protected inputs. Normal/duplicate/list/recovery paths decode using independently selected SQL identities; mutable authenticated state is resealed. Clear protected IDs use fixed namespaces plus 24/32 hex opaque identifiers; an arbitrary ASCII label prefixed `p1_` is rejected. Existing unrelated legacy repositories remain readable; this is not a legacy-wide migration.

## Cryptography and persistence protocol

AES-256-GCM uses the verified existing MasterKey, fresh nonce per seal and domain-separated AAD containing schema version, existing store/key identity, keyed resolved child-store path, Group, lineage, table/file, independently supplied row and field. No key file/environment fallback or regeneration is added. Strict UTF-8 JSON has a 2 MiB payload bound, bounded nesting/items and pre-serialization byte accounting; ciphertext bounds precede base64/decryption. Errors are content-free. Generic legacy JSON/compression utilities are unchanged. Repositories retain no additional plaintext cache; returned domain objects are necessarily plaintext authorized views.

Registration commits first. A child record's authenticated opaque identity claim commits to runtime SQLite before the corresponding child SQL/file write. Child completion writes remain one local SQLite transaction. A failed child transaction can therefore leave an unused claim and first-run binding; these are identity reservations, not completion/execution evidence. Same-identity retry is supported. Existing-row overwrites cannot recreate a lost claim. Reopen requires the same sentinel/key, mapping, Group scope and exact record context. There is no cross-store atomic commit or automatic effect replay claim. Group/source callbacks are not invoked inside completion SQL; outer repository guards acquire lifecycle locks first.

Private purge first commits a durable revoked fence and erases the mapping body, retaining only content-free opaque identifiers/authentication receipts. It then removes this lineage's envelope files and adapted SQL rows, dependencies before parents. Authenticated claims plus actual task/completion relations cover retained receipts and missing child claims. Interrupted or partially completed cleanup remains denied and is idempotently retryable; late writers cannot reopen revoked bindings. Other legacy rows are preserved. Existing domain objects returned to callers cannot be retroactively zeroized. SQLite WAL/backups, filesystem snapshots and physical sector secure erasure are not guaranteed by this slice; historical bytes were encrypted, but deleting the live body is not a claim that all prior ciphertext copies cease to exist. Whole-app cascade/retention policy is P2.

## Behavioral evidence and dependency fixes

Initial real dependency RED: delegate create raised missing `compress_json_dumps`; commit_success fault injection left task_runs count **1**, expected **0** (2 failed / 1 passed). Minimal fixes import the existing compression/digest helpers and replace transaction-internal executescript with individual fixed DDL statements. The rollback assertion explicitly accepts either transactional removal of the table or zero affected rows, not arbitrary query errors.

Initial protected boundary RED: actual WAITING read failed scalar authentication (1 failed / 4 passed), identifying wait_tokens_v2 question/dependencies_json/answer/answer_ref writes that had not all been adapted. Subsequent raw SQLite marker scanning exposed agent_completion_outcomes_v2.terminal_reason still clear; it now uses the same explicit scalar-reference adapter. Both normal returned values and raw storage are asserted. These early five tests were not a full matrix. Final self-review RED: a delivery_id containing an ASCII content label was accepted; fixed opaque-identity validation rejects it (1 failed before fix).

Final focused storage module: 41 passed. The latest combined pre-final-format-tightening run was 142 passed (41 P1 plus 101 bound-card tests); the final fixed-format implementation then passed all 41 P1 tests again. Tests use real temporary GroupRepository, TaskRuntimeService/cards, random MasterKey, SQLite and files. They cover start/progress/success/wait/fail/cancel/block, decisions/history, outbox claim/fail/ack, receipt retention, owner success vs cancellation, source/course/removal denial, unsupported targets, real close/reopen, duplicate and fault rollback, partial claims, key/sentinel/mapping/format/row/table/field/store/version tamper, content bounds, path/file denial and mixed legacy purge.

Scans inspect populated DB SQL, raw DB/WAL/envelope files and decoded legacy JSON/compressed candidates for Chinese/ASCII markers and raw key bytes. Reached populated task_runs/result/journal/outbox/outcome tables are asserted, not merely scanned as empty. Additional success/wait tests scan their reached scalar tables. No Worker/provider/tool is constructed or invoked by these tests; no UI/provider callback acceptance is claimed.

## Freeze, regression and limitations

Production and tests were frozen after the final 41-test focused pass. The single frozen full backend run returned **1763 passed, 5 existing skipped, 10 subtests passed in 166.35s**. Raw final regression and SHA-256 freeze evidence are in `protected-storage-p1-evidence.txt`; `py -3.12 -m compileall -q backend` and scoped `git diff --check` also exited 0 on the frozen implementation. No skips, xfails or exclusions were added. No TypeScript changed, so prior frontend evidence is preserved without another frontend run.

P2 must compose canonical Engine holder registration, actual Worker start callback, Hub/chat/session/result persistence, notification/projection/SeenSpeech policy, whole-app deletion and restart behavior. Protected WAITING→new Worker-run resume is deliberately unavailable: a different run UUID is denied rather than auto-resumed. Store relocation changes keyed path binding and is not silently migrated. Global list methods over a store containing multiple protected lineages fail closed when the selected capability does not match a returned row; P2 must supply deliberate per-lineage composition/filtering rather than treating one capability as global authority. No arbitrary expert executor profile is supported.

Backend Python Expert guidance informed lifecycle-lock placement and real storage/fault tests; the SDD implementer prompt guided the bounded implementation and separate report. Self-review retained the narrow dependency repairs, explicit adapters and no P2 composition. Independent security/spec review remains required; P1 passing alone is not release authorization.
