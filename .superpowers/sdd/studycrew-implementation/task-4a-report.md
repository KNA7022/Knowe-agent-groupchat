# Task 4A implementation report

## Status

DONE

## Indivisible Knowledge authorization

- `_KnowledgeScope` now retains both the complete Group source/course allowlist and the optional
  narrow request boundary. Authorization recursively collects structured `source_id(s)` and
  `course_id(s)` identities from the whole object graph before any body, summary, evidence,
  related object, profile or skill instruction can be returned or mutated.
- A narrow request requires every contributor to belong to that exact source/course. A broad
  Group request still rejects contributors outside the Group bindings. Aggregate objects are no
  longer converted into permitted-looking metadata shells while keeping foreign-derived text.
- The rule is shared by asset/node list, search and detail routes, Project profile, every
  asset/node/profile write, and skill-pack list/detail/status/review/purge. Skill-pack detail also
  validates ownership carried outside the pack metadata in the enclosing result.
- Cross-project global objects remain visible only with explicit user global scope plus
  `validated/core` and a non-pending/non-retired view. Their complete contributor graph must also
  be clean. The formal matrix proves a clean approved global remains visible while a global with
  a foreign private contributor is excluded.

## Shared durable deletion generation fence

- The renderer deletion fence now tombstones canonical `group_id` and the exact legacy Project
  alias before teardown mutates any store. Both identities persist in localStorage; every new
  deletion also advances a renderer-wide revision used by multi-identity responses.
- Records `load`, `loadMore` and `loadHandoffs` capture the legacy identity generation before
  dispatch and check it on both success and error paths after every await. A known-deleted
  identity performs no dispatch.
- Settings reconciliation captures the deletion revision, discards a response if deletion occurs
  in flight, and filters model/approval maps through durable tombstones. Same-version Zustand
  hydration filters old deleted keys immediately and rewrites the sanitized localStorage state.
- Knowledge list, skill-pack list, asset/profile/skill detail, preview, all asset/skill mutations
  and follow-up reloads bind both the active canonical scope and explicit Project/card/pack
  identity to the same generation fence. This also closes redispatch after teardown has cleared
  `activeScope`.
- Directory recovery events and controls reject tombstoned legacy identities. Legacy snapshot
  requests, menu reconciliation, pin/mute/fold/rename/delete-Agent responses, incremental events,
  WebSocket events and transient conversation paths now share the fence. The legacy permanent
  delete path establishes its own durable alias tombstone after the backend confirms deletion.
- Teardown still clears Task Runtime, Group summaries, Knowledge, records, settings, directory,
  side-task watermarks, conversations, DMs, attachments, timers and request caches. The fence is
  established first, so no late success, error or out-of-order response can refill them.

## Adversarial acceptance coverage

- Backend mixed-object matrix places allowed and foreign sources/courses on the same asset, node
  and skill pack with foreign markers in `body_md`, summary, description, evidence and skill
  instructions. List/search exclude the object; details and every applicable write fail closed;
  serialized responses contain no foreign marker. Narrow profile GET/POST are denied.
- Renderer deferred-response matrix starts records history/page/handoffs, settings reconcile,
  Knowledge asset/profile/pack detail, then deletes the Group before releasing success responses.
  The final serialized stores contain none of the Group, legacy Project or content markers.
- The same tests cover error-safe commit guards, post-delete zero-dispatch, durable restart
  tombstone hydration and settings localStorage sanitization. A separate stress test releases
  1,000 successful Knowledge responses in reverse order after deletion and observes 1,000 null
  results with zero store mutation.
- Existing Task Runtime atomic cursor/snapshot tests remain in the complete frontend suite; this
  Task does not change Task Runtime snapshot production or renderer authority projection.

## Verification evidence

- Frontend full regression: `43 files / 500 tests passed`.
- Exact deletion tests: `4 files / 7 tests passed`, including 1,000 reverse-order late responses.
- Exact Knowledge scope tests: `3 passed`.
- Scoped backend ordinary Group/Knowledge/Task Runtime set: `184 passed`.
- Task Runtime quarantine isolation set: `68 passed`.
- Combined scoped backend long run reached `251 passed / 1 timing-sensitive quarantine failure`;
  the failed exact case immediately passed `1/1`, then its complete file passed `68/68`. This is
  the previously disclosed Windows quarantine PermissionError/timing flake, not a Task 4A path.
