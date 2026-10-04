# Encrypted approval-card foundation implementation report

Base: `1345ec4abd4f85937a9257983bfc5ed6bb8baff7`, branch `feature/studycrew-foundation`.

## Scope and authority

This is the private foundation only. It is not usable card feedback, restored dispatch, a working producer, or commercial acceptance. No renderer endpoint, provider flight, feedback child, Worker, legacy allowlist, Hub plaintext history, or TaskEnvelope adapter was added. The existing local RAG implementation was preserved. A precise opaque Group-agent ID classification repair was separately approved by the controller after the first complete backend regression exposed it.

`TaskRuntimeService(..., card_master_key=..., card_source_validator=...)` exposes `service.cards.create/read/amend/resolve` to trusted backend composition. `create` accepts the real owner task/run tuple and resolves exact target membership from the canonical Group roster. A role without a target ID must identify exactly one existing member; arbitrary targets and ambiguous expert roles are denied. The persisted approval relation supplies group/run ownership. Legacy nullable rows are never backfilled from events or latest runs.

`BoundCardState` and `CardProposal` are private frozen validated contracts with content-free repr. Model proposal input accepts only target role/agent, instruction, title and description. Instruction/title/description limits are 16,384/256/2,048 Unicode characters, with strict UTF-8. Identifiers and integer versions are bounded; encrypted envelopes are bounded at 128 KiB. A card has at most 64 typed request receipts, with a final resolution slot reserved. Conflicting reuse is denied; matching retries return current state/the original resolution without repeating a write or minting another grant. Duplicate rejected resolutions use authenticated opaque tombstone receipts after content erasure.

Creation atomically commits the approval, encrypted state, runtime event and WAITING_APPROVAL owner. Amendment and approval share `TaskRuntimeStore.change_run`'s `BEGIN IMMEDIATE` boundary; its new pre-idempotency transaction guard authenticates and checks the same current generation/digest before mutations. Amendment changes generation, state version, epoch and digest. Resolution increments state version/epoch, creates at most one exact single-use `card_dispatch` grant and queues the owner; this foundation does not schedule or dispatch it. Public event projection happens only after all admission locks have been released. Generic resolution denies both the reserved purpose and any bound-card envelope marker, including when clear purpose metadata is tampered.

## Storage and keys

Nullable columns live on `studycrew_approvals`; no parallel approval/identity database exists. `studycrew_card_crypto` holds one stable store identity, opaque key identifier and encrypted sentinel. Existing `MasterKey` AES-256-GCM is injected, supporting the repository's two established Python import spellings. No new key file, secret dependency, environment fallback, key regeneration or renderer key was introduced. Future production composition must pass the existing verified DPAPI-protected application key.

Strict canonical UTF-8 JSON is sealed before SQLite receives it. Every write gets a fresh nonce. Domain-separated AAD binds format version, stable store identity, key ID, canonical group/task/run/approval, content generation, feedback epoch, state version and field identity. Proposal equality and request/receipt/scope fingerprints use domain-separated keyed HMAC, never an unkeyed content hash. Clear policy/expiry metadata is cross-checked against authenticated state. Every private operation verifies the persistent sentinel; wrong/missing keys, absent/corrupt sentinel, substituted fields and tampered metadata deny without replacing existing ciphertext.

Public `ApprovalRequest`, runtime snapshots/events and generic logs do not receive private snapshots. Only opaque metadata and keyed receipts remain after invalidation. Existing runtime terminalization/rejection paths clear card ciphertext and revoke card grants through SQLite triggers. Explicit scope/expiry revocation and card rejection checkpoint the WAL after their commit; `secure_delete` is enabled. This does not claim deletion of historical external backups or protection against rollback of an entire stolen database.

## Group concurrency and source boundary

Controller-approved scope clarification added a process-local `RLock` shared by canonical Group registry path. Actual Group read/modify/write entry points participate: creation, replacement, rename/archive/restore, roster, course/source binding mutations, duplicate transitions, deletion transitions and initial journal normalization. Deletion listeners retain their first-insertion notification semantics and run after the complete mutation guard releases.

Lock order is runtime service mutex -> Group registry admission guard -> optional backend source-version guard -> runtime SQLite transaction. No Group/RAG callback or await occurs within the SQLite transaction. Card event listeners run after the service and Group locks release. Two independent GroupRepository instances over the same root and two independent TaskRuntimeService connections are exercised with barriers. This is explicitly not an interprocess file lock.

