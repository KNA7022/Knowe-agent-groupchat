from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.hub import Client
from backend.server import KnoweServer
from backend.tests.test_task_runtime_recovery_ack import _OpenSocket, _close, _wait_for


def _assert_window(client: Client) -> None:
    phase_total = (
        len(client.task_runtime_recovery_reserved)
        + len(client.task_runtime_recovery_notified)
        + len(client.task_runtime_recovery_snapshots)
        + len(client.task_runtime_recovery_applied)
    )
    assert phase_total == client.task_runtime_recovery_slot_count
    assert phase_total <= client.TASK_RUNTIME_RECOVERY_WINDOW_MAX
    assert len(client.task_runtime_recovery_revisions) <= phase_total
    assert len(client.task_runtime_recovery_order) <= phase_total
    assert len(client.task_runtime_recovery_send_tasks) <= phase_total
    assert len(client.task_runtime_recovery_send_deadlines) <= phase_total


def _seed(server: KnoweServer, count: int) -> list[str]:
    assert server.task_runtime is not None
    groups: list[str] = []
    for index in range(count):
        group_id = f"grp_{index:032x}"
        groups.append(group_id)
        server.task_runtime.store.record_live_recovery(
            run_id=f"run_{index:032x}",
            group_id=group_id,
            through_seq=1,
            reason="ownership-probe",
            attempts=4,
            recovery_token=f"recovery_{index:032x}",
        )
    return groups


