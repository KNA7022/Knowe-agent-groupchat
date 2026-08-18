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
    assert len(client.task_runtime_recovery_kinds) <= phase_total
    assert len(client.task_runtime_recovery_order) <= phase_total
    assert len(client.task_runtime_recovery_update_order) <= phase_total
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
    server._admit_task_runtime_client(client)  # noqa: SLF001
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
    server._admit_task_runtime_client(replacement)  # noqa: SLF001
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
    server._admit_task_runtime_client(client)  # noqa: SLF001
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
    server._admit_task_runtime_client(client)  # noqa: SLF001
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
    server._admit_task_runtime_client(replacement)  # noqa: SLF001
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
    server._admit_task_runtime_client(client)  # noqa: SLF001
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
    server._admit_task_runtime_client(client)  # noqa: SLF001
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
    server._admit_task_runtime_client(replacement)  # noqa: SLF001
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
        server._admit_task_runtime_client(client)  # noqa: SLF001
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
@pytest.mark.parametrize("group_count", [8, 513])
async def test_dirty_generation_stream_coalesces_changed_group_without_suffix_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, group_count: int,
) -> None:
    server = KnoweServer(str(tmp_path))
    groups = _seed(server, group_count)
    assert server.task_runtime is not None
    monkeypatch.setattr(
        server, "_runtime_project_id_for_group", lambda group_id: f"project_{group_id[4:]}",
    )
    clients = [Client(_OpenSocket()) for _ in range(3)]
    for client in clients:
        server._admit_task_runtime_client(client)  # noqa: SLF001
        await client.release_live_replay_fence()
        await server._notify_task_runtime_recoveries(client)  # noqa: SLF001

    async def drain(client: Client) -> tuple[set[str], set[str]]:
        base_seen: set[str] = set()
        update_seen: set[str] = set()
        while client.task_runtime_recovery_notified:
            key = min(
                client.task_runtime_recovery_notified,
                key=lambda item: client.task_runtime_recovery_revisions[item],
            )
            marker = server.task_runtime.store.live_recovery(key[0])
            assert marker is not None
            kind = client.task_runtime_recovery_kinds.get(key, "base")
            if int(marker["generation"]) != key[2]:
                # This direct pager probe models the production snapshot CAS replacing
                # an old notice with the latest generation at the same owned slot.
                replacement = (key[0], key[1], int(marker["generation"]))
                with server._task_runtime_event_bridge_lock:  # noqa: SLF001
                    revision = client.task_runtime_recovery_revisions[key]
                    server._drop_client_recovery_key_locked(client, key)  # noqa: SLF001
                    client.task_runtime_recovery_notified.add(replacement)
                    client.task_runtime_recovery_revisions[replacement] = revision
                    client.task_runtime_recovery_kinds[replacement] = kind
                    server._normalize_client_recovery_slots_locked(client)  # noqa: SLF001
                key = replacement
            cursor = f"{client.id}:{key[0]}:{key[2]}".encode().hex()[:64].ljust(64, "0")
            client.task_runtime_recovery_notified.remove(key)
            client.task_runtime_recovery_snapshots[key] = cursor
            assert server._ack_task_runtime_recovery(  # noqa: SLF001
                client, group_id=key[0], recovery_token=key[1],
                generation=key[2], snapshot_cursor=cursor,
            )
            (update_seen if kind == "update" else base_seen).add(key[0])
            _assert_window(client)
            await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
        return base_seen, update_seen

    fast_base, fast_updates = await drain(clients[0])
    assert fast_base == set(groups) and fast_updates == set()
    assert len(server.task_runtime.store.live_recoveries()) == group_count
    fast_notice_count = sum(
        row.get("type") == "resync_required" for row in clients[0].ws.messages
    )

    first = server.task_runtime.store.live_recovery(groups[0])
    assert first is not None
    for generation in range(2, 8):
        updated = server.task_runtime.store.record_live_recovery(
            run_id=str(first["affected_run_id"]), group_id=groups[0],
            through_seq=generation, reason=f"dirty-{generation}", attempts=4,
            recovery_token=str(first["recovery_token"]), run_version=generation,
        )
        assert int(updated["generation"]) == generation
    dirty = server.task_runtime.store.live_recovery_dirty()
    assert len(dirty) == 1
    assert dirty[0]["group_id"] == groups[0] and int(dirty[0]["generation"]) == 7

    await server._notify_task_runtime_recoveries(clients[0])  # noqa: SLF001
    new_fast_notices = [
        row for row in clients[0].ws.messages[fast_notice_count:]
        if row.get("type") == "resync_required"
    ]
    assert [(row["group_id"], row["recovery_generation"]) for row in new_fast_notices] == [
        (groups[0], 7),
    ]
    # Further advances while that changed-Group notice is in flight coalesce into
    # the same owned slot; capture upgrades it to the newest tail revision.
    for generation in (8, 9):
        server.task_runtime.store.record_live_recovery(
            run_id=str(first["affected_run_id"]), group_id=groups[0],
            through_seq=generation, reason=f"dirty-{generation}", attempts=4,
            recovery_token=str(first["recovery_token"]), run_version=generation,
        )
    second_base, second_updates = await drain(clients[0])
    assert second_base == set() and second_updates == {groups[0]}
    changed_notices = [
        row for row in clients[0].ws.messages
        if row.get("type") == "resync_required" and row.get("group_id") == groups[0]
    ]
    assert [row["recovery_generation"] for row in changed_notices[-2:]] == [7, 9]
    assert len(server.task_runtime.store.live_recoveries()) == group_count

    slow_a_base, _slow_a_updates = await drain(clients[1])
    assert slow_a_base == set(groups)
    assert len(server.task_runtime.store.live_recoveries()) == group_count
    slow_b_base, _slow_b_updates = await drain(clients[2])
    assert slow_b_base == set(groups)
    assert server.task_runtime.store.live_recovery_dirty() == ()
    assert server.task_runtime.store.live_recoveries() == ()
    for client in clients:
        _assert_window(client)
        assert client.task_runtime_recovery_slot_count == 0
    await _close(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("group_count", [8, 513])
async def test_frozen_dirty_entry_appends_without_skipping_intervening_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, group_count: int,
) -> None:
    server = KnoweServer(str(tmp_path))
    groups = _seed(server, group_count)
    assert server.task_runtime is not None
    monkeypatch.setattr(
        server, "_runtime_project_id_for_group", lambda group_id: f"project_{group_id[4:]}",
    )
    fast, slow = Client(_OpenSocket()), Client(_OpenSocket())
    for client in (fast, slow):
        server._admit_task_runtime_client(client)  # noqa: SLF001
        await client.release_live_replay_fence()
        await server._notify_task_runtime_recoveries(client)  # noqa: SLF001

    async def ack_one(client: Client) -> tuple[str, int, str]:
        key = min(
            client.task_runtime_recovery_notified,
            key=lambda item: client.task_runtime_recovery_revisions[item],
        )
        kind = client.task_runtime_recovery_kinds.get(key, "base")
        revision = client.task_runtime_recovery_revisions[key]
        marker = server.task_runtime.store.live_recovery(key[0])
        assert marker is not None
        if int(marker["generation"]) != key[2]:
            replacement = (key[0], key[1], int(marker["generation"]))
            with server._task_runtime_event_bridge_lock:  # noqa: SLF001
                server._drop_client_recovery_key_locked(client, key)  # noqa: SLF001
                client.task_runtime_recovery_notified.add(replacement)
                client.task_runtime_recovery_revisions[replacement] = revision
                client.task_runtime_recovery_kinds[replacement] = kind
                server._normalize_client_recovery_slots_locked(client)  # noqa: SLF001
            key = replacement
        cursor = f"{client.id}:{kind}:{revision}".encode().hex().ljust(64, "0")[:64]
        client.task_runtime_recovery_notified.remove(key)
        client.task_runtime_recovery_snapshots[key] = cursor
        assert server._ack_task_runtime_recovery(  # noqa: SLF001
            client, group_id=key[0], recovery_token=key[1],
            generation=key[2], snapshot_cursor=cursor,
        )
        _assert_window(client)
        return kind, revision, key[0]

    fast_base: set[str] = set()
    while fast.task_runtime_recovery_notified:
        kind, _revision, group_id = await ack_one(fast)
        assert kind == "base"
        fast_base.add(group_id)
        await server._notify_task_runtime_recoveries(fast)  # noqa: SLF001
    assert fast_base == set(groups)
    assert len(server.task_runtime.store.live_recoveries()) == group_count

    for group_id in groups[:5]:
        current = server.task_runtime.store.live_recovery(group_id)
        assert current is not None
        server.task_runtime.store.record_live_recovery(
            run_id=str(current["affected_run_id"]), group_id=group_id,
            through_seq=2, reason="five-dirty", attempts=4,
            recovery_token=str(current["recovery_token"]), run_version=2,
        )
    await server._notify_task_runtime_recoveries(fast)  # noqa: SLF001
    assert sorted(fast.task_runtime_recovery_revisions.values()) == [1, 2, 3, 4]
    assert [int(row["dirty_frozen"]) for row in server.task_runtime.store.live_recovery_dirty()] == [
        1, 1, 1, 1, 0,
    ]

    first = server.task_runtime.store.live_recovery(groups[0])
    assert first is not None
    server.task_runtime.store.record_live_recovery(
        run_id=str(first["affected_run_id"]), group_id=groups[0],
        through_seq=3, reason="append-after-issue", attempts=4,
        recovery_token=str(first["recovery_token"]), run_version=3,
    )
    dirty_rows = server.task_runtime.store.live_recovery_dirty()
    assert [int(row["dirty_queue_revision"]) for row in dirty_rows] == [1, 2, 3, 4, 5, 6]
    assert [int(row["generation"]) for row in dirty_rows] == [2, 2, 2, 2, 2, 3]

    fast_dirty: list[int] = []
    for expected_revision in (1, 2, 3, 4):
        kind, revision, _group_id = await ack_one(fast)
        assert kind == "update" and revision == expected_revision
        fast_dirty.append(revision)
    assert fast.task_runtime_recovery_update_ack_revision == 4
    await server._notify_task_runtime_recoveries(fast)  # noqa: SLF001
    assert sorted(fast.task_runtime_recovery_revisions.values()) == [5, 6]
    for expected_revision in (5, 6):
        kind, revision, _group_id = await ack_one(fast)
        assert kind == "update" and revision == expected_revision
        fast_dirty.append(revision)
    assert fast_dirty == [1, 2, 3, 4, 5, 6]
    assert fast.task_runtime_recovery_update_ack_revision == 6
    assert len(server.task_runtime.store.live_recoveries()) == group_count

    slow_base: set[str] = set()
    slow_updates: list[int] = []
    while slow.task_runtime_recovery_notified:
        kind, revision, group_id = await ack_one(slow)
        if kind == "base":
            slow_base.add(group_id)
        else:
            slow_updates.append(revision)
        await server._notify_task_runtime_recoveries(slow)  # noqa: SLF001
    assert slow_base == set(groups)
    assert slow.task_runtime_recovery_update_ack_revision == 6
    assert slow_updates in ([], [6])
    assert server.task_runtime.store.live_recovery_dirty() == ()
    assert server.task_runtime.store.live_recoveries() == ()
    _assert_window(fast)
    _assert_window(slow)
    await _close(server)