- `npm run lint`, Electron TypeScript compile, Node command tests `2/2`, Python compilation and
  `git diff --check` passed.
- Runtime alignment verified with Node `v24.19.0` and Python `3.12.10`.

## Existing repository boundary

The full renderer `npm run typecheck` still reports the previously documented baseline errors in
legacy test fixtures, old UI files, PDF worker typing and older store strictness. Task 4A's one
initial test-only unused-parameter diagnostic was fixed; no Task 4A production file adds a new
diagnostic. Electron TypeScript compilation passes independently. This Task does not broaden into
the planned baseline cleanup or Task 5 Tool/PII/RAG work.

## Independent review fix round 1/5

### Status

DONE

### Unconditional global-object policy

- `scope=global` now always requires `scope_set_by=user`, `validated/core`, and a
  non-pending/non-retired view, including when the supplying Project belongs to the current Group.
  Project ownership can no longer bypass global approval.
- The exact matrix adds an own-Project pending global marker and proves it is absent from lists,
  denied on detail and denied on write. Clean approved global data remains allowed; approved
  global data with a foreign private contributor remains excluded.

### Original-scope mutation fence

- All six Knowledge mutation families capture an immutable canonical scope, its scope identity,
  and the complete deletion generation before dispatch: skill review/status/purge and asset
  override/review/purge.
- Every success, failure, local mutation and follow-up reload rechecks that original fence and
  active scope. Reloads explicitly carry the captured scope; they never consult a teardown-reset
  `activeScope=null`. A late rejection therefore performs no second unscoped request and writes no
  error into the cleared Knowledge projection.
- The formal matrix covers every mutation with both a late successful response and a late
  rejected response after deletion: `12/12` operations return fail-closed with one request only.

### Records identity and request epochs

- The records singleton now holds its active legacy/canonical identity, an identity epoch and
  independent history/handoff request epochs. `load`, `loadMore`, `loadHandoffs` and both success
  and error updaters require identity, epoch and deletion generation equality.
- A Group switch invalidates both lanes and clears the old projection. Category/date changes
  invalidate history; close, reset and confirmed teardown invalidate the identity and both lanes.
  Teardown checks both exact legacy and canonical identities without clearing an unrelated active
  Group.
- A/B reverse-order tests prove late A load, loadMore error and handoff success cannot replace B
  content or error state.

### Synchronous confirmed-delete fence and retryable cleanup

- After validating the backend deletion receipt, `confirmedDelete` synchronously fences the
  captured canonical and legacy identities before the dynamic teardown-module import can yield.
- The cleanup obligation remains in an in-memory retry map until teardown succeeds. A failed
  module import leaves both durable tombstones intact; the next Group load or an explicit retry
  resumes cleanup. Tests pause the import boundary while a legacy history response resolves, and
  separately force import failure then recovery.

### Corrected verification evidence

- Frontend full regression: `45 files / 517 tests passed`.
- Exact Task 4A renderer set: `7 files / 37 tests passed`; the review's broken `_input/input`
  fixture and both new-test TypeScript diagnostics are fixed.
- Knowledge scope matrix: `3/3 passed`.
- Scoped backend ordinary partition: `184/184 passed in 47.97s`.
- Task Runtime quarantine partition: clean isolated rerun `68/68 passed in 29.34s`.
- `npm run lint`, Electron TypeScript compilation, Node command tests `2/2`, Python compilation
  and `git diff --check` pass. Node is `v24.19.0`; Python is `3.12.10`.
- Full renderer typecheck still exits on the previously disclosed historical baseline, but its
  output contains zero diagnostics for every Task 4A round-1 production or test file.

## Independent review fix round 2/5

### Status

DONE

### Object-bound Knowledge requests

- Renderer detail/profile/skill-pack and all six mutation families now require exactly one active
  canonical Group and prove the addressed Project belongs to its captured legacy Project set
  before dispatch. Asset contributor source/course identities must be subsets of the narrow scope;
  global assets require explicit user approval plus validated/core live state. Approved immutable
  system packs retain their controlled global path.
- A monotonic scope epoch supplements the durable deletion generation. A request started in A is
  stale after A→B→A even when its serialized scope key matches again. Detail, profile, pack and
  mutations capture this token and recheck it after every await.
- Exact tests prove stale A objects under live B perform zero network dispatch, and foreign-source,
  foreign-course and unapproved-global assets fail before detail or mutation dispatch.