@pytest.mark.asyncio
async def test_cancelled_notice_send_keeps_one_bounded_owner_then_refills_four(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_SEND_TIMEOUT_SECONDS", 0.05)
    groups = _seed(server, 8)
    monkeypatch.setattr(
        server, "_runtime_project_id_for_group", lambda group_id: f"project_{group_id[4:]}",
    )
    client = Client(_OpenSocket())
    server.hub.add_client(client)
    await client.release_live_replay_fence()
    original_send = server.hub.send_to
    entered = asyncio.Event()

    async def suspended_send(target: Client, payload: dict[str, object]) -> bool:
        entered.set()
        await asyncio.Future()
        return await original_send(target, payload)

    monkeypatch.setattr(server.hub, "send_to", suspended_send)
    fill = asyncio.create_task(server._notify_task_runtime_recoveries(client))  # noqa: SLF001
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert len(client.task_runtime_recovery_reserved) == 1
    _assert_window(client)
    assert client.ws.messages == []

    fill.cancel()
    with pytest.raises(asyncio.CancelledError):
        await fill
    assert not client.task_runtime_recovery_fill_lock.locked()
    # The transferred ambiguous owner remains bounded only until its original deadline.
    assert len(client.task_runtime_recovery_reserved) == 1
    _assert_window(client)
    assert client.ws.messages == []

    await _wait_for(lambda: client.task_runtime_recovery_fenced, timeout=1)
    await _wait_for(
        lambda: not server._task_runtime_recovery_reconcile_tasks, timeout=1,  # noqa: SLF001
    )
    await _wait_for(
        lambda: not server._task_runtime_recovery_write_tasks, timeout=1,  # noqa: SLF001
    )
    assert client.task_runtime_recovery_slot_count == 0
    assert client not in server.hub.clients
    assert server.task_runtime is not None
    assert len(server.task_runtime.store.live_recoveries()) == 8
    monkeypatch.setattr(server.hub, "send_to", original_send)
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_SEND_TIMEOUT_SECONDS", 1.0)
    replacement = Client(_OpenSocket())
    server.hub.add_client(replacement)
    await replacement.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(replacement)  # noqa: SLF001
    _assert_window(replacement)
    assert replacement.task_runtime_recovery_slot_count == 4
    notices = [
        row for row in replacement.ws.messages if row.get("type") == "resync_required"
    ]
    assert len(notices) == 4
    assert notices[0]["group_id"] == groups[0]
    await _close(server)


@pytest.mark.asyncio
async def test_shutdown_cancels_inflight_reservation_without_stranding_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    groups = _seed(server, 1)
    monkeypatch.setattr(server, "_runtime_project_id_for_group", lambda _group: "project_one")
    client = Client(_OpenSocket())
    server.hub.add_client(client)
    await client.release_live_replay_fence()
    entered = asyncio.Event()

    async def never_sent(_target: Client, _payload: dict[str, object]) -> bool:
        entered.set()
        await asyncio.Future()
        return True

    monkeypatch.setattr(server.hub, "send_to", never_sent)
    fill = asyncio.create_task(server._notify_task_runtime_recoveries(client))  # noqa: SLF001
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert client.task_runtime_recovery_slot_count == 1
    await server._stop_task_runtime_event_bridge()  # noqa: SLF001
    await asyncio.wait_for(fill, timeout=2)
    assert client.task_runtime_recovery_slot_count == 0
    assert not client.task_runtime_recovery_fill_lock.locked()
    assert server.task_runtime is not None
    assert server.task_runtime.store.live_recovery(groups[0]) is not None
    server.task_runtime.close()


@pytest.mark.asyncio
async def test_notice_timeout_rolls_back_exact_slot_and_next_fill_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    groups = _seed(server, 1)
    monkeypatch.setattr(server, "_runtime_project_id_for_group", lambda _group: "project_timeout")
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_SEND_TIMEOUT_SECONDS", 0.01)
    client = Client(_OpenSocket())
    server.hub.add_client(client)
    await client.release_live_replay_fence()
    original_send = server.hub.send_to

    async def hangs(_target: Client, _payload: dict[str, object]) -> bool:
        await asyncio.Future()
        return True

    monkeypatch.setattr(server.hub, "send_to", hangs)
    await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
    assert client.task_runtime_recovery_slot_count == 0
    assert not client.task_runtime_recovery_fill_lock.locked()
    assert server.task_runtime is not None
    assert server.task_runtime.store.live_recovery(groups[0]) is not None
    assert client.task_runtime_recovery_fenced
    assert client not in server.hub.clients
    monkeypatch.setattr(server.hub, "send_to", original_send)
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_SEND_TIMEOUT_SECONDS", 1.0)
    replacement = Client(_OpenSocket())
    server.hub.add_client(replacement)
    await replacement.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(replacement)  # noqa: SLF001
    assert replacement.task_runtime_recovery_slot_count == 1
    assert len(replacement.ws.messages) == 1
    await _close(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["false", "exception"])
async def test_notice_failure_rolls_back_without_fencing_healthy_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_mode: str,
) -> None:
    server = KnoweServer(str(tmp_path))
    _seed(server, 4)
    monkeypatch.setattr(
        server, "_runtime_project_id_for_group", lambda group_id: f"project_{group_id[4:]}",
    )
    client = Client(_OpenSocket())
    server.hub.add_client(client)
    await client.release_live_replay_fence()
    original_send = server.hub.send_to

    async def fail(_target: Client, _payload: dict[str, object]) -> bool:
        if failure_mode == "exception":
            raise OSError("socket failed before handoff")
        return False

    monkeypatch.setattr(server.hub, "send_to", fail)
    await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
    assert client.alive
    assert client.task_runtime_recovery_slot_count == 0
    assert not client.task_runtime_recovery_fill_lock.locked()
    assert server.task_runtime is not None
    assert len(server.task_runtime.store.live_recoveries()) == 4
    monkeypatch.setattr(server.hub, "send_to", original_send)
    await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
    assert client.task_runtime_recovery_slot_count == 4
    await _close(server)


