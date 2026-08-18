from __future__ import annotations

from pathlib import Path

import pytest

from backend.groups import GroupRepository
from backend.server import KnoweServer
from backend.task_runtime import RuntimeConflictError, RuntimeNotFoundError, TaskRuntimeService
from backend.task_runtime.contracts import (
    AgentPhase,
    BudgetLimits,
    BudgetUsage,
    EvidenceRef,
    FailureType,
    QueueClass,
    RiskLevel,
    RouteLabel,
    RunKind,
    TaskStatus,
)


@pytest.fixture
def runtime(tmp_path: Path):
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    try:
        yield groups, service
    finally:
        service.close()


def make_group(groups: GroupRepository, name: str):
    return groups.create_group(name)


def submit_primary(service: TaskRuntimeService, group_id: str, key: str, **kwargs):
    return service.submit(group_id, title=f"Task {key}", request_key=key, **kwargs)


def verifier_for(group):
    return next(agent for agent in group.roster if agent.role == "verifier")


def approve_completion(service, group, run_id: str, outcome: TaskStatus = TaskStatus.SUCCEEDED):
    verifier = verifier_for(group)
    service.update_agent(
        group.group_id, run_id, agent_id=verifier.agent_id, role="verifier",
        phase=AgentPhase.VERIFYING, operation_summary="Checking evidence",
        idempotency_key=f"verifier-agent:{run_id}",
    )
    return service.record_verifier_decision(
        group.group_id, run_id, verifier_agent_id=verifier.agent_id,
        outcome=outcome, approved=True,
        evidence=(EvidenceRef("ev-1", "src-1", "ver-1", "page:1"),),
        reason="Evidence supports the result", request_key=f"verifier-decision:{run_id}",
    )


def test_state_machine_rejects_invalid_transitions_and_requires_verifier(runtime) -> None:
    groups, service = runtime
    group = make_group(groups, "State")
    run = submit_primary(service, group.group_id, "state-1")
    with pytest.raises(RuntimeConflictError, match="queued -> succeeded"):
        service.transition(group.group_id, run.run_id, TaskStatus.SUCCEEDED, idempotency_key="bad")

    running = service.schedule()[0]
    with pytest.raises(RuntimeConflictError, match="only verifying"):
        service.complete(
            group.group_id, running.run_id, outcome=TaskStatus.SUCCEEDED,
            request_key="complete-too-soon", summary="No",
        )
    verifying = service.begin_verification(group.group_id, running.run_id, request_key="verify")
    assert verifying.status is TaskStatus.VERIFYING
    with pytest.raises(RuntimeConflictError, match="Verifier"):
        service.complete(
            group.group_id, running.run_id, outcome=TaskStatus.SUCCEEDED,
            request_key="complete-unverified", summary="No",
        )
    approve_completion(service, group, running.run_id)
    done = service.complete(
        group.group_id, running.run_id, outcome=TaskStatus.SUCCEEDED,
        request_key="complete", summary="Verified answer",
    )
    assert done.status is TaskStatus.SUCCEEDED


def test_agent_state_machine_rejects_invalid_phase_jumps(runtime) -> None:
    groups, service = runtime
    group = make_group(groups, "Agent state")
    run = submit_primary(service, group.group_id, "agent-state")
    service.schedule()
    planned = service.update_agent(
        group.group_id, run.run_id, agent_id="coordinator", role="coordinator",
        phase=AgentPhase.PLANNING, idempotency_key="agent-plan",
    )
    responding = service.update_agent(
        group.group_id, run.run_id, agent_id="coordinator", role="coordinator",
        phase=AgentPhase.RESPONDING, idempotency_key="agent-respond",
    )
    assert responding.agent_runs[0].phase is AgentPhase.RESPONDING
    assert service.update_agent(
        group.group_id, run.run_id, agent_id="coordinator", role="coordinator",
        phase=AgentPhase.RESPONDING, idempotency_key="agent-respond",
    ).version == responding.version
    with pytest.raises(RuntimeConflictError, match="responding -> retrieving"):
        service.update_agent(
            group.group_id, run.run_id, agent_id="coordinator", role="coordinator",
            phase=AgentPhase.RETRIEVING, idempotency_key="agent-invalid",
        )
    assert planned.agent_runs[0].phase is AgentPhase.PLANNING


