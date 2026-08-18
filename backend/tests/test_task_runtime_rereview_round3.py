from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.engine import ProjectEngine
from backend.groups import GroupRepository
from backend.hub import Hub
from backend.server import KnoweServer
from backend.task_runtime import TaskRuntimeService
from backend.task_runtime.contracts import (
    EvidenceRef,
    FailureType,
    TaskStatus,
    normalize_legacy_public_payload,
)


def _close_server(server: KnoweServer) -> None:
    if server.task_runtime is not None:
        server.task_runtime.close()
    if server.store is not None:
        server.store.close(timeout=5)


def test_legacy_public_events_are_allowlist_migrated_and_second_open_is_stable(
    tmp_path: Path,
) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Legacy events")
    run = service.submit(group.group_id, title="Legacy", request_key="legacy-run")
    service.schedule()
    service.route_message(
        group.group_id, content="Additional legacy context",
        request_key="legacy-message-routed", confidence=1.0, read_only=False,
    )
    service.cancel(group.group_id, run.run_id, request_key="stabilize-legacy-run")
    database = Path(service.store.db.path)
    service.close()

    legacy_payload = {
        "status": "queued", "kind": "primary", "route_label": "queue_new",
        "summary": "Queued status", "access_token": "legacy-secret",
        "extra": {"Authorization": "Bearer sk-secret", "malformed": True},
    }
    with sqlite3.connect(database) as conn:
        conn.execute(
            """UPDATE studycrew_run_events SET public_payload_json=?
               WHERE run_id=? AND event_type='run_queued'""",
            (json.dumps(legacy_payload, ensure_ascii=False), run.run_id),
        )
        conn.commit()

    first = TaskRuntimeService(tmp_path, groups)
    replay = first.replay(group.group_id, run.run_id, after_seq=0)
    assert replay["events"]
    with first.store.db.transaction(immediate=False) as conn:
        normalized_first = [tuple(row) for row in conn.execute(
            """SELECT event_id, run_id, group_id, seq, version, event_type,
                      public_payload_json, idempotency_key, created_at
               FROM studycrew_run_events ORDER BY run_id, seq"""
        ).fetchall()]
        public_json = str(conn.execute(
            "SELECT public_payload_json FROM studycrew_run_events WHERE run_id=? AND seq=1",
            (run.run_id,),
        ).fetchone()[0])
    first.close()

    assert "access_token" not in public_json
    assert "legacy-secret" not in public_json
    assert "sk-secret" not in public_json
    assert '"extra"' not in public_json
    assert '"classification":["credential"]' in public_json

    second = TaskRuntimeService(tmp_path, groups)
    try:
        assert second.replay(group.group_id, run.run_id, after_seq=0)["events"]
        with second.store.db.transaction(immediate=False) as conn:
            normalized_second = [tuple(row) for row in conn.execute(
                """SELECT event_id, run_id, group_id, seq, version, event_type,
                          public_payload_json, idempotency_key, created_at
                   FROM studycrew_run_events ORDER BY run_id, seq"""
            ).fetchall()]
        assert normalized_second == normalized_first
    finally:
        second.close()


@pytest.mark.parametrize("event_type,payload", [
    ("run_queued", {"status": "queued", "unknown": 1}),
    ("status_changed", {"status": "running", "unknown": 1}),
    ("agent_phase_changed", {"phase": "planning", "unknown": 1}),
    ("budget_updated", {"usage": {"tokens": 1, "unknown": 2}, "unknown": 1}),
    ("budget_exhausted", {"limits": {"tokens": 1, "unknown": 2}, "unknown": 1}),
    ("approval_requested", {"status": "waiting_approval", "approval": {
        "approval_id": "apr_" + "1" * 32, "access_token": "legacy-secret",
    }, "unknown": 1}),
    ("approval_resolved", {"status": "queued", "unknown": 1}),
    ("checkpoint_created", {"label": "Checkpoint", "unknown": 1}),
    ("message_routed", {"summary": "Authorization: Bearer sk-secret", "unknown": 1}),
    ("restart_recovery", {"status": "paused", "unknown": 1}),
    ("tool_prepared", {"status": "prepared", "unknown": 1}),
    ("verifier_decision", {"status": "verifying", "unknown": 1}),
])
def test_every_reachable_legacy_event_type_projects_deterministically(
    event_type: str, payload: dict,
) -> None:
    first = normalize_legacy_public_payload(event_type, payload)
    second = normalize_legacy_public_payload(event_type, first)
    assert first == second
    assert "unknown" not in first
    assert "access_token" not in json.dumps(first)
    assert "sk-secret" not in json.dumps(first)


