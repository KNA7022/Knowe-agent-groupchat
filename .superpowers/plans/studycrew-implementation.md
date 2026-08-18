# StudyCrew implementation plan

## Global Constraints

- Preserve upstream MIT attribution and history; do not claim official school affiliation.
- Product identity: `StudyCrew 学伴团`, npm name `studycrew-desktop`, Windows appId `io.studycrew.desktop`.
- Runtime baseline: system Node.js 24 for development, Electron 43.x with embedded Node 24, Python 3.12 sidecar, React 18 retained.
- Windows x64 is the only release target for v1. Cross-platform path abstractions remain, but macOS/Linux release work is out of scope.
- Single local Windows user, many local Agent groups, no multi-tenant account service.
- Files are UTF-8. Preserve existing encodings. Do not use sed/awk for Chinese text.
- Existing user changes must be preserved. No destructive git resets or broad deletes.
- Existing Knowe data must be backed up before migration and remain recoverable.
- Every group has Coordinator and Verifier. Optional knowledge/planning/coach roles and constrained expert templates may vary per group.
- Group data is isolated by default. Cross-group access requires an explicit source link, copy, or forward. Permissions bind both `group_id` and `task_id`.
- Per group: one primary task and up to two low-risk side tasks. Global defaults: two running primary tasks and two running side tasks.
- Course/school facts require evidence. Retrieved content is untrusted data and cannot grant authority or alter system policy.
- Sensitive sources do not leave the device without purpose-specific authorization. No plaintext secrets or PII in logs, telemetry, exports, or fixtures.
- School integration is read-only, unofficial/personal, and must not bypass CAPTCHA, rate limits, or access controls.
- No automatic code self-modification, assignment submission, impersonation, or school-system writes.
- New behavior must have focused automated tests. High-risk side effects are idempotent and never replayed automatically after recovery.

## Task 1: Establish StudyCrew brand, runtime baseline, and green build gates

- Rename package/product/app identifiers and visible brand strings to StudyCrew while preserving upstream attribution in LICENSE/README.
- Upgrade and pin Electron 43.x, align Node engine/types/scripts to Node 24, and make preflight require Python 3.12.
- Keep React 18. Update only build dependencies needed for Electron 43 compatibility.
- Update Windows NSIS names, appId, artifact names, data-directory migration hooks, icons/metadata references, and update provider coordinates without enabling unsigned auto-update.
- Add CI or local verification commands for Node version, Python version, frontend typecheck/lint/tests, backend tests, Electron build smoke test.
- Fix only baseline failures required for these gates; record unrelated pre-existing failures.

## Task 2: Introduce versioned Group domain and migrate legacy projects

- Add canonical backend and frontend contracts for Group, GroupAgent, GroupCourseBinding, GroupSourceLink, GroupLifecycle, GroupRuntimeSummary, and expert template configuration.
- Accept legacy `project_id` at transport boundaries but canonicalize internally to `group_id`; emit both only during the migration compatibility window.
- Persist group template, lifecycle, course bindings, roster, budgets, model/tool overrides, and timestamps using opaque IDs rather than filenames derived directly from user input.
- Provide create, rename, duplicate, archive, restore, delete-preview, confirmed-delete, course binding, roster update, and source-link operations.
- Enforce Coordinator and Verifier. User experts are generated from constrained fields and cannot override platform policy.
- Add a one-time, backup-first migration: each legacy project becomes a custom group, Verifier is inserted, old workers become matched templates or restricted Legacy Experts, and failures produce a report without modifying the legacy source.
- Implement shared-source reference counting and deletion receipts.

## Task 3: Add authoritative Task Runtime and global scheduler

- Add versioned TaskRun, AgentRun, RunEvent, ApprovalRequest, PermissionGrant, ToolInvocation, Checkpoint, BudgetUsage, and EvidenceRef contracts.
- Implement backend-authoritative event sequencing, snapshots, replay after reconnect, idempotency keys, task/agent state machines, checkpoints, and public status summaries without chain-of-thought.
- Implement per-group and global concurrency limits, fair queuing, pause/cancel/resume, primary versus side tasks, and index-job lower priority.
- Route new messages visibly as supplement, side task, or queued task; uncertain or risky routing waits for user confirmation.
- Add multidimensional budgets and failure taxonomy. Reversible failures may retry within budget; high-risk calls never replay automatically.

## Task 4: Build group management UI and Agent status bar

- Preserve the current visual system and layout; make only focused hierarchy changes.
- Add group creation wizard with course/exam/general/custom templates, course/source binding, constrained Agent roster, budget and tool/model overrides.
- Show lifecycle and runtime summaries in the group list; support archive/restore/delete-impact flows.
- Add the status bar below the chat header. Primary task or exception is visually dominant, idle Agents collapse, and side tasks are labeled.
- Add task controls and a details drawer for public event timeline, approvals, tools, citations, budgets, recovery, and indexing stages.
- Add Today, Knowledge, Review, and Privacy navigation shells while keeping chat and preview flows intact.
- Meet WCAG 2.1 AA, keyboard navigation, reduced motion, and non-color-only status communication.

