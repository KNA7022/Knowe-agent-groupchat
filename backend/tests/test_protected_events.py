from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
import os

import pytest
from websockets.protocol import State

from backend.hub import Hub, Client
from backend.persist import Store
from backend.protected_events import ProtectedEvents, TABLE, HEADS, REF
from backend.protected_storage import ProtectedLineages, ProtectedStorageError
from backend.knowe_harness.completion import SQLiteCompletionStore, ProjectionKind, CompletionStatus
from backend.server import KnoweServer
from backend.config import CONFIG
from backend.groups import GroupRepository
from backend.task_runtime import TaskRuntimeService
from backend.task_runtime.contracts import TaskStatus, EvidenceRef
from backend.tests.test_protected_history import world, seed, ANSWER, REASONING


class Socket:
    state = State.OPEN
    def __init__(self):
        self.frames = []
    async def send(self, raw):
        self.frames.append(json.loads(raw))
    def respond(self, status, body):
        return status


def setup_delivery(world, tmp_path, *, worker_status="SUCCEEDED"):
    custom = worker_status not in {"SUCCEEDED", "WAITING"}
    fixture = seed(world, tmp_path, completed=False, worker_status="RUNNING" if custom else worker_status)
    registry, access, envelope, run, completion, owner, group = fixture
    if custom:
        from backend.runtime import TaskState
        run.state = TaskState.IDLE
        run.final_candidate = ANSWER
        run.terminal_reason = ANSWER
        run.metadata["reasoning"] = REASONING
        run.set_completion_status(worker_status)
        with SQLiteCompletionStore(tmp_path / "shared.sqlite3", protected=access) as selected:
            completion = selected.commit_run(run, status=CompletionStatus(worker_status))
        fixture = (registry, access, envelope, run, completion, owner, group)
    authority = ProtectedEvents(registry)
    authority.attach(access, tmp_path / "shared.sqlite3")
    store = Store(tmp_path / "events")
    hub = Hub(store)
    hub.bind_protected_events(authority)
    with SQLiteCompletionStore(tmp_path / "shared.sqlite3", protected=access) as selected:
        outbox = next(row for row in selected.pending_outbox() if row.projection_kind is ProjectionKind.DELIVERY)
    return fixture, authority, store, hub, outbox


async def admitted(world, hub, monkeypatch):
    server = KnoweServer.__new__(KnoweServer)
    server.groups = world[1]
    server.task_runtime = world[2]
    server.hub = hub
    socket = Socket()
    monkeypatch.setattr("backend.server.CONFIG", replace(CONFIG, runtime_token="a" * 64))
    assert await server._authenticate_ws(socket, SimpleNamespace(headers={"X-Knowe-Runtime-Token": "a" * 64}, path="/")) is None
    client = Client(socket)
    server._admit_protected_client(client)
    hub.add_client(client)
    return server, client


async def drain(client):
    if client._ordered_task is not None:
        await client._ordered_task


@pytest.mark.asyncio
async def test_real_commit_hub_store_client_and_nested_snapshot(world, tmp_path, monkeypatch):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    access, project = fixture[1], fixture[2].project_id
    server, client = await admitted(world, hub, monkeypatch)
    commit = await hub.emit_protected(access, outbox.outbox_id)
    await drain(client)
    assert client.ws.frames == []  # Real live-replay buffer retains the opaque ref.
    assert REF in client._live_replay_buffer[0]
    assert ANSWER not in repr(client._live_replay_buffer)
    await client.release_live_replay_fence()
    assert client.ws.frames[-1]["content"] == ANSWER
    from backend.contract import validate_outbound
    validate_outbound(client.ws.frames[-1])
    assert client.ws.frames[-1]["metadata"] == {"evidence": [
        {"kind": "completion", "ref": fixture[4].completion_id}]}
    assert REASONING not in json.dumps(client.ws.frames)
    repeated = await hub.emit_protected(access, outbox.outbox_id)
    assert repeated.reference == commit.reference and not repeated.created
    assert len(client.ws.frames) == 1
    await hub.snapshot(project)
    await drain(client)
    assert client.ws.frames[-1]["conversation"][0]["content"] == ANSWER
    assert ANSWER not in repr(hub.projects[project].ring.events())
    assert ANSWER not in repr(hub._idempotent_events)
    store.flush()
    store.rotate_and_compress(project)
    assert store.load_all_events(project) == [commit.reference]
    assert authority.head(project) == 1
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.suffix != ".py":
            assert ANSWER.encode() not in path.read_bytes()
            assert REASONING.encode() not in path.read_bytes()
    store.close()


