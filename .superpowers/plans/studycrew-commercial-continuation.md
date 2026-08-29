# StudyCrew commercial continuation plan

## Global Constraints

- Continue from branch `feature/studycrew-foundation` and baseline commit `3a35d8b`.
- Product: `StudyCrew 学伴团`; Windows x64 controlled trial built to paid-product quality.
- Runtime: Electron 43.4.0 with Node 24, development Node 24, Python 3.12 PyInstaller sidecar, React 18.
- Single local Windows user, many isolated Agent groups; no accounts, activation, payment, multi-tenancy, or Obsidian integration.
- User data is local-first. Chat, difficult vision and optional cloud embeddings use BYOK only after DLP authorization.
- Group and task authority are backend-owned. Retrieved content and memories are untrusted and cannot grant tools or change policy.
- Preserve upstream MIT attribution; record Mem0 Apache-2.0 and model licenses.
- No plaintext secrets, PII, source bodies, memories, embeddings or OCR caches in logs, telemetry, diagnostics or durable indexes.
- Files remain UTF-8. Preserve user-owned untracked `skills/`; never modify or commit it.
- Each task requires focused tests and independent 0 Critical / 0 Important review before dependent work begins.

## Task 1: Close atomic Profile authorization (Task 4B)

- Add a generation-consistent `ProfileBundle` whose body, availability, contributors, locked state and generation token are read under one existing `root_lock` critical section.
- Make merged and project-specific Assets routes consume only lock-bound Profile availability; no unlocked general snapshot may authorize Profile fields.
- Make Profile POST read the current generation, authorize, compare the generation and mutate within one lock boundary. Stale authorization must fail closed and never overwrite a newer generation.
- Add deterministic barrier tests for availability, GET, POST, auto-compilation, deletion and concurrent generation swaps.
- Re-run Knowledge/Task Runtime backend partitions, Task 4A frontend set, full frontend and static gates.

## Task 2: Add Provider, Tool, MCP, PII and secret gateways

- Add `ModelCapabilityRegistry`, `ToolDescriptor`, `ToolInvocation`, `PermissionGrant`, `McpServerConfig`, `PiiFinding`, `DlpDecision` and `SecretRef` contracts.
- Protect master keys and provider credentials through Electron `safeStorage`; Python receives only purpose-bound short-lived credentials.
- Route built-in tools and Shell through Tool Gateway. Grants bind `group_id`, `task_id`, tool, executable, cwd, read/write, network and child-process capabilities.
- Require per-call approval for destructive actions, external writes/messages, privilege expansion and sensitive outbound data.
- Support MCP stdio and Streamable HTTP; prompts disabled by default; tools/resources inspected and granted before use.
- Permit provider failover only among pre-authorized candidates when privacy does not decrease and budget remains.
- Redact logs, telemetry and diagnostic packages.

## Task 3: Implement local-first encrypted RAG

- Parse PDF, DOCX, PPTX, XLSX, Markdown, TXT and HTML locally with structural locations.
- Ship no PaddlePaddle runtime. Download a pinned PP-OCRv6 Small ONNX capability pack on demand with license manifest, SHA-256 verification and atomic install/rollback/delete.
- Use BYOK multimodal vision only for formulas, charts, complex tables and low-confidence pages after DLP approval.
- Use local INT8 ONNX `multilingual-e5-small` by default; offer BGE-M3 as an optional quality pack; cloud embeddings require per-source authorization.
- Implement deterministic child/parent chunks, stable IDs, deduplication, incremental rebuild and delete propagation.
- Encrypt sources, OCR, chunks, embeddings and caches with AES-256-GCM; keep only opaque metadata plaintext.
- Build group/course-scoped in-memory BM25 and USearch indexes; retrieve BM25 40, vector 40 and graph 10, then RRF, optional rerank, MMR and 8-12 evidence packing.
- Verify permission, version, course isolation and exact citations; abstain for unsupported school facts.

## Task 4: Integrate governed Mem0 OSS memory

- Add `MemoryGateway`, `MemoryRecord`, `MemoryCandidate`, `MemoryScope`, `MemoryEvidence`, `MemoryTombstone`, `MemoryPromotion` and `MemoryEvaluationResult`.
- Pin a reviewed Mem0 OSS 2.x patch, disable telemetry and do not use Mem0 Cloud, Docker server, graph memory or default local Qdrant persistence.
- Keep Task Runtime working memory and structured action memory outside Mem0. Use Mem0 only for semantic and episodic extraction, deduplication and semantic recall.
- Store all durable memory in StudyCrew's encrypted `MemoryRepository`. Rebuild an ephemeral Mem0/USearch index from approved decrypted records with `infer=False` on startup.
- Direct, non-sensitive facts may be written after deterministic checks. Inferred preferences, weak points, grades and sensitive facts enter an isolated Candidate namespace and cannot be recalled until approved.
- Enforce group/course/sensitivity/status scope before and after every Mem0 query; never rely solely on Mem0 metadata filters.
- Version edits, enforce TTL, write tombstones, prevent relearning and cascade source/course/group deletion through records, candidates, indexes and caches.

## Task 5: Finish learning workflows and controlled evolution

- Complete courses, assessments, study plans/tasks, mastery, practice and review domain behavior.
- Keep Today as a minimal cross-group projection. Add exam planning, postpone/skip, Windows notifications, ICS, adaptive quizzes and spaced review.
- Provide a read-only SchoolGateway contract, deterministic mock adapter and sanitized fixtures; leave real integration behind documented API boundaries.
- Generate only reviewable prompt, routing, retrieval, learning-method and Agent-template candidates. Require golden-set, permission regression, shadow comparison and user approval.
- Never modify code, enable third-party skills, submit graded work, impersonate users or write to school systems.

## Task 6: Commercial-quality release closure

- Eliminate renderer TypeScript baseline diagnostics and backend `ACTION_CONTRACT` collection failures.
- Verify Electron/Node/Python alignment, PyInstaller sidecar, reproducible Windows NSIS, install/upgrade/migration/uninstall, crash recovery and encrypted backup.
- Make OCR/embedding packs independently downloadable, verifiable, removable and disk-usage visible.
- Prepare certificate injection and signed-update verification; unsigned builds remain controlled trial artifacts with public auto-update disabled.
- Add third-party notices, privacy documentation, threat model, permission matrix, PII flow, architecture diagrams and reproducible demo data.
- Build at least 120 sanitized/synthetic scenarios, fault-injection reports, demo script, README/architecture summary and truthful resume/interview material.
- Acceptance: Recall@10 >= 0.85, citation precision >= 0.95, grounded-answer pass >= 0.90, unauthorized high-risk blocking 100%, and no deleted content recoverable from any derivative store.
