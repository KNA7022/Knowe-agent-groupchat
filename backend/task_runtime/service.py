from __future__ import annotations

"""Authoritative state machine, recovery policy, and fair local scheduler."""

import contextlib
import hashlib
import json
import math
import re
import sqlite3
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
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
    EvidenceRef,
    FailureType,
    PermissionGrant,
    QueueClass,
    RiskLevel,
    RouteLabel,
    RunKind,
    TaskRun,
    TaskStatus,
    ToolInvocation,
    VerifierDecision,
    classify_public_text,
    opaque_id,
    utc_now,
)
from .store import RuntimeStoreError, TaskRuntimeStore


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
        AgentPhase.WAITING, AgentPhase.VERIFYING, AgentPhase.FAILED,
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
    SCHEDULER_LEASE_SECONDS = 30.0
    APPROVAL_DEFAULT_TTL_SECONDS = 600.0
    APPROVAL_MAX_TTL_SECONDS = 86_400.0
    DEFERRED_QUARANTINE_LIMIT = 64
    DEFERRED_QUARANTINE_RETRY_BASE_SECONDS = 0.1
    DEFERRED_QUARANTINE_RETRY_MAX_SECONDS = 5.0
    _AWARE_ISO8601 = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
    )

    def __init__(self, data_root: str | Path, groups: GroupRepository) -> None:
        self.groups = groups
        self.store = TaskRuntimeStore(Path(data_root) / "runtime" / "task-runtime-v1.sqlite3")
        self._lock = threading.RLock()
        self._scheduler_owner = opaque_id("lease")
        self._scheduler_retry: threading.Timer | None = None
        self._approval_expiry_timer: threading.Timer | None = None
        self._quarantine_retry_timer: threading.Timer | None = None
        self._deferred_quarantines: dict[str, str] = {}
        self._deferred_quarantine_overflow = False
        self._quarantine_retry_attempt = 0
        self._quarantine_retry_enabled = False
        self._quarantine_retry_running = False
        self._quarantine_retry_thread_id: int | None = None
        self._closed = False
        self.recovery_report = {"deletions": self.reconcile_group_deletions()}
        self.recovery_report.update(self.recover_after_restart())
        self.recovery_report["expired_approvals"] = list(
            self.expire_pending_approvals(reschedule=False)
        )
        self.recovery_report["scheduled_runs"] = [run.run_id for run in self.schedule()]
        self._arm_approval_expiry_watcher()
        self._quarantine_retry_enabled = True
        self._arm_deferred_quarantine_retry()

    def add_event_listener(self, listener: Any) -> None:
        self.store.add_event_listener(listener)

    def remove_event_listener(self, listener: Any) -> None:
        self.store.remove_event_listener(listener)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            timer = self._scheduler_retry
            self._scheduler_retry = None
            if timer is not None:
                timer.cancel()
            approval_timer = self._approval_expiry_timer
            self._approval_expiry_timer = None
            if approval_timer is not None:
                approval_timer.cancel()
            quarantine_timer = self._quarantine_retry_timer
            self._quarantine_retry_timer = None
            if quarantine_timer is not None:
                quarantine_timer.cancel()
            self.store.release_scheduler_lease(self._scheduler_owner)
            self.store.close()

    def _assert_group_mutable(self, group_id: str) -> None:
        transaction = self.groups.delete_transaction(group_id)
        if transaction is not None or self.store.group_is_deleting(group_id):
            raise RuntimeConflictError("Task Runtime group is fenced for deletion")

    def _assert_not_deferred_quarantine(self, group_id: str) -> None:
        with self._lock:
            if (
                group_id in self._deferred_quarantines
                or self._deferred_quarantine_overflow
                or (
                    self._quarantine_retry_running
                    and self._quarantine_retry_thread_id != threading.get_ident()
                )
            ):
                raise RuntimeConflictError(
                    "Task Runtime deletion intent is quarantined pending audit",
                )

    def _canonical_group(self, group_id: str, *, require_active: bool = False) -> str:
        """Internal calls require the canonical opaque ID, never the legacy alias."""
        try:
            group = self.groups.get(str(group_id))
        except GroupNotFoundError as exc:
            raise RuntimeNotFoundError(str(exc)) from exc
        if require_active and group.lifecycle is not GroupLifecycle.ACTIVE:
            raise RuntimeConflictError("archived groups cannot start or resume tasks")
        return group.group_id

    def _owned_run(self, run_id: str, group_id: str, *, allow_deleting: bool = False) -> TaskRun:
        canonical = self._canonical_group(group_id)
        if not allow_deleting:
            self._assert_group_mutable(canonical)
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
        data_classification: tuple[str, ...] = (),
    ) -> TaskRun:
        canonical = self._canonical_group(group_id, require_active=True)
        self._assert_group_mutable(canonical)
        if parent_run_id:
            self._owned_run(parent_run_id, canonical)
        run = TaskRun(
            run_id=opaque_id("run"), task_id=opaque_id("task"), group_id=canonical,
            kind=kind, title=title, queue_class=queue_class, route_label=route_label,
            read_only=read_only, risk=risk, parent_run_id=parent_run_id,
            budget_limits=budget_limits or BudgetLimits(), public_summary=public_summary,
            data_classification=tuple(dict.fromkeys(data_classification)),
        )
        stored, _event, _created = self.store.create_run(run, request_key=request_key)
        if stored.group_id != canonical:
            raise RuntimeConflictError("request_key is already bound to another group")
        return stored

    def transition(
        self, group_id: str, run_id: str, target: TaskStatus, *,
        idempotency_key: str, summary: str = "", blocking_reason: str = "",
        failure_type: FailureType | None = None,
        _transaction_hook: Any | None = None,
        _allow_deleting: bool = False,
    ) -> TaskRun:
        with self._lock:
            current = self._owned_run(
                run_id, group_id, allow_deleting=_allow_deleting,
            )
            if self.store.event_by_key(run_id, idempotency_key) is not None:
                return current

            def mutate(run: TaskRun) -> None:
                if run.group_id != current.group_id:
                    raise RuntimeNotFoundError(f"run not found in group: {run_id}")
                if target not in _ALLOWED_TRANSITIONS[run.status]:
                    raise RuntimeConflictError(
                        f"invalid task transition: {run.status.value} -> {target.value}"
                    )
                previous = run.status
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
                public_payload=lambda updated: {
                    "from": current.status.value, "status": target.value,
                    "summary": summary, "blocking_reason": blocking_reason,
                    "failure_type": failure_type.value if failure_type else None,
                },
                idempotency_key=idempotency_key, mutate=mutate,
                transaction_hook=_transaction_hook,
            )
            return run

    def update_agent(
        self, group_id: str, run_id: str, *, agent_id: str, role: str,
        phase: AgentPhase, operation_summary: str = "", blocking_reason: str = "",
        idempotency_key: str,
    ) -> TaskRun:
        current = self._owned_run(run_id, group_id)
        if self.store.event_by_key(run_id, idempotency_key) is not None:
            return current

        def mutate(run: TaskRun) -> None:
            if run.group_id != current.group_id or run.status.terminal:
                raise RuntimeConflictError("terminal or cross-group run cannot update Agent state")
            stored_agent = next((item for item in run.agent_runs if item.agent_id == agent_id), None)
            previous_phase = stored_agent.phase if stored_agent else AgentPhase.IDLE
            if phase is not previous_phase and phase not in _ALLOWED_AGENT_TRANSITIONS[previous_phase]:
                raise RuntimeConflictError(
                    f"invalid Agent transition: {previous_phase.value} -> {phase.value}"
                )
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
        if current.status is TaskStatus.CANCELLED:
            return current
        if current.status.terminal:
            raise RuntimeConflictError("terminal runs cannot be cancelled")

        def terminalize_owned_state(conn: Any, _run: TaskRun, _event: Any) -> None:
            now = utc_now()
            self.store.reject_pending_approvals_in_transaction(
                conn, run_id=run_id, resolution="run_cancelled", resolved_at=now,
            )
            self.store.terminalize_executor_continuations_in_transaction(
                conn, run_id=run_id, status="cancelled",
            )

        cancelled = self.transition(group_id, run_id, TaskStatus.CANCELLED,
                                    idempotency_key=request_key, summary="Task cancelled",
                                    _transaction_hook=terminalize_owned_state)
        self.schedule()
        return cancelled

    def resume(self, group_id: str, run_id: str, *, request_key: str) -> TaskRun:
        self._canonical_group(group_id, require_active=True)
        current = self._owned_run(run_id, group_id)
        if self.store.event_by_key(run_id, request_key) is not None:
            return current
        if current.status is not TaskStatus.PAUSED:
            raise RuntimeConflictError("only paused runs can resume")
        if any(
            item.get("status") == "recovered_blocked"
            for item in self.store.executor_continuations_for_run(run_id)
        ):
            raise RuntimeConflictError(
                "recovered high-risk execution requires explicit reapproval"
            )
        run = self.transition(group_id, run_id, TaskStatus.QUEUED,
                              idempotency_key=request_key, summary="Task queued for resume")
        self.schedule()
        return self.store.get_run(run.run_id) or run

    def begin_verification(self, group_id: str, run_id: str, *, request_key: str) -> TaskRun:
        return self.transition(group_id, run_id, TaskStatus.VERIFYING,
                               idempotency_key=request_key, summary="Verifier is checking evidence")

    def record_verifier_decision(
        self, group_id: str, run_id: str, *, verifier_agent_id: str,
        outcome: TaskStatus, approved: bool, evidence: tuple[EvidenceRef, ...],
        reason: str, request_key: str,
    ) -> VerifierDecision:
        if outcome not in {TaskStatus.SUCCEEDED, TaskStatus.PARTIAL, TaskStatus.FAILED}:
            raise ValueError("invalid Verifier outcome")
        if approved and outcome in {TaskStatus.SUCCEEDED, TaskStatus.PARTIAL} and not evidence:
            raise RuntimeConflictError("successful Verifier decisions require bound evidence")
        group = self.groups.get(self._canonical_group(group_id))
        verifier = next((agent for agent in group.roster if agent.role == "verifier"), None)
        if verifier is None or verifier.agent_id != verifier_agent_id:
            raise RuntimeConflictError("decision actor is not the mandatory Group Verifier")
        current = self._owned_run(run_id, group_id)
        agent_run = next((item for item in current.agent_runs if item.agent_id == verifier_agent_id), None)
        if (current.status is not TaskStatus.VERIFYING or agent_run is None
                or agent_run.role != "verifier" or agent_run.phase is not AgentPhase.VERIFYING):
            raise RuntimeConflictError("Verifier AgentRun must be in verifying phase")
        decision = VerifierDecision(
            decision_id=opaque_id("vfd"), run_id=run_id, task_id=current.task_id,
            group_id=current.group_id, verifier_agent_id=verifier_agent_id,
            run_version=current.version + 1, outcome=outcome, approved=approved,
            evidence=evidence, reason=reason,
        )

        def persist(conn: Any, updated: TaskRun, _event: Any) -> None:
            if updated.version != decision.run_version:
                raise RuntimeConflictError("Verifier decision version drift")
            self.store.save_verifier_decision_in_transaction(conn, decision)

        self.store.change_run(
            run_id, event_type="verifier_decision",
            public_payload={
                "status": TaskStatus.VERIFYING.value, "decision_id": decision.decision_id,
                "outcome": outcome.value, "approved": approved, "evidence_count": len(evidence),
            }, idempotency_key=request_key, transaction_hook=persist,
        )
        winner = self.store.verifier_decision_by_request_key(run_id, request_key)
        if winner is None:
            raise RuntimeConflictError("Verifier decision idempotency winner is unavailable")
        return winner

    def complete(
        self, group_id: str, run_id: str, *, outcome: TaskStatus,
        request_key: str, summary: str,
        _trusted_failure_type: FailureType | None = None,
    ) -> TaskRun:
        if outcome not in {TaskStatus.SUCCEEDED, TaskStatus.PARTIAL, TaskStatus.FAILED}:
            raise ValueError("completion outcome must be succeeded, partial, or failed")
        current = self._owned_run(run_id, group_id)
        failure = (
            _trusted_failure_type or FailureType.VERIFICATION_FAILED
            if outcome is TaskStatus.FAILED else None
        )

        def mutate(run: TaskRun) -> None:
            if run.group_id != current.group_id or run.status is not TaskStatus.VERIFYING:
                raise RuntimeConflictError("only verifying runs may complete")
            run.status = outcome
            run.public_summary = summary or run.public_summary
            run.blocking_reason = ""
            run.failure_type = failure
            run.finished_at = utc_now()

        def verify_decision(conn: Any, updated: TaskRun, _event: Any) -> None:
            decision = self.store.verifier_decision_for_version_in_transaction(
                conn, run_id, updated.version - 1,
            )
            expected_approval = failure is not FailureType.VERIFICATION_FAILED
            if (decision is None or decision.approved is not expected_approval
                    or decision.outcome is not outcome or decision.task_id != updated.task_id
                    or decision.group_id != updated.group_id):
                raise RuntimeConflictError("a current task-bound Verifier decision is required")

        completed, _event, _created = self.store.change_run(
            run_id, event_type="status_changed",
            public_payload={"from": TaskStatus.VERIFYING.value, "status": outcome.value,
                            "summary": summary, "blocking_reason": "",
                            "failure_type": failure.value if failure else None},
            idempotency_key=request_key, mutate=mutate, transaction_hook=verify_decision,
        )
        self.schedule()
        return completed

    def finalize_trusted_execution(
        self, group_id: str, run_id: str, *, execution_key: str,
        outcome: TaskStatus, evidence: tuple[EvidenceRef, ...] = (),
        summary: str = "Task execution completed",
        failure_type: FailureType | None = None,
        verifier_approved: bool = True,
    ) -> TaskRun:
        """Bridge a trusted executor lifecycle into mandatory Verifier completion.

        This is not exposed as a client decision endpoint.  Every step has a stable
        executor-derived idempotency key, and the persisted Group Verifier remains the
        sole producer of the version/evidence-bound completion decision.
        """
        if outcome is TaskStatus.CANCELLED:
            current = self._owned_run(run_id, group_id)
            if current.status.terminal:
                return current
            return self.cancel(
                group_id, run_id, request_key=f"executor:{execution_key}:cancelled",
            )
        if outcome not in {TaskStatus.SUCCEEDED, TaskStatus.PARTIAL, TaskStatus.FAILED}:
            raise ValueError("trusted execution outcome must be terminal and verifiable")
        if outcome is TaskStatus.FAILED and failure_type is None:
            failure_type = FailureType.EXECUTOR_FAILURE
        if outcome is not TaskStatus.FAILED and failure_type is not None:
            raise ValueError("failure_type is only valid for failed executor outcomes")
        if not verifier_approved and outcome is not TaskStatus.FAILED:
            raise ValueError("Verifier rejection must produce a failed outcome")
        current = self._owned_run(run_id, group_id)
        if current.status.terminal:
            return current
        if current.status is TaskStatus.RUNNING:
            current = self.begin_verification(
                group_id, run_id, request_key=f"executor:{execution_key}:begin-verification",
            )
        if current.status is not TaskStatus.VERIFYING:
            raise RuntimeConflictError(
                f"trusted executor cannot finalize a {current.status.value} run"
            )
        group = self.groups.get(current.group_id)
        verifier = next((agent for agent in group.roster if agent.role == "verifier"), None)
        if verifier is None:
            raise RuntimeConflictError("mandatory Group Verifier is unavailable")
        current = self.update_agent(
            group_id, run_id, agent_id=verifier.agent_id, role="verifier",
            phase=AgentPhase.VERIFYING, operation_summary="Verifying executor evidence",
            idempotency_key=f"executor:{execution_key}:verifier-agent",
        )
        self.record_verifier_decision(
            group_id, run_id, verifier_agent_id=verifier.agent_id,
            outcome=outcome, approved=verifier_approved, evidence=evidence,
            reason=("Trusted executor lifecycle verified" if verifier_approved
                    else "Verifier rejected executor evidence"),
            request_key=f"executor:{execution_key}:verifier-decision",
        )
        return self.complete(
            group_id, run_id, outcome=outcome,
            request_key=f"executor:{execution_key}:complete", summary=summary,
            _trusted_failure_type=(
                FailureType.VERIFICATION_FAILED if not verifier_approved else failure_type
            ),
        )

    def consume_budget(
        self, group_id: str, run_id: str, delta: BudgetUsage, *, request_key: str,
    ) -> TaskRun:
        current = self._owned_run(run_id, group_id)
        result: dict[str, Any] = {}

        def mutate(run: TaskRun) -> None:
            if run.group_id != current.group_id or run.status.terminal:
                raise RuntimeConflictError("terminal or cross-group run cannot consume budget")
            next_usage = BudgetUsage(
                wall_clock_seconds=run.budget_usage.wall_clock_seconds + delta.wall_clock_seconds,
                model_turns=run.budget_usage.model_turns + delta.model_turns,
                tool_calls=run.budget_usage.tool_calls + delta.tool_calls,
                tokens=run.budget_usage.tokens + delta.tokens,
                cost_usd=run.budget_usage.cost_usd + delta.cost_usd,
            )
            exceeded = next_usage.exceeded(run.budget_limits)
            result.update({"usage": next_usage, "exceeded": exceeded, "limits": run.budget_limits})
            run.budget_usage = next_usage
            if exceeded:
                # Progress copy is not a verified partial result.  Budget exhaustion
                # therefore fails deterministically; only Verifier may emit ``partial``.
                run.status = TaskStatus.FAILED
                run.failure_type = FailureType.BUDGET_EXHAUSTED
                run.blocking_reason = f"budget exhausted: {exceeded}"
                run.finished_at = utc_now()

        run, _event, _created = self.store.change_run(
            run_id, event_type="budget_updated",
            public_payload=lambda updated: {
                "usage": result["usage"].__dict__, "limits": result["limits"].__dict__,
                "exhausted_dimension": result["exceeded"], "status": updated.status.value,
            },
            idempotency_key=request_key, mutate=mutate,
        )
        if result.get("exceeded"):
            self.schedule()
        return run

    def request_approval(
        self, group_id: str, run_id: str, *, kind: str, summary: str,
        risk: RiskLevel, request_key: str, requested_capabilities: tuple[str, ...] = (),
        tool_name: str = "", operation_scope: Mapping[str, str] | None = None,
        purpose: str = "confirm_route", expires_at: str | None = None,
        executor_private_request: Mapping[str, Any] | None = None,
        executor_execution_key: str | None = None,
        route_request_key: str | None = None,
    ) -> ApprovalRequest:
        normalized_expiry = self._normalize_approval_expiry(expires_at)
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
            requested_capabilities=tuple(dict.fromkeys(requested_capabilities)),
            tool_name=tool_name, operation_scope=self._normalize_operation_scope(operation_scope),
            purpose=purpose,
            expires_at=normalized_expiry,
        )
        continuation_id = opaque_id("cont") if executor_private_request is not None else None

        def mutate(run: TaskRun) -> None:
            if run.status not in {TaskStatus.ROUTING, TaskStatus.RUNNING}:
                raise RuntimeConflictError("approval can only gate routing or running work")
            run.status = TaskStatus.WAITING_APPROVAL
            run.public_summary = summary or run.public_summary
            run.blocking_reason = "User approval required"

        def persist_approval(conn: Any, updated: TaskRun, _event: Any) -> None:
            self.store.upsert_approval_in_transaction(
                conn, approval, request_key=request_key,
            )
            if continuation_id is not None:
                if not executor_execution_key or not route_request_key:
                    raise RuntimeConflictError("executor continuation identity is required")
                self.store.insert_executor_continuation_in_transaction(
                    conn, continuation_id=continuation_id, run=updated,
                    approval_id=approval.approval_id,
                    execution_key=executor_execution_key,
                    route_request_key=route_request_key,
                    risk=approval.risk, status="waiting_approval",
                    private_request=executor_private_request or {},
                )

        try:
            self.store.change_run(
                run_id, event_type="approval_requested",
                public_payload={"status": TaskStatus.WAITING_APPROVAL.value, "approval": approval.to_dict()},
                idempotency_key=request_key, mutate=mutate,
                transaction_hook=persist_approval,
            )
        except sqlite3.IntegrityError as exc:
            raise RuntimeConflictError("a pending approval already gates this task") from exc
        winner = self.store.approval_by_request_key(request_key)
        if winner is None:
            raise RuntimeConflictError("approval idempotency winner is unavailable")
        self._arm_approval_expiry_watcher()
        return winner

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
            return approval, self.store.grant_for_approval(approval_id), run
        requested = set(approval.requested_capabilities)
        narrowed = tuple(capabilities) if capabilities else approval.requested_capabilities
        if not set(narrowed).issubset(requested):
            raise RuntimeConflictError("approval resolution cannot expand requested capabilities")
        if approval.expires_at and self._is_expired(approval.expires_at):
            approved = False
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
                capabilities=narrowed, tool_name=approval.tool_name,
                operation_scope=dict(approval.operation_scope), purpose=approval.purpose,
                expires_at=approval.expires_at,
            )

            def mutate(run: TaskRun) -> None:
                if run.group_id != canonical or run.status is not TaskStatus.WAITING_APPROVAL:
                    raise RuntimeConflictError("approval owner is not waiting for approval")
                run.status = TaskStatus.QUEUED
                run.public_summary = "Approval granted; task returned to queue"
                run.blocking_reason = ""

            def persist_approval_and_grant(conn: Any, _run: TaskRun, _event: Any) -> None:
                self.store.resolve_pending_approval_in_transaction(conn, resolved)
                self.store.insert_grant_in_transaction(conn, grant)
                self.store.update_executor_continuation_in_transaction(
                    conn, approval_id=approval_id, status="ready", run=_run,
                )

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
                if run.group_id != canonical or run.status is not TaskStatus.WAITING_APPROVAL:
                    raise RuntimeConflictError("approval owner is not waiting for approval")
                run.status = TaskStatus.FAILED
                run.public_summary = "Approval rejected"
                run.failure_type = FailureType.PERMISSION_DENIED
                run.finished_at = utc_now()

            def persist_rejection(conn: Any, _run: TaskRun, _event: Any) -> None:
                self.store.resolve_pending_approval_in_transaction(conn, resolved)
                self.store.update_executor_continuation_in_transaction(
                    conn, approval_id=approval_id, status="rejected", run=_run,
                )

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
        durable_approval = self.store.get_approval(approval_id)
        if durable_approval is None:
            raise RuntimeConflictError("resolved approval is unavailable")
        durable_grant = self.store.grant_for_approval(approval_id) if approved else None
        self._arm_approval_expiry_watcher()
        return durable_approval, durable_grant, self.store.get_run(run.run_id) or run

    def expire_pending_approvals(self, *, reschedule: bool = True) -> tuple[str, ...]:
        """Durably remove expired approval actions without waiting for a user click."""
        expired_ids: list[str] = []
        now_dt = datetime.now(timezone.utc)
        now = self._canonical_utc(now_dt)
        with self._lock:
            for approval in self.store.all_pending_approvals():
                invalid_expiry = False
                try:
                    if not approval.expires_at:
                        raise ValueError("missing expiry")
                    parsed_expiry = self._parse_approval_expiry(approval.expires_at)
                    remaining = (parsed_expiry - now_dt).total_seconds()
                    if not math.isfinite(remaining) or remaining > self.APPROVAL_MAX_TTL_SECONDS:
                        raise ValueError("legacy expiry exceeds policy")
                    canonical_expiry = self._canonical_utc(parsed_expiry)
                except ValueError:
                    # Legacy malformed/missing/out-of-policy rows are quarantined as
                    # immediately expired rather than bricking startup or snapshots.
                    invalid_expiry = True
                    parsed_expiry = now_dt
                    canonical_expiry = now
                if approval.expires_at != canonical_expiry:
                    try:
                        normalized = self.store.normalize_pending_approval_expiry(
                            approval.approval_id, expires_at=canonical_expiry,
                        )
                    except RuntimeStoreError:
                        continue
                    if normalized is not None:
                        approval = normalized
                if not invalid_expiry and parsed_expiry > now_dt:
                    continue
                run = self.store.get_run(approval.run_id)
                if run is None:
                    with contextlib.suppress(RuntimeStoreError):
                        self.store.reject_pending_approval(
                            approval.approval_id,
                            resolution="invalid_expiry" if invalid_expiry else "expired",
                            resolved_at=now,
                        )
                    continue
                resolution = "invalid_expiry" if invalid_expiry else "expired"
                if run.status is not TaskStatus.WAITING_APPROVAL:
                    try:
                        durable = self.store.reject_pending_approval(
                            approval.approval_id, resolution=resolution, resolved_at=now,
                        )
                    except RuntimeStoreError:
                        # Deletion reconciliation owns fenced Group content.
                        continue
                    if durable is not None and durable.status != "pending":
                        expired_ids.append(approval.approval_id)
                    continue
                resolved = replace(
                    approval, status="rejected", resolved_at=now,
                    summary="Approval is no longer actionable", expires_at=canonical_expiry,
                )

                def mutate(item: TaskRun, *, expected_group: str = approval.group_id) -> None:
                    if item.group_id != expected_group or item.status is not TaskStatus.WAITING_APPROVAL:
                        raise RuntimeConflictError("approval owner is not waiting for approval")
                    item.status = TaskStatus.FAILED
                    item.public_summary = "Approval expired"
                    item.blocking_reason = ""
                    item.failure_type = FailureType.PERMISSION_DENIED
                    item.finished_at = now

                def persist_expiry(
                    conn: Any, updated: TaskRun, _event: Any,
                    *, current_approval: ApprovalRequest = resolved,
                ) -> None:
                    self.store.resolve_pending_approval_in_transaction(conn, current_approval)
                    self.store.update_executor_continuation_in_transaction(
                        conn, approval_id=current_approval.approval_id,
                        status="rejected", run=updated,
                    )

                try:
                    self.store.change_run(
                        run.run_id, event_type="approval_resolved",
                        public_payload={
                            "status": TaskStatus.FAILED.value,
                            "approval_id": approval.approval_id,
                            "resolution": resolution,
                            "failure_type": FailureType.PERMISSION_DENIED.value,
                        },
                        idempotency_key=f"approval-expired:{approval.approval_id}",
                        mutate=mutate, transaction_hook=persist_expiry,
                    )
                except (RuntimeConflictError, RuntimeStoreError):
                    # A concurrent cancel/resolution has already made the approval
                    # non-actionable; report only the durable winner below.
                    pass
                durable = self.store.get_approval(approval.approval_id)
                if durable is not None and durable.status != "pending":
                    expired_ids.append(approval.approval_id)
        if expired_ids and reschedule:
            self.schedule(sweep_expired=False)
        self._arm_approval_expiry_watcher()
        return tuple(expired_ids)

    def reapprove_recovered_continuation(
        self, group_id: str, continuation_id: str, *, request_key: str,
        purpose: str | None = None,
        operation_scope: Mapping[str, str] | None = None,
        expires_at: str | None = None, ttl_seconds: float | int | None = None,
    ) -> tuple[ApprovalRequest, TaskRun]:
        """Create a fresh governed attempt for an uncertain high-risk request.

        The old continuation remains ``recovered_blocked`` forever.  This operation
        copies its private payload only into a new, separately approved identity.
        """
        canonical = self._canonical_group(group_id, require_active=True)
        self._assert_group_mutable(canonical)
        if not request_key.strip():
            raise ValueError("request_key is required")
        if expires_at is not None and ttl_seconds is not None:
            raise ValueError("specify approval expiry or ttl_seconds, not both")
        normalized_expiry = (
            self._normalize_approval_expiry(expires_at)
            if expires_at is not None
            else self.approval_expiry_from_ttl(ttl_seconds)
        )

        with self._lock:
            blocked = self.store.executor_continuation(continuation_id=continuation_id)
            if blocked is None or blocked.get("group_id") != canonical:
                raise RuntimeNotFoundError("recovered continuation not found in group")
            if blocked.get("status") != "recovered_blocked" or blocked.get("risk") != RiskLevel.HIGH.value:
                raise RuntimeConflictError("continuation is not a crash-blocked high-risk request")
            run = self._owned_run(str(blocked.get("run_id") or ""), canonical)
            if run.task_id != blocked.get("task_id"):
                raise RuntimeConflictError("executor continuation task binding mismatch")

            existing = self.store.approval_by_request_key(request_key)
            if existing is not None:
                linked = self.store.executor_continuation(approval_id=existing.approval_id)
                if (
                    existing.run_id != run.run_id or existing.group_id != canonical
                    or linked is None
                    or linked.get("recovery_of_continuation_id") != continuation_id
                ):
                    raise RuntimeConflictError("reapproval request_key is bound to another operation")
                return existing, self.store.get_run(run.run_id) or run
            if run.status is not TaskStatus.PAUSED:
                raise RuntimeConflictError("recovered continuation owner must remain paused")

            old_approval_id = str(blocked.get("approval_id") or "")
            old_approval = self.store.get_approval(old_approval_id) if old_approval_id else None
            if old_approval is None:
                raise RuntimeConflictError("recovered continuation has no auditable approval")
            inherited_purpose = old_approval.purpose
            if purpose is not None and str(purpose) != inherited_purpose:
                raise RuntimeConflictError("reapproval purpose must match the blocked operation")
            inherited_scope = dict(old_approval.operation_scope)
            if operation_scope is not None:
                supplied_scope = self._normalize_operation_scope(operation_scope)
                if supplied_scope != inherited_scope:
                    raise RuntimeConflictError("reapproval scope must match the blocked operation")

            private_request = blocked.get("private_request")
            if not isinstance(private_request, dict):
                raise RuntimeConflictError("recovered continuation private request is unavailable")
            private_digest = hashlib.sha256(
                json.dumps(
                    private_request, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), default=str,
                ).encode("utf-8")
            ).hexdigest()
            stored_digest = str(blocked.get("private_request_digest") or "")
            if stored_digest and stored_digest != private_digest:
                raise RuntimeConflictError("recovered continuation request digest mismatch")
            bound_digest = inherited_scope.get("request_digest")
            if bound_digest and bound_digest != private_digest:
                raise RuntimeConflictError("recovered approval request digest mismatch")
            bound_scope = {**inherited_scope, "request_digest": private_digest}
            request_digest = hashlib.sha256(request_key.encode("utf-8")).hexdigest()
            approval = ApprovalRequest(
                approval_id=opaque_id("apr"), run_id=run.run_id, group_id=canonical,
                kind="recovered_message_route", summary="Confirm recovered high-risk request",
                risk=RiskLevel.HIGH,
                requested_capabilities=old_approval.requested_capabilities,
                tool_name=old_approval.tool_name,
                operation_scope=bound_scope,
                purpose=inherited_purpose,
                expires_at=normalized_expiry,
            )
            new_continuation_id = opaque_id("cont")
            new_execution_key = (
                f"{blocked.get('execution_key')}:recovery:{request_digest[:24]}"
            )

            def mutate(item: TaskRun) -> None:
                if item.status is not TaskStatus.PAUSED or item.group_id != canonical:
                    raise RuntimeConflictError("recovered continuation owner must remain paused")
                item.status = TaskStatus.WAITING_APPROVAL
                item.public_summary = "Recovered request awaits fresh approval"
                item.blocking_reason = "User reapproval required after restart"

            def persist_reapproval(conn: Any, updated: TaskRun, _event: Any) -> None:
                self.store.upsert_approval_in_transaction(
                    conn, approval, request_key=request_key,
                )
                self.store.insert_executor_continuation_in_transaction(
                    conn, continuation_id=new_continuation_id, run=updated,
                    approval_id=approval.approval_id,
                    execution_key=new_execution_key,
                    route_request_key=f"recovery:{request_digest[:32]}",
                    risk=RiskLevel.HIGH, status="waiting_approval",
                    private_request=private_request,
                    recovery_of_continuation_id=continuation_id,
                    private_request_digest=private_digest,
                    inherited_purpose=inherited_purpose,
                )

            try:
                self.store.change_run(
                    run.run_id, event_type="approval_requested",
                    public_payload={
                        "status": TaskStatus.WAITING_APPROVAL.value,
                        "approval": approval.to_dict(),
                    },
                    idempotency_key=request_key, mutate=mutate,
                    transaction_hook=persist_reapproval,
                )
            except sqlite3.IntegrityError as exc:
                raise RuntimeConflictError("a pending approval already gates this task") from exc
            winner = self.store.approval_by_request_key(request_key)
            durable_run = self.store.get_run(run.run_id)
            if winner is None or durable_run is None:
                raise RuntimeConflictError("reapproval durability winner is unavailable")
            self._arm_approval_expiry_watcher()
            return winner, durable_run

    def prepare_tool(
        self, group_id: str, run_id: str, *, tool_name: str,
        idempotency_key: str, risk: RiskLevel, public_summary: str,
        approval_id: str | None = None, operation_scope: Mapping[str, str] | None = None,
        purpose: str = "execute_tool",
    ) -> ToolInvocation:
        run = self._owned_run(run_id, group_id)
        existing = self.store.tool_by_key(run_id, idempotency_key)
        if existing is not None:
            return existing
        normalized_scope = self._normalize_operation_scope(operation_scope)
        if risk is RiskLevel.HIGH:
            approval = self.store.get_approval(str(approval_id or ""))
            grant = self.store.grant_for_approval(str(approval_id or ""))
            authorized = not (approval is None or approval.status != "approved"
                    or approval.run_id != run.run_id or approval.group_id != run.group_id
                    or grant is None or grant.run_id != run.run_id or grant.group_id != run.group_id
                    or grant.tool_name != tool_name or tool_name not in grant.capabilities
                    or grant.operation_scope != normalized_scope or grant.purpose != purpose
                    or (grant.expires_at is not None and self._is_expired(grant.expires_at)))
            if not authorized:
                blocked = ToolInvocation(
                    invocation_id=opaque_id("tool"), run_id=run.run_id, group_id=run.group_id,
                    tool_name=tool_name, idempotency_key=idempotency_key, risk=risk,
                    status="requires_new_approval", replay_allowed=False,
                    public_summary="Purpose-specific approval is required",
                    operation_scope=normalized_scope, purpose=purpose,
                    disposition="requires_new_approval",
                )

                def ensure_running(current: TaskRun) -> None:
                    if current.status is not TaskStatus.RUNNING or current.group_id != run.group_id:
                        raise RuntimeConflictError("tools require a running task")

                self.store.change_run(
                    run_id, event_type="tool_prepared",
                    public_payload=lambda updated: {
                        "status": blocked.status, "invocation_id": blocked.invocation_id,
                        "tool_name": tool_name, "disposition": blocked.disposition,
                        "usage": updated.budget_usage.__dict__,
                    }, idempotency_key=f"tool-event:{idempotency_key}", mutate=ensure_running,
                    transaction_hook=lambda conn, _updated, _event: self.store.insert_tool_in_transaction(
                        conn, blocked,
                    ),
                )
                winner = self.store.tool_by_key(run_id, idempotency_key)
                if winner is None:
                    raise RuntimeConflictError("tool idempotency winner is unavailable")
                return winner
        invocation = ToolInvocation(
            invocation_id=opaque_id("tool"), run_id=run.run_id, group_id=run.group_id,
            tool_name=tool_name, idempotency_key=idempotency_key, risk=risk,
            replay_allowed=risk is not RiskLevel.HIGH, public_summary=public_summary,
            operation_scope=normalized_scope, purpose=purpose, disposition="created",
        )
        result: dict[str, Any] = {}

        def mutate(current: TaskRun) -> None:
            if current.status is not TaskStatus.RUNNING or current.group_id != run.group_id:
                raise RuntimeConflictError("tools require a running task")
            usage = BudgetUsage(
                wall_clock_seconds=current.budget_usage.wall_clock_seconds,
                model_turns=current.budget_usage.model_turns,
                tool_calls=current.budget_usage.tool_calls + 1,
                tokens=current.budget_usage.tokens, cost_usd=current.budget_usage.cost_usd,
            )
            if usage.exceeded(current.budget_limits):
                raise RuntimeConflictError("tool budget exhausted")
            current.budget_usage = usage
            result["usage"] = usage

        def persist(conn: Any, _updated: TaskRun, _event: Any) -> None:
            self.store.insert_tool_in_transaction(conn, invocation)

        try:
            self.store.change_run(
                run_id, event_type="tool_prepared",
                public_payload=lambda _updated: {
                    "status": "prepared", "invocation_id": invocation.invocation_id,
                    "tool_name": tool_name, "disposition": invocation.disposition,
                    "usage": result["usage"].__dict__,
                }, idempotency_key=f"tool-event:{idempotency_key}", mutate=mutate,
                transaction_hook=persist,
            )
        except sqlite3.IntegrityError:
            duplicate = self.store.tool_by_key(run_id, idempotency_key)
            if duplicate is not None:
                return duplicate
            raise
        winner = self.store.tool_by_key(run_id, idempotency_key)
        if winner is None:
            raise RuntimeConflictError("tool idempotency winner is unavailable")
        return winner

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
        winner = self.store.checkpoint_by_key(run_id, request_key)
        if winner is None:
            raise RuntimeConflictError("checkpoint idempotency winner is unavailable")
        return winner

    def snapshot(self, group_id: str, run_id: str) -> dict[str, Any]:
        self.expire_pending_approvals()
        run = self._owned_run(run_id, group_id, allow_deleting=True)
        return {"schema_version": 1, "run": run.to_public_dict(),
                "last_seq": run.last_seq, "version": run.version}

    def replay(
        self, group_id: str, run_id: str, *, after_seq: int, after_version: int | None = None,
    ) -> dict[str, Any]:
        run = self._owned_run(run_id, group_id, allow_deleting=True)
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
        self.expire_pending_approvals()
        canonical = self._canonical_group(group_id)
        runs = self.store.list_runs(group_id=canonical)
        return {
            "schema_version": 1, "group_id": canonical,
            "runs": [run.to_public_dict() for run in runs],
            "pending_approvals": [item.to_dict() for item in self.store.pending_approvals(canonical)],
            "summary": self.group_summary(canonical),
        }

    def group_summary(self, group_id: str) -> dict[str, Any]:
        self.expire_pending_approvals()
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

    def _suspend_group_runs(
        self, canonical: str, *, reason: str, allow_deleting: bool,
    ) -> tuple[str, ...]:
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
                    _allow_deleting=allow_deleting,
                )
                changed.append(run.run_id)
            self.store.revoke_group_grants(canonical)
        self.schedule()
        return tuple(changed)

    def suspend_group(self, group_id: str, *, reason: str) -> tuple[str, ...]:
        """Fence all work before a non-destructive Group archive transition."""
        canonical = self._canonical_group(group_id, require_active=True)
        self._assert_group_mutable(canonical)
        return self._suspend_group_runs(
            canonical, reason=reason, allow_deleting=False,
        )

    def suspend_group_for_deletion(
        self, group_id: str, group_transaction_id: str, *, reason: str,
    ) -> tuple[str, ...]:
        """Fence work only for the exact user-confirmed canonical delete intent."""
        canonical = self._canonical_group(group_id)
        self._assert_not_deferred_quarantine(canonical)
        _transaction, proof = self._canonical_group_deletion_transaction(
            canonical, group_transaction_id,
            allowed_stages={"prepared", "runtime_deleted"},
        )
        intent = next((
            item for item in self.store.group_deletion_intents()
            if item["group_id"] == canonical
        ), None)
        if intent is not None and any((
            intent["group_transaction_id"] != group_transaction_id,
            intent.get("group_proof_digest") != proof,
            intent.get("stage") not in {"prepared"},
        )):
            raise RuntimeConflictError("Task Runtime deletion proof mismatch")
        return self._suspend_group_runs(
            canonical, reason=reason, allow_deleting=True,
        )

    def delete_group_data(self, group_id: str) -> dict[str, int]:
        """Called only after the Group repository committed its deletion receipt."""
        with self._lock:
            self._validated_runtime_group_deletion(
                str(group_id), canonical_stages={"committed"},
                runtime_stages={"prepared", "runtime_cleaned", "complete"},
            )
            return self.store.delete_group(str(group_id))

    def prepare_group_deletion(self, group_id: str, group_transaction_id: str) -> dict[str, str]:
        canonical = self._canonical_group(group_id)
        self._assert_not_deferred_quarantine(canonical)
        transaction, proof = self._canonical_group_deletion_transaction(
            canonical, group_transaction_id,
            allowed_stages={"prepared", "runtime_deleted"},
        )
        return self.store.prepare_group_deletion(
            canonical, group_transaction_id,
            group_proof_digest=proof,
            confirmation_token=str(transaction["confirmation_token"]),
            runtime_project_id=str(transaction["runtime_project_id"]),
        )

    @staticmethod
    def _group_deletion_proof(transaction: Mapping[str, Any]) -> str:
        receipt = transaction.get("receipt")
        if not isinstance(receipt, Mapping):
            raise RuntimeConflictError("canonical Group deletion receipt proof is missing")
        required = (
            transaction.get("transaction_id"), transaction.get("group_id"),
            transaction.get("runtime_project_id"), transaction.get("confirmation_token"),
            receipt.get("receipt_id"), receipt.get("group_id"),
        )
        if not all(str(value or "") for value in required):
            raise RuntimeConflictError("canonical Group deletion proof is incomplete")
        # Stage/timestamps and the final deleted-source set are the only fields the
        # Group state machine is allowed to change.  Hash everything else so a
        # modified source scope, receipt count, identity, or confirmation token
        # cannot inherit the original Task Runtime authorization.
        proof = dict(transaction)
        proof.pop("stage", None)
        proof.pop("updated_at", None)
        proof_receipt = dict(receipt)
        proof_receipt.pop("deleted_source_ids", None)
        proof["receipt"] = proof_receipt
        return hashlib.sha256(
            json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ).hexdigest()

    def _canonical_group_deletion_transaction(
        self, group_id: str, group_transaction_id: str, *,
        allowed_stages: set[str],
    ) -> tuple[dict[str, Any], str]:
        transaction = self.groups.delete_transaction(group_id)
        if transaction is None:
            raise RuntimeConflictError("canonical Group deletion transaction is required")
        if str(transaction.get("group_id") or "") != group_id:
            raise RuntimeConflictError("canonical Group deletion identity mismatch")
        if str(transaction.get("transaction_id") or "") != group_transaction_id:
            raise RuntimeConflictError("canonical Group deletion transaction mismatch")
        if str(transaction.get("stage") or "") not in allowed_stages:
            raise RuntimeConflictError("canonical Group deletion stage is not authorized")
        proof = self._group_deletion_proof(transaction)
        return transaction, proof

    def _validated_runtime_group_deletion(
        self, group_id: str, *, canonical_stages: set[str],
        runtime_stages: set[str], allow_legacy_proof_bind: bool = False,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        self._assert_not_deferred_quarantine(group_id)
        intent = next((
            item for item in self.store.group_deletion_intents()
            if item["group_id"] == group_id
        ), None)
        if intent is None:
            raise RuntimeConflictError("Task Runtime deletion intent is required")
        if intent.get("stage") not in runtime_stages:
            raise RuntimeConflictError("Task Runtime deletion intent stage is not authorized")
        transaction, proof = self._canonical_group_deletion_transaction(
            group_id, str(intent["group_transaction_id"]),
            allowed_stages=canonical_stages,
        )
        if (
            allow_legacy_proof_bind
            and not intent.get("group_proof_digest")
            and not intent.get("confirmation_token")
            and not intent.get("runtime_project_id")
        ):
            intent = self.store.bind_group_deletion_proof(
                group_id, str(intent["group_transaction_id"]),
                group_proof_digest=proof,
                confirmation_token=str(transaction["confirmation_token"]),
                runtime_project_id=str(transaction["runtime_project_id"]),
            )
        if any((
            intent.get("group_proof_digest") != proof,
            intent.get("confirmation_token") != str(transaction["confirmation_token"]),
            intent.get("runtime_project_id") != str(transaction["runtime_project_id"]),
        )):
            raise RuntimeConflictError("Task Runtime deletion proof does not match canonical Group")
        return intent, transaction

    def clean_prepared_group_deletion(self, group_id: str) -> dict[str, int]:
        with self._lock:
            intent, _transaction = self._validated_runtime_group_deletion(
                group_id,
                canonical_stages={"prepared", "runtime_deleted", "committed"},
                runtime_stages={"prepared", "runtime_cleaned"},
            )
            # The durable intent itself is the mutation fence.  Re-clean on every
            # reconciliation so a row written by an older binary or a pre-fence race
            # can never brick startup after the canonical Group commit.
            counts = self.store.delete_group(group_id)
            if intent["stage"] != "complete":
                self.store.set_group_deletion_stage(group_id, "runtime_cleaned")
            return counts

    def complete_group_deletion(self, group_id: str) -> None:
        # Idempotently sweep legacy/race residue before marking completion.  The
        # deletion row remains as a permanent write fence without retaining content.
        with self._lock:
            self._validated_runtime_group_deletion(
                group_id, canonical_stages={"committed"},
                runtime_stages={"prepared", "runtime_cleaned", "complete"},
            )
            self.store.delete_group(group_id)
            remaining = self.store.group_content_counts(group_id)
            if any(remaining.values()):
                raise RuntimeConflictError(
                    f"Task Runtime content remains after deletion: {remaining}",
                )
            self.store.set_group_deletion_stage(group_id, "complete")

    def _defer_quarantine(self, group_id: str, audit_code: str) -> None:
        """Retain one bounded fail-closed retry marker per affected Group."""
        with self._lock:
            if group_id in self._deferred_quarantines:
                self._deferred_quarantines[group_id] = str(audit_code)[:120]
            elif len(self._deferred_quarantines) < self.DEFERRED_QUARANTINE_LIMIT:
                self._deferred_quarantines[group_id] = str(audit_code)[:120]
            else:
                # The durable invalid proof remains the primary fence.  Overflow is
                # one coalesced rescan bit rather than an unbounded in-memory queue.
                self._deferred_quarantine_overflow = True
            if self._quarantine_retry_enabled:
                self._arm_deferred_quarantine_retry_locked()

    def _clear_deferred_quarantine(self, group_id: str) -> None:
        with self._lock:
            self._deferred_quarantines.pop(group_id, None)

    def _arm_deferred_quarantine_retry(self) -> None:
        with self._lock:
            self._arm_deferred_quarantine_retry_locked()

    def _arm_deferred_quarantine_retry_locked(self) -> None:
        if (
            self._closed
            or not self._quarantine_retry_enabled
            or self._quarantine_retry_running
            or self._quarantine_retry_timer is not None
            or not (self._deferred_quarantines or self._deferred_quarantine_overflow)
        ):
            return
        delay = min(
            self.DEFERRED_QUARANTINE_RETRY_MAX_SECONDS,
            self.DEFERRED_QUARANTINE_RETRY_BASE_SECONDS
            * (2 ** min(self._quarantine_retry_attempt, 6)),
        )
        timer = threading.Timer(delay, self._retry_deferred_quarantines)
        timer.daemon = True
        self._quarantine_retry_timer = timer
        timer.start()

    def _retry_deferred_quarantines(self) -> None:
        """Lifecycle-owned retry; all faults are observed and re-armed with backoff."""
        with self._lock:
            self._quarantine_retry_timer = None
            if self._closed:
                return
            self._quarantine_retry_running = True
            self._quarantine_retry_thread_id = threading.get_ident()
            pending = tuple(self._deferred_quarantines.items())
            had_overflow = self._deferred_quarantine_overflow
            self._deferred_quarantine_overflow = False
        made_progress = False
        try:
            for group_id, audit_code in pending:
                try:
                    self.store.quarantine_group_deletion(group_id, audit_code)
                except Exception:  # noqa: BLE001 - lifecycle containment boundary
                    continue
                with self._lock:
                    if self._deferred_quarantines.get(group_id) == audit_code:
                        self._deferred_quarantines.pop(group_id, None)
                        made_progress = True
            if had_overflow:
                # Rediscover overflowed invalid proofs after slots become available.
                # This scan is bounded by the durable intent table and stores only
                # the fixed-size retry cohort above.
                reports = self.reconcile_group_deletions()
                if hasattr(self, "recovery_report"):
                    self.recovery_report["deletions"] = reports
        except Exception:  # noqa: BLE001 - Timer callbacks never leak exceptions
            pass
        finally:
            with self._lock:
                self._quarantine_retry_running = False
                self._quarantine_retry_thread_id = None
                if made_progress:
                    self._quarantine_retry_attempt = 0
                elif self._deferred_quarantines or self._deferred_quarantine_overflow:
                    self._quarantine_retry_attempt += 1
                self._arm_deferred_quarantine_retry_locked()

    def deletion_recovery_exclusions(self) -> tuple[set[str], bool]:
        """Return automatic-delete exclusions and whether the Store view is unavailable."""
        with self._lock:
            excluded = set(self._deferred_quarantines)
            overflowed = (
                self._deferred_quarantine_overflow
                or self._quarantine_retry_running
            )
        try:
            intents = self.store.group_deletion_intents()
        except Exception:  # noqa: BLE001 - fail all candidates closed for this pass
            return excluded, True
        excluded.update(
            item["group_id"] for item in intents
            if item.get("stage") == "quarantined"
        )
        return excluded, overflowed

    def reconcile_group_deletions(self) -> list[dict[str, str]]:
        report: list[dict[str, str]] = []
        try:
            intents = self.store.group_deletion_intents()
        except Exception as exc:  # noqa: BLE001 - constructor must remain available
            return [{
                "group_id": "", "status": "deferred_global",
                "reason": type(exc).__name__,
            }]
        for intent in intents:
            group_id = intent["group_id"]
            if intent.get("stage") == "quarantined":
                self._clear_deferred_quarantine(group_id)
                report.append({
                    "group_id": group_id, "status": "quarantined",
                    "reason": str(intent.get("audit_code") or "invalid_canonical_proof"),
                })
                continue
            try:
                _bound, transaction = self._validated_runtime_group_deletion(
                    group_id,
                    canonical_stages={"prepared", "runtime_deleted", "committed"},
                    runtime_stages={"prepared", "runtime_cleaned", "complete"},
                    allow_legacy_proof_bind=True,
                )
                if transaction.get("stage") == "committed":
                    if intent.get("stage") != "complete":
                        self.clean_prepared_group_deletion(group_id)
                    self.complete_group_deletion(group_id)
                    report.append({"group_id": group_id, "status": "complete"})
                elif intent.get("stage") == "complete":
                    raise RuntimeConflictError(
                        "Runtime deletion completed before canonical Group commit",
                    )
                else:
                    self.clean_prepared_group_deletion(group_id)
                    report.append({"group_id": group_id, "status": "awaiting_group_commit"})
            except RuntimeConflictError as exc:
                reason = type(exc).__name__
                try:
                    self.store.quarantine_group_deletion(group_id, reason)
                except Exception as audit_exc:  # noqa: BLE001 - quarantine must not brick startup
                    self._defer_quarantine(group_id, reason)
                    report.append({
                        "group_id": group_id, "status": "deferred_quarantine",
                        "reason": type(audit_exc).__name__,
                    })
                else:
                    self._clear_deferred_quarantine(group_id)
                    report.append({
                        "group_id": group_id, "status": "quarantined",
                        "reason": reason,
                    })
            except RuntimeStoreError as exc:
                # A storage outage is retryable and must not rewrite a valid
                # canonical authorization into a permanent quarantine.
                report.append({
                    "group_id": group_id, "status": "deferred",
                    "reason": type(exc).__name__,
                })
            except Exception as exc:  # noqa: BLE001 - isolate validator/repository faults
                report.append({
                    "group_id": group_id, "status": "deferred",
                    "reason": type(exc).__name__,
                })
        return report

    def _round_robin(self, candidates: list[TaskRun], lane: str) -> list[TaskRun]:
        if not candidates:
            return []
        groups = sorted({run.group_id for run in candidates})
        cursor = self.store.scheduler_cursor(lane)
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
            return (
                self.groups.get(group_id).lifecycle is GroupLifecycle.ACTIVE
                and self.groups.delete_transaction(group_id) is None
                and not self.store.group_is_deleting(group_id)
            )
        except GroupNotFoundError:
            return False

    def _arm_approval_expiry_watcher(self) -> None:
        """Own one nearest-deadline timer reconstructed from durable approvals."""
        with self._lock:
            current = self._approval_expiry_timer
            self._approval_expiry_timer = None
            if current is not None:
                current.cancel()
            if self._closed:
                return
            now = datetime.now(timezone.utc)
            nearest: datetime | None = None
            for approval in self.store.all_pending_approvals():
                if not approval.expires_at:
                    candidate = now
                else:
                    try:
                        candidate = self._parse_approval_expiry(approval.expires_at)
                        ttl = (candidate - now).total_seconds()
                        if not math.isfinite(ttl) or ttl > self.APPROVAL_MAX_TTL_SECONDS:
                            candidate = now
                    except ValueError:
                        candidate = now
                if nearest is None or candidate < nearest:
                    nearest = candidate
            if nearest is None:
                return
            delay = max(0.005, (nearest - now).total_seconds() + 0.002)
            timer = threading.Timer(delay, self._approval_expiry_deadline)
            timer.daemon = True
            self._approval_expiry_timer = timer
            timer.start()

    def _approval_expiry_deadline(self) -> None:
        with self._lock:
            self._approval_expiry_timer = None
            if self._closed:
                return
        try:
            self.expire_pending_approvals()
        except Exception:  # noqa: BLE001 -- durable next startup/read retries safely
            # Re-arm shortly instead of letting one transient SQLite race permanently
            # disable autonomous expiry.
            with self._lock:
                if self._closed:
                    return
                timer = threading.Timer(0.05, self._approval_expiry_deadline)
                timer.daemon = True
                self._approval_expiry_timer = timer
                timer.start()

    def schedule(self, *, sweep_expired: bool = True) -> tuple[TaskRun, ...]:
        if self._closed:
            return ()
        if sweep_expired:
            self.expire_pending_approvals(reschedule=False)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=self.SCHEDULER_LEASE_SECONDS)
        if not self.store.acquire_scheduler_lease(
            self._scheduler_owner,
            now=now.isoformat().replace("+00:00", "Z"),
            expires_at=expires.isoformat().replace("+00:00", "Z"),
        ):
            self._arm_scheduler_retry(self.store.scheduler_lease_expiry())
            return ()
        try:
            return self._schedule_under_lease()
        finally:
            self.store.release_scheduler_lease(self._scheduler_owner)

    def _arm_scheduler_retry(self, expires_at: str | None) -> None:
        if self._closed:
            return
        try:
            expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
            delay = max(0.01, (expiry - datetime.now(timezone.utc)).total_seconds() + 0.02)
        except (TypeError, ValueError):
            delay = max(0.05, self.SCHEDULER_LEASE_SECONDS)
        with self._lock:
            current = self._scheduler_retry
            if current is not None and current.is_alive():
                return
            timer = threading.Timer(delay, self._retry_scheduler_after_lease)
            timer.daemon = True
            self._scheduler_retry = timer
            timer.start()

    def _retry_scheduler_after_lease(self) -> None:
        with self._lock:
            self._scheduler_retry = None
            if self._closed:
                return
            try:
                self.schedule()
            except Exception:  # noqa: BLE001 -- background retry is best-effort and bounded
                # A competing healthy instance may have renewed the lease; schedule()
                # arms the next expiry retry in that case.  Other errors remain visible
                # through normal foreground operations without killing this watcher.
                return

    def _schedule_under_lease(self) -> tuple[TaskRun, ...]:
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
            ], "primary")
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
            ], "side")
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
            ], "index")[:max(0, index_capacity)]
            started: list[TaskRun] = []
            for run in primary + side + index:
                try:
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
                except (RuntimeConflictError, RuntimeNotFoundError, RuntimeStoreError):
                    # A concurrent durable delete fence wins over dispatch.
                    continue
                lane = "index" if run.queue_class is QueueClass.INDEX else run.kind.value
                self.store.set_scheduler_cursor(lane, run.group_id)
                started.append(running)
            return tuple(started)

    def route_message(
        self, group_id: str, *, content: str, request_key: str,
        confidence: float, read_only: bool, writes: bool = False,
        sensitive_outbound: bool = False, explicit_new_task: bool = False,
        executor_private_request: Mapping[str, Any] | None = None,
        executor_execution_key: str | None = None,
    ) -> dict[str, Any]:
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("route confidence must be finite and between 0 and 1")
        classifications = classify_public_text(content)
        sensitive_outbound = sensitive_outbound or bool(classifications)
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
        private_request = dict(executor_private_request or {})
        if executor_private_request is not None:
            private_request.setdefault("content", content)
            if not executor_execution_key:
                raise ValueError("executor_execution_key is required with a private executor request")

        if label is RouteLabel.SUPPLEMENT_CURRENT and primary is not None and not requires_approval:
            continuation_id = opaque_id("cont") if executor_private_request is not None else None

            def persist_supplement(conn: Any, updated: TaskRun, _event: Any) -> None:
                if continuation_id is not None and executor_execution_key is not None:
                    self.store.insert_executor_continuation_in_transaction(
                        conn, continuation_id=continuation_id, run=updated, approval_id=None,
                        execution_key=executor_execution_key, route_request_key=request_key,
                        risk=updated.risk, status="ready", private_request=private_request,
                    )

            updated, event, _created = self.store.change_run(
                primary.run_id, event_type="message_routed",
                public_payload={"route_label": label.value, "summary": "Message supplements current task",
                                "classification": list(classifications)},
                idempotency_key=request_key,
                sensitive_payload={"content": content}, sensitivity="private",
                transaction_hook=persist_supplement,
            )
            return {"route_label": label.value, "auto_routed": True,
                    "run": updated.to_public_dict(), "event": event.to_public_dict()}

        kind = RunKind.SIDE if label is RouteLabel.SIDE_ANSWER and not requires_approval else RunKind.PRIMARY
        protected = requires_approval or sensitive_outbound
        public_title = "Protected request awaiting confirmation" if protected else "New read-only task"
        public_summary = "Request requires routing confirmation" if protected else "Read-only request accepted"
        run = self.submit(
            canonical, title=public_title, request_key=request_key,
            kind=kind, route_label=label, read_only=(kind is RunKind.SIDE),
            risk=RiskLevel.LOW if read_only and not writes else RiskLevel.MEDIUM,
            parent_run_id=primary.run_id if kind is RunKind.SIDE and primary else None,
            public_summary=public_summary,
            data_classification=classifications,
        )
        continuation_id = opaque_id("cont") if executor_private_request is not None else None

        def persist_auto_continuation(conn: Any, updated: TaskRun, _event: Any) -> None:
            if (not requires_approval and continuation_id is not None
                    and executor_execution_key is not None):
                self.store.insert_executor_continuation_in_transaction(
                    conn, continuation_id=continuation_id, run=updated, approval_id=None,
                    execution_key=executor_execution_key, route_request_key=request_key,
                    risk=updated.risk, status="ready", private_request=private_request,
                )

        self.store.change_run(
            run.run_id, event_type="message_routed",
            public_payload={"route_label": label.value, "summary": public_summary,
                            "classification": list(classifications)},
            sensitive_payload={"content": content}, sensitivity="private",
            idempotency_key=f"route-content:{request_key}",
            transaction_hook=persist_auto_continuation,
        )
        approval: ApprovalRequest | None = None
        if requires_approval:
            routed = self.transition(canonical, run.run_id, TaskStatus.ROUTING,
                                     idempotency_key=f"route-gate:{request_key}",
                                     summary="Routing needs confirmation")
            approval = self.request_approval(
                canonical, routed.run_id, kind="message_route",
                summary="Confirm task routing or sensitive/write operation",
                risk=RiskLevel.HIGH if sensitive_outbound or writes else RiskLevel.MEDIUM,
                request_key=f"approval:{request_key}",
                requested_capabilities=("route_message",), purpose="confirm_route",
                operation_scope={"route": label.value},
                executor_private_request=private_request if executor_private_request is not None else None,
                executor_execution_key=executor_execution_key,
                route_request_key=request_key,
            )
            run = self.store.get_run(run.run_id) or run
        else:
            self.schedule()
            run = self.store.get_run(run.run_id) or run
        return {"route_label": label.value, "auto_routed": not requires_approval,
                "run": run.to_public_dict(),
                "approval": approval.to_dict() if approval else None}

    @staticmethod
    def _contains_pii_or_secret(content: str) -> bool:
        return bool(classify_public_text(content))

    @staticmethod
    def _normalize_operation_scope(value: Mapping[str, str] | None) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, item in dict(value or {}).items():
            key_text, item_text = str(key), str(item)
            if not key_text or len(key_text) > 64 or len(item_text) > 160:
                raise ValueError("operation scope is invalid")
            if not all(char.isalnum() or char in "_.:/-" for char in key_text + item_text):
                raise ValueError("operation scope must contain normalized identifiers only")
            result[key_text] = item_text
        return result

    @classmethod
    def _parse_approval_expiry(cls, value: str) -> datetime:
        text = str(value)
        if not cls._AWARE_ISO8601.fullmatch(text):
            raise ValueError("approval expiry must be a timezone-aware ISO-8601 timestamp")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            offset = parsed.utcoffset()
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("approval expiry is invalid") from exc
        if parsed.tzinfo is None or offset is None:
            raise ValueError("approval expiry must include a timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _canonical_utc(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")

    @classmethod
    def _normalize_approval_expiry(
        cls, value: str | None, *, now: datetime | None = None,
    ) -> str:
        reference = now or datetime.now(timezone.utc)
        if value is None:
            parsed = reference + timedelta(seconds=cls.APPROVAL_DEFAULT_TTL_SECONDS)
        else:
            parsed = cls._parse_approval_expiry(value)
        delta = (parsed - reference).total_seconds()
        if not math.isfinite(delta) or delta <= 0:
            raise ValueError("approval expiry must be in the future")
        if delta > cls.APPROVAL_MAX_TTL_SECONDS:
            raise ValueError("approval expiry exceeds the policy maximum")
        return cls._canonical_utc(parsed)

    @classmethod
    def approval_expiry_from_ttl(
        cls, ttl_seconds: float | int | None,
    ) -> str:
        ttl = cls.APPROVAL_DEFAULT_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            raise ValueError("approval ttl_seconds must be a finite number")
        numeric = float(ttl)
        if (
            not math.isfinite(numeric) or numeric <= 0
            or numeric > cls.APPROVAL_MAX_TTL_SECONDS
        ):
            raise ValueError("approval ttl_seconds is outside the policy range")
        try:
            expiry = datetime.now(timezone.utc) + timedelta(seconds=numeric)
        except OverflowError as exc:
            raise ValueError("approval ttl_seconds is invalid") from exc
        return cls._normalize_approval_expiry(cls._canonical_utc(expiry))

    @classmethod
    def _is_expired(cls, value: str) -> bool:
        try:
            return cls._parse_approval_expiry(value) <= datetime.now(timezone.utc)
        except ValueError:
            # Legacy malformed permission records are never treated as authority.
            return True

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
                    self.store.mark_tool_recovered_blocked(invocation.invocation_id)
                    self.store.revoke_group_grants(invocation.group_id)
                    report["high_risk_blocked"].append(invocation.invocation_id)
                else:
                    # A later Tool Gateway may offer an explicit user retry.  Recovery
                    # only reports it; it does not call the tool.
                    report["replayable_pending"].append(invocation.invocation_id)
            continuation_report = self.store.reconcile_executor_continuations()
            report["executor_continuations"] = continuation_report
            for continuation_id in continuation_report["recovered_blocked"]:
                continuation = self.store.executor_continuation(
                    continuation_id=continuation_id,
                )
                if continuation is None:
                    continue
                run = self.store.get_run(str(continuation.get("run_id") or ""))
                if run is not None and run.status is TaskStatus.QUEUED:
                    routed = self.transition(
                        run.group_id, run.run_id, TaskStatus.ROUTING,
                        idempotency_key=f"continuation-recovery-route:{continuation_id}",
                        summary="Recovered request requires explicit approval",
                    )
                    self.transition(
                        routed.group_id, routed.run_id, TaskStatus.PAUSED,
                        idempotency_key=f"continuation-recovery-pause:{continuation_id}",
                        summary="Recovered request requires explicit approval",
                        blocking_reason="Executor request was not replayed after restart",
                    )
        return report

    def claim_executor_continuation(
        self, group_id: str, run_id: str, *, execution_key: str,
    ) -> tuple[dict[str, Any] | None, bool]:
        run = self._owned_run(run_id, group_id)
        continuation = self.store.executor_continuation(
            run_id=run_id, execution_key=execution_key,
        )
        if continuation is None:
            return None, False
        if (continuation.get("group_id") != run.group_id
                or continuation.get("task_id") != run.task_id):
            raise RuntimeConflictError("executor continuation task binding mismatch")
        if run.status is not TaskStatus.RUNNING:
            return continuation, False
        recovery_of = str(continuation.get("recovery_of_continuation_id") or "")
        if recovery_of:
            private_request = continuation.get("private_request")
            if not isinstance(private_request, dict):
                raise RuntimeConflictError("recovered continuation private request is unavailable")
            digest = hashlib.sha256(
                json.dumps(
                    private_request, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), default=str,
                ).encode("utf-8")
            ).hexdigest()
            approval_id = str(continuation.get("approval_id") or "")
            approval = self.store.get_approval(approval_id)
            grant = self.store.grant_for_approval(approval_id)
            blocked = self.store.executor_continuation(continuation_id=recovery_of)
            inherited_purpose = str(continuation.get("inherited_purpose") or "")
            if (
                continuation.get("risk") != RiskLevel.HIGH.value
                or continuation.get("private_request_digest") != digest
                or approval is None or approval.status != "approved"
                or approval.risk is not RiskLevel.HIGH
                or approval.run_id != run.run_id or approval.group_id != run.group_id
                or approval.purpose != inherited_purpose
                or approval.operation_scope.get("request_digest") != digest
                or grant is None or grant.approval_id != approval_id
                or grant.run_id != run.run_id or grant.group_id != run.group_id
                or grant.task_id != run.task_id or grant.purpose != inherited_purpose
                or grant.operation_scope != approval.operation_scope
                or "route_message" not in grant.capabilities
                or (grant.expires_at is not None and self._is_expired(grant.expires_at))
                or blocked is None or blocked.get("status") != "recovered_blocked"
                or blocked.get("group_id") != run.group_id
                or blocked.get("task_id") != run.task_id
            ):
                raise RuntimeConflictError("recovered continuation approval binding mismatch")
        return self.store.claim_executor_continuation(str(continuation["continuation_id"]))

    def claim_approved_executor_continuation(
        self, group_id: str, approval_id: str,
    ) -> tuple[dict[str, Any] | None, bool]:
        continuation = self.store.executor_continuation(approval_id=approval_id)
        if continuation is None:
            return None, False
        run_id = str(continuation.get("run_id") or "")
        return self.claim_executor_continuation(
            group_id, run_id, execution_key=str(continuation.get("execution_key") or ""),
        )

    def finish_executor_continuation(
        self, continuation_id: str, *, status: str, failure_type: FailureType | None = None,
    ) -> dict[str, Any]:
        return self.store.finish_executor_continuation(
            continuation_id, status=status,
            failure_type=failure_type.value if failure_type else None,
        )


__all__ = ["RuntimeConflictError", "RuntimeNotFoundError", "TaskRuntimeService"]
