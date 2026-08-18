from __future__ import annotations

"""Authoritative state machine, recovery policy, and fair local scheduler."""

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from backend.groups import GroupLifecycle, GroupNotFoundError, GroupRepository

from .contracts import (
    AgentPhase,
    AgentRun,
    ApprovalRequest,
    BudgetLimits,
    BudgetUsage,
    Checkpoint,
    FailureType,
    PermissionGrant,
    QueueClass,
    RiskLevel,
    RouteLabel,
    RunKind,
    TaskRun,
    TaskStatus,
    ToolInvocation,
    opaque_id,
    utc_now,
)
from .store import TaskRuntimeStore


class RuntimeNotFoundError(RuntimeError):
    pass


class RuntimeConflictError(RuntimeError):
    pass


_ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.QUEUED: frozenset({TaskStatus.ROUTING, TaskStatus.CANCELLED}),
    TaskStatus.ROUTING: frozenset({
        TaskStatus.RUNNING, TaskStatus.WAITING_APPROVAL, TaskStatus.PAUSED,
        TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.RUNNING: frozenset({
        TaskStatus.WAITING_APPROVAL, TaskStatus.PAUSED, TaskStatus.VERIFYING,
        TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.WAITING_APPROVAL: frozenset({
        TaskStatus.QUEUED, TaskStatus.FAILED, TaskStatus.CANCELLED,
    }),
    TaskStatus.PAUSED: frozenset({TaskStatus.QUEUED, TaskStatus.CANCELLED}),
    TaskStatus.VERIFYING: frozenset({
        TaskStatus.SUCCEEDED, TaskStatus.PARTIAL, TaskStatus.FAILED,
        TaskStatus.PAUSED, TaskStatus.CANCELLED,
    }),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.PARTIAL: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

_EXECUTING = {TaskStatus.ROUTING, TaskStatus.RUNNING, TaskStatus.VERIFYING}
_GROUP_PRIMARY_OCCUPIED = _EXECUTING | {TaskStatus.WAITING_APPROVAL, TaskStatus.PAUSED}
_NONTERMINAL = {status for status in TaskStatus if not status.terminal}
_ALLOWED_AGENT_TRANSITIONS: dict[AgentPhase, frozenset[AgentPhase]] = {
    AgentPhase.IDLE: frozenset({
        AgentPhase.PLANNING, AgentPhase.RETRIEVING, AgentPhase.EXECUTING,
        AgentPhase.WAITING, AgentPhase.FAILED,
    }),
    AgentPhase.PLANNING: frozenset({
        AgentPhase.RETRIEVING, AgentPhase.EXECUTING, AgentPhase.WAITING,
        AgentPhase.VERIFYING, AgentPhase.RESPONDING, AgentPhase.FAILED,
    }),
    AgentPhase.RETRIEVING: frozenset({
        AgentPhase.PLANNING, AgentPhase.EXECUTING, AgentPhase.WAITING, AgentPhase.FAILED,
    }),
    AgentPhase.EXECUTING: frozenset({
        AgentPhase.RETRIEVING, AgentPhase.WAITING, AgentPhase.VERIFYING,
        AgentPhase.RESPONDING, AgentPhase.FAILED,
    }),
    AgentPhase.WAITING: frozenset({
        AgentPhase.IDLE, AgentPhase.PLANNING, AgentPhase.RETRIEVING,
        AgentPhase.EXECUTING, AgentPhase.VERIFYING, AgentPhase.RESPONDING,
        AgentPhase.FAILED,
    }),
    AgentPhase.VERIFYING: frozenset({
        AgentPhase.EXECUTING, AgentPhase.WAITING, AgentPhase.RESPONDING, AgentPhase.FAILED,
    }),
    AgentPhase.RESPONDING: frozenset({AgentPhase.IDLE, AgentPhase.FAILED}),
    AgentPhase.FAILED: frozenset({AgentPhase.IDLE}),
}


class TaskRuntimeService:
    """One-process coordinator backed by a durable SQLite event journal.

    The lock makes capacity checks and dispatch decisions atomic inside the desktop
    backend.  Every state change is then committed atomically by ``TaskRuntimeStore``.
    """

    GLOBAL_PRIMARY_LIMIT = 2
    GLOBAL_SIDE_LIMIT = 2
    GLOBAL_INDEX_LIMIT = 1
    GROUP_SIDE_LIMIT = 2
    ROUTE_AUTO_CONFIDENCE = 0.85

    def __init__(self, data_root: str | Path, groups: GroupRepository) -> None:
        self.groups = groups
        self.store = TaskRuntimeStore(Path(data_root) / "runtime" / "task-runtime-v1.sqlite3")
        self._lock = threading.RLock()
        self.recovery_report = self.recover_after_restart()

    def close(self) -> None:
        self.store.close()

    def _canonical_group(self, group_id: str, *, require_active: bool = False) -> str:
        """Internal calls require the canonical opaque ID, never the legacy alias."""
        try:
            group = self.groups.get(str(group_id))
        except GroupNotFoundError as exc:
            raise RuntimeNotFoundError(str(exc)) from exc
        if require_active and group.lifecycle is not GroupLifecycle.ACTIVE:
            raise RuntimeConflictError("archived groups cannot start or resume tasks")
        return group.group_id

    def _owned_run(self, run_id: str, group_id: str) -> TaskRun:
        canonical = self._canonical_group(group_id)
        run = self.store.get_run(run_id)
        if run is None:
            raise RuntimeNotFoundError(f"run not found: {run_id}")
        if run.group_id != canonical:
            raise RuntimeNotFoundError(f"run not found in group: {run_id}")
        return run

    def submit(
        self,
        group_id: str,
        *,
        title: str,
        request_key: str,
        kind: RunKind = RunKind.PRIMARY,
        queue_class: QueueClass = QueueClass.FOREGROUND,
        route_label: RouteLabel = RouteLabel.QUEUE_NEW,
        read_only: bool = False,
        risk: RiskLevel = RiskLevel.LOW,
        parent_run_id: str | None = None,
        budget_limits: BudgetLimits | None = None,
        public_summary: str = "",
    ) -> TaskRun:
        canonical = self._canonical_group(group_id, require_active=True)
        if parent_run_id:
            self._owned_run(parent_run_id, canonical)
        run = TaskRun(
            run_id=opaque_id("run"), task_id=opaque_id("task"), group_id=canonical,
            kind=kind, title=title, queue_class=queue_class, route_label=route_label,
            read_only=read_only, risk=risk, parent_run_id=parent_run_id,
            budget_limits=budget_limits or BudgetLimits(), public_summary=public_summary,
        )
        stored, _event, _created = self.store.create_run(run, request_key=request_key)
        if stored.group_id != canonical:
            raise RuntimeConflictError("request_key is already bound to another group")
        return stored

    def transition(
        self, group_id: str, run_id: str, target: TaskStatus, *,
        idempotency_key: str, summary: str = "", blocking_reason: str = "",
        failure_type: FailureType | None = None,
    ) -> TaskRun:
        with self._lock:
            current = self._owned_run(run_id, group_id)
            if self.store.event_by_key(run_id, idempotency_key) is not None:
                return current
            if target not in _ALLOWED_TRANSITIONS[current.status]:
                raise RuntimeConflictError(
                    f"invalid task transition: {current.status.value} -> {target.value}"
                )

            def mutate(run: TaskRun) -> None:
                run.status = target
                run.public_summary = summary or run.public_summary
                run.blocking_reason = blocking_reason
                run.failure_type = failure_type
                if target is TaskStatus.RUNNING and run.started_at is None:
                    run.started_at = utc_now()
                if target.terminal:
                    run.finished_at = utc_now()

            run, _event, _created = self.store.change_run(
                run_id, event_type="status_changed",
                public_payload={
                    "from": current.status.value, "status": target.value,
                    "summary": summary, "blocking_reason": blocking_reason,
                    "failure_type": failure_type.value if failure_type else None,
                },
                idempotency_key=idempotency_key, mutate=mutate,
            )
            return run

    def update_agent(
        self, group_id: str, run_id: str, *, agent_id: str, role: str,
        phase: AgentPhase, operation_summary: str = "", blocking_reason: str = "",
        idempotency_key: str,
    ) -> TaskRun:
        current = self._owned_run(run_id, group_id)
        if current.status.terminal:
            raise RuntimeConflictError("terminal runs cannot update Agent state")
        if self.store.event_by_key(run_id, idempotency_key) is not None:
            return current
        existing = next((item for item in current.agent_runs if item.agent_id == agent_id), None)
        previous_phase = existing.phase if existing else AgentPhase.IDLE
        if phase is not previous_phase and phase not in _ALLOWED_AGENT_TRANSITIONS[previous_phase]:
            raise RuntimeConflictError(
                f"invalid Agent transition: {previous_phase.value} -> {phase.value}"
            )

        def mutate(run: TaskRun) -> None:
            stored_agent = next((item for item in run.agent_runs if item.agent_id == agent_id), None)
            version = (stored_agent.version + 1) if stored_agent else 1
            updated = AgentRun(
                agent_run_id=stored_agent.agent_run_id if stored_agent else opaque_id("arun"),
                run_id=run.run_id, agent_id=agent_id, role=role, phase=phase,
                operation_summary=operation_summary, blocking_reason=blocking_reason,
                version=version,
            )
            run.agent_runs = tuple(item for item in run.agent_runs if item.agent_id != agent_id) + (updated,)

        run, _event, _created = self.store.change_run(
            run_id, event_type="agent_phase_changed",
            public_payload={"agent_id": agent_id, "role": role, "phase": phase.value,
                            "operation_summary": operation_summary,
                            "blocking_reason": blocking_reason},
            idempotency_key=idempotency_key, mutate=mutate,
        )
        return run

    def pause(self, group_id: str, run_id: str, *, request_key: str) -> TaskRun:
        current = self._owned_run(run_id, group_id)
        if self.store.event_by_key(run_id, request_key) is not None:
            return current
        if current.status not in _EXECUTING:
            raise RuntimeConflictError(f"cannot pause {current.status.value} run")
        paused = self.transition(group_id, run_id, TaskStatus.PAUSED,
                                 idempotency_key=request_key, summary="Task paused")
        self.schedule()
        return paused

    def cancel(self, group_id: str, run_id: str, *, request_key: str) -> TaskRun:
        current = self._owned_run(run_id, group_id)
        if self.store.event_by_key(run_id, request_key) is not None:
            return current
        if current.status.terminal:
            raise RuntimeConflictError("terminal runs cannot be cancelled")
        cancelled = self.transition(group_id, run_id, TaskStatus.CANCELLED,
                                    idempotency_key=request_key, summary="Task cancelled")
        self.schedule()
        return cancelled

    def resume(self, group_id: str, run_id: str, *, request_key: str) -> TaskRun:
        self._canonical_group(group_id, require_active=True)
        current = self._owned_run(run_id, group_id)
        if self.store.event_by_key(run_id, request_key) is not None:
            return current
        if current.status is not TaskStatus.PAUSED:
            raise RuntimeConflictError("only paused runs can resume")
        run = self.transition(group_id, run_id, TaskStatus.QUEUED,
                              idempotency_key=request_key, summary="Task queued for resume")
        self.schedule()
        return self.store.get_run(run.run_id) or run

    def begin_verification(self, group_id: str, run_id: str, *, request_key: str) -> TaskRun:
        return self.transition(group_id, run_id, TaskStatus.VERIFYING,
                               idempotency_key=request_key, summary="Verifier is checking evidence")

    def complete(
        self, group_id: str, run_id: str, *, outcome: TaskStatus,
        request_key: str, summary: str, verifier_approved: bool,
    ) -> TaskRun:
        if outcome not in {TaskStatus.SUCCEEDED, TaskStatus.PARTIAL, TaskStatus.FAILED}:
            raise ValueError("completion outcome must be succeeded, partial, or failed")
        current = self._owned_run(run_id, group_id)
        if current.status is not TaskStatus.VERIFYING:
            raise RuntimeConflictError("only verifying runs can complete")
        if outcome is TaskStatus.SUCCEEDED and not verifier_approved:
            raise RuntimeConflictError("Verifier must approve a succeeded outcome")
        failure = FailureType.VERIFICATION_FAILED if outcome is TaskStatus.FAILED else None
        completed = self.transition(group_id, run_id, outcome, idempotency_key=request_key,
                                    summary=summary, failure_type=failure)
        self.schedule()
        return completed

    def consume_budget(
        self, group_id: str, run_id: str, delta: BudgetUsage, *, request_key: str,
    ) -> TaskRun:
        current = self._owned_run(run_id, group_id)
        if current.status.terminal:
            raise RuntimeConflictError("terminal runs cannot consume budget")
        next_usage = BudgetUsage(
            wall_clock_seconds=current.budget_usage.wall_clock_seconds + delta.wall_clock_seconds,
            model_turns=current.budget_usage.model_turns + delta.model_turns,
            tool_calls=current.budget_usage.tool_calls + delta.tool_calls,
            tokens=current.budget_usage.tokens + delta.tokens,
            cost_usd=current.budget_usage.cost_usd + delta.cost_usd,
        )
        exceeded = next_usage.exceeded(current.budget_limits)

        def mutate(run: TaskRun) -> None:
            run.budget_usage = next_usage
            if exceeded:
                # Progress copy is not a verified partial result.  Budget exhaustion
                # therefore fails deterministically; only Verifier may emit ``partial``.
                run.status = TaskStatus.FAILED
                run.failure_type = FailureType.BUDGET_EXHAUSTED
                run.blocking_reason = f"budget exhausted: {exceeded}"
                run.finished_at = utc_now()

        run, _event, _created = self.store.change_run(
            run_id, event_type="budget_exhausted" if exceeded else "budget_updated",
            public_payload={"usage": next_usage.__dict__, "limits": current.budget_limits.__dict__,
                            "exhausted_dimension": exceeded,
                            "status": TaskStatus.FAILED.value
                            if exceeded else current.status.value},
            idempotency_key=request_key, mutate=mutate,
        )
        if exceeded:
            self.schedule()
        return run

    def request_approval(
        self, group_id: str, run_id: str, *, kind: str, summary: str,
        risk: RiskLevel, request_key: str,
    ) -> ApprovalRequest:
        current = self._owned_run(run_id, group_id)
        existing = self.store.approval_by_request_key(request_key)
        if existing is not None:
            if existing.run_id != run_id or existing.group_id != current.group_id:
                raise RuntimeConflictError("approval request_key is bound to another task")
            return existing
        if current.status not in {TaskStatus.ROUTING, TaskStatus.RUNNING}:
            raise RuntimeConflictError("approval can only gate routing or running work")
        approval = ApprovalRequest(
            approval_id=opaque_id("apr"), run_id=run_id, group_id=current.group_id,
            kind=kind, summary=summary, risk=risk,
        )

        def mutate(run: TaskRun) -> None:
            run.status = TaskStatus.WAITING_APPROVAL
            run.public_summary = summary or run.public_summary
            run.blocking_reason = "User approval required"

        def persist_approval(conn: Any, _run: TaskRun, _event: Any) -> None:
            self.store.upsert_approval_in_transaction(
                conn, approval, request_key=request_key,
            )

        self.store.change_run(
            run_id, event_type="approval_requested",
            public_payload={
                "status": TaskStatus.WAITING_APPROVAL.value,
                "approval_id": approval.approval_id, "kind": kind,
                "risk": risk.value, "summary": summary,
                "blocking_reason": "User approval required",
            },
            idempotency_key=request_key, mutate=mutate,
            transaction_hook=persist_approval,
        )
        return approval

    def resolve_approval(
        self, group_id: str, approval_id: str, *, approved: bool,
        capabilities: tuple[str, ...] = (), request_key: str,
    ) -> tuple[ApprovalRequest, PermissionGrant | None, TaskRun]:
        approval = self.store.get_approval(approval_id)
        canonical = self._canonical_group(group_id, require_active=True)
        if approval is None or approval.group_id != canonical:
            raise RuntimeNotFoundError(f"approval not found in group: {approval_id}")
        if approval.status != "pending":
            run = self._owned_run(approval.run_id, canonical)
            return approval, None, run
        resolved = replace(
            approval, status="approved" if approved else "rejected", resolved_at=utc_now(),
        )
        grant: PermissionGrant | None = None
        current = self._owned_run(approval.run_id, canonical)
        if current.status is not TaskStatus.WAITING_APPROVAL:
            raise RuntimeConflictError("approval owner is not waiting for approval")
        if approved:
            grant = PermissionGrant(
                grant_id=opaque_id("grant"), approval_id=approval.approval_id,
                run_id=current.run_id, group_id=canonical, task_id=current.task_id,
                capabilities=tuple(capabilities),
            )

            def mutate(run: TaskRun) -> None:
                run.status = TaskStatus.QUEUED
                run.public_summary = "Approval granted; task returned to queue"
                run.blocking_reason = ""

            def persist_approval_and_grant(conn: Any, _run: TaskRun, _event: Any) -> None:
                self.store.upsert_approval_in_transaction(conn, resolved)
                self.store.insert_grant_in_transaction(conn, grant)

            run, _event, _created = self.store.change_run(
                current.run_id, event_type="approval_resolved",
                public_payload={
                    "status": TaskStatus.QUEUED.value, "approval_id": approval_id,
                    "resolution": "approved",
                },
                idempotency_key=request_key, mutate=mutate,
                transaction_hook=persist_approval_and_grant,
            )
            self.schedule()
            run = self.store.get_run(run.run_id) or run
        else:

            def mutate(run: TaskRun) -> None:
                run.status = TaskStatus.FAILED
                run.public_summary = "Approval rejected"
                run.failure_type = FailureType.PERMISSION_DENIED
                run.finished_at = utc_now()

            def persist_rejection(conn: Any, _run: TaskRun, _event: Any) -> None:
                self.store.upsert_approval_in_transaction(conn, resolved)

            run, _event, _created = self.store.change_run(
                current.run_id, event_type="approval_resolved",
                public_payload={
                    "status": TaskStatus.FAILED.value, "approval_id": approval_id,
                    "resolution": "rejected",
                    "failure_type": FailureType.PERMISSION_DENIED.value,
                },
                idempotency_key=request_key, mutate=mutate,
                transaction_hook=persist_rejection,
            )
            self.schedule()
        return resolved, grant, run

    def prepare_tool(
        self, group_id: str, run_id: str, *, tool_name: str,
        idempotency_key: str, risk: RiskLevel, public_summary: str,
        approval_id: str | None = None,
    ) -> ToolInvocation:
        run = self._owned_run(run_id, group_id)
        if run.status is not TaskStatus.RUNNING:
            raise RuntimeConflictError("tools require a running task")
        if risk is RiskLevel.HIGH:
            approval = self.store.get_approval(str(approval_id or ""))
            grant = self.store.grant_for_approval(str(approval_id or ""))
            if (approval is None or approval.status != "approved"
                    or approval.run_id != run.run_id or approval.group_id != run.group_id
                    or grant is None or grant.run_id != run.run_id or grant.group_id != run.group_id
                    or tool_name not in grant.capabilities):
                raise RuntimeConflictError("high-risk tools require an explicit task-scoped approval")
        invocation = ToolInvocation(
            invocation_id=opaque_id("tool"), run_id=run.run_id, group_id=run.group_id,
            tool_name=tool_name, idempotency_key=idempotency_key, risk=risk,
            replay_allowed=risk is not RiskLevel.HIGH, public_summary=public_summary,
        )
        stored, _created = self.store.save_tool(invocation)
        return stored

    def checkpoint(self, group_id: str, run_id: str, *, label: str, request_key: str) -> Checkpoint:
        run = self._owned_run(run_id, group_id)
        existing = self.store.checkpoint_by_key(run_id, request_key)
        if existing is not None:
            return existing
        if run.status.terminal:
            raise RuntimeConflictError("terminal runs cannot create checkpoints")
        checkpoint = Checkpoint(
            checkpoint_id=opaque_id("ckp"), run_id=run_id, group_id=run.group_id,
            seq=run.last_seq + 1, version=run.version + 1, label=label,
        )

        def persist_checkpoint(conn: Any, updated: TaskRun, _event: Any) -> None:
            self.store.insert_checkpoint_in_transaction(
                conn, checkpoint, updated, idempotency_key=request_key,
            )

        self.store.change_run(
            run_id, event_type="checkpoint_created", public_payload={"label": label},
            idempotency_key=request_key,
            transaction_hook=persist_checkpoint,
        )
        return checkpoint

    def snapshot(self, group_id: str, run_id: str) -> dict[str, Any]:
        run = self._owned_run(run_id, group_id)
        return {"schema_version": 1, "run": run.to_public_dict(),
                "last_seq": run.last_seq, "version": run.version}

    def replay(
        self, group_id: str, run_id: str, *, after_seq: int, after_version: int | None = None,
    ) -> dict[str, Any]:
        run = self._owned_run(run_id, group_id)
        after_seq = int(after_seq)
        version_ahead = after_version is not None and int(after_version) > run.version
        cursor_ahead = after_seq > run.last_seq
        events = self.store.events_after(run_id, after_seq) if not (version_ahead or cursor_ahead) else ()
        expected = after_seq + 1
        gap = bool(version_ahead or cursor_ahead or any(event.seq != expected + index for index, event in enumerate(events)))
        return {
            "schema_version": 1, "run_id": run_id, "group_id": run.group_id,
            "after_seq": after_seq, "last_seq": run.last_seq, "version": run.version,
            "gap": gap, "events": [event.to_public_dict() for event in events],
            "snapshot": self.snapshot(group_id, run_id) if gap else None,
        }

    def group_snapshot(self, group_id: str) -> dict[str, Any]:
        canonical = self._canonical_group(group_id)
        runs = self.store.list_runs(group_id=canonical)
        return {
            "schema_version": 1, "group_id": canonical,
            "runs": [run.to_public_dict() for run in runs],
            "pending_approvals": [item.to_dict() for item in self.store.pending_approvals(canonical)],
            "summary": self.group_summary(canonical),
        }

    def group_summary(self, group_id: str) -> dict[str, Any]:
        canonical = self._canonical_group(group_id)
        runs = self.store.list_runs(group_id=canonical)
        primary = next((run for run in reversed(runs) if run.kind is RunKind.PRIMARY and run.status in _NONTERMINAL), None)
        usage = BudgetUsage(
            wall_clock_seconds=sum(run.budget_usage.wall_clock_seconds for run in runs),
            model_turns=sum(run.budget_usage.model_turns for run in runs),
            tool_calls=sum(run.budget_usage.tool_calls for run in runs),
            tokens=sum(run.budget_usage.tokens for run in runs),
            cost_usd=sum(run.budget_usage.cost_usd for run in runs),
        )
        return {
            "active_primary_run_id": primary.run_id if primary else None,
            "primary_task_status": primary.status.value if primary else None,
            "queued_task_count": sum(run.status is TaskStatus.QUEUED for run in runs),
            "active_side_task_count": sum(
                run.kind is RunKind.SIDE and run.queue_class is QueueClass.FOREGROUND
                and run.status in _EXECUTING for run in runs
            ),
            "pending_approval_count": len(self.store.pending_approvals(canonical)),
            "budget_usage": usage.__dict__,
            "indexing_state": "running" if any(
                run.queue_class is QueueClass.INDEX and run.status in _EXECUTING for run in runs
            ) else ("queued" if any(
                run.queue_class is QueueClass.INDEX and run.status is TaskStatus.QUEUED for run in runs
            ) else "idle"),
        }

    def suspend_group(self, group_id: str, *, reason: str) -> tuple[str, ...]:
        """Fence all work before a Group archive/delete lifecycle transition."""
        canonical = self._canonical_group(group_id, require_active=True)
        changed: list[str] = []
        with self._lock:
            for run in self.store.list_runs(group_id=canonical, statuses=_NONTERMINAL):
                if run.status is TaskStatus.QUEUED:
                    target = TaskStatus.CANCELLED
                elif run.status in _EXECUTING:
                    target = TaskStatus.PAUSED
                else:
                    continue
                self.transition(
                    canonical, run.run_id, target,
                    idempotency_key=f"group-suspend:{run.run_id}:{run.version}",
                    summary=reason, blocking_reason=reason,
                )
                changed.append(run.run_id)
            self.store.revoke_group_grants(canonical)
        self.schedule()
        return tuple(changed)

    def delete_group_data(self, group_id: str) -> dict[str, int]:
        """Called only after the Group repository committed its deletion receipt."""
        with self._lock:
            return self.store.delete_group(str(group_id))

    def _round_robin(self, candidates: list[TaskRun]) -> list[TaskRun]:
        if not candidates:
            return []
        groups = sorted({run.group_id for run in candidates})
        cursor = self.store.scheduler_cursor()
        start = (groups.index(cursor) + 1) % len(groups) if cursor in groups else 0
        ordered_groups = groups[start:] + groups[:start]
        by_group: dict[str, list[TaskRun]] = {group: [] for group in groups}
        for run in candidates:
            by_group[run.group_id].append(run)
        result: list[TaskRun] = []
        while any(by_group.values()):
            for group in ordered_groups:
                if by_group[group]:
                    result.append(by_group[group].pop(0))
        return result

    def _group_is_active(self, group_id: str) -> bool:
        try:
            return self.groups.get(group_id).lifecycle is GroupLifecycle.ACTIVE
        except GroupNotFoundError:
            return False

    def schedule(self) -> tuple[TaskRun, ...]:
        """Dispatch queued work within all local limits using durable group rotation."""
        with self._lock:
            all_runs = list(self.store.list_runs())
            executing = [run for run in all_runs if run.status in _EXECUTING]
            primary_capacity = self.GLOBAL_PRIMARY_LIMIT - sum(
                run.kind is RunKind.PRIMARY for run in executing
            )
            side_capacity = self.GLOBAL_SIDE_LIMIT - sum(
                run.kind is RunKind.SIDE and run.queue_class is QueueClass.FOREGROUND
                for run in executing
            )
            index_capacity = self.GLOBAL_INDEX_LIMIT - sum(
                run.queue_class is QueueClass.INDEX for run in executing
            )
            primary_occupied = {
                run.group_id for run in all_runs
                if run.kind is RunKind.PRIMARY and run.status in _GROUP_PRIMARY_OCCUPIED
            }
            side_counts: dict[str, int] = {}
            for run in executing:
                if run.kind is RunKind.SIDE and run.queue_class is QueueClass.FOREGROUND:
                    side_counts[run.group_id] = side_counts.get(run.group_id, 0) + 1

            queued = [
                run for run in all_runs
                if run.status is TaskStatus.QUEUED and self._group_is_active(run.group_id)
            ]
            primary_candidates = self._round_robin([
                run for run in queued if run.kind is RunKind.PRIMARY
                and run.queue_class is QueueClass.FOREGROUND and run.group_id not in primary_occupied
            ])
            primary: list[TaskRun] = []
            selected_primary_groups: set[str] = set()
            for run in primary_candidates:
                if len(primary) >= max(0, primary_capacity):
                    break
                if run.group_id not in selected_primary_groups:
                    primary.append(run)
                    selected_primary_groups.add(run.group_id)

            side_candidates = self._round_robin([
                run for run in queued if run.kind is RunKind.SIDE
                and run.queue_class is QueueClass.FOREGROUND
                and side_counts.get(run.group_id, 0) < self.GROUP_SIDE_LIMIT
            ])
            side: list[TaskRun] = []
            selected_side_counts = dict(side_counts)
            for run in side_candidates:
                if len(side) >= max(0, side_capacity):
                    break
                if selected_side_counts.get(run.group_id, 0) >= self.GROUP_SIDE_LIMIT:
                    continue
                side.append(run)
                selected_side_counts[run.group_id] = selected_side_counts.get(run.group_id, 0) + 1
            # Indexing is deliberately considered only after foreground queues and has
            # independent capacity, so it cannot consume the two interactive side slots.
            index = self._round_robin([
                run for run in queued if run.queue_class is QueueClass.INDEX
            ])[:max(0, index_capacity)]
            started: list[TaskRun] = []
            for run in primary + side + index:
                routed = self.transition(
                    run.group_id, run.run_id, TaskStatus.ROUTING,
                    idempotency_key=f"dispatch:{run.run_id}:{run.version}",
                    summary="Coordinator routed task",
                )
                running = self.transition(
                    run.group_id, run.run_id, TaskStatus.RUNNING,
                    idempotency_key=f"start:{run.run_id}:{routed.version}",
                    summary="Task running",
                )
                self.store.set_scheduler_cursor(run.group_id)
                started.append(running)
            return tuple(started)

    def route_message(
        self, group_id: str, *, content: str, request_key: str,
        confidence: float, read_only: bool, writes: bool = False,
        sensitive_outbound: bool = False, explicit_new_task: bool = False,
    ) -> dict[str, Any]:
        canonical = self._canonical_group(group_id, require_active=True)
        primary = next((
            run for run in reversed(self.store.list_runs(group_id=canonical))
            if run.kind is RunKind.PRIMARY and run.status in _NONTERMINAL
        ), None)
        if explicit_new_task or primary is None:
            label = RouteLabel.QUEUE_NEW
        elif read_only and len(content.strip()) <= 280:
            label = RouteLabel.SIDE_ANSWER
        else:
            label = RouteLabel.SUPPLEMENT_CURRENT
        requires_approval = confidence < self.ROUTE_AUTO_CONFIDENCE or writes or sensitive_outbound

        if label is RouteLabel.SUPPLEMENT_CURRENT and primary is not None and not requires_approval:
            updated, event, _created = self.store.change_run(
                primary.run_id, event_type="message_routed",
                public_payload={"route_label": label.value, "content_summary": content[:160]},
                idempotency_key=request_key,
                sensitive_payload={"content": content}, sensitivity="private",
            )
            return {"route_label": label.value, "auto_routed": True,
                    "run": updated.to_public_dict(), "event": event.to_public_dict()}

        kind = RunKind.SIDE if label is RouteLabel.SIDE_ANSWER and not requires_approval else RunKind.PRIMARY
        run = self.submit(
            canonical, title=content[:120] or "New task", request_key=request_key,
            kind=kind, route_label=label, read_only=(kind is RunKind.SIDE),
            risk=RiskLevel.LOW if read_only and not writes else RiskLevel.MEDIUM,
            parent_run_id=primary.run_id if kind is RunKind.SIDE and primary else None,
            public_summary=content[:160],
        )
        approval: ApprovalRequest | None = None
        if requires_approval:
            routed = self.transition(canonical, run.run_id, TaskStatus.ROUTING,
                                     idempotency_key=f"route-gate:{request_key}",
                                     summary="Routing needs confirmation")
            approval = self.request_approval(
                canonical, routed.run_id, kind="message_route",
                summary="Confirm task routing or sensitive/write operation",
                risk=RiskLevel.HIGH if sensitive_outbound else RiskLevel.MEDIUM,
                request_key=f"approval:{request_key}",
            )
            run = self.store.get_run(run.run_id) or run
        else:
            self.schedule()
            run = self.store.get_run(run.run_id) or run
        return {"route_label": label.value, "auto_routed": not requires_approval,
                "run": run.to_public_dict(),
                "approval": approval.to_dict() if approval else None}

    @staticmethod
    def recovery_action(
        failure: FailureType, *, attempts: int, max_attempts: int,
        same_privacy: bool = True, preauthorized_fallback: bool = False,
    ) -> str:
        if attempts >= max_attempts or not failure.reversible:
            return "stop"
        if failure is FailureType.PARAMETER_ERROR:
            return "correct_parameters"
        if (failure is FailureType.TRANSIENT_PROVIDER_FAILURE and same_privacy
                and preauthorized_fallback):
            return "provider_fallback"
        return "retry"

    def recover_after_restart(self) -> dict[str, Any]:
        """Fence interrupted work; never execute or replay a side effect here."""
        report: dict[str, Any] = {"paused_runs": [], "high_risk_blocked": [], "replayable_pending": []}
        with self._lock:
            for run in self.store.list_runs(statuses=_EXECUTING):
                recovered, _event, _created = self.store.change_run(
                    run.run_id, event_type="restart_recovery",
                    public_payload={"status": TaskStatus.PAUSED.value,
                                    "blocking_reason": "Recovered after backend restart"},
                    idempotency_key=f"restart-recovery:{run.run_id}:{run.version}",
                    mutate=lambda item: (
                        setattr(item, "status", TaskStatus.PAUSED),
                        setattr(item, "blocking_reason", "Recovered after backend restart"),
                    ),
                )
                report["paused_runs"].append(recovered.run_id)
            for invocation in self.store.incomplete_tools():
                if invocation.risk is RiskLevel.HIGH or not invocation.replay_allowed:
                    report["high_risk_blocked"].append(invocation.invocation_id)
                else:
                    # A later Tool Gateway may offer an explicit user retry.  Recovery
                    # only reports it; it does not call the tool.
                    report["replayable_pending"].append(invocation.invocation_id)
        return report


__all__ = ["RuntimeConflictError", "RuntimeNotFoundError", "TaskRuntimeService"]