@pytest.mark.asyncio
async def test_wrong_token_and_unadmitted_client_cannot_hydrate(world, tmp_path, monkeypatch):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    commit = await hub.emit_protected(fixture[1], outbox.outbox_id)
    server, valid = await admitted(world, hub, monkeypatch)
    socket = Socket()
    assert await server._authenticate_ws(socket, SimpleNamespace(headers={}, path="/?token=wrong")) == 401
    denied = Client(socket)
    server._admit_protected_client(denied)
    assert not await denied.send(commit.reference)
    assert socket.frames == []
    substituted = {**commit.reference, "group_id": "foreign"}
    assert not await valid.send(substituted)
    assert valid.ws.frames == []
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before_commit", "inside_commit"])
async def test_failed_authority_commit_returns_no_receipt_or_send(world, tmp_path, monkeypatch, phase):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    server, client = await admitted(world, hub, monkeypatch)
    def fail(current):
        if current == phase:
            raise OSError("injected write failure")
    authority.fault = fail
    with pytest.raises(ProtectedStorageError):
        await hub.emit_protected(fixture[1], outbox.outbox_id)
    assert authority.references(fixture[2].project_id) == []
    assert authority.head(fixture[2].project_id) == 0
    assert client.ws.frames == [] and not client._ordered_events
    authority.fault = None
    assert (await hub.emit_protected(fixture[1], outbox.outbox_id)).reference["seq"] == 1
    await drain(client)
    store.close()


@pytest.mark.asyncio
async def test_commit_enqueue_fault_requests_resync_retry_does_not_send_old_seq(world, tmp_path, monkeypatch):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    server, client = await admitted(world, hub, monkeypatch)
    def fail():
        raise OSError("enqueue fault")
    hub.protected_enqueue_fault = fail
    with pytest.raises(OSError):
        await hub.emit_protected(fixture[1], outbox.outbox_id)
    await drain(client)
    assert client.ws.frames[-1]["type"] == "resync_required"
    hub.protected_enqueue_fault = None
    await hub.emit(fixture[2].project_id, {"type": "message", "content": "legacy"})
    retry = await hub.emit_protected(fixture[1], outbox.outbox_id)
    assert not retry.created and retry.reference["seq"] == 1
    await drain(client)
    assert all(frame["type"] == "resync_required" for frame in client.ws.frames)
    assert len(hub.replay(fixture[2].project_id, 0)[0]) == 2
    store.close()


@pytest.mark.asyncio
async def test_nonzero_cursor_replay_recovers_committed_event_missing_from_ring(world, tmp_path):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    project = fixture[2].project_id
    legacy = await hub.emit(project, {"type": "message", "content": "Earlier legacy"})
    def fail():
        raise OSError("enqueue fault")
    hub.protected_enqueue_fault = fail
    with pytest.raises(OSError):
        await hub.emit_protected(fixture[1], outbox.outbox_id)
    hub.protected_enqueue_fault = None
    later = await hub.emit(project, {"type": "message", "content": "Later legacy"})
    retry = await hub.emit_protected(fixture[1], outbox.outbox_id)
    assert not retry.created
    # Recovering a derived cache must not duplicate the authoritative event.
    store.append_event(project, retry.reference)
    replay, gap, _ = hub.replay(project, legacy["seq"])
    assert not gap and [event["seq"] for event in replay] == [retry.reference["seq"], later["seq"]]
    assert authority.hydrate(replay[0])["content"] == ANSWER
    world[1].rename(fixture[6].group_id, "Replay scope changed")
    with pytest.raises(ProtectedStorageError):
        authority.hydrate(hub.replay(project, legacy["seq"])[0][0])
    store.close()


