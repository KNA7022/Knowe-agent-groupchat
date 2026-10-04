# P2b2a actual execution evidence

Working directory: `D:/agentwork/StudyCrew-worktree`. Python 3.12; Windows PowerShell UTF-8. Entries below are actual relevant stdout/stderr excerpts, not synthetic complete logs. Frozen source/testing gate ran once; earlier focused iterations are separately labeled. No new skips/exclusions.

## Initial accepted-P1/P2b1 regression

Command: `py -3.12 -m pytest backend/tests/test_protected_storage.py backend/tests/test_protected_events.py -q --tb=short`

```text
126 passed in 31.88s
```

## Genuine startup RED (before narrow recovery fix)

Command: `py -3.12 -m pytest backend/tests/test_protected_children.py -q --tb=short`

```text
test_quarantine_retains_unrevoked_body_and_revoked_cleanup_retries
TaskRuntimeService(...) service.py:164 self.recovery_report.update(self.recover_after_restart())
service.py:2174 recovered, _event, _created = self.store.change_run(...)
store.py:880 self._assert_group_mutable_in_transaction(...)
store.py:716 raise RuntimeStoreError
RuntimeStoreError: Task Runtime group is fenced for deletion: grp_8144f084f79e4ea5ac3a3737a2631ac4
1 failed, 27 passed in 7.68s
```

This was actual RUNNING + invalid-proof quarantine reopening, not a mocked constructor. A temporary test setup pause was rejected by the controller; final regression retains original RUNNING. Fix preserves fence/status, reports backend limitation and does not swallow unknown errors. Other early failures involved typed fixture construction and are not claimed as product RED.

## Focused GREEN and final child matrix

```text
child + quarantine iteration:
102 passed in 37.98s

py -3.12 -m pytest backend/tests/test_protected_children.py -q --tb=short
39 passed in 9.77s
```

Command (PYTHONUTF8=1): `py -3.12 -m pytest backend/tests/test_protected_children.py backend/tests/test_protected_storage.py backend/tests/test_protected_history.py backend/tests/test_protected_events.py backend/tests/test_task_runtime_quarantine_isolation.py -q --tb=short`

```text
274 passed in 81.00s (0:01:21)
```

## Single frozen complete backend gate

Command (PYTHONUTF8=1): `py -3.12 -m pytest backend/tests -q`

Actual session 81242, exit 0; last tool output:

```text
....... [ 93%]
........................................................................ [ 96%]
...........................................................    [100%]
1926 passed, 5 skipped, 10 subtests passed in 216.66s (0:03:36)
```

Baseline 1887 + 39 new cases. Existing 5 skip/10 subtest accounting retained, no exclusions added.

## Node 24 contract mirrors

Command: `& .\.tools\node24.ps1 -Command 'npm test -- src/contract/parity.test.ts src/contract/publicEvents.drift.test.ts src/contract/taskRuntime.test.ts'`

```text
Node v24.9.0
vitest v1.6.1
Test Files  3 passed (3)
Tests       25 passed (25)
Duration    3.53s
```

Exit 0. Expected negative-contract stderr covered Group mismatch, stream_delta missing content, tool_gen missing tool_name, missing sequence, missing approval card and unknown turn_end. No TS source changes. No system Node 26 gate or new runtime download.

## Compile / diff / frozen SHA256

`py -3.12 -m compileall -q backend`: no output, exit 0.

`git diff --check`: exit 0. Existing Git advisory: `LF will be replaced by CRLF the next time Git touches it` for two modified Python files and controller-owned Markdown; not a whitespace failure.

```text
8027312C36DEB8ABCCA6465F97F15CAB73929EB335096B01BA6183AAF2F791FB  backend/protected_children.py
C8732C7CF5B3F1A2A10A197FE66B06036942B8ED07C90522A099070FDDF6B5AC  backend/protected_storage.py
54A2C2807CF47744681C6E7A8D2B8382EE4006FDB5AC308D666455E43003F018  backend/task_runtime/service.py
D9E96E531B107CA88420ED6F8FC2622B3FCE48F16070A702FEDA67F91C5C55D6  backend/tests/test_protected_children.py
```

## Supplementary actual two-Group probe

Ran Python here-string through `py -3.12 -`, with PYTHONPATH containing worktree/backend and worktree. Imported the committed fixture helpers; created real temporary GroupRepository/TaskRuntime/key, enrolled two canonical distinct project Groups, wrote envelope+started journal for each, used actual prepare/clean/mark/commit/complete deletion for the first, asserted all first child artifacts absent, second envelope goal unchanged and second catalog descriptor unchanged. Finally closed runtime and collected unreachable test-only native SQLite assertion handles before TemporaryDirectory exit.

Actual final stdout, exit 0:

```text
actual group deletion: selected enrolled started lineage fully cleaned; unrelated enrolled group remains readable; unrelated discovery retained
temporary directory cleanup complete
```

