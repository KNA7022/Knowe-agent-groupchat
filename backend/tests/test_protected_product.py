"""Local product composition evidence; transport is synthetic, never paid."""
import asyncio
import json
import uuid
import httpx
import pytest

from backend.engine import ProjectEngine
from backend.hub import Hub
from backend.persist import Store
from backend.task_runtime import TaskRuntimeService
from backend.task_runtime.product import ProtectedProduct
from backend.task_runtime.card_state import CardStateError
from backend.task_runtime.contracts import TaskStatus
from backend.tests.test_protected_children import world, INSTRUCTION, ANSWER, REASONING
from backend.tests.test_protected_worker import provider, Transport


@pytest.fixture(autouse=True)
def trusted_fixture_model(monkeypatch):
    """The inert transport explicitly declares its concrete tool model."""
    from dataclasses import replace
    from backend import commercial_gateway
    original = commercial_gateway.production_model_capability_registry
    def registry_factory():
        registry = original()
        registry._providers['openai-api'] = replace(registry.get('openai-api'), models=('gpt-4o-mini',))
        return registry
    monkeypatch.setattr(commercial_gateway, 'production_model_capability_registry', registry_factory)


def body():
    return {"request_key": uuid.uuid4().hex,
            "proposal": {"target_role": "planning", "instruction": INSTRUCTION}}


@pytest.mark.asyncio
async def test_product_propose_approve_real_worker_snapshot(world, provider):
    root, groups, runtime, key = world
    group = groups.create_group("Product", optional_roles=("planning",), legacy_project_id="project_20261011000001")
    store = Store(root / "events")
    transport = Transport()
    engine = ProjectEngine(Hub(store), group.legacy_project_id, client_factory=transport.client,
        internal_workspace_root=root / group.legacy_project_id, backend_data_root=root,
        task_runtime_service=runtime)
    product = ProtectedProduct(runtime, lambda pid: engine)
    try:
        payload = body()
        first = product.propose(group.group_id, payload)
        assert product.propose(group.group_id, payload)["cards"] == first["cards"]
        assert not transport.requests
        card = first["cards"][0]
        assert card["proposal"]["instruction"] == INSTRUCTION
        with pytest.raises(CardStateError):
            product.resolve(group.group_id, {**card, "generation": 99, "approved": True, "request_key": "stale"})
        assert not transport.requests
        product.resolve(group.group_id, {**card, "approved": True, "request_key": "approve"})
        await asyncio.gather(*engine._worker_turns.values())
        state = product.snapshot(group.group_id)
        assert state["runs"][0]["status"] == "succeeded"
        assert ANSWER in state["messages"][0]["content"]
        assert REASONING not in str(state)
        assert len(transport.requests) == 1
        # Actual populated persistence cannot contain request, output or reasoning.
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".sqlite3", ".jsonl", ".db"}:
                data = path.read_bytes()
                for marker in (INSTRUCTION, ANSWER, REASONING):
                    assert marker.encode("utf-8") not in data, path
        before = len(transport.requests)
        runtime.close()
        reopened = TaskRuntimeService(root, groups, card_master_key=key)
        try:
            state = ProtectedProduct(reopened, lambda pid: pytest.fail("history must not start engine")).snapshot(group.group_id)
            assert ANSWER in state["messages"][0]["content"]
            assert len(transport.requests) == before
        finally:
            reopened.close()
    finally:
        await engine.stop()


def test_missing_key_no_owner_or_plaintext_and_reject(world):
    root, groups, runtime, _key = world
    group = groups.create_group("No key", optional_roles=("planning",))
    other = TaskRuntimeService(root / "nokey", groups)
    try:
        product = ProtectedProduct(other, lambda pid: pytest.fail("no execution"))
        with pytest.raises(CardStateError):
            product.propose(group.group_id, body())
        assert not other.store.list_runs()
    finally:
        other.close()
    product = ProtectedProduct(runtime, lambda pid: pytest.fail("rejection cannot execute"))
    card = product.propose(group.group_id, body())["cards"][0]
    product.resolve(group.group_id, {**card, "approved": False, "request_key": "reject"})
    assert runtime.store.list_runs(group_id=group.group_id)[0].status is TaskStatus.FAILED