@pytest.mark.asyncio
async def test_reopen_paused_replays_authority_without_new_writes(world, tmp_path, monkeypatch):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    commit = await hub.emit_protected(fixture[1], outbox.outbox_id)
    store.close()
    world[2].close()
    reopened_runtime = TaskRuntimeService(world[0], GroupRepository(world[0]), card_master_key=world[3])
    try:
        reopened = ProtectedEvents.existing(reopened_runtime)
        disk = Store(tmp_path / "events")
        fresh_hub = Hub(disk)
        fresh_hub.bind_protected_events(reopened)
        project = fixture[2].project_id
        fresh_hub.restore(project, "History", disk.load_all_events(project), disk.load_seq_watermark(project))
        with reopened._stores[project].transaction(immediate=False) as conn:
            before = tuple(conn.iterdump())
        replay, gap, _ = fresh_hub.replay(project, 0)
        assert replay == [commit.reference] and not gap
        assert reopened.hydrate(replay[0])["content"] == ANSWER
        with reopened._stores[project].transaction(immediate=False) as conn:
            assert tuple(conn.iterdump()) == before
        assert reopened_runtime.store.get_run(fixture[5].run_id).status is TaskStatus.PAUSED
        disk.close()
    finally:
        reopened_runtime.close()


@pytest.mark.asyncio
async def test_revoke_while_send_waits_for_backpressure_blocks_hydration(world, tmp_path, monkeypatch):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    commit = await hub.emit_protected(fixture[1], outbox.outbox_id)
    server, client = await admitted(world, hub, monkeypatch)
    await client._send_lock.acquire()
    pending = asyncio.create_task(client.send(commit.reference))
    await asyncio.sleep(0)
    authority.purge(fixture[6].group_id, fixture[2].task_id)
    client._send_lock.release()
    assert not await pending
    assert client.ws.frames == []
    assert authority.references(fixture[2].project_id) == []
    assert authority.head(fixture[2].project_id) == commit.reference["seq"]
    store.close()


@pytest.mark.asyncio
async def test_real_loopback_websocket_token_upgrade_and_delivery(world, tmp_path, monkeypatch):
    from websockets.asyncio.server import serve
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    commit = await hub.emit_protected(fixture[1], outbox.outbox_id)
    server = KnoweServer.__new__(KnoweServer)
    server.groups, server.task_runtime, server.hub = world[1], world[2], hub
    monkeypatch.setattr("backend.server.CONFIG", replace(CONFIG, runtime_token="b" * 64))
    async def connection(ws):
        client = Client(ws)
        server._admit_protected_client(client)
        await client.send(commit.reference)
        await ws.wait_closed()
    async with serve(connection, "127.0.0.1", 0, process_request=server._authenticate_ws) as listener:
        port = listener.sockets[0].getsockname()[1]
        with pytest.raises(InvalidStatus):
            async with connect(f"ws://127.0.0.1:{port}/?token=wrong"):
                pytest.fail("invalid token connected")
        async with connect(f"ws://127.0.0.1:{port}/?token={'b' * 64}") as ws:
            raw = await asyncio.wait_for(ws.recv(), 3)
            frame = json.loads(raw)
            assert frame["content"] == ANSWER and frame["seq"] == commit.reference["seq"]
            assert REASONING not in raw and REF not in frame
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["WAITING", "FAILED", "BLOCKED", "CANCELLED"])
async def test_closed_result_projection_retains_useful_output(world, tmp_path, status):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path, worker_status=status)
    commit = await hub.emit_protected(fixture[1], outbox.outbox_id)
    body = authority.hydrate(commit.reference)
    assert body["content"] == ANSWER and body["status"] == status
    assert REASONING not in json.dumps(body)
    store.close()


@pytest.mark.asyncio
async def test_two_lineages_legacy_order_purge_highest_and_reopen(world, tmp_path, monkeypatch):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    first = await hub.emit_protected(fixture[1], outbox.outbox_id)
    owner = fixture[5]
    world[2].finalize_trusted_execution(fixture[6].group_id, owner.run_id,
        execution_key="event-fixture", outcome=TaskStatus.SUCCEEDED,
        evidence=(EvidenceRef("completion", fixture[4].completion_id, "1", fixture[4].completion_id),))
    second = seed(world, tmp_path, completed=False, group=fixture[6])
    authority.attach(second[1], tmp_path / "shared.sqlite3")
    legacy = await hub.emit(fixture[2].project_id, {"type": "message", "content": "Legacy retained"})
    with SQLiteCompletionStore(tmp_path / "shared.sqlite3", protected=second[1]) as selected:
        next_out = next(row for row in selected.pending_outbox() if row.projection_kind is ProjectionKind.DELIVERY)
    last = await hub.emit_protected(second[1], next_out.outbox_id)
    assert [first.reference["seq"], legacy["seq"], last.reference["seq"]] == [1, 2, 3]
    with pytest.raises(ProtectedStorageError):
        await hub.emit_protected(fixture[1], next_out.outbox_id)
    authority.purge(second[6].group_id, second[2].task_id)
    assert authority.hydrate(first.reference)["content"] == ANSWER
    store.close()
    world[2].close()
    runtime = TaskRuntimeService(world[0], GroupRepository(world[0]), card_master_key=world[3])
    try:
        reopened = ProtectedEvents.existing(runtime)
        disk = Store(tmp_path / "events")
        next_hub = Hub(disk)
        next_hub.bind_protected_events(reopened)
        later = await next_hub.emit(fixture[2].project_id, {"type": "message", "content": "New legacy"})
        assert later["seq"] == 4
        disk.flush()
        events = disk.load_all_events(fixture[2].project_id)
        assert [row["seq"] for row in events] == [1, 2, 4]
        disk.close()
    finally:
        runtime.close()


