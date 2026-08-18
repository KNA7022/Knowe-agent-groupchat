from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.engine import ProjectEngine
from backend.groups import GroupRepository
from backend.hub import Hub
from backend.server import KnoweServer
from backend.task_runtime import RuntimeConflictError, TaskRuntimeService
from backend.task_runtime.contracts import RiskLevel, TaskStatus


def _close_server(server: KnoweServer) -> None:
    if server.task_runtime is not None:
        server.task_runtime.close()
    if server.store is not None:
        server.store.close(timeout=5)


def _engine(tmp_path: Path, name: str) -> ProjectEngine:
    workspace = tmp_path / f"{name}-workspace"
    workspace.mkdir()
    engine = ProjectEngine(
        Hub(), name, agent=None, workspace_root=workspace,
        internal_workspace_root=tmp_path / f"{name}-internal",
    )
    engine.agent = None
    return engine


@pytest.mark.asyncio
async def test_tracked_cancel_before_root_start_never_enters_coordinator(
    tmp_path: Path, monkeypatch,
) -> None:
    engine = _engine(tmp_path, "cancel-before-root")
    calls: list[str] = []

    async def harness(content: str, _target: str | None, *, internal: bool = False) -> bool:
        del internal
        calls.append(content)
        return True

    monkeypatch.setattr(engine, "_harness_turn_impl", harness)
    future = await engine.submit_tracked("must-not-run")
    assert future.cancel()
    await asyncio.sleep(0)
    engine.start()
    for _ in range(100):
        if not engine._governed_task_scopes:  # noqa: SLF001
            break
        await asyncio.sleep(0.01)
    assert future.cancelled()
    assert calls == []
    assert not engine._governed_task_scopes  # noqa: SLF001

    next_future = await engine.submit_tracked("next-run")
    assert (await asyncio.wait_for(next_future, timeout=2))["outcome"] == "succeeded"
    assert calls == ["next-run"]
    await engine.stop(immediate=True)


@pytest.mark.asyncio
async def test_tracked_cancel_during_root_cancels_root_worker_and_keeps_loop_alive(
    tmp_path: Path, monkeypatch,
) -> None:
    engine = _engine(tmp_path, "cancel-during-root")
    root_started = asyncio.Event()
    root_cancelled = asyncio.Event()
    worker_started = asyncio.Event()
    worker_cancelled = asyncio.Event()
    post_cancel_effects: list[str] = []

    async def process_turn(_content: str, target: str | None, *, internal: bool = False) -> None:
        del internal
        if target == "worker_cancelled_with_root":
            worker_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                worker_cancelled.set()
                raise

    async def harness(content: str, _target: str | None, *, internal: bool = False) -> bool:
        del internal
        if content == "held-root":
            engine._start_worker_turn(  # noqa: SLF001
                "worker_cancelled_with_root", SimpleNamespace(goal="held worker"),
            )
            scope = next(iter(engine._governed_task_scopes.values()))  # noqa: SLF001
            engine._task_envelopes["pending_worker"] = SimpleNamespace(goal="never start")  # noqa: SLF001
            scope["pending_worker_ids"].add("pending_worker")
            root_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                root_cancelled.set()
                raise
            post_cancel_effects.append("root-resumed")
        return True

    monkeypatch.setattr(engine, "_process_turn", process_turn)
    monkeypatch.setattr(engine, "_harness_turn_impl", harness)
    engine.start()
    future = await engine.submit_tracked("held-root")
    await asyncio.wait_for(root_started.wait(), timeout=2)
    await asyncio.wait_for(worker_started.wait(), timeout=2)
    assert future.cancel()
    await asyncio.wait_for(root_cancelled.wait(), timeout=2)
    await asyncio.wait_for(worker_cancelled.wait(), timeout=2)
    for _ in range(100):
        if not engine._governed_task_scopes and not engine._worker_turns:  # noqa: SLF001
            break
        await asyncio.sleep(0.01)
    assert future.cancelled()
    assert post_cancel_effects == []
    assert not engine._governed_task_scopes  # noqa: SLF001
    assert not engine._worker_turns  # noqa: SLF001
    assert "pending_worker" not in engine._task_envelopes  # noqa: SLF001

    next_future = await engine.submit_tracked("next-after-cancel")
    assert (await asyncio.wait_for(next_future, timeout=2))["outcome"] == "succeeded"
    await engine.stop(immediate=True)


