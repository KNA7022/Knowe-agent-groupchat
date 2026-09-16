# Knowe — Technical Overview

A quick technical guide for developers who want to run, build, or extend Knowe. For the full user manual, see [docs](./docs/EN/00-Overview.md) (中文见 [docs/CN](./docs/CN/00-概述.md)).

---

## Architecture at a glance

![Knowe architecture](./docs/assets/TECH-architecture-en.png)

- **Frontend**: Electron + React 18 + TypeScript, built with electron-vite
- **Backend**: Python 3.12 (FastAPI-style async server), packaged with PyInstaller
- **Communication**: local WebSocket (127.0.0.1:8080) + health endpoint (127.0.0.1:8081)
- **Data**: stored locally — see [Data & privacy](#data--privacy)

## Harness: task lifecycle & completion mechanism

The Harness (`backend/knowe_harness/`, ~4,300 lines) is the mechanism layer that governs the full lifecycle of every task — from task envelope and journaling, through the completion state machine, to user-facing projections and outbox delivery. It guarantees the mechanism cannot go wrong: results must be traced and decisions must be recorded — while placing no restriction on agent behavior.

![Harness mechanism](./docs/assets/TECH-harness-en.png)

## Repository layout

| Path | What it is |
|---|---|
| `electron/` | Electron main process & preload (desktop shell, window, updater, tray) |
| `src/` | React renderer (UI components, state, i18n) |
| `backend/` | Python backend (agents, harness, storage, knowledge, tools) |
| `backend/tests/` | Backend test suite (55+ test files) |
| `docs/` | Full user manual (CN + EN) |
| `assets/` | Banner image & demo videos |
| `scripts/` | Build/dev helper scripts |

## Development

Requirements: Node.js 24+, Python 3.12, and a model API key (e.g. DeepSeek).

```bash
# install frontend dependencies
npm install

# install backend dependencies
pip install -r backend/requirements.txt

# run in dev mode (frontend + Electron shell)
npm run electron:dev
```

The backend is started automatically by the Electron main process in dev mode. For backend-only work, see `backend/run_backend.py`.

### Verify / lint / test

```bash
npm run typecheck   # TypeScript check (renderer + main)
npm run lint        # ESLint
npm run test        # Vitest (frontend)
# backend tests: pytest backend/tests/ (from backend/)
```

### Configuration

Configuration is read from environment variables (see `backend/config.py`). Key ones:

| Variable | Purpose |
|---|---|
| `DEEPSEEK_API_KEY` | Model provider API key (or configure models in the app UI) |
| `DEEPSEEK_BASE_URL` | Custom provider base URL |
| `KNOWE_AGENT` | Agent mode: `fake` (offline, no LLM) or `deepseek` |
| `KNOWE_DATA_DIR` | Override data directory |

A `.env` file is **not** read by the backend. Credentials are never sourced from
environment variables or plaintext files: the Electron main process stores provider
keys through OS `safeStorage`, and the Python side can only redeem short-lived,
purpose-bound leases. Use the in-app Settings page to configure a provider; set the
non-secret variables above (e.g. `KNOWE_AGENT`, `KNOWE_DATA_DIR`) in the environment
if you need to override them.

## Building the Windows installer

The full build pipeline produces an NSIS installer via electron-builder, with the Python backend bundled by PyInstaller.

Prerequisites: Node.js 24.x (`npm run preflight` enforces this) and Python 3.12 with
`pip install pyinstaller playwright pyyaml ddgs`. PyInstaller only bundles what is
importable at build time, so anything missing on the build machine is missing from the
shipped app:

- `pyinstaller` — required.
- `playwright` — required; `npm run backend:build` **fails loudly** without it rather
  than shipping an app whose browser automation is silently absent.
- `pyyaml` and `ddgs` — optional; `backend:build` only warns. `pyyaml` enables the
  stricter YAML check in `file_ops.py` (that module documents it as optional), and
  `ddgs` powers web search. Add them if you want those capabilities in the artifact.

On a fresh checkout you must also fetch the Electron binary once (it is not part of
the npm install when binary download was skipped):

```bash
cd node_modules/electron && node install.js
```

`electron-builder.yml` pins `electronDist: node_modules/electron/dist`, so packaging
fails with a clear error if that directory is missing.

```bash
# One command does everything (NSIS resources → icon → PyInstaller → chromium → electron-builder NSIS):
npm run dist:win
```

Or the individual steps:

```bash
# 1. generate build/ resources (build/ is gitignored, so both must be regenerated per checkout)
npm run build:resources   # build/installer.nsh — referenced by electron-builder.yml nsis.include
npm run icon:ico          # build/icon.ico

# 2. build the Python backend (PyInstaller) — runs from backend/
npm run backend:build

# 3. prepare Chromium for browser automation
npm run copy:chromium

# 4. build the full installer (electron-vite build + electron-builder NSIS)
npm run dist:win:app
```

Output lands in `release/` as `StudyCrew-Setup-<version>.exe`.

> **Note**: after changing backend code, you MUST re-run step 2 (PyInstaller) — electron-builder only assembles the frontend and does not rebuild the backend. Verify the bundled `StudyCrewBackend.exe` timestamp is fresh after packaging.

## Data & privacy

- All data lives **locally** under the install directory: `data/` (projects, chat history, knowledge base, token ledger)
- Chat history is never deleted by the app
- Agents operate inside a sandboxed project directory — they cannot touch files outside the project root
- No telemetry, no cloud storage: everything stays on your machine

## Common issues

| Symptom | Likely cause / fix |
|---|---|
| App opens but agents don't respond | Model API key missing or invalid — configure in Settings → Models |
| Backend fails to start | Port 8080/8081 occupied — check for another Knowe instance |
| Installer is ~250 MB | Normal: bundles Python runtime + Chromium for browser automation |

## License

[MIT](./LICENSE)
