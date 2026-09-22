r"""Reproduce the seventh review's storage-failure probes against any revision.

Why this file exists in the repository: this probe lived in `%TEMP%` for several rounds and
the temp directory was eventually cleaned, taking it — and the baseline files it was compared
against — with it. The measurements it produces are the strongest evidence that the
sqlite-vs-cancellation Important was real and is closed, so it is kept here with instructions
instead of being reconstructed from memory later.

What it does: it closes the **real** database inside an effect (or takes the write lock with a
second connection) and then cancels, recording the exception type that reaches the caller.

Before `ddddaad`: a `sqlite3.*` exception escaped and *replaced* the caller's cancellation —
on the tool path and on the provider path alike.
After: the caller gets its own `CancelledError`, and `aclose()` ends quietly.

Usage
-----
    # against the working tree
    py -3.12 .superpowers/sdd/studycrew-commercial-continuation/probes/probe-sqlite-cancel.py

    # against a named revision (this is the control run that makes the probe meaningful)
    git archive <pre-fix-sha> | tar -x -C %TEMP%\pre-fix
    py -3.12 .../probe-sqlite-cancel.py %TEMP%\pre-fix

`PYTHONPATH` does not need to be set: the script prepends the checkout it is probing, and the
checkout's `backend` directory, to `sys.path` itself.

The three probes are deliberately *not* shipped as ordinary tests: they close a real database
connection, so they need their own process and a disposable directory. The pytest suite covers
the same mechanism with injected exception types (`test_task2_round2_mcp_security.py`,
`test_tool_cancellation_survives_a_failing_terminal_write`) — cheaper and CI-friendly, and
weaker. Both exist on purpose.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
from pathlib import Path

#: Pass a checkout root as argv[1] to probe a different revision (that is how the
#: control run against a pre-fix commit is done). Default: this repository.
_REPO_ROOT = Path(__file__).resolve().parents[4]
ROOT_ARG = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else _REPO_ROOT

# Refuse to run against a path that is not a checkout. This check exists because the probe
# silently produced **false evidence** once: the `%TEMP%` export it was pointed at had been
# deleted by a temp cleanup, `sys.path` ignored the missing entry, `backend` came from
# `PYTHONPATH` instead, and the "control run against the pre-fix revision" printed the
# post-fix result. A probe that cannot tell which tree it measured is not evidence.
if not (ROOT_ARG / "backend" / "commercial_gateway.py").is_file():
    raise SystemExit(
        f"not a StudyCrew checkout: {ROOT_ARG} has no backend/commercial_gateway.py.\n"
        "Refusing to probe -- a missing path would fall back to PYTHONPATH and silently "
        "measure a different tree. Re-export it with:\n"
        "  git archive <sha> | tar -x -C <dir>"
    )

sys.path[:0] = [str(ROOT_ARG), str(ROOT_ARG / "backend")]

import backend.commercial_gateway as _gateway_module  # noqa: E402

# Printed so the reader can see which tree was actually measured, rather than inferring it.
print(f"probing           : {ROOT_ARG}")
print(f"commercial_gateway: {_gateway_module.__file__}")

from backend.commercial_gateway import (  # noqa: E402
    ToolDescriptor,
    ToolExecutionRequest,
    ToolGateway,
)
from backend.groups import GroupRepository  # noqa: E402
from backend.task_runtime.contracts import RiskLevel  # noqa: E402
from backend.task_runtime.service import TaskRuntimeService  # noqa: E402


def describe(exc: BaseException) -> str:
    return f"{type(exc).__module__}.{type(exc).__name__}: {exc}"


def _tool_probe(*, hold_lock: bool) -> str:
    """Cancel a tool effect while the store is unusable.

    ``hold_lock=False`` closes the connection (shutdown race -> `ProgrammingError`);
    ``hold_lock=True`` has a second connection take the write lock, so the store's own
    `BEGIN IMMEDIATE` times out after `busy_timeout` (1000 ms by default) and raises
    `OperationalError: database is locked`.
    """
    import sqlite3

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        groups = GroupRepository(root)
        runtime = TaskRuntimeService(root, groups)
        group = groups.create_group("probe-lock" if hold_lock else "probe")
        run = runtime.submit(group.group_id, title="probe", request_key="p:1")
        run = runtime.schedule()[0]

        db_path = str(runtime.store.db.path)
        holder: dict[str, sqlite3.Connection] = {}

        def effect(_request: object) -> str:
            if hold_lock:
                # Take the lock *here*, so the claim that precedes the effect succeeds and
                # only the terminal record has to fight for it.
                conn = sqlite3.connect(db_path)
                conn.execute("BEGIN IMMEDIATE")
                holder["conn"] = conn
            else:
                runtime.store.db.close()
            raise asyncio.CancelledError()

        gateway = ToolGateway(runtime)
        gateway.register(
            ToolDescriptor(tool_name="reader", kind="builtin", risk=RiskLevel.LOW),
            effect,
        )
        request = ToolExecutionRequest(
            group_id=group.group_id,
            task_id=run.task_id,
            run_id=run.run_id,
            tool_name="reader",
            idempotency_key="p:effect",
            purpose="read",
        )

        async def go() -> str:
            try:
                await gateway.invoke(request)
            except BaseException as exc:  # noqa: BLE001 - measuring the type
                return describe(exc)
            return "NO EXCEPTION (worse: cancellation was swallowed)"

        try:
            return asyncio.run(go())
        finally:
            conn = holder.get("conn")
            if conn is not None:
                conn.rollback()
                conn.close()
            # Windows will not let TemporaryDirectory remove a database that is still open,
            # so the runtime connection has to go before the context manager exits. But the
            # probe's own effect may have closed it already, and closing twice raises from
            # `transaction()`'s rollback — that is this script's cleanup, not the behaviour
            # under test, so it is suppressed rather than allowed to mask the measurement.
            with contextlib.suppress(Exception):
                runtime.close()


def probe_provider_path() -> str:
    """Cancel a `chat_stream` while the store is closed underneath it."""
    import httpx

    from backend.commercial_gateway import (
        LegacyToolAuthority,
        ProviderGatewayClient,
        legacy_tool_authority_context,
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        groups = GroupRepository(root)
        runtime = TaskRuntimeService(root, groups)
        group = groups.create_group("probe-provider")
        run = runtime.submit(group.group_id, title="probe", request_key="pp:1")
        run = runtime.schedule()[0]
        authority = LegacyToolAuthority(
            group_id=group.group_id, task_id=run.task_id, run_id=run.run_id, runtime=runtime,
        )

        sse = (
            'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
            "data: [DONE]\n\n"
        )
        client = ProviderGatewayClient(
            base_url="https://api.deepseek.com",
            api_key="k",
            model="deepseek-chat",
            provider="deepseek",
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(
                lambda _r: httpx.Response(
                    200, headers={"content-type": "text/event-stream"}, text=sse,
                ),
            )),
        )

        async def go() -> str:
            with legacy_tool_authority_context(authority):
                stream = client.chat_stream([{"role": "user", "content": "hi"}])
                await stream.__anext__()
                runtime.store.db.close()   # the store dies mid-stream
                try:
                    await stream.aclose()
                except BaseException as exc:  # noqa: BLE001 - measuring the type
                    return describe(exc)
            return "NO EXCEPTION (clean cancellation)"

        return asyncio.run(go())


if __name__ == "__main__":
    print("tool path (closed db) :", _tool_probe(hold_lock=False))
    print("tool path (locked db) :", _tool_probe(hold_lock=True))
    print("provider path         :", probe_provider_path())
