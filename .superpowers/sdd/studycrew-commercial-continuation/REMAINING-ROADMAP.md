# Remaining roadmap — StudyCrew commercial continuation

## Current checkpoint — 2026-10-04, `49f9c89` (protected event delivery independently accepted)

P2b1 authoritative encrypted event delivery is accepted after two scoped fix rounds at0C/0I. Tests cover derived-cache loss, real authenticated WS/HTTP projection, ordered replay, lifecycle fences and actual recovery-snapshot/overflow protocol. Final backend1887passed/5existingSKIP/10subtests; focused117passed; controller Node24 typecheck/lint,588frontendtests and33foundationtests passed. P2b2 real Worker/Gateway/completion/notification composition remains pending. See the top checkpoint in progress.md for live evidence; delivery acceptance is not whole Unit P or commercial acceptance.

OCR and Mem0 actual SDK preflights are now complete alongside E5. A combined59-wheel dependency candidate resolves and installs atprotobuf6.33.6; real USearch native mechanics pass, but combined-process shutdown emits an unexplained diagnostic. These are isolated runtime experiments, not production integrations or measured RAG acceptance. Mem0 insert failures can still return ADD, and infer=False does not deduplicate; StudyCrew authority and explicit postvalidation are required. The new Group chat privacy map confirms pre-run user/coordinator plaintext sinks that must be fixed before Unit D product dispatch, without fabricating a Worker lineage before a real task exists. Current packaging still excludes NumPy; frozen runtime/dependency/license and installation gates remain open.

P2a explicit private read-only history and selected-store queries are accepted at0C/0I. Frozen backend1838 passed/5existing skipped/10subtests, focused116 passed. Next P2b must complete actual Worker/Gateway/Hub/restart composition; no public protected-history/producer feature is claimed yet. Local model design and E5 isolated real-runtime preflight are complete; E5 ranking concerns remain, not a passing RAG metric. OCR isolated preflight is ongoing.

P1 protected storage is accepted after two fix rounds, 0 Critical / 0 Important. Current frozen backend: 1799 passed, 5 existing skipped, 10 subtests; P1 focused: 77 passed. Next is P2 real Worker/event/restart composition, then Unit D real producer/approval/dispatch. Storage acceptance alone is not product-path or commercial acceptance. Earlier row-specific counts below are historical checkpoints.

This section supersedes the old `e1d2bd9` snapshot below. Do not resume from its “not started” claims.

| Area | Verified current position | Remaining acceptance work |
| --- | --- | --- |
| Profile authorization | Task 1 accepted | Whole-release regression |
| Provider / Tool / MCP / PII / secrets | Recorded Task 2 composed acceptance with limitations | Resolve release-critical limitations and actual product configuration flows |
| Approval/dispatch Harness | Encrypted version-bound card foundation accepted after one fix round, 0 Critical / 0 Important; frozen full backend 1722 passed / 5 unchanged skips / 10 subtests | Protected Worker/event persistence (Unit P), real producer/approval/dispatch (Unit D), bounded feedback and RAG version fence |
| RAG | Bottom-layer fixes verified; encrypted bytes import, single-group/course BM25, citation text and deletion/restart connected to real server/UI. Fixes in `a245c9c` independently accepted at 0 Critical / 0 Important | OCR/semantic capabilities, complete answer/Verifier loop, indexing UX, quantitative product evaluation |
| Memory | Contracts and encrypted repository, 63 tests passed this turn | Actual pinned Mem0 adapter, ephemeral indexes, candidate/approval/edit/delete/UI integration |
| Learning | Existing Today is runtime summary, not a completed student workflow | Courses/exams/plans/practice/review, read-only school mock adapter, controlled evolution |
| Release | TypeScript and lint green; 588 frontend tests; 33 foundation tests and runtime contract green; frozen complete backend 1620 passed / 5 unchanged Windows skips / 10 subtests, independent baseline review 0 Critical / 0 Important | Real card/task ownership and supported dispatch; production Windows sandbox/descendant/security checks; current sidecar/NSIS rebuild and smoke, dependency packaging, privacy/license docs, evaluation and evidence pack |

