# Backend regression gate repair

Date: 2026-10-04. Checkout: `D:/agentwork/StudyCrew-worktree`, branch `feature/studycrew-foundation`. Exact implementation BASE: `71998357e9b8380d2d5f34c846cb24e4d3f1188d`.

The complete default Python 3.12 backend command passes: **1619 passed, 5 skipped, 10 subtests passed**, zero failures/errors, 149.82 seconds. This is the frozen final source/test state. There are no waived failures, new skips, xfails, excluded test partitions or collection-error escape flags. The accepted local RAG slice through `a245c9c` is preserved; its source/tests were not edited and participate in this complete run. This report establishes the regression gate, not a claim that all legacy conversational effects or release packaging are available.

## Production changes and confirmed defects

Only three production files changed:

- `backend/worker_prompt.md`: the canonical response-language rule refers to the active system language from governed system context rather than hardcoding English.
- `backend/worker_gateway_runtime.py`: factory assembly supplies `Chinese (zh)` or `English (en)` from the explicit configured language, before identity/canonical protocol/user-address assembly. No task-text detection, runtime translation, output rewrite or second Worker protocol was introduced.
- `backend/task_runtime/service.py`: after retry frees bounded quarantine slots and overflow debt exists, the next bounded reconciliation starts at the beginning. This rediscovers invalid proofs behind the old page cursor instead of scanning only the tail and silently losing the overflow debt. Batch size, cohort size, backoff, proof validation and side-effect fences are unchanged.

The quarantine issue was exposed by the actual whole-suite diagnostic run, then reproduced individually. A targeted temporary probe showed the first two deferred entries quarantined successfully, an earlier overflow entry still `prepared`, and an idle service with empty deferred cohort, no overflow, no remaining pages and an empty cursor. Captured page sequence: first two entries fill the cohort; `e/f` overflow while the cursor advances past them; capacity becomes available; the old implementation scans a healthy terminal tail and forgets the entries behind the cursor. No debug instrumentation remains.

`test_overflow_retry_rediscovers_invalid_proofs_behind_current_page` reproduces that sequence deterministically through the real store/reconciliation/retry methods, with only timer scheduling held inert. It was red before the production change and green afterward. Its assertions preserve the two-entry cohort bound, stop once sweep debt is cleared, and require exactly one successful quarantine write per affected entry, with no repeated writes on an idle retry. The original valid-deletion/fairness test remains intact and passed eight consecutive isolated runs after the repair. Recovery/quarantine/ownership focused coverage also passed.

The Worker language defect was reproduced first through factory -> Runtime -> outgoing prompt assembly: zh and en both lacked an authoritative language context, and zh inherited the canonical English claim. The final eight-case zh/en matrix sends mixed historical/task text, Chinese/English explicit overrides and a multilingual requirement through the real ProviderGatewayClient and an inert `httpx.MockTransport`. A genuine running backend authority and purpose-bound credential lease are used. It checks the exact outgoing system/task messages, identity and language policy, model-turn budget accounting, path/code preservation rules and unchanged returned raw text. The earlier three-item static matrix whose task strings never entered the tested path was replaced by one clearly named canonical-policy check plus this actual assembled-message matrix.

## Old-to-new intent mapping

