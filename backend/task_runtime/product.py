"""Authenticated HTTP product composition for the accepted protected Worker.

Only source-free instruction tasks are supported. Request bodies remain transient;
the first durable body is the existing encrypted, version-bound approval card.
"""
from dataclasses import asdict

from backend.protected_storage import ProtectedLineages, ProtectedStorageError
from backend.protected_events import ProtectedEvents, TABLE
from backend.runtime import TaskEnvelope
from .card_state import CardProposal, CardStateError, request_id
from .contracts import TaskStatus, RiskLevel


class ProtectedProduct:
    def __init__(self, runtime, engine_for):
        self.runtime = runtime
        self.engine_for = engine_for

    @staticmethod
    def card_view(card, status="pending"):
        return {"approval_id": card.approval_id, "generation": card.content_generation,
                "digest": card.proposal_digest, "expires_at": card.expires_at,
                "proposal": asdict(card.proposal), "status": status}

    def propose(self, group_id, payload):
        key = payload.get("request_key")
        request_id(key)
        card_key = "product:" + self.runtime.store.card_states.digest("product-request", [group_id, key])[:48]
        proposal = CardProposal.from_payload(payload.get("proposal"))
        # Validate encryption and exact roster/scope BEFORE a public owner exists.
        with self.runtime.cards._admission(group_id) as (group, _revision):
            proposal = self.runtime.cards._target(group, proposal)
            with self.runtime.store.db.transaction() as conn:
                self.runtime.store.card_states.verify(conn, initialize=True)
                row = conn.execute("SELECT approval_id FROM studycrew_approvals WHERE group_id=? AND request_key=?",
                                   (group_id, "card:" + card_key)).fetchone()
            if row:
                card = self.runtime.cards.read(group_id, row["approval_id"])
                if asdict(card.proposal) != asdict(proposal):
                    raise CardStateError("Conflicting proposal request key")
                return self.snapshot(group_id)
            if any(not run.status.terminal for run in self.runtime.store.list_runs(group_id=group_id)):
                raise CardStateError("Group is busy; finish or cancel its current task first")
            if sum(run.status in {TaskStatus.RUNNING, TaskStatus.ROUTING, TaskStatus.VERIFYING}
                   for run in self.runtime.store.list_runs()) >= self.runtime.GLOBAL_PRIMARY_LIMIT:
                raise CardStateError("Execution capacity is busy; retry shortly")
            run = self.runtime.submit(group_id, title="Protected instruction task",
                risk=RiskLevel.HIGH, request_key=card_key)
            self.runtime.schedule()
            run = self.runtime.store.get_run(run.run_id)
            if run.status not in {TaskStatus.RUNNING, TaskStatus.ROUTING}:
                raise CardStateError("Group is busy; retry after its current task completes")
            self.runtime.cards.create(group_id, run.run_id, task_id=run.task_id,
                proposal=proposal, request_key=card_key)
        return self.snapshot(group_id)

    def resolve(self, group_id, payload):
        key = payload.get("request_key")
        request_id(key)
        aid = payload.get("approval_id")
        generation, digest = payload.get("generation"), payload.get("digest")
        approved = payload.get("approved")
        with self.runtime._lock:
            card = self.runtime.cards.read(group_id, aid)
            self.runtime.cards._match(card, generation, digest)
            registry = ProtectedLineages(self.runtime)
            access = registry.register(group_id, aid, generation=generation, digest=digest) if approved is True else None
            resolution = self.runtime.cards.resolve(group_id, aid, expected_generation=generation,
                expected_digest=digest, approved=approved, request_key=key)
            if approved is True:
                self.runtime.schedule()
                owner = self.runtime.store.get_run(resolution.run.run_id)
                if owner.status is TaskStatus.RUNNING:
                    binding = access.identity
                    try:
                        engine = self.engine_for(binding["project_id"])
                    except Exception:
                        self.runtime.pause(group_id, owner.run_id, request_key="product-engine-unavailable:" + aid)
                        raise ProtectedStorageError("Protected task workspace unavailable; create a new proposal after fixing it") from None
                    # A duplicate approval cannot allocate another attempt.
                    if access.task_id not in engine._protected_workers:
                        from backend.protected_children import ProtectedChildren
                        children = ProtectedChildren.existing(self.runtime)
                        if children is not None and children._read(access.task_id) is not None:
                            raise ProtectedStorageError("Interrupted protected attempt requires a new proposal")
                        envelope = TaskEnvelope(task_id=access.task_id, attempt_id=binding["attempt_id"],
                            project_id=binding["project_id"], worker_id=binding["worker_id"],
                            goal=card.proposal.instruction)
                        engine.inject_task_envelope(envelope, protected=access)
                        engine.start_committed_workers()
        return self.snapshot(group_id)

    def snapshot(self, group_id):
        group = self.runtime.groups.get(group_id)
        self.runtime._assert_group_mutable(group_id)
        cards = []
        for approval in self.runtime.store.pending_approvals(group_id):
            if approval.purpose == "card_dispatch":
                cards.append(self.card_view(self.runtime.cards.read(group_id, approval.approval_id)))
        with self.runtime.store.db.transaction(immediate=False) as conn:
            rows = conn.execute("SELECT approval_id FROM studycrew_approvals WHERE group_id=? AND status='approved' AND card_state_ciphertext IS NOT NULL",
                                (group_id,)).fetchall()
        for row in rows:
            approval = self.runtime.store.get_approval(row["approval_id"])
            owner = self.runtime.store.get_run(approval.run_id)
            if approval.purpose == "card_dispatch" and owner.status in {TaskStatus.QUEUED, TaskStatus.RUNNING}:
                cards.append(self.card_view(self.runtime.cards.read(group_id, approval.approval_id), "approved"))
        runs = [run.to_public_dict() for run in self.runtime.store.list_runs(group_id=group_id)
                if run.title == "Protected instruction task"]
        messages = []
        events = ProtectedEvents.existing(self.runtime)
        if events is not None:
            db = events._stores.get(group.legacy_project_id or group_id)
            if db is not None:
                with db.transaction(immediate=False) as conn:
                    rows = conn.execute(f"SELECT * FROM {TABLE} WHERE group_id=? ORDER BY seq DESC LIMIT 50",
                                        (group_id,)).fetchall()
                for row in reversed(rows):
                    # History hydration itself never obtains execution permission.
                    try:
                        message = events.hydrate(events._reference(row), history=True)
                    except ProtectedStorageError:
                        continue
                    messages.append({k: message[k] for k in ("event_id", "agent_id", "content", "status", "task_id")})
        return {"cards": cards, "runs": runs[-50:], "messages": messages,
                "scope": "version-bound Group instruction and read-only source tasks", "model_execution": "requires provider credential"}
