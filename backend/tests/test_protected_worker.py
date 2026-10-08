from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import gzip

import httpx
import pytest

from backend import runtime_settings
from backend.config import CONFIG
from backend.engine import ProjectEngine
from backend.commercial_gateway import SecretLeaseBroker, derive_provider_credential_purpose
from backend.hub import Hub
from backend.persist import Store
from backend.protected_storage import ProtectedStorageError
from backend.knowe_harness.completion import SQLiteCompletionStore, ProjectionKind
from backend.task_runtime.contracts import TaskStatus, FailureType
from backend.task_runtime import TaskRuntimeService
from backend.protected_events import ProtectedEvents, REF
from backend.runtime import TaskEnvelope, TaskRun, TaskState
from backend.tests.test_protected_children import world, enrolled, INSTRUCTION, ANSWER, REASONING
from backend.tests.test_protected_events import admitted, drain


WAIT = "等待用户-WAIT-134795"
FAILURE = "私有错误-FAILURE-597433"


@pytest.fixture
def provider(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.config.CONFIG", replace(CONFIG, data_dir=str(tmp_path)))
    monkeypatch.setattr(runtime_settings, "_state", runtime_settings._default_state())
    monkeypatch.setattr(runtime_settings, "_loaded", True)
    broker = SecretLeaseBroker()
    monkeypatch.setattr(runtime_settings, "_secret_lease_broker", broker)
    binding = {"provider": "openai-api", "model": "gpt-4o-mini",
        "base_url": "https://api.openai.com/v1", "transport": "openai_chat",
        "secret_ref": "secret_" + "a" * 32, "credential_slot": "main",
        "credential_kind": "provider_credential", "credential_audience": "provider_gateway"}
    binding["credential_purpose"] = derive_provider_credential_purpose(slot="main",
        provider_id=binding["provider"], endpoint=binding["base_url"], transport="openai_chat")
    runtime_settings.apply({"main_model": binding})
    broker.provision(lease_id="lease_" + "b" * 32, secret_ref=binding["secret_ref"],
        slot="main", kind="provider_credential", purpose=binding["credential_purpose"],
        audience="provider_gateway", provider_id=binding["provider"],
        endpoint=binding["base_url"], transport="openai_chat", value="synthetic-test-secret",
        expires_at=(datetime.now(timezone.utc) + timedelta(seconds=250)).isoformat())
    yield broker, binding
    broker.close()


class Transport:
    def __init__(self, mode="success", control=None):
        self.mode, self.control = mode, control
        self.requests = []
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.before_send = None

    async def handle(self, request):
        if self.before_send is not None:
            self.before_send()
        self.requests.append(json.loads(request.content))
        self.entered.set()
        if self.mode == "slow":
            await self.release.wait()
        if self.mode == "failure":
            return httpx.Response(400, json={"error": {"message": FAILURE}})
        delta = {"content": ANSWER}
        if self.mode == "prose":
            delta = {"content": '{"protected_wait_for_user":{"question":"not a native call"}}'}
        if self.mode == "wait":
            delta = {"tool_calls": self.control or [{"index": 0, "id": "call_wait",
                "type": "function", "function": {"name": "protected_wait_for_user",
                "arguments": json.dumps({"question": WAIT}, ensure_ascii=False)}}]}
        chunks = [
            {"choices": [{"index": 0, "delta": {"reasoning_content": REASONING}}]},
            {"choices": [{"index": 0, "delta": delta}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}},
        ]
        data = "".join("data: " + json.dumps(row, ensure_ascii=False) + "\n\n" for row in chunks)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
            content=(data + "data: [DONE]\n\n").encode())

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


def compose(world, transport, *, group=None):
    item = enrolled(world, group=group, enroll=False)
    _, access, envelope, owner, group, _ = item
    store = Store(world[0] / "events")
    hub = Hub(store)
    engine = ProjectEngine(hub, envelope.project_id, client_factory=transport.client,
        internal_workspace_root=world[0] / envelope.project_id, backend_data_root=world[0],
        task_runtime_service=world[2])
    engine.inject_task_envelope(envelope, protected=access)
    return engine, store, access, envelope, owner, group


def assert_idle_pair(engine, envelope):
    # Active is an ephemeral live phase, intentionally absent from full disk replay.
    frames = engine.hub.get_or_create(envelope.project_id).ring.events()
    phases = [frame for frame in frames if frame.get("type") in {"agent_active", "agent_idle"}]
    assert phases and phases[0]["type"] == "agent_active" and phases[-1]["type"] == "agent_idle"
    assert len([frame for frame in phases if frame["type"] == "agent_idle"]) == 1
    for frame in phases:
        assert frame["agent_id"] == envelope.worker_id
        assert frame["scope_id"] == envelope.task_id and frame["channel_id"] == envelope.project_id
        assert "content" not in frame and "error" not in frame
    assert engine.open_activity_snapshot() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("success", TaskStatus.SUCCEEDED), ("wait", TaskStatus.PAUSED),
    ("failure", TaskStatus.FAILED)])