| Recorded cluster / old check | Repair and retained or replacement intent |
| --- | --- |
| Duplicate `backend/backend` roots in invariants/redaction/souls | Point to the actual backend source root. Preserve credential redaction, no hidden goal inference, no work-quality adjudication, raw Worker output, single completion-review channel, canonical Worker soul, silence and incremental member rules. Source-root correction revealed no further violation of those retained guards. |
| Missing explicit project directories (E2E and six Zinnia/server cases) | Real `tmp_path` workspace directories are passed to project creation, wire requests and approved card bodies. WebSocket persistence/replay/broadcast, explicit approval, user rename, kickoff and idempotent creation checks remain. The missing-directory rejection stays in production. |
| Create-project stub signature and subsequent hidden stub drift | Accept `roles`; use a real temporary directory; implement Hub `unread_count` and initialize `groups=None` in the deliberately minimal identity fixture. Keep approval-bound canonical identity and renderer retry-alias assertions. |
| POSIX lifecycle command on Windows | Use quoted `sys.executable -u -c` with flushed ticks and finite sleeps. Keep live-process, timeout, kill, state, owner and cleanup assertions on both platforms. |
| Implicit GBK reads/manual memory temporary roots | Use explicit UTF-8 reads/writes and pytest-owned directories/context-managed streams. Chinese activity text, harness compactness, install/change/version lifecycle and exclusion assertions remain. |
| English Coordinator display-name expectations | Align current catalog display with `Leader`; preserve zh -> en -> zh behavior and stable `coordinator` role identity. No role IDs or product catalog changed. |
| Compression-disabled test assumed global default false | Explicitly set the existing frozen-config field false in that test. Preserve unchanged output/zero ledger assertions and the product's enabled default. |
| Authority-free tool trace and protected-root fixtures | History AgentLoop executes the inert read through a real running authority and LegacyToolBoundary, retaining the full assistant/tool/final chain and call ID. Protected-root coverage separately calls the actual external-read handler to prove the path guard, then checks the governed migration denial. A denial before the handler is not represented as proof of its guard or as a positive external-read integration. |
| Three prompt-derived external-root successes | Replace each with the required negative assertion for forward-slash file, backslash file and directory prose. Add a real ToolGateway structured approval/grant positive using a descriptor-bound inert sandbox executor that reads only the selected temporary file. Missing approval, mismatched task, outside path and fresh revoked-grant branches have no effect. No prompt can grant permissions. |
| Four conversational Zinnia platform creation successes | Replace with explicit legacy adapter blocked/no-card/no-approval/no-effect receipts and continued model wrap-up/tool rounds. Keep manual approved-card creation/user-rename tests with valid directories. Add authenticated loopback `/groups/create` success: unauthenticated input produces no group; explicit user request creates the selected workspace/group and mandatory roles with canonical group/project mapping and no invented task/run. |
| Six morph/aux behavior fixtures lacked purpose-bound binding | Give this behavior-only module a genuine running test authority, actual aux-slot lease and opaque binding. Retain unchanged instruction, provider failure, multiple rounds, feedback history, amendment and approval lifecycle checks. Stubbed chat tests do not establish real UI aux availability. |
| Five pre-i18n Chinese-source-literal checks | Check actual zh/en dispatch receipts and frozen no-dispatch behavior (including invalid instruction), with no repeated card/effect or report jargon. The server feedback check now runs the real control command and verifies one localized failure event, same-card replay and unchanged pending card. A small source-order guard still verifies freeze precedes validation without depending on Chinese literals. Handler receipt checks are explicitly handler-level, not positive governed legacy dispatch integration. |
| Unbounded-loop/no-effort/no-correction historical invariants | Replace obsolete unbounded-policy assertions with real Task Runtime exhaustion for all five dimensions, public terminal exhaustion evidence and no governed effect after exhaustion. Recovery permits bounded parameter correction but stops at the attempt limit, permission/budget/irreversible failures, and cannot select a provider fallback without same privacy and prior authority. The Worker no-framework-correction guard remains. |
| Task-language inference expectation | Active configured-system language is authoritative; explicit language overrides and multilingual requests stay opaque in the task payload. Single canonical protocol and no detector/translation guards remain, with real outbound assembly coverage described above. |

## Explicit production-risk dispositions and availability limits

**Aux control-plane availability: reproduced, deliberately fail-closed, not restored.** The real `KnoweServer._cmd_feedback_instruction` calls engine adjustment for a real pending card while a valid aux lease is installed. No Task Runtime authority is inherited by this control command. The real gateway denies before the inert transport is called. The card remains pending with its old instruction, no feedback-history pollution, exactly one error event and one same-card receipt replay. A settled-card follow-up also has no egress. Even valid-looking group/task/run IDs injected into renderer input and the card cannot become authority.

`Gate._Pending` and the durable legacy card lack a backend-owned card -> Task Runtime run association. A safe success path needs a separately scoped Harness integration that persists/validates that ownership and lifecycle, preserves cancellation, stale-card handling, budgets and PII policy, and carries exact authority into the cancellable flight. This increment creates no background run or broad capability and trusts no model/renderer IDs. Positive morph behavior fixtures with active authority are separate evidence, not a claim that the unsupported UI path works.

**Configured zh Worker: confirmed and repaired**, as above. The tests validate assembled policy and payload preservation with an inert response; they do not claim a real model's language compliance.