def test_event_sequence_version_replay_snapshot_gap_and_idempotency(runtime) -> None:
    groups, service = runtime
    group = make_group(groups, "Events")
    first = submit_primary(service, group.group_id, "same-request")
    duplicate = submit_primary(service, group.group_id, "same-request")
    assert duplicate.run_id == first.run_id
    assert duplicate.last_seq == 1

    routed = service.transition(
        group.group_id, first.run_id, TaskStatus.ROUTING, idempotency_key="route-1",
    )
    event = service.store.event_by_key(first.run_id, "route-1")
    retried = service.transition(
        group.group_id, first.run_id, TaskStatus.ROUTING, idempotency_key="route-1",
    )
    assert retried.version == routed.version == 2
    assert retried.last_seq == routed.last_seq == 2
    assert service.store.event_by_key(first.run_id, "route-1").event_id == event.event_id

    replay = service.replay(group.group_id, first.run_id, after_seq=0)
    assert [row["seq"] for row in replay["events"]] == [1, 2]
    assert [row["version"] for row in replay["events"]] == [1, 2]
    assert all("sensitive_payload" not in row for row in replay["events"])
    gap = service.replay(group.group_id, first.run_id, after_seq=999, after_version=999)
    assert gap["gap"] is True
    assert gap["snapshot"]["run"]["run_id"] == first.run_id


def test_snapshot_and_event_roll_back_together_when_transaction_hook_fails(runtime) -> None:
    groups, service = runtime
    group = make_group(groups, "Atomic")
    run = submit_primary(service, group.group_id, "atomic")

    def fail(_connection, _updated, _event):
        raise OSError("injected durable side-record failure")

    with pytest.raises(OSError, match="injected"):
        service.store.change_run(
            run.run_id, event_type="status_changed",
            public_payload={"from": "queued", "status": "routing", "summary": "",
                            "blocking_reason": "", "failure_type": None},
            idempotency_key="atomic-failure",
            mutate=lambda item: setattr(item, "status", TaskStatus.ROUTING),
            transaction_hook=fail,
        )
    stored = service.store.get_run(run.run_id)
    assert stored is not None and stored.status is TaskStatus.QUEUED
    assert stored.version == 1 and stored.last_seq == 1
    assert service.store.event_by_key(run.run_id, "atomic-failure") is None


def test_public_payload_rejects_chain_of_thought_and_keeps_private_separate(runtime) -> None:
    groups, service = runtime
    group = make_group(groups, "Privacy")
    run = submit_primary(service, group.group_id, "privacy")
    with pytest.raises(ValueError, match="private field"):
        service.store.change_run(
            run.run_id, event_type="message_routed",
            public_payload={"route_label": "queue_new", "chain_of_thought": "hidden"},
            idempotency_key="bad-public",
        )
    _run, event, _created = service.store.change_run(
        run.run_id, event_type="message_routed",
        public_payload={"route_label": "queue_new", "summary": "public"},
        sensitive_payload={"content": "private document text"}, sensitivity="private",
        idempotency_key="private-event",
    )
    assert "sensitive_payload" not in event.to_public_dict()
    replay = service.replay(group.group_id, run.run_id, after_seq=1)
    assert "private document text" not in str(replay)


def test_budget_exhaustion_is_deterministic_and_public(runtime) -> None:
    groups, service = runtime
    group = make_group(groups, "Budget")
    run = submit_primary(
        service, group.group_id, "budget", budget_limits=BudgetLimits(
            wall_clock_seconds=10, model_turns=1, tool_calls=1, tokens=10, cost_usd=1,
        ),
    )
    service.schedule()
    exhausted = service.consume_budget(
        group.group_id, run.run_id, BudgetUsage(model_turns=2), request_key="usage",
    )
    assert exhausted.status is TaskStatus.FAILED
    assert exhausted.failure_type is FailureType.BUDGET_EXHAUSTED
    assert exhausted.blocking_reason == "budget exhausted: model_turns"
    assert service.replay(group.group_id, run.run_id, after_seq=0)["events"][-1][
        "public_payload"
    ]["exhausted_dimension"] == "model_turns"