@pytest.mark.asyncio
async def test_approved_message_resumes_exact_private_request_once(tmp_path: Path, monkeypatch) -> None:
    server = KnoweServer(str(tmp_path))
    group = server.groups.create_group(  # type: ignore[union-attr]
        "Approval continuation", legacy_project_id="project_approval_continuation",
    )
    server.hub.get_or_create("project_approval_continuation", "Approval continuation")

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
        server, "_prepare_attachments",
        lambda *_args: asyncio.sleep(0, result=(True, [])),
    )
    original = "Explain the private course note exactly"
    await server._cmd_user_message(None, {  # noqa: SLF001
        "project_id": "project_approval_continuation", "content": original,
        "client_msg_id": "approval-continuation-1", "route_confidence": 0.4,
        "read_only": True,
    })
    assert engine.calls == []
    approval = server.task_runtime.store.pending_approvals(group.group_id)[0]  # type: ignore[union-attr]
    run = server.task_runtime.store.get_run(approval.run_id)  # type: ignore[union-attr]
    assert run is not None and run.status is TaskStatus.WAITING_APPROVAL

    payload = {
        "group_id": group.group_id, "approval_id": approval.approval_id,
        "approved": True, "capabilities": ["route_message"],
        "request_key": "resolve-private-continuation",
    }
    server._task_runtime_dispatch("approval", payload)  # noqa: SLF001
    for _ in range(100):
        terminal = server.task_runtime.store.get_run(approval.run_id)  # type: ignore[union-attr]
        if terminal is not None and terminal.status.terminal:
            break
        await asyncio.sleep(0.01)
    assert terminal is not None and terminal.status is TaskStatus.SUCCEEDED
    assert engine.calls == [original]
    server._task_runtime_dispatch(  # noqa: SLF001
        "approval", {**payload, "request_key": "duplicate-resolution"},
    )
    await asyncio.sleep(0.05)
    assert engine.calls == [original]
    continuation = server.task_runtime.store.executor_continuation(  # type: ignore[union-attr]
        approval_id=approval.approval_id,
    )
    assert continuation is not None and continuation["status"] == "completed"
    public = json.dumps(
        [event.to_public_dict() for event in server.task_runtime.store.events_after(approval.run_id, 0)],  # type: ignore[union-attr]
        ensure_ascii=False,
    )
    assert original not in public
    _close_server(server)


