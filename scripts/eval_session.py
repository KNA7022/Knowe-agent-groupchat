"""Read a temporary credential from a non-echo stdin pipe; run bounded batches.

First stdin line is the credential; subsequent lines are JSON argument arrays.
The credential is never written to disk, argv, stdout, stderr, or run reports.
Send an empty argument array to exit. Use only synthetic evaluation fixtures.
"""
from __future__ import annotations
import json
import getpass
import os
import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    credential = getpass.getpass("Evaluation credential (hidden): ") if sys.stdin.isatty() else sys.stdin.readline().strip()
    if not credential:
        raise SystemExit("Credential required on non-echo stdin")
    child_env = dict(os.environ, STUDYCREW_EVAL_API_KEY=credential, PYTHONIOENCODING="utf-8")
    print("Credential loaded in memory; waiting for batch arguments", flush=True)
    for line in sys.stdin:
        args = json.loads(line)
        if args == []:
            break
        if not isinstance(args, list) or any(not isinstance(x, str) for x in args):
            raise ValueError("argument_array_required")
        result = subprocess.run([sys.executable, str(root/"scripts/eval_runner.py"), *args], cwd=root, env=child_env, check=False)
        print(json.dumps({"batch_exit_code": result.returncode}), flush=True)
    child_env.pop("STUDYCREW_EVAL_API_KEY", None)


if __name__ == "__main__":
    main()
