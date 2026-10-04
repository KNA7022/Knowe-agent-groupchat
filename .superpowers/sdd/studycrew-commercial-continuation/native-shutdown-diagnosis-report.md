# Native dependency preflight shutdown diagnosis

Date: 2026-10-04. Scope: isolated dependency preflight harness only. Eleven isolated child invocations were used, below the authorized maximum of twelve. No production code, configuration, lockfile, installed package, Git state, prior evidence file, SDK operation, model, provider or network service was changed or exercised.

## Finding and bounded correction

The exact diagnostic is reproducible with **only an imported Pyclipper and a module-level `PyclipperOffset()` instance left alive until interpreter shutdown**, under the retained Python audit network/process guard. A combination of all native dependencies is not necessary. Constructing the object alone is sufficient; `AddPath` and `Execute` are not required to trigger it.

Explicitly releasing that test object before interpreter shutdown eliminates the diagnostic in both the minimal regression and the original complete native-operation scenario. The only functional correction in the complete harness variant is:

```diff
     assert len(offset.Execute(1))==1
+    del offset
     assert Image.new('RGB',(4,4)).size==(4,4)
```

This is a verified **harness lifecycle correction**, retained as source in this report. The original evidence script remains unchanged. It does not prove that the installed Pyclipper wheel is free from a finalization defect, identify its internal exception, or exclude an interaction with the audit hook. No run removed the audit guard. A package-level fix is outside this task and remains unproven. Production code was not inspected, so no claim is made about its object lifetimes.

The original full scenario was red twice (invocations 1 and 10), and the corrected full scenario was green twice (9 and 11). Both red runs exited zero and reported successful operation assertions. Therefore **exit zero is not an adequate acceptance condition**. Green here requires exit zero, completed assertions and literally empty stderr.

## Feedback loop and hypotheses

The deterministic loop captures the child process through its normal shutdown with `subprocess.run(..., capture_output=True, text=True, encoding='utf-8', timeout=45)`. It neither suppresses nor redirects away stderr and never forces exit. Full-harness pass criterion:

```python
r.returncode == 0 and r.stderr == '' and json.loads(r.stdout)['status'] == 'passed'
```

Exact red stderr, including newlines:

```text
Error in sys.excepthook:

Original exception was:
```

Escaped representation: `"Error in sys.excepthook:\n\nOriginal exception was:\n"`.

After invocation 1 confirmed the signal, the following ranked hypotheses were communicated before testing. Minimization and adaptive tests consumed this same stderr-specific loop.

1. Harness audit/profiler lifetime interacts with interpreter shutdown. Prediction: an equivalent smaller shutdown-safe guard removes the symptom. Existing evidence had already shown that merely disabling the profiler did not. The minimal guard still reproduces, so the profiler and full guard's event/state bookkeeping are not required; audit interaction remains unexcluded.
2. An actual native operation or surviving native object triggers finalization. Prediction: combined imports without operations become green, and object-lifetime reduction isolates the trigger. Supported by invocations 2–8.
3. Combined imports or import ordering create a package interaction. Prediction: combined imports alone remain red, or a required package subset emerges. Combined imports were green, and Pyclipper alone with one instance was red; the full combination is not required.
4. Result introspection retains problematic module/thread/runtime references. Prediction: removing result collection eliminates the symptom. The minimal constructor repro has no such introspection and still fails, so it is not required.

## Results

Every invocation used the same existing CPython 3.12.10 Windows x64 venv and `-B -X utf8`. All eleven child exits were zero. Every full run retained telemetry/offline flags, audit checks and the original constructor guard; the small regression imports only `sys` and `pyclipper`, keeps the same six forbidden audit events, and performs no SDK/client operation. No package install, upgrade, acquisition or external access was attempted.

