# SDD ledger — plan: .superpowers/plans/studycrew-commercial-continuation.md

- Workspace: D:/agentwork/StudyCrew-worktree
- Branch: feature/studycrew-foundation
- Baseline: 3a35d8b
- Global constraints reviewed: no internal conflicts found.
- Task 1: pending — atomic Profile availability and Profile POST authorization boundary extracted from the prior Task 4A breaker.
- Task 1: complete (commits f06fd38..30a24ec, independent review clean — 0 Critical / 0 Important / 0 Minor).
- Task 1: reviewer warning resolved — Task 1 changed no TypeScript files, and its affected backend Knowledge/Task Runtime partitions are green; repository-wide historical baselines remain assigned to Task 6.
- Task 2: pending — Provider, Tool, MCP, PII and secret gateways.
- Task 2: implementation complete (commits 30a24ec..2e72e99), focused set green (74 passed), but **acceptance FAILED**.
- Task 2: independent re-review verdict FAIL — 2 Critical / 8 Important / 1 Minor (`.superpowers/sdd/studycrew-commercial-continuation/task-2-rereview.md`).
- Task 2: Round 2 started but not implemented — only an untracked red test exists (`backend/tests/test_task2_round2_mcp_security.py`), currently `1 failed` (`McpRegistry.__init__() got an unexpected keyword argument 'http_client_factory'`).
- Task 2: three self-statements in `task-2-report.md` were independently verified as **inconsistent with the code** and corrected in its new `§0` (see also `.superpowers/BASELINE-2026-09-16.md`). Task 2 must not be recorded as accepted until Round 2 closes.

## Phase 0 (added 2026-09-16, not part of the original 6-task plan)

Rationale: the plan intended Task 6 to close packaging, but the build chain was found **broken for every
fresh checkout** — this blocks all later demonstration work, so it was pulled forward.

- Phase 0: packaging chain repaired — `build/installer.nsh` now has a generator (`scripts/make-build-resources.mjs`), PyInstaller spec renamed/rebranded/re-versioned (`backend/StudyCrewBackend.spec`), `dotenv` and unresolvable `ddgs`/`duckduckgo_search` removed from `hiddenimports`, new `scripts/backend-build.mjs` driver + `npm run backend:build`, artifact name unified to `StudyCrewBackend` across spec / `electron-builder.yml` / `electron/main.ts` / `copy-chromium.mjs`.
- Phase 0: guards added — `scripts/packaging-contract.test.mjs` (10 tests) plus `scripts/spec-check.mjs`, a **Python AST** checker for the spec's structural invariants. It replaced text regexes after an independent review pierced the regex version, and then **replaced its own heuristic rules** after a second review pierced those too: the `_VERSION` overwrite heuristic had its logic backwards (it missed the only dangerous order and rejected a safer one), so it was replaced by **executing** the spec's version block and reading the resulting value. `scripts/spec-check.test.mjs` adds 17 tests (1 real spec + 14 malformed mutations that must be rejected + 2 legitimate variants that must be accepted), all driving the **real** checker — deliberately not a re-implementation, because a self-written probe that mirrors the logic only proves itself. `npm run test:packaging` runs 27 tests; 4 `verify-runtime.mjs` checks pin the packaging artifact name.
- Phase 0: the spec's version block now has **no try/except at all** (the checker forbids any except there), so a wrong class name or a renamed module makes PyInstaller fail outright instead of producing an exe with empty version fields. The guard uses `sys.platform == "win32"` rather than `os.name == "nt"` so the checker can actually simulate the non-Windows branch. The product-level assertion (reading the built exe) remains the only hard proof that the version resource landed.
- Phase 0: reproducibility — `.tools/node24.ps1` downloads and pins Node 24.9.0 and **is tracked in git** (`.gitignore` narrowed to ignore only the downloaded runtime/zip); host Node 26 caused a failing `preflight` and 41 spurious frontend test failures (`localStorage` undefined). Under Node 24: `preflight OK`, `vitest 563 passed (50 files)`.
- Phase 0: documentation corrected — `TECH.md` / `TECH.zh-CN.md` no longer claim python-dotenv `.env` support, document the real one-command build (`npm run dist:win`) **including `npm run build:resources` in the step-by-step path**, the Electron binary prerequisite (`node install.js`), the correct Python 3.12 / Node 24 requirements, and the `pip install pyinstaller playwright pyyaml ddgs` prerequisites graded by whether a missing package hard-fails or only warns.
- Phase 0: **produced** — `release/StudyCrew-Setup-1.0.36.exe` (254.6 MiB) built from a checkout whose `build/installer.nsh` predates it. Verified end-to-end: silent install, packaged app launch (Electron + PyInstaller backend writing data), and silent uninstall that **keeps `$INSTDIR\data` and `$INSTDIR\Logs`** while removing all program files.
- Phase 0: acceptance report `.superpowers/PHASE0-ACCEPTANCE-2026-09-16.md` is a **third version**. It has been independently re-reviewed **twice**, both times judged FAIL, and both times the reviewer found statements in the report that did not match code. See its §8, §10 and §11 for the full correction log — including a wrong causal claim repeated in two different forms, and the "self-written probe proved nothing" mistake.

## Standing warnings for any future audit

- **pytest under a restricted sandbox produces false errors**: if the temp dir is unwritable, `tmp_path` fixtures raise `PermissionError: [WinError 5]`, which was observed to fabricate 404 errors in a full run. Always confirm the suite can write temp before trusting a backend baseline.
- **Node version must match `engines` (`>=24 <25`)** before quoting frontend results, otherwise `localStorage`-dependent suites fail spuriously.
- **Do not read `sdd/studycrew-implementation/task-3-report.md` / `task-4-report.md` as RAG/Mem0 progress** — they belong to the earlier 9-task plan (group domain / Task Runtime / UI). New 3 ← old 6, new 4 ← old 8, new 5 ← old 7.