@pytest.mark.asyncio
async def test_generation_update_keeps_queue_position_and_cannot_skip_fifth_group(
    tmp_path: Path,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    client = Client(_OpenSocket())
    server._admit_task_runtime_client(client)  # noqa: SLF001
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
    server._admit_task_runtime_client(old)  # noqa: SLF001
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
        server._admit_task_runtime_client(replacement)  # noqa: SLF001
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


@pytest.mark.asyncio
async def test_session_admission_baseline_prevents_late_predebt_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    groups = _seed(server, 4)
    assert server.task_runtime is not None
    monkeypatch.setattr(
        server, "_runtime_project_id_for_group", lambda group_id: f"project_{group_id[4:]}",
    )
    old = Client(_OpenSocket())
    predebt = Client(_OpenSocket())
    server._admit_task_runtime_client(old)  # noqa: SLF001
    server._admit_task_runtime_client(predebt)  # noqa: SLF001
    assert old.task_runtime_recovery_disconnect_epoch_baseline == 0
    assert predebt.task_runtime_recovery_disconnect_epoch_baseline == 0
    await old.release_live_replay_fence()
    await predebt.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(old)  # noqa: SLF001

    server._retain_client_recovery_on_disconnect(old)  # noqa: SLF001
    server.hub.remove_client(old)
    assert server.task_runtime.store.live_recovery_disconnect_epoch() == 1
    await server._notify_task_runtime_recoveries(predebt)  # noqa: SLF001
    key = min(
        predebt.task_runtime_recovery_notified,
        key=lambda item: predebt.task_runtime_recovery_revisions[item],
    )
    cursor = "c" * 64
    predebt.task_runtime_recovery_notified.remove(key)
    predebt.task_runtime_recovery_snapshots[key] = cursor
    assert not server._ack_task_runtime_recovery(  # noqa: SLF001
        predebt, group_id=key[0], recovery_token=key[1],
        generation=key[2], snapshot_cursor=cursor,
    )
    assert server.task_runtime.store.live_recovery(key[0]) is not None

    server._retain_client_recovery_on_disconnect(predebt)  # noqa: SLF001
    server.hub.remove_client(predebt)
    assert server.task_runtime.store.live_recovery_disconnect_epoch() == 2
    replacement = Client(_OpenSocket())
    server._admit_task_runtime_client(replacement)  # noqa: SLF001
    assert replacement.task_runtime_recovery_disconnect_epoch_baseline == 2
    await replacement.release_live_replay_fence()
    # The old handler's delayed finally is idempotent and cannot move the epoch
    # beyond the baseline already captured by the admitted replacement.
    server._retain_client_recovery_on_disconnect(old)  # noqa: SLF001
    assert server.task_runtime.store.live_recovery_disconnect_epoch() == 2

    seen: set[str] = set()
    await server._notify_task_runtime_recoveries(replacement)  # noqa: SLF001
    while replacement.task_runtime_recovery_notified:
        current = min(
            replacement.task_runtime_recovery_notified,
            key=lambda item: replacement.task_runtime_recovery_revisions[item],
        )
        replacement.task_runtime_recovery_notified.remove(current)
        replacement.task_runtime_recovery_snapshots[current] = "d" * 64
        assert server._ack_task_runtime_recovery(  # noqa: SLF001
            replacement, group_id=current[0], recovery_token=current[1],
            generation=current[2], snapshot_cursor="d" * 64,
        )
        seen.add(current[0])
        _assert_window(replacement)
        await server._notify_task_runtime_recoveries(replacement)  # noqa: SLF001
    assert seen == set(groups)
    assert server.task_runtime.store.live_recoveries() == ()
    await _close(server)


@pytest.mark.asyncio
async def test_volatile_store_failure_uses_changed_group_stream_without_base_rewind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    project_id = "project_volatile_dirty"
    group = server.groups.create_group("Volatile dirty", legacy_project_id=project_id)
    run = server.task_runtime.submit(
        group.group_id, title="Volatile", request_key="volatile-dirty",
    )
    event = server.task_runtime.store.events_after(run.run_id, 0)[0]

    def unavailable(**_kwargs: object) -> dict[str, object]:
        raise OSError("injected recovery-table outage")

    monkeypatch.setattr(server.task_runtime.store, "record_live_recovery", unavailable)
    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(  # noqa: SLF001
            event, reason="volatile-one", attempts=4,
        )
    client = Client(_OpenSocket())
    server._admit_task_runtime_client(client)  # noqa: SLF001
    await client.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
    first = next(row for row in client.ws.messages if row.get("type") == "resync_required")
    await server._cmd_request_snapshot(client, {  # noqa: SLF001
        "project_id": project_id, "recovery_token": first["recovery_token"],
        "recovery_generation": first["recovery_generation"],
    })
    response = next(
        row for row in reversed(client.ws.messages)
        if row.get("type") == "task_runtime_result"
    )
    await server._cmd_task_runtime_recovery_ack(client, {  # noqa: SLF001
        "project_id": project_id, "group_id": group.group_id,
        "recovery_token": response["recovery_token"],
        "recovery_generation": response["recovery_generation"],
        "snapshot_cursor": response["snapshot_cursor"],
    })
    base_frontier = client.task_runtime_recovery_ack_revision

    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(  # noqa: SLF001
            event, reason="volatile-two", attempts=4,
        )
    await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
    notices = [
        row for row in client.ws.messages if row.get("type") == "resync_required"
    ]
    assert len(notices) == 2
    assert notices[-1]["group_id"] == group.group_id
    assert notices[-1]["recovery_generation"] == 2
    assert client.task_runtime_recovery_ack_revision == base_frontier
    assert next(iter(client.task_runtime_recovery_kinds.values())) == "update"
    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(  # noqa: SLF001
            event, reason="volatile-three", attempts=4,
        )
        hot = server._task_runtime_event_bridge_recovery[group.group_id]  # noqa: SLF001
        assert [
            (int(item["queue_revision"]), int(item["generation"]), bool(item["frozen"]))
            for item in hot["volatile_dirty_entries"]
        ] == [(1, 2, True), (2, 3, False)]
    _assert_window(client)
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


def test_dirty_revision_survives_restart_and_remains_monotonic(tmp_path: Path) -> None:
    server = KnoweServer(str(tmp_path))
    group_id = _seed(server, 1)[0]
    assert server.task_runtime is not None
    first = server.task_runtime.store.live_recovery(group_id)
    assert first is not None
    for generation in (2, 3):
        server.task_runtime.store.record_live_recovery(
            run_id=str(first["affected_run_id"]), group_id=group_id,
            through_seq=generation, reason="restart-dirty", attempts=4,
            recovery_token=str(first["recovery_token"]), run_version=generation,
        )
    before = server.task_runtime.store.live_recovery_dirty_marker(group_id)
    assert before is not None
    base_revision = int(before["recovery_revision"])
    dirty_revision = int(before["dirty_queue_revision"])
    server.task_runtime.close()

    restarted = KnoweServer(str(tmp_path))
    assert restarted.task_runtime is not None
    durable = restarted.task_runtime.store.live_recovery_dirty_marker(group_id)
    assert durable is not None
    assert int(durable["generation"]) == 3
    assert int(durable["recovery_revision"]) == base_revision
    assert int(durable["dirty_queue_revision"]) == dirty_revision
    advanced = restarted.task_runtime.store.record_live_recovery(
        run_id=str(first["affected_run_id"]), group_id=group_id,
        through_seq=4, reason="restart-dirty", attempts=4,
        recovery_token=str(first["recovery_token"]), run_version=4,
    )
    after = restarted.task_runtime.store.live_recovery_dirty_marker(group_id)
    assert int(advanced["generation"]) == 4
    assert after is not None
    assert int(after["recovery_revision"]) == base_revision
    assert int(after["dirty_queue_revision"]) == dirty_revision
    assert int(after["generation"]) == 4
    frozen = restarted.task_runtime.store.freeze_live_recovery_dirty(
        dirty_revision, group_id=group_id,
        recovery_token=str(after["recovery_token"]), generation=4,
    )
    assert frozen is not None and int(frozen["dirty_frozen"]) == 1
    restarted.task_runtime.store.record_live_recovery(
        run_id=str(first["affected_run_id"]), group_id=group_id,
        through_seq=5, reason="restart-dirty", attempts=4,
        recovery_token=str(first["recovery_token"]), run_version=5,
    )
    appended = restarted.task_runtime.store.live_recovery_dirty_marker(group_id)
    assert appended is not None
    assert int(appended["generation"]) == 5
    assert int(appended["dirty_queue_revision"]) > dirty_revision
    restarted.task_runtime.close()