@pytest.mark.asyncio
async def test_real_engine_tracked_future_waits_for_worker_and_coordinator_aggregation(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = ProjectEngine(
        Hub(), "real-engine-scope", agent=None, workspace_root=workspace,
        internal_workspace_root=tmp_path / "internal",
    )
    engine.agent = None
    worker_release = asyncio.Event()
    worker_started = asyncio.Event()
    aggregation_done = asyncio.Event()

    async def process_turn(content: str, target: str | None, *, internal: bool = False):
        del content, internal
        if target == "worker_1":
            worker_started.set()
            await worker_release.wait()
            await engine.notify_coordinator("aggregate worker result")

    async def harness_turn(content: str, target: str | None, *, internal: bool = False):
        del target, internal
        if content == "root request":
            engine._start_worker_turn(  # noqa: SLF001
                "worker_1", SimpleNamespace(goal="held worker"),
            )
        elif content == "aggregate worker result":
            aggregation_done.set()
        return True

    monkeypatch.setattr(engine, "_process_turn", process_turn)
    monkeypatch.setattr(engine, "_harness_turn_impl", harness_turn)
    async def model_ready(*_args, **_kwargs):
        return None
    monkeypatch.setattr("backend.engine.runtime_settings.wait_for_model_ready", model_ready)
    engine.start()
    future = await engine.submit_tracked("root request")
    await asyncio.wait_for(worker_started.wait(), timeout=2)
    await asyncio.sleep(0)
    assert not future.done()
    worker_release.set()
    await asyncio.wait_for(aggregation_done.wait(), timeout=2)
    result = await asyncio.wait_for(future, timeout=2)
    assert result["outcome"] == "succeeded"
    assert not engine._governed_task_scopes  # noqa: SLF001
    await engine.stop(immediate=True)


@pytest.mark.asyncio
async def test_real_engine_tracked_cancellation_propagates_to_scoped_worker(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace = tmp_path / "cancel-workspace"
    workspace.mkdir()
    engine = ProjectEngine(
        Hub(), "real-engine-cancel", agent=None, workspace_root=workspace,
        internal_workspace_root=tmp_path / "cancel-internal",
    )
    engine.agent = None
    worker_started = asyncio.Event()

    async def process_turn(_content: str, target: str | None, *, internal: bool = False):
        del internal
        if target == "worker_cancel":
            worker_started.set()
            await asyncio.Event().wait()

    async def harness_turn(_content: str, _target: str | None, *, internal: bool = False):
        del internal
        engine._start_worker_turn(  # noqa: SLF001
            "worker_cancel", SimpleNamespace(goal="held worker"),
        )
        return True

    monkeypatch.setattr(engine, "_process_turn", process_turn)
    monkeypatch.setattr(engine, "_harness_turn_impl", harness_turn)
    engine.start()
    future = await engine.submit_tracked("cancel root")
    await asyncio.wait_for(worker_started.wait(), timeout=2)
    future.cancel()
    for _ in range(100):
        if not engine._worker_turns and not engine._governed_task_scopes:  # noqa: SLF001
            break
        await asyncio.sleep(0.01)
    assert future.cancelled()
    assert not engine._worker_turns and not engine._governed_task_scopes  # noqa: SLF001
    await engine.stop(immediate=True)


def test_trusted_failure_taxonomy_and_verifier_rejection_are_distinct(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Failure provenance")

    failed = service.submit(group.group_id, title="Executor failed", request_key="executor-failed")
    service.schedule()
    failed = service.finalize_trusted_execution(
        group.group_id, failed.run_id, execution_key="executor-failed",
        outcome=TaskStatus.FAILED, failure_type=FailureType.TRANSIENT_PROVIDER_FAILURE,
    )
    assert failed.failure_type is FailureType.TRANSIENT_PROVIDER_FAILURE
    assert service.store.latest_verifier_decision(failed.run_id).approved is True  # type: ignore[union-attr]

    rejected = service.submit(group.group_id, title="Verifier rejected", request_key="verifier-rejected")
    service.schedule()
    rejected = service.finalize_trusted_execution(
        group.group_id, rejected.run_id, execution_key="verifier-rejected",
        outcome=TaskStatus.FAILED, verifier_approved=False,
    )
    assert rejected.failure_type is FailureType.VERIFICATION_FAILED
    assert service.store.latest_verifier_decision(rejected.run_id).approved is False  # type: ignore[union-attr]

    partial = service.submit(group.group_id, title="Partial", request_key="partial")
    service.schedule()
    partial = service.finalize_trusted_execution(
        group.group_id, partial.run_id, execution_key="partial",
        outcome=TaskStatus.PARTIAL,
        evidence=(EvidenceRef("partial", "engine", "v1", "turn"),),
    )
    assert partial.status is TaskStatus.PARTIAL and partial.failure_type is None

    cancelled = service.submit(group.group_id, title="Cancelled", request_key="cancelled")
    service.schedule()
    cancelled = service.finalize_trusted_execution(
        group.group_id, cancelled.run_id, execution_key="cancelled",
        outcome=TaskStatus.CANCELLED,
    )
    assert cancelled.status is TaskStatus.CANCELLED and cancelled.failure_type is None
    service.close()


def test_high_risk_ready_continuation_is_blocked_and_paused_on_restart(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("High risk restart")
    routed = service.route_message(
        group.group_id, content="张三成绩95分，排名第2", request_key="high-risk-route",
        confidence=1.0, read_only=True,
        executor_private_request={"project_id": group.group_id, "content": "private"},
        executor_execution_key="high-risk-execution",
    )
    approval_id = routed["approval"]["approval_id"]
    service.resolve_approval(
        group.group_id, approval_id, approved=True,
        capabilities=("route_message",), request_key="approve-high-risk",
    )
    continuation = service.store.executor_continuation(approval_id=approval_id)
    assert continuation is not None and continuation["status"] == "ready"
    run_id = continuation["run_id"]
    service.close()

    recovered = TaskRuntimeService(tmp_path, groups)
    try:
        continuation = recovered.store.executor_continuation(approval_id=approval_id)
        run = recovered.store.get_run(run_id)
        assert continuation is not None and continuation["status"] == "recovered_blocked"
        assert run is not None and run.status is TaskStatus.PAUSED
        assert "private" not in json.dumps(recovered.snapshot(group.group_id, run_id))
    finally:
        recovered.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type,expected", [
    ("missing_model", FailureType.TRANSIENT_PROVIDER_FAILURE),
    ("RuntimeError", FailureType.EXECUTOR_FAILURE),
])
async def test_server_bridge_preserves_trusted_executor_failure_provenance(
    tmp_path: Path, error_type: str, expected: FailureType,
) -> None:
    server = KnoweServer(str(tmp_path / error_type))
    group = server.groups.create_group(  # type: ignore[union-attr]
        "Bridge failure", legacy_project_id=f"project_{error_type}",
    )
    run = server.task_runtime.submit(  # type: ignore[union-attr]
        group.group_id, title="Bridge", request_key=f"run-{error_type}",
    )
    server.task_runtime.schedule()  # type: ignore[union-attr]
    await server._bridge_task_runtime_execution(  # noqa: SLF001
        group_id=group.group_id, run_id=run.run_id,
        project_id=f"project_{error_type}", execution_key=f"failure-{error_type}",
        completion={"outcome": "failed", "error_type": error_type},
    )
    terminal = server.task_runtime.store.get_run(run.run_id)  # type: ignore[union-attr]
    assert terminal is not None and terminal.status is TaskStatus.FAILED
    assert terminal.failure_type is expected
    _close_server(server)