Default admission requires no linked sources and no course bindings. Group lifecycle, delete intent, runtime quarantine, roster and keyed full Group revision remain current through commit. Any changed captured Group revision invalidates a card, including source/course additions or target removal. Nonempty source/course groups fail closed without a real backend-owned source-version guard. `CardSourceValidator.guard` is a typed composition capability that must hold the actual source mutation fence until commit; it cannot be a caller boolean. No concrete cross-RAG document-content version fence is implemented or claimed. Later source-backed proposals cannot be enabled until that seam is implemented and independently verified.

## Files

- `backend/task_runtime/card_state.py`: private contracts, bounded encoding, injected encryption, sentinel, envelope, keyed receipts and lifecycle cleanup.
- `backend/task_runtime/bound_cards.py`: canonical admission, target binding, transactional create/read/amend/exact resolve, private integration capability.
- `backend/task_runtime/store.py`: nullable state attachment, transaction precondition hook, deferred event publication and generic bound-card detection.
- `backend/task_runtime/service.py`: optional backend capabilities and generic resolver denial.
- `backend/task_runtime/contracts.py`: exact `group_agent_id` opaque control-value recognition, preserving existing agent/role validation and general PII filtering.
- `backend/groups.py`: shared in-process admission guard on actual registry mutations and unlocked deletion notification.
- `backend/tests/test_bound_cards.py`: focused real repositories/encryption/transactions and deterministic concurrency tests.
- `backend/tests/test_task_runtime_rereview_round2.py`: fixed-real-ID variant of the existing trusted completion bridge regression.
- This report and `card-state-test-output.txt`: evidence and exact final terminal summaries.

## Test-first evidence and development corrections

Initial contract/transaction tests were written before the new module. Command: `py -3.12 -m pytest backend/tests/test_bound_cards.py -q`. RED: `ModuleNotFoundError: No module named 'backend.task_runtime.bound_cards'`, `1 error in 0.85s`. This was a collection-level RED only; it is not evidence that all security cases independently failed against an old implementation.

Initial implementation iterations exposed import-identity mismatches in Group enums, a missing optional target in the fixture, and a test/event name outside the existing public-event schema. Those were corrected without expanding public event allowlists. An incorrectly located private identity validator was also caught and corrected before the frozen regression.

Meaningful behavior RED during self-review: `py -3.12 -m pytest backend/tests/test_bound_cards.py -q -k 'publication_occurs or bounded_receipts' --tb=short` produced `2 failed, 69 deselected in 3.09s`. The bounded-receipt test reported `Failed: DID NOT RAISE CardStateError`; the publication test reported `[False] == [True]`, proving an independently acquiring listener could not obtain the service/Group guard while publication was running. Fixes reserved the final resolution slot and deferred publication until guard release. The subsequent focused run returned `71 passed in 5.77s`, then additional edge tests grew it to `88 passed in 7.20s`.

Coverage includes strict payload/reserved-field validation; canonical owner/target; no key/wrong key and restart; sentinel loss/tamper; store/key/row/group/field/version/epoch/ciphertext/task/expiry substitution; fresh nonces and exact decrypt; no plaintext/key material in raw DB/WAL, public SQL metadata, snapshots or captured logs; create rollback before publication; exact duplicate/conflict/bounded receipts; all invalid creation and pending owner states; finite expiry; real archive/delete/source/course/target/quarantine invalidation; cancellation and terminal erasure; default resolver denial even after purpose tamper; source-backed default denial; legacy non-backfill; independent-store amendment/approval winner ordering; real archive/card commit ordering; actual cross-instance Group archive/delete/unlink/unbind fences; listener lock release; and zero grants/invocations/continuations/child runs on denied paths. No external provider was contacted.

The first frozen full backend command returned `1 failed, 1707 passed, 5 skipped, 10 subtests passed in 188.40s (0:03:08)`. The original failing test was `test_live_engine_completion_reaches_verifier_terminal_and_dispatches_next`. Its real generated verifier ID, `gag_931dd5c0d0d14143976714bcd4f4cbd6`, contained a phone-shaped numeric substring, and the prior opaque-ID set omitted Group Agent IDs. This caused public event validation to deny trusted verifier progress. The controller approved one exact `group_agent_id` pattern, `^gag_[0-9a-f]{32}$`, without changing general text policy or narrowing legacy agent/role fields. A fixed-ID variant of the actual bridge test reproduced RED (`1 failed, 1 passed, 9 deselected in 4.04s`). After the fix it passes; four adversarial cases keep bare phone text, attached text/phone and malformed IDs denied. No timeout or skip was added.

