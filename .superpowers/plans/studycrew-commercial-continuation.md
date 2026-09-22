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
- **PDF dependency decision (measured 2026-09-21; sizes and licences read from each wheel's own `METADATA`, not from a summary page).** The library was left open because it is a PyInstaller-size trade-off. It is also a **licence** trade-off, and that half is the one that would have been discovered too late:
  - `pymupdf` 1.28.2 — 18.91 MB — declares `Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License`. **Excluded on licence, not on size.** AGPL means a closed-source product must either buy an Artifex commercial licence or release itself under AGPL, and that is not a decision to make silently on the user's behalf.
  - `pypdf` 6.19.0 — 0.38 MB — `BSD-3-Clause`. Text extraction only, no page rendering, so the scanned-page path this same bullet requires would need a second library and a second pass over the PDF code.
  - **Chosen: `pypdfium2` 5.13.0 — 3.71 MB — `BSD-3-Clause, Apache-2.0, dependency licenses`.** One library covers both text extraction and page rendering; that is +3.71 MB on a 254.6 MiB installer (≈1.5%); and it ships its complete dependency licence set under `LICENSES/`, so Task 6's THIRD-PARTY notice can be written from the package instead of guessed at.
  - **Verified when it landed, not assumed** (`abb70ff`). PyInstaller 6.13 does collect the PDFium binary: a one-file exe built from `import pypdfium2` runs frozen and really parses a page (`frozen: True / pages: 2 / text: 'Page one'`). Measured increments on that probe: bare exe 7.68 MB → 11.48 MB with pypdfium2, i.e. **+3.80 MB ≈ 1.5%** of the 254.6 MiB installer, which matches the 3.71 MB wheel.
  - **The same measurement found 18 MB that would otherwise ship by accident.** Without exclusions the probe exe is 29.70 MB (+22.01 MB), because PyInstaller's static scan finds the *lazy* `import numpy` / `import PIL.Image` inside `pypdfium2/_lazy.py` and collects both — including a 38 MB uncompressed OpenBLAS DLL.
  - ⚠ **Corrected 2026-09-21 (vector-search round): numpy is no longer excludable, so most of that 18 MB is unavoidable.** Local vector search needs numpy — measured, and `usearch` does not remove the need (see the vector-search decision below). So the spec's exclusion list shrinks to `["PIL"]` and the honest figure for pypdfium2 in a build that also does vector search is not +3.80 MB but part of a combined **+18.71 MB ≈ 7.35%**. The earlier "excludes = ['numpy', 'PIL'] recovers all of it" was true when written and is now false; leaving it standing would have been a plan that quietly overstates what is recoverable.
- **Vector search decision (measured 2026-09-21, same method as the other two dependencies: wheel data from each package's own `METADATA`, packaged delta from frozen exes).** `usearch` 2.26.2 is a 0.33 MB wheel under Apache-2.0; `faiss-cpu` 1.15.1 is a 15.54 MB wheel under MIT. **This plan asked for USearch and the implementation does not use it.** The reason is a measurement, not a preference:
  - `usearch.index.Index.add` asserts `isinstance(vectors, np.ndarray)` and importing the package pulls numpy in. So **numpy is a hard prerequisite either way** — frozen delta measured at **+14.92 MB** on its own — and usearch's 0.33 MB buys only *approximation* on top of it.
  - Exact cosine scan has **no recall loss**, and the acceptance target is Recall@10 ≥ 0.85. Approximation would spend recall to save time that this product does not need to save: at personal-knowledge-base scale (thousands to tens of thousands of chunks) an exact scan answers in tens of milliseconds, while one LLM call is measured in seconds.
  - The contract is a plain `search(vector, limit) -> ((chunk_id, score), ...)`, so adding an ANN backend later is one class plus one dependency, with no caller changes. That is the trade being made: leave the door open rather than walk through it now.
- Build group/course-scoped in-memory BM25 and vector indexes; retrieve BM25 40, vector 40 and graph 10, then RRF, optional rerank, MMR and 8-12 evidence packing.
- Consequence of that decision, recorded here so it is a known trade rather than a later discovery:
  - a user with no BYOK provider configured **cannot** turn a scanned page into text at all, where the local OCR path would have managed it offline;
  - every scanned page leaves the machine (under DLP authorization) where the local path would have kept it local. Text-layer documents are unaffected and remain fully local.
- Embeddings are **cloud-only** (decision 2026-09-21): a Qwen text-embedding model on the `alibaba` provider, through the Provider Gateway. There is no local ONNX embedding pack and no BGE-M3 pack, so **no capability pack remains in the product at all** and Task 6's pack requirement disappears with it.
- Consequences of that decision, recorded rather than discovered later — they are larger than the OCR decision, because embeddings touch **every** document rather than only scans:
  - every indexed passage leaves the machine under DLP authorization, including ordinary text documents and text-layer PDFs that were otherwise fully local;
  - retrieval itself becomes a network call: queries must be embedded too, so there is a per-query cost and an online dependency, and offline use loses retrieval entirely (it keeps parsing and chat);
  - the local index and cache still stay AES-256-GCM encrypted and still must be rebuilt if the embedding model or its dimension changes, so the model id and dimension are part of the index's identity.
- The gateway has **no embedding path yet**: `ProviderGatewayClient` exposes only `chat` and `chat_stream`. Cloud embeddings therefore require a third authorized call type — DLP on the outbound text, budget reservation, `provider_egress` audit — not merely a different model name. The `alibaba` capability entry already declares `embedding` (Task 2's I-R1 left that slot on purpose) and its endpoints are registered, so the authorization half is in place; the transport half is not.
- The pricing table now carries `text-embedding-v4` for `alibaba`: **CNY 0.5 per 1M input tokens**, with the source URL recorded in the table (Batch File 0.25; Singapore/International 0.514 — the mainland rate is the one listed). It is input-only, so `output_cost_per_1M` is `0.0` rather than `None`; writing `None` would make `ModelPricing.known` report the model as unpriced and the cost dimension would silently fall back to the provider estimate. `cache_hit` is `None` because the provider states there is no context-cache tier. **Only v4 is listed** — v3's price page was not verified, and an unverified row is a silent wrong number where a missing one is a visible gap. Supported dimensions are 64–2048 with 1024 as the default, so the model id **and the dimension** belong in the index's identity: changing either means rebuilding the index.
- Implement deterministic child/parent chunks, stable IDs, deduplication, incremental rebuild and delete propagation.
- Encrypt sources, vision-extracted text, chunks, embeddings and caches with AES-256-GCM; keep only opaque metadata plaintext.
  - **AES library decision (measured 2026-09-21: wheel data from each package's own `METADATA`, packaged delta from two frozen exes rather than from the wheel).** `cryptography` 50.0.1 is a 3.66 MB wheel under `Apache-2.0 OR BSD-3-Clause`; `pycryptodome` 3.23.0 is a 1.72 MB wheel under `BSD, Public Domain`. **`cryptography` is chosen, and not on size** — AES-GCM is the one place in this task where a subtle mistake (nonce reuse) costs confidentiality *and* integrity at once, so the mainstream audited implementation with an explicit `AESGCM(key).encrypt(nonce, data, aad)` surface is worth 1.9 MB over the smaller one. Frozen delta measured the same way as pypdfium2's: 7.68 MB bare exe → 11.06 MB, i.e. **+3.37 MB ≈ 1.3%** of the 254.6 MiB installer, and the frozen exe really round-trips and really rejects a ciphertext whose AAD does not match (`InvalidTag`).
  - **AAD carries the chunk id and the field name**, so a ciphertext moved from one row — or from one column of the same row — cannot be opened. That is the difference between "encrypt each field" and "encrypt each row": without the binding, anyone with write access to the database can swap two values and both still decrypt cleanly.
  - The master key is protected by **Windows DPAPI** (`CryptProtectData` through `ctypes`, no new dependency): the key file can be copied but only decrypts for the same user on the same machine. That matches the actual threat — a data directory inside a backup, a shared PC, a cloud-synced folder — whereas storing the key beside the ciphertext in plaintext would make the whole exercise decorative.
  - Plaintext stays limited to what the index needs in order to work without decrypting everything: `document_id`, `chunk_id`, `content_hash`, `location`, `kind`, `level`, sizes and timestamps. `location` and `content_hash` leak structure (how many pages a document has) and equality (two rows hold the same text), but no content — that is the trade "opaque metadata" asks for, stated rather than assumed.
  - **Combined dependency growth for Task 3, measured rather than added up**: an exe carrying **all three** dependencies (numpy, pypdfium2, cryptography) is 29.54 MB against a bare 7.68 MB — **+21.85 MB ≈ 8.58%** of the 254.6 MiB installer — and all three really work in it (page rendering returns a 792×612×3 array, AES-GCM round-trips, and the earlier two-dependency probe also extracted PDF text and ranked vectors correctly). Measured without any application code attached, so the figure means "what Task 3's dependencies cost" rather than "what one probe happened to import".
  - ⚠ **An earlier version of this entry quoted +18.51 MB ≈ 7.27% and was wrong by omission**: that measurement covered only numpy + pypdfium2. Cryptography belongs to the same task, and its +3.37 MB (plus the libcrypto/libssl DLLs it drags in) was being left out of a figure labelled "Task 3". The separate deltas (14.92 + 3.79 + 3.37 = 22.08) again came out slightly above the measured combination, consistent with the two-dependency case.
  - **Measuring through the real module is not the same measurement.** A probe that imports `backend.knowe_rag.rendering` freezes at 37.15 MB (+29.46 MB) because the package `__init__` pulls in `store` → `crypto` → cryptography, plus sqlite3 and the rest of the backend. That number is not wrong, it just answers a different question — and it is the reason the figure above is measured on a bare probe. Recorded because a plan that silently mixes the two would overstate the cost of rendering by 11 MB.
  - The `excludes` question is settled: `PIL` out, numpy **in**.
- Build group/course-scoped in-memory BM25 and vector indexes; retrieve BM25 40, vector 40 and graph 10, then RRF, optional rerank, MMR and 8-12 evidence packing. (The BM25 + RRF/MMR/packing half landed at `a5cdc78`; the vector index at the commit following this decision. Graph recall — the 10 — is still to come.)
- Verify permission, version, course isolation and exact citations; abstain for unsupported school facts.

## Task 4: Integrate governed Mem0 OSS memory

- Add `MemoryGateway`, `MemoryRecord`, `MemoryCandidate`, `MemoryScope`, `MemoryEvidence`, `MemoryTombstone`, `MemoryPromotion` and `MemoryEvaluationResult`.
- Pin a reviewed Mem0 OSS 2.x patch, disable telemetry and do not use Mem0 Cloud, Docker server, graph memory or default local Qdrant persistence.
  - **Dependency measurement (2026-09-21, same method as the other three: the wheel read from its own `METADATA`, the tree measured by downloading it).** `mem0ai` 2.0.20 is a 0.33 MB pure-Python wheel under Apache-2.0 — the wheel size says nothing about the cost. Its **eight core dependencies** are `httpx>=0.28`, `openai>=1.90`, **`posthog>=7.14`**, `protobuf>=5.29.6`, `pydantic>=2.7.3`, `pytz`, **`qdrant-client>=1.12`**, `sqlalchemy>=2.0.31`. The resolved tree is **36 wheels / 34.23 MB**, led by `numpy` 11.99 (already ours), `pywin32` 6.59, `grpcio` 5.01, `sqlalchemy` 2.07, `openai` 1.96, `pydantic_core` 1.95 and `posthog` 0.75. Net new weight is roughly 22 MB on top of what Task 3 already ships.
  - ⚠ **Three things in that list need deciding before any code is written, not after.**
    1. **`posthog` is a core dependency, not an extra.** This plan says "disable telemetry", and the plumbing to disable it exists — but the analytics SDK is installed by default, so "telemetry is off" becomes a claim that depends on a configuration flag being right rather than on the SDK being absent. For a product whose whole premise is that a student's material does not leave the machine, that is a different kind of guarantee, and it deserves to be a deliberate choice rather than an inherited default.
    2. **`httpx>=0.28` would upgrade the Gateway's transport.** Task 3's provider egress runs on `httpx` (declared `>=0.27`), and it is the layer that carries every authorized request. Pinning Mem0 means pinning `httpx` too, and that interaction needs to be tested rather than assumed.
    3. **`qdrant-client` and `grpcio` arrive even though this plan forbids Qdrant persistence and graph memory.** They would be dead weight carried for features that are switched off — and dead weight in a frozen build is not free.
  - **The honest question, recorded rather than acted on:** this bullet already strips Mem0 down to "semantic and episodic extraction, deduplication and semantic recall" with `infer=False`, no cloud, no graph, no persistence. What remains is an extraction prompt plus JSON parsing — and this project already owns the other two thirds (an exact vector index for recall, content-hash dedup). Whether 22 MB, an analytics SDK and a transport upgrade are worth that remainder is a **product decision for the user**, not something to settle silently in a commit. It is written here so the decision is made with the measurement in hand.
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
