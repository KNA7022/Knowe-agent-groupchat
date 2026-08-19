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