@pytest.mark.asyncio
async def test_server_cancel_cancels_real_root_and_schedules_next_runtime_run(
    tmp_path: Path, monkeypatch,
) -> None:
    server = KnoweServer(str(tmp_path / "server-root-cancel"))
    group = server.groups.create_group(  # type: ignore[union-attr]
        "Server root cancel", legacy_project_id="project_server_root_cancel",
    )
    server.hub.get_or_create("project_server_root_cancel", "Server root cancel")
    engine = _engine(tmp_path, "project_server_root_cancel")
    root_started = asyncio.Event()
    root_cancelled = asyncio.Event()

    async def harness(content: str, _target: str | None, *, internal: bool = False) -> bool:
        del internal
        if content == "held server root":
            root_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                root_cancelled.set()
                raise
        return True

    monkeypatch.setattr(engine, "_harness_turn_impl", harness)
    monkeypatch.setattr(server, "_project_dir_is_valid", lambda _project_id: True)
    monkeypatch.setattr(server, "engine_for", lambda _project_id: engine)
    monkeypatch.setattr(
        server, "_prepare_attachments", lambda *_args: asyncio.sleep(0, result=(True, [])),
    )
    engine.start()
    await server._cmd_user_message(None, {  # noqa: SLF001
        "project_id": "project_server_root_cancel", "content": "held server root",
        "client_msg_id": "held-server-root", "route_confidence": 1.0,
        "read_only": False,
    })
    await asyncio.wait_for(root_started.wait(), timeout=2)
    first = server.task_runtime.store.list_runs(group_id=group.group_id)[0]  # type: ignore[union-attr]
    queued = server.task_runtime.submit(  # type: ignore[union-attr]
        group.group_id, title="Next after cancellation", request_key="next-after-server-cancel",
    )
    assert server.task_runtime.store.get_run(queued.run_id).status is TaskStatus.QUEUED  # type: ignore[union-attr]
    server._task_runtime_dispatch("control", {  # noqa: SLF001
        "group_id": group.group_id, "run_id": first.run_id, "command": "cancel",
        "request_key": "cancel-live-server-root",
    })
    await asyncio.wait_for(root_cancelled.wait(), timeout=2)
    for _ in range(100):
        next_run = server.task_runtime.store.get_run(queued.run_id)  # type: ignore[union-attr]
        if (
            next_run is not None and next_run.status is TaskStatus.RUNNING
            and not engine._governed_task_scopes  # noqa: SLF001
        ):
            break
        await asyncio.sleep(0.01)
    assert server.task_runtime.store.get_run(first.run_id).status is TaskStatus.CANCELLED  # type: ignore[union-attr]
    assert next_run is not None and next_run.status is TaskStatus.RUNNING
    assert not engine._governed_task_scopes  # noqa: SLF001
    await engine.stop(immediate=True)
    _close_server(server)


def test_cancel_waiting_approval_is_atomic_idempotent_and_late_resolution_is_terminal(
    tmp_path: Path,
) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Cancel approval")
    routed = service.route_message(
        group.group_id, content="ambiguous request", request_key="route-cancel",
        confidence=0.2, read_only=True,
        executor_private_request={"project_id": group.group_id, "content": "private request"},
        executor_execution_key="route-cancel-execution",
    )
    approval_id = routed["approval"]["approval_id"]
    run_id = routed["run"]["run_id"]
    cancelled = service.cancel(group.group_id, run_id, request_key="cancel-waiting")
    duplicate = service.cancel(group.group_id, run_id, request_key="duplicate-cancel")
    assert cancelled.status is duplicate.status is TaskStatus.CANCELLED
    assert service.store.pending_approvals(group.group_id) == ()
    approval = service.store.get_approval(approval_id)
    assert approval is not None and approval.status == "rejected"
    continuation = service.store.executor_continuation(approval_id=approval_id)
    assert continuation is not None and continuation["status"] == "cancelled"

    late, grant, owner = service.resolve_approval(
        group.group_id, approval_id, approved=True,
        capabilities=("route_message",), request_key="late-resolution",
    )
    assert late.status == "rejected" and grant is None
    assert owner.status is TaskStatus.CANCELLED
    assert service.group_snapshot(group.group_id)["pending_approvals"] == []
    service.close()