async def test_actual_worker_gateway_completion_projector(world, provider, monkeypatch, caplog, mode, expected):
    transport = Transport(mode)
    engine, store, access, envelope, owner, group = compose(world, transport)
    selected = engine._protected_workers[envelope.task_id]
    server, client = await admitted(world, engine.hub, monkeypatch)
    def assert_started():
        assert selected.store.active_attempts()
        assert selected.agent.agent_id == envelope.worker_id
        assert access.identity["worker_run_id"]
    transport.before_send = assert_started
    try:
        await engine._run_worker(envelope, envelope.worker_id)
        assert world[2].store.get_run(owner.run_id).status is expected
        assert len(transport.requests) == 1
        assert [r["function"]["name"] for r in transport.requests[0]["tools"]] == ["protected_wait_for_user"]
        await drain(client)
        await client.release_live_replay_fence()
        if mode != "failure":
            reader = (access.registry.paused_history(group.group_id, envelope.task_id)
                      if mode == "wait" else access)
            with SQLiteCompletionStore(selected.store.db.path, protected=reader) as reopened:
                rows = reopened.pending_outbox()
                assert len(rows) == 1 and rows[0].projection_kind is ProjectionKind.COORDINATOR
                assert reopened.projection_receipt(rows[0].outbox_id) is None
                run = reopened.task_result(task_id=envelope.task_id)
                assert run.worker_id == envelope.worker_id
            assert any(frame.get("content") == (WAIT if mode == "wait" else ANSWER) for frame in client.ws.frames)
        else:
            with pytest.raises(ProtectedStorageError):
                access.identity
        assert REASONING not in json.dumps(client.ws.frames, ensure_ascii=False)
        assert FAILURE not in json.dumps(client.ws.frames, ensure_ascii=False)
        assert envelope.worker_id not in engine._agents
        assert not engine._completion_notifications_queued
        assert_idle_pair(engine, envelope)
        assert not list(engine.internal_workspace.rglob("completion-notifications/*.json"))
        store.flush()
        store.rotate_and_compress(envelope.project_id)
        for path in world[0].rglob("*"):
            if path.is_file():
                raw = path.read_bytes()
                if path.suffix == ".gz":
                    raw += gzip.decompress(raw)
                for marker in (INSTRUCTION, ANSWER, WAIT, FAILURE, REASONING):
                    assert marker.encode() not in raw, path.name
        from backend.knowe_storage._sqlite import json_loads
        for path in world[0].rglob("*.sqlite3"):
            _, rows = database_snapshot(path)
            for records in rows.values():
                for record in records:
                    for value in record:
                        if isinstance(value, str):
                            try:
                                decoded = repr(json_loads(value))
                            except Exception:
                                decoded = value
                            for marker in (INSTRUCTION, ANSWER, WAIT, FAILURE, REASONING):
                                assert marker not in decoded
        for marker in (INSTRUCTION, ANSWER, WAIT, FAILURE, REASONING):
            assert marker not in caplog.text
            assert marker not in repr(engine.hub._idempotent_events)
    finally:
        await engine.stop()
        store.close()


@pytest.mark.asyncio
async def test_preflight_missing_lease_is_useful_block(world, provider, monkeypatch):
    transport = Transport()
    engine, store, access, envelope, owner, group = compose(world, transport)
    provider[0].revoke_secret(provider[1]["secret_ref"])
    server, client = await admitted(world, engine.hub, monkeypatch)
    try:
        await engine._run_worker(envelope, envelope.worker_id)
        assert not transport.requests
        assert world[2].store.get_run(owner.run_id).status is TaskStatus.PAUSED
        await drain(client)
        await client.release_live_replay_fence()
        assert any(frame.get("status") == "BLOCKED" and frame.get("content") for frame in client.ws.frames)
    finally:
        await engine.stop()
        store.close()


@pytest.mark.asyncio
async def test_actual_engine_cancel_reaches_public_runtime_bridge(world, provider, monkeypatch):
    transport = Transport("slow")
    engine, store, access, envelope, owner, group = compose(world, transport)
    server, client = await admitted(world, engine.hub, monkeypatch)
    worker = asyncio.create_task(engine._run_worker(envelope, envelope.worker_id))
    try:
        await asyncio.wait_for(transport.entered.wait(), 3)
        assert (await engine.stop_worker(envelope.worker_id))["ok"]
        await worker
        assert world[2].store.get_run(owner.run_id).status is TaskStatus.CANCELLED
        assert_idle_pair(engine, envelope)
        for event in world[2].store.events_after(owner.run_id, 0):
            assert await server._broadcast_task_runtime_event(event)
        await drain(client)
        await client.release_live_replay_fence()
        assert any(frame.get("type") == "task_runtime_event" and
            "cancelled" in json.dumps(frame) for frame in client.ws.frames)
        assert world[2].snapshot(group.group_id, owner.run_id)["run"]["status"] == "cancelled"
        with pytest.raises(ProtectedStorageError):
            access.identity
    finally:
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await engine.stop()
        store.close()