def test_scheduler_enforces_fair_global_and_per_group_primary_limits(runtime) -> None:
    groups, service = runtime
    first = make_group(groups, "First")
    second = make_group(groups, "Second")
    third = make_group(groups, "Third")
    for index in range(3):
        submit_primary(service, first.group_id, f"first-{index}")
    submit_primary(service, second.group_id, "second")
    submit_primary(service, third.group_id, "third")

    started = service.schedule()
    assert len(started) == service.GLOBAL_PRIMARY_LIMIT
    assert len({run.group_id for run in started}) == 2
    assert sum(run.group_id == first.group_id for run in started) <= 1
    assert service.schedule() == ()
    running = service.store.list_runs(statuses={TaskStatus.RUNNING})
    assert len(running) == 2
    assert all(
        sum(item.group_id == group_id and item.kind is RunKind.PRIMARY for item in running) <= 1
        for group_id in (first.group_id, second.group_id, third.group_id)
    )


def test_side_tasks_are_read_only_isolated_and_index_queue_has_separate_capacity(runtime) -> None:
    groups, service = runtime
    first = make_group(groups, "Side A")
    second = make_group(groups, "Side B")
    with pytest.raises(ValueError, match="low-risk and read-only"):
        service.submit(
            first.group_id, title="unsafe", request_key="unsafe", kind=RunKind.SIDE,
            read_only=False,
        )
    for index in range(3):
        service.submit(
            first.group_id, title=f"Side {index}", request_key=f"side-a-{index}",
            kind=RunKind.SIDE, read_only=True,
        )
    service.submit(
        second.group_id, title="Side other", request_key="side-b",
        kind=RunKind.SIDE, read_only=True,
    )
    service.submit(
        first.group_id, title="Index", request_key="index-a", kind=RunKind.SIDE,
        queue_class=QueueClass.INDEX, read_only=True,
    )
    started = service.schedule()
    foreground = [run for run in started if run.queue_class is QueueClass.FOREGROUND]
    index = [run for run in started if run.queue_class is QueueClass.INDEX]
    assert len(foreground) == service.GLOBAL_SIDE_LIMIT
    assert len(index) == service.GLOBAL_INDEX_LIMIT
    assert all(run.read_only and run.risk is RiskLevel.LOW for run in foreground)


def test_pause_cancel_resume_and_checkpoint_are_idempotent(runtime) -> None:
    groups, service = runtime
    group = make_group(groups, "Controls")
    run = submit_primary(service, group.group_id, "control")
    service.schedule()
    checkpoint = service.checkpoint(group.group_id, run.run_id, label="before", request_key="ck")
    assert service.checkpoint(
        group.group_id, run.run_id, label="ignored", request_key="ck",
    ).checkpoint_id == checkpoint.checkpoint_id
    paused = service.pause(group.group_id, run.run_id, request_key="pause")
    assert paused.status is TaskStatus.PAUSED
    assert service.pause(group.group_id, run.run_id, request_key="pause").version == paused.version
    resumed = service.resume(group.group_id, run.run_id, request_key="resume")
    assert resumed.status is TaskStatus.RUNNING
    cancelled = service.cancel(group.group_id, run.run_id, request_key="cancel")
    assert cancelled.status is TaskStatus.CANCELLED
    assert service.cancel(group.group_id, run.run_id, request_key="cancel").version == cancelled.version