### Nested continuation fence

- Skill review/status and asset/pack override/review/purge now recheck the original scope epoch,
  object identity and deletion generation after every awaited reconciliation. A nested reload can
  neither turn a deleted/switched operation into `true` nor commit stale data.
- The matrix covers deletion and scope switching across successful, rejected and catch-triggered
  nested reloads. An A→B→A detail response is also rejected.

### Synchronous receipt-boundary purge

- The fallible dynamic teardown import and renderer-local pending map were removed. A static
  `teardownGroupProjections` core is available at the backend receipt boundary and synchronously
  purges Task Runtime, side-task, Knowledge, records, settings, directory, conversations, DMs,
  attachments, transient legacy caches and skeletons before `confirmedDelete` resolves.
- A narrow runtime bridge removes the former TaskRuntime→Group module dependency, so the static
  teardown core does not recreate the Group-store import cycle. Group metadata and summary
  mirroring semantics remain covered by the existing Task Runtime suite.
- Because no asynchronous cleanup obligation remains after a successful receipt, no pending
  cleanup persistence or startup retry protocol is required. Tests seed old conversations,
  records and a deferred history response and prove all content is invisible immediately and
  cannot revive.

### Durable skeleton fence

- Skeleton load/save/enumeration self-delete tombstoned keys. Direct incremental fetch captures
  the deletion generation before network I/O and rechecks it before parsing/returning.
- Both canonical and legacy skeleton keys are cleared during teardown. A simulated restart with a
  manually restored stale localStorage key produces zero network calls, zero projection writes
  and removes the key, while ChatStream unmount flushes inherit the guarded save path.

### Round-2 verification evidence

- Exact renderer set after the final scope-epoch/bridge changes: `7 files / 80 tests passed`.
- Full frontend regression: `45 files / 539 tests passed`.
- Scoped backend ordinary partition: `184/184 passed in 45.79s`.
- Task Runtime quarantine partition: `68/68 passed in 31.08s`.
- `npm run lint`, Electron TypeScript compilation, Node command tests `2/2`, Python compilation
  and `git diff --check` pass. Node is `v24.19.0`; Python is `3.12.10`.
- Full renderer typecheck retains only the documented historical baseline; filtering its output
  for every round-2 production/test file returns zero diagnostics.

## Independent review fix round 3/5

### Status

DONE

### Positive contributor proof for narrow Knowledge scope

- `scopedKnowledgeObjectAllowed` now requires a non-empty, matching contributor set in every
  active narrow dimension: source-scoped requests require one allowed source and no foreign
  source; course-scoped requests require the exact course and no foreign course. This makes an
  ownerless Profile, Asset, or SkillPack fail closed before any detail or mutation request.
- The same gate is now applied to Asset and SkillPack list responses, and Profile availability is
  suppressed for a narrow request because an aggregate profile has no row-level contributor proof.
  Detail responses are re-authorized from their returned Asset/SkillPack metadata before a body
  can be exposed, closing a malformed or stale response path.
- A broad Group scope deliberately remains unchanged for an explicitly user-approved,
  validated/core, live global Asset with no private contributor: it remains readable.

### Regression and acceptance matrix

- Preserved and extended the untracked `src/store/task4aRound2Adversarial.test.ts` rather than
  replacing it. Before the production change, its original reproduction failed `2/3`: ownerless
  Profile/Asset/SkillPack detail requests exposed private bodies, and all six ownerless writes
  returned `true`. After the change, it passes and retains the no-dispatch assertions.
- The added route matrix proves narrow Asset/SkillPack lists retain only fully matching rows,
  suppresses narrow Profile availability and dispatch, accepts matching Asset/SkillPack details,
  rejects ownerless/foreign entries, and preserves broad clean approved-global Asset detail.

### Verification evidence

- Exact Task 4A renderer set: `7 files / 46 tests passed`.
- Knowledge authorization backend matrix: `3/3 passed`.
- Task Runtime backend matrix: `214/214 passed in 71.28s`.
- Full frontend regression: `46 files / 543 tests passed`.
- `npm run lint`, Electron TypeScript compilation, Node foundation tests `2/2` plus runtime
  contract verification, Python `compileall`, and `git diff --check` passed.

## Independent review fix round 4/5

### Status

DONE

### Fail-closed contributor authorization

