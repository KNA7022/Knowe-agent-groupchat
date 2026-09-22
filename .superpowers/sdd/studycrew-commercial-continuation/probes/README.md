# Probes

Reusable measurement scripts that back claims made in `../progress.md` and
`../TASK2-ROUND2-ACCEPTANCE.md`.

They live in the repository because they were previously kept in `%TEMP%`, and a temp cleanup
deleted them along with the baseline files they were compared against. One of them then produced
**false evidence** — see below — which is exactly the kind of thing worth keeping where it can be
re-read.

## `probe-sqlite-cancel.py`

Answers one question: when the store fails underneath a cancellation, does the caller still get
its own cancellation, or does a `sqlite3.*` exception take its place?

```powershell
# against the working tree
py -3.12 .superpowers/sdd/studycrew-commercial-continuation/probes/probe-sqlite-cancel.py

# the control run that makes the measurement meaningful
git archive 8331715 | tar -x -C $env:TEMP\pre-fix-8331715
py -3.12 .../probe-sqlite-cancel.py $env:TEMP\pre-fix-8331715
```

Measured results:

| scenario | `8331715` (before the fix) | working tree (after) |
|---|---|---|
| tool path, database closed inside the effect | `sqlite3.ProgrammingError` | `asyncio.CancelledError` |
| tool path, another connection holds the write lock | `sqlite3.OperationalError: database is locked` | `asyncio.CancelledError` |
| provider path, `chat_stream.aclose()` | `sqlite3.ProgrammingError` | clean end, no exception |

The **control column is the point**: without it, "the working tree passes the probe" would only
show that the probe runs.

### The false-evidence bug, kept as a warning

The first committed version of this probe did not check that the path it was pointed at exists.
When the `%TEMP%` export it was aiming at had been deleted by a cleanup, `sys.path` quietly
ignored the missing entry, `backend` was imported from `PYTHONPATH` instead, and the
"control run against the pre-fix revision" printed the **post-fix** result — a wrong answer
presented as a controlled comparison.

It now refuses to run against a path with no `backend/commercial_gateway.py`, and prints the
`__file__` of the module it actually loaded. If you extend this directory, do the same: a probe
that cannot say which tree it measured is not evidence.

### Why these are not shipped as ordinary tests

They close a **real** database connection, so each needs its own process and a disposable
directory. The pytest suite covers the same mechanism with injected exception types
(`backend/tests/test_task2_round2_mcp_security.py`,
`test_tool_cancellation_survives_a_failing_terminal_write`) — cheaper, CI-friendly, and weaker.
Both exist on purpose; do not delete one because the other passes.