Earlier same behavioral probe printed the successful assertion line but exited 1 during TemporaryDirectory teardown:

```text
PermissionError: [WinError 32]
<TEMP>\tmpqu2ebzal\project_20261004000001\runtime\runtime.sqlite3
```

The test helper's `with sqlite3.connect(...)` ends the transaction, not necessarily connection lifetime; explicit post-close `gc.collect()` resolved the standalone probe teardown. No production/source edits occurred after freezing. An intermediate retry without test PYTHONPATH failed immediately with `ModuleNotFoundError: No module named 'knowe_core'`; corrected invocation above passed. A requested removal of the exact leftover disposable `tmpqu2ebzal` directory was policy-rejected before execution; no bypass attempted. It may remain under the user's Temp directory, containing synthetic test data only.

## Earlier P2b2 scope-discovery probe excerpts

Actual store/Hub/runtime close/reopen probe, both before/after DELIVERY ACK: recovered owner PAUSED, selected paused-history answer readable, child SQL unchanged, claims unchanged; absent/present delivery receipt respectively. Existing `finalize_trusted_execution` refused PAUSED. Controller chose explicit recovery without late bookkeeping.

Actual FAILED/CANCELLED finalize probe: body readable before finalization; afterward `ProtectedStorageError: protected owner revoked`. Existing public TaskRuntime status is the selected future result surface; P2b2 bridge/Client integration is not claimed by these probes. They justified the bounded split and are not counted among the 39 new tests.

## Review fix round 1 — actual RED → GREEN

Fix base `97332fbe689289018e54e1a5fc292fd89d5e9f0d`. I1 startup composition regression added before production fix. Command (PYTHONUTF8=1):

`py -3.12 -m pytest backend/tests/test_protected_children.py -q --tb=short -k restart_cleanup_cannot`

Actual relevant RED output, exit 1:

```text
FFF                                                                      [100%]
test_restart_cleanup_cannot_revoke_other_quarantined_group[revoked-False]
test_restart_cleanup_cannot_revoke_other_quarantined_group[revoked-True]
test_restart_cleanup_cannot_revoke_other_quarantined_group[canonical-True]
E   AssertionError: assert 'revoked' == 'active'
E     - active
E     + revoked
3 failed, 39 deselected in 3.60s
```

Each failed the same retained-lineage assertion after actual TaskRuntimeService close/reopen. A test-only capability accessor typo in a later assertion was corrected from `open` to the existing typed `access` API before GREEN; the RED above is the actual state mismatch, not that typo.

Same command after narrow event retry fix, exit 0:

```text
...                                                                      [100%]
3 passed, 39 deselected in 4.04s
```

Focused command:

`py -3.12 -m pytest backend/tests/test_protected_children.py backend/tests/test_protected_events.py backend/tests/test_task_runtime_deletion_authorization.py backend/tests/test_task_runtime_quarantine_isolation.py backend/tests/test_groups.py -q --tb=short`

```text
.....................                                      [100%]
179 passed in 62.01s (0:01:02)
```

Frozen source SHA256:

```text
CDE5032C044512BF2EA5D4BFB9DBDC71648549A48CCEA02BC7F16AE615D39063  backend/protected_events.py
6DEE93136C3E714D30A876EB23394B4750F90F76B7987ADA35BA504225BE1E8D  backend/tests/test_protected_children.py
```

Node command: `& .\.tools\node24.ps1 -Command 'npm test -- src/contract/parity.test.ts src/contract/publicEvents.drift.test.ts src/contract/taskRuntime.test.ts'`

```text
[node24] node v24.9.0
RUN v1.6.1
Test Files  3 passed (3)
Tests       25 passed (25)
Duration    2.79s
```

Exit 0. Same expected negative-contract stderr classes documented above. `py -3.12 -m compileall -q backend` exited 0, no output. Diff check exited 0 with existing LF→CRLF advisories.

I2: earlier WinError32 path prefix is intentionally redacted to `<TEMP>`; error and synthetic suffix are preserved. Both slice report/evidence scanned for profile prefixes and account identifier with no matches. No historical rewrite, prior scratch deletion or release-sanitization claim. M2: `closing(...)` and the original SQL transaction context now deterministically release each helper connection after commit/rollback; new I1 snapshots use explicit closing, not GC.

Single frozen full gate for fix round 1: `py -3.12 -m pytest backend/tests -q`, PYTHONUTF8=1, session 53692, exit 0:

```text
........................................................................ [ 96%]
.............................................................. [100%]
1929 passed, 5 skipped, 10 subtests passed in 220.76s (0:03:40)
```

This adds three I1 restart variants to the previous 1926 full count; the five existing skips and ten subtests remain unchanged. No exclusions/xfails added. Source/test hashes remained as recorded above throughout this gate.
