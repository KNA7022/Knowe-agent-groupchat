"""Narrow, synchronous card transactions. No renderer, provider or worker route.

Composition may inject a source-version guard only when source mutations share
that guard. The default deliberately rejects groups with sources or courses.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, replace
import hmac
import json
import re
from typing import ContextManager, Protocol, TYPE_CHECKING

from backend.groups import Group, GroupDomainError, GroupLifecycle

from .card_state import (
    BoundCardState, CardProposal, CardReceipt, CardStateError, STORAGE_FAILURES,
    bounded, canonical, request_id,
)
from .contracts import ApprovalRequest, PermissionGrant, RiskLevel, TaskRun, TaskStatus, opaque_id, utc_now
from .store import RuntimeStoreError

if TYPE_CHECKING:
    from .service import TaskRuntimeService


class CardScopeRevoked(CardStateError):
    pass


@dataclass(frozen=True)
class CardSourceRevision:
    revision: str

    def __post_init__(self):
        bounded(self.revision, 256)


class CardSourceValidator(Protocol):
    def guard(self, group: Group) -> ContextManager[CardSourceRevision]:
        """Validate actual sources and retain their mutation fence through commit.

        Backend composition only. Do not perform async/network work. Lock order:
        runtime service -> Group registry -> source guard -> runtime SQLite.
        Raise CardScopeRevoked for removed/version-changed source scope.
        """
        ...


@dataclass(frozen=True)
class CardResolution:
    approval: ApprovalRequest
    grant: PermissionGrant | None
    run: TaskRun


class BoundCardService:
    def __init__(self, runtime: TaskRuntimeService, source_validator: CardSourceValidator | None = None):
        if source_validator is not None and not callable(getattr(source_validator, "guard", None)):
            raise CardStateError("backend source validator capability required")
        self.runtime = runtime
        self.store = runtime.store
        self.repository = runtime.store.card_states
        self._source_validator = source_validator

    @contextmanager
    def _admission(self, group_id: str, approval_id: str | None = None):
        # Acquire the same service mutex used by scheduling, shutdown and quarantine.
        with self.runtime._lock:
            try:
                if self.runtime._closed:
                    raise CardStateError("card store is closed")
                with self.runtime.groups.admission_guard():
                    try:
                        group = self.runtime.groups.get(group_id)
                        if group.lifecycle is not GroupLifecycle.ACTIVE:
                            raise CardScopeRevoked("card group unavailable")
                        if self.runtime.groups.delete_transaction(group_id) is not None:
                            raise CardScopeRevoked("card group deletion fenced")
                    except GroupDomainError:
                        raise CardScopeRevoked("card group unavailable") from None
                    if group_id in self.runtime._deferred_quarantines:
                        raise CardScopeRevoked("card group quarantined")
                    if self.store.group_is_deleting(group_id):
                        raise CardScopeRevoked("card runtime group fenced")
                    if self._source_validator is None:
                        if group.source_links or group.course_bindings:
                            raise CardScopeRevoked("card source version fence unavailable")
                        source_guard = nullcontext(CardSourceRevision("no-source-content"))
                    else:
                        source_guard = self._source_validator.guard(group)
                    with source_guard as source:
                        if not isinstance(source, CardSourceRevision):
                            raise CardStateError("invalid backend source validator contract")
                        revision = self.repository.digest("scope", [group.to_dict(), source.revision])
                        yield group, revision
            except CardScopeRevoked:
                if approval_id is not None:
                    try:
                        self._invalidate_owned(group_id, approval_id)
                    except STORAGE_FAILURES:
                        raise CardStateError("card invalidation storage unavailable") from None
                raise
            except (RuntimeStoreError, ValueError, TypeError, *STORAGE_FAILURES):
                raise CardStateError("card storage or authority unavailable") from None

    def _invalidate_owned(self, group_id: str, approval_id: str) -> None:
        # Wrong/missing keys and tampered envelopes never become an erase operation.
        with self.store.db.transaction() as conn:
            row = conn.execute("SELECT * FROM studycrew_approvals WHERE approval_id=? AND group_id=?",
                               (approval_id, group_id)).fetchone()
            if row is None or row["card_state_ciphertext"] is None:
                return
            run = self._owner(conn, row["run_id"], group_id)
            self.repository.open(conn, row, run.task_id)
            self.repository.invalidate(conn, approval_id, "scope_or_expiry_revoked")
        self.repository.checkpoint()

    def _owner(self, conn, run_id: str, group_id: str) -> TaskRun:
        row = conn.execute("SELECT * FROM studycrew_task_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row["group_id"] != group_id:
            raise CardStateError("card owner unavailable")
        run = self.store._row_run(row)
        if (run.run_id, run.task_id, run.group_id, run.status.value) != (
            row["run_id"], row["task_id"], row["group_id"], row["status"]
        ):
            raise CardStateError("card owner identity mismatch")
        return run

    @staticmethod
    def _target(group: Group, proposal: CardProposal) -> CardProposal:
        matches = [agent for agent in group.roster if agent.role == proposal.target_role
                   and (not proposal.target_agent_id or agent.agent_id == proposal.target_agent_id)]
        if len(matches) != 1:
            raise CardScopeRevoked("card target no longer available or ambiguous")
        return replace(proposal, target_agent_id=matches[0].agent_id)

    def _current(self, conn, group: Group, approval_id: str, revision: str):
        row = conn.execute("SELECT * FROM studycrew_approvals WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None or row["group_id"] != group.group_id:
            raise CardStateError("card not found in group")
        self.store._assert_group_mutable_in_transaction(conn, group.group_id)
        run = self._owner(conn, row["run_id"], group.group_id)
        state = self.repository.open(conn, row, run.task_id)
        approval = self.store._approval_from_raw(json.loads(row["payload_json"]))
        if (approval.approval_id, approval.run_id, approval.group_id, approval.status, approval.purpose,
            approval.expires_at) != (row["approval_id"], row["run_id"], row["group_id"], row["status"],
                                     "card_dispatch", state.expires_at):
            raise CardStateError("card approval identity or policy mismatch")
        expected_status = "approved" if state.receipts[-1].action == "approved" else "pending"
        if approval.status != expected_status:
            raise CardStateError("card lifecycle mismatch")
        if (approval.requested_capabilities != ("card_dispatch",) or approval.kind != "propose_next"
            or approval.risk is not RiskLevel.HIGH):
            raise CardStateError("card policy envelope mismatch")
        if self.runtime._is_expired(state.expires_at) or state.scope_revision != revision:
            raise CardScopeRevoked("card scope or expiry revoked")
        self._target(group, state.proposal)
        allowed = {TaskStatus.WAITING_APPROVAL} if approval.status == "pending" else {TaskStatus.QUEUED, TaskStatus.RUNNING}
        if run.status not in allowed:
            raise CardStateError("card owner is not actionable")
        expected_scope = self._scope(state)
        if approval.operation_scope != expected_scope or approval.tool_name != "governed_propose_next":
            raise CardStateError("card exact operation scope mismatch")
        return row, run, approval, state

    @staticmethod
    def _scope(state: BoundCardState):
        return {"approval_id": state.approval_id, "generation": str(state.content_generation),
                "proposal_digest": state.proposal_digest, "target_agent_id": state.proposal.target_agent_id}

    @staticmethod
    def _match(state: BoundCardState, generation: int, digest: str):
        if (type(generation) is not int or generation != state.content_generation or
            not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            raise CardStateError("stale or missing card version")
        if not hmac.compare_digest(state.proposal_digest, digest):
            raise CardStateError("stale card proposal digest")

    @staticmethod
    def _duplicate(state: BoundCardState, key: str, digest: str, *, reserve_resolution: bool = False) -> bool:
        for receipt in state.receipts:
            if receipt.request_key == key:
                if not hmac.compare_digest(receipt.request_digest, digest):
                    raise CardStateError("conflicting card request key")
                return True
        if len(state.receipts) >= (63 if reserve_resolution else 64):
            raise CardStateError("card request receipt capacity exceeded")
        return False

    def read(self, group_id: str, approval_id: str) -> BoundCardState:
        with self._admission(group_id, approval_id) as (group, revision):
            with self.store.db.transaction() as conn:
                return self._current(conn, group, approval_id, revision)[3]

    def create(self, group_id: str, run_id: str, *, task_id: str, proposal: CardProposal,
               request_key: str, expires_at: str | None = None) -> BoundCardState:
        request_id(request_key)
        if not isinstance(proposal, CardProposal):
            raise CardStateError("validated card proposal required")
        with self._admission(group_id) as (group, revision):
            proposal = self._target(group, proposal)
            fingerprint = self.repository.digest("create", [run_id, task_id, asdict(proposal), expires_at])
            result = {}
            def guard(conn):
                self.repository.verify(conn, initialize=True)
                run = self._owner(conn, run_id, group_id)
                if run.task_id != task_id:
                    raise CardStateError("card owner task identity mismatch")
                existing = conn.execute("SELECT approval_id FROM studycrew_approvals WHERE request_key=?",
                                        ("card:" + request_key,)).fetchone()
                if existing is not None:
                    _, _, _, state = self._current(conn, group, existing["approval_id"], revision)
                    if not self._duplicate(state, request_key, fingerprint):
                        raise CardStateError("card creation request mismatch")
                    result["state"] = state
                    return
                if conn.execute("SELECT 1 FROM studycrew_run_events WHERE run_id=? AND idempotency_key=?",
                                (run_id, "card:create:" + request_key)).fetchone():
                    raise CardStateError("card event key has no matching approval")
                if run.status not in {TaskStatus.ROUTING, TaskStatus.RUNNING}:
                    raise CardStateError("card owner must be routing or running")
                expiry = self.runtime._normalize_approval_expiry(expires_at)
                state = BoundCardState(approval_id=opaque_id("apr"), owner_task_id=run.task_id,
                    proposal=proposal, proposal_digest=self.repository.digest("proposal", asdict(proposal)),
                    scope_revision=revision, expires_at=expiry,
                    receipts=(CardReceipt(request_key, fingerprint, "created", 1),))
                result["state"] = state
            def mutate(run):
                run.status = TaskStatus.WAITING_APPROVAL
                run.blocking_reason = "User approval required"
            def persist(conn, run, event):
                state = result["state"]
                approval = ApprovalRequest(state.approval_id, run_id, group_id,
                    kind="propose_next", summary="Pending governed proposal", risk=RiskLevel.HIGH,
                    purpose="card_dispatch", tool_name="governed_propose_next",
                    requested_capabilities=("card_dispatch",), operation_scope=self._scope(state),
                    expires_at=state.expires_at)
                self.store.upsert_approval_in_transaction(conn, approval, request_key="card:" + request_key)
                row = conn.execute("SELECT * FROM studycrew_approvals WHERE approval_id=?", (state.approval_id,)).fetchone()
                self.repository.write(conn, row, state)
            _, event, created = self.store.change_run(run_id, event_type="approval_requested",
                public_payload={"status": TaskStatus.WAITING_APPROVAL.value},
                idempotency_key="card:create:" + request_key, mutate=mutate,
                transaction_guard=guard, transaction_hook=persist, publish=False)
        if created:
            self.store._publish(event)
        self.runtime._arm_approval_expiry_watcher()
        return result["state"]

    def amend(self, group_id: str, approval_id: str, *, expected_generation: int,
              expected_digest: str, proposal: CardProposal, request_key: str) -> BoundCardState:
        return self._update(group_id, approval_id, expected_generation=expected_generation,
            expected_digest=expected_digest, proposal=proposal, approved=None, request_key=request_key)

    def resolve(self, group_id: str, approval_id: str, *, expected_generation: int,
                expected_digest: str, approved: bool, request_key: str) -> CardResolution:
        if type(approved) is not bool:
            raise CardStateError("explicit card resolution required")
        return self._update(group_id, approval_id, expected_generation=expected_generation,
            expected_digest=expected_digest, proposal=None, approved=approved, request_key=request_key)

    def _update(self, group_id, approval_id, *, expected_generation, expected_digest,
                proposal, approved, request_key):
        request_id(request_key)
        with self._admission(group_id, approval_id) as (group, revision):
            if approved is None:
                if not isinstance(proposal, CardProposal):
                    raise CardStateError("validated card proposal required")
                proposal = self._target(group, proposal)
            action = "amended" if approved is None else "approved" if approved else "rejected"
            fingerprint = self.repository.digest("mutation", [approval_id, action, expected_generation,
                expected_digest, asdict(proposal) if proposal else None])
            approval = self.store.get_approval(approval_id)
            if approval is None or approval.group_id != group_id:
                raise CardStateError("card not found in group")
            result = {}
            def guard(conn):
                row = conn.execute("SELECT * FROM studycrew_approvals WHERE approval_id=? AND group_id=?",
                                   (approval_id, group_id)).fetchone()
                if row is not None and row["card_invalidated"] == "rejected" and approved is False:
                    receipts = self.repository.terminal_receipts(conn, row)
                    if any(r.request_key == request_key and r.action == "rejected" and
                           hmac.compare_digest(r.request_digest, fingerprint) for r in receipts):
                        result["approval"] = self.store._approval_from_raw(json.loads(row["payload_json"]))
                        return
                    raise CardStateError("card rejection request conflict")
                row, run, current, state = self._current(conn, group, approval_id, revision)
                result.update(state=state, approval=current)
                if self._duplicate(state, request_key, fingerprint, reserve_resolution=approved is None):
                    return
                if conn.execute("SELECT 1 FROM studycrew_run_events WHERE run_id=? AND idempotency_key=?",
                    (run.run_id, "card:update:" + approval_id + ":" + request_key)).fetchone():
                    raise CardStateError("card event key has no matching receipt")
                if current.status != "pending" or run.status is not TaskStatus.WAITING_APPROVAL:
                    raise CardStateError("card is no longer pending")
                self._match(state, expected_generation, expected_digest)
                if conn.execute("SELECT 1 FROM studycrew_permission_grants WHERE approval_id=?", (approval_id,)).fetchone():
                    raise CardStateError("pending card cannot inherit a grant")
                generation = state.content_generation + (1 if approved is None else 0)
                state = replace(state, proposal=proposal or state.proposal,
                    proposal_digest=self.repository.digest("proposal", asdict(proposal or state.proposal)),
                    content_generation=generation, state_version=state.state_version + 1,
                    feedback_epoch=state.feedback_epoch + 1,
                    receipts=(*state.receipts, CardReceipt(request_key, fingerprint, action, generation)))
                result["state"] = state
                result["row"] = row
            def mutate(run):
                if approved is not None:
                    run.status = TaskStatus.QUEUED if approved else TaskStatus.FAILED
                    run.blocking_reason = ""
                    if not approved:
                        run.finished_at = utc_now()
            def persist(conn, run, event):
                state = result["state"]
                self.repository.write(conn, result["row"], state)
                updated = replace(result["approval"], operation_scope=self._scope(state),
                    status="pending" if approved is None else action,
                    resolved_at=None if approved is None else utc_now())
                self.store.resolve_pending_approval_in_transaction(conn, updated)
                result["approval"] = updated
                if approved:
                    grant = PermissionGrant(opaque_id("grant"), approval_id, run.run_id, group_id,
                        run.task_id, ("card_dispatch",), tool_name="governed_propose_next",
                        operation_scope=self._scope(state), purpose="card_dispatch", expires_at=state.expires_at)
                    self.store.insert_grant_in_transaction(conn, grant)
            run, event, changed = self.store.change_run(approval.run_id, event_type="approval_resolved" if approved is not None else "approval_requested",
                public_payload={"approval_id": approval_id} if approved is not None else
                    {"status": TaskStatus.WAITING_APPROVAL.value},
                idempotency_key="card:update:" + approval_id + ":" + request_key,
                transaction_guard=guard, mutate=mutate, transaction_hook=persist, publish=False)
            if approved is None:
                outcome = result["state"]
            else:
                outcome = CardResolution(result["approval"], self.store.grant_for_approval(approval_id), run)
        if changed:
            self.store._publish(event)
            if approved is False:
                self.repository.checkpoint()
        return outcome
