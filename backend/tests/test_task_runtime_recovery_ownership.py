from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from websockets.protocol import State

from backend.hub import Client
from backend.server import KnoweServer
from backend.tests.test_task_runtime_recovery_ack import _OpenSocket, _close, _wait_for
from backend.task_runtime.store import RecoveryDirtyBudgetError, RuntimeStoreError


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


class _ClosingHandlerSocket(_OpenSocket):
    """Production-shaped socket whose receive loop closes immediately."""

    def __aiter__(self) -> "_ClosingHandlerSocket":
        return self

    async def __anext__(self) -> object:
        raise StopAsyncIteration

    async def close(self, **_kwargs: object) -> None:
        self.state = State.CLOSED


def test_dirty_global_budget_rolls_back_producer_generation(tmp_path: Path) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.task_runtime is not None
    store = server.task_runtime.store
    groups = _seed(server, 5)
    for index, group_id in enumerate(groups[:4]):
        updated = store.record_live_recovery(
            run_id=f"run_{index:032x}", group_id=group_id, through_seq=2,
            reason="global-budget", attempts=4,
            recovery_token=f"recovery_{index:032x}",
            dirty_global_limit=4, dirty_group_limit=10,
        )
        dirty = store.live_recovery_dirty_marker(group_id)
        assert dirty is not None
        assert store.freeze_live_recovery_dirty(
            int(dirty["dirty_queue_revision"]), group_id=group_id,
            recovery_token=str(updated["recovery_token"]),
            generation=int(updated["generation"]),
        ) is not None
    with pytest.raises(RecoveryDirtyBudgetError):
        store.record_live_recovery(
            run_id="run_00000000000000000000000000000004",
            group_id=groups[4], through_seq=2, reason="global-budget",
            attempts=4, recovery_token="recovery_00000000000000000000000000000004",
            dirty_global_limit=4, dirty_group_limit=10,
        )
    assert store.live_recovery_dirty_count() == 4
    unchanged = store.live_recovery(groups[4])
    assert unchanged is not None and int(unchanged["generation"]) == 1
    server.task_runtime.close()