- Broad and narrow Group scopes now require positive source/course contributor proof for every
  indivisible Project Profile, Asset and SkillPack. The only empty-contributor exception is an
  explicitly user-approved, validated/core, live global object with no private contributors.
  Immutable/system SkillPacks, merely active packs and ordinary Project-owned objects are not
  exceptions. An explicitly approved but `view_state=pending` pack is also denied.
- The renderer applies this rule before Profile/Asset/SkillPack detail or preview dispatch, while
  filtering Profile availability and Asset/SkillPack list rows. The same pre-dispatch gate covers
  Asset override/review/purge and SkillPack review/status/purge. Detail responses are authorized
  again from their returned contributor metadata before their bodies are exposed.
- The backend uses the same strict exception and contributor predicate for merged lists, Profile
  availability/detail/update, Asset detail/writes and SkillPack list/detail/writes. Broad scope no
  longer treats an empty contributor set as harmless.

### Contributor data-model closure

- Profile was previously represented only as a Project ID/string and could not prove ownership.
  Asset snapshots now derive the exact source/course union of an auto-compiled Profile, return it
  with availability/detail responses, and the renderer caches it per Project. A locked/free-form
  Profile has no trustworthy row-level provenance and intentionally remains ownerless.
- SkillPack transport previously discarded approval and contributor fields. Project-experience
  registry rows now inherit real source/course IDs, scope setter, source-asset approval state and
  review state; public rows and the renderer mapping preserve those fields. System/third-party
  packs do not acquire synthetic contributors.
- Existing positive fixtures were corrected instead of weakening assertions:
  `knowledge.test.ts` uses contributor-backed Project Assets/packs and Profile preview metadata;
  `groupDeletionAsync.test.ts` gives all deferred/stress Profile, Asset and pack requests real
  contributors; `knowledgeMutationDeletion.test.ts` uses full contributor-backed objects across
  all six mutation continuations; the backend base fixture derives pack contributors from its
  source rows. Matching narrow Profile/Asset/SkillPack paths remain positively covered.

### Red-first evidence

- Before the production change:

  `npx vitest run src/store/task4aRound2Adversarial.test.ts --reporter=verbose`

  ```text
  Test Files  1 failed (1)
       Tests  2 failed | 4 passed (6)
  ```

  The broad ownerless Project case returned an ownerless Asset, and the global/system case
  returned an immutable ownerless system pack.
- The strict live-state follow-up was also demonstrated red before its mapping fix:

  ```text
  Test Files  1 failed (1)
       Tests  1 failed | 5 passed (6)
  AssertionError: expected [ { …(18) } ] to deeply equal []
  ```

  The unexpected row was an approved `status=active`, `view_state=pending` global pack whose
  renderer `st` had incorrectly been inferred as `ok`.
- Direct backend reproduction before its production change:

  `py -3.12 -m pytest backend/tests/test_knowledge_group_scope.py::test_broad_ownerless_objects_fail_closed_except_strict_clean_global -q`

  ```text
  1 failed
  ```

  The broad Asset list still contained the ownerless Project row. The same command passed after
  the fix.

### Adversarial and regression verification

- Broad/narrow adversarial route matrix after the final live-state fix:

  `npx vitest run src/store/task4aRound2Adversarial.test.ts --reporter=verbose`

  ```text
  Test Files  1 passed (1)
       Tests  6 passed (6)
  Duration  1.43s
  ```

- Exact Task 4A renderer set:

  `npx vitest run src/store/groupDeletionAsync.test.ts src/store/groupDeletionFence.test.ts src/store/groupTeardown.test.ts src/store/knowledge.test.ts src/store/knowledgeMutationDeletion.test.ts src/store/skeletonCache.test.ts src/store/task4aRound2Adversarial.test.ts --reporter=dot`

  ```text
  Test Files  7 passed (7)
       Tests  54 passed (54)
  Duration  3.04s
  ```

- Knowledge backend authorization/graph set:

  `py -3.12 -m pytest backend/tests/test_knowledge_group_scope.py backend/tests/test_knowledge_graph.py -q`

  ```text
  16 passed in 1.87s
  ```

  The real-manager contributor projection test alone also returned `1 passed in 0.20s`.
