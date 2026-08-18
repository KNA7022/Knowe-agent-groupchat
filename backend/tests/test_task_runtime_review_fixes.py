from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from pathlib import Path

import pytest

from backend.groups import GroupNotFoundError, GroupRepository, StaleDeletePreviewError
from backend.server import KnoweServer
from backend.task_runtime import RuntimeConflictError, RuntimeNotFoundError, TaskRuntimeService
from backend.task_runtime.contracts import (
    AgentPhase, BudgetUsage, EvidenceRef, RiskLevel, RunEvent, TaskRun, TaskStatus,
)


class CaptureWriter:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, value: bytes) -> None:
        self.buffer.extend(value)

    def payload(self) -> tuple[int, dict]:
        head, body = bytes(self.buffer).split(b"\r\n\r\n", 1)
        return int(head.split(b" ", 2)[1]), json.loads(body.decode("utf-8"))


@pytest.mark.parametrize("field", [
    "token", "access_token", "refresh_token", "password", "authorization", "cookie",
])
def test_public_contract_recursively_rejects_credentials(field: str) -> None:
    with pytest.raises(ValueError, match="private field"):
        RunEvent(
            event_id="evt_" + "1" * 32, run_id="run_" + "2" * 32,
            group_id="grp_" + "3" * 32, seq=1, version=1,
            event_type="message_routed",
            public_payload={"route_label": "queue_new", "summary": {"nested": {field: "secret"}}},
        )


def test_hydration_rejects_unsupported_schema_and_invalid_identity() -> None:
    raw = {
        "run_id": "run_" + "1" * 32, "task_id": "task_" + "2" * 32,
        "group_id": "grp_" + "3" * 32, "kind": "primary", "title": "Task",
    }
    with pytest.raises(ValueError, match="unsupported"):
        TaskRun.from_dict({**raw, "schema_version": 2})
    with pytest.raises(ValueError, match="invalid run_id"):
        TaskRun.from_dict({**raw, "run_id": "run_forged"})


def close_server(server: KnoweServer) -> None:
    if server.task_runtime is not None:
        server.task_runtime.close()
    if server.store is not None:
        server.store.close(timeout=5)


def test_concurrent_budget_updates_use_fresh_transaction_state(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    first = TaskRuntimeService(tmp_path, groups)
    second = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Concurrent budget")
    run = first.submit(group.group_id, title="Budget", request_key="budget-run")
    first.schedule()
    barrier = threading.Barrier(2)

    def add(service: TaskRuntimeService, key: str) -> None:
        barrier.wait(timeout=5)
        service.consume_budget(group.group_id, run.run_id, BudgetUsage(tokens=7), request_key=key)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(add, first, "budget-a"), pool.submit(add, second, "budget-b")]
            for future in futures:
                future.result(timeout=10)
        stored = first.store.get_run(run.run_id)
        assert stored is not None and stored.budget_usage.tokens == 14
        assert stored.version == 5 and stored.last_seq == 5
    finally:
        first.close()
        second.close()


def test_concurrent_approval_requests_leave_one_policy_valid_pending_row(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    first = TaskRuntimeService(tmp_path, groups)
    second = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Concurrent approval")
    run = first.submit(group.group_id, title="Approval", request_key="approval-run")
    first.schedule()
    barrier = threading.Barrier(2)

    def request(service: TaskRuntimeService, key: str):
        barrier.wait(timeout=5)
        return service.request_approval(
            group.group_id, run.run_id, kind="tool", summary="Confirm exact read",
            risk=RiskLevel.MEDIUM, request_key=key, requested_capabilities=("read",),
            tool_name="read", operation_scope={"path": "notes"}, purpose="read_notes",
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(request, first, "approval-a"), pool.submit(request, second, "approval-b")]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=10))
                except RuntimeConflictError:
                    pass
        pending = first.store.pending_approvals(group.group_id)
        assert len(outcomes) == 1 and len(pending) == 1
        assert pending[0].tool_name == "read" and pending[0].purpose == "read_notes"
        assert first.store.get_run(run.run_id).status is TaskStatus.WAITING_APPROVAL  # type: ignore[union-attr]
    finally:
        first.close()
        second.close()