Whole-backend pre-integration run had 57 failures and 16 setup errors; the frozen final run at `687dc6d` has zero failures/errors. Its test-intent changes and two real production fixes passed independent review after one scoped fix round. The older “54 TypeScript diagnostics” and “default test collection executes zero tests” statements are obsolete. Existing installer artifacts do not validate current RAG or memory code. The spec still excludes NumPy and must be corrected before claiming a current packaged vector implementation.

The explicit user-approved Mem0-version plan controls: local ONNX OCR/default embeddings, production USearch, separately verified downloadable model packs, and optional per-source-authorized cloud processing. The on-disk plan now marks conflicting cloud-only passages historical. No silent upload or cloud fallback is authorized. Mem0 OSS reuse is explicitly authorized; dependency size alone is not a reason to omit it. Pinned-wheel design is complete, actual integration remains pending.

The current controller and independent review evidence live in `progress.md`, `task-3-integration-report.md`, and `reviews/`. No commercial-release or real-student-trial claim is made.

### Explicit release carry-forwards

Do not restart the old Task 2 review loop or interpret its composed acceptance as a waiver of these release gates. `TASK2-ROUND2-ACCEPTANCE.md` records per-provider wildcard model policy/flat cost reservation, absent closed Electron slot catalog and eventual credential revocation. Before commercial-quality acceptance, implement and verify model-specific bounded pricing/privacy authorization and preauthorized fallback, current-key revocation/slot boundaries, and actual supported Windows Shell/network/descendant isolation. Unknown/unverified policy must not silently become permissive. Existing inert fixture success does not establish production sandbox availability. Also close deferred noisy-gate M1 and current frozen sidecar/NSIS dependency coverage; the old installer and dependency-only probe EXEs are not evidence for the new code.

## Historical snapshot (superseded)

Status of the six tasks in `.superpowers/plans/studycrew-commercial-continuation.md`, measured
at `e1d2bd9` rather than recalled. The plan itself is unchanged; this file only records **where
the project actually is** and **what order the rest has to happen in**.

## Where things stand

| task | state | evidence |
|---|---|---|
| **1** Atomic Profile authorization | **done** | `task-1-review.md`: independent review 0 Critical / 0 Important / 0 Minor |
| **2** Provider / Tool / MCP / PII / secret gateways | **done** | `TASK2-ROUND2-ACCEPTANCE.md`: composed verdict PASS, 9 review rounds, 0 Critical / 0 Important |
| **3** Encrypted RAG — local parsing and storage, cloud embeddings | **not started** | measured against the **revised** spec: `git grep` over `backend src electron` returns **0 files** for every needed piece — `pdfplumber`, `pymupdf`, `python-docx`, `openpyxl`, `python-pptx` (parsers), `AESGCM`, `cryptography` (encryption), `usearch` (vector index), `text-embedding` and `def embed` (the gateway call path). `embeddings` matches one file, and only as a word in a test about secret leases |
| **4** Governed Mem0 memory | **not started** | `git grep -li mem0 -- backend src electron docs` → **0 files** |
| **5** Learning workflows | **not started** | **0 files** for `assessment`, `study_plan`, `mastery`, `spaced_review`, `SchoolGateway`. (`ICS` and `exam` match 37 and 25 files only as substrings of `statistics` / `example` — a false positive worth knowing about before someone reads those counts as progress) |
| **6** Commercial release closure | **partly done** | packaging chain, `release/StudyCrew-Setup-1.0.36.exe`, install/uninstall verified in Phase 0; but the four `ACTION_CONTRACT` collection errors remain, `npm run typecheck` carries 54 diagnostics, and there are no `THIRD-PARTY` / threat-model artefacts |

Base size for comparison: backend non-test **90 files / 66,157 lines**, tests **86 files /
24,014 lines**. Task 2's gateway alone was ~3,200 lines of implementation plus ~1,600 lines of
tests and took **nine** independent review rounds.