class ProcessFault(BaseException):
    pass


def sql_dump(path):
    with closing(sqlite3.connect(path)) as conn:
        return "\n".join(conn.iterdump())


def database_snapshot(path):
    with closing(sqlite3.connect(path)) as conn:
        schema = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
        rows = {name: conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in schema}
        return schema, rows


def assert_only_legacy_constructor_bookkeeping(before, after):
    schemas, rows = before
    after_schemas, after_rows = after
    print("restart SQL evidence:", json.dumps({
        "whole_snapshot_equal": before == after,
        "added_empty_tables": sorted(set(after_schemas) - set(schemas)),
        "changed_existing_tables": sorted(name for name in rows if rows[name] != after_rows[name]),
        "schema_registry_before": rows.get("schema_registry"),
        "schema_registry_after": after_rows.get("schema_registry"),
        "sqlite_sequence_before": rows.get("sqlite_sequence"),
        "sqlite_sequence_after": after_rows.get("sqlite_sequence"),
    }))
    added = set(after_schemas) - set(schemas)
    assert added <= {"deliveries", "delivery_artifacts"}
    for table in added:
        assert after_rows[table] == []
    assert not set(schemas) - set(after_schemas)
    for table, schema in schemas.items():
        assert after_schemas[table] == schema
        if table == "schema_registry":
            # Only seven existing Completion component applied_at values may refresh.
            previous = {row[0]: row for row in rows[table]}
            current = {row[0]: row for row in after_rows[table]}
            assert set(previous) == set(current)
            for component, old in previous.items():
                new = current[component]
                if component in {"harness.completion_event", "harness.completion_outbox",
                        "harness.wait_token", "harness.decision_event", "harness.agent_completion_outcome",
                        "harness.task_result", "harness.task_journal"}:
                    assert old[:4] + old[5:] == new[:4] + new[5:]
                else:
                    assert old == new
        elif table == "sqlite_sequence":
            previous, current = dict(rows[table]), dict(after_rows[table])
            assert set(previous) == set(current)
            assert current["migration_log"] == previous["migration_log"] + 7
            assert {k: v for k, v in previous.items() if k != "migration_log"} == {
                k: v for k, v in current.items() if k != "migration_log"}
        else:
            assert rows[table] == after_rows[table], table


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_started", "after_started", "before_completion", "after_completion",
    "after_protected_event_commit", "after_protected_ack", "after_ack", "before_finalize", "after_finalize"])
async def test_actual_crash_reopen_engine_has_no_protected_recovery_work(world, provider, monkeypatch, phase):
    transport = Transport()
    engine, event_store, access, envelope, owner, group = compose(world, transport)
    selected = engine._protected_workers[envelope.task_id]
    path = selected.store.db.path
    def fault(current, *args):
        if current == phase:
            if current == "after_protected_ack" and args[0].projection_kind is not ProjectionKind.DELIVERY:
                return
            raise ProcessFault()
    selected.fault = fault
    selected.projector.fault_injector = fault
    try:
        with pytest.raises(ProcessFault):
            await engine._run_worker(envelope, envelope.worker_id)
        calls = len(transport.requests)
        await engine.stop()
        event_store.close()
        world[2].close()
        reopened = TaskRuntimeService(world[0], world[1], card_master_key=world[3])
        new_store = Store(world[0] / "events")
        hub = Hub(new_store)
        authority = ProtectedEvents.existing(reopened)
        if authority is not None:
            hub.bind_protected_events(authority)
        recovered_engine = ProjectEngine(hub, envelope.project_id, client_factory=transport.client,
            internal_workspace_root=world[0] / envelope.project_id, backend_data_root=world[0],
            task_runtime_service=reopened)
        before = database_snapshot(path)
        runtime_before = sql_dump(reopened.store.db.path)
        try:
            recovered_engine.start()
            await recovered_engine._completion_recovery_task
            await asyncio.sleep(0)
            assert_only_legacy_constructor_bookkeeping(before, database_snapshot(path))
            assert sql_dump(reopened.store.db.path) == runtime_before
            assert not recovered_engine._protected_workers
            assert not recovered_engine._completion_notifications_queued
            assert not recovered_engine._task_envelopes
            assert not recovered_engine._agents
            assert len(transport.requests) == calls
            assert reopened.store.get_run(owner.run_id).status is (
                TaskStatus.SUCCEEDED if phase == "after_finalize" else TaskStatus.PAUSED)
            refs = [row for row in hub.replay(envelope.project_id, 0)[0] if REF in row]
            if phase in {"after_protected_event_commit", "after_protected_ack", "after_ack", "before_finalize", "after_finalize"}:
                before_history = sql_dump(path)
                assert len(refs) == 1
                assert authority.hydrate(refs[0])["content"] == ANSWER
                reader = (authority.registry.completed_history(group.group_id, envelope.task_id,
                    completion_path=path) if phase == "after_finalize"
                    else authority.registry.paused_history(group.group_id, envelope.task_id))
                with SQLiteCompletionStore(path, protected=reader) as selected_store:
                    assert any(row.projection_kind is ProjectionKind.COORDINATOR for row in selected_store.pending_outbox())
                assert sql_dump(path) == before_history
        finally:
            await recovered_engine.stop()
            new_store.close()
            reopened.close()
    finally:
        await engine.stop()
        event_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