@pytest.mark.asyncio
async def test_purge_last_event_retains_only_counter_and_restarts(world, tmp_path):
    from backend.protected_events import CATALOG
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    commit = await hub.emit_protected(fixture[1], outbox.outbox_id)
    authority.purge(fixture[6].group_id, fixture[2].task_id)
    with world[2].store.db.transaction(immediate=False) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {CATALOG}").fetchone()[0] == 0
    store.close()
    world[2].close()
    runtime = TaskRuntimeService(world[0], GroupRepository(world[0]), card_master_key=world[3])
    try:
        events = ProtectedEvents.existing(runtime)
        assert events.head(fixture[2].project_id) == commit.reference["seq"]
        assert events.references(fixture[2].project_id) == []
    finally:
        runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("column,value", [("seq", 82), ("group_id", "foreign"),
    ("task_id", "foreign"), ("completion_id", "foreign"), ("event_type", "reasoning_delta"),
    ("wire", b"x" * 40), ("semantic_digest", "0" * 64)])
async def test_corrupt_authority_is_explicit_not_jsonl_fallback(world, tmp_path, column, value):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    commit = await hub.emit_protected(fixture[1], outbox.outbox_id)
    store.flush()
    with authority._stores[fixture[2].project_id].transaction() as conn:
        conn.execute(f"UPDATE {TABLE} SET {column}=? WHERE event_id=?", (value, commit.reference["event_id"]))
    with pytest.raises(ProtectedStorageError):
        store.load_all_events(fixture[2].project_id)
    with pytest.raises(ProtectedStorageError):
        authority.hydrate(commit.reference)
    store.close()


@pytest.mark.asyncio
async def test_actual_sqlite_write_failure_produces_no_commit(world, tmp_path):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    db = authority._stores[fixture[2].project_id]
    db.initialize("PRAGMA query_only=ON")
    with pytest.raises(ProtectedStorageError):
        await hub.emit_protected(fixture[1], outbox.outbox_id)
    assert authority.references(fixture[2].project_id) == []
    assert hub.projects[fixture[2].project_id].seq == 0
    db.initialize("PRAGMA query_only=OFF")
    store.close()


@pytest.mark.asyncio
async def test_derived_write_failure_and_gzip_cache_loss_do_not_lose_answer(world, tmp_path):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    project = fixture[2].project_id
    # A real filesystem append failure, not a mocked persistence implementation.
    store.events_path(project).mkdir()
    commit = await hub.emit_protected(fixture[1], outbox.outbox_id)
    store.flush()
    assert authority.hydrate(store.load_all_events(project)[0])["content"] == ANSWER
    store.events_path(project).rmdir()
    store.append_event(project, commit.reference)
    store._compress_day_file(store.events_path(project))
    import gzip
    archive = store.events_path(project).with_suffix(".jsonl.gz")
    with gzip.open(archive, "rt", encoding="utf-8") as handle:
        cached = handle.read()
    assert ANSWER not in cached and REASONING not in cached
    archive.unlink()
    hub.projects.clear()
    assert store.load_all_events(project) == [commit.reference]
    assert hub.replay(project, 0)[0] == [commit.reference]
    store.close()


