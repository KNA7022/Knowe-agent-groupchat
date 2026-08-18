from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.groups import GroupRepository
from backend.server import KnoweServer
from backend.task_runtime import RuntimeConflictError, TaskRuntimeService
from backend.task_runtime.contracts import EvidenceRef, RiskLevel, TaskStatus


class CaptureWriter:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, value: bytes) -> None:
        self.buffer.extend(value)

    def payload(self) -> tuple[int, dict]:
        head, body = bytes(self.buffer).split(b"\r\n\r\n", 1)
        return int(head.split(b" ", 2)[1]), json.loads(body.decode("utf-8"))


def _future(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace(
        "+00:00", "Z",
    )


def _high_risk_ready(
    service: TaskRuntimeService, group_id: str, *, project_id: str,
    prefix: str,
) -> tuple[str, str, str]:
    original = "张三成绩95分，排名第2"
    routed = service.route_message(
        group_id, content=original, request_key=f"{prefix}-route",
        confidence=1.0, read_only=True,
        executor_private_request={"project_id": project_id, "content": original},
        executor_execution_key=f"{prefix}-execution",
    )
    approval_id = routed["approval"]["approval_id"]
    service.resolve_approval(
        group_id, approval_id, approved=True,
        capabilities=("route_message",), request_key=f"{prefix}-approve",
    )
    continuation = service.store.executor_continuation(approval_id=approval_id)
    assert continuation is not None and continuation["status"] == "ready"
    return approval_id, str(continuation["run_id"]), original


def test_nearest_expiry_watcher_fires_while_idle_and_releases_capacity(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Autonomous expiry")
    run = service.submit(group.group_id, title="Expiring", request_key="idle-expiry-run")
    service.schedule()
    approval = service.request_approval(
        group.group_id, run.run_id, kind="message_route", summary="Confirm route",
        risk=RiskLevel.HIGH, request_key="idle-expiry-approval",
        requested_capabilities=("route_message",), operation_scope={"route": "queue_new"},
        purpose="confirm_route", expires_at=_future(0.05),
        executor_private_request={"project_id": group.group_id, "content": "private"},
        executor_execution_key="idle-expiry-execution", route_request_key="idle-expiry-route",
    )
    queued = service.submit(group.group_id, title="Next", request_key="idle-expiry-next")
    service.schedule()
    assert service.store.get_run(queued.run_id).status is TaskStatus.QUEUED  # type: ignore[union-attr]

    # No snapshot, summary, resolution, or scheduler call occurs during this wait.
    time.sleep(0.20)
    assert service.store.get_approval(approval.approval_id).status == "rejected"  # type: ignore[union-attr]
    assert service.store.get_run(run.run_id).status is TaskStatus.FAILED  # type: ignore[union-attr]
    continuation = service.store.executor_continuation(approval_id=approval.approval_id)
    assert continuation is not None and continuation["status"] == "rejected"
    assert service.store.get_run(queued.run_id).status is TaskStatus.RUNNING  # type: ignore[union-attr]
    assert len([
        event for event in service.store.events_after(run.run_id, 0)
        if event.event_type == "approval_resolved"
    ]) == 1
    service.close()


def test_restart_reconstructs_nearest_expiry_watcher(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    first = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Restart watcher")
    run = first.submit(group.group_id, title="Expiring", request_key="restart-watch-run")
    first.schedule()
    approval = first.request_approval(
        group.group_id, run.run_id, kind="message_route", summary="Confirm route",
        risk=RiskLevel.MEDIUM, request_key="restart-watch-approval",
        requested_capabilities=("route_message",), operation_scope={"route": "queue_new"},
        purpose="confirm_route", expires_at=_future(0.18),
    )
    first.close()
    time.sleep(0.04)

    recovered = TaskRuntimeService(tmp_path, groups)
    time.sleep(0.22)
    try:
        assert recovered.store.get_approval(approval.approval_id).status == "rejected"  # type: ignore[union-attr]
        assert recovered.store.get_run(run.run_id).status is TaskStatus.FAILED  # type: ignore[union-attr]
        assert len([
            event for event in recovered.store.events_after(run.run_id, 0)
            if event.event_type == "approval_resolved"
        ]) == 1
    finally:
        recovered.close()


def test_two_service_expiry_timer_race_commits_one_resolution_event(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    first = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Timer race")
    run = first.submit(group.group_id, title="Race", request_key="timer-race-run")
    first.schedule()
    approval = first.request_approval(
        group.group_id, run.run_id, kind="message_route", summary="Confirm route",
        risk=RiskLevel.MEDIUM, request_key="timer-race-approval",
        requested_capabilities=("route_message",), operation_scope={"route": "queue_new"},
        purpose="confirm_route", expires_at=_future(0.08),
    )
    second = TaskRuntimeService(tmp_path, groups)
    time.sleep(0.24)
    try:
        assert first.store.get_approval(approval.approval_id).status == "rejected"  # type: ignore[union-attr]
        events = [
            event for event in second.store.events_after(run.run_id, 0)
            if event.event_type == "approval_resolved"
        ]
        assert len(events) == 1 and events[0].public_payload["resolution"] == "expired"
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("expires_at", [
    "not-a-date",
    "2026-08-19T12:00:00",
    (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
    "9999-12-31T23:59:59Z",
])
def test_service_rejects_invalid_expiry_before_any_mutation(
    tmp_path: Path, expires_at: str,
) -> None:
    root = tmp_path / expires_at[:4].replace(":", "_")
    groups = GroupRepository(root)
    service = TaskRuntimeService(root, groups)
    group = groups.create_group("Invalid expiry")
    run = service.submit(group.group_id, title="Keep healthy", request_key="invalid-expiry-run")
    service.schedule()
    before = service.store.get_run(run.run_id).to_dict()  # type: ignore[union-attr]
    before_events = service.store.events_after(run.run_id, 0)
    with pytest.raises(ValueError, match="expiry"):
        service.request_approval(
            group.group_id, run.run_id, kind="message_route", summary="Confirm route",
            risk=RiskLevel.MEDIUM, request_key=f"invalid-{expires_at}",
            requested_capabilities=("route_message",), operation_scope={"route": "queue_new"},
            purpose="confirm_route", expires_at=expires_at,
        )
    assert service.store.get_run(run.run_id).to_dict() == before  # type: ignore[union-attr]
    assert service.store.events_after(run.run_id, 0) == before_events
    assert service.store.pending_approvals(group.group_id) == ()
    service.close()
    reopened = TaskRuntimeService(root, groups)
    try:
        assert reopened.snapshot(group.group_id, run.run_id)["run"]["status"] == "paused"
    finally:
        reopened.close()


def test_legacy_bad_expiry_is_quarantined_and_second_open_is_stable(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Legacy bad expiry")
    run = service.submit(group.group_id, title="Legacy", request_key="legacy-expiry-run")
    service.schedule()
    approval = service.request_approval(
        group.group_id, run.run_id, kind="message_route", summary="Confirm route",
        risk=RiskLevel.MEDIUM, request_key="legacy-expiry-approval",
        requested_capabilities=("route_message",), operation_scope={"route": "queue_new"},
        purpose="confirm_route",
    )
    database = Path(service.store.db.path)
    service.close()
    with sqlite3.connect(database) as conn:
        raw = json.loads(conn.execute(
            "SELECT payload_json FROM studycrew_approvals WHERE approval_id=?",
            (approval.approval_id,),
        ).fetchone()[0])
        raw["expires_at"] = "not-a-date"
        conn.execute(
            "UPDATE studycrew_approvals SET payload_json=? WHERE approval_id=?",
            (json.dumps(raw, ensure_ascii=False, separators=(",", ":")), approval.approval_id),
        )
        conn.commit()

    first = TaskRuntimeService(tmp_path, groups)
    durable = first.store.get_approval(approval.approval_id)
    assert durable is not None and durable.status == "rejected"
    assert durable.expires_at is not None and durable.expires_at.endswith("Z")
    TaskRuntimeService._parse_approval_expiry(durable.expires_at)
    assert first.store.get_run(run.run_id).status is TaskStatus.FAILED  # type: ignore[union-attr]
    events_first = first.store.events_after(run.run_id, 0)
    assert len([event for event in events_first if event.event_type == "approval_resolved"]) == 1
    with first.store.db.transaction(immediate=False) as conn:
        normalized_first = str(conn.execute(
            "SELECT payload_json FROM studycrew_approvals WHERE approval_id=?",
            (approval.approval_id,),
        ).fetchone()[0])
    assert "not-a-date" not in normalized_first
    first.close()

    second = TaskRuntimeService(tmp_path, groups)
    try:
        assert second.store.events_after(run.run_id, 0) == events_first
        with second.store.db.transaction(immediate=False) as conn:
            normalized_second = str(conn.execute(
                "SELECT payload_json FROM studycrew_approvals WHERE approval_id=?",
                (approval.approval_id,),
            ).fetchone()[0])
        assert normalized_second == normalized_first
    finally:
        second.close()


@pytest.mark.asyncio
async def test_http_ws_reject_raw_expiry_without_recovery_mutation(
    tmp_path: Path, monkeypatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    group = server.groups.create_group(  # type: ignore[union-attr]
        "Transport expiry", legacy_project_id="project_transport_expiry",
    )
    approval_id, run_id, _original = _high_risk_ready(
        server.task_runtime, group.group_id,  # type: ignore[arg-type]
        project_id="project_transport_expiry", prefix="transport-expiry",
    )
    server.task_runtime.close()  # type: ignore[union-attr]
    server.task_runtime = TaskRuntimeService(tmp_path, server.groups)  # type: ignore[arg-type]
    blocked = server.task_runtime.store.executor_continuation(approval_id=approval_id)
    assert blocked is not None and blocked["status"] == "recovered_blocked"
    before = server.task_runtime.store.get_run(run_id).to_dict()  # type: ignore[union-attr]
    event_count = len(server.task_runtime.store.events_after(run_id, 0))

    payload = {
        "group_id": group.group_id, "continuation_id": blocked["continuation_id"],
        "request_key": "bad-transport-expiry", "expires_at": "not-a-date",
    }
    writer = CaptureWriter()
    await server._serve_task_runtime_operation(  # noqa: SLF001
        writer, "/task-runtime/reapprove-recovered",
        json.dumps(payload).encode("utf-8"),
    )
    assert writer.payload()[0] == 400

    sent: list[dict] = []

    async def send_to(_client, body: dict) -> None:
        sent.append(body)

    monkeypatch.setattr(server.hub, "send_to", send_to)
    with pytest.raises(ValueError, match="ttl_seconds"):
        await server._cmd_task_runtime(object(), {  # type: ignore[arg-type]  # noqa: SLF001
            "type": "task_runtime", "action": "reapprove-recovered", **payload,
        })
    assert sent == []
    assert server.task_runtime.store.get_run(run_id).to_dict() == before  # type: ignore[union-attr]
    assert len(server.task_runtime.store.events_after(run_id, 0)) == event_count
    assert server.task_runtime.store.pending_approvals(group.group_id) == ()
    server.task_runtime.close()
    if server.store is not None:
        server.store.close(timeout=5)


def test_recovered_reapproval_inherits_exact_purpose_and_digest_binding(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Purpose binding", legacy_project_id="project_purpose_binding")
    first_approval_id, run_id, original = _high_risk_ready(
        service, group.group_id, project_id="project_purpose_binding", prefix="purpose",
    )
    service.close()

    recovered = TaskRuntimeService(tmp_path, groups)
    blocked = recovered.store.executor_continuation(approval_id=first_approval_id)
    assert blocked is not None and blocked["status"] == "recovered_blocked"
    before = recovered.store.get_run(run_id).to_dict()  # type: ignore[union-attr]
    event_count = len(recovered.store.events_after(run_id, 0))
    with pytest.raises(RuntimeConflictError, match="purpose"):
        recovered.reapprove_recovered_continuation(
            group.group_id, blocked["continuation_id"],
            purpose="export_grades", operation_scope={"route": "queue_new"},
            request_key="purpose-launder",
        )
    assert recovered.store.get_run(run_id).to_dict() == before  # type: ignore[union-attr]
    assert len(recovered.store.events_after(run_id, 0)) == event_count
    assert recovered.store.pending_approvals(group.group_id) == ()
    assert recovered.store.executor_continuation(
        continuation_id=blocked["continuation_id"],
    )["status"] == "recovered_blocked"  # type: ignore[index]
    assert len(recovered.store.executor_continuations_for_run(run_id)) == 1
    with recovered.store.db.transaction(immediate=False) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM studycrew_permission_grants WHERE run_id=?", (run_id,),
        ).fetchone()[0] == 0

    approval, waiting = recovered.reapprove_recovered_continuation(
        group.group_id, blocked["continuation_id"],
        operation_scope={"route": "queue_new"}, request_key="proper-recovery",
    )
    assert waiting.status is TaskStatus.WAITING_APPROVAL
    assert approval.purpose == "confirm_route"
    digest = approval.operation_scope["request_digest"]
    continuation = recovered.store.executor_continuation(approval_id=approval.approval_id)
    assert continuation is not None
    assert continuation["inherited_purpose"] == "confirm_route"
    assert continuation["private_request_digest"] == digest
    duplicate, _ = recovered.reapprove_recovered_continuation(
        group.group_id, blocked["continuation_id"], request_key="proper-recovery",
    )
    assert duplicate.approval_id == approval.approval_id

    resolved, grant, _run = recovered.resolve_approval(
        group.group_id, approval.approval_id, approved=True,
        capabilities=("route_message",), request_key="proper-recovery-approve",
    )
    assert resolved.purpose == "confirm_route"
    assert grant is not None and grant.purpose == "confirm_route"
    assert grant.operation_scope == approval.operation_scope
    claimed, did_claim = recovered.claim_approved_executor_continuation(
        group.group_id, approval.approval_id,
    )
    assert did_claim and claimed is not None
    assert claimed["inherited_purpose"] == "confirm_route"
    assert claimed["private_request"]["content"] == original
    duplicate_claim, did_claim_again = recovered.claim_approved_executor_continuation(
        group.group_id, approval.approval_id,
    )
    assert duplicate_claim is not None and not did_claim_again
    recovered.finish_executor_continuation(claimed["continuation_id"], status="completed")
    recovered.finalize_trusted_execution(
        group.group_id, run_id, execution_key=claimed["execution_key"],
        outcome=TaskStatus.SUCCEEDED,
        evidence=(EvidenceRef("purpose", "engine", "v1", "turn"),),
    )
    recovered.close()

    reopened = TaskRuntimeService(tmp_path, groups)
    try:
        stable = reopened.store.executor_continuation(approval_id=approval.approval_id)
        assert stable is not None and stable["status"] == "completed"
        assert stable["inherited_purpose"] == "confirm_route"
        stable_grant = reopened.store.grant_for_approval(approval.approval_id)
        assert stable_grant is not None and stable_grant.purpose == "confirm_route"
        recovery_events = [
            event for event in reopened.store.events_after(run_id, 0)
            if event.event_type == "approval_requested"
            and event.public_payload.get("approval", {}).get("approval_id") == approval.approval_id
        ]
        assert recovery_events[-1].public_payload["approval"]["operation_scope"]["request_digest"] == digest
        assert reopened.store.get_run(run_id).status is TaskStatus.SUCCEEDED  # type: ignore[union-attr]
    finally:
        reopened.close()