async def test_unhydrated_private_ref_does_not_hide_terminal_bridge(world, provider, monkeypatch, terminal):
    transport = Transport()
    engine, store, access, envelope, owner, group = compose(world, transport)
    selected = engine._protected_workers[envelope.task_id]
    server, client = await admitted(world, engine.hub, monkeypatch)
    def fault(stage, *args):
        if stage == "after_protected_event_commit":
            raise ProcessFault()
    selected.projector.fault_injector = fault
    try:
        with pytest.raises(ProcessFault):
            await engine._run_worker(envelope, envelope.worker_id)
        await drain(client)
        assert any(REF in row for row in client._live_replay_buffer)
        world[2].finalize_trusted_execution(group.group_id, owner.run_id,
            execution_key="terminal-probe", outcome=TaskStatus(terminal))
        await client.release_live_replay_fence()  # Private hydrate now denies and fences history.
        for event in world[2].store.events_after(owner.run_id, 0):
            assert await server._broadcast_task_runtime_event(event)
        await drain(client)
        assert any(frame.get("type") == "task_runtime_event" and
            terminal in json.dumps(frame) for frame in client.ws.frames)
        assert ANSWER not in json.dumps(client.ws.frames, ensure_ascii=False)
        assert REASONING not in json.dumps(client.ws.frames, ensure_ascii=False)
        assert world[2].snapshot(group.group_id, owner.run_id)["run"]["status"] == terminal
        assert any(row.get("type") == "task_runtime_event" and terminal in json.dumps(row)
            for row in engine.hub.replay(envelope.project_id, 0)[0])
    finally:
        await engine.stop()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ['{"question":"","extra":1}', '{"question":" "}',
    '{"question":3}', '[]', '{"question":"one","question":"two"}',
    json.dumps({"question": "中" * 2731}), '{"question":"valid","authority":"forged"}'])
async def test_native_wait_rejects_malformed_control(world, provider, arguments):
    transport = Transport("wait", [{"index": 0, "id": "wait", "type": "function",
        "function": {"name": "protected_wait_for_user", "arguments": arguments}}])
    engine, store, access, envelope, owner, group = compose(world, transport)
    try:
        await engine._run_worker(envelope, envelope.worker_id)
        assert len(transport.requests) == 1
        assert world[2].store.get_run(owner.run_id).status is TaskStatus.FAILED
        assert not engine.hub.protected_events.references(envelope.project_id)
    finally:
        await engine.stop()
        store.close()


@pytest.mark.asyncio
async def test_two_lineages_and_legacy_selected_in_actual_engine(world, provider):
    transport = Transport()
    engine, store, first, envelope, owner, group = compose(world, transport)
    try:
        await engine._run_worker(envelope, envelope.worker_id)
        _, second, other, second_owner, _, _ = enrolled(world, group=group, enroll=False)
        engine.inject_task_envelope(other, protected=second)
        await engine._run_worker(other, other.worker_id)
        legacy = TaskRun(TaskEnvelope(task_id="legacy", attempt_id="legacy", project_id=envelope.project_id,
            worker_id="legacy", goal="legacy"), state=TaskState.RUNNING)
        engine.completion_store.create_started_run(legacy)
        assert [r.task_id for r in engine.completion_store.active_attempts()] == ["legacy"]
        assert engine.completion_store.pending_outbox() == ()
        assert engine.completion_store.list_active() == ()
        engine.start()
        await engine._completion_recovery_task
        assert not engine._completion_notifications_queued
        assert len(transport.requests) == 2
        first.registry.revoke(group.group_id, first.task_id)
        with pytest.raises(ProtectedStorageError):
            first.identity
        with SQLiteCompletionStore(engine.internal_workspace / "runtime/runtime.sqlite3", protected=second) as selected:
            assert len(selected.pending_outbox()) == 1
        assert [r.task_id for r in engine.completion_store.active_attempts()] == ["legacy"]
    finally:
        await engine.stop()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("deny", ["missing", "wrong_task", "paused", "revoked", "wrong_runtime", "wrong_path"])