def test_expired_approval_read_sweep_terminalizes_continuation_and_releases_queue(
    tmp_path: Path,
) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Expiry")
    run = service.submit(group.group_id, title="Expiring", request_key="expiry-run")
    service.schedule()
    expiry = (datetime.now(timezone.utc) + timedelta(milliseconds=30)).isoformat().replace(
        "+00:00", "Z",
    )
    approval = service.request_approval(
        group.group_id, run.run_id, kind="message_route", summary="Confirm route",
        risk=RiskLevel.HIGH, request_key="expiry-approval",
        requested_capabilities=("route_message",), operation_scope={"route": "queue_new"},
        purpose="confirm_route", expires_at=expiry,
        executor_private_request={"project_id": group.group_id, "content": "private"},
        executor_execution_key="expiry-execution", route_request_key="expiry-route",
    )
    queued = service.submit(group.group_id, title="Next", request_key="expiry-next")
    service.schedule()
    assert service.store.get_run(queued.run_id).status is TaskStatus.QUEUED  # type: ignore[union-attr]
    import time
    time.sleep(0.05)

    snapshot = service.snapshot(group.group_id, run.run_id)
    assert snapshot["run"]["status"] == TaskStatus.FAILED.value
    assert service.store.get_approval(approval.approval_id).status == "rejected"  # type: ignore[union-attr]
    continuation = service.store.executor_continuation(approval_id=approval.approval_id)
    assert continuation is not None and continuation["status"] == "rejected"
    assert service.store.pending_approvals(group.group_id) == ()
    assert service.store.get_run(queued.run_id).status is TaskStatus.RUNNING  # type: ignore[union-attr]
    service.close()


