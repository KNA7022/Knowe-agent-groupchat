"""Caller-budgeted live protected product probe; never invokes itself or logs credentials."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid
import traceback


async def run_product_probe(data_root, *, binding: dict, credential: str, client_factory) -> dict:
    """Run production composition with caller-owned HTTP and an in-memory secret lease.

    The caller must supply a fresh isolated directory and enforce its network budget
    in client_factory. No mocked provider, test fixture, or persisted API key is used.
    """
    from backend import config, runtime_settings
    from backend.commercial_gateway import SecretLeaseBroker, derive_provider_credential_purpose
    from backend.engine import ProjectEngine
    from backend.groups import GroupRepository
    from backend.hub import Hub
    from backend.persist import Store
    from backend.knowe_rag.crypto import MasterKey
    from backend.knowe_rag.parsers import parse_document_bytes
    from backend.knowe_rag.chunks import build_chunks, CHILD
    from backend.local_knowledge import LocalKnowledgeService, LocalKnowledgeCardSourceGuard
    from backend.task_runtime import TaskRuntimeService
    from backend.task_runtime.product import ProtectedProduct
    from backend.task_runtime.group_content import GroupContent
    from backend.protected_worker import ProtectedWorker
    from backend.knowe_adapters.model_adapter import ProviderModelAdapter

    root = Path(data_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("product probe requires a fresh isolated data directory")
    previous = (config.CONFIG, runtime_settings._state, runtime_settings._loaded,
                runtime_settings._secret_lease_broker)
    broker = SecretLeaseBroker()
    runtime = knowledge = engine = reopened = None
    diagnostics = []
    original_fail_closed = ProtectedWorker.fail_closed
    def diagnostic_fail_closed(worker, error):
        diagnostics.append({"error_type": type(error).__name__, "frames": [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(error.__traceback__)]})
        return original_fail_closed(worker, error)
    ProtectedWorker.fail_closed = diagnostic_fail_closed
    original_stream = ProviderModelAdapter._stream_step
    async def diagnostic_stream(adapter, *args, **kwargs):
        try:
            return await original_stream(adapter, *args, **kwargs)
        except Exception as error:
            diagnostics.append({"stage": "provider_stream", "error_type": type(error).__name__, "frames": [
                {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(error.__traceback__)]})
            raise
    ProviderModelAdapter._stream_step = diagnostic_stream
    try:
        config.CONFIG = replace(config.CONFIG, data_dir=str(root))
        runtime_settings._state = runtime_settings._default_state()
        runtime_settings._loaded = True
        runtime_settings._secret_lease_broker = broker
        selected = dict(binding)
        selected.update(secret_ref="secret_" + uuid.uuid4().hex, credential_slot="main",
                        credential_kind="provider_credential", credential_audience="provider_gateway")
        selected["credential_purpose"] = derive_provider_credential_purpose(
            slot="main", provider_id=selected["provider"], endpoint=selected["base_url"],
            transport=selected["transport"])
        # Remove legacy inline fields before saving the reference-only binding.
        for name in ("api_key", "key", "credential", "secret", "token"):
            selected.pop(name, None)
        runtime_settings.apply({"main_model": selected})
        broker.provision(lease_id="lease_" + uuid.uuid4().hex, secret_ref=selected["secret_ref"],
            slot="main", kind="provider_credential", purpose=selected["credential_purpose"],
            audience="provider_gateway", provider_id=selected["provider"],
            endpoint=selected["base_url"], transport=selected["transport"], value=credential,
            expires_at=(datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat())
        key = MasterKey.load_or_create(root / "local_knowledge" / "master-key.dpapi")
        groups = GroupRepository(root)
        group = groups.create_group("Live protected evidence", optional_roles=("knowledge",),
                                    legacy_project_id="project_20261011000991")
        runtime = TaskRuntimeService(root, groups, card_master_key=key)
        knowledge = LocalKnowledgeService(root, groups, master_key=key)
        sid = "src_" + uuid.uuid4().hex
        source = b"Synthetic evidence: spaced review means revisiting material at increasing intervals."
        chunks = build_chunks(sid, parse_document_bytes(source, ".txt"),
                              content_hasher=knowledge.store.content_hasher)
        ids = {chunk.chunk_id for chunk in chunks if chunk.level == CHILD}
        knowledge.store.write_document(sid, chunks)
        knowledge.store.write_source(sid, {"filename": "synthetic-evidence.txt", "course_id": None,
            "version": 1, "status": "indexed", "chunk_count": len(chunks)}, source)
        groups.link_source(group.group_id, sid)
        runtime.cards._source_validator = LocalKnowledgeCardSourceGuard(knowledge)
        hub = Hub(Store(root / "events"))
        hub.group_content = GroupContent(runtime)
        engine = ProjectEngine(hub, group.legacy_project_id, client_factory=client_factory,
            internal_workspace_root=root / group.legacy_project_id, backend_data_root=root,
            task_runtime_service=runtime)
        product = ProtectedProduct(runtime, lambda _pid: engine)
        proposed = product.propose(group.group_id, {"request_key": "live-propose", "proposal": {
            "target_role": "knowledge", "instruction":
            "Call protected_source_search with query 'Synthetic evidence' and limit 1. "
            "Then explain spaced review in one sentence, citing the returned exact chunk_id "
            "as [[chunk_id]]. Do not use any other tool or request more information."}})
        assert runtime.store.list_runs(group_id=group.group_id)[0].budget_usage.model_turns == 0
        card = proposed["cards"][0]
        product.resolve(group.group_id, {**card, "approved": True, "request_key": "live-approve"})
        await asyncio.wait_for(asyncio.gather(*engine._worker_turns.values()), timeout=150)
        completed = product.snapshot(group.group_id)
        run = runtime.store.list_runs(group_id=group.group_id)[0]
        if completed["runs"][0]["status"] != "succeeded":
            return {"ok": False, "stage": "worker", "status": completed["runs"][0]["status"],
                    "model_calls": run.budget_usage.model_turns, "tool_calls": run.budget_usage.tool_calls,
                    "diagnostics": diagnostics, "snapshots": {"proposed": proposed, "completed": completed}}
        assert completed["runs"][0]["status"] == "succeeded", "protected Worker did not succeed"
        assert run.budget_usage.tool_calls >= 1, "no actual source tool result"
        assert 1 <= run.budget_usage.model_turns <= 3, "live model turn budget exceeded"
        content = completed["messages"][0]["content"]
        assert any(f"[[{cid}]]" in content for cid in ids), "no verified source citation"
        await engine.stop()
        engine = None
        runtime.close()
        runtime = None
        reopened = TaskRuntimeService(root, GroupRepository(root), card_master_key=key)
        reopened.cards._source_validator = LocalKnowledgeCardSourceGuard(knowledge)
        def forbid_execution(_pid):
            raise AssertionError("reopened history must not start a Worker")
        restored = ProtectedProduct(reopened, forbid_execution).snapshot(group.group_id)
        assert restored["messages"][0]["content"] == content, "result did not survive reopen"
        assert restored["runs"][0]["status"] == "succeeded"
        return {"ok": True, "status": "succeeded", "approved_generation": card["generation"],
                "model_calls": run.budget_usage.model_turns, "tool_calls": run.budget_usage.tool_calls,
                "citation_verified": True, "reopen_verified": True,
                "snapshots": {"proposed": proposed, "completed": completed, "reopened": restored}}
    finally:
        if engine is not None:
            await engine.stop()
        if runtime is not None:
            runtime.close()
        if reopened is not None:
            reopened.close()
        if knowledge is not None:
            knowledge.store.close()
        broker.close()
        ProtectedWorker.fail_closed = original_fail_closed
        ProviderModelAdapter._stream_step = original_stream
        config.CONFIG, runtime_settings._state, runtime_settings._loaded, runtime_settings._secret_lease_broker = previous