async def test_missing_or_wrong_private_selection_has_zero_sends(world, provider, deny):
    transport = Transport()
    _, access, envelope, owner, group, _ = enrolled(world, enroll=False)
    events = Store(world[0] / "events")
    engine = ProjectEngine(Hub(events), envelope.project_id, client_factory=transport.client,
        internal_workspace_root=world[0] / ("alternate" if deny == "wrong_path" else envelope.project_id),
        backend_data_root=world[0], task_runtime_service=None if deny == "wrong_runtime" else world[2])
    if deny == "paused":
        world[2].pause(group.group_id, owner.run_id, request_key="pause")
    if deny == "revoked":
        access.registry.revoke(group.group_id, access.task_id)
    if deny == "wrong_task":
        envelope = replace(envelope, task_id="p1_task_" + "a" * 32)
    try:
        with pytest.raises(ProtectedStorageError):
            engine.inject_task_envelope(envelope, protected=None if deny == "missing" else access)
        assert not transport.requests
        assert not engine._agents
        assert not engine._task_envelopes
    finally:
        await engine.stop()
        events.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_started", "before_completion", "after_completion", "after_ack"])
async def test_real_storage_failure_keeps_true_failed_owner(world, provider, monkeypatch, phase):
    transport = Transport()
    engine, store, access, envelope, owner, group = compose(world, transport)
    selected = engine._protected_workers[envelope.task_id]
    server, client = await admitted(world, engine.hub, monkeypatch)
    def fault(current):
        if current == phase:
            raise OSError(FAILURE)
    selected.fault = fault
    try:
        with pytest.raises(ProtectedStorageError, match="execution failed"):
            await engine._run_worker(envelope, envelope.worker_id)
        assert world[2].store.get_run(owner.run_id).status is TaskStatus.FAILED
        await drain(client)
        if phase != "before_started":
            assert_idle_pair(engine, envelope)
        await client.release_live_replay_fence()
        for event in world[2].store.events_after(owner.run_id, 0):
            await server._broadcast_task_runtime_event(event)
        await drain(client)
        assert any(frame.get("type") == "task_runtime_event" and "failed" in json.dumps(frame)
            for frame in client.ws.frames)
        assert FAILURE not in json.dumps(client.ws.frames, ensure_ascii=False)
        assert REASONING not in json.dumps(client.ws.frames, ensure_ascii=False)
    finally:
        await engine.stop()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["revoke", "archive", "shutdown", "delete", "quarantine", "runtime_close"])
async def test_slow_provider_lifecycle_cancels_pending_work(world, provider, lifecycle):
    transport = Transport("slow")
    engine, store, access, envelope, owner, group = compose(world, transport)
    selected = engine._protected_workers[envelope.task_id]
    task = asyncio.create_task(engine._run_worker(envelope, envelope.worker_id))
    try:
        await asyncio.wait_for(transport.entered.wait(), 3)
        if lifecycle == "revoke":
            access.registry.revoke(group.group_id, access.task_id)
        elif lifecycle == "archive":
            world[1].archive(group.group_id)
        elif lifecycle == "delete":
            groups, service = world[1:3]
            tx = groups.prepare_delete(group.group_id, groups.delete_preview(group.group_id).confirmation_token)
            service.prepare_group_deletion(group.group_id, tx["transaction_id"])
            service.clean_prepared_group_deletion(group.group_id)
            groups.mark_runtime_deleted(tx["transaction_id"])
            groups.commit_delete(tx["transaction_id"])
            service.complete_group_deletion(group.group_id)
        elif lifecycle == "quarantine":
            world[2].store.prepare_group_deletion(group.group_id, "invalid-proof",
                group_proof_digest="invalid", confirmation_token="invalid", runtime_project_id=envelope.project_id)
            world[2].reconcile_group_deletions()
        elif lifecycle == "runtime_close":
            world[2].close()
        else:
            await engine.stop(immediate=True)
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        assert selected.closed and selected.store._closed and selected.agent is None
        assert len(transport.requests) == 1
        assert not engine._task_envelopes
        assert_idle_pair(engine, envelope)
        with pytest.raises(ProtectedStorageError):
            selected.authority()
        if lifecycle in {"revoke", "archive"}:
            terminal = world[2].store.get_run(owner.run_id)
            assert terminal.status is TaskStatus.FAILED
            assert terminal.failure_type is FailureType.PERMISSION_DENIED
        elif lifecycle in {"quarantine", "shutdown"}:
            assert world[2].store.get_run(owner.run_id).status is TaskStatus.RUNNING
        with closing(sqlite3.connect(selected.store.db.path)) as conn:
            assert conn.execute("SELECT count(*) FROM completion_events_v2").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == (
                0 if lifecycle in {"delete", "revoke"} else 1)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.stop()
        store.close()


@pytest.mark.asyncio
async def test_actual_scheduler_and_slow_client_archive_history(world, provider, monkeypatch):
    transport = Transport("slow")
    engine, store, access, envelope, owner, group = compose(world, transport)
    server, client = await admitted(world, engine.hub, monkeypatch)
    await client.release_live_replay_fence()
    engine._spawn_pending_workers()
    worker = engine._worker_turns[envelope.worker_id]
    try:
        await asyncio.wait_for(transport.entered.wait(), 3)
        await client._send_lock.acquire()
        transport.release.set()
        await asyncio.wait_for(worker, 3)
        assert world[2].store.get_run(owner.run_id).status is TaskStatus.SUCCEEDED
        world[1].archive(group.group_id)
        client._send_lock.release()
        await drain(client)
        assert ANSWER not in json.dumps(client.ws.frames, ensure_ascii=False)
        refs = engine.hub.protected_events.references(envelope.project_id)
        assert len(refs) == 1
        assert await client.send(refs[0], _protected_history=True)
        assert client.ws.frames[-1]["content"] == ANSWER
        assert REASONING not in json.dumps(client.ws.frames, ensure_ascii=False)
        assert not engine._completion_notifications_queued
        assert not engine._agents
    finally:
        if client._send_lock.locked():
            client._send_lock.release()
        await engine.stop()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["owner", "lease", "scope", "revoke"])