- Related ordinary backend partition:

  `py -3.12 -m pytest -q backend/tests/test_groups.py backend/tests/test_group_http.py backend/tests/test_knowledge_graph.py backend/tests/test_knowledge_group_scope.py backend/tests/test_task_runtime.py backend/tests/test_task_runtime_review_fixes.py backend/tests/test_task_runtime_rereview_round2.py backend/tests/test_task_runtime_rereview_round3.py backend/tests/test_task_runtime_rereview_round4.py backend/tests/test_task_runtime_rereview_round5.py backend/tests/test_task_runtime_live_event_bridge.py backend/tests/test_task_runtime_recovery_ack.py backend/tests/test_task_runtime_recovery_ownership.py backend/tests/test_task_runtime_deletion_authorization.py`

  ```text
  186 passed in 44.39s
  ```

  `py -3.12 -m pytest backend/tests/test_task_runtime_quarantine_isolation.py -q`

  ```text
  68 passed in 31.56s
  ```

- Full frontend regression after the final production/test changes:

  `npm test -- --reporter=dot`

  ```text
  Test Files  46 passed (46)
       Tests  545 passed (545)
  Duration  24.02s
  ```

### Static, runtime and baseline evidence

- `npm run lint`: exit `0`, zero warnings.
- `npx tsc --noEmit -p electron/tsconfig.json`: exit `0`, no output.
- `npm run test:foundation`: Node tests `2` passed / `0` failed, followed by
  `✅ StudyCrew runtime and Windows packaging contract verified`.
- `py -3.12 -m compileall -q backend`: exit `0`, no output.
- Node is `v24.19.0`; Python is `3.12.10`.
- Full renderer `npx tsc --noEmit` retains the documented historical baseline:
  `renderer_tsc_exit=2`, `renderer_tsc_diagnostic_lines=68`,
  `task4a_changed_file_diagnostics=0`.
- Full backend aggregation `py -3.12 -m pytest backend/tests -q` stops during collection on the
  pre-existing Task 9 baseline: four modules (`test_v10182_runtime_ui.py`,
  `test_v22_dispatch_reliability.py`, `test_v27_weld.py`, `test_v28_weld.py`) cannot import
  `ACTION_CONTRACT` from `backend.engine` (`4 errors in 1.83s`). The scoped partitions above are
  fully green.
- `git diff --check` passes after the report update. The pre-existing untracked `skills/`
  directory was neither read, modified, staged nor committed.

## Independent review fix round 5/5

### Status

DONE

### Project-specific Profile availability authorization

- `GET /api/knowledge/{project}/assets` no longer spreads the manager's raw snapshot into the
  scoped HTTP response. It emits an explicit response field set and therefore cannot accidentally
  transport a private Profile marker or a future unreviewed snapshot field.
- The merged and project-specific Asset routes now call the same `_profile_authorization` helper.
  That helper constructs one canonical Profile row and applies `_row_allowed`; a denied Profile is
  represented only as `profile_exists=false` with empty source/course arrays.
- The formal route test proves a foreign-only Profile is hidden by both list forms, its exact
  source/course IDs and a synthetic private marker are absent from the direct serialized response,
  and a subsequently matching same-Group Profile remains positively visible with its real IDs.

### Atomic Profile body and contributor generation

- `KnowledgeAssetManager.profile_snapshot` is a single manager operation guarded by the existing
  per-root `KnowledgeGraphManager.root_lock`, the same lock already held by `set_profile` and every
  `_after_write` Profile compilation. While that lock is held, its synchronous worker reads the
  Profile availability/contributor snapshot and body as one coupled result.
- Profile GET now uses a dedicated async HTTP branch, awaits only that coupled result, then applies
  the shared Profile row authorization. It never performs the former post-authorization
  `profile_text` read. Engine-loop identity and a bounded snapshot timeout remain fail closed.
- The deterministic real-manager concurrency test pauses after the allowed contributor snapshot,
  starts a real `set_profile` replacement with `PRIVATE OWNERLESS PROFILE`, and proves the writer
  remains blocked on the same root lock. The first response contains only the old allowed body and
  matching IDs; after the writer completes, a second request returns 403 and no private marker.

### Red-first evidence

Before the production change, both permanent attack tests failed with the exact disclosures:

`py -3.12 -m pytest backend/tests/test_knowledge_group_scope.py::test_project_assets_route_sanitizes_denied_profile_metadata backend/tests/test_knowledge_group_scope.py::test_real_manager_profile_detail_body_and_contributors_share_one_generation -q`

