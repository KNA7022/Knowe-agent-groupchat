# P2b2 evidence ledger

Worktree `D:/agentwork/StudyCrew-worktree`; Python 3.12, PowerShell 7, `PYTHONUTF8=1`. Outputs are local inert-transport tests, not external provider calls. BASE `25693e0`.

## Iteration evidence (not the final frozen gate)

- Initial actual-chain fixtures failed at CONFIG patch location; corrected to backend.config.CONFIG.
- Actual empty effect registry exposed WorkerRuntime's `registry or tools` bug; explicit empty selection now preserved. A RuntimeEvent accessor typo was fixed before real-chain GREEN.
- Flat receipt `status: skipped` collided with the P1 clear-query-field validator. Receipt detail is nested under `projection`; no accepted guard was relaxed.
- First real success/wait/provider-failure slice: 3 passed in 2.47s.
- Expanded first restart run: 15 passed, 8 failed. Failure was the assertion that entire child SQL remained equal immediately after actual Engine.start. Existing legacy constructor initialization refreshed harness schema_registry timestamps, advanced migration sequence by seven and created empty delivery tables. The controller approved only this precisely bounded distinction, not a blanket-ignore or a migration rewrite.
- Preserved initial failed assertion: `assert sql_dump(path) == before` raised AssertionError in all eight then-defined crash/reopen phases. This line/result is transcribed from the iteration, not represented as an intact raw capture. The final `protected-worker-p2b2-restart.txt` independently retains exact before/after rows for the nine final phases. Protected content/runtime rows equality and full pure-history dump equality are now independently asserted.
- Intermediate targeted: 31 passed/8 deselected/13.13s; restart8 passed/31 deselected/4.54s; three-file focused103 passed/10 subtests/38.72s.
- A command naming nonexistent `backend/tests/test_worker_gateway_runtime.py` collected no tests; it is not evidence of a pass.
- Final boundary additions first run: 49 passed/1 failed/20.29s. Production actual SQLite readonly failure did reach FAILED, but the assertion queried nonexistent `completion_events` instead of actual `completion_events_v2`. Corrected the test's table name. No production workaround or skip.
- Final three-file focused: 106 passed/10 subtests/40.92s, exit0. Full output retained in `protected-worker-p2b2-focused.txt`.

## Frozen commands

Each pytest log was captured with UTF-8 PowerShell `2>&1 | Tee-Object -FilePath <named evidence>; exit $LASTEXITCODE`.

```text
py -3.12 -m pytest backend/tests/test_protected_worker.py backend/tests/test_worker_completion_v1.py backend/tests/test_protected_events.py -q --tb=short
106 passed, 10 subtests passed in 40.92s

py -3.12 -m pytest backend/tests/test_protected_worker.py -k actual_crash_reopen -q -s --tb=short
9 passed, 41 deselected in 5.02s

py -3.12 -m compileall -q backend
exit 0; no compiler output

git diff --check
exit 0; existing LF-to-CRLF advisories

. ./.tools/node24.ps1
npm run test:foundation
node v24.9.0; 33 passed; 0 failed; 0 skipped; duration_ms 12101.939
verify:runtime: StudyCrew runtime and Windows packaging contract verified
compileall=0 diff-check=0 foundation=0
```

The restart evidence logs print only non-private schema component metadata/sequence counters. For each of nine cases added tables are empty deliveries/delivery_artifacts; changed existing tables are precisely schema_registry/sqlite_sequence. Existing migration_log rows, all other rows/schemas, complete TaskRuntime dump, and post-constructor history SQL must match. No golden-fixture body comparison bypass was used.

Full regression and mirror outputs/results are finalized below. Existing inherited skips/noisy stderr are disclosed; no new skips/exclusions.

## Resume ledger — 2026-10-08 (historical dates above and in raw logs retained)

Recovered additional inherited outputs: `protected-worker-p2b2-focused-final.txt` = 106 passed / 10 subtests / 39.58s; `protected-worker-p2b2-backend.txt` = 1979 passed / 5 skipped / 10 subtests / 237.58s; `protected-worker-p2b2-backend-final.txt` = 1979 passed / 5 skipped / 10 subtests / 233.15s. These are preserved original results, not new execution claims or evidence for later scheduler changes.

Initial resumed frozen selection passed 106 / 10 subtests in 55.38s, mirrors 25 in 31.58s and foundation 33 in 13.134s; compileall/diff-check/runtime verification exited 0. Hashes and logs carry `-resume`. The complete resume backend command was explicitly stopped at 39% with exit 1, before source/test changes, after self-review identified the queued-denial cleanup gap. Its partial `backend-resume` log is not a completed gate. The paired idle assertions already existed and were retained.

Actual scheduler RED (`-queued-fence-red.txt`):

