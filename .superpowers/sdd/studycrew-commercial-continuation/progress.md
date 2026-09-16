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
- Phase 0: guards added — `scripts/packaging-contract.test.mjs` (6 assertions) and 4 new `verify-runtime.mjs` checks for packaging-artifact identity.
- Phase 0: reproducibility — `.tools/node24.ps1` downloads and pins Node 24.9.0; host Node 26 caused a failing `preflight` and 41 spurious frontend test failures (`localStorage` undefined). Under Node 24: `preflight OK`, `vitest 563 passed (50 files)`.
- Phase 0: documentation corrected — `TECH.md` / `TECH.zh-CN.md` no longer claim python-dotenv `.env` support and now document the real one-command build (`npm run dist:win`).
- Phase 0: outstanding — `npm run dist:win` end-to-end NSIS artifact not yet produced (see the Phase 0 acceptance report).

## Standing warnings for any future audit

- **pytest under a restricted sandbox produces false errors**: if the temp dir is unwritable, `tmp_path` fixtures raise `PermissionError: [WinError 5]`, which was observed to fabricate 404 errors in a full run. Always confirm the suite can write temp before trusting a backend baseline.
- **Node version must match `engines` (`>=24 <25`)** before quoting frontend results, otherwise `localStorage`-dependent suites fail spuriously.
- **Do not read `sdd/studycrew-implementation/task-3-report.md` / `task-4-report.md` as RAG/Mem0 progress** — they belong to the earlier 9-task plan (group domain / Task Runtime / UI). New 3 ← old 6, new 4 ← old 8, new 5 ← old 7.

