# Task 3 application integration: local group knowledge vertical slice

## Goal

Connect the existing encrypted RAG modules to the real desktop knowledge center. Existing parser/store/index tests are not evidence of usable product integration. Finish a coherent local import -> encrypted source/chunks -> scoped lexical search -> inspect citation -> delete/restart workflow.

## Binding requirements

- Work in D:/agentwork/StudyCrew-worktree on feature/studycrew-foundation. Base ce03997. UTF-8. Preserve skills/ and unrelated changes.
- Reuse backend/knowe_rag, canonical GroupRepository and existing authenticated HTTP listener. No new parallel approval or group authority. New production HTTP dispatch must be invoked by KnoweServer, not tests only.
- User-selected local import supports existing seven native formats where possible through bytes/in-memory parsing. No renderer-supplied filesystem path reads; limit bytes, supported extension and decompression using existing safeguards. Never create plaintext persistent copies or log filenames/content. Source originals and user-visible filenames/metadata must be encrypted. Backend-generated random IDs only.
- Group and optional course scope are required/validated by authoritative group state; unbound course and unknown/deleted group denied. Archive is read-only. Access checks apply to list/detail/search/delete and again at publication after long import/search. Preserve existing group deletion fences and shared-source references. Delete must remove chunks, originals, manifest, vectors/index/cache; shared sources retained for remaining authorized groups. Do not invent cross-group grants from IDs.
- Local BM25 works without a Key/network. Display lexical/degraded state honestly. Do not silently call cloud embedding/OCR. Scan-only PDF reports needs-vision rather than claiming indexed text. Provider/OCR choice is pending user response; this slice needs neither.
- Rebuild in-memory scoped index from encrypted data on restart; bounded cache and invalidate on source/group changes. Use existing key-protected storage boundary; wrong key fails without overwriting data.
- Add actual knowledge-center UI inside current group/course selection: local file chooser/import state, source list, query/results, citations with exact stored location and source text inspection, deletion confirmation. Keep existing visual tokens and keyboard labels. Switching A->B->A, deleting group and delayed response cannot display stale data; use identity+generation fencing.
- No automatic model-generated answer in this slice. Search results are evidence, not verified answers. Do not label snippet inspection as original PDF highlight if not implemented.
- Tests must exercise real authenticated route dispatch and real encrypted storage plus UI behavior, not only mock a new service. Cover import/search/restart/delete, wrong scope/course, archive/deleted groups, no plaintext body/filename in DB, delayed responses and core old knowledge regression. Node24/Python3.12.

## Scope and report

One implementer owns backend and UI to avoid concurrent modifications. Read applicable skills when working in React/Python. No unrelated refactor. Existing master-key design, old whole-repo failing tests and broader Task4/Task5 remain explicit limitations; don't mark entire product accepted. If integration cannot safely handle sharing/deletion, report a concrete blocker instead of registering routes with weaker permissions.

Write implementation and precise test evidence to task-3-integration-report.md in this same directory. Commit scoped changes after verification, include report, exclude skills/. Return status, commits, concise tests and remaining limitations. An independent task review follows.