**Legacy Zinnia conversational creation and external tool adapters remain unavailable.** Provider authority alone does not authorize their platform/filesystem effects. Supported explicit group creation and manual approval-card lifecycle are verified at their actual seams; the block in `agents/zinnia.py` is unchanged. The selected structured sandbox executor is test-local and makes no process/network calls; it does not attest a production Windows shell sandbox.

No installer/sidecar was rebuilt. Controller separately reported Node24 `npm run test:foundation`: 33 passed plus runtime contract passed, exit 0; this is independent static packaging-contract evidence. It does not resolve the separately tracked sidecar/NSIS NumPy packaging exclusion/rebuild blocker.

## Verification evidence

All commands were run from the checkout with PowerShell 7.6.5; Python output was explicitly UTF-8. Files preserve UTF-8.

- Initial focused reproduction ran the 18 files named by the triage, preserving failures as a diagnostic signal. Source-root repair alone then produced 24 passes in the invariant/redaction/soul/lifecycle selection (one separate frozen-config fixture misuse was immediately corrected).
- Focused Zinnia + WebSocket E2E: **46 passed**, 9.78 s, before addition of the separately passing authenticated group-creation case.
- Worker actual configured-language gateway matrix and canonical checks: **25 passed**, 1.71 s.
- Actual unowned-card control command + budget/invariant selection: **16 passed**, 2.51 s at that stage.
- Receipt/freeze/morph/path-guard selection: **71 existing focused cases passed**; two initial new-test import errors were corrected and the actual command probes subsequently passed.
- Structured creation/external permission selection: **7 passed**, 1.49 s before addition of the separately passing revoked-grant branch.
- Final focused budget/grant/identity/quarantine selection: **28 passed**, 2.28 s.
- Recovery/quarantine/ownership/acknowledgement selection: **114 passed**, 36.51 s. Strengthened deterministic overflow regression: **1 passed**, 0.71 s; original race-sensitive case subsequently passed **8/8** separate runs.
- Mechanical/memory/display/compression/terminal/identity/history/redaction/soul final selection: **81 passed, 5 skipped**, 6.66 s.
- A whole-suite diagnostic run discovered the deeper identity stub and real overflow failures: **2 failed, 1614 passed, 5 skipped, 10 subtests passed**, 177.63 s. Some source/test edits occurred during that run; its total is diagnostic only, not the delivered passing gate.
- Frozen final command: `py -3.12 -m pytest backend/tests -q --tb=short` -> **1619 passed, 5 skipped, 10 subtests passed**, 149.82 s, exit 0. No collection escape, partition filter or failure cache was used.
- `py -3.12 -m compileall -q backend` -> exit 0 after final source/test edits.
- Node24.9.0 via `.tools/node24.ps1`: `npm run typecheck`, `npm run lint`, `npm test` (each checked before proceeding) -> exit 0; frontend **55 test files, 588 tests passed**, 64.63 s. No frontend source changed afterward.
- `git -c core.safecrlf=false diff --check` -> exit 0. Temporary diagnostic tags were removed; `rg DEBUG-baseline backend` returned no matches.

The five unchanged Windows skips are all in `test_batch4_terminal.py`:

| Case | Existing condition / reason |
| --- | --- |
| `test_timeout_kills_and_reports` | `_WIN`: POSIX `sleep` command semantics. |
| `test_timeout_takes_the_children_too` | `_WIN`: POSIX shell process-group descendant-kill scenario. |
| `test_large_output_does_not_hang_and_is_fully_logged` | `_WIN`: large-output fixture uses POSIX shell/`seq`. |
| `test_provider_key_is_not_handed_to_children` | `_WIN`: shell expansion of POSIX environment variables. |
| `test_prefers_project_venv` | `_WIN`: fixture synthesizes `.venv/bin/python3` with a POSIX shell script. |

These platform-specific fixtures were not increased, weakened or replaced to force green. The separately repaired process-lifecycle test now runs on Windows.

Cleanup: the initial failing manual memory fixtures leaked eight generated files under three verified test-only roots (`tmpu0q4dr40`, `tmpxggmqt5s`, `tmpy2lr10rs` in the Windows temp directory). Their timestamp/content matched this run's context/manifest fixtures; only those exact files and then empty directories were removed, without recursive deletion. Subsequent memory fixtures use pytest-owned `tmp_path`. No pre-existing temporary artifacts or user data were removed; no actual provider calls, user credentials, dependency installation or installer mutation occurred.