```text
py -3.12 -m pytest backend/tests/test_protected_worker.py -k fence_between_injection -q --tb=short
3 failed, 50 deselected in 2.86s; exit 1
assert legacy_failures == []
Each archive/revoke/pause case instead entered legacy fail_task twice.
```

Moving initial authority validation inside cleanup and excluding selected attempts from the two legacy failure fallbacks produced 3 passed / 50 deselected / 2.47s (`-queued-fence-green.txt`). An added actual post-send scheduler storage-failure test brought the covering slice to 4 passed / 50 deselected / 2.64s (`-scheduler-green.txt`). No private storage guard changed.

Controller-requested owner-capacity proof then strengthened the same queued tests. The actual Runtime scheduler counts all executing statuses against global capacity; private lineage revocation alone does not settle the owner. The initial cleanup-only fix therefore still failed archive/revoke terminal-state assertions (`-slot-red.txt`):

```text
py -3.12 -m pytest backend/tests/test_protected_worker.py -k fence_between_injection -q --tb=short
2 failed, 1 passed, 51 deselected in 2.17s; exit 1
TaskStatus.RUNNING != TaskStatus.FAILED
```

The approved narrow fix retains immutable original ownership and uses the existing trusted finalizer/Verifier for content-free FAILED, without private reads or service/guard changes. Current canonical Group/owner/task/RUNNING are rechecked; closed runtime, PAUSED/terminal and deletion/quarantine retain existing protections. The same tests now assert actual bridge/snapshot statuses, release of global capacity 1 and protection against another owner's IDs injected into the mutable binding view. Additional lifecycle controls assert no terminal mutation for quarantine/shutdown.

```text
py -3.12 -m pytest backend/tests/test_protected_worker.py -k 'fence_between_injection or scheduler_storage_failure or slow_provider_lifecycle or authority_and_lease' -q --tb=short
14 passed, 40 deselected in 7.72s; exit 0
```

Raw result: `protected-worker-p2b2-slot-green.txt`. The earlier intermediate 110-test focused pass (48.63s) and its hashes are retained as `-focused-pre-slot.txt` / `-freeze-pre-slot.txt`; they precede the slot correction. Intermediate `-mirrors-refrozen.txt` / `-foundation-refrozen.txt` (25 / 33 passed) likewise precede that correction. Names are historical evidence only, not final acceptance.

## Final frozen commands — 2026-10-08

The final nine source/test SHA256 values are in `protected-worker-p2b2-freeze-final-gate.txt`. Final raw logs use `-final-gate` to distinguish them from inherited `-final` and intermediate results. No new dependencies, provider network calls, skips, xfails or exclusions. Only model HTTP transport is inert; actual storage/runtime/Engine/Hub/Client paths remain in use.

```text
py -3.12 -m pytest backend/tests/test_protected_worker.py backend/tests/test_worker_completion_v1.py backend/tests/test_protected_events.py -q --tb=short
110 passed, 10 subtests passed in 74.91s (0:01:14); exit 0

py -3.12 -m pytest backend/tests -q
1983 passed, 5 skipped, 10 subtests passed in 361.58s (0:06:01); exit 0

py -3.12 -m compileall -q backend
exit 0; no compiler output

git diff --check -- backend
exit 0; existing LF-to-CRLF advisories

. ./.tools/node24.ps1
node --version
v24.9.0
npm run test -- src/contract/parity.test.ts src/contract/publicEvents.drift.test.ts src/contract/taskRuntime.test.ts
3 files, 25 tests passed; duration 3.77s; exit 0
npm run test:foundation
33 passed; 0 failed; 0 skipped; duration_ms 18191.3074
verify:runtime: StudyCrew runtime and Windows packaging contract verified; exit 0
compileall=0 diff-check=0 mirrors=0 foundation=0
```

Final post-run SHA256 recheck: all nine entries match exactly (`protected-worker-p2b2-post-gate-hashes.txt`), `source_test_freeze_matches=True`. The full regression adds exactly 54 Worker tests to prior accepted 1929; no skips or exclusions were introduced. Existing contract-negative stderr/newline warnings remain disclosed. The old restart evidence contains all nine phase records; only the first creates empty legacy delivery tables, all nine permit only the specifically asserted legacy registry timestamps/sequence differences. No diagnostic, numeric result or date was replaced to manufacture a cleaner result. UTF-8 scan of all current slice reports/logs found no username-bearing profile prefix/account identifier. Independent review, not this local passing gate, decides Unit P acceptance.

Staging the captured text exposed four Vitest trailing blank EOF lines and three pytest RED `E` lines with trailing spaces. The four `mirrors*.txt` files and `queued-fence-red.txt` received only trailing-whitespace/EOF normalization, preserving original UTF-8/BOM status and all diagnostic text/results/dates. Thus those five files are captured output with presentation whitespace normalized, not byte-identical terminal captures. No source/test changed. The complete staged diff-check then passed without an exclusion or waiver.
