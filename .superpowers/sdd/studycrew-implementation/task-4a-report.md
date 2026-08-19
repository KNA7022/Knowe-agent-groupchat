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