@pytest.mark.asyncio
async def test_completed_explicit_authenticated_history_after_scope_change(world, tmp_path, monkeypatch):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    commit = await hub.emit_protected(fixture[1], outbox.outbox_id)
    server, client = await admitted(world, hub, monkeypatch)
    world[2].finalize_trusted_execution(fixture[6].group_id, fixture[5].run_id,
        execution_key="event-history", outcome=TaskStatus.SUCCEEDED,
        evidence=(EvidenceRef("completion", fixture[4].completion_id, "1", fixture[4].completion_id),))
    world[1].rename(fixture[6].group_id, "Changed current scope")
    assert not await client.send(commit.reference)
    with authority._stores[fixture[2].project_id].transaction(immediate=False) as conn:
        before = tuple(conn.iterdump())
    await server._on_frame(client, json.dumps({"type": "protected_history", "group_id": fixture[6].group_id,
        "project_id": fixture[2].project_id, "event_id": commit.reference["event_id"]}))
    assert client.ws.frames[-1]["content"] == ANSWER
    assert REASONING not in json.dumps(client.ws.frames)
    with authority._stores[fixture[2].project_id].transaction(immediate=False) as conn:
        assert tuple(conn.iterdump()) == before
    store.close()


@pytest.mark.asyncio
async def test_actual_group_delete_fences_then_cleans_event_authority(world, tmp_path, monkeypatch):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    commit = await hub.emit_protected(fixture[1], outbox.outbox_id)
    server, client = await admitted(world, hub, monkeypatch)
    groups, runtime, group = world[1], world[2], fixture[6]
    preview = groups.delete_preview(group.group_id)
    transaction = groups.prepare_delete(group.group_id, preview.confirmation_token)
    transaction_id = transaction["transaction_id"]
    runtime.prepare_group_deletion(group.group_id, transaction_id)
    assert not await client.send(commit.reference)
    runtime.clean_prepared_group_deletion(group.group_id)
    groups.mark_runtime_deleted(transaction_id)
    groups.commit_delete(transaction_id)
    runtime.complete_group_deletion(group.group_id)
    assert authority.references(fixture[2].project_id) == []
    assert not await client.send(commit.reference)
    assert authority.head(fixture[2].project_id) == commit.reference["seq"]
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["archive", "rename", "source", "role", "quarantine", "expired"])
async def test_current_scope_fences_buffered_delivery(world, tmp_path, monkeypatch, change):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    server, client = await admitted(world, hub, monkeypatch)
    commit = await hub.emit_protected(fixture[1], outbox.outbox_id)
    await drain(client)
    group = fixture[6]
    if change == "archive":
        world[1].archive(group.group_id)
    elif change == "rename":
        world[1].rename(group.group_id, "Changed")
    elif change == "source":
        world[1].link_source(group.group_id)
    elif change == "role":
        world[1].update_roster(group.group_id, [agent for agent in group.roster if agent.role != "planning"])
    elif change == "quarantine":
        world[2]._defer_quarantine(group.group_id, "fixture-quarantine")
    else:
        from datetime import datetime, timedelta
        class FutureClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(days=2)
        monkeypatch.setattr("backend.task_runtime.service.datetime", FutureClock)
    await client.release_live_replay_fence()
    assert client.ws.frames == []
    with pytest.raises(ProtectedStorageError):
        authority.hydrate(commit.reference)
    store.close()


@pytest.mark.asyncio
async def test_interrupted_revoke_cleanup_is_retried_on_real_reopen(world, tmp_path):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    commit = await hub.emit_protected(fixture[1], outbox.outbox_id)
    db = authority._stores[fixture[2].project_id]
    db.initialize("PRAGMA query_only=ON")
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        authority.purge(fixture[6].group_id, fixture[2].task_id)
    with pytest.raises(ProtectedStorageError):
        authority.hydrate(commit.reference)
    db.initialize("PRAGMA query_only=OFF")
    store.close()
    world[2].close()
    runtime = TaskRuntimeService(world[0], GroupRepository(world[0]), card_master_key=world[3])
    try:
        reopened = ProtectedEvents.existing(runtime)
        assert reopened.references(fixture[2].project_id) == []
        assert reopened.head(fixture[2].project_id) == commit.reference["seq"]
    finally:
        runtime.close()


@pytest.mark.asyncio
async def test_missing_key_and_corrupt_catalog_fail_without_plaintext_fallback(world, tmp_path):
    from backend.protected_events import CATALOG
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    commit = await hub.emit_protected(fixture[1], outbox.outbox_id)
    original = authority.registry.crypto._key
    authority.registry.crypto._key = None
    try:
        with pytest.raises(Exception):
            store.load_all_events(fixture[2].project_id)
    finally:
        authority.registry.crypto._key = original
    with world[2].store.db.transaction() as conn:
        conn.execute(f"UPDATE {CATALOG} SET mac=?", ("0" * 64,))
    store.close()
    world[2].close()
    runtime = TaskRuntimeService(world[0], GroupRepository(world[0]), card_master_key=world[3])
    try:
        with pytest.raises(ProtectedStorageError):
            ProtectedEvents.existing(runtime)
    finally:
        runtime.close()