```text
FF                                                                       [100%]
FAILED ...test_project_assets_route_sanitizes_denied_profile_metadata
E assert True is False
FAILED ...test_real_manager_profile_detail_body_and_contributors_share_one_generation
E 'PRIVATE OWNERLESS PROFILE' is contained here:
E 'text': 'PRIVATE OWNERLESS PROFILE\n', 'source_ids': [...],
E 'course_ids': ['course-atomic']
2 failed in 0.73s
```

The same two-test command after the fix returned:

```text
..                                                                       [100%]
2 passed in 0.51s
```

The final strengthened real-manager test (including writer-blocked and post-swap 403 assertions)
returned:

```text
.                                                                        [100%]
1 passed in 0.51s
```

### Backend verification

- Complete Knowledge Group scope, including broad/narrow Profile, Asset and SkillPack positives,
  ownerless rejection and the strict clean-global exception:

  `py -3.12 -m pytest backend/tests/test_knowledge_group_scope.py -q`

  ```text
  .......                                                                  [100%]
  7 passed in 0.77s
  ```

- Related ordinary Group/Knowledge/Task Runtime partition:

  `py -3.12 -m pytest -q backend/tests/test_groups.py backend/tests/test_group_http.py backend/tests/test_knowledge_graph.py backend/tests/test_knowledge_group_scope.py backend/tests/test_task_runtime.py backend/tests/test_task_runtime_review_fixes.py backend/tests/test_task_runtime_rereview_round2.py backend/tests/test_task_runtime_rereview_round3.py backend/tests/test_task_runtime_rereview_round4.py backend/tests/test_task_runtime_rereview_round5.py backend/tests/test_task_runtime_live_event_bridge.py backend/tests/test_task_runtime_recovery_ack.py backend/tests/test_task_runtime_recovery_ownership.py backend/tests/test_task_runtime_deletion_authorization.py`

  ```text
  188 passed in 48.08s
  ```

- Task Runtime quarantine first run hit the existing Windows retry-timing flake, outside all
  Profile paths:

  `py -3.12 -m pytest backend/tests/test_task_runtime_quarantine_isolation.py -q`

  ```text
  FAILED ...::test_limit_two_overflow_never_starves_valid_confirmed_deletion
  E AssertionError: condition was not satisfied before timeout
  1 failed, 67 passed in 34.04s
  ```

  Its exact immediate rerun returned `1 passed in 0.71s`; the complete clean isolation rerun
  returned `68 passed in 31.88s`.

### Frontend and static verification

- Exact Task 4A renderer set:

  `npx vitest run src/store/groupDeletionAsync.test.ts src/store/groupDeletionFence.test.ts src/store/groupTeardown.test.ts src/store/knowledge.test.ts src/store/knowledgeMutationDeletion.test.ts src/store/skeletonCache.test.ts src/store/task4aRound2Adversarial.test.ts --reporter=dot`

  ```text
  Test Files  7 passed (7)
       Tests  54 passed (54)
  Duration  3.30s
  ```

- Full frontend regression, `npm test -- --reporter=dot`:

  ```text
  Test Files  46 passed (46)
       Tests  545 passed (545)
  Duration  27.15s
  ```

- `npm run lint`: exit `0`, zero warnings.
- `npx tsc --noEmit -p electron/tsconfig.json`: exit `0`, no output.
- `npm run test:foundation`: Node tests `2` passed / `0` failed, followed by
  `✅ StudyCrew runtime and Windows packaging contract verified`.
- `py -3.12 -m compileall -q backend`: exit `0`, no output.
- Node is `v24.19.0`; Python is `3.12.10`.
- `git diff --check`: exit `0` (only Git's configured LF→CRLF working-copy notices).

### Recorded historical baselines

- Full backend aggregation, `py -3.12 -m pytest backend/tests -q`, remains stopped only by the
  pre-existing Task 9 collection baseline: four modules (`test_v10182_runtime_ui.py`,
  `test_v22_dispatch_reliability.py`, `test_v27_weld.py`, `test_v28_weld.py`) cannot import
  `ACTION_CONTRACT` from `backend.engine` (`4 errors in 1.50s`). This round does not expand into
  that unrelated contract restoration.
- Full renderer `npx tsc --noEmit` retains the disclosed historical baseline: process exit `1`,
  `68` non-empty output lines containing `54` TypeScript diagnostics, and zero diagnostics for
  this round's changed files (all changed production/test files are Python).
- The pre-existing untracked `skills/` directory was not read, modified, staged or committed.