async def test_authority_and_lease_rechecked_after_started_before_model_send(world, provider, denial):
    transport = Transport()
    engine, store, access, envelope, owner, group = compose(world, transport)
    selected = engine._protected_workers[envelope.task_id]
    def change(stage):
        if stage != "after_started":
            return
        if denial == "owner":
            world[2].pause(group.group_id, owner.run_id, request_key="pause")
        elif denial == "lease":
            provider[0].revoke_secret(provider[1]["secret_ref"])
        elif denial == "scope":
            world[1].rename(group.group_id, "Changed")
        else:
            access.registry.revoke(group.group_id, access.task_id)
    selected.fault = change
    try:
        try:
            await engine._run_worker(envelope, envelope.worker_id)
        except ProtectedStorageError:
            pass
        assert transport.requests == []
    finally:
        await engine.stop()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [("protected_wait_for_user", "safe_write_file"),
    ("protected_wait_for_user", "protected_wait_for_user"), ("safe_write_file",)])
async def test_mixed_native_controls_never_execute_partial_actions(world, provider, names):
    calls = [{"index": i, "id": f"call_{i}", "type": "function", "function": {
        "name": name, "arguments": json.dumps({"question": WAIT})}} for i, name in enumerate(names)]
    transport = Transport("wait", calls)
    engine, store, access, envelope, owner, group = compose(world, transport)
    try:
        await engine._run_worker(envelope, envelope.worker_id)
        assert world[2].store.get_run(owner.run_id).status is TaskStatus.FAILED
        assert len(transport.requests) == 1
    finally:
        await engine.stop()
        store.close()


@pytest.mark.asyncio
async def test_control_looking_prose_is_an_answer_not_waiting(world, provider):
    transport = Transport("prose")
    engine, store, access, envelope, owner, group = compose(world, transport)
    try:
        await engine._run_worker(envelope, envelope.worker_id)
        assert world[2].store.get_run(owner.run_id).status is TaskStatus.SUCCEEDED
        assert len(transport.requests) == 1
        with pytest.raises(ProtectedStorageError):
            engine.inject_task_envelope(envelope, protected=access)
        assert len(transport.requests) == 1
    finally:
        await engine.stop()
        store.close()


@pytest.mark.asyncio
async def test_real_sqlite_readonly_write_failure_has_true_failed_status(world, provider):
    transport = Transport()
    engine, store, access, envelope, owner, group = compose(world, transport)
    selected = engine._protected_workers[envelope.task_id]
    def readonly(stage):
        if stage == "before_completion":
            selected.store.db.initialize("PRAGMA query_only=ON;")
    selected.fault = readonly
    try:
        with pytest.raises(ProtectedStorageError, match="execution failed"):
            await engine._run_worker(envelope, envelope.worker_id)
        assert len(transport.requests) == 1
        assert world[2].store.get_run(owner.run_id).status is TaskStatus.FAILED
        assert not engine._protected_workers
        with closing(sqlite3.connect(selected.store.db.path)) as conn:
            assert conn.execute("SELECT count(*) FROM completion_events_v2").fetchone()[0] == 0
    finally:
        await engine.stop()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["archive", "revoke", "pause"])