@pytest.mark.asyncio
async def test_raw_protected_events_cannot_enter_generic_hub(world, tmp_path):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    for event in [
        {"type": "reasoning_delta", "task_id": fixture[2].task_id, "content": REASONING},
        {"type": "message", "task_id": fixture[2].task_id, "content": ANSWER},
        {"type": "message", REF: 1, "content": ANSWER},
    ]:
        with pytest.raises(ProtectedStorageError):
            await hub.emit(fixture[2].project_id, event)
        with pytest.raises(ProtectedStorageError):
            await hub.emit_no_seq(event)
    assert store.load_all_events(fixture[2].project_id) == []
    store.close()


@pytest.mark.asyncio
async def test_actual_ack_then_retry_keeps_identity_and_never_claims_again(world, tmp_path):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    commit = await hub.emit_protected(fixture[1], outbox.outbox_id)
    with SQLiteCompletionStore(tmp_path / "shared.sqlite3", protected=fixture[1]) as selected:
        selected.acknowledge_outbox(outbox.outbox_id,
            effect_ref=commit.reference["event_id"], effect_payload=commit.reference)
        with selected.db.transaction(immediate=False) as conn:
            before = tuple(conn.iterdump())
        retry = await hub.emit_protected(fixture[1], outbox.outbox_id)
        assert retry.reference == commit.reference and not retry.created
        with selected.db.transaction(immediate=False) as conn:
            assert tuple(conn.iterdump()) == before
    store.close()