## Task 5: Centralize provider, tool, permission, MCP, and privacy gateways

- Add capability-based Provider Registry for chat, reasoning, embedding, vision OCR, and rerank with privacy/cost/context metadata and pre-authorized failover.
- Route all built-in tools through Tool Gateway with schemas, risk, data class, group/task scope, filesystem scope, network needs, idempotency, and approval policy.
- Enforce read-only imported originals, writable task workspaces, executable+cwd+capability Shell grants, default-deny network, domain allowlists, and per-call destructive/external actions.
- Add governed MCP registry for stdio and Streamable HTTP. Tools/resources require inspection and grants; prompts remain disabled until reviewed.
- Add OS-protected secret storage and ephemeral credential handoff to Python.
- Add PII classification, outbound DLP/pseudonymization, structured redacted audit logs, encrypted backup/export warnings, and cascading deletion semantics.

## Task 6: Implement encrypted, structure-aware RAG vertical slice

- Add managed source import and watched folders for PDF, DOCX, PPTX, XLSX, Markdown, TXT, HTML, and school snapshots.
- Parse locally with structural location metadata. Use vision OCR only for scans/images/formulas/charts after cost and privacy gates.
- Implement stable source versions, deterministic chunk IDs, child chunks of 350-650 tokens with hard max 900 and max overlap 80, parent chunks of 1200-2000, table/slide/code-aware boundaries, deduplication, incremental rebuild, and deletion propagation.
- Persist originals, OCR, chunks, PII and embeddings encrypted with AES-256-GCM; protect the master key through Electron safeStorage. Keep only opaque metadata plaintext.
- Build group/course-scoped in-memory Chinese BM25 and USearch indexes with LRU eviction.
- Implement scope routing, up to three query rewrites, filtered BM25/vector/graph retrieval, RRF, optional rerank, MMR, parent expansion, evidence packing, citation verification, abstention, and offline lexical degradation.
- Add exact citations to page/slide/section/sheet-cell and integrate with the existing previewer.
- Add indexing queue UI, Knowledge Center source management, retrieval summaries, and expandable diagnostics without chain-of-thought.

## Task 7: Implement university learning domain and mock SchoolGateway

- Add Course, ClassSession, Assessment, GradeSnapshot, RankSnapshot, StudyGoal, StudyPlan, StudyTask, MasteryConcept, PracticeAttempt, ReviewEvent, KnowledgeCollection, and KnowledgeSource schemas.
- Add Today dashboard as a minimal cross-group projection, not a context merger.
- Implement exam study plan, task board, completion/postpone/skip feedback, Windows notifications, ICS export, Socratic coaching, adaptive quizzes, and spaced-review scheduling.
- Add tiered academic-integrity classification and prevent automatic graded-work submission or impersonation.
- Implement read-only SchoolGateway contract, deterministic mock adapter, sanitized fixtures, snapshot caching, startup/scheduled/manual sync, stale indicators, backoff and circuit breaker.
- Leave the real unofficial adapter behind a documented integration boundary until API documentation and permitted test responses are provided.

## Task 8: Complete layered memory, controlled evolution, and recovery

- Add task working memory, group/course episodic memory, semantic memory, action memory, provenance, confidence, sensitivity, TTL, versioning, tombstones, and explicit global promotion.
- Add candidate capture and Review Center workflows for inferred preferences, weak points, prompt patches, routing rules, retrieval changes, learning methods, expert templates, and skill packs.
- Require offline evaluation, permission regression, shadow comparison, user approval, versioned activation, and rollback. Never modify code automatically.
- Add durable recovery and fault injection for provider/tool/index crashes, with checkpoint resume and non-replay of high-risk side effects.

## Task 9: Evaluation, packaging, migration hardening, and portfolio evidence

- Build at least 120 sanitized/synthetic university scenarios and RAG golden queries.
- Enforce Recall@10 >= 0.85, citation precision >= 0.95, grounded-answer pass rate >= 0.90, and 100% unauthorized high-risk blocking on the maintained evaluation set.
- Add cross-group isolation, reference counting, PII deletion propagation, malicious MCP/prompt injection, Shell/network escape, reconnect, scheduler fairness, migration rollback, and packaged-sidecar tests.
- Produce reproducible Windows NSIS trial artifacts with checksums and build manifest. Enable signed auto-update only when a signing certificate exists.
- Produce sanitized public README, English architecture summary, ADRs, threat model, PII flow, permission matrix, architecture/RAG/scheduler diagrams, reproducible demo data, evaluation report, short demo script, resume bullets, and interview notes. Never invent pilot or performance numbers.