async def test_fence_between_injection_and_scheduler_closes_selection_without_legacy_fallback(
        world, provider, monkeypatch, fence):
    monkeypatch.setattr(world[2], "GLOBAL_PRIMARY_LIMIT", 1)
    transport = Transport()
    engine, store, access, envelope, owner, group = compose(world, transport)
    selected = engine._protected_workers[envelope.task_id]
    server, client = await admitted(world, engine.hub, monkeypatch)
    await client.release_live_replay_fence()
    other_group = world[1].create_group("Other owner")
    other_owner = world[2].submit(other_group.group_id, title="Waiting capacity", request_key="capacity")
    world[2].schedule()
    assert world[2].store.get_run(other_owner.run_id).status is TaskStatus.QUEUED
    # Failure authority must retain the originally admitted owner, not this mutable view.
    selected.binding["owner_run_id"] = other_owner.run_id
    selected.binding["owner_task_id"] = other_owner.task_id
    legacy_failures = []
    original_fail = engine.fail_task

    async def record_legacy_failure(*args, **kwargs):
        legacy_failures.append(args)
        return await original_fail(*args, **kwargs)

    monkeypatch.setattr(engine, "fail_task", record_legacy_failure)
    if fence == "archive":
        world[1].archive(group.group_id)
    elif fence == "revoke":
        access.registry.revoke(group.group_id, access.task_id)
    else:
        world[2].pause(group.group_id, owner.run_id, request_key="queued-pause")
    try:
        engine._spawn_pending_workers()
        await engine._worker_turns[envelope.worker_id]
        assert transport.requests == []
        assert legacy_failures == []
        assert selected.closed and selected.store._closed and selected.agent is None
        assert not engine._protected_workers
        assert not engine._task_envelopes
        assert engine.open_activity_snapshot() == []
        terminal = world[2].store.get_run(owner.run_id)
        assert terminal.status is (TaskStatus.PAUSED if fence == "pause" else TaskStatus.FAILED)
        if fence != "pause":
            assert terminal.failure_type is FailureType.PERMISSION_DENIED
        assert world[2].store.get_run(other_owner.run_id).status is TaskStatus.RUNNING
        for event in world[2].store.events_after(owner.run_id, 0):
            assert await server._broadcast_task_runtime_event(event)
        await drain(client)
        status = "paused" if fence == "pause" else "failed"
        assert any(frame.get("type") == "task_runtime_event" and status in json.dumps(frame)
            for frame in client.ws.frames)
        assert world[2].snapshot(group.group_id, owner.run_id)["run"]["status"] == status
        with closing(sqlite3.connect(selected.store.db.path)) as conn:
            assert conn.execute("SELECT count(*) FROM completion_events_v2").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0
        assert engine._completion_store_instance is None
    finally:
        await engine.stop()
        store.close()


@pytest.mark.asyncio
async def test_scheduler_storage_failure_keeps_selected_finalizer_and_cleanup(world, provider, monkeypatch, caplog):
    transport = Transport()
    engine, store, access, envelope, owner, group = compose(world, transport)
    selected = engine._protected_workers[envelope.task_id]
    legacy_failures = []

    async def legacy_failure(*args, **kwargs):
        legacy_failures.append(args)

    def fail_completion(phase):
        if phase == "before_completion":
            raise OSError(FAILURE)

    monkeypatch.setattr(engine, "fail_task", legacy_failure)
    selected.fault = fail_completion
    try:
        engine._spawn_pending_workers()
        await engine._worker_turns[envelope.worker_id]
        assert len(transport.requests) == 1
        assert legacy_failures == []
        assert world[2].store.get_run(owner.run_id).status is TaskStatus.FAILED
        assert selected.closed and selected.store._closed and selected.agent is None
        assert not engine._protected_workers and not engine._task_envelopes
        assert engine._completion_store_instance is None
        assert_idle_pair(engine, envelope)
        assert FAILURE not in caplog.text and INSTRUCTION not in caplog.text
        assert REASONING not in caplog.text and ANSWER not in caplog.text
    finally:
        await engine.stop()
        store.close()


@pytest.mark.asyncio
async def test_first_public_phase_does_not_wait_for_authenticated_client_socket(world, provider, monkeypatch):
    transport = Transport("slow")
    engine, store, access, envelope, owner, group = compose(world, transport)
    selected = engine._protected_workers[envelope.task_id]
    server, client = await admitted(world, engine.hub, monkeypatch)
    await client.release_live_replay_fence()
    assert engine.hub.protected_events is None
    await client._send_lock.acquire()
    worker = asyncio.create_task(engine._run_worker(envelope, envelope.worker_id))
    try:
        await asyncio.wait_for(transport.entered.wait(), 1)
        assert selected.store.active_attempts()
        assert not client.ws.frames
        assert engine.hub.protected_events is not None
        with closing(sqlite3.connect(selected.store.db.path)) as conn:
            assert conn.execute("SELECT count(*) FROM completion_events_v2").fetchone()[0] == 0
        transport.release.set()
        await asyncio.wait_for(asyncio.shield(worker), 2)
        assert world[2].store.get_run(owner.run_id).status is TaskStatus.SUCCEEDED
        assert not client.ws.frames
        client._send_lock.release()
        await drain(client)
        assert any(frame.get("content") == ANSWER for frame in client.ws.frames)
        assert_idle_pair(engine, envelope)
    finally:
        if client._send_lock.locked():
            client._send_lock.release()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await drain(client)
        await engine.stop()
        store.close()


@pytest.mark.asyncio
async def test_startup_publication_binding_failure_cleans_without_starting_worker(world, provider, monkeypatch):
    transport = Transport()
    engine, store, access, envelope, owner, group = compose(world, transport)
    selected = engine._protected_workers[envelope.task_id]

    def fail_binding(_authority):
        raise OSError(FAILURE)

    monkeypatch.setattr(engine.hub, "bind_protected_events", fail_binding)
    try:
        with pytest.raises(ProtectedStorageError, match="execution failed"):
            await engine._run_worker(envelope, envelope.worker_id)
        assert transport.requests == []
        assert selected.closed and selected.store._closed and selected.agent is None
        assert not engine._protected_workers and not engine._task_envelopes
        assert engine.open_activity_snapshot() == []
        assert world[2].store.get_run(owner.run_id).status is TaskStatus.FAILED
        with closing(sqlite3.connect(selected.store.db.path)) as conn:
            assert conn.execute("SELECT count(*) FROM task_runs").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM completion_events_v2").fetchone()[0] == 0
    finally:
        await engine.stop()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["archive", "revoke", "user_cancel", "shutdown",
    "pause", "terminal", "quarantine", "runtime_close"])