@pytest.mark.asyncio
async def test_real_http_token_delta_history_projection_and_scope_fence(world, tmp_path, monkeypatch):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    commit = await hub.emit_protected(fixture[1], outbox.outbox_id)
    store.flush()
    project = fixture[2].project_id
    server = KnoweServer.__new__(KnoweServer)
    server.groups, server.task_runtime, server.hub, server.store = world[1], world[2], hub, store
    # Read-only legacy history container; no Worker/Engine/provider is constructed.
    server.engines = {project: SimpleNamespace(history=[])}
    server.project_card_ids = {}
    server.project_dirs = {}
    server._closing_projects = set()
    server._project_metadata_lock = asyncio.Lock()
    server._project_internal_io_tasks = {}
    monkeypatch.setattr("backend.server.CONFIG", replace(CONFIG, runtime_token="c" * 64))
    listener = await asyncio.start_server(server._health_conn, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    async def http(route, token="c" * 64):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write((f"GET {route}?project_id={project}&after_seq=0 HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\nX-Knowe-Runtime-Token: {token}\r\n\r\n").encode())
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        await writer.wait_closed()
        head, body = response.split(b"\r\n\r\n", 1)
        return int(head.split()[1]), json.loads(body)
    try:
        with authority._stores[project].transaction(immediate=False) as conn:
            child_before = tuple(conn.iterdump())
        with world[2].store.db.transaction(immediate=False) as conn:
            runtime_before = tuple(conn.iterdump())
        for route in ("/api/events", "/history"):
            status, body = await http(route, token="wrong")
            assert status == 401 and ANSWER not in repr(body)
            status, body = await http(route)
            assert status == 200 and ANSWER in repr(body)
            assert REF not in repr(body) and REASONING not in repr(body)
            if route == "/api/events":
                from backend.contract import validate_outbound
                validate_outbound(body["events"][0])
                assert body["last_seq"] == commit.reference["seq"]
        with authority._stores[project].transaction(immediate=False) as conn:
            assert tuple(conn.iterdump()) == child_before
        with world[2].store.db.transaction(immediate=False) as conn:
            assert tuple(conn.iterdump()) == runtime_before
        world[1].rename(fixture[6].group_id, "HTTP scope changed")
        for route in ("/api/events", "/history"):
            status, body = await http(route)
            assert status == 409 and ANSWER not in repr(body) and REF not in repr(body)
            assert "last_seq" not in body
    finally:
        listener.close()
        await listener.wait_closed()
        store.close()


def recovery_routes(server, client, store, project):
    # Only UI/Engine metadata is inert. Admission, authority, collection, dispatch,
    # Client.send and final hydration are the actual production methods.
    server.store = store
    server._canonical_project_id_from_request = lambda value: value
    server._project_dir_is_valid = lambda *args, **kwargs: False
    server._open_activity_all = lambda: []
    server._conversation_state = lambda project: {"muted": False, "folded": False}
    server._record_task_runtime_snapshot_publication = lambda snapshot: None
    client.sent_projects.add(project)


async def fail_committed_enqueue(hub, access, outbox):
    def fail():
        raise OSError("committed enqueue failure")
    hub.protected_enqueue_fault = fail
    try:
        with pytest.raises(OSError):
            await hub.emit_protected(access, outbox.outbox_id)
    finally:
        hub.protected_enqueue_fault = None


@pytest.mark.asyncio
async def test_request_snapshot_recovers_resync_and_orders_inflight_live(world, tmp_path, monkeypatch):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    project = fixture[2].project_id
    server, client = await admitted(world, hub, monkeypatch)
    recovery_routes(server, client, store, project)
    await client.release_live_replay_fence()
    await fail_committed_enqueue(hub, fixture[1], outbox)
    await drain(client)
    await client._send_lock.acquire()
    try:
        await server._on_frame(client, json.dumps({"type": "request_snapshot", "project_id": project}))
        await asyncio.sleep(0)  # Recovery snapshot has reached the real send lock.
        later = await hub.emit(project, {"type": "message", "content": "After snapshot capture"})
        assert project in client._protected_resync_projects
    finally:
        client._send_lock.release()
    await drain(client)
    assert [event["type"] for event in client.ws.frames] == ["resync_required", "state_snapshot", "message"]
    snapshot = client.ws.frames[1]
    assert [event["content"] for event in snapshot["conversation"]] == [ANSWER]
    assert client.ws.frames[2]["seq"] == later["seq"] > snapshot["seq"]
    assert project not in client._protected_resync_projects
    await hub.emit(project, {"type": "message", "content": "Live resumed"})
    await drain(client)
    assert client.ws.frames[-1]["content"] == "Live resumed"
    assert REASONING not in repr(client.ws.frames)
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["scope", "key"])
async def test_request_snapshot_failed_final_hydration_keeps_resync(world, tmp_path, monkeypatch, denial):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    project = fixture[2].project_id
    server, client = await admitted(world, hub, monkeypatch)
    recovery_routes(server, client, store, project)
    await client.release_live_replay_fence()
    await fail_committed_enqueue(hub, fixture[1], outbox)
    await drain(client)
    original_key = authority.registry.crypto._key
    await client._send_lock.acquire()
    try:
        await server._on_frame(client, json.dumps({"type": "request_snapshot", "project_id": project}))
        await asyncio.sleep(0)
        if denial == "scope":
            world[1].rename(fixture[6].group_id, "Denied at snapshot handoff")
        else:
            authority.registry.crypto._key = None
    finally:
        client._send_lock.release()
    try:
        await drain(client)
    finally:
        authority.registry.crypto._key = original_key
    assert project in client._protected_resync_projects
    assert [event["type"] for event in client.ws.frames] == ["resync_required"]
    await hub.emit(project, {"type": "message", "content": "Held until recovery"})
    await drain(client)
    assert len(client.ws.frames) == 1
    if denial == "key":
        await server._on_frame(client, json.dumps({"type": "request_snapshot", "project_id": project}))
        await drain(client)
        assert client.ws.frames[-1]["type"] == "state_snapshot"
        assert ANSWER in repr(client.ws.frames[-1])
        assert project not in client._protected_resync_projects
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["scope", "key", "authority"])
async def test_do_replay_final_hydration_failure_never_acknowledges_cursor(world, tmp_path, monkeypatch, denial):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    project = fixture[2].project_id
    await hub.emit_protected(fixture[1], outbox.outbox_id)
    await hub.emit(project, {"type": "message", "content": "Later durable message"})
    server, client = await admitted(world, hub, monkeypatch)
    recovery_routes(server, client, store, project)
    selected = authority._stores[project]
    original_key = authority.registry.crypto._key
    await client._send_lock.acquire()
    pending = asyncio.create_task(server._do_replay(client, {"project_id": project, "since_seq": 0}))
    await asyncio.sleep(0)  # Actual replay collection is complete, final send is blocked.
    if denial == "scope":
        world[1].rename(fixture[6].group_id, "Denied at replay handoff")
    elif denial == "key":
        authority.registry.crypto._key = None
    else:
        authority._stores.pop(project)
    client._send_lock.release()
    try:
        await pending
        await drain(client)
        await client.release_live_replay_fence()
        assert not any(event["type"] in {"message", "replay_complete"} for event in client.ws.frames)
        assert project in client._protected_resync_projects
    finally:
        authority.registry.crypto._key = original_key
        authority._stores[project] = selected
    if denial != "scope":
        await server._on_frame(client, json.dumps({"type": "request_snapshot", "project_id": project}))
        await drain(client)
        snapshot = client.ws.frames[-1]
        assert snapshot["type"] == "state_snapshot"
        assert [event["content"] for event in snapshot["conversation"]] == [ANSWER, "Later durable message"]
        assert project not in client._protected_resync_projects
        await hub.emit(project, {"type": "message", "content": "Live after retry"})
        await drain(client)
        assert client.ws.frames[-1]["content"] == "Live after retry"
    assert REASONING not in repr(client.ws.frames)
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_fence", ["resync", "revoke"])
async def test_old_snapshot_handoff_cannot_clear_newer_recovery(world, tmp_path, monkeypatch, new_fence):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    project = fixture[2].project_id
    server, client = await admitted(world, hub, monkeypatch)
    recovery_routes(server, client, store, project)
    await client.release_live_replay_fence()
    await fail_committed_enqueue(hub, fixture[1], outbox)
    await drain(client)
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    send = client.ws.send
    seen = 0
    async def delayed(raw):
        nonlocal seen
        if json.loads(raw)["type"] == "state_snapshot" and seen < 2:
            index = seen
            seen += 1
            entered[index].set()
            await release[index].wait()
        await send(raw)
    client.ws.send = delayed
    try:
        await server._on_frame(client, json.dumps({"type": "request_snapshot", "project_id": project}))
        await asyncio.wait_for(entered[0].wait(), 2)
        if new_fence == "resync":
            client.request_resync(project, hub.projects[project].seq)
        else:
            authority.purge(fixture[6].group_id, fixture[2].task_id)
        await server._on_frame(client, json.dumps({"type": "request_snapshot", "project_id": project}))
        later = await hub.emit(project, {"type": "message", "content": "After newer snapshot"})
        release[0].set()
        await asyncio.wait_for(entered[1].wait(), 2)
        assert project in client._protected_resync_projects
        assert not any(event.get("content") == "After newer snapshot" for event in client.ws.frames)
        release[1].set()
        await drain(client)
        assert project not in client._protected_resync_projects
        assert client.ws.frames[-1]["seq"] == later["seq"]
        snapshots = [event for event in client.ws.frames if event["type"] == "state_snapshot"]
        assert snapshots[0]["seq"] < snapshots[1]["seq"] < later["seq"]
        if new_fence == "revoke":
            assert ANSWER not in repr(snapshots[1])  # The earlier handoff cannot be recalled.
    finally:
        for gate in release:
            gate.set()
        await drain(client)
        store.close()


@pytest.mark.asyncio
async def test_do_replay_success_and_live_handoff_remain_ordered(world, tmp_path, monkeypatch):
    fixture, authority, store, hub, outbox = setup_delivery(world, tmp_path)
    project = fixture[2].project_id
    commit = await hub.emit_protected(fixture[1], outbox.outbox_id)
    legacy = await hub.emit(project, {"type": "message", "content": "Second"})
    server, client = await admitted(world, hub, monkeypatch)
    recovery_routes(server, client, store, project)
    with authority._stores[project].transaction(immediate=False) as conn:
        before = tuple(conn.iterdump())
    await server._do_replay(client, {"project_id": project, "since_seq": 0})
    await client.release_live_replay_fence()
    assert [event["type"] for event in client.ws.frames] == ["message", "message", "replay_complete"]
    assert [event["seq"] for event in client.ws.frames[:2]] == [commit.reference["seq"], legacy["seq"]]
    assert client.ws.frames[-1]["last_seq"] == legacy["seq"]
    with authority._stores[project].transaction(immediate=False) as conn:
        assert tuple(conn.iterdump()) == before
    await hub.emit(project, {"type": "message", "content": "Next"})
    await drain(client)
    assert client.ws.frames[-1]["content"] == "Next"
    store.close()
