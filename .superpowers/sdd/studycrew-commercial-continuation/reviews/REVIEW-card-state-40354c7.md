# Spec Compliance

- ❌ Issues found. The foundation satisfies most of the bounded storage, ownership, encryption, lifecycle and concurrency requirements, but an invalid amendment can erase the valid current proposal before version validation. This violates the requirement that amendment compare the current generation/digest before changing state (`backend/task_runtime/bound_cards.py:271`, `:298`, `:98`).
- Review range: `1345ec4abd4f85937a9257983bfc5ed6bb8baff7` → `40354c7`. Counts: **0 Critical / 1 Important / 1 Minor**. The independent acceptance gate is not met.
- ⚠️ Cannot verify from this foundation diff: production DPAPI key composition, an actual RAG content-version fence, governed producer/tool accounting, exact approved dispatch, encrypted Hub/history/TaskEnvelope copies, feedback child admission/egress/cancellation/recovery and renderer routing. These are explicitly subsequent seams in `card-state-report.md:107`; this review does not approve a usable feedback feature or automatic restart execution. Default source/course denial is present at `backend/task_runtime/bound_cards.py:83`, with a regression at `backend/tests/test_bound_cards.py:575`.
- ⚠️ Complete backend/frontend/static results are recorded implementation evidence, not independently rerun results (`card-state-report.md:59`). No broad suite was rerun, as requested. No new provider or worker integration is present in the reviewed diff.

# Strengths

- The private contract bounds proposal fields, rejects reserved model fields, hides sensitive representations and retains bounded typed receipts (`backend/task_runtime/card_state.py:42`, `:87`; `backend/tests/test_bound_cards.py:370`, `:646`). Canonical owner identity is recovered from the approval/run relation, and null legacy rows are not backfilled (`backend/task_runtime/bound_cards.py:118`, `:139`; `backend/tests/test_bound_cards.py:497`).
- The repository reuses the existing MasterKey capability, verifies a persistent sentinel, binds identity and versions into domain-separated AAD, authenticates canonical UTF-8 content and uses keyed equality digests (`backend/task_runtime/card_state.py:168`, `:192`, `:199`, `:226`). Wrong-key/tamper cases and scalar-BLOB rejection have real encryption coverage (`backend/tests/test_bound_cards.py:156`, `:179`, `:697`).
- Creation and exact resolution use the existing transaction boundary with a pre-idempotency guard; generic approval resolution rejects bound state (`backend/task_runtime/store.py:881`; `backend/task_runtime/bound_cards.py:246`, `:332`; `backend/task_runtime/service.py:740`). The amendment/approval winner, rollback, grant retry and restart tests assert persisted behavior (`backend/tests/test_bound_cards.py:430`, `:519`, `:536`, `:550`).
- The Group mutation lock is shared by canonical registry path and participates in actual mutations; deletion notification and card event publication occur after admission guard release (`backend/groups.py:389`, `:405`, `:936`; `backend/tests/test_bound_cards.py:376`, `:411`, `:479`, `:597`). Its process-local limit is stated accurately.
- Terminal/rejection triggers scrub encrypted payloads and remove grants, while read-time scope revocation and expiry are enforced by the backend (`backend/task_runtime/card_state.py:143`, `:154`, `:255`; `backend/tests/test_bound_cards.py:295`, `:330`, `:564`). The exact Group-agent ID fix remains a distinct opaque format and has producer-path and negative PII tests (`backend/task_runtime/contracts.py:98`; `backend/tests/test_task_runtime_rereview_round2.py:402`; `backend/tests/test_bound_cards.py:716`).

# Issues

## Critical (Must Fix)

- None found within this task's scope.

## Important (Should Fix)

- **I1 — Invalid incoming amendment irreversibly invalidates the current valid card.** `backend/task_runtime/bound_cards.py:271` calls `_target` on the incoming proposal before loading the current card or checking the supplied version at `:298`. `_target` raises `CardScopeRevoked` when that incoming target is absent/ambiguous (`:132`); `_admission` treats that exception as revocation of the persisted card and calls `_invalidate_owned` (`:98`). Consequently, even a stale request with generation `0`, a wrong digest and a syntactically valid nonexistent target erases the original ciphertext. The approval remains `pending` and the owner remains `waiting_approval`, but the original proposal can no longer be read or approved. A malformed/model-generated amendment must not have that destructive effect. Separate validation failure of the incoming proposal from revocation of the authenticated existing proposal, and reject stale/conflicting requests before any invalidation attributable to their input. Add a regression asserting unchanged ciphertext, receipts, approval and owner for invalid-target amendments, including stale-version and conflicting-key cases; retain revocation when the original persisted target actually disappears.

## Minor (Nice to Have)

- **M1 — Verification output is not pristine.** `card-state-report.md:92` records unknown-event diagnostics, a local port 8081 connection refusal, Zustand deprecation and Git newline warnings during successful gates. These are reported as existing fixture/configuration noise and do not invalidate the passing gate claims, but obscure new failures. Track cleanup of intentional diagnostic expectations and obsolete fixture setup separately; do not suppress unexpected errors globally.

# Focused Check Evidence

- **Named risk: input validation accidentally invokes lifecycle erasure.** Ran one `py -3.12 -B -` inline diagnostic against a real temporary GroupRepository, TaskRuntimeService, MasterKey and SQLite store. Created a valid planning card, then called `amend` with generation `0`, digest `"0" * 64` and nonexistent `gag_00000000000000000000000000000000`. Output: `invalid amendment: CardScopeRevoked card target no longer available or ambiguous`; `ciphertext erased: True reason: scope_or_expiry_revoked approval status: pending`; `original remains readable: False CardStateError card is unbound or invalidated`; `owner state: waiting_approval`; `grant absent: True`. The check used temporary storage only and did not modify source/tests or invoke a provider.
- **Named risk: encryption/transaction dependency claims could be nominal.** Focused inspection of unchanged `backend/knowe_rag/crypto.py:129`, `:181`, `:201` confirms a 32-byte MasterKey, fresh AES-GCM nonce and HMAC-SHA256 helper. `backend/knowe_storage/_sqlite.py:317` confirms default `BEGIN IMMEDIATE`, rollback and commit boundaries. These checks support the new repository's use of existing capabilities.
- **Named risk: card creation relies on unchanged pending-approval constraints.** Focused inspection of `backend/task_runtime/store.py:296`, `:711`, `:977`, `:992` confirms unique pending approval per run, unique request keys, transaction-local deletion fencing and pending-only approval update. `backend/groups.py:1119` confirms repository-only confirmed deletion enters the newly guarded prepare/mark/commit methods. The existing service and new card service both use `backend.groups`, avoiding a separate enum/lock class in this reviewed integration (`backend/task_runtime/service.py:17`; `backend/task_runtime/bound_cards.py:15`).
- Reviewed the supplied diff in bounded chunks. The initial combined output was truncated, so the missing displayed material was recovered; no changed implementation function was independently reread. Outside-diff inspection was limited to the named dependency/constraint/call-site risks above. No broad tests, source/test changes, git/index operations or subagents were used. Only this report was added.

# Assessment

**Task quality: Needs fixes.**

The storage and transaction foundation is generally well separated and backed by meaningful real-repository tests. I1 is a destructive validation-order defect in the new mutation boundary and must be fixed before the required 0 Critical / 0 Important gate can pass.