Separate self-review tightened corrupt SQLite BLOB handling before converting or decrypting data: integer values must not invoke `bytes(n)` allocation. Two instrumented-real-crypto cases failed (`2 failed, 88 deselected in 0.86s`) before the bytes-type/length guard and then passed (`2 passed, 88 deselected in 0.95s`). Sentinel loss also treats all persisted envelope version markers as occupied, preventing silent reinitialization if clear key metadata was damaged.

## Frozen verification

Final full backend: `1715 passed, 5 skipped, 10 subtests passed in 159.20s (0:02:39)`, exit 0. No source or test edits occurred during this final run. Exact original failure and final terminal summaries are saved in `card-state-test-output.txt`.

Completed commands and actual output:

```
py -3.12 -m pytest backend/tests/test_bound_cards.py backend/tests/test_task_runtime_rereview_round2.py -q --tb=short
105 passed in 9.88s

py -3.12 -m pytest backend/tests -q
1715 passed, 5 skipped, 10 subtests passed in 159.20s (0:02:39)

. ./.tools/node24.ps1
npm run typecheck
npm run lint
npm run test
Node v24.9.0; typecheck exit 0; lint exit 0
Test Files  55 passed (55)
Tests       588 passed (588)
Duration    38.25s

# Public contract mirror rerun after the Python-only exact Group-agent ID fix:
npm run test -- src/contract/publicEvents.drift.test.ts src/contract/taskRuntime.test.ts
Test Files  2 passed (2)
Tests       10 passed (10)
Duration    1.21s

py -3.12 -m compileall -q backend
exit 0

git diff --check
exit 0
```

Frontend tests retain existing fixture diagnostics (unknown events, local port 8081 connection refusal, Zustand deprecation); there were no failing tests. Git emitted the configured LF-to-CRLF checkout warnings for existing modified files, with no whitespace errors. All file edits used UTF-8; no skips, xfails or exclusions were added.

The full Node gates ran before the final Python-only hardening/classification correction; frontend source was unchanged. The targeted public-contract mirror, compileall and diff check ran after that correction, as agreed with the controller. Final frozen SHA-256 source/test fingerprints:

```
backend/groups.py                                      69F2F85210D86F8C797FD786A0B70B86A26C98E14FACFE1259D819844833E644
backend/task_runtime/contracts.py                      E96DE52834545A37A7FF9D9B91B602449CFC866B25E35CE754AE77073FFE684C
backend/task_runtime/store.py                          3EB82E2F5B3E4EF39DB418679082E733F4296F4FDC4F16793761075A296A7FC3
backend/task_runtime/service.py                        C6EBB83591DC085F0415C374E11B5D01B89319BEEDC3277C72E6ED4E42035930
backend/task_runtime/card_state.py                     71BD78D1ABF9203B0F7B5DE0F9C6716F71E370E42571C4C757549A60503608A9
backend/task_runtime/bound_cards.py                    03185E5B110406848560A0E86F72FD5BC4A61CD1AEB475F7C84E1FB55F53CC3F
backend/tests/test_bound_cards.py                     7D9C27E274B7D2C2E8C34EDDF3F1A846E55F80BBED0A8C986D22563A638F54BC
backend/tests/test_task_runtime_rereview_round2.py      6416E69539E42297DD10BDA8499CAB14ABCEAC1AD6124C9A57194B5279F61FE4
```

## Subsequent integration seams and limitations

1. Production composition must inject the verified existing DPAPI MasterKey and a real source-content version guard shared with actual RAG mutations. Default source/course denial must stay until this is proven.
2. The governed coordinator `propose_next` adapter must call this private service with backend-captured owner authority and publish only after commit. The legacy four-read-tool allowlist remains unchanged.
3. Hub/Gate rendering and encrypted historical generation projections must hydrate authorized transient snapshots without sending them through existing JSONL/history serializers. No public API should expose `BoundCardState` wholesale.
4. Exact approved dispatch must wait for scheduler RUNNING, reauthorize current scope/target/busy state, consume the exact single-use grant, and use encrypted TaskEnvelope persistence plus stable queue/intent identity. No dispatch adapter exists in this slice.
5. Feedback child admission, parent budget reservation, child purpose/epoch checks, Provider Gateway, aux secret binding, cancellation, supersession and no automatic restart replay remain separate required implementation.
6. Renderer version/digest controls, common runtime/legacy resolution routing, source-backed workflows, end-to-end authenticated command and inert-provider tests remain unimplemented. Legacy unowned cards remain unsupported.

