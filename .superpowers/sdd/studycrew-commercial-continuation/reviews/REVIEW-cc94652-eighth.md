# Focused review — cc94652

> **Provenance.** This is the text the reviewer returned to the parent agent, reproduced from
> the returned message rather than from a file it wrote. Unlike the fifth, sixth and seventh
> reports (which the reviewer left as `.md` files under `%TEMP%`), this one only ever existed
> as a message. Parent annotations are confined to the two blockquotes marked **"Parent note"**
> at the end; the body below is the reviewer's own text.

Verdict: **PASS — 0 Critical, 0 Important, 3 Minor**. Completed in one pass; nothing deferred.

Note: the worktree HEAD moved to `86a7b92` while I reviewed (`git log cc94652..HEAD` = one
commit, `.gitignore` only; `git diff cc94652..HEAD --stat` = 1 file, 6 insertions). The two code
files I studied are byte-identical to cc94652. All measurements come from a frozen
`git archive cc94652` export at `%TEMP%\rev7_cc94652`. Nothing was written in the worktree;
`git status --porcelain` is clean and restored-file hashes equal cc94652.

## Findings

### M-1 The registry comment under-reports the ledger's own measurement (3 of 5 providers)
- Where: `backend/commercial_gateway.py:2196-2197` (added by `4877985`)
- What: The comment says the pricing catalogue has no entry for "`copilot` / `gmi` /
  `huggingface` / `kimi-coding` / `kimi-coding-cn` 五个 provider" but then names only three of
  them (`copilot`, `gmi`, `huggingface`). Not false — the count (5) is right — but it is weaker
  than the ledger entry it points at (`progress.md:28`), which lists all five correctly. The
  fifth named provider is never stated in the code.
- Evidence: `py -3.12` against the frozen export: `registry: 19  catalogue: 17`; `registry
  providers with NO pricing entry:
  ['copilot','gmi','huggingface','kimi-coding','kimi-coding-cn'] 5`.
- Severity rationale: Documentation precision in a comment whose whole purpose is to prevent a
  future reader from guessing; the risk is a reader concluding the set is
  `{copilot, gmi, huggingface}`.

