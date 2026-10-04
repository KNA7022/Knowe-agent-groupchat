# Finding Verdicts

- **I1 — Invalid incoming amendment erases the valid current card before exact version checks: ADDRESSED.** `_target` now raises the non-destructive `CardStateError` (`backend/task_runtime/bound_cards.py:136`). Only validation of the authenticated persisted proposal translates target loss into `CardScopeRevoked` (`:160`). Inside the transaction guard, new request keys check pending ownership and generation/digest before binding the incoming target (`:296`); existing keys still canonicalize the proposal and compare the recorded request fingerprint (`:301`). Invalid input therefore no longer reaches `_admission`'s lifecycle-erasure branch.
- **Regression evidence:** `backend/tests/test_bound_cards.py:724` adds six real-repository cases covering nonexistent/ambiguous targets with current, stale and conflicting request keys. Assertions preserve the complete approval row, public approval, owner, event journal and original readable card, and assert no grant or other effect. `:757` separately checks that removal of the actual persisted target still revokes and scrubs the card.

# New Breakage in the Fix Diff

- None found. Creation still binds its target through the same validator; failed creation has no existing card to revoke. Resolution fingerprints and tombstone retries are unchanged. Successful amendment retries retain canonical target fingerprints, while conflicting reused keys fail before mutation (`backend/task_runtime/bound_cards.py:132`, `:277`, `:287`, `:296`).
- Counts for this fix round: **0 Critical / 0 Important / 0 new Minor**.

# Out-of-Scope Observations

- Existing **M1** remains controller-deferred and non-blocking: noisy successful frontend/static gates from the original report (`card-state-report.md:92`, fix disposition at `:120`). No new outside-diff issue was identified. Production key/source integration, producer/dispatch, feedback and UI remain subsequent tasks, as already recorded (`card-state-report.md:107`).

# Checks and Evidence

- Read the scoped re-review instructions, retained the same task brief and prior I1 finding, and reviewed the complete supplied fix diff once: `40354c7cb2994c1aff9fb0c527f73ba3432c0b99` → `8804382`.
- The appended report names the actual covering tests and results (`card-state-report.md:124`, `:132`): original behavioral RED **3 failed**, focused GREEN **12 passed**, expanded related regression **209 passed**, and final frozen backend **1722 passed / 5 unchanged skips / 10 subtests passed**. The corresponding terminal summaries are included in the fix diff's `card-state-test-output.txt:177` append. These are implementer-run evidence; the new tests' assertions and fix control flow were independently reviewed.
- No additional test was needed to answer an unresolved fix-specific doubt. No broad suites, outside-diff source reads, git/index operations, production/test edits or subagents were used; only this report was added.

# Verdict

**Fix round: All findings addressed, no new Critical/Important breakage.** I1 is closed. The foundation task now meets the required **0 Critical / 0 Important** review gate within its stated scope; deferred M1 does not block this fix round.