@pytest.mark.asyncio
async def test_cancelled_snapshot_write_retains_bounded_issued_owner_until_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_SEND_TIMEOUT_SECONDS", 0.05)
    assert server.groups is not None and server.task_runtime is not None
    project_id = "project_snapshot_cancel"
    group = server.groups.create_group("Snapshot cancel", legacy_project_id=project_id)
    run = server.task_runtime.submit(group.group_id, title="Recover", request_key="snap-cancel")
    event = server.task_runtime.store.events_after(run.run_id, 0)[0]
    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(event, reason="snapshot", attempts=4)  # noqa: SLF001
    client = Client(_OpenSocket())
    server.hub.add_client(client)
    await client.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
    notice = next(row for row in client.ws.messages if row.get("type") == "resync_required")
    original_send = server.hub.send_to
    entered = asyncio.Event()

    async def suspend_snapshot(target: Client, payload: dict[str, object]) -> bool:
        if payload.get("type") == "task_runtime_result":
            entered.set()
            await asyncio.Future()
        return await original_send(target, payload)

    monkeypatch.setattr(server.hub, "send_to", suspend_snapshot)
    request = asyncio.create_task(server._cmd_request_snapshot(client, {  # noqa: SLF001
        "project_id": project_id,
        "recovery_token": notice["recovery_token"],
        "recovery_generation": notice["recovery_generation"],
    }))
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert len(client.task_runtime_recovery_snapshots) == 1
    _assert_window(client)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert len(client.task_runtime_recovery_snapshots) == 1
    _assert_window(client)
    await _wait_for(lambda: client.task_runtime_recovery_fenced, timeout=1)
    await _wait_for(
        lambda: not server._task_runtime_recovery_reconcile_tasks, timeout=1,  # noqa: SLF001
    )
    await _wait_for(
        lambda: not server._task_runtime_recovery_write_tasks, timeout=1,  # noqa: SLF001
    )
    assert client.task_runtime_recovery_slot_count == 0
    assert client not in server.hub.clients
    assert server.task_runtime.store.live_recovery(group.group_id) is not None
    monkeypatch.setattr(server.hub, "send_to", original_send)
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_SEND_TIMEOUT_SECONDS", 1.0)
    replacement = Client(_OpenSocket())
    server.hub.add_client(replacement)
    await replacement.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(replacement)  # noqa: SLF001
    assert replacement.task_runtime_recovery_slot_count == 1
    assert any(row.get("type") == "resync_required" for row in replacement.ws.messages)
    await _close(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("group_count", [8, 100, 513])
async def test_three_staggered_clients_remain_four_slots_and_retire_every_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, group_count: int,
) -> None:
    server = KnoweServer(str(tmp_path))
    expected = set(_seed(server, group_count))
    monkeypatch.setattr(
        server, "_runtime_project_id_for_group", lambda group_id: f"project_{group_id[4:]}",
    )
    clients = [Client(_OpenSocket()) for _ in range(3)]
    for client in clients:
        server.hub.add_client(client)
        await client.release_live_replay_fence()
        await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
        assert client.task_runtime_recovery_slot_count == min(4, group_count)
        _assert_window(client)

    applied_by_client: list[set[str]] = [set(), set(), set()]

    async def drain(client_index: int) -> None:
        client = clients[client_index]
        while client.task_runtime_recovery_notified:
            key = min(
                client.task_runtime_recovery_notified,
                key=lambda item: client.task_runtime_recovery_revisions[item],
            )
            cursor = f"{client_index + 1:064x}"
            client.task_runtime_recovery_notified.remove(key)
            client.task_runtime_recovery_snapshots[key] = cursor
            assert server._ack_task_runtime_recovery(  # noqa: SLF001
                client,
                group_id=key[0],
                recovery_token=key[1],
                generation=key[2],
                snapshot_cursor=cursor,
            )
            applied_by_client[client_index].add(key[0])
            _assert_window(client)
            await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
            _assert_window(client)

    # The fast client cannot retire the suffix before both slow live pagers traverse it.
    await drain(0)
    assert applied_by_client[0] == expected
    assert clients[0].task_runtime_recovery_applied == set()
    assert clients[1].task_runtime_recovery_slot_count == min(4, group_count)
    assert clients[2].task_runtime_recovery_slot_count == min(4, group_count)
    assert len(server.task_runtime.store.live_recoveries()) == group_count
    await drain(1)
    assert applied_by_client[1] == expected
    assert len(server.task_runtime.store.live_recoveries()) == group_count
    await drain(2)
    assert applied_by_client[2] == expected
    assert server.task_runtime is not None
    assert server.task_runtime.store.live_recoveries() == ()
    for client in clients:
        _assert_window(client)
        assert client.task_runtime_recovery_slot_count == 0
    await _close(server)


