# Narrow review — does `ddddaad` close the sqlite-vs-cancellation Important?

> **Provenance.** Text as returned by the reviewer to the parent agent, from a
> `git archive ddddaad` export. Parent annotations are confined to the blockquotes marked
> **"Parent note"** at the end.

## Verdict
**CLOSED** (tool path measured closed) — in a `git archive ddddaad` export, with the effect
closing the real database, the tool path hands back `CancelledError`; reverting the guard to
the narrow tuple immediately reproduces "store exception replaced the cancellation", and the
shipped test's two sqlite3 parameters fail. The provider path I could **not** independently
falsify — reported as an observation only.

## Evidence
Export: `%TEMP%\sc_ddddaad` (CRLF). Two probes over real paths (real store, real authority;
only the effect and the socket layer are replaced):

| path | `ddddaad` (wide guard) | mutated back to the narrow tuple |
|---|---|---|
| tool `_invoke` | `asyncio.CancelledError` ✅ | `sqlite3.ProgrammingError`, chain `[ProgrammingError ← CancelledError]` ❌ |
| provider `chat_stream`/`aclose` | `asyncio.CancelledError` ✅ | `asyncio.CancelledError` (could not separate the guard width) |

Confirmed that `knowe_storage/_sqlite.py:317-326`'s `transaction()` really does
`except BaseException: rollback; raise`, so raw `sqlite3.*` is not wrapped — the mechanism
described in the brief holds.

**One brief assumption does not hold**: the worktree is not byte-identical to `ddddaad`.
`git diff` shows 9 insertions, all comments above `estimated_cost_usd` (the round-14 ledger
change), unrelated to the guards; the test file is indeed identical.

## Mutation
Guard reverted to `(RuntimeConflictError, RuntimeNotFoundError, RuntimeStoreError, ValueError)`
→ `2 failed, 1 passed`; the two sqlite3 parameters fail by name. **Mutation CAUGHT.** Restored
from `git cat-file blob ddddaad:...` (LF), **sha256 `76d140dab4d343d62a9cbf688aa0ab980fafde2ec987ee6c7ce68952404746da`**.
Worktree `git status --porcelain` = 0 changes (read-only constraint held).

## Adversarial follow-up
Could not construct a successful case. In theory `except Exception` misses `BaseException`
and the bare `raise` that follows would re-raise that new exception instead of the original
cancellation; but the production store has no path that raises
`KeyboardInterrupt`/`SystemExit`/a second cancellation inside `cancel_tool`, and my cleanup
probe measured the bare `raise` correctly re-raising the original `CancelledError`.
**Did not construct it; not guessing.**

## Not checked
1. The provider path's guard width was not isolated (`_reconcile_provider_usage` appears to
   absorb the path first).
2. No real concurrent `database is locked` end-to-end construction.
3. The `GeneratorExit` branch of `chat_stream` was not exercised.
4. No full suite (by constraint).

> **Parent note 1 — the reviewer caught a false statement of mine.** I told it the worktree's
> `backend/commercial_gateway.py` was byte-identical to `ddddaad`. It was not: `607e989` had
> added nine lines of comment above `estimated_cost_usd` between the review being written and
> being run. That is the **fourth** time in this task that I asserted a fact I had not checked
> at the moment of asserting it (the others: "no configuration lets a provider egress
> proceed…", "no production call site passes `authority_provider`", and a commit-message test
> count written from memory). The reviewer was right to check rather than trust the brief.

> **Parent note 2 — the two gaps it declared were filled independently, in the opposite
> direction.** Its "Not checked" items 1 and 2 are exactly what my own probe isolated, because
> it closes the database at a different moment (before `aclose()` rather than inside the
> transport). Running the same three probes against a `git archive 8331715` export and against
> the working tree gave:
>
> | scenario | `8331715` (before) | after `ddddaad` |
> |---|---|---|
> | tool path, `db.close()` | `sqlite3.ProgrammingError` | `asyncio.CancelledError` |
> | tool path, **another writer holds the lock** | `sqlite3.OperationalError: database is locked` | `asyncio.CancelledError` |
> | provider path, `aclose()` | `sqlite3.ProgrammingError` | clean end, no exception |
>
> So the provider path *is* affected by the guard width, and `database is locked` *is* covered
> end to end — both of which this review honestly reported it could not establish. Its verdict
> and mine agree on the tool path and on the fix being real; the control row is what makes
> either meaningful.