def test_pending_reopen_cancel_and_group_isolation(world):
    root, groups, runtime, key = world
    group = groups.create_group("Pending", optional_roles=("planning",))
    other = groups.create_group("Other", optional_roles=("planning",))
    product = ProtectedProduct(runtime, lambda pid: pytest.fail("pending/history cannot execute"))
    card = product.propose(group.group_id, body())["cards"][0]
    assert product.snapshot(other.group_id)["cards"] == []
    with pytest.raises(CardStateError):
        product.resolve(other.group_id, {**card, "approved": True, "request_key": "crossgroup"})
    runtime.close()
    reopened = TaskRuntimeService(root, groups, card_master_key=key)
    try:
        product = ProtectedProduct(reopened, lambda pid: pytest.fail("restart cannot execute"))
        state = product.snapshot(group.group_id)
        assert state["cards"][0]["proposal"]["instruction"] == INSTRUCTION
        run_id = state["runs"][0]["run_id"]
        reopened.cancel(group.group_id, run_id, request_key="cancel")
        assert product.snapshot(group.group_id)["cards"] == []
        assert product.snapshot(group.group_id)["runs"][0]["status"] == "cancelled"
        with pytest.raises(CardStateError):
            product.resolve(group.group_id, {**card, "approved": True, "request_key": "late"})
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_citation', [False, True])
async def test_source_native_read_actual_gateway_and_exact_citation(world, provider, monkeypatch, bad_citation):
    from dataclasses import replace
    from backend import commercial_gateway
    original_registry = commercial_gateway.production_model_capability_registry
    def fixture_registry():
        registry = original_registry()
        registry._providers['openai-api'] = replace(registry.get('openai-api'), models=('gpt-4o-mini',))
        return registry
    monkeypatch.setattr(commercial_gateway, 'production_model_capability_registry', fixture_registry)
    from backend.local_knowledge import LocalKnowledgeService, LocalKnowledgeCardSourceGuard
    from backend.knowe_rag.parsers import parse_document_bytes
    from backend.knowe_rag.chunks import build_chunks, CHILD
    root, groups, runtime, key = world
    group = groups.create_group("Sources", optional_roles=("knowledge",), legacy_project_id="project_20261011000004")
    knowledge = LocalKnowledgeService(root, groups, master_key=key)
    sid = "src_" + "a" * 32
    chunks = build_chunks(sid, parse_document_bytes(b'synthetic evidence for learning', '.txt'), content_hasher=knowledge.store.content_hasher)
    chunk_id = next(c.chunk_id for c in chunks if c.level == CHILD)
    knowledge.store.write_document(sid, chunks)
    knowledge.store.write_source(sid, {"filename": "fixture.txt", "course_id": None, "version": 1,
        "status": "indexed", "chunk_count": len(chunks)}, b'synthetic evidence for learning')
    groups.link_source(group.group_id, sid)
    runtime.cards._source_validator = LocalKnowledgeCardSourceGuard(knowledge)
    requests = []
    async def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            delta = {"tool_calls": [{"index": 0, "id": "call_read", "type": "function", "function": {
                "name": "protected_source_search", "arguments": '{"query":"synthetic evidence","limit":1}'}}]}
        else:
            assert any(item.get('role') == 'tool' and 'synthetic evidence' in item.get('content', '')
                       for item in requests[-1]['messages'])
            citation = 'fabricated-chunk' if bad_citation else chunk_id
            delta = {"content": f"Synthetic evidence supports learning [[{citation}]]"}
        wire = "data: " + json.dumps({"choices": [{"index": 0, "delta": delta}]}) + "\n\ndata: [DONE]\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=wire.encode())
    engine = ProjectEngine(Hub(Store(root / 'events')), group.legacy_project_id,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        internal_workspace_root=root / group.legacy_project_id, backend_data_root=root, task_runtime_service=runtime)
    try:
        product = ProtectedProduct(runtime, lambda pid: engine)
        card = product.propose(group.group_id, {"request_key": "source-product", "proposal": {
            "target_role": "knowledge", "instruction": "Explain the linked source"}})["cards"][0]
        product.resolve(group.group_id, {**card, "approved": True, "request_key": "source-approve"})
        await asyncio.gather(*engine._worker_turns.values())
        state = product.snapshot(group.group_id)
        assert state['runs'][0]['status'] == ('failed' if bad_citation else 'succeeded')
        if bad_citation:
            assert not state['messages']
        else:
            assert f'[[{chunk_id}]]' in state['messages'][0]['content']
        assert len(requests) == 2
        assert runtime.store.list_runs(group_id=group.group_id)[0].budget_usage.tool_calls == 1
        if not bad_citation:
            metadata = {"filename": "fixture.txt", "course_id": None, "version": 2,
                        "status": "indexed", "chunk_count": len(chunks)}
            knowledge.store.update_source_metadata(sid, metadata)
            assert product.snapshot(group.group_id)['messages'] == []
    finally:
        await engine.stop()
        knowledge.store.close()
