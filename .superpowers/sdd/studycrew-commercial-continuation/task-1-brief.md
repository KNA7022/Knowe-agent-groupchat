# Task 1: Close atomic Profile authorization (Task 4B)

- Add a generation-consistent `ProfileBundle` whose body, availability, contributors, locked state and generation token are read under one existing `root_lock` critical section.
- Make merged and project-specific Assets routes consume only lock-bound Profile availability; no unlocked general snapshot may authorize Profile fields.
- Make Profile POST read the current generation, authorize, compare the generation and mutate within one lock boundary. Stale authorization must fail closed and never overwrite a newer generation.
- Add deterministic barrier tests for availability, GET, POST, auto-compilation, deletion and concurrent generation swaps.
- Re-run Knowledge/Task Runtime backend partitions, Task 4A frontend set, full frontend and static gates.

## Binding context

- Baseline is commit `3a35d8b`.
- Prior independent evidence identified two exact races to reproduce formally:
  1. merged/direct Assets availability reads the graph and `PROFILE.md` through an unlocked general snapshot. A concurrent `set_profile()` can replace an allowed auto Profile with a locked ownerless generation while the read is paused, yet the first response still emits `profile_exists=true` and stale allowed contributor proof.
  2. Profile POST authorizes an unlocked old snapshot, then separately enters `set_profile()`. A concurrent writer can replace the current Profile with an ownerless generation between those steps, after which the stale-authorized POST overwrites the newer generation.
- Existing `KnowledgeGraphManager.root_lock()` is the authoritative lock and must remain safe on the registered engine event loop.
- Preserve all already-passing Profile detail GET generation consistency and strict ownerless/global contributor policy.
- Do not begin Task 2.
