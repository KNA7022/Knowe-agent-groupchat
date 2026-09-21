# Task 2, Round 2 — acceptance status

**Revision:** `86a7b92` (branch `feature/studycrew-foundation`)
**Acceptance threshold:** 0 Critical / 0 Important from an independent review. Minor findings are
recorded; they do not block.
**Verdict:** *(pending.)* The eighth review returned **PASS — 0 Critical / 0 Important / 3 Minor**
for `cc94652`, but it was deliberately scoped to three checks and did not reach the Important the
seventh review found on the neighbouring revision (`8331715`): the cancellation guards caught only
our own exception types, while the storage layer re-raises raw `sqlite3.*`. That finding is fixed
here. Nothing in this file should be read as "passed" until a review of the **current** revision
returns 0 Critical / 0 Important.

Scope: the commercial gateway layer — `backend/commercial_gateway.py` is the single production
egress for LLM provider calls, plus the MCP, tool, PII and secret gateways — and the Electron
environment allowlist.

## Required items

| item | what it was | disposition | evidence |
|---|---|---|---|
| **C-R1** | Provider ungoverned test seam: a constructor flag made `_authorize` return early, skipping authority, capability registry, DLP, budget, approval and audit | **Fixed** (`ebf7feb`, hardened in `247c9ae`). The flag, the short circuit, the three agent call sites and the `ProviderClient` copy are gone; `_authorize` has exactly one `return`; a stand-in runtime is refused by `isinstance(runtime, TaskRuntimeService)` | `test_injecting_a_transport_no_longer_bypasses_authorization`, `test_a_duck_typed_runtime_cannot_authorize_provider_egress`, `test_a_duck_typed_runtime_is_rejected_before_any_approval_work`; mutations "restore the parameter", "insert `return None`", "drop the isinstance check" all CAUGHT |
| **C-R2** | MCP transport/effect binding: callers could supply the transport or swap the effect after registration | **Fixed** (`e95b92c`). `McpRegistry` builds and seals its own transports; `_seal_target` re-checks server_id, canonical endpoint and transport type on every use; protocol version must be from the supported set | `test_registry_builds_and_seals_transport_from_registered_config`, `test_seal_check_detects_a_swapped_stdio_executable` |
| **I-R1 (budget half)** | Reservations were charged in full, retries were never billed, failures charged the worst case, and `settle_budget` could erase usage that was never reserved | **Fixed** (`f50d082`, `b5b96d3`, `3d458b0`, `5196369`, `8ae1263`). Reservation and consume share one `_reserved_usage`; settlement refunds the unused reservation; failed calls settle turns/cost to the actual attempt count; `total_tokens` takes precedence; the release is bounded by recorded usage in all five dimensions | `test_retried_call_charges_every_attempt`, `test_successful_first_attempt_refunds_the_unused_retry_reservation`, the five-parameter `test_settle_budget_refuses_to_release_more_than_was_reserved`; mutations per dimension CAUGHT |
| **I-R1 (per-model half)** | `models=("*",)` means privacy and price are declared **per provider**, not per model | **Not done — recorded as a measured product trade-off.** The code now explains why at the wildcard itself, and a test locks the limitation's behaviour so tightening it forces the docs to be updated with it | see "Known limitations" below for the measurement |
| **I-R4** | MCP schemas, protocol version and tool results were returned verbatim and mutable | **Fixed** (`59dd60c`). Discovery results are deep-copied; resource textness is decided by MIME type; protocol version and `capabilities` are validated strictly; `tools/call` results are validated (shape, bounds, prompt/DLP on text, decoded data, `structuredContent`) | `test_discovery_result_is_a_copy_so_callers_cannot_weaken_validation`, `test_textual_mime_blob_is_scanned_for_prompt_injection`, `test_binary_mime_blob_is_not_forced_through_text_policy` |
| **I-R6** | Electron copied all of `process.env` and removed seven name suffixes, which let credential-shaped variables through and dropped `ComSpec`/`ProgramData`/`ProgramFiles` | **Fixed** (`f5fd83e`, `ea3e9d6`, `4354db9`). Explicit allowlist, case-insensitive matching, proxy/CA variables restored; the builder was extracted into `electron/backendEnv.ts` so a test can actually execute it | `electron/backendEnv.test.ts` (7 tests running the real logic against a mixed-case Windows environment) |
| **I-R8** | The Settings connection test could never work: it built a client with no authority, so the Gateway refused before the network, and nothing tested it | **Fixed** (`a97b33c`, `d998d75`). It now runs inside a backend-authored short-lived Task Runtime run, reuses a RUNNING probe run instead of leaking one per click, cancels when done, and prunes its history | probe authority tests + the bounded-retention test that replaced one asserting the leak |
| **I-R2** | Shell sandbox trust was a name in a set, so a trusted marker could be paired with a different executor | **Fixed** (`8466221`). `ShellSandboxAttestation` carries the executor object; `register()` refuses a mismatch; `invoke_with_effect` refuses effect override for shell descriptors | `test_shell_attestation_must_be_bound_to_the_registered_executor`, `test_shell_effect_cannot_be_replaced_by_invoke_with_effect` |
| **I-R3b, I-R7** | Electron main lacks a closed slot catalogue; secret revocation is eventual (240 s) rather than atomic | **Recorded as known limitations with assertions pinning the defences that do hold.** This round those assertions were mutation-verified for the first time: dropping the `transport` check, dropping the `endpoint` check, or disabling the expiry sweep each fail a test | `test_provider_lease_redemption_binds_every_security_field`, `test_expired_lease_cannot_be_redeemed` |