Backend Python Expert and its testing guidance informed validated private contracts, typed denials, synchronous bounded transaction critical sections, real repository fixtures and independent concurrency tests. Existing dataclass conventions were retained without adding a Pydantic dependency solely for this internal boundary. SDD implementer self-review checked completeness, security/lifecycle ordering, minimal scope and meaningful tests; independent task review is still required before dependent integration starts.

## Review fix round 1/5 — I1

Fix base: `40354c7cb2994c1aff9fb0c527f73ba3432c0b99`. Independent review: `reviews/REVIEW-card-state-40354c7.md`, 0 Critical / 1 Important / 1 Minor. This fix addresses Important I1 only; noisy-gate cleanup M1 remains controller-deferred.

Reviewer finding: an absent/ambiguous **incoming** amendment target raised `CardScopeRevoked` before checking the current card/version. `_admission` then irreversibly cleared the valid persisted card while leaving the approval pending and owner waiting. The reviewer's real temporary-repository diagnostic supplied generation `0`, an incorrect digest and nonexistent `gag_00000000000000000000000000000000`; it observed `ciphertext erased: True`, `original remains readable: False`, `owner state: waiting_approval`, and no grant.

Reproduced before production edits with `py -3.12 -m pytest backend/tests/test_bound_cards.py -q -k invalid_incoming_amendment --tb=short`: **3 failed, 94 deselected in 1.53s**. For current, stale and conflicting-key requests, the unchanged-row assertion showed `card_state_ciphertext: None` and `card_invalidated: scope_or_expiry_revoked`. The original RED cases are recorded in `card-state-test-output.txt`.

Fix: `_target` is now a non-destructive input validator raising `CardStateError`. Only `_current`, after authenticating the persisted state, converts an unavailable persisted target into `CardScopeRevoked`. Incoming amendment target binding moved into the runtime transaction after loading the authenticated current card; new request keys check pending ownership and exact generation/digest first. Existing request keys still compare canonical request fingerprints to preserve idempotent retries and reject conflicts. Invalid input cannot enter the lifecycle-erasure branch.

Final added coverage includes nonexistent and ambiguous incoming targets, each with current versions, stale versions and a conflicting key from a real previously successful amendment. Every denial asserts byte-for-byte unchanged approval row (ciphertext and keyed receipts included), unchanged ApprovalRequest and TaskRun, unchanged persisted event journal, no published event, successful reread of the original exact card, and no grant/invocation/continuation/extra run. A separate amendment test proves actual removal of the original persisted target still scrubs the card and grants nothing. Existing duplicate, amendment/approval, revocation and deletion cases remain covered.

Commands/results before final freeze:

```
py -3.12 -m pytest backend/tests/test_bound_cards.py -q -k 'invalid_incoming_amendment or real_scope_revocation or bounded_receipts or amend_and_exact or approved_retry' --tb=short
12 passed, 85 deselected in 1.68s

py -3.12 -m pytest backend/tests/test_bound_cards.py backend/tests/test_task_runtime.py backend/tests/test_task_runtime_rereview_round2.py backend/tests/test_task_runtime_deletion_authorization.py backend/tests/test_task_runtime_quarantine_isolation.py -q --tb=short
209 passed in 43.75s

py -3.12 -m compileall -q backend
exit 0
git diff --check
exit 0
```

Final frozen command: `py -3.12 -m pytest backend/tests -q`. Actual result: **1722 passed, 5 skipped, 10 subtests passed in 160.41s (0:02:40)**, exit 0. Source/tests were unchanged throughout that run. No frontend source or public contract changed, so the unchanged frontend suite was not repeated, as directed. This fix changes only `bound_cards.py`, its focused tests and the two evidence reports. Controller plan/brief/design/roadmap/ledger changes and user skills remain unstaged by the implementer.

Final SHA-256 fingerprints:

```
backend/task_runtime/bound_cards.py  74BEAFEA8B6681471C71A744EB4729C58CAF70C450EBECD1F98D6D245AD17042
backend/tests/test_bound_cards.py   B13FEC7E26048B16D20AEED4EB227EF85E6648D0426954E57926AEA72BAA5EDA
```

Self-review: the input validator has no lifecycle-erasure exception; only authenticated persisted target/scope revocation reaches invalidation. Version checks precede new-request target binding inside the same real transaction. Existing request canonicalization and idempotent retry semantics are preserved. I1 is implemented and verified; independent scoped re-review must confirm its disposition. No additional scope changes or unresolved implementation blockers were identified.