@pytest.mark.asyncio
async def test_generation_update_keeps_queue_position_and_cannot_skip_fifth_group(
    tmp_path: Path,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    client = Client(_OpenSocket())
    server.hub.add_client(client)
    await client.release_live_replay_fence()
    groups = []
    events = []
    for index in range(5):
        project_id = f"project_queue_{index}"
        group = server.groups.create_group(
            f"Queue {index}", legacy_project_id=project_id,
        )
        run = server.task_runtime.submit(
            group.group_id, title=f"Queue {index}", request_key=f"queue-{index}",
        )
        event = server.task_runtime.store.events_after(run.run_id, 0)[0]
        groups.append(group)
        events.append(event)
        with server._task_runtime_event_bridge_lock:  # noqa: SLF001
            server._record_task_runtime_recovery_locked(  # noqa: SLF001
                event, reason="queue", attempts=4,
            )

    initial_rows = server.task_runtime.store.live_recoveries()
    initial_positions = {
        str(row["group_id"]): int(row["recovery_revision"]) for row in initial_rows
    }
    await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
    notices = [
        row for row in client.ws.messages if row.get("type") == "resync_required"
    ]
    assert [row["group_id"] for row in notices] == [
        group.group_id for group in groups[:4]
    ]

    # Advance the oldest in-window Group several generations. Its queue position is
    # immutable, so the unrelated fifth Group cannot fall behind a moving cursor.
    for generation in range(2, 5):
        with server._task_runtime_event_bridge_lock:  # noqa: SLF001
            server._record_task_runtime_recovery_locked(  # noqa: SLF001
                events[0], reason=f"queue-gen-{generation}", attempts=4,
            )
    updated = server.task_runtime.store.live_recovery(groups[0].group_id)
    assert updated is not None
    assert int(updated["generation"]) == 4
    assert int(updated["recovery_revision"]) == initial_positions[groups[0].group_id]

    for notice in notices:
        await server._cmd_request_snapshot(client, {  # noqa: SLF001
            "project_id": notice["project_id"],
            "recovery_token": notice["recovery_token"],
            "recovery_generation": notice["recovery_generation"],
        })
        response = next(
            row for row in reversed(client.ws.messages)
            if row.get("type") == "task_runtime_result"
            and row.get("project_id") == notice["project_id"]
        )
        await server._cmd_task_runtime_recovery_ack(client, {  # noqa: SLF001
            "project_id": notice["project_id"],
            "group_id": notice["group_id"],
            "recovery_token": response["recovery_token"],
            "recovery_generation": response["recovery_generation"],
            "snapshot_cursor": response["snapshot_cursor"],
        })
        _assert_window(client)

    fifth_id = groups[4].group_id
    assert client.task_runtime_recovery_ack_revision == initial_positions[groups[3].group_id]
    remaining = server.task_runtime.store.live_recoveries()
    assert [row["group_id"] for row in remaining] == [fifth_id]
    fifth_notice = next(
        row for row in client.ws.messages
        if row.get("type") == "resync_required" and row.get("group_id") == fifth_id
    )
    await server._cmd_request_snapshot(client, {  # noqa: SLF001
        "project_id": fifth_notice["project_id"],
        "recovery_token": fifth_notice["recovery_token"],
        "recovery_generation": fifth_notice["recovery_generation"],
    })
    fifth_response = next(
        row for row in reversed(client.ws.messages)
        if row.get("type") == "task_runtime_result"
        and row.get("project_id") == fifth_notice["project_id"]
    )
    await server._cmd_task_runtime_recovery_ack(client, {  # noqa: SLF001
        "project_id": fifth_notice["project_id"], "group_id": fifth_id,
        "recovery_token": fifth_response["recovery_token"],
        "recovery_generation": fifth_response["recovery_generation"],
        "snapshot_cursor": fifth_response["snapshot_cursor"],
    })
    assert server.task_runtime.store.live_recoveries() == ()
    await _close(server)


@pytest.mark.asyncio
async def test_disconnect_transfer_is_one_shot_and_replacement_claims_exact_debt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    expected = set(_seed(server, 8))
    assert server.task_runtime is not None
    monkeypatch.setattr(
        server, "_runtime_project_id_for_group", lambda group_id: f"project_{group_id[4:]}",
    )
    old = Client(_OpenSocket())
    server.hub.add_client(old)
    await old.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(old)  # noqa: SLF001
    assert old.task_runtime_recovery_disconnect_epoch_baseline == 0

    server._retain_client_recovery_on_disconnect(old)  # noqa: SLF001
    server.hub.remove_client(old)
    first_transfer = {
        str(row["group_id"]): (
            int(row["disconnect_debt_generation"]), str(row["disconnect_fence_id"]),
        )
        for row in server.task_runtime.store.live_recoveries()
    }
    assert set(first_transfer) == expected
    assert {value[1] for value in first_transfer.values()} == {
        old.task_runtime_recovery_session_id,
    }

    replacements = [Client(_OpenSocket()), Client(_OpenSocket())]
    for replacement in replacements:
        server.hub.add_client(replacement)
        await replacement.release_live_replay_fence()
        await server._notify_task_runtime_recoveries(replacement)  # noqa: SLF001
        assert replacement.task_runtime_recovery_disconnect_epoch_baseline == 1

    # This is the old handler's delayed ``finally`` after the replacement joined.
    server._retain_client_recovery_on_disconnect(old)  # noqa: SLF001
    second_transfer = {
        str(row["group_id"]): (
            int(row["disconnect_debt_generation"]), str(row["disconnect_fence_id"]),
        )
        for row in server.task_runtime.store.live_recoveries()
    }
    assert second_transfer == first_transfer
    first = server.task_runtime.store.live_recoveries()[0]
    assert not server.task_runtime.store.consume_live_recovery_disconnect(
        str(first["group_id"]), str(first["recovery_token"]), int(first["generation"]),
        claim_id="forged-pre-debt-session", claim_after_epoch=0,
    )

    async def drain(replacement: Client, cursor: str) -> set[str]:
        applied: set[str] = set()
        while replacement.task_runtime_recovery_notified:
            key = min(
                replacement.task_runtime_recovery_notified,
                key=lambda item: replacement.task_runtime_recovery_revisions[item],
            )
            replacement.task_runtime_recovery_notified.remove(key)
            replacement.task_runtime_recovery_snapshots[key] = cursor
            assert server._ack_task_runtime_recovery(  # noqa: SLF001
                replacement, group_id=key[0], recovery_token=key[1],
                generation=key[2], snapshot_cursor=cursor,
            )
            applied.add(key[0])
            _assert_window(replacement)
            await server._notify_task_runtime_recoveries(replacement)  # noqa: SLF001
        return applied

    assert await drain(replacements[0], "a" * 64) == expected
    assert len(server.task_runtime.store.live_recoveries()) == len(expected)
    assert await drain(replacements[1], "b" * 64) == expected
    assert server.task_runtime.store.live_recoveries() == ()
    assert old.task_runtime_recovery_slot_count == 0
    await _close(server)


def test_recovery_revision_never_reuses_after_full_table_retirement(tmp_path: Path) -> None:
    server = KnoweServer(str(tmp_path))
    first = _seed(server, 8)
    assert server.task_runtime is not None
    first_rows = server.task_runtime.store.live_recoveries()
    first_high_water = max(int(row["recovery_revision"]) for row in first_rows)
    for row in first_rows:
        assert server.task_runtime.store.clear_live_recovery(
            str(row["group_id"]), str(row["recovery_token"]), int(row["generation"]),
        )
    assert set(first) and server.task_runtime.store.live_recoveries() == ()
    second = _seed(server, 100)
    second_rows = server.task_runtime.store.live_recoveries()
    assert len(second) == 100
    assert min(int(row["recovery_revision"]) for row in second_rows) > first_high_water
    server.task_runtime.close()