## Review history

| round | revision | verdict | outcome |
|---|---|---|---|
| 1st/2nd | `2e72e99` | FAIL — 2 Critical / 8 Important | Round 2 opened |
| 3rd | — | FAIL — 2 Critical / 7 Important | both Criticals proven; all seven Importants fixed |
| 4th | — | FAIL — 1 Critical / 5 Important / 10 Minor | the Critical was self-inflicted by a previous fix; all five Importants closed. **This round's Minor list is lost** — see below |
| 5th | `73d7e33` | FAIL — 0 Critical / 4 Important / 7 Minor | report persisted in `reviews/` |
| 6th | `448c4c3` | FAIL — 0 Critical / 2 Important / 2 Minor | report persisted in `reviews/` |
| 7th | `8331715` | FAIL — 0 Critical / 1 Important / 1 Minor | returned after being cancelled as unresponsive; its Important was real — see limitation 6 |
| 8th | `cc94652` | PASS — 0 Critical / 0 Important / 3 Minor | scoped to three checks; two of its three Minors did not hold against its own frozen revision |
| 9th | *(current)* | *(pending)* | re-review after the seventh review's Important was fixed |

## Known limitations, with their consequences

1. **Per-model privacy and price (I-R1's remaining half).** `models=("*",)` means any model name
   passes for a registered provider, with `privacy=STANDARD` and `estimated_cost_usd=0.01` at the
   **provider** level. Measured this round: the repository's sourced pricing catalogue
   (`backend/token_pricing.py`, 14 providers with source URLs) has **no entry at all** for five
   registry providers (`copilot`, `gmi`, `huggingface`, `kimi-coding`, `kimi-coding-cn`), so
   tightening the wildcard would make those five unusable, and it contains **no privacy data
   whatsoever** — per-model privacy cannot be derived from it. Recommendation on record: keep the
   wildcard until a verified per-model privacy list exists, then make **privacy** per-model (the
   part with real user consequence) and leave price at provider level. This is a product decision.
2. **`chat_stream` terminal event is not guaranteed.** If a consumer neither closes the generator
   nor lets the event loop run `shutdown_asyncgens`, the terminal `provider_egress` event can be
   missing entirely. Measured by the fifth review; not fixable inside that function.
3. **`_gateway_managed` is a declaration, not a control.** It refuses a missing declaration; once
   declared, nothing inspects what was injected. A wrapper around a real client reaches a real
   socket. Stated in the code, and pinned by a test that asserts both halves.
4. **`isinstance(runtime, TaskRuntimeService)` is not a cryptographic boundary.** A subclass gets
   through — measured by the sixth review, and that is exactly what the comment says. The real
   constraint is that production rebuilds the authority from the store
   (`ProjectEngine._canonical_legacy_tool_authority`), which now has a test of its own after
   having had none.
5. **The review that only exists in a reply is lost.** The fourth review's ten Minor findings
   disappeared this way. Fifth, sixth, seventh and eighth reports are now committed under `reviews/`
   with an index; the third and fourth are unrecoverable.
6. **The cancellation guards now swallow every exception, and that is a deliberate trade.**
   `knowe_storage/_sqlite.py`'s `transaction()` rolls back and re-raises raw `sqlite3.*`, and nothing
   wraps them into `RuntimeStoreError` — so a narrow `except` list missed exactly the failures a real
   store produces. Measured by the seventh review with a probe that closed the database inside the
   effect: `sqlite3.ProgrammingError: Cannot operate on a closed database` escaped `aclose()` and
   replaced the caller's cancellation. The six swallow-type guards (and the `prepare_tool` →
   `GatewayDenied` conversion) are now `except Exception`. The cost is real and is written at the
   first guard: a programming error on the same line — a store interface mismatch, say — is also
   swallowed, silently, because this module has no logger. On this path "cancellation replaced" is
   worse than "one audit record missing".

## Evidence summary at this revision

- `backend/tests/test_task2_round2_mcp_security.py`: 50 passed
- Full backend suite: 56 failed / 988 passed / 5 skipped / 20 errors — no failure name appears that
  is not one of the recorded flaky cases
- Frontend: 575 passed / 52 files
- `git status --porcelain -uall` clean
- Per-change mutation table: 15 behaviour changes, each with the mutation that reverts it and the
  test that must fail (see `progress.md`)
