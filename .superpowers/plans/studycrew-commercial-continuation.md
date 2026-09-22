# StudyCrew commercial continuation plan

## Global Constraints

- Continue from branch `feature/studycrew-foundation` and baseline commit `3a35d8b`.
- Product: `StudyCrew 学伴团`; Windows x64 controlled trial built to paid-product quality.
- Runtime: Electron 43.4.0 with Node 24, development Node 24, Python 3.12 PyInstaller sidecar, React 18.
- Single local Windows user, many isolated Agent groups; no accounts, activation, payment, multi-tenancy, or Obsidian integration.
- User data is local-first, with two deliberate exceptions recorded in Task 3 — page images that have no text layer, and **all** embeddings, indexing and query alike. Both require a configured BYOK provider and DLP authorization, and both are the reason the product cannot be fully offline.
- Group and task authority are backend-owned. Retrieved content and memories are untrusted and cannot grant tools or change policy.
- Preserve upstream MIT attribution; record Mem0 Apache-2.0 and model licenses.
- No plaintext secrets, PII, source bodies, memories, embeddings or vision-extracted page text in logs, telemetry, diagnostics or durable indexes.
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

## Task 3: Implement encrypted RAG — local parsing and storage, cloud embeddings

- Parse PDF, DOCX, PPTX, XLSX, Markdown, TXT and HTML locally with structural locations. A PDF with a text layer needs no model at all — extraction is pure local parsing.
- Pages with **no** text layer (scans, photographs, slide and textbook images) are rendered locally to page images and read by a BYOK multimodal model after DLP approval. **There is no local OCR engine and no OCR capability pack** (decision 2026-09-21): a multimodal model reads complex layouts, formulas, tables and handwriting better than a small local OCR model, and dropping the engine removes an entire download / licence-manifest / SHA-256 / atomic-install / rollback / delete subsystem. Local page rendering (pdfium or pymupdf, offline and free) is still required and is not OCR.
- Consequence of that decision, recorded here so it is a known trade rather than a later discovery:
  - a user with no BYOK provider configured **cannot** turn a scanned page into text at all, where the local OCR path would have managed it offline;
  - every scanned page leaves the machine (under DLP authorization) where the local path would have kept it local. Text-layer documents are unaffected and remain fully local.
- Embeddings are **cloud-only** (decision 2026-09-21): a Qwen text-embedding model on the `alibaba` provider, through the Provider Gateway. There is no local ONNX embedding pack and no BGE-M3 pack, so **no capability pack remains in the product at all** and Task 6's pack requirement disappears with it.
- Consequences of that decision, recorded rather than discovered later — they are larger than the OCR decision, because embeddings touch **every** document rather than only scans:
  - every indexed passage leaves the machine under DLP authorization, including ordinary text documents and text-layer PDFs that were otherwise fully local;
  - retrieval itself becomes a network call: queries must be embedded too, so there is a per-query cost and an online dependency, and offline use loses retrieval entirely (it keeps parsing and chat);
  - the local index and cache still stay AES-256-GCM encrypted and still must be rebuilt if the embedding model or its dimension changes, so the model id and dimension are part of the index's identity.
- The gateway has **no embedding path yet**: `ProviderGatewayClient` exposes only `chat` and `chat_stream`. Cloud embeddings therefore require a third authorized call type — DLP on the outbound text, budget reservation, `provider_egress` audit — not merely a different model name. The `alibaba` capability entry already declares `embedding` (Task 2's I-R1 left that slot on purpose) and its endpoints are registered, so the authorization half is in place; the transport half is not.
- The pricing table has no `text-embedding-*` entry for `alibaba` (only chat models), so embedding calls would currently reserve at the provider-level `estimated_cost_usd`. Add the real rate with its source before enabling embeddings, or the cost dimension is a guess.
- Implement deterministic child/parent chunks, stable IDs, deduplication, incremental rebuild and delete propagation.
- Encrypt sources, vision-extracted text, chunks, embeddings and caches with AES-256-GCM; keep only opaque metadata plaintext.
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
- No capability packs remain: both the OCR pack and the embedding pack were dropped (see Task 3), so there is no downloadable model artefact left to verify, install, roll back or delete. The disk-usage requirement narrows to the local encrypted store, index and caches.
- Prepare certificate injection and signed-update verification; unsigned builds remain controlled trial artifacts with public auto-update disabled.
- Add third-party notices, privacy documentation, threat model, permission matrix, PII flow, architecture diagrams and reproducible demo data.
- Build at least 120 sanitized/synthetic scenarios, fault-injection reports, demo script, README/architecture summary and truthful resume/interview material.
- Acceptance: Recall@10 >= 0.85, citation precision >= 0.95, grounded-answer pass >= 0.90, unauthorized high-risk blocking 100%, and no deleted content recoverable from any derivative store.