Commit scope is the listed backend source/tests and this report only. Controller brief/progress/roadmap/other reports and `skills/` are excluded. Independent specification and quality review remains required.

## Review fix round 1 — I-1 transient restarted-sweep read failure

Fix BASE: `9e5264cf94d5455d3b7886a4e51478476274b87c`. Read the complete durable review `reviews/REVIEW-backend-baseline-9e5264c.md`. This increment addresses its single Important finding I-1 only. The frozen full result below supersedes the initial implementation run above; no remaining failure is waived.

Confirmed sequence: a two-entry retry cohort fills while the next two invalid intents overflow; the cursor advances past those intents; recovered quarantine writes free the cohort; the restarted head page raises one inert `RuntimeStoreError`. The former one-call cursor override leaves the service-owned cursor at the old tail. With both the cohort and overflow flag cleared, a subsequent tail read settles scanning while two intents remain `prepared`.

Production change is limited to `backend/task_runtime/service.py`. Before requesting the restarted sweep, record the head cursor and outstanding page debt in the existing service-owned in-memory scan state under its lock. A failed page read leaves that state intact, so the next bounded retry again starts at the head. Only a successfully read page updates the cursor/remaining-page state; normal durable cursor persistence remains on that successful-page path. No new queue, authority, provider call, side effect, unbounded scan, batch/cohort change or timer/backoff change is introduced. This is an in-memory retry obligation, not a new crash-atomic persistence protocol.

The old healthy-read overflow regression remains and now has a second single-failure parameter. Both use the real store and reconciliation/retry methods; only timer scheduling and one page-read failure are inert. The fault branch asserts the cohort really freed, overflow cleared, the two missing intents still prepared, and the failed report plus head-cursor/page-debt state retained. Within five explicit retry invocations every invalid intent must become quarantined, every page request must remain limited to two entries, and the first two fault-branch requests must both be from the head. Each invalid intent has exactly one successful quarantine write. An additional idle invocation must issue neither further page requests nor repeated writes. The existing ownership, deletion authorization and valid-deletion fairness coverage is unchanged.

Commands and actual results (PowerShell 7.6.5, Python output UTF-8):

- Red before production repair: `py -3.12 -m pytest backend/tests/test_task_runtime_quarantine_isolation.py -q --tb=short -k overflow_retry_rediscovers` -> **1 failed, 1 passed, 68 deselected in 1.14s**, exit 1. The single-read-failure case failed because not every invalid intent was quarantined; the healthy-read case passed.
- Green after repair and strengthened failed-page assertions: same command -> **2 passed, 68 deselected in 2.48s**, exit 0.
- Focused quarantine/recovery/ownership/ack/deletion authorization: `py -3.12 -m pytest backend/tests/test_task_runtime_quarantine_isolation.py backend/tests/test_task_runtime_recovery_ownership.py backend/tests/test_task_runtime_recovery_ack.py backend/tests/test_task_runtime_deletion_authorization.py -q --tb=short` -> **121 passed in 36.78s**, exit 0.
- Production and test files then frozen; complete default backend command `py -3.12 -m pytest backend/tests -q --tb=short` -> **1620 passed, 5 skipped, 10 subtests passed in 145.83s (0:02:25)**, exit 0. No exclusions, failure-cache evidence, collection escape, xfail or new skip. The extra passing case is the new transient-read-failure parameter.
- Actual final terminal tail returned by session `77020` is preserved verbatim in `backend-baseline-fix1-test-output.txt`; the final tool result reported exit code 0. It is raw command output, not reconstructed counts.
- On the same frozen source/tests: `py -3.12 -m compileall -q backend` -> exit 0; `git -c core.safecrlf=false diff --check` -> exit 0.

The five existing Windows skips and all previously documented release availability limits are unchanged. No frontend, Worker, RAG or packaging source changed in this round; per review scope their earlier passing gates were not rerun, and no sidecar/installer rebuild is claimed. Commit scope is exactly the service, quarantine regression, this report and the raw terminal-tail artifact. Controller bookkeeping/review documents and `skills/` are excluded; independent review remains required.