def test_exact_clear_cannot_bypass_disconnect_debt_or_claim(
    tmp_path: Path,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.task_runtime is not None
    store = server.task_runtime.store
    group_id = _seed(server, 1)[0]
    token = "recovery_00000000000000000000000000000000"
    updated = store.record_live_recovery(
        run_id="run_00000000000000000000000000000000",
        group_id=group_id, through_seq=2, reason="exact-clear-debt",
        attempts=4, recovery_token=token,
    )
    generation = int(updated["generation"])
    assert generation == 2
    assert store.live_recovery_dirty_count(group_id) == 1
    assert store.mark_live_recoveries_disconnect_after(
        0, update_revision=0, fence_id="fence_exact_clear",
        disconnected_at_ns=1,
    ) == 1
    before = store.live_recovery(group_id)
    assert before is not None and int(before["disconnect_retry"]) == 1

    assert not store.clear_live_recovery(group_id, "forged-token", generation)
    assert not store.clear_live_recovery(group_id, token, generation + 1)
    assert not store.clear_live_recovery(group_id, token, generation)
    assert store.live_recovery(group_id) == before
    assert store.live_recovery_dirty_count(group_id) == 1
    with pytest.raises(RuntimeStoreError, match="deletion intent"):
        store.clear_group_live_recovery(group_id)
    assert store.live_recovery(group_id) == before
    assert store.live_recovery_dirty_count(group_id) == 1

    epoch = store.live_recovery_disconnect_epoch()
    assert not store.consume_live_recovery_disconnect(
        group_id, "forged-token", generation,
        claim_id="claim_forged", claim_after_epoch=epoch,
    )
    assert store.consume_live_recovery_disconnect(
        group_id, token, generation,
        claim_id="claim_exact", claim_after_epoch=epoch,
    )
    assert store.clear_live_recovery(group_id, token, generation)
    assert store.live_recovery(group_id) is None
    assert store.live_recovery_dirty_count(group_id) == 0
    server.task_runtime.close()


def test_exact_clear_racing_disconnect_mark_is_atomic(tmp_path: Path) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.task_runtime is not None
    store = server.task_runtime.store
    for index in range(12):
        group_id = f"grp_{index + 100:032x}"
        run_id = f"run_{index + 100:032x}"
        token = f"recovery_{index + 100:032x}"
        store.record_live_recovery(
            run_id=run_id, group_id=group_id, through_seq=1,
            reason="clear-race-base", attempts=4, recovery_token=token,
        )
        updated = store.record_live_recovery(
            run_id=run_id, group_id=group_id, through_seq=2,
            reason="clear-race-dirty", attempts=4, recovery_token=token,
        )
        generation = int(updated["generation"])
        barrier = threading.Barrier(2)

        def clear_exact() -> bool:
            barrier.wait(timeout=2)
            return store.clear_live_recovery(group_id, token, generation)

        def mark_debt() -> int:
            barrier.wait(timeout=2)
            return store.mark_live_recoveries_disconnect_after(
                0, update_revision=0, fence_id=f"fence_race_{index}",
                disconnected_at_ns=index + 1,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            clear_future = pool.submit(clear_exact)
            mark_future = pool.submit(mark_debt)
            cleared = clear_future.result(timeout=5)
            marked = mark_future.result(timeout=5)
        # SQLite's immediate transactions choose exactly one legal serial order:
        # clear wins before debt exists, or debt wins and exact clear is a no-op.
        assert not (cleared and marked > 0)
        row = store.live_recovery(group_id)
        if marked > 0:
            assert not cleared
            assert row is not None and int(row["disconnect_retry"]) == 1
            assert store.live_recovery_dirty_count(group_id) == 1
            assert store.consume_live_recovery_disconnect(
                group_id, token, generation,
                claim_id=f"claim_race_{index}",
                claim_after_epoch=store.live_recovery_disconnect_epoch(),
            )
            assert store.clear_live_recovery(group_id, token, generation)
        else:
            assert cleared
            assert row is None
            assert store.live_recovery_dirty_count(group_id) == 0
    server.task_runtime.close()


async def _ack_next_notified(server: KnoweServer, client: Client) -> int:
    key = min(
        client.task_runtime_recovery_notified,
        key=lambda item: client.task_runtime_recovery_revisions[item],
    )
    project_id = server._runtime_project_id_for_group(key[0])  # noqa: SLF001
    before = sum(
        row.get("type") == "task_runtime_result" for row in client.ws.messages
    )
    await server._cmd_request_snapshot(client, {  # noqa: SLF001
        "project_id": project_id,
        "recovery_token": key[1],
        "recovery_generation": key[2],
    })
    responses = [
        row for row in client.ws.messages if row.get("type") == "task_runtime_result"
    ]
    assert len(responses) == before + 1
    response = responses[-1]
    await server._cmd_task_runtime_recovery_ack(client, {  # noqa: SLF001
        "project_id": project_id,
        "group_id": key[0],
        "recovery_token": response["recovery_token"],
        "recovery_generation": response["recovery_generation"],
        "snapshot_cursor": response["snapshot_cursor"],
    })
    _assert_window(client)
    return int(response["recovery_generation"])


def _ack_next_notified_direct(server: KnoweServer, client: Client) -> int:
    """Exercise ACK/CAS/retirement without serializing the already-covered snapshot path."""
    key = min(
        client.task_runtime_recovery_notified,
        key=lambda item: client.task_runtime_recovery_revisions[item],
    )
    cursor = "f" * 64
    client.task_runtime_recovery_notified.remove(key)
    client.task_runtime_recovery_snapshots[key] = cursor
    assert server._ack_task_runtime_recovery(  # noqa: SLF001
        client, group_id=key[0], recovery_token=key[1],
        generation=key[2], snapshot_cursor=cursor,
    )
    _assert_window(client)
    return key[2]


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
@pytest.mark.parametrize("update_count", [200, 1000])
async def test_stalled_client_is_fenced_before_dirty_log_exceeds_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, update_count: int,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_DIRTY_GLOBAL_MAX", 8)
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_DIRTY_GROUP_MAX", 8)
    # These clients are intentionally stalled. Budget pressure may fence them only
    # after this no-progress lease expires (healthy-progress behavior is covered
    # separately below).
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_ACK_LEASE_SECONDS", 0.02)
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_FLUSH_INITIAL_SECONDS", 0.001)
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_FLUSH_MAX_SECONDS", 0.01)
    project_id = f"project_budget_{update_count}"
    group = server.groups.create_group("Budget", legacy_project_id=project_id)
    run = server.task_runtime.submit(
        group.group_id, title="Budget", request_key=f"budget-{update_count}",
    )
    event = server.task_runtime.store.events_after(run.run_id, 0)[0]
    server._start_task_runtime_event_bridge()  # noqa: SLF001
    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(  # noqa: SLF001
            event, reason="budget-base", attempts=4,
        )

    fast = Client(_OpenSocket())
    stalled_clients = (Client(_OpenSocket()), Client(_OpenSocket()))
    for client in (fast, *stalled_clients):
        server._admit_task_runtime_client(client)  # noqa: SLF001
        await client.release_live_replay_fence()
    await server._notify_task_runtime_recoveries()  # noqa: SLF001
    assert _ack_next_notified_direct(server, fast) == 1
    assert all(
        len(stalled.task_runtime_recovery_notified) == 1
        for stalled in stalled_clients
    )

    latest_generation = 1
    backpressured = False
    for index in range(update_count):
        latest_generation = index + 2
        with server._task_runtime_event_bridge_lock:  # noqa: SLF001
            server._record_task_runtime_recovery_locked(  # noqa: SLF001
                event, reason=f"budget-update-{index}", attempts=4,
            )
        assert server.task_runtime.store.live_recovery_dirty_count() <= 8
        assert server.task_runtime.store.live_recovery_dirty_count(group.group_id) <= 8
        if not backpressured:
            await _wait_for(lambda: (
                (
                    (row := server.task_runtime.store.live_recovery(group.group_id))
                    is not None
                    and int(row["generation"]) == latest_generation
                )
                or (
                    server.task_runtime.store.has_live_recovery_disconnect_debt()
                    and server._task_runtime_event_bridge_recovery[group.group_id][  # noqa: SLF001
                        "persisted"
                    ] is False
                )
            ))
            durable_now = server.task_runtime.store.live_recovery(group.group_id)
            assert durable_now is not None
            backpressured = int(durable_now["generation"]) < latest_generation
            if not backpressured:
                await server._notify_task_runtime_recoveries(fast)  # noqa: SLF001
                while fast.task_runtime_recovery_notified:
                    assert _ack_next_notified_direct(server, fast) == latest_generation
        assert server.task_runtime.store.live_recovery_dirty_count() <= 8
        _assert_window(fast)
        for stalled in stalled_clients:
            _assert_window(stalled)

    assert all(stalled.task_runtime_recovery_fenced for stalled in stalled_clients)
    assert all(stalled not in server.hub.clients for stalled in stalled_clients)
    assert fast.alive
    durable = server.task_runtime.store.live_recovery(group.group_id)
    assert durable is not None
    durable_generation = int(durable["generation"])
    assert durable_generation < latest_generation
    assert int(durable["disconnect_retry"]) == 1
    hot = server._task_runtime_event_bridge_recovery[group.group_id]  # noqa: SLF001
    assert hot["persisted"] is False
    assert int(hot["generation"]) == latest_generation

    replacement = Client(_OpenSocket())
    server._admit_task_runtime_client(replacement)  # noqa: SLF001
    await replacement.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(replacement)  # noqa: SLF001
    # First consume the durable disconnect fence. The bounded hot generation can
    # then flush and is delivered as the next authoritative recovery.
    assert await _ack_next_notified(server, replacement) == durable_generation
    await _wait_for(lambda: (
        (row := server.task_runtime.store.live_recovery(group.group_id)) is not None
        and int(row["generation"]) == latest_generation
    ))
    await server._notify_task_runtime_recoveries()  # noqa: SLF001
    for client in (fast, replacement):
        while client.task_runtime_recovery_notified:
            assert _ack_next_notified_direct(server, client) == latest_generation
    assert server.task_runtime.store.live_recoveries() == ()
    assert server.task_runtime.store.live_recovery_dirty() == ()
    await _close(server)


@pytest.mark.asyncio
async def test_recovery_ack_lease_fences_no_progress_and_reconnects_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_ACK_LEASE_SECONDS", 0.05)
    group = server.groups.create_group("Lease", legacy_project_id="project_lease")
    run = server.task_runtime.submit(group.group_id, title="Lease", request_key="lease")
    event = server.task_runtime.store.events_after(run.run_id, 0)[0]
    server._start_task_runtime_event_bridge()  # noqa: SLF001
    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(  # noqa: SLF001
            event, reason="lease", attempts=4,
        )
    stalled = Client(_OpenSocket())
    server._admit_task_runtime_client(stalled)  # noqa: SLF001
    await stalled.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(stalled)  # noqa: SLF001
    assert stalled.task_runtime_recovery_progress_deadline is not None
    await _wait_for(lambda: stalled.task_runtime_recovery_fenced, timeout=1)
    debt = server.task_runtime.store.live_recovery(group.group_id)
    assert debt is not None and int(debt["disconnect_retry"]) == 1

    replacement = Client(_OpenSocket())
    server._admit_task_runtime_client(replacement)  # noqa: SLF001
    await replacement.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(replacement)  # noqa: SLF001
    assert await _ack_next_notified(server, replacement) == 1
    assert server.task_runtime.store.live_recoveries() == ()
    await _close(server)
    assert server._task_runtime_recovery_watchdog_task is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_fence_debt_write_retries_before_releasing_retirement_blocker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_FLUSH_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_FLUSH_MAX_SECONDS", 0.02)
    group = server.groups.create_group("Debt retry", legacy_project_id="project_debt_retry")
    run = server.task_runtime.submit(
        group.group_id, title="Debt", request_key="debt-retry",
    )
    event = server.task_runtime.store.events_after(run.run_id, 0)[0]
    server._start_task_runtime_event_bridge()  # noqa: SLF001
    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(  # noqa: SLF001
            event, reason="debt-retry", attempts=4,
        )
    client = Client(_OpenSocket())
    server._admit_task_runtime_client(client)  # noqa: SLF001
    await client.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(client)  # noqa: SLF001

    original = server.task_runtime.store.mark_live_recoveries_disconnect_after
    attempts = 0

    def transient(*args: object, **kwargs: object) -> int:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise OSError("transient debt-store outage")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        server.task_runtime.store, "mark_live_recoveries_disconnect_after", transient,
    )
    await server._fence_task_runtime_recovery_client(client)  # noqa: SLF001
    assert client in server.hub.clients
    assert not client.task_runtime_recovery_disconnect_debt_transferred
    await _wait_for(
        lambda: client.task_runtime_recovery_disconnect_debt_transferred,
    )
    assert attempts == 3
    assert client not in server.hub.clients
    debt = server.task_runtime.store.live_recovery(group.group_id)
    assert debt is not None and int(debt["disconnect_retry"]) == 1
    await _close(server)


@pytest.mark.asyncio
async def test_failed_persistence_flushes_latest_before_notice_without_new_event(
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

    server._start_task_runtime_event_bridge()  # noqa: SLF001
    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(  # noqa: SLF001
            event, reason="durable-one", attempts=4,
        )
    client = Client(_OpenSocket())
    server._admit_task_runtime_client(client)  # noqa: SLF001
    await client.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
    first = next(row for row in client.ws.messages if row.get("type") == "resync_required")
    original_record = server.task_runtime.store.record_live_recovery
    unavailable = True
    store_attempts = 0

    def flaky(**kwargs: object) -> dict[str, object]:
        nonlocal store_attempts
        store_attempts += 1
        if unavailable:
            raise OSError("injected recovery-table outage")
        return original_record(**kwargs)

    monkeypatch.setattr(server.task_runtime.store, "record_live_recovery", flaky)
    latest_generation = 1001
    for generation in range(2, latest_generation + 1):
        with server._task_runtime_event_bridge_lock:  # noqa: SLF001
            server._record_task_runtime_recovery_locked(  # noqa: SLF001
                event, reason=f"volatile-{generation}", attempts=4,
            )
    durable = server.task_runtime.store.live_recovery(group.group_id)
    assert durable is not None and int(durable["generation"]) == 1
    hot = server._task_runtime_event_bridge_recovery[group.group_id]  # noqa: SLF001
    assert hot["persisted"] is False and int(hot["generation"]) == latest_generation
    assert "volatile_dirty_entries" not in hot
    assert store_attempts < 20
    await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
    assert len([row for row in client.ws.messages if row.get("type") == "resync_required"]) == 1

    unavailable = False
    await _wait_for(
        lambda: (
            (row := server.task_runtime.store.live_recovery(group.group_id)) is not None
            and int(row["generation"]) == latest_generation
        ),
    )
    await _wait_for(
        lambda: server._task_runtime_event_bridge_recovery[group.group_id]["persisted"]  # noqa: SLF001
    )
    # The already-issued generation-1 notice upgrades to the now-durable latest
    # snapshot; no unpersisted generation was ever advertised.
    await server._cmd_request_snapshot(client, {  # noqa: SLF001
        "project_id": project_id, "recovery_token": first["recovery_token"],
        "recovery_generation": first["recovery_generation"],
    })
    response = next(
        row for row in reversed(client.ws.messages)
        if row.get("type") == "task_runtime_result"
    )
    assert response["recovery_generation"] == latest_generation
    await server._cmd_task_runtime_recovery_ack(client, {  # noqa: SLF001
        "project_id": project_id, "group_id": group.group_id,
        "recovery_token": response["recovery_token"],
        "recovery_generation": response["recovery_generation"],
        "snapshot_cursor": response["snapshot_cursor"],
    })
    assert server.task_runtime.store.live_recoveries() == ()
    assert server.task_runtime.store.live_recovery_dirty() == ()
    _assert_window(client)
    await _close(server)


@pytest.mark.asyncio
async def test_persistence_flush_isolates_poison_group_and_cancels_on_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    groups = [
        server.groups.create_group(
            name, legacy_project_id=f"project_flush_{name.lower()}",
        )
        for name in ("Poison", "Healthy")
    ]
    events = []
    for index, group in enumerate(groups):
        run = server.task_runtime.submit(
            group.group_id, title=group.name, request_key=f"flush-{index}",
        )
        events.append(server.task_runtime.store.events_after(run.run_id, 0)[0])
    server._start_task_runtime_event_bridge()  # noqa: SLF001
    for event in events:
        with server._task_runtime_event_bridge_lock:  # noqa: SLF001
            server._record_task_runtime_recovery_locked(  # noqa: SLF001
                event, reason="flush-base", attempts=4,
            )

    original_record = server.task_runtime.store.record_live_recovery
    fail_all = True

    def selective(**kwargs: object) -> dict[str, object]:
        if fail_all or kwargs.get("group_id") == groups[0].group_id:
            raise OSError("poison recovery row")
        return original_record(**kwargs)

    monkeypatch.setattr(server.task_runtime.store, "record_live_recovery", selective)
    for event in events:
        with server._task_runtime_event_bridge_lock:  # noqa: SLF001
            server._record_task_runtime_recovery_locked(  # noqa: SLF001
                event, reason="flush-update", attempts=4,
            )
    fail_all = False
    await _wait_for(
        lambda: (
            (row := server.task_runtime.store.live_recovery(groups[1].group_id)) is not None
            and int(row["generation"]) == 2
        ),
    )
    poison = server.task_runtime.store.live_recovery(groups[0].group_id)
    assert poison is not None and int(poison["generation"]) == 1
    assert server._task_runtime_event_bridge_recovery[groups[0].group_id]["persisted"] is False  # noqa: SLF001
    assert server._task_runtime_event_bridge_recovery[groups[1].group_id]["persisted"] is True  # noqa: SLF001
    assert server._task_runtime_recovery_flush_task is not None  # noqa: SLF001
    await _close(server)
    assert server._task_runtime_recovery_flush_task is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_dirty_budget_preserves_recent_progress_until_lease_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.task_runtime is not None
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_ACK_LEASE_SECONDS", 60.0)
    group_id = _seed(server, 1)[0]
    monkeypatch.setattr(
        server, "_runtime_project_id_for_group", lambda _group_id: "project_pressure",
    )
    server._start_task_runtime_event_bridge()  # noqa: SLF001
    client = Client(_OpenSocket())
    server._admit_task_runtime_client(client)  # noqa: SLF001
    await client.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(client)  # noqa: SLF001
    assert client.task_runtime_recovery_notified

    for generation in range(2, 6):
        marker = server.task_runtime.store.record_live_recovery(
            run_id="run_00000000000000000000000000000000",
            group_id=group_id, through_seq=generation,
            reason="healthy-pressure", attempts=4,
            recovery_token="recovery_00000000000000000000000000000000",
            dirty_global_limit=8, dirty_group_limit=8,
        )
        dirty = server.task_runtime.store.live_recovery_dirty_marker(group_id)
        assert dirty is not None
        assert server.task_runtime.store.freeze_live_recovery_dirty(
            int(dirty["dirty_queue_revision"]), group_id=group_id,
            recovery_token=str(marker["recovery_token"]),
            generation=int(marker["generation"]),
        ) is not None

    # Proven contiguous progress 15ms ago with almost the full lease remaining is
    # never an eviction candidate, even under repeated hard-cap relief attempts.
    now = asyncio.get_running_loop().time()
    client.task_runtime_recovery_last_progress_at = now - 0.015
    client.task_runtime_recovery_progress_deadline = now + 59.985
    for _ in range(5):
        assert not await server._relieve_task_runtime_recovery_budget()  # noqa: SLF001
        assert not client.task_runtime_recovery_fenced
        assert client in server.hub.clients
    assert server.task_runtime.store.live_recovery_dirty_count() == 4

    client.task_runtime_recovery_last_progress_at = now - 60.001
    client.task_runtime_recovery_progress_deadline = now - 0.001
    assert await server._relieve_task_runtime_recovery_budget()  # noqa: SLF001
    assert client.task_runtime_recovery_fenced
    assert client not in server.hub.clients
    await _close(server)


@pytest.mark.asyncio
async def test_handler_finally_keeps_debt_blocker_until_store_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_FLUSH_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_FLUSH_MAX_SECONDS", 0.02)
    group = server.groups.create_group(
        "Handler debt", legacy_project_id="project_handler_debt",
    )
    run = server.task_runtime.submit(
        group.group_id, title="Handler", request_key="handler-debt",
    )
    event = server.task_runtime.store.events_after(run.run_id, 0)[0]
    server._start_task_runtime_event_bridge()  # noqa: SLF001
    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(  # noqa: SLF001
            event, reason="handler-finally", attempts=4,
        )

    async def no_handshake(_client: Client) -> None:
        return None

    monkeypatch.setattr(server, "_handshake", no_handshake)
    original_transfer = server.task_runtime.store.mark_live_recoveries_disconnect_after
    unavailable = True

    def failing_transfer(*args: object, **kwargs: object) -> int:
        if unavailable:
            raise OSError("debt store unavailable")
        return original_transfer(*args, **kwargs)

    monkeypatch.setattr(
        server.task_runtime.store,
        "mark_live_recoveries_disconnect_after",
        failing_transfer,
    )
    await server.handle(_ClosingHandlerSocket())
    blockers = tuple(server.hub.clients)
    assert len(blockers) == 1
    blocker = blockers[0]
    assert blocker.task_runtime_recovery_fenced
    assert not blocker.task_runtime_recovery_disconnect_debt_transferred
    # Failed cleanup cannot make a replacement observe a falsely clean store.
    assert server.task_runtime.store.live_recovery(group.group_id) is not None
    server.hub.prune()
    assert blocker in server.hub.clients

    unavailable = False
    await _wait_for(lambda: blocker.task_runtime_recovery_retirement_complete)
    assert blocker not in server.hub.clients
    debt = server.task_runtime.store.live_recovery(group.group_id)
    assert debt is not None and int(debt["disconnect_retry"]) == 1
    await _close(server)


@pytest.mark.asyncio
async def test_budget_retirement_honors_failed_and_durable_disconnect_blockers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_FLUSH_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_FLUSH_MAX_SECONDS", 0.02)
    group = server.groups.create_group(
        "Retirement frontier", legacy_project_id="project_retirement_frontier",
    )
    run = server.task_runtime.submit(
        group.group_id, title="Frontier", request_key="retirement-frontier",
    )
    event = server.task_runtime.store.events_after(run.run_id, 0)[0]
    server._start_task_runtime_event_bridge()  # noqa: SLF001
    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(  # noqa: SLF001
            event, reason="frontier-base", attempts=4,
        )

    fast = Client(_OpenSocket())
    transferred_blocker = Client(_OpenSocket())
    failed_blocker = Client(_OpenSocket())
    for client in (fast, transferred_blocker, failed_blocker):
        server._admit_task_runtime_client(client)  # noqa: SLF001
        await client.release_live_replay_fence()
    await server._notify_task_runtime_recoveries()  # noqa: SLF001
    assert _ack_next_notified_direct(server, fast) == 1

    # Freeze one immutable dirty update and let the fast renderer cover it. Both
    # slow sessions still own the earlier frontier.
    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(  # noqa: SLF001
            event, reason="frontier-dirty", attempts=4,
        )
    await server._notify_task_runtime_recoveries(fast)  # noqa: SLF001
    assert _ack_next_notified_direct(server, fast) == 2
    assert server.task_runtime.store.live_recovery_dirty_count() == 1
    assert server.task_runtime.store.live_recovery(group.group_id) is not None

    original_transfer = server.task_runtime.store.mark_live_recoveries_disconnect_after
    failed_store = True

    def selective_transfer(*args: object, **kwargs: object) -> int:
        if (
            failed_store
            and kwargs.get("fence_id") == failed_blocker.task_runtime_recovery_session_id
        ):
            raise OSError("second blocker debt store unavailable")
        return original_transfer(*args, **kwargs)

    monkeypatch.setattr(
        server.task_runtime.store,
        "mark_live_recoveries_disconnect_after",
        selective_transfer,
    )
    assert await server._retire_task_runtime_recovery_client(  # noqa: SLF001
        transferred_blocker,
    )
    assert not await server._retire_task_runtime_recovery_client(  # noqa: SLF001
        failed_blocker,
    )
    assert transferred_blocker not in server.hub.clients
    assert failed_blocker in server.hub.clients
    assert failed_blocker in server._task_runtime_recovery_retirement_blockers  # noqa: SLF001
    assert server.task_runtime.store.has_live_recovery_disconnect_debt()

    # The alive fast client, failed lifecycle blocker, and durable replacement
    # debt all participate in one fail-closed frontier. No alternate no-client or
    # high-water path may delete dirty1/base.
    for _ in range(3):
        assert not await server._relieve_task_runtime_recovery_budget()  # noqa: SLF001
        assert server.task_runtime.store.live_recovery_dirty_count() == 1
        assert server.task_runtime.store.live_recovery(group.group_id) is not None

    failed_store = False
    await _wait_for(lambda: failed_blocker.task_runtime_recovery_retirement_complete)
    assert failed_blocker not in server.hub.clients
    assert server.task_runtime.store.has_live_recovery_disconnect_debt()
    assert server.task_runtime.store.live_recovery_dirty_count() == 1
    # Even the Store-level compactor rejects bypassing unconsumed debt.
    server.task_runtime.store.retire_live_recovery_dirty_through(
        server.task_runtime.store.live_recovery_dirty_revision_high_water(),
    )
    assert server.task_runtime.store.live_recovery_dirty_count() == 1

    # After the caught-up fast renderer leaves, durable debt is itself an
    # authoritative participant; the no-client high-water shortcut stays closed.
    assert await server._retire_task_runtime_recovery_client(fast)  # noqa: SLF001
    assert not tuple(client for client in server.hub.clients if client.alive)
    assert not await server._relieve_task_runtime_recovery_budget()  # noqa: SLF001
    assert server.task_runtime.store.live_recovery_dirty_count() == 1

    replacement = Client(_OpenSocket())
    server._admit_task_runtime_client(replacement)  # noqa: SLF001
    await replacement.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(replacement)  # noqa: SLF001
    assert await _ack_next_notified(server, replacement) == 2
    assert not server.task_runtime.store.has_live_recovery_disconnect_debt()
    assert server.task_runtime.store.live_recovery_dirty_count() == 0
    assert server.task_runtime.store.live_recoveries() == ()
    await _close(server)


@pytest.mark.asyncio
async def test_shutdown_debt_failure_preserves_restart_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    group = server.groups.create_group(
        "Restart debt", legacy_project_id="project_restart_debt",
    )
    run = server.task_runtime.submit(
        group.group_id, title="Restart", request_key="restart-debt",
    )
    event = server.task_runtime.store.events_after(run.run_id, 0)[0]
    server._start_task_runtime_event_bridge()  # noqa: SLF001
    with server._task_runtime_event_bridge_lock:  # noqa: SLF001
        server._record_task_runtime_recovery_locked(  # noqa: SLF001
            event, reason="restart-debt", attempts=4,
        )
    old_client = Client(_OpenSocket())
    server._admit_task_runtime_client(old_client)  # noqa: SLF001
    await old_client.release_live_replay_fence()
    await server._notify_task_runtime_recoveries(old_client)  # noqa: SLF001

    def permanently_failed_transfer(*_args: object, **_kwargs: object) -> int:
        raise OSError("permanent debt failure during shutdown")

    monkeypatch.setattr(
        server.task_runtime.store,
        "mark_live_recoveries_disconnect_after",
        permanently_failed_transfer,
    )
    await server._stop_task_runtime_event_bridge()  # noqa: SLF001
    assert old_client in server.hub.clients
    assert old_client.task_runtime_recovery_fenced
    assert not old_client.task_runtime_recovery_disconnect_debt_transferred
    assert server.task_runtime.store.live_recovery(group.group_id) is not None
    server.task_runtime.close()
    if server.store is not None:
        server.store.close(timeout=5)

    restarted = KnoweServer(str(tmp_path))
    assert restarted.task_runtime is not None
    restarted._start_task_runtime_event_bridge()  # noqa: SLF001
    replacement = Client(_OpenSocket())
    restarted._admit_task_runtime_client(replacement)  # noqa: SLF001
    await replacement.release_live_replay_fence()
    await restarted._notify_task_runtime_recoveries(replacement)  # noqa: SLF001
    assert replacement.task_runtime_recovery_notified
    assert await _ack_next_notified(restarted, replacement) == 1
    assert restarted.task_runtime.store.live_recoveries() == ()
    await _close(restarted)


@pytest.mark.asyncio
async def test_full_hot_backlog_keeps_drain_alive_and_rescans_after_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_FLUSH_BACKLOG_MAX", 2)
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_MARKER_MAX", 2)
    monkeypatch.setattr(server, "TASK_RUNTIME_DELIVERY_RETRIES", 0)
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_FLUSH_INITIAL_SECONDS", 0.005)
    monkeypatch.setattr(server, "TASK_RUNTIME_RECOVERY_FLUSH_MAX_SECONDS", 0.02)
    groups = [
        server.groups.create_group(
            f"Backlog {index}", legacy_project_id=f"project_backlog_{index}",
        )
        for index in range(4)
    ]
    runs = [
        server.task_runtime.submit(
            group.group_id, title=group.name, request_key=f"backlog-{index}",
        )
        for index, group in enumerate(groups)
    ]
    server._start_task_runtime_event_bridge()  # noqa: SLF001
    original_record = server.task_runtime.store.record_live_recovery
    unavailable = True

    def failed_record(**kwargs: object) -> dict[str, object]:
        if unavailable:
            raise OSError("permanent recovery-store outage")
        return original_record(**kwargs)

    async def failed_broadcast(_event: object, *, recovering: bool = False) -> bool:
        del recovering
        return False

    monkeypatch.setattr(server.task_runtime.store, "record_live_recovery", failed_record)
    monkeypatch.setattr(server, "_broadcast_task_runtime_event", failed_broadcast)
    loop = asyncio.get_running_loop()
    leaked: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: leaked.append(dict(context)))
    try:
        for index, (group, run) in enumerate(zip(groups, runs)):
            server.task_runtime.checkpoint(
                group.group_id, run.run_id, label=f"outage-{index}",
                request_key=f"outage-checkpoint-{index}",
            )
        await _wait_for(
            lambda: server._task_runtime_recovery_bridge_degraded,  # noqa: SLF001
        )
        await _wait_for(
            lambda: server._task_runtime_event_drain_task is None,  # noqa: SLF001
        )
        assert len(server._task_runtime_event_bridge_recovery) == 2  # noqa: SLF001
        assert len(server._task_runtime_event_bridge_pending) == 0  # noqa: SLF001
        assert not server._task_runtime_event_bridge_scheduled  # noqa: SLF001
        assert not leaked

        unavailable = False
        await _wait_for(
            lambda: all(
                server.task_runtime.store.live_recovery(group.group_id) is not None
                for group in groups
            ),
            timeout=5,
        )
        await _wait_for(
            lambda: not server._task_runtime_recovery_bridge_degraded,  # noqa: SLF001
        )

        delivered: list[object] = []

        async def healthy_broadcast(event: object, *, recovering: bool = False) -> bool:
            del recovering
            delivered.append(event)
            return True

        monkeypatch.setattr(server, "_broadcast_task_runtime_event", healthy_broadcast)
        server.task_runtime.checkpoint(
            groups[-1].group_id, runs[-1].run_id, label="after-recovery",
            request_key="backlog-after-recovery",
        )
        await _wait_for(lambda: bool(delivered))
        await _wait_for(
            lambda: server._task_runtime_event_drain_task is None,  # noqa: SLF001
        )
        assert not server._task_runtime_event_bridge_scheduled  # noqa: SLF001
        assert not leaked
    finally:
        loop.set_exception_handler(previous_handler)
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
