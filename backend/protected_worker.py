"""Explicit instruction-only Engine attempt, with per-lineage dependencies.

This is a backend execution seam, not a coordinator producer or a storage router.
The approved card supplies identity; storage selection never supplies execution rights.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from .commercial_gateway import LegacyToolAuthority
from .knowe_harness.completion import SQLiteCompletionStore, CompletionAwareTaskRunRepository
from .knowe_harness.projections import CompletionProjector
from .knowe_harness.store import TaskEnvelopeStore
from .protected_children import ProtectedChildren
from .protected_events import ProtectedEvents
from .protected_storage import ProtectedAccess, ProtectedStorageError
from .runtime import RuntimeEventType
from .task_runtime.contracts import TaskStatus, EvidenceRef, FailureType


class ProtectedWorker:
    def __init__(self, engine, envelope, access):
        if (type(access) is not ProtectedAccess or access.read_only
                or access.registry.runtime is not engine._task_runtime_service):
            raise ProtectedStorageError("same-runtime live Worker selection required")
        self.engine, self.access = engine, access
        self.service = access.registry.runtime
        self.closed = False
        self.task = None
        self.agent = None
        self.fault = None
        self._active_published = False
        self.binding = access.identity
        if self.binding["project_id"] != engine.project_id:
            raise ProtectedStorageError("protected Engine project mismatch")
        self.authority()
        self._owner_identity = (access.group_id, self.binding["owner_run_id"],
            self.binding["owner_task_id"], access.task_id)
        children = ProtectedChildren.existing(self.service) or ProtectedChildren(access.registry)
        # Validate the selected canonical destination before enrollment or any body claim.
        _, envelope_path = children._locations(self.binding["project_id"])
        if engine.internal_workspace.resolve() != envelope_path.parent.parent.resolve():
            raise ProtectedStorageError("canonical protected Engine workspace required")
        locations = children.register(access)
        root = Path(locations["envelopes"]).parent.parent
        self.envelopes = TaskEnvelopeStore(root, protected=access)
        self.envelopes.commit(envelope)
        self.store = SQLiteCompletionStore(locations["completion"], protected=access)
        try:
            self.repository = CompletionAwareTaskRunRepository(locations["completion"], self.store)
            if self.repository.get(envelope.task_id) is not None:
                raise ProtectedStorageError("protected new-Worker resume is unavailable")
        except BaseException:
            if hasattr(self, "repository"):
                self.repository.close()
            self.store.close()
            raise
        self.projector = CompletionProjector(self.store, engine)
        # A previously opened legacy store must now use the explicit classifier too.
        legacy = engine._completion_store_instance
        if legacy is not None:
            legacy.legacy_lineages = access.registry

    def authority(self):
        if self.closed or self.engine._stopping or self.engine._stopped:
            raise ProtectedStorageError("protected Worker closed")
        with self.access.guard() as binding:
            owner = self.service.store.get_run(binding["owner_run_id"])
            approval = self.service.store.get_approval(binding["approval_id"])
            if (owner is None or owner.status is not TaskStatus.RUNNING
                    or owner.group_id != self.access.group_id
                    or owner.task_id != binding["owner_task_id"]
                    or approval is None or approval.status != "approved"):
                raise ProtectedStorageError("running approved protected Worker authority required")
            return LegacyToolAuthority(group_id=owner.group_id, run_id=owner.run_id,
                task_id=owner.task_id, runtime=self.service)

    def checkpoint(self, phase):
        if self.fault is not None:
            self.fault(phase)

    def started(self, run):
        self.authority()
        self.checkpoint("before_started")
        self.repository.create(run)
        self.checkpoint("after_started")

    def delivery_identity(self, run):
        self.authority()
        digest = self.store.protection.digest("worker-delivery", [
            run.envelope.task_id, run.envelope.attempt_id, run.run_id])
        return "p1_delivery_" + digest[:32], "p1_deliverykey_" + digest[32:]

    async def phase(self, event):
        if event.type not in {RuntimeEventType.STATE_CHANGED, RuntimeEventType.MODEL_CALLED}:
            return
        self.authority()
        self._active_published = True
        self.engine._record_open_activity(self.binding["worker_id"], self.access.task_id, self.engine.project_id)
        await self.engine.hub.emit(self.engine.project_id, {
            "type": "agent_active", "agent_id": self.binding["worker_id"],
            "reason": "protected_worker_running",
            "scope_id": self.access.task_id, "channel_id": self.engine.project_id,
        })

    async def idle(self):
        if not self._active_published:
            return
        self._active_published = False
        self.engine._release_open_activity(self.binding["worker_id"], self.access.task_id, self.engine.project_id)
        # This closes a public activity phase, not a private-read permission or outcome.
        # A closed/deleted Store may refuse publication; the next snapshot is authoritative.
        try:
            await self.engine.hub.emit(self.engine.project_id, {
                "type": "agent_idle", "agent_id": self.binding["worker_id"],
                "scope_id": self.access.task_id, "channel_id": self.engine.project_id,
            })
        except Exception:
            pass

    async def _watch(self, worker_task):
        while not worker_task.done():
            await asyncio.sleep(0.05)
            try:
                self.authority()
            except Exception:
                worker_task.cancel()
                return

    def finalize(self, status, completion=None):
        self.authority()
        self.checkpoint("before_finalize")
        if status in {"WAITING", "BLOCKED"}:
            self.service.pause(self.access.group_id, self.binding["owner_run_id"],
                request_key="protected-wait:" + self.access.task_id)
        else:
            outcome = {"SUCCEEDED": TaskStatus.SUCCEEDED, "PARTIAL": TaskStatus.PARTIAL,
                "CANCELLED": TaskStatus.CANCELLED}.get(status, TaskStatus.FAILED)
            evidence = (() if completion is None else (EvidenceRef(
                "completion", completion.completion_id, str(completion.version), completion.completion_id),))
            self.service.finalize_trusted_execution(self.access.group_id,
                self.binding["owner_run_id"], execution_key=self.access.task_id,
                outcome=outcome, evidence=evidence,
                summary=("Protected task execution failed" if outcome is TaskStatus.FAILED
                         else "Protected task execution completed"),
                failure_type=FailureType.EXECUTOR_FAILURE if outcome is TaskStatus.FAILED else None)
        self.checkpoint("after_finalize")

    def fail_closed(self, error):
        # Finishing the originally admitted execution does not require private-body
        # access. The existing runtime guards/Verifier still own the state change.
        group_id, owner_id, task_id, execution_key = self._owner_identity
        with self.service._lock:
            if self.service._closed or self.engine._stopping or self.engine._stopped:
                return
            owner = self.service._owned_run(owner_id, group_id)
            if owner.task_id != task_id or owner.status is not TaskStatus.RUNNING:
                return
            self.service.finalize_trusted_execution(group_id, owner_id,
                execution_key=execution_key, outcome=TaskStatus.FAILED,
                summary="Protected task execution failed",
                failure_type=(FailureType.PERMISSION_DENIED if isinstance(error, ProtectedStorageError)
                    else FailureType.EXECUTOR_FAILURE))

    async def run(self, envelope):
        if self.task is not None:
            raise ProtectedStorageError("protected attempt already started")
        self.task = asyncio.current_task()
        watcher = None
        try:
            self.authority()
            from . import runtime_settings
            from .worker_gateway_runtime import WorkerContextError
            binding = self.engine._effective_provider_binding(envelope.worker_id)
            try:
                runtime_settings.resolve_provider_credential(binding)
            except Exception:
                blocked = self.engine._worker_runtime_factory.blocked_run(self.engine, envelope,
                    WorkerContextError("provider_unavailable", "provider_binding",
                        "A current provider credential is required before execution."))
                self.started(blocked)
                run, record = blocked, None
            else:
                self.agent = self.engine._new_agent(envelope.worker_id, self.binding["worker_role"],
                    0.0, authority_provider=self.authority)
                worker = self.engine._worker_runtime_factory.create(
                    self.engine, envelope.worker_id, envelope, self.agent, protected=self)
                worker.events.add_listener(self.phase)
                watcher = asyncio.create_task(self._watch(self.task))
                result = await worker.run(envelope)
                run, record = result
            self.authority()
            self.checkpoint("before_completion")
            if record is None:
                completion = self.store.commit_run(run)
            else:
                completion, _ = self.store.commit_success(run, record)
            self.checkpoint("after_completion")
            authority = ProtectedEvents.existing(self.service) or ProtectedEvents(self.access.registry)
            authority.attach(self.access, self.store.db.path)
            if self.engine.hub.protected_events is None:
                self.engine.hub.bind_protected_events(authority)
            elif self.engine.hub.protected_events is not authority:
                raise ProtectedStorageError("protected publication runtime mismatch")
            await self.projector.drain(completion_id=completion.completion_id, raise_errors=True)
            self.checkpoint("after_ack")
            self.finalize(completion.status.value, completion)
        except Exception as error:
            # Content, provider exceptions and reasoning never enter the public bridge.
            # Deletion/quarantine/closed runtime still deny; paused/terminal owners stay.
            try:
                self.fail_closed(error)
            except Exception:
                pass
            raise ProtectedStorageError("protected Worker execution failed") from None
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            self.engine._close_task_envelope(envelope.worker_id)
            try:
                await self.close()
            finally:
                await self.idle()
                self.task = None
                self.engine._protected_workers.pop(envelope.task_id, None)

    async def close(self):
        self.closed = True
        if self.task is not None and self.task is not asyncio.current_task() and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        try:
            if self.agent is not None:
                await self.agent._client.aclose()
        finally:
            self.agent = None
            self.projector.close()
            self.repository.close()
            self.store.close()
