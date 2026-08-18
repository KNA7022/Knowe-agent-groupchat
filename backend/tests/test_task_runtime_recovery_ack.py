from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.protocol import State

from backend.hub import Client
from backend.server import KnoweServer
from backend.task_runtime.contracts import opaque_id
from backend.task_runtime.store import TaskRuntimeStore


class _OpenSocket:
    state = State.OPEN

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send(self, raw: str) -> None:
        self.messages.append(json.loads(raw))


async def _wait_for(predicate: Any, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


async def _close(server: KnoweServer) -> None:
    await server._stop_task_runtime_event_bridge()  # noqa: SLF001
    if server.task_runtime is not None:
        server.task_runtime.close()
    if server.store is not None:
        server.store.close(timeout=5)


async def _receive_until(ws: Any, event_type: str) -> dict[str, Any]:
    for _ in range(100):
        payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        if payload.get("type") == event_type:
            return payload
    raise AssertionError(f"did not receive {event_type}")


@pytest.mark.asyncio
async def test_two_failed_runs_share_one_group_recovery_and_scoped_ack(tmp_path: Path) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    first_group = server.groups.create_group("First", legacy_project_id="project_ack_first")
    second_group = server.groups.create_group("Second", legacy_project_id="project_ack_second")
    first_run = server.task_runtime.submit(first_group.group_id, title="One", request_key="ack-one")
    other_run = server.task_runtime.submit(first_group.group_id, title="Two", request_key="ack-two")
    second_run = server.task_runtime.submit(second_group.group_id, title="Other", request_key="ack-other")
    first_event = server.task_runtime.store.events_after(first_run.run_id, 0)[0]
    other_event = server.task_runtime.store.events_after(other_run.run_id, 0)[0]
    second_event = server.task_runtime.store.events_after(second_run.run_id, 0)[0]
    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(  # noqa: SLF001
            first_event, reason="first", attempts=4,
        )
        server._record_task_runtime_recovery_locked(  # noqa: SLF001
            second_event, reason="other", attempts=4,
        )

    socket = _OpenSocket()
    client = Client(socket)
    server.hub.add_client(client)
    await client.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(  # noqa: SLF001
            other_event, reason="second", attempts=4,
        )
    await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
    rows = server.task_runtime.store.live_recoveries()
    assert len(rows) == 2
    first_row = next(row for row in rows if row["group_id"] == first_group.group_id)
    assert first_row["generation"] == 2 and first_row["failure_count"] == 2
    first_notices = [
        item for item in socket.messages
        if item.get("type") == "resync_required"
        and item.get("group_id") == first_group.group_id
    ]
    assert len(first_notices) == 1
    notice = first_notices[0]
    await server._cmd_request_snapshot(client, {  # noqa: SLF001
        "project_id": notice["project_id"],
        "recovery_token": notice["recovery_token"],
        "recovery_generation": notice["recovery_generation"],
    })
    response = next(
        item for item in socket.messages
        if item.get("type") == "task_runtime_result" and item.get("recovery_token") == notice["recovery_token"]
    )
    assert {run["run_id"] for run in response["task_runtime"]["runs"]} == {
        first_run.run_id, other_run.run_id,
    }

    valid_ack = {
        "project_id": notice["project_id"], "group_id": first_group.group_id,
        "recovery_token": notice["recovery_token"],
        "recovery_generation": response["recovery_generation"],
        "snapshot_cursor": response["snapshot_cursor"],
    }
    for forged in (
        {**valid_ack, "snapshot_cursor": "0" * 64},
        {**valid_ack, "recovery_generation": response["recovery_generation"] + 1},
        {**valid_ack, "recovery_token": "recovery_forged"},
        {**valid_ack, "project_id": "project_ack_second"},
        {**valid_ack, "group_id": second_group.group_id},
    ):
        await server._cmd_task_runtime_recovery_ack(client, forged)  # noqa: SLF001
        assert len(server.task_runtime.store.live_recoveries()) == 2
    await server._cmd_task_runtime_recovery_ack(client, valid_ack)  # noqa: SLF001
    remaining = server.task_runtime.store.live_recoveries()
    assert [row["group_id"] for row in remaining] == [second_group.group_id]
    await _close(server)


@pytest.mark.asyncio
async def test_more_than_dispatch_batch_remains_durable_and_eventually_notified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.task_runtime is not None
    count = 513
    for index in range(count):
        group_id = f"grp_{index:032x}"
        server.task_runtime.store.record_live_recovery(
            run_id=f"run_{index:032x}", group_id=group_id, through_seq=index + 1,
            reason="bulk", attempts=4, recovery_token=f"recovery_{index:032x}",
        )
    assert len(server.task_runtime.store.live_recoveries()) == count
    assert server.task_runtime.store.live_recovery("grp_" + "0" * 32) is not None
    await _close(server)
    restarted = KnoweServer(str(tmp_path))
    assert restarted.task_runtime is not None
    assert len(restarted.task_runtime.store.live_recoveries()) == count
    restarted._start_task_runtime_event_bridge()  # noqa: SLF001
    assert len(restarted._task_runtime_event_bridge_recovery) == (  # noqa: SLF001
        restarted.TASK_RUNTIME_RECOVERY_MARKER_MAX
    )
    monkeypatch.setattr(
        restarted, "_runtime_project_id_for_group", lambda group_id: f"project_{group_id[4:]}",
    )
    socket = _OpenSocket()
    client = Client(socket)
    restarted.hub.add_client(client)
    await client.release_live_replay_fence()
    await restarted._notify_task_runtime_recoveries(client)  # noqa: SLF001
    other_socket = _OpenSocket()
    other_client = Client(other_socket)
    restarted.hub.add_client(other_client)
    await other_client.release_live_replay_fence()
    await restarted._notify_task_runtime_recoveries(other_client)  # noqa: SLF001
    assert len(other_client.task_runtime_recovery_notified) == other_client.TASK_RUNTIME_RECOVERY_WINDOW_MAX
    assert len([
        item for item in other_socket.messages if item.get("type") == "resync_required"
    ]) == other_client.TASK_RUNTIME_RECOVERY_WINDOW_MAX
    restarted.hub.remove_client(other_client)
    notices = [item for item in socket.messages if item.get("type") == "resync_required"]
    assert len(notices) == client.TASK_RUNTIME_RECOVERY_WINDOW_MAX
    assert len(client.task_runtime_recovery_notified) <= client.TASK_RUNTIME_RECOVERY_WINDOW_MAX
    seen = {item["group_id"] for item in notices}
    while client.task_runtime_recovery_notified:
        group_id, token, generation = min(client.task_runtime_recovery_notified)
        assert restarted.task_runtime.store.clear_live_recovery(group_id, token, generation)
        client.task_runtime_recovery_notified.discard((group_id, token, generation))
        await restarted._notify_task_runtime_recoveries(client)  # noqa: SLF001
        assert len(client.task_runtime_recovery_notified) <= client.TASK_RUNTIME_RECOVERY_WINDOW_MAX
        seen.update(
            item["group_id"] for item in socket.messages
            if item.get("type") == "resync_required"
        )
    assert len(seen) == count
    all_notices = [item for item in socket.messages if item.get("type") == "resync_required"]
    assert len(all_notices) == len({item["group_id"] for item in all_notices}) == count
    assert "grp_" + "0" * 32 in seen
    assert len(restarted._task_runtime_event_bridge_recovery) <= restarted.TASK_RUNTIME_RECOVERY_MARKER_MAX  # noqa: SLF001
    await _close(restarted)


@pytest.mark.asyncio
async def test_snapshot_generation_is_rebuilt_when_recovery_advances_mid_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    project_id = "project_stable_capture"
    group = server.groups.create_group("Stable capture", legacy_project_id=project_id)
    run = server.task_runtime.submit(group.group_id, title="Capture", request_key="capture")
    first_event = server.task_runtime.store.events_after(run.run_id, 0)[0]
    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(first_event, reason="first", attempts=4)  # noqa: SLF001
    socket = _OpenSocket()
    client = Client(socket)
    server.hub.add_client(client)
    await client.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
    notice = next(item for item in socket.messages if item.get("type") == "resync_required")
    original_snapshot = server.task_runtime.group_snapshot
    injected = False

    def racing_snapshot(group_id: str) -> dict[str, Any]:
        nonlocal injected
        snapshot = original_snapshot(group_id)
        if not injected:
            injected = True
            server.task_runtime.checkpoint(
                group.group_id, run.run_id, label="Concurrent", request_key="capture-race",
            )
            newest = server.task_runtime.store.events_after(run.run_id, 0)[-1]
            with server._task_runtime_event_bridge_lock:  # noqa: SLF001
                server._record_task_runtime_recovery_locked(  # noqa: SLF001
                    newest, reason="advanced", attempts=4,
                )
        return snapshot

    monkeypatch.setattr(server.task_runtime, "group_snapshot", racing_snapshot)
    await server._cmd_request_snapshot(client, {  # noqa: SLF001
        "project_id": project_id,
        "recovery_token": notice["recovery_token"],
        "recovery_generation": notice["recovery_generation"],
    })
    response = next(item for item in socket.messages if item.get("type") == "task_runtime_result")
    current = server.task_runtime.store.get_run(run.run_id)
    marker = server.task_runtime.store.live_recovery(group.group_id)
    assert current is not None and marker is not None
    assert response["recovery_generation"] == marker["generation"] == 2
    projected = next(item for item in response["task_runtime"]["runs"] if item["run_id"] == run.run_id)
    assert projected["last_seq"] == current.last_seq
    await server._cmd_task_runtime_recovery_ack(client, {  # noqa: SLF001
        "type": "task_runtime_recovery_ack", "project_id": project_id,
        "group_id": group.group_id, "recovery_token": response["recovery_token"],
        "recovery_generation": response["recovery_generation"],
        "snapshot_cursor": response["snapshot_cursor"],
    })
    assert server.task_runtime.store.live_recovery(group.group_id) is None
    await _close(server)


@pytest.mark.asyncio
async def test_two_clients_must_ack_and_disconnect_retries_on_fresh_connection(
    tmp_path: Path,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    project_id = "project_multi_client"
    group = server.groups.create_group("Multi", legacy_project_id=project_id)
    run = server.task_runtime.submit(group.group_id, title="Multi", request_key="multi")

    async def issue(client: Client) -> dict[str, Any]:
        await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
        notice = next(
            item for item in reversed(client.ws.messages)
            if item.get("type") == "resync_required"
            and item.get("group_id") == group.group_id
        )
        await server._cmd_request_snapshot(client, {  # noqa: SLF001
            "project_id": project_id, "recovery_token": notice["recovery_token"],
            "recovery_generation": notice["recovery_generation"],
        })
        return next(
            item for item in reversed(client.ws.messages)
            if item.get("type") == "task_runtime_result"
        )

    async def acknowledge(client: Client, response: dict[str, Any]) -> None:
        await server._cmd_task_runtime_recovery_ack(client, {  # noqa: SLF001
            "type": "task_runtime_recovery_ack", "project_id": project_id,
            "group_id": group.group_id, "recovery_token": response["recovery_token"],
            "recovery_generation": response["recovery_generation"],
            "snapshot_cursor": response["snapshot_cursor"],
        })

    event = server.task_runtime.store.events_after(run.run_id, 0)[0]
    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(event, reason="both", attempts=4)  # noqa: SLF001
    clients = [Client(_OpenSocket()), Client(_OpenSocket())]
    for client in clients:
        server.hub.add_client(client)
        await client.release_live_replay_fence()
    responses = [await issue(client) for client in clients]
    await acknowledge(clients[0], responses[0])
    assert server.task_runtime.store.live_recovery(group.group_id) is not None
    await acknowledge(clients[1], responses[1])
    assert server.task_runtime.store.live_recovery(group.group_id) is None

    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(event, reason="disconnect", attempts=4)  # noqa: SLF001
    responses = [await issue(client) for client in clients]
    server._retain_client_recovery_on_disconnect(clients[0])  # noqa: SLF001
    server.hub.remove_client(clients[0])
    await acknowledge(clients[1], responses[1])
    marker = server.task_runtime.store.live_recovery(group.group_id)
    assert marker is not None and marker["disconnect_retry"] == 1
    replacement = Client(_OpenSocket())
    server.hub.add_client(replacement)
    await replacement.release_live_replay_fence()
    replacement_response = await issue(replacement)
    await acknowledge(replacement, replacement_response)
    assert server.task_runtime.store.live_recovery(group.group_id) is None
    await _close(server)


def test_group_deletion_atomically_cleans_bridge_rows_and_rolls_back_fault(tmp_path: Path) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    group = server.groups.create_group("Delete bridge")
    run = server.task_runtime.submit(group.group_id, title="Delete", request_key="delete-bridge")
    server.task_runtime.store.record_live_recovery(
        run_id=run.run_id, group_id=group.group_id, through_seq=1,
        reason="delete", attempts=4, recovery_token=opaque_id("recovery"),
    )
    server.task_runtime.store.record_live_publication(
        run_id=run.run_id, group_id=group.group_id, last_seq=1,
    )
    server.task_runtime.prepare_group_deletion(group.group_id, "txn_delete_bridge")
    with server.task_runtime.store.db.transaction() as conn:
        conn.execute(
            """CREATE TRIGGER fail_group_delete BEFORE DELETE ON studycrew_task_runs
               BEGIN SELECT RAISE(ABORT, 'fault boundary'); END""",
        )
    with pytest.raises(sqlite3.DatabaseError, match="fault boundary"):
        server.task_runtime.complete_group_deletion(group.group_id)
    counts = server.task_runtime.store.group_content_counts(group.group_id)
    assert counts["runs"] == counts["live_recovery"] == counts["live_publication"] == 1
    intent = next(
        item for item in server.task_runtime.store.group_deletion_intents()
        if item["group_id"] == group.group_id
    )
    assert intent["stage"] != "complete"
    with server.task_runtime.store.db.transaction() as conn:
        conn.execute("DROP TRIGGER fail_group_delete")
    server.task_runtime.complete_group_deletion(group.group_id)
    assert not any(server.task_runtime.store.group_content_counts(group.group_id).values())
    intent = next(
        item for item in server.task_runtime.store.group_deletion_intents()
        if item["group_id"] == group.group_id
    )
    assert intent["stage"] == "complete"
    server.task_runtime.close()
    if server.store is not None:
        server.store.close(timeout=5)


def test_run_scoped_recovery_schema_migrates_to_one_group_generation(tmp_path: Path) -> None:
    database = tmp_path / "legacy-runtime.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """CREATE TABLE studycrew_live_recovery (
                   run_id TEXT PRIMARY KEY, group_id TEXT NOT NULL,
                   through_seq INTEGER NOT NULL, reason TEXT NOT NULL,
                   attempts INTEGER NOT NULL, recovery_token TEXT NOT NULL UNIQUE,
                   created_at TEXT NOT NULL, updated_at TEXT NOT NULL
               );
               CREATE INDEX idx_studycrew_live_recovery_group
                 ON studycrew_live_recovery(group_id, updated_at);""",
        )
        for index in range(2):
            conn.execute(
                "INSERT INTO studycrew_live_recovery VALUES (?, ?, ?, ?, 4, ?, ?, ?)",
                (
                    f"run_{index:032x}", f"grp_{1:032x}", index + 1, f"reason-{index}",
                    f"recovery_{index:032x}", f"2026-08-19T00:00:0{index}Z",
                    f"2026-08-19T00:00:0{index}Z",
                ),
            )
    store = TaskRuntimeStore(database)
    rows = store.live_recoveries()
    assert len(rows) == 1
    assert rows[0]["group_id"] == f"grp_{1:032x}"
    assert rows[0]["generation"] == rows[0]["failure_count"] == 2
    assert rows[0]["through_seq"] == 2
    store.close()


@pytest.mark.asyncio
async def test_real_websocket_retries_same_token_until_renderer_ack(tmp_path: Path) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    project_id = "project_20260819000001"
    group = server.groups.create_group("Real ack", legacy_project_id=project_id)
    run = server.task_runtime.submit(group.group_id, title="Recover", request_key="real-ack")
    token = opaque_id("recovery")
    server.task_runtime.store.record_live_recovery(
        run_id=run.run_id, group_id=group.group_id, through_seq=1,
        reason="real", attempts=4, recovery_token=token,
    )
    server.hub.get_or_create(project_id, "Real ack")
    async with serve(server.handle, "127.0.0.1", 0) as ws_server:
        server._start_task_runtime_event_bridge()  # noqa: SLF001
        port = ws_server.sockets[0].getsockname()[1]
        uri = f"ws://127.0.0.1:{port}"
        async with connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "replay_request", "project_id": project_id, "since_seq": 0,
            }))
            notice = await _receive_until(ws, "resync_required")
            await ws.send(json.dumps({
                "type": "request_snapshot", "project_id": notice["project_id"],
                "recovery_token": notice["recovery_token"],
                "recovery_generation": notice["recovery_generation"],
            }))
            # Drop before the response can be projected; the durable token must retry.
        assert server.task_runtime.store.live_recovery(group.group_id) is not None
        async with connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "replay_request", "project_id": project_id, "since_seq": 0,
            }))
            retry = await _receive_until(ws, "resync_required")
            assert retry["recovery_token"] == token
            await ws.send(json.dumps({
                "type": "request_snapshot", "project_id": retry["project_id"],
                "recovery_token": retry["recovery_token"],
                "recovery_generation": retry["recovery_generation"],
            }))
            response = await _receive_until(ws, "task_runtime_result")
            await ws.send(json.dumps({
                "type": "task_runtime_recovery_ack",
                "project_id": retry["project_id"], "group_id": group.group_id,
                "recovery_token": token,
                "recovery_generation": retry["recovery_generation"],
                "snapshot_cursor": response["snapshot_cursor"],
            }))
            await _wait_for(lambda: server.task_runtime.store.live_recovery(group.group_id) is None)
        second_token = opaque_id("recovery")
        server.task_runtime.store.record_live_recovery(
            run_id=run.run_id, group_id=group.group_id, through_seq=1,
            reason="ack-lost", attempts=4, recovery_token=second_token,
        )
        async with connect(uri) as ws:
            await ws.send(json.dumps({
                "type": "replay_request", "project_id": project_id, "since_seq": 0,
            }))
            notice = await _receive_until(ws, "resync_required")
            await ws.send(json.dumps({
                "type": "request_snapshot", "project_id": notice["project_id"],
                "recovery_token": notice["recovery_token"],
                "recovery_generation": notice["recovery_generation"],
            }))
            await _receive_until(ws, "task_runtime_result")
            # Projection succeeded, but the ACK frame is lost with this connection.
    await _close(server)

    restarted = KnoweServer(str(tmp_path))
    assert restarted.task_runtime is not None
    restarted.hub.get_or_create(project_id, "Real ack")
    async with serve(restarted.handle, "127.0.0.1", 0) as ws_server:
        restarted._start_task_runtime_event_bridge()  # noqa: SLF001
        port = ws_server.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(json.dumps({
                "type": "replay_request", "project_id": project_id, "since_seq": 0,
            }))
            retry = await _receive_until(ws, "resync_required")
            assert retry["recovery_token"] == second_token
            await ws.send(json.dumps({
                "type": "request_snapshot", "project_id": project_id,
                "recovery_token": second_token,
                "recovery_generation": retry["recovery_generation"],
            }))
            response = await _receive_until(ws, "task_runtime_result")
            await ws.send(json.dumps({
                "type": "task_runtime_recovery_ack", "project_id": project_id,
                "group_id": group.group_id, "recovery_token": second_token,
                "recovery_generation": retry["recovery_generation"],
                "snapshot_cursor": response["snapshot_cursor"],
            }))
            await _wait_for(
                lambda: restarted.task_runtime.store.live_recovery(group.group_id) is None,
            )
    await _close(restarted)


@pytest.mark.asyncio
async def test_real_websocket_two_clients_keep_independent_recovery_obligations(
    tmp_path: Path,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    project_id = "project_20260819000002"
    group = server.groups.create_group("Real multi", legacy_project_id=project_id)
    run = server.task_runtime.submit(group.group_id, title="Recover", request_key="real-multi")
    event = server.task_runtime.store.events_after(run.run_id, 0)[0]
    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(event, reason="multi", attempts=4)  # noqa: SLF001
    server.hub.get_or_create(project_id, "Real multi")
    async with serve(server.handle, "127.0.0.1", 0) as ws_server:
        server._start_task_runtime_event_bridge()  # noqa: SLF001
        port = ws_server.sockets[0].getsockname()[1]
        uri = f"ws://127.0.0.1:{port}"
        async with connect(uri) as first, connect(uri) as second:
            for ws in (first, second):
                await ws.send(json.dumps({
                    "type": "replay_request", "project_id": project_id, "since_seq": 0,
                }))
            notices = [
                await _receive_until(first, "resync_required"),
                await _receive_until(second, "resync_required"),
            ]
            for ws, notice in zip((first, second), notices, strict=True):
                await ws.send(json.dumps({
                    "type": "request_snapshot", "project_id": project_id,
                    "recovery_token": notice["recovery_token"],
                    "recovery_generation": notice["recovery_generation"],
                }))
            responses = [
                await _receive_until(first, "task_runtime_result"),
                await _receive_until(second, "task_runtime_result"),
            ]
            await first.send(json.dumps({
                "type": "task_runtime_recovery_ack", "project_id": project_id,
                "group_id": group.group_id,
                "recovery_token": responses[0]["recovery_token"],
                "recovery_generation": responses[0]["recovery_generation"],
                "snapshot_cursor": responses[0]["snapshot_cursor"],
            }))
            await asyncio.sleep(0.05)
            assert server.task_runtime.store.live_recovery(group.group_id) is not None
            await second.send(json.dumps({
                "type": "task_runtime_recovery_ack", "project_id": project_id,
                "group_id": group.group_id,
                "recovery_token": responses[1]["recovery_token"],
                "recovery_generation": responses[1]["recovery_generation"],
                "snapshot_cursor": responses[1]["snapshot_cursor"],
            }))
            await _wait_for(lambda: server.task_runtime.store.live_recovery(group.group_id) is None)
    await _close(server)