### M-2 The same new comment paraphrases `authorize()` by dropping one of its two conditions
- Where: `backend/commercial_gateway.py:2195-2196` vs `backend/commercial_gateway.py:2007`
- What: The comment says `authorize()` only checks the model name "在 `"*" not in
  capability.models` 时才核对模型名". The code is `if "*" not in capability.models and
  str(model) not in capability.models:`. The conclusion drawn (a wildcard means any model
  passes) is correct; the quoted condition is incomplete.
- Severity rationale: The conclusion is right, so nothing misleads about behaviour; only the
  quoted condition is inexact.

### M-3 Ledger counts one test too many for the revision it describes
- Where: `.superpowers/sdd/studycrew-commercial-continuation/progress.md:64` ("Verified as
  genuinely closed by the sixth review")
- What: At `8331715` — where the sixth-review verdict block and its "verified as closed" list
  were written — the round-2 file holds **47** tests, not 48. The 48th
  (`test_the_model_wildcard_is_a_recorded_limitation_not_an_oversight`) arrives only in
  `4877985`, the very next commit. This is the same "count written from memory" habit as M-2 of
  the sixth review and `4877985`'s own amended message.
- Evidence: test-function counts from git (`8331715`: 39 `def test_`/`async def test_`;
  `4877985` and HEAD: 40; the file also carries 8 parametrised cases) and my own baseline run of
  the frozen export: `48 passed in 7.19s`. `4877985`'s message itself states "Round-2 acceptance
  file: 48 passed" and its amendment note records the earlier wrong 49.
- Severity rationale: Ledger arithmetic only; no code or test behaviour is affected, and the
  commit message for `8331715` states 47 correctly.

## Verified

**Check 1 — do the two fixes bite? Both CAUGHT.**

- Mutation 1 (remove the `try`/`except` around `store.cancel_tool`, `commercial_gateway.py:395-398`,
  leaving the bare call + `raise`): `py -3.12 -m pytest
  backend/tests/test_task2_round2_mcp_security.py --continue-on-collection-errors -q -k
  tool_cancellation_survives` → **1 failed, 47 deselected**. Not passing for the wrong reason: the
  traceback lands on the real code path `invoke → _invoke:395 → exploding()` and the exception is
  the *effect's own* `RuntimeStoreError("store is unavailable: cancel_tool")` injected at test line
  1850 — not pytest machinery. The `CancelledError` under test comes from the registered effect
  (`def effect(_request): raise asyncio.CancelledError()`). Baseline (unmutated) on the same
  export: `-k tool_cancellation_survives` passes.
- Mutation 2 (delete the `isinstance(runtime, TaskRuntimeService)` check,
  `commercial_gateway.py:2465-2468`): `-k duck_typed` → **2 failed, 46 deselected**, and the new
  test fails with exactly `AssertionError: prepare_tool must not be reached` raised at
  `backend/tests/test_task2_round2_mcp_security.py:1542`, faulting from
  `commercial_gateway.py:2502 invocation = runtime.prepare_tool(...)`. So the test genuinely
  reaches the approval path rather than the non-sensitive one. (My first attempt at this mutation
  silently did not apply — the hash was unchanged and the run was a false "2 passed". I redid it
  and confirmed the hash changed before trusting the result.)
- Precondition is real, not decoration: `ProviderGatewayClient._serialized_request(messages, None,
  None)` on `"学号 2026123456，手机号 13800138000"` →
  `PiiDlpGateway().decide_outbound(serialized, approved=False)` → `requires_approval=True`,
  `allowed=False`, `reason=sensitive_outbound`. The payload is byte-identical to what `chat()`
  hands `_authorize` (only call site `:2487` passes the same triple `messages, tools, extra_body`,
  both default `None`).

**Check 2 — the `cancelled` terminal state is consistent.** Measured on a real
`TaskRuntimeService`:

- `cancel_tool` on a **prepared** invocation → no-op returning the current object with
  `status='prepared'`, `completed_at=None`, `error_type=''`; the invocation is then *still*
  claimable (`claim` → `running`, `True`). No event is written and nothing is corrupted, because
  `_transition_tool_with_event` returns `(current, False)` before the transaction body when
  `current.status not in expected_statuses` (`store.py:1322`). So "cancel a not-running
  invocation" is a silent no-op, not an error and not a bogus terminal write.
- `cancel_tool` on a **running** invocation → `status='cancelled'`, `error_type=''`,
  `started_at`/`completed_at` both set, `replay_allowed` left `True` (it is only reset by the
  recovery path, `store.py:1457`). Calling it again → no-op, same `completed_at`, no duplicate
  event.
- **Can a cancelled invocation be replayed or recovered into a different state? No.**
  `complete_tool(completed)`, `complete_tool(failed)`, `mark_tool_recovered_blocked`, and
  `claim_tool_execution` called on a cancelled invocation each **return the same `cancelled`
  object without raising**; `incomplete_tools()` (only `status IN ('prepared','running')`,
  `store.py:1283`) excluded it; `recover_after_restart()` put only the genuinely-running peer in
  `replayable_pending` and never touched the cancelled one.
- Public-payload contract: the broadcast `tool_cancelled` payload keys are exactly
  `{status, invocation_id, tool_name, purpose, risk, started_at, completed_at, duration_ms, usage,
  error_type}` — measured `extra=[] missing=[]` against `contracts.py:82-85`, and identical in the
  frontend mirror `publicEventFields.tool_cancelled` (`src/contract/taskRuntime.ts:175-178`).

**Check 3 — no new Critical/Important in the later commits.** `git diff --stat cc94652..HEAD` and
per-commit stats: `350beaf`, `214dc24`, `f25e039`, `cc94652` touch only `progress.md`/`reviews/`;
`4877985` touches only the registry comment and one test. The wildcard test genuinely reaches its
subject. Ledger spot-checks: (a) "**5 registry providers have no pricing entry at all**" —
**TRUE**; (b) "the catalogue has no privacy data whatsoever" — consistent; (c) "`anthropic` 11,
`openai-api` 13, `openrouter` 35, `zai` 8" — **exactly 11 / 13 / 35 / 8**; (d) `progress.md:83`
publishes `store.py 52d0f299…` as a restored-file hash — measured
`52D0F299AE78AEA333343885E924BF082A70DAFADCA5CC2CBB691B8EE43C981D`, matching.

Restored-file hashes (mutations applied and reverted in `%TEMP%`):
`backend/commercial_gateway.py 3851514FFE044E704357B97E671C2CB0411AE94A323CF1E52F12B549F7BA9B55`,
`backend/tests/test_task2_round2_mcp_security.py 3E9FC2A52BED14E7930F3D18BE4C1614E0094A1CF9799FCBD2148F6C3E249CB2`.
Baseline of the unmutated export: **48 passed**.

**Observation (pre-existing, not new in this round, so not classed as a finding):** the frontend
accepts and stores `tool_cancelled`, but the tools panel only ever enumerates `tool_prepared`
(`src/components/AgentStatusBar.tsx:196-197`), and no reducer branch in `src/store/taskRuntime.ts`
handles any `tool_*` event except `tool_prepared` (`:337`). So a cancelled tool keeps rendering as
"Governed tool effect prepared".

## Not checked

- The full backend suite (explicitly out of scope).
- `git diff` review of `f25e039`'s persisted report contents beyond confirming it adds only
  `reviews/README.md` + the fifth and sixth reports.
- The `MagicMock(spec=TaskRuntimeService)` / `__class__`-lie route around the `isinstance` gate.
- The frontend `vitest` drift suite: the mirror was confirmed field-by-field by hand, not executed.

> **Parent note 1.** Two of the three Minor findings do not hold against the frozen revision, and
> I checked rather than "fixing" them. M-1: at `cc94652` the comment lists **all five** providers —
> `git show cc94652:backend/commercial_gateway.py` lines 2202-2203 read
> "`copilot` / `gmi` / `huggingface` / `kimi-coding` / `kimi-coding-cn` 五个 provider", and that
> file is byte-identical to HEAD. M-3: `progress.md:64` contains **no test count at all**; the only
> count near there is in `4877985`'s commit message, which says 48 and is correct for that commit
> (the test count at `8331715` is 39 functions / 47 tests, as this reviewer itself measured).
> M-2 held and is fixed. The verdict — 0 Critical / 0 Important — is unaffected either way.

> **Parent note 2.** This PASS does not close the stage. The seventh review, running against the
> neighbouring revision `8331715`, found an Important the three scoped checks here did not cover:
> the cancellation guards caught only our own exception types, while `knowe_storage/_sqlite.py`
> re-raises raw `sqlite3.*`, so a real storage failure still replaced the caller's cancellation.
> That was fixed after this review and is recorded in `progress.md` and in the acceptance file.