async def test_first_phase_cancellation_settles_owner_without_waiting_for_slow_client(
        world, provider, monkeypatch, lifecycle):
    monkeypatch.setattr(world[2], "GLOBAL_PRIMARY_LIMIT", 1)
    transport = Transport()
    engine, store, access, envelope, owner, group = compose(world, transport)
    selected = engine._protected_workers[envelope.task_id]
    server, client = await admitted(world, engine.hub, monkeypatch)
    await client.release_live_replay_fence()
    other_group = world[1].create_group("Startup capacity")
    other = world[2].submit(other_group.group_id, title="Next owner", request_key="startup-capacity")
    world[2].schedule()
    assert world[2].store.get_run(other.run_id).status is TaskStatus.QUEUED
    await client._send_lock.acquire()
    # A local sequence barrier gives cancellation an exact first-phase await, before send.
    await engine.hub._seq_lock.acquire()
    worker = asyncio.create_task(engine._run_worker(envelope, envelope.worker_id))
    action = None
    try:
        async with asyncio.timeout(2):
            while not selected._active_published:
                await asyncio.sleep(0)
        assert selected.store.active_attempts()
        assert transport.requests == []
        if lifecycle == "archive":
            world[1].archive(group.group_id)
        elif lifecycle == "revoke":
            access.registry.revoke(group.group_id, access.task_id)
        elif lifecycle == "user_cancel":
            action = asyncio.create_task(engine.stop_worker(envelope.worker_id))
        elif lifecycle == "shutdown":
            action = asyncio.create_task(engine.stop(immediate=True))
        elif lifecycle == "pause":
            world[2].pause(group.group_id, owner.run_id, request_key="startup-pause")
        elif lifecycle == "terminal":
            world[2].finalize_trusted_execution(group.group_id, owner.run_id,
                execution_key="startup-terminal", outcome=TaskStatus.FAILED)
        elif lifecycle == "quarantine":
            world[2].store.prepare_group_deletion(group.group_id, "invalid-proof",
                group_proof_digest="invalid", confirmation_token="invalid",
                runtime_project_id=envelope.project_id)
            world[2].reconcile_group_deletions()
        else:
            world[2].close()
        async with asyncio.timeout(2):
            while not worker.cancelling():
                await asyncio.sleep(0)
        engine.hub._seq_lock.release()
        outcome = await asyncio.wait_for(asyncio.shield(
            asyncio.gather(worker, return_exceptions=True)), 1)
        assert isinstance(outcome[0], asyncio.CancelledError)
        if action is not None:
            await asyncio.wait_for(action, 1)
        assert selected.closed and selected.store._closed and selected.agent is None
        assert not engine._protected_workers and not engine._task_envelopes
        assert engine.open_activity_snapshot() == []
        assert transport.requests == [] and not client.ws.frames
        if lifecycle != "runtime_close":
            expected = {"archive": TaskStatus.FAILED, "revoke": TaskStatus.FAILED,
                "user_cancel": TaskStatus.CANCELLED, "pause": TaskStatus.PAUSED,
                "terminal": TaskStatus.FAILED}.get(lifecycle, TaskStatus.RUNNING)
            settled = world[2].store.get_run(owner.run_id)
            assert settled.status is expected
            if lifecycle in {"archive", "revoke"}:
                assert settled.failure_type is FailureType.PERMISSION_DENIED
            elif lifecycle == "terminal":
                assert settled.failure_type is FailureType.EXECUTOR_FAILURE
            assert world[2].store.get_run(other.run_id).status is (
                TaskStatus.QUEUED if lifecycle in {"shutdown", "quarantine"} else TaskStatus.RUNNING)
            if lifecycle in {"archive", "revoke", "user_cancel"}:
                for event in world[2].store.events_after(owner.run_id, 0):
                    assert await server._broadcast_task_runtime_event(event)
        client._send_lock.release()
        await drain(client)
        if lifecycle in {"archive", "revoke", "user_cancel"}:
            assert any(frame.get("type") == "task_runtime_event" and expected.value in json.dumps(frame)
                for frame in client.ws.frames)
        for marker in (INSTRUCTION, ANSWER, REASONING, FAILURE):
            assert marker not in json.dumps(client.ws.frames, ensure_ascii=False)
    finally:
        if engine.hub._seq_lock.locked():
            engine.hub._seq_lock.release()
        if client._send_lock.locked():
            client._send_lock.release()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        if action is not None:
            await asyncio.gather(action, return_exceptions=True)
        await drain(client)
        await engine.stop()
        store.close()