def test_expired_approval_is_reconciled_on_restart_without_user_resolution(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Restart expiry")
    run = service.submit(group.group_id, title="Expiring", request_key="restart-expiry-run")
    service.schedule()
    approval = service.request_approval(
        group.group_id, run.run_id, kind="message_route", summary="Confirm route",
        risk=RiskLevel.MEDIUM, request_key="restart-expiry-approval",
        requested_capabilities=("route_message",), operation_scope={"route": "queue_new"},
        purpose="confirm_route",
        expires_at=(datetime.now(timezone.utc) + timedelta(milliseconds=40)).isoformat().replace(
            "+00:00", "Z",
        ),
    )
    service.close()
    import time
    time.sleep(0.07)

    recovered = TaskRuntimeService(tmp_path, groups)
    try:
        assert approval.approval_id in recovered.recovery_report["expired_approvals"]
        assert recovered.store.get_approval(approval.approval_id).status == "rejected"  # type: ignore[union-attr]
        assert recovered.store.get_run(run.run_id).status is TaskStatus.FAILED  # type: ignore[union-attr]
        assert recovered.store.pending_approvals(group.group_id) == ()
    finally:
        recovered.close()


@pytest.mark.asyncio
async def test_recovered_high_risk_requires_fresh_linked_attempt_and_executes_once(
    tmp_path: Path, monkeypatch,
) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group(
        "Recovered route", legacy_project_id="project_recovered_route",
    )
    original = "张三成绩95分，排名第2"
    routed = service.route_message(
        group.group_id, content=original, request_key="recovery-route",
        confidence=1.0, read_only=True,
        executor_private_request={
            "project_id": "project_recovered_route", "content": original,
            "attachments": [],
        },
        executor_execution_key="recovery-execution",
    )
    first_approval_id = routed["approval"]["approval_id"]
    service.resolve_approval(
        group.group_id, first_approval_id, approved=True,
        capabilities=("route_message",), request_key="recovery-first-approve",
    )
    first = service.store.executor_continuation(approval_id=first_approval_id)
    assert first is not None and first["status"] == "ready"
    run_id = str(first["run_id"])
    service.close()

    first_restart = TaskRuntimeService(tmp_path, groups)
    first_blocked = first_restart.store.executor_continuation(approval_id=first_approval_id)
    assert first_blocked is not None and first_blocked["status"] == "recovered_blocked"
    assert first_restart.store.grant_for_approval(first_approval_id) is None
    assert first_restart.store.get_run(run_id).status is TaskStatus.PAUSED  # type: ignore[union-attr]
    with pytest.raises(RuntimeConflictError, match="explicit reapproval"):
        first_restart.resume(group.group_id, run_id, request_key="unsafe-normal-resume")
    assert first_restart.store.get_run(run_id).status is TaskStatus.PAUSED  # type: ignore[union-attr]

    second_approval, waiting = first_restart.reapprove_recovered_continuation(
        group.group_id, str(first_blocked["continuation_id"]),
        operation_scope={"route": "queue_new"},
        request_key="reapprove-first-blocked",
    )
    duplicate, _ = first_restart.reapprove_recovered_continuation(
        group.group_id, str(first_blocked["continuation_id"]),
        operation_scope={"route": "queue_new"},
        request_key="reapprove-first-blocked",
    )
    assert duplicate.approval_id == second_approval.approval_id
    assert waiting.status is TaskStatus.WAITING_APPROVAL
    second = first_restart.store.executor_continuation(approval_id=second_approval.approval_id)
    assert second is not None
    assert second["recovery_of_continuation_id"] == first_blocked["continuation_id"]
    assert second["private_request_digest"]
    first_restart.close()

    # Restart while the fresh approval is pending, then simulate a crash after its
    # approval but before the server can claim the private request.
    second_restart = TaskRuntimeService(tmp_path, groups)
    assert second_restart.store.get_approval(second_approval.approval_id).status == "pending"  # type: ignore[union-attr]
    second_restart.resolve_approval(
        group.group_id, second_approval.approval_id, approved=True,
        capabilities=("route_message",), request_key="approve-second-before-crash",
    )
    second_continuation = second_restart.store.executor_continuation(
        approval_id=second_approval.approval_id,
    )
    assert second_continuation is not None and second_continuation["status"] == "ready"
    second_restart.close()

    server = KnoweServer(str(tmp_path))
    server.hub.get_or_create("project_recovered_route", "Recovered route")
    blocked_again = server.task_runtime.store.executor_continuation(  # type: ignore[union-attr]
        approval_id=second_approval.approval_id,
    )
    assert blocked_again is not None and blocked_again["status"] == "recovered_blocked"
    with pytest.raises(RuntimeConflictError, match="explicit reapproval"):
        server._task_runtime_dispatch("control", {  # noqa: SLF001
            "group_id": group.group_id, "run_id": run_id, "command": "resume",
            "request_key": "unsafe-second-resume",
        })

    response = server._task_runtime_dispatch("reapprove-recovered", {  # noqa: SLF001
        "group_id": group.group_id,
        "continuation_id": blocked_again["continuation_id"],
        "request_key": "reapprove-second-blocked",
    })
    third_approval_id = response["approval"]["approval_id"]
    assert response["run"]["status"] == TaskStatus.WAITING_APPROVAL.value

    class TrackedEngine:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def resolve_group_mentions(self, _content: str):
            return SimpleNamespace(worker_ids=())

        async def submit_tracked(self, content: str, attachments=None):
            del attachments
            self.calls.append(content)
            future = asyncio.get_running_loop().create_future()
            future.set_result({"outcome": "succeeded", "error_type": ""})
            return future

    engine = TrackedEngine()
    monkeypatch.setattr(server, "_project_dir_is_valid", lambda _project_id: True)
    monkeypatch.setattr(server, "engine_for", lambda _project_id: engine)
    monkeypatch.setattr(
        server, "_prepare_attachments", lambda *_args: asyncio.sleep(0, result=(True, [])),
    )
    approval_payload = {
        "group_id": group.group_id, "approval_id": third_approval_id,
        "approved": True, "capabilities": ["route_message"],
        "request_key": "approve-third-attempt",
    }
    server._task_runtime_dispatch("approval", approval_payload)  # noqa: SLF001
    for _ in range(100):
        terminal = server.task_runtime.store.get_run(run_id)  # type: ignore[union-attr]
        if terminal is not None and terminal.status.terminal:
            break
        await asyncio.sleep(0.01)
    assert terminal is not None and terminal.status is TaskStatus.SUCCEEDED
    assert engine.calls == [original]
    server._task_runtime_dispatch(  # noqa: SLF001
        "approval", {**approval_payload, "request_key": "duplicate-third-approval"},
    )
    await asyncio.sleep(0.05)
    assert engine.calls == [original]
    assert first_blocked["status"] == "recovered_blocked"
    assert blocked_again["status"] == "recovered_blocked"
    third = server.task_runtime.store.executor_continuation(  # type: ignore[union-attr]
        approval_id=third_approval_id,
    )
    assert third is not None and third["status"] == "completed"
    _close_server(server)

    final_restart = TaskRuntimeService(tmp_path, groups)
    try:
        assert final_restart.store.get_run(run_id).status is TaskStatus.SUCCEEDED  # type: ignore[union-attr]
        assert final_restart.store.executor_continuation(approval_id=third_approval_id)["status"] == "completed"  # type: ignore[index]
    finally:
        final_restart.close()