def test_approval_gates_ambiguous_sensitive_routing_and_high_risk_tool(runtime) -> None:
    groups, service = runtime
    group = make_group(groups, "Approval")
    routed = service.route_message(
        group.group_id, content="Send my grade to an external service", request_key="route-sensitive",
        confidence=0.4, read_only=False, writes=True, sensitive_outbound=True,
    )
    assert routed["auto_routed"] is False
    assert routed["run"]["status"] == "waiting_approval"
    approval_id = routed["approval"]["approval_id"]
    approval, grant, run = service.resolve_approval(
        group.group_id, approval_id, approved=True,
        request_key="approve-route",
    )
    assert approval.status == "approved"
    assert grant is not None and grant.group_id == group.group_id and grant.task_id == run.task_id
    assert run.status is TaskStatus.RUNNING
    launder = service.prepare_tool(
        group.group_id, run.run_id, tool_name="external_send", idempotency_key="launder",
        risk=RiskLevel.HIGH, public_summary="Must not launder", approval_id=approval_id,
        operation_scope={"destination": "portal"}, purpose="send_result",
    )
    assert launder.disposition == "requires_new_approval"
    assert launder.status == "requires_new_approval" and launder.replay_allowed is False
    tool_approval = service.request_approval(
        group.group_id, run.run_id, kind="tool", summary="Approve exact send",
        risk=RiskLevel.HIGH, request_key="tool-approval",
        requested_capabilities=("external_send",), tool_name="external_send",
        operation_scope={"destination": "portal"}, purpose="send_result",
    )
    service.resolve_approval(
        group.group_id, tool_approval.approval_id, approved=True,
        capabilities=("external_send",), request_key="tool-approval-resolve",
    )
    invocation = service.prepare_tool(
        group.group_id, run.run_id, tool_name="external_send",
        idempotency_key="send-1", risk=RiskLevel.HIGH,
        public_summary="Send approved study result", approval_id=tool_approval.approval_id,
        operation_scope={"destination": "portal"}, purpose="send_result",
    )
    assert invocation.replay_allowed is False
    assert service.prepare_tool(
        group.group_id, run.run_id, tool_name="external_send",
        idempotency_key="send-1", risk=RiskLevel.HIGH,
        public_summary="duplicate", approval_id=tool_approval.approval_id,
        operation_scope={"destination": "portal"}, purpose="send_result",
    ).invocation_id == invocation.invocation_id


def test_restart_pauses_interrupted_runs_and_never_replays_high_risk(tmp_path: Path) -> None:
    groups = GroupRepository(tmp_path)
    group = make_group(groups, "Recovery")
    first = TaskRuntimeService(tmp_path, groups)
    run = submit_primary(first, group.group_id, "restart")
    first.schedule()
    approval = first.request_approval(
        group.group_id, run.run_id, kind="tool", summary="Dangerous operation",
        risk=RiskLevel.HIGH, request_key="approval", requested_capabilities=("danger",),
        tool_name="danger", operation_scope={"target": "workspace"}, purpose="dangerous_test",
    )
    first.resolve_approval(
        group.group_id, approval.approval_id, approved=True, capabilities=("danger",),
        request_key="approval-resolve",
    )
    first.prepare_tool(
        group.group_id, run.run_id, tool_name="danger", idempotency_key="danger-1",
        risk=RiskLevel.HIGH, public_summary="Approved once", approval_id=approval.approval_id,
        operation_scope={"target": "workspace"}, purpose="dangerous_test",
    )
    first.close()

    recovered = TaskRuntimeService(tmp_path, groups)
    try:
        snapshot = recovered.snapshot(group.group_id, run.run_id)
        assert snapshot["run"]["status"] == "paused"
        assert len(recovered.recovery_report["high_risk_blocked"]) == 1
        assert recovered.recovery_report["replayable_pending"] == []
        assert snapshot["run"]["blocking_reason"] == "Recovered after backend restart"
        blocked = recovered.store.tool_by_key(run.run_id, "danger-1")
        assert blocked is not None and blocked.disposition == "recovered_blocked"
        assert blocked.status == "recovered_blocked" and blocked.replay_allowed is False
    finally:
        recovered.close()


def test_group_isolation_rejects_forged_ids_and_cross_group_parent(runtime) -> None:
    groups, service = runtime
    first = make_group(groups, "Owner")
    second = make_group(groups, "Attacker")
    run = submit_primary(service, first.group_id, "owned")
    with pytest.raises(RuntimeNotFoundError, match="not found in group"):
        service.snapshot(second.group_id, run.run_id)
    with pytest.raises(RuntimeNotFoundError):
        service.submit(
            second.group_id, title="Cross parent", request_key="cross-parent",
            kind=RunKind.SIDE, read_only=True, parent_run_id=run.run_id,
        )
    with pytest.raises(RuntimeNotFoundError):
        service.submit(first.legacy_project_id or "legacy", title="Alias", request_key="internal-alias")