| Invocation | Scenario | Operation/assertion result | stderr | Verdict |
| --- | --- | --- | --- | --- |
| 1 | Original full harness; only output location isolated and result-file write removed | passed | Exact diagnostic | RED |
| 2 | All original direct imports; native operations omitted; ORT telemetry disabled | passed | empty | GREEN |
| 3 | Full harness except Tokenizers/USearch operations | passed | Exact diagnostic | RED |
| 4 | Invocation 3 with OCR operations also omitted | passed | empty | GREEN |
| 5 | Full harness except the three Pyclipper offset-operation statements | passed | empty | GREEN |
| 6 | Minimal guard + PyclipperOffset + original AddPath/Execute assertion | passed | Exact diagnostic | RED |
| 7 | Minimal guard + PyclipperOffset construction only | passed | Exact diagnostic | RED |
| 8 | Invocation 7 plus `del offset` before the success marker | passed | empty | GREEN |
| 9 | Original complete harness + `del offset` immediately after Execute assertion | passed | empty | GREEN |
| 10 | Repeat original complete harness | passed | Exact diagnostic | RED |
| 11 | Repeat corrected complete harness | passed | empty | GREEN |

There was no invocation 12. Existing six import-only results and the earlier no-profiler result were read, not rerun or overwritten. Budgeted minimization established a sufficient trigger; it did not attempt to prove every guard element is necessary, since removing network protection was outside scope.

Corrected full runs retained all seven result groups: ORT, Tokenizers, USearch, Numkong, OCR dependencies, Mem0 and baseline. USearch returned keys `[11,22,33]`, cosine distances `[0.0,1.0,2.0]` and similarities `[1.0,0.0,-1.0]`; remove/re-add/reset assertions completed. Clipper still executed its real polygon offset before destruction. Every full run recorded only the deliberate denied DNS self-test, zero guarded client constructor attempts, and `MainThread` only at the checkpoint. The original complete limitations concerning native socket interception and post-checkpoint observations still apply.

## Exact command/source recipe

Read-only original source: `D:/studycrew-runtime-dependency-preflight-20261004/native_probe.py`.

SHA-256 of that original source after testing: `d926694b447901bf0427beab9e5bbf8e95a77692c12c5793f9e52df2b10a95e3`.

All children used this exact argv shape, with `source` supplied in memory:

```python
['D:/studycrew-runtime-dependency-preflight-20261004/venv/Scripts/python.exe',
 '-B', '-X', 'utf8', '-c', source]
```

The outer launcher was PowerShell 7, passing the following UTF-8 Python source through a single-quoted here-string `$probe` and invoking:

```powershell
& 'E:/python/python.exe' -B -X utf8 -c $probe
```

The new directory was explicitly named before creation and created once with:

```powershell
# Diagnostic copy: <TEMP> replaces the local account-bearing temporary root; substitute the actual temporary root to reproduce.
New-Item -ItemType Directory -Path '<TEMP>/studycrew-native-shutdown-diagnosis-20261004' -ErrorAction Stop
```

Common in-memory source preparation, exactly as tested (do not run the original file directly, because it writes its previous evidence paths):

```python
import pathlib, subprocess, json, ast
src=pathlib.Path('D:/studycrew-runtime-dependency-preflight-20261004/native_probe.py').read_text(encoding='utf-8')
src=src.replace('ROOT=pathlib.Path(__file__).parent', "ROOT=pathlib.Path('<TEMP>/studycrew-native-shutdown-diagnosis-20261004')")
src=src.replace("    (ROOT/result_file).write_text(json.dumps(results,indent=2),encoding='utf-8')",'')
```

Source variants for invocations 1/10 and 9/11 respectively:

```python
original = src
corrected = src.replace('    assert len(offset.Execute(1))==1',
                        '    assert len(offset.Execute(1))==1\n    del offset')
```

Prepared original source SHA-256: `c45a3e6d4e05f00102a7f8226793b525f7d9671b00ed7e45de1de61a17af273d`.
Prepared corrected source SHA-256: `1d5f0dc842b6aaf1d93e63df55fd12b3c73050aecc9c807e1571df12f58f9e58`.

AST-based minimization for invocations 2–5 used `tree=ast.parse(src)` and `probe=next(n for n in tree.body if isinstance(n,ast.Try) and n.finalbody)`, followed by the respective transformation below and `source=ast.unparse(tree)`:

