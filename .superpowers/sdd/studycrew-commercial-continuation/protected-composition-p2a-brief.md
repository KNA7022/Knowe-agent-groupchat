# P2a — explicit protected history access and selected repository views

## Scope and controlling decision

This is the concrete prerequisite split from P2; P1 is accepted at 4f7ee30. Worktree D:/agentwork/StudyCrew-worktree, UTF-8/apply_patch, preserve skills/ and unrelated work. No public producer, Engine/Worker/Hub/Client wiring, provider calls, new dependencies or child agents. Read the P1 report and latest replacement decision in card-authority-design.md. Original P2 real-chain acceptance remains owed by P2b.

The earlier P2 requirement to prove every intervening group change was only rename/archive/restore is superseded. It was a design-added restriction, not a user requirement. Separate execution authorization from the student's explicit access to existing source-free completed history. Do NOT implement signed Group transition chains, monotonic cross-file heads, Group signer injection or a new two-store commit protocol.

## Existing-record capabilities

1. Introduce a narrowly named backend-only existing-record read capability, distinct from live ProtectedAccess. Require authenticated original source-free/course-free P1 lineage, canonical same-group owner/task/record binding, current group existence and current user's access. Only already-succeeded/partial owner history is eligible after current Group name/role/course changes or archive/restore. Original binding/ciphertext scope/AAD remains immutable; never restamp old output to current scope. Current Group changes do not authorize reading another group's or source-derived records. Pending deletion, deleted/missing group, explicit runtime/lineage revocation, failed/cancelled owner, tampering, unknown lineage format and unsupported original source/course provenance deny.

2. An independent narrow same-scope paused-history read may support restart-paused owners; require original current scope/target and persisted binding/card version as appropriate. It never resumes the card or task. Completed-history relaxation must not leak into this nonterminal path.

3. Both capabilities are for explicit backend selection only. They must reject every write route, including codec claim/seal/bind_worker_run, direct _binding(write=True), envelope creation, journal/progress/completion mutation, retry ACK, outbox claim/fail, and authority allocation. Use explicit capability mode checks at central writer seams plus actual repository tests; a guard wrapper alone is insufficient. Identity access may return only needed authenticated private metadata, never execution grants. Existing live ProtectedAccess retains current strict lifecycle/scope behavior.

4. Reads authenticate the independently selected existing record and its original claims. No new record claims, receipts, mappings or lifecycle evidence may be created by history lookup. No fallback/backfill, no plaintext index/cache, no model/raw reasoning public projection in this slice. Returned private domain objects remain transient; P2b owes final public answer-only projection.

## Selected-store filtering

Add bounded concrete filters to reached list/recovery queries BEFORE any decrypt: completion list_active, active_attempts, pending_outbox, reconcile_terminal_runs, list_decisions, orphan_summary, task-run list_by_state and wrapper. Bind any completion selector to its independently verified task relation. Selected protected views contain only their authenticated lineage; direct keyed foreign record requests must still fail explicitly, not return an authorization-shaped empty success. Corrupt selected rows remain failures.

Define explicit legacy-view exclusion selection for later P2b composition so legacy decode does not consume protected records. Reuse authenticated registry opaque IDs or an explicitly verified record classification, not caller-controlled model metadata. Keep query filtering narrowly scoped; no generic repository-router framework, mutable shared capability or ambient ContextVar. Prove two protected lineages and a legacy record coexist in one physical DB; each view sees only intended records and wrong selections fail closed. Do not claim actual Engine recovery is wired yet.

## Tests, report and acceptance

Real GroupRepository/TaskRuntimeService/cards/key/SQLite/files; no mocks of authority or persistence. Reproduce currently unavailable manual completed-history behavior, then prove same-group completed source-free history after rename, archive→restore, role removal and course/source binding changes while live read/write remains denied by its old scope. Explicit history access to an archived group is read-only. Original source/course-derived registration remains unsupported; no fabricated original-scope proof. Reject other-group substitution, pending deletion/missing/deleted, explicit revoke, failed/cancelled, corrupt mapping/key/AAD and unrecognized history mode. Same-scope paused read succeeds only as specified, changed-scope paused read denies. Close/reopen preserves these decisions with zero provider/tool/task starts.

For each read-only capability snapshot DB/claims/row counts and file bytes before/after successful reads and rejected mutation attempts. Check actual public repository mutation routes, not only a fake codec. Selected mixed-store tests cover listings, pending outbox, verified completion filtering, history/recovery queries, corrupt selected rows, foreign direct reads and preserved legacy behavior. Marker scans continue proving protected text does not become durable plaintext. Existing full P1 tests must remain green.

Focused iteration, then one frozen complete backend run with Python3.12, compileall and diff-check. No added skips/xfails/exclusions. No TypeScript expected; if truly needed ask before expanding. Write protected-composition-p2a-report.md plus raw actual regression output; document exact private APIs and original-scope proof, no execution/product-path claims. Commit scoped source/tests/report/evidence only. Independent 0 Critical / 0 Important required before P2b.
