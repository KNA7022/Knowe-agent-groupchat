from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.groups import GroupRepository
from backend.server import KnoweServer
from backend.task_runtime import RuntimeConflictError, RuntimeNotFoundError, TaskRuntimeService
from backend.task_runtime.contracts import (
    AgentPhase,
    Checkpoint,
    EvidenceRef,
    RiskLevel,
    RunEvent,
    TaskStatus,
    classify_public_text,
)


def _close_server(server: KnoweServer) -> None:
    if server.task_runtime is not None:
        server.task_runtime.close()
    if server.store is not None:
        server.store.close(timeout=5)


def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not satisfied before timeout")


def test_delete_intent_fences_late_mutations_and_concurrent_dispatch(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Deletion fence")
    run = service.submit(group.group_id, title="Existing task", request_key="existing")
    service.schedule()
    token = groups.delete_preview(group.group_id).confirmation_token
    transaction = groups.prepare_delete(
        group.group_id, token, runtime_project_id=str(group.legacy_project_id),
    )
    service.prepare_group_deletion(group.group_id, str(transaction["transaction_id"]))

    operations = (
        lambda: service.submit(group.group_id, title="Late", request_key="late-submit"),
        lambda: service.checkpoint(group.group_id, run.run_id, label="Late", request_key="late-checkpoint"),
        lambda: service.request_approval(
            group.group_id, run.run_id, kind="tool", summary="Late approval",
            risk=RiskLevel.MEDIUM, request_key="late-approval",
        ),
        lambda: service.prepare_tool(
            group.group_id, run.run_id, tool_name="read", idempotency_key="late-tool",
            risk=RiskLevel.LOW, public_summary="Late tool",
        ),
    )
    for operation in operations:
        with pytest.raises((RuntimeConflictError, RuntimeNotFoundError), match="deletion|fenced"):
            operation()

    barrier = threading.Barrier(2)

    def dispatch() -> tuple:
        barrier.wait(timeout=5)
        return service.schedule()

    def cleanup() -> dict[str, int]:
        barrier.wait(timeout=5)
        return service.clean_prepared_group_deletion(group.group_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        dispatched = pool.submit(dispatch)
        cleaned = pool.submit(cleanup)
        dispatched.result(timeout=10)
        cleaned.result(timeout=10)
    assert not any(service.store.group_content_counts(group.group_id).values())
    with pytest.raises(RuntimeConflictError, match="fenced"):
        service.submit(group.group_id, title="After cleanup", request_key="after-cleanup")
    service.close()


def test_restart_recleans_residue_after_canonical_group_delete(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Residue", legacy_project_id="project_residue")
    run = service.submit(group.group_id, title="Original", request_key="original")
    token = groups.delete_preview(group.group_id).confirmation_token
    transaction = groups.prepare_delete(group.group_id, token, runtime_project_id="project_residue")
    service.prepare_group_deletion(group.group_id, str(transaction["transaction_id"]))
    service.clean_prepared_group_deletion(group.group_id)
    groups.mark_runtime_deleted(str(transaction["transaction_id"]))
    groups.commit_delete(str(transaction["transaction_id"]))
    service.complete_group_deletion(group.group_id)
    database = Path(service.store.db.path)
    service.close()

    payload = run.to_dict()
    payload.update({"version": 1, "last_seq": 1})
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            """INSERT INTO studycrew_task_runs(
                   run_id, task_id, group_id, kind, queue_class, status, version,
                   last_seq, request_key, payload_json, created_at, updated_at
               ) VALUES (?, ?, ?, 'primary', 'foreground', 'queued', 1, 1, ?, ?, ?, ?)""",
            (run.run_id, run.task_id, group.group_id, "legacy-residue",
             json.dumps(payload), run.created_at, run.updated_at),
        )
        conn.commit()

    recovered = TaskRuntimeService(tmp_path, groups)
    try:
        assert not any(recovered.store.group_content_counts(group.group_id).values())
        with pytest.raises(RuntimeNotFoundError):
            recovered.snapshot(group.group_id, run.run_id)
    finally:
        recovered.close()


def _barrier_first_lookup(target: object, method_name: str, barrier: threading.Barrier) -> None:
    original = getattr(target, method_name)
    state = {"first": True}

    def wrapped(*args, **kwargs):
        if state["first"]:
            state["first"] = False
            result = original(*args, **kwargs)
            barrier.wait(timeout=5)
            return result
        return original(*args, **kwargs)

    setattr(target, method_name, wrapped)


def test_same_key_concurrent_side_records_return_durable_winner(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    first = TaskRuntimeService(tmp_path, groups)
    second = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Winner identity")
    run = first.submit(group.group_id, title="Winner", request_key="winner-run")
    first.schedule()

    approval_barrier = threading.Barrier(2)
    _barrier_first_lookup(first.store, "approval_by_request_key", approval_barrier)
    _barrier_first_lookup(second.store, "approval_by_request_key", approval_barrier)

    def approval(service: TaskRuntimeService):
        return service.request_approval(
            group.group_id, run.run_id, kind="tool", summary="Confirm read",
            risk=RiskLevel.MEDIUM, request_key="same-approval",
            requested_capabilities=("read",), tool_name="read",
            operation_scope={"path": "notes"}, purpose="read_notes",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        returned = [future.result(timeout=10) for future in (
            pool.submit(approval, first), pool.submit(approval, second),
        )]
    assert returned[0] == returned[1]
    assert returned[0] == first.store.approval_by_request_key("same-approval")

    first.resolve_approval(
        group.group_id, returned[0].approval_id, approved=True,
        capabilities=("read",), request_key="approve-read",
    )
    first.schedule()
    tool_barrier = threading.Barrier(2)
    _barrier_first_lookup(first.store, "tool_by_key", tool_barrier)
    _barrier_first_lookup(second.store, "tool_by_key", tool_barrier)

    def tool(service: TaskRuntimeService):
        return service.prepare_tool(
            group.group_id, run.run_id, tool_name="read", idempotency_key="same-tool",
            risk=RiskLevel.LOW, public_summary="Read notes", purpose="read_notes",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        tools = [future.result(timeout=10) for future in (
            pool.submit(tool, first), pool.submit(tool, second),
        )]
    assert tools[0] == tools[1] == first.store.tool_by_key(run.run_id, "same-tool")
    stored = first.store.get_run(run.run_id)
    assert stored is not None and stored.budget_usage.tool_calls == 1

    checkpoint_barrier = threading.Barrier(2)
    _barrier_first_lookup(first.store, "checkpoint_by_key", checkpoint_barrier)
    _barrier_first_lookup(second.store, "checkpoint_by_key", checkpoint_barrier)

    def checkpoint(service: TaskRuntimeService) -> Checkpoint:
        return service.checkpoint(
            group.group_id, run.run_id, label="Stable checkpoint", request_key="same-checkpoint",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        checkpoints = [future.result(timeout=10) for future in (
            pool.submit(checkpoint, first), pool.submit(checkpoint, second),
        )]
    assert checkpoints[0] == checkpoints[1]

    first.begin_verification(group.group_id, run.run_id, request_key="begin-winner-verification")
    verifier = next(agent for agent in group.roster if agent.role == "verifier")
    first.update_agent(
        group.group_id, run.run_id, agent_id=verifier.agent_id, role="verifier",
        phase=AgentPhase.VERIFYING, operation_summary="Verify winner",
        idempotency_key="winner-verifier-agent",
    )
    decision_barrier = threading.Barrier(2)
    for service in (first, second):
        original_owned = service._owned_run  # noqa: SLF001
        state = {"first": True}

        def wrapped_owned(*args, _original=original_owned, _state=state, **kwargs):
            result = _original(*args, **kwargs)
            if _state["first"]:
                _state["first"] = False
                decision_barrier.wait(timeout=5)
            return result

        service._owned_run = wrapped_owned  # type: ignore[method-assign]  # noqa: SLF001

    def decision(service: TaskRuntimeService):
        return service.record_verifier_decision(
            group.group_id, run.run_id, verifier_agent_id=verifier.agent_id,
            outcome=TaskStatus.SUCCEEDED, approved=True,
            evidence=(EvidenceRef("winner", "source", "version", "page:1"),),
            reason="Verified", request_key="same-verifier-decision",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        decisions = [future.result(timeout=10) for future in (
            pool.submit(decision, first), pool.submit(decision, second),
        )]
    assert decisions[0] == decisions[1]
    first.close()
    second.close()


def test_scheduler_retries_after_crash_lease_expiry_without_external_action(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    first = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Lease recovery")
    run = first.submit(group.group_id, title="Queued", request_key="queued")
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(seconds=0.25)).isoformat().replace("+00:00", "Z")
    current = now.isoformat().replace("+00:00", "Z")
    assert first.store.acquire_scheduler_lease("crashed-owner", now=current, expires_at=expires)
    first.close()

    recovered = TaskRuntimeService(tmp_path, groups)
    try:
        _wait_for(
            lambda: recovered.store.get_run(run.run_id).status is TaskStatus.RUNNING,  # type: ignore[union-attr]
            timeout=2.0,
        )
    finally:
        recovered.close()
    assert recovered._scheduler_retry is None  # noqa: SLF001


@pytest.mark.parametrize("content", [
    "张三成绩95分，排名第2",
    "Authorization: Bearer sk-secret",
])
def test_value_dlp_never_enters_public_event_title_or_snapshot(tmp_path: Path, content: str) -> None:
    with pytest.raises(ValueError, match="PII or credential"):
        RunEvent(
            event_id="evt_" + "1" * 32, run_id="run_" + "2" * 32,
            group_id="grp_" + "3" * 32, seq=1, version=1,
            event_type="message_routed",
            public_payload={"route_label": "queue_new", "summary": content},
        )
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("DLP")
    routed = service.route_message(
        group.group_id, content=content, request_key=f"route-{len(content)}",
        confidence=1.0, read_only=True,
    )
    assert routed["auto_routed"] is False
    run_id = routed["run"]["run_id"]
    public_blob = json.dumps({
        "snapshot": service.snapshot(group.group_id, run_id),
        "events": [event.to_public_dict() for event in service.store.events_after(run_id, 0)],
    }, ensure_ascii=False)
    assert content not in public_blob and "sk-secret" not in public_blob
    assert routed["run"]["data_classification"]
    service.close()


def test_value_dlp_avoids_generic_rank_and_timetable_false_positives() -> None:
    assert classify_public_text("Explain rank 2 tensors") == ()
    assert classify_public_text("查看课程表") == ()


def test_forward_migration_backfills_old_rows_and_dedupes_pending(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Migration")
    run = service.submit(group.group_id, title="Migration", request_key="migration-run")
    service.schedule()
    tool = service.prepare_tool(
        group.group_id, run.run_id, tool_name="read", idempotency_key="old-tool",
        risk=RiskLevel.LOW, public_summary="Read notes", purpose="read_notes",
    )
    granted_approval = service.request_approval(
        group.group_id, run.run_id, kind="tool", summary="Confirm grant",
        risk=RiskLevel.MEDIUM, request_key="grant-approval",
        requested_capabilities=("read",), tool_name="read", purpose="read_notes",
    )
    _resolved, grant, _run = service.resolve_approval(
        group.group_id, granted_approval.approval_id, approved=True,
        capabilities=("read",), request_key="resolve-grant",
    )
    assert grant is not None
    service.schedule()
    approval = service.request_approval(
        group.group_id, run.run_id, kind="tool", summary="Confirm",
        risk=RiskLevel.MEDIUM, request_key="old-approval",
        requested_capabilities=("read",), purpose="read_notes",
    )
    database = Path(service.store.db.path)
    service.close()

    duplicate_id = "apr_" + "f" * 32
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("DROP INDEX IF EXISTS idx_studycrew_one_pending_approval")
        original = json.loads(conn.execute(
            "SELECT payload_json FROM studycrew_approvals WHERE approval_id=?", (approval.approval_id,),
        ).fetchone()[0])
        for field in ("purpose", "operation_scope", "tool_name", "requested_capabilities"):
            original.pop(field, None)
        original.update({"approval_id": duplicate_id, "created_at": "2099-01-01T00:00:00Z"})
        conn.execute(
            """INSERT INTO studycrew_approvals(
                   approval_id, run_id, group_id, status, payload_json, created_at, resolved_at, request_key
               ) VALUES (?, ?, ?, 'pending', ?, ?, NULL, NULL)""",
            (duplicate_id, run.run_id, group.group_id, json.dumps(original), original["created_at"]),
        )
        conn.execute(
            "UPDATE studycrew_approvals SET payload_json=? WHERE approval_id=?",
            (json.dumps({key: value for key, value in original.items() if key != "approval_id"}
                        | {"approval_id": approval.approval_id}), approval.approval_id),
        )
        old_grant = json.loads(conn.execute(
            "SELECT payload_json FROM studycrew_permission_grants WHERE grant_id=?", (grant.grant_id,),
        ).fetchone()[0])
        for field in ("purpose", "operation_scope", "tool_name"):
            old_grant.pop(field, None)
        conn.execute(
            "UPDATE studycrew_permission_grants SET payload_json=? WHERE grant_id=?",
            (json.dumps(old_grant), grant.grant_id),
        )
        old_tool = json.loads(conn.execute(
            "SELECT payload_json FROM studycrew_tool_invocations WHERE invocation_id=?",
            (tool.invocation_id,),
        ).fetchone()[0])
        for field in ("purpose", "operation_scope", "disposition"):
            old_tool.pop(field, None)
        conn.execute(
            "UPDATE studycrew_tool_invocations SET payload_json=? WHERE invocation_id=?",
            (json.dumps(old_tool), tool.invocation_id),
        )
        conn.commit()

    recovered = TaskRuntimeService(tmp_path, groups)
    try:
        pending = recovered.store.pending_approvals(group.group_id)
        assert len(pending) == 1 and pending[0].purpose == "legacy_confirm_route"
        migrated_grant = recovered.store.grant_for_approval(granted_approval.approval_id)
        migrated_tool = recovered.store.tool_by_key(run.run_id, "old-tool")
        assert migrated_grant is not None and migrated_grant.purpose == "legacy_approved_operation"
        assert migrated_tool is not None and migrated_tool.purpose == "legacy_execute_tool"
        assert migrated_tool.disposition == "created"
        with recovered.store.db.transaction(immediate=False) as conn:
            tombstones = conn.execute(
                "SELECT record_id, resolution FROM studycrew_migration_tombstones"
            ).fetchall()
        assert [(row["record_id"], row["resolution"]) for row in tombstones] == [
            (duplicate_id, "superseded_duplicate_pending"),
        ]
    finally:
        recovered.close()
    second_open = TaskRuntimeService(tmp_path, groups)
    second_open.close()


async def test_live_engine_completion_reaches_verifier_terminal_and_dispatches_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    group = server.groups.create_group("Completion", legacy_project_id="project_completion")  # type: ignore[union-attr]
    server.hub.get_or_create("project_completion", "Completion")

    class TrackedEngine:
        def __init__(self) -> None:
            self.future: asyncio.Future[dict] | None = None

        def resolve_group_mentions(self, _content: str):
            return SimpleNamespace(worker_ids=())

        async def submit_tracked(self, _content: str, attachments=None):
            self.future = asyncio.get_running_loop().create_future()
            return self.future

    engine = TrackedEngine()
    monkeypatch.setattr(server, "_project_dir_is_valid", lambda _project_id: True)
    monkeypatch.setattr(server, "engine_for", lambda _project_id: engine)
    monkeypatch.setattr(server, "_prepare_attachments", lambda *_args: asyncio.sleep(0, result=(True, [])))
    await server._cmd_user_message(None, {  # noqa: SLF001
        "project_id": "project_completion", "content": "Explain derivatives",
        "client_msg_id": "completion-1", "read_only": False,
    })
    first = server.task_runtime.store.list_runs(group_id=group.group_id)[0]  # type: ignore[union-attr]
    queued = server.task_runtime.submit(  # type: ignore[union-attr]
        group.group_id, title="Next task", request_key="next-task",
    )
    assert engine.future is not None
    engine.future.set_result({"outcome": "succeeded"})

    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        terminal = server.task_runtime.store.get_run(first.run_id)  # type: ignore[union-attr]
        next_run = server.task_runtime.store.get_run(queued.run_id)  # type: ignore[union-attr]
        if terminal and terminal.status is TaskStatus.SUCCEEDED and next_run and next_run.status is TaskStatus.RUNNING:
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("trusted completion did not terminate and release capacity")

    events = server.task_runtime.store.events_after(first.run_id, 0)  # type: ignore[union-attr]
    verifier_events = [event for event in events if event.event_type == "verifier_decision"]
    assert verifier_events and verifier_events[0].public_payload["evidence_count"] == 1
    duplicate = server.task_runtime.finalize_trusted_execution(  # type: ignore[union-attr]
        group.group_id, first.run_id, execution_key="chat:project_completion:completion-1",
        outcome=TaskStatus.SUCCEEDED,
        evidence=(EvidenceRef("duplicate", "engine", "turn", "completion"),),
    )
    assert duplicate.status is TaskStatus.SUCCEEDED
    assert len([event for event in server.task_runtime.store.events_after(first.run_id, 0)  # type: ignore[union-attr]
                if event.event_type == "verifier_decision"]) == 1
    _close_server(server)


def test_trusted_completion_bridge_reflects_partial_and_failed_outcomes(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Completion outcomes")
    partial = service.submit(group.group_id, title="Partial", request_key="partial-run")
    service.schedule()
    partial_result = service.finalize_trusted_execution(
        group.group_id, partial.run_id, execution_key="partial-executor",
        outcome=TaskStatus.PARTIAL,
        evidence=(EvidenceRef("partial", "engine", "turn", "completion"),),
        summary="Task partially completed",
    )
    assert partial_result.status is TaskStatus.PARTIAL

    failed = service.submit(group.group_id, title="Failed", request_key="failed-run")
    service.schedule()
    failed_result = service.finalize_trusted_execution(
        group.group_id, failed.run_id, execution_key="failed-executor",
        outcome=TaskStatus.FAILED, summary="Task failed",
    )
    assert failed_result.status is TaskStatus.FAILED
    assert service.store.latest_verifier_decision(failed.run_id).approved is True  # type: ignore[union-attr]
    service.close()