```python
# Invocation 2
probe.body=[n for n in probe.body if isinstance(n,(ast.Import,ast.ImportFrom))]+ast.parse("ort.disable_telemetry_events(); results['status']='passed'").body

# Invocation 3: operate on a fresh original tree
lines=src.splitlines()
start=next(i+1 for i,l in enumerate(lines) if l.strip().startswith('tokenizer='))
end=next(i+1 for i,l in enumerate(lines) if l.strip()=='import cv2, pyclipper')
probe.body=[n for n in probe.body if not(start<=n.lineno<end) or isinstance(n,(ast.Import,ast.ImportFrom))]

# Invocation 4: operate on a fresh original tree; same start as 3
end=next(i+1 for i,l in enumerate(lines) if l.strip().startswith('from google.protobuf'))
probe.body=[n for n in probe.body if not(start<=n.lineno<end) or isinstance(n,(ast.Import,ast.ImportFrom))]

# Invocation 5: operate on a fresh original tree
probe.body=[n for n in probe.body if not any(s in ast.unparse(n) for s in ['offset =','offset.AddPath','offset.Execute'])]
```

Unedited descriptive strings in minimization runs are not additional operation evidence; only the statements retained by each transformation were executed.

The minimal failing regression (invocation 7) was this exact child source. Invocation 8 inserted `del offset` before `print`. Invocation 6 inserted the original `AddPath` and `assert len(offset.Execute(1))==1` statements between construction and `print`.

```python
import sys

def guard(event,args,_error=RuntimeError):
 if event in ('socket.connect','socket.getaddrinfo','socket.sendto','socket.sendmsg','subprocess.Popen','os.system'):
  raise _error('Network/process forbidden in dependency probe')
sys.addaudithook(guard)
import pyclipper
offset=pyclipper.PyclipperOffset()
print('passed')
```

The exact added statements for invocation 6:

```python
offset.AddPath([(0,0),(10,0),(10,10),(0,10)],pyclipper.JT_ROUND,pyclipper.ET_CLOSEDPOLYGON)
assert len(offset.Execute(1))==1
```

All runs used `subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', timeout=45)`. Minimal pass criterion additionally required `r.stdout == 'passed\n'`; full runs parsed stdout JSON. The wrapper printed return code, raw escaped stderr and the independent PASS/FAIL verdict. A zero outer launcher exit therefore did not hide a failed child stderr verdict.

## Scope, cleanup and follow-up

The original source, earlier logs/results and installed venv were preserved. No bytecode was generated by these `-B` probes. The only new temporary filesystem output was the private `runtime/config.json` (59 bytes) beneath `<TEMP>/studycrew-native-shutdown-diagnosis-20261004`. No helper script was written to disk; all variants were in memory.

Temporary contents were inventoried. Cleanup was attempted with a PowerShell command that first checks the exact directory name, its parent against the actual OS temp root, and that the target is not a reparse point, then calls native `Remove-Item -LiteralPath $diagnosticTarget.FullName -Recurse -Force`. Automatic approval rejected that command before execution with `blocked by policy`, without a more specific reason. Therefore the command's path-validation and deletion steps did not run. The directory remains; no alternative deletion mechanism was attempted to bypass that rejection. No prior preflight/reviewer scratch was removed. No debug instrumentation was added to production or installed dependencies.

The sole intentionally retained artifact is this report, at `D:/agentwork/StudyCrew-worktree/.superpowers/sdd/studycrew-commercial-continuation/native-shutdown-diagnosis-report.md`, including its exact correction and regression source. It is implementation evidence, contains local paths and synthetic-operation data but no credentials or user document content, and has no temporary cleanup requirement. The unintended temporary residue is the explicitly named OS temp directory above, containing only `runtime/config.json`; it is private test configuration, not application/user document data. Cleanup requires deleting that exact owned directory once the policy restriction is resolved. No commit was created.

For future harnesses, use a bounded local lifetime for temporary native objects and assert on captured stderr after normal process termination. The observed correction supports clean shutdown of this corrected combined synthetic preflight on this host. It does not certify the production application, a frozen executable, OCR/E5 inference under this closure, or all native object lifetimes. If production retains Pyclipper objects through interpreter teardown, the smallest follow-up is to verify and bound that ownership lifetime with the same guarded subprocess regression; investigate the matching upstream extension's finalizer separately before attributing its internal cause. No package upgrade or patch is justified by this report alone.