def test_durable_scheduler_lease_prevents_cross_instance_overbooking(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    first = TaskRuntimeService(tmp_path, groups)
    second = TaskRuntimeService(tmp_path, groups)
    for index in range(4):
        group = groups.create_group(f"Group {index}")
        first.submit(group.group_id, title=f"Task {index}", request_key=f"run-{index}")
    barrier = threading.Barrier(2)

    def schedule(service: TaskRuntimeService):
        barrier.wait(timeout=5)
        return service.schedule()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            left = pool.submit(schedule, first)
            right = pool.submit(schedule, second)
            left.result(timeout=10)
            right.result(timeout=10)
        running = first.store.list_runs(statuses={TaskStatus.RUNNING})
        assert len(running) == first.GLOBAL_PRIMARY_LIMIT
    finally:
        first.close()
        second.close()


def test_scheduler_repeated_turn_fairness_and_restart_dispatch(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    first_group = groups.create_group("First")
    second_group = groups.create_group("Second")
    service = TaskRuntimeService(tmp_path, groups)
    service.GLOBAL_PRIMARY_LIMIT = 1
    runs = [
        service.submit(first_group.group_id, title="First 1", request_key="first-1"),
        service.submit(first_group.group_id, title="First 2", request_key="first-2"),
        service.submit(second_group.group_id, title="Second", request_key="second"),
    ]
    started = service.schedule()[0]
    service.cancel(started.group_id, started.run_id, request_key="fair-cancel")
    next_run = next(run for run in service.store.list_runs() if run.status is TaskStatus.RUNNING)
    assert next_run.group_id != started.group_id
    recovery_group = groups.create_group("Recovery eligible")
    recovery_run = service.submit(
        recovery_group.group_id, title="Eligible after restart", request_key="recovery-eligible",
    )
    service.close()

    recovered = TaskRuntimeService(tmp_path, groups)
    try:
        interrupted = recovered.store.get_run(next_run.run_id)
        assert interrupted is not None and interrupted.status is TaskStatus.PAUSED
        assert recovered.store.get_run(recovery_run.run_id).status is TaskStatus.RUNNING  # type: ignore[union-attr]
        assert recovered.store.scheduler_cursor("index") == ""
    finally:
        recovered.close()


def test_verifier_decision_is_evidence_and_version_bound(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    group = groups.create_group("Verifier binding")
    service = TaskRuntimeService(tmp_path, groups)
    run = service.submit(group.group_id, title="Verify", request_key="verify-run")
    service.schedule()
    service.begin_verification(group.group_id, run.run_id, request_key="begin-verify")
    verifier = next(agent for agent in group.roster if agent.role == "verifier")
    service.update_agent(
        group.group_id, run.run_id, agent_id=verifier.agent_id, role="verifier",
        phase=AgentPhase.VERIFYING, idempotency_key="verifier-agent-verify",
    )
    with pytest.raises(RuntimeConflictError, match="evidence"):
        service.record_verifier_decision(
            group.group_id, run.run_id, verifier_agent_id=verifier.agent_id,
            outcome=TaskStatus.SUCCEEDED, approved=True, evidence=(), reason="none",
            request_key="no-evidence",
        )
    service.record_verifier_decision(
        group.group_id, run.run_id, verifier_agent_id=verifier.agent_id,
        outcome=TaskStatus.SUCCEEDED, approved=True,
        evidence=(EvidenceRef("ev", "source", "version", "page:2"),), reason="supported",
        request_key="decision",
    )
    service.checkpoint(group.group_id, run.run_id, label="version drift", request_key="drift")
    with pytest.raises(RuntimeConflictError, match="current task-bound"):
        service.complete(
            group.group_id, run.run_id, outcome=TaskStatus.SUCCEEDED,
            request_key="forged-complete", summary="forged",
        )
    service.close()


@pytest.mark.parametrize("boundary", [
    "after_group_prepare", "after_runtime_intent", "after_runtime_cleanup",
    "after_legacy_runtime_deleted", "after_group_commit", "after_runtime_complete",
])
async def test_group_deletion_recovers_after_every_durable_boundary(
    tmp_path: Path, boundary: str,
) -> None:
    data = tmp_path / boundary
    server = KnoweServer(str(data))
    group = server.groups.create_group("Delete", legacy_project_id=f"project_{boundary}")  # type: ignore[union-attr]
    run = server.task_runtime.submit(group.group_id, title="Private", request_key="private-run")  # type: ignore[union-attr]
    server.task_runtime.schedule()  # type: ignore[union-attr]
    token = server.groups.delete_preview(group.group_id).confirmation_token  # type: ignore[union-attr]

    def fail(point: str, _group_id: str) -> None:
        if point == boundary:
            raise OSError(f"crash:{boundary}")

    server._task_runtime_delete_fault = fail  # noqa: SLF001
    with pytest.raises(OSError, match="crash"):
        await server._run_confirmed_group_delete(  # noqa: SLF001
            group.group_id, str(group.legacy_project_id), token,
        )
    close_server(server)

    restarted = KnoweServer(str(data))
    try:
        await restarted._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        with pytest.raises(GroupNotFoundError):
            restarted.groups.get(group.group_id)  # type: ignore[union-attr]
        assert restarted.task_runtime.store.group_content_counts(group.group_id) == {  # type: ignore[union-attr]
            "runs": 0, "events": 0, "approvals": 0, "grants": 0,
                "tools": 0, "checkpoints": 0, "verifier_decisions": 0,
                "executor_continuations": 0,
            }
        with pytest.raises(RuntimeNotFoundError):
            restarted.task_runtime.snapshot(group.group_id, run.run_id)  # type: ignore[union-attr]
    finally:
        close_server(restarted)


async def test_invalid_delete_token_causes_zero_runtime_mutation(tmp_path: Path) -> None:
    server = KnoweServer(str(tmp_path))
    group = server.groups.create_group("Keep", legacy_project_id="project_keep")  # type: ignore[union-attr]
    run = server.task_runtime.submit(group.group_id, title="Keep", request_key="keep-run")  # type: ignore[union-attr]
    before = server.task_runtime.snapshot(group.group_id, run.run_id)  # type: ignore[union-attr]
    counts = server.task_runtime.store.group_content_counts(group.group_id)  # type: ignore[union-attr]
    with pytest.raises(StaleDeletePreviewError):
        await server._run_confirmed_group_delete(  # noqa: SLF001
            group.group_id, "project_keep", "0" * 64,
        )
    assert server.task_runtime.snapshot(group.group_id, run.run_id) == before  # type: ignore[union-attr]
    assert server.task_runtime.store.group_content_counts(group.group_id) == counts  # type: ignore[union-attr]
    assert server.groups.delete_transaction(group.group_id) is None  # type: ignore[union-attr]
    assert server.task_runtime.store.pending_group_deletions() == ()  # type: ignore[union-attr]
    close_server(server)


async def test_live_chat_and_http_mutations_broadcast_replayable_public_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    group = server.groups.create_group("Live", legacy_project_id="project_live")  # type: ignore[union-attr]
    server.hub.get_or_create("project_live", "Live")

    async def directory_required(_project_id: str) -> None:
        return None

    monkeypatch.setattr(server, "_project_dir_is_valid", lambda _project_id: False)
    monkeypatch.setattr(server, "_respond_directory_required", directory_required)
    await server._cmd_user_message(None, {  # noqa: SLF001
        "project_id": "project_live", "content": "Explain derivatives",
        "client_msg_id": "chat-live-1", "read_only": True,
    })
    await asyncio.sleep(0.05)
    runs = server.task_runtime.store.list_runs(group_id=group.group_id)  # type: ignore[union-attr]
    # The authoritative execution bridge now reflects the directory precondition
    # failure as terminal instead of leaking primary capacity indefinitely.
    assert len(runs) == 1 and runs[0].status is TaskStatus.FAILED
    public = [
        event for event in server.hub.projects["project_live"].ring.events()
        if event.get("type") == "task_runtime_event"
    ]
    assert public and all("sensitive_payload" not in event["event"] for event in public)

    writer = CaptureWriter()
    await server._serve_task_runtime_operation(  # noqa: SLF001
        writer, "/task-runtime/submit", json.dumps({
            "group_id": group.group_id, "title": "HTTP task", "request_key": "http-task",
        }).encode("utf-8"),
    )
    assert writer.payload()[0] == 200
    await asyncio.sleep(0.05)
    assert any(
        event.get("type") == "task_runtime_event"
        and event["event"]["event_type"] == "run_queued"
        for event in server.hub.projects["project_live"].ring.events()
    )

    before_dm = len(server.task_runtime.store.list_runs(group_id=group.group_id))  # type: ignore[union-attr]
    called: list[str] = []

    async def dm(*_args) -> None:
        called.append("dm")

    monkeypatch.setattr(server, "_cmd_user_message_dm", dm)
    await server._cmd_user_message(None, {  # noqa: SLF001
        "project_id": f"dm:project_live:{group.roster[0].agent_id}",
        "content": "private", "client_msg_id": "dm-1",
    })
    assert called == ["dm"]
    assert len(server.task_runtime.store.list_runs(group_id=group.group_id)) == before_dm  # type: ignore[union-attr]
    close_server(server)


async def test_http_rejects_non_finite_json_and_transport_cannot_forge_verifier(tmp_path: Path) -> None:
    server = KnoweServer(str(tmp_path))
    group = server.groups.create_group("Finite", legacy_project_id="project_finite")  # type: ignore[union-attr]
    writer = CaptureWriter()
    await server._serve_task_runtime_operation(  # noqa: SLF001
        writer, "/task-runtime/route",
        (f'{{"group_id":"{group.group_id}","content":"x","request_key":"nan","confidence":NaN}}').encode(),
    )
    assert writer.payload()[0] == 400

    run = server.task_runtime.submit(group.group_id, title="Forge", request_key="forge")  # type: ignore[union-attr]
    server.task_runtime.schedule()  # type: ignore[union-attr]
    server._task_runtime_dispatch("verify", {  # noqa: SLF001
        "group_id": group.group_id, "run_id": run.run_id, "request_key": "verify",
    })
    with pytest.raises(RuntimeConflictError, match="Verifier decision"):
        server._task_runtime_dispatch("complete", {  # noqa: SLF001
            "group_id": group.group_id, "run_id": run.run_id, "request_key": "forge-complete",
            "outcome": "succeeded", "summary": "forged", "verifier_approved": True,
        })
    close_server(server)