**The honest headline:** Tasks 1 and 2 are *infrastructure*. None of it is what a user of a
study app would recognise as the product. Tasks 3–5 are the features, and all three are at zero.

## Order the rest has to happen in

Dependencies, not preference:

```
Task 3 (parsers → encrypted store → indexes → retrieval → citation checks)
   ├── Task 4 (memories live in Task 3's encrypted store; the ephemeral index is rebuilt from it)
   │      └── Task 5 (study plans and review consume memories)
   └── Task 6's acceptance metrics (Recall@10 ≥ 0.85, citation precision ≥ 0.95,
          grounded-answer pass ≥ 0.90) cannot be measured without Task 3
```

The two items inside Task 6 that are **independent of everything else**, and cheap:

1. **The four `ACTION_CONTRACT` collection errors.** `ImportError: cannot import name
   'ACTION_CONTRACT' from 'backend.engine'` in `test_v10182_runtime_ui.py`,
   `test_v22_dispatch_reliability.py`, `test_v27_weld.py`, `test_v28_weld.py`. They are not four
   broken tests; they are why **the default `pytest backend/tests` invocation executes zero
   tests** and why every regression claim in this repository needs
   `--continue-on-collection-errors`. For a portfolio project that is the worst kind of defect:
   the command everyone types first silently proves nothing.
2. **The 54 renderer TypeScript diagnostics** (baseline as well as HEAD — see `progress.md`), so
   `npm run typecheck` can become a gate instead of a known-red command.

## Suggested starting point

Start with those two, then Task 3's **data layer** (parsers + AES-256-GCM store) before anything
touching models. Reasons: the collection errors are small, independent, and immediately raise how
much the repository's own tests are worth; and within Task 3 the parsers plus encrypted store are
what everything else — page rendering, embeddings, indexes, memories — is built on. With both the
OCR engine (2026-09-21) and the local embedding pack (2026-09-21) dropped, the capability-pack
machinery is gone **entirely**: there is no model artefact left to download, verify, install or roll
back, which removes the largest chunk of the original Task 3 spec. What replaces it is a new
authorized call type on the gateway — see below.

## What I have not estimated

Sizes for Tasks 3–5. I have no measurement for them, and inventing a number would be the same
failure this project keeps punishing. The one real datum is Task 2: ~3,200 + ~1,600 lines across
nine review rounds.

The local OCR path was **removed from the Task 3 spec on 2026-09-21**, and the local embedding pack
on the same day (both recorded in `.superpowers/plans/studycrew-commercial-continuation.md`, with
their consequences). What Task 3 still needs to build is: seven document parsers, local page
rendering for text-layer-free PDFs, an **embedding call path through the Provider Gateway**
(authorization, DLP on the outbound text, budget reservation, `provider_egress` audit — the gateway
speaks only `chat` and `chat_stream` today), an AES-256-GCM store, two index types, and hybrid
retrieval with RRF and MMR.

What it no longer needs: an OCR engine, an OCR capability pack, a local embedding pack, and with
them the whole download / licence-manifest / SHA-256 / atomic-install / rollback subsystem — the
single largest piece of the original spec, and the only one that would have shipped ONNX models and
licence manifests. That is a large simplification. The price is recorded, not hidden: retrieval now
requires a configured BYOK provider and a network, so the product can no longer be fully offline,
and every indexed passage as well as every query leaves the machine under DLP authorization.

## Two decisions that belong to the user, not to me

Both were recorded during Task 2 and are still open:

- **Per-model privacy and price** (I-R1's remaining half). Measured this round: within a single
  provider the per-call price spread is two orders of magnitude (`openai-api` $0.6600 vs `arcee`
  $0.0041), so no provider-wide constant is right and the fix is per-model reservation — a data
  structure change, plus a verified per-model privacy list that does not exist anywhere in the
  repository. See `TASK2-ROUND2-ACCEPTANCE.md` limitation 1.
- **The five registry providers with no pricing entry** (`copilot`, `gmi`, `huggingface`,
  `kimi-coding`, `kimi-coding-cn`). Filling those in needs sources I would have to verify; I did
  not guess them.