def test_archived_group_keeps_readable_snapshot_but_cannot_resume(runtime) -> None:
    groups, service = runtime
    group = make_group(groups, "Archive")
    run = submit_primary(service, group.group_id, "archive-run")
    service.schedule()
    service.suspend_group(group.group_id, reason="Group archived")
    groups.archive(group.group_id)
    assert service.snapshot(group.group_id, run.run_id)["run"]["status"] == "paused"
    with pytest.raises(RuntimeConflictError, match="archived"):
        service.resume(group.group_id, run.run_id, request_key="resume-archived")
    assert service.schedule() == ()


def test_group_deletion_removes_runtime_events_approvals_tools_and_checkpoints(runtime) -> None:
    groups, service = runtime
    group = make_group(groups, "Delete runtime content")
    run = submit_primary(service, group.group_id, "delete-run")
    service.schedule()
    service.checkpoint(group.group_id, run.run_id, label="private checkpoint", request_key="delete-ck")
    approval = service.request_approval(
        group.group_id, run.run_id, kind="tool", summary="private approval",
        risk=RiskLevel.HIGH, request_key="delete-approval",
        requested_capabilities=("private-tool",), tool_name="private-tool",
        operation_scope={"target": "workspace"}, purpose="delete_test",
    )
    service.resolve_approval(
        group.group_id, approval.approval_id, approved=True, capabilities=("private-tool",),
        request_key="delete-approve",
    )
    service.prepare_tool(
        group.group_id, run.run_id, tool_name="private-tool", idempotency_key="delete-tool",
        risk=RiskLevel.HIGH, public_summary="private operation", approval_id=approval.approval_id,
        operation_scope={"target": "workspace"}, purpose="delete_test",
    )
    service.suspend_group(group.group_id, reason="Delete")
    service.prepare_group_deletion(group.group_id, "txn_delete_runtime_content")
    preview = groups.delete_preview(group.group_id)
    groups.confirmed_delete(group.group_id, preview.confirmation_token)
    counts = service.delete_group_data(group.group_id)
    assert counts["runs"] > 0 and counts["events"] > 0
    assert counts["approvals"] > 0 and counts["tools"] > 0 and counts["checkpoints"] > 0
    assert counts["grants"] == 0  # suspend_group revokes temporary authority first
    assert service.store.get_run(run.run_id) is None
    assert service.store.pending_approvals(group.group_id) == ()
    assert all(item.group_id != group.group_id for item in service.store.incomplete_tools())


def test_failure_taxonomy_retry_hooks_never_expand_authority() -> None:
    assert TaskRuntimeService.recovery_action(
        FailureType.PARAMETER_ERROR, attempts=0, max_attempts=2,
    ) == "correct_parameters"
    assert TaskRuntimeService.recovery_action(
        FailureType.TRANSIENT_PROVIDER_FAILURE, attempts=0, max_attempts=2,
        same_privacy=True, preauthorized_fallback=True,
    ) == "provider_fallback"
    assert TaskRuntimeService.recovery_action(
        FailureType.TRANSIENT_PROVIDER_FAILURE, attempts=0, max_attempts=2,
        same_privacy=False, preauthorized_fallback=True,
    ) == "retry"
    assert TaskRuntimeService.recovery_action(
        FailureType.PERMISSION_DENIED, attempts=0, max_attempts=2,
    ) == "stop"


def test_server_boundary_accepts_legacy_alias_but_rejects_mismatched_identity(tmp_path: Path) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    first = server.groups.create_group("Legacy", legacy_project_id="project_legacy")
    second = server.groups.create_group("Other", legacy_project_id="project_other")
    try:
        response = server._task_runtime_dispatch("submit", {  # noqa: SLF001
            "project_id": "project_legacy", "title": "Compatible", "request_key": "legacy-submit",
        })
        assert response["group_id"] == first.group_id
        assert response["project_id"] == "project_legacy"
        assert response["run"]["group_id"] == first.group_id
        with pytest.raises(ValueError, match="different groups"):
            server._task_runtime_dispatch("snapshot", {  # noqa: SLF001
                "group_id": first.group_id, "project_id": second.legacy_project_id,
            })
    finally:
        server.task_runtime.close()
        if server.store is not None:
            server.store.close(timeout=5)
