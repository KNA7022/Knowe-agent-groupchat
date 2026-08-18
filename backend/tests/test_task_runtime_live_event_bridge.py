from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from websockets.protocol import State

from backend.hub import Client, Hub
from backend import server as server_module
from backend.server import KnoweServer
from backend.task_runtime.contracts import RiskLevel, RunEvent, TaskStatus


def _future(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace(
        "+00:00", "Z",
    )


class OpenCaptureSocket:
    state = State.OPEN

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.thread_ids: list[int] = []

    async def send(self, raw: str) -> None:
        self.thread_ids.append(threading.get_ident())
        self.messages.append(json.loads(raw))


class BlockingReplaySocket(OpenCaptureSocket):
    def __init__(self) -> None:
        super().__init__()
        self.replay_complete_entered = asyncio.Event()
        self.release_replay_complete = asyncio.Event()

    async def send(self, raw: str) -> None:
        payload = json.loads(raw)
        if payload.get("type") == "replay_complete":
            self.replay_complete_entered.set()
            await self.release_replay_complete.wait()
        self.thread_ids.append(threading.get_ident())
        self.messages.append(payload)


async def _wait_for(predicate: Any, *, timeout: float = 1.5) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for live Task Runtime event")
        await asyncio.sleep(0.01)


async def _close(server: KnoweServer) -> None:
    await server._stop_task_runtime_event_bridge()  # noqa: SLF001
    if server.task_runtime is not None:
        server.task_runtime.close()
    if server.store is not None:
        server.store.close(timeout=5)


@pytest.mark.asyncio
async def test_new_epoch_cursor_replays_complete_new_history() -> None:
    hub = Hub()
    await hub.emit("epoch", {
        "type": "message", "agent_id": "coordinator", "content": "new epoch",
    })
    events, gap, source, cursor = await hub.replay_with_cursor("epoch", 99)
    assert not gap and source == "ring" and cursor == 1
    assert [(item["seq"], item["content"]) for item in events] == [(1, "new epoch")]


@pytest.mark.asyncio
async def test_real_server_loop_receives_timer_expiry_and_follow_on_schedule_once(
    tmp_path: Path,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    group = server.groups.create_group(
        "Live expiry", legacy_project_id="project_live_expiry",
    )
    server.hub.get_or_create("project_live_expiry", "Live expiry")
    socket = OpenCaptureSocket()
    client = Client(socket)
    server._admit_task_runtime_client(client)  # noqa: SLF001
    await client.release_live_replay_fence()
    loop_thread = threading.get_ident()
    server._start_task_runtime_event_bridge()  # noqa: SLF001

    owner = server.task_runtime.submit(
        group.group_id, title="Needs approval", request_key="bridge-expiry-owner",
    )
    server.task_runtime.schedule()
    approval = server.task_runtime.request_approval(
        group.group_id, owner.run_id,
        kind="message_route", summary="Confirm route", risk=RiskLevel.HIGH,
        request_key="bridge-expiry-approval",
        requested_capabilities=("route_message",),
        operation_scope={"route": "queue_new"}, purpose="confirm_route",
        expires_at=_future(0.12),
    )
    queued = server.task_runtime.submit(
        group.group_id, title="Runs after expiry", request_key="bridge-expiry-next",
    )
    server.task_runtime.schedule()
    assert server.task_runtime.store.get_run(queued.run_id).status is TaskStatus.QUEUED  # type: ignore[union-attr]

    await _wait_for(lambda: any(
        item.get("type") == "task_runtime_event"
        and item.get("event", {}).get("event_type") == "approval_requested"
        for item in socket.messages
    ))
    socket.messages.clear()
    socket.thread_ids.clear()

    # The expiry is owned by a real threading.Timer.  No read, snapshot, explicit
    # resolution, or scheduler call is made while the server loop remains responsive.
    await _wait_for(lambda: any(
        item.get("type") == "task_runtime_event"
        and item.get("event", {}).get("event_type") == "approval_resolved"
        for item in socket.messages
    ))
    await _wait_for(lambda: any(
        item.get("type") == "task_runtime_event"
        and item.get("event", {}).get("run_id") == queued.run_id
        and item.get("event", {}).get("public_payload", {}).get("status") == "running"
        for item in socket.messages
    ))

    live = [item for item in socket.messages if item.get("type") == "task_runtime_event"]
    expiry = [
        item for item in live
        if item["event"]["run_id"] == owner.run_id
        and item["event"]["event_type"] == "approval_resolved"
    ]
    assert len(expiry) == 1
    expiry_index = live.index(expiry[0])
    routing_index = next(
        index for index, item in enumerate(live)
        if item["event"]["run_id"] == queued.run_id
        and item["event"]["public_payload"].get("status") == "routing"
    )
    running_index = next(
        index for index, item in enumerate(live)
        if item["event"]["run_id"] == queued.run_id
        and item["event"]["public_payload"].get("status") == "running"
    )
    assert expiry_index < routing_index < running_index
    assert all(item["group_id"] == group.group_id for item in live)
    assert socket.thread_ids and set(socket.thread_ids) == {loop_thread}
    assert server.task_runtime.store.get_approval(approval.approval_id).status == "rejected"  # type: ignore[union-attr]
    assert len([
        event for event in server.task_runtime.store.events_after(owner.run_id, 0)
        if event.event_type == "approval_resolved"
    ]) == 1
    await _close(server)


@pytest.mark.asyncio
async def test_foreign_thread_burst_is_ordered_and_duplicate_notifications_publish_once(
    tmp_path: Path,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    group = server.groups.create_group("Burst", legacy_project_id="project_burst")
    server.hub.get_or_create("project_burst", "Burst")
    run = server.task_runtime.submit(group.group_id, title="Burst", request_key="burst-run")
    server.task_runtime.schedule()
    socket = OpenCaptureSocket()
    client = Client(socket)
    server._admit_task_runtime_client(client)  # noqa: SLF001
    await client.release_live_replay_fence()
    server._start_task_runtime_event_bridge()  # noqa: SLF001

    def produce() -> None:
        for index in range(12):
            server.task_runtime.checkpoint(
                group.group_id, run.run_id, label=f"step-{index}",
                request_key=f"burst-checkpoint-{index}",
            )

    await asyncio.to_thread(produce)
    await _wait_for(lambda: len([
        item for item in socket.messages
        if item.get("event", {}).get("event_type") == "checkpoint_created"
    ]) == 12)
    checkpoints = [
        item["event"] for item in socket.messages
        if item.get("event", {}).get("event_type") == "checkpoint_created"
    ]
    assert [item["public_payload"]["label"] for item in checkpoints] == [
        f"step-{index}" for index in range(12)
    ]
    assert [item["seq"] for item in checkpoints] == sorted(item["seq"] for item in checkpoints)

    durable_last = server.task_runtime.store.events_after(run.run_id, 0)[-1]
    before = len(socket.messages)
    await asyncio.to_thread(server._on_task_runtime_event, durable_last)  # noqa: SLF001
    await asyncio.to_thread(server._on_task_runtime_event, durable_last)  # noqa: SLF001
    await asyncio.sleep(0.05)
    assert len(socket.messages) == before
    assert len([
        item for item in socket.messages
        if item.get("event", {}).get("event_id") == durable_last.event_id
    ]) == 1
    await _close(server)


@pytest.mark.asyncio
async def test_shutdown_fences_racing_callback_and_restart_reconciles_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    group = server.groups.create_group("Restart", legacy_project_id="project_restart_bridge")
    server.hub.get_or_create("project_restart_bridge", "Restart")
    run = server.task_runtime.submit(group.group_id, title="Restart", request_key="restart-run")
    server.task_runtime.schedule()
    socket = OpenCaptureSocket()
    client = Client(socket)
    server._admit_task_runtime_client(client)  # noqa: SLF001
    await client.release_live_replay_fence()
    server._start_task_runtime_event_bridge()  # noqa: SLF001

    entered = asyncio.Event()
    release = asyncio.Event()
    original_emit = server.hub.emit_with_status

    async def delayed_emit(
        project_id: str, payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        if payload.get("event", {}).get("public_payload", {}).get("label") == "racing":
            entered.set()
            await release.wait()
        return await original_emit(project_id, payload)

    monkeypatch.setattr(server.hub, "emit_with_status", delayed_emit)
    await asyncio.to_thread(
        server.task_runtime.checkpoint,
        group.group_id, run.run_id,
        label="racing", request_key="restart-racing",
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    await server._stop_task_runtime_event_bridge()  # noqa: SLF001
    release.set()
    await asyncio.sleep(0.05)
    assert not any(
        item.get("event", {}).get("public_payload", {}).get("label") == "racing"
        for item in socket.messages
    )

    # A commit while stopped is durable but cannot mutate the Hub.  Even a stale
    # callback copied immediately before listener removal is fenced by stopped state.
    await asyncio.to_thread(
        server.task_runtime.checkpoint,
        group.group_id, run.run_id,
        label="offline", request_key="restart-offline",
    )
    offline_event = server.task_runtime.store.events_after(run.run_id, 0)[-1]
    server._on_task_runtime_event(offline_event)  # noqa: SLF001
    assert not server._task_runtime_event_bridge_pending  # noqa: SLF001
    replay = server.task_runtime.replay(group.group_id, run.run_id, after_seq=0)
    assert {event["public_payload"].get("label") for event in replay["events"]} >= {
        "racing", "offline",
    }

    # A fresh lifecycle takes a new durable baseline: missed events come from replay,
    # not as duplicate live frames.  Only a post-restart commit is broadcast.
    socket.messages.clear()
    monkeypatch.setattr(server.hub, "emit", original_emit)
    server._start_task_runtime_event_bridge()  # noqa: SLF001
    await asyncio.sleep(0.05)
    assert socket.messages == []
    await asyncio.to_thread(
        server.task_runtime.checkpoint,
        group.group_id, run.run_id,
        label="fresh", request_key="restart-fresh",
    )
    await _wait_for(lambda: any(
        item.get("event", {}).get("public_payload", {}).get("label") == "fresh"
        for item in socket.messages
    ))
    assert not any(
        item.get("event", {}).get("public_payload", {}).get("label") in {"racing", "offline"}
        for item in socket.messages
    )
    await _close(server)


@pytest.mark.asyncio
async def test_replay_fence_sends_all_group_snapshots_before_racing_live_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    first_pid = "project_20260819010101"
    second_pid = "project_20260819010102"
    first = server.groups.create_group("First", legacy_project_id=first_pid)
    second = server.groups.create_group("Second", legacy_project_id=second_pid)
    server.hub.get_or_create(first_pid, "First")
    server.hub.get_or_create(second_pid, "Second")
    monkeypatch.setattr(server, "_project_dir_is_valid", lambda *_args, **_kwargs: False)
    first_run = server.task_runtime.submit(first.group_id, title="First", request_key="handoff-first")
    second_run = server.task_runtime.submit(second.group_id, title="Second", request_key="handoff-second")
    server.task_runtime.schedule()
    server._start_task_runtime_event_bridge()  # noqa: SLF001

    socket = BlockingReplaySocket()
    client = Client(socket)
    server._admit_task_runtime_client(client)  # noqa: SLF001
    replay_task = asyncio.create_task(server._do_replay(client, {  # noqa: SLF001
        "project_id": first_pid, "since_seq": 0,
    }))
    await asyncio.wait_for(socket.replay_complete_entered.wait(), timeout=1)

    # These commits happen after the replay snapshots have been built but while the
    # replay_complete socket write is deliberately blocked.
    await asyncio.to_thread(
        server.task_runtime.checkpoint, first.group_id, first_run.run_id,
        label="first-race", request_key="handoff-first-race",
    )
    await asyncio.to_thread(
        server.task_runtime.checkpoint, second.group_id, second_run.run_id,
        label="second-race", request_key="handoff-second-race",
    )
    await server.hub.emit(first_pid, {
        "type": "message", "agent_id": "coordinator", "content": "ordinary live message",
    })
    await _wait_for(lambda: len(client._live_replay_buffer) >= 3)  # noqa: SLF001
    assert not any(item.get("type") == "task_runtime_event" for item in socket.messages)

    socket.release_replay_complete.set()
    await replay_task
    await client.release_live_replay_fence()
    types = [item.get("type") for item in socket.messages]
    complete_index = types.index("replay_complete")
    assert all(index > complete_index for index, kind in enumerate(types) if kind in {
        "task_runtime_event", "message",
    })
    complete = socket.messages[complete_index]
    snapshots = {item["group_id"]: item for item in complete["task_runtime_groups"]}
    assert set(snapshots) >= {first.group_id, second.group_id}
    racing = [
        item for item in socket.messages if item.get("type") == "task_runtime_event"
        and item["event"]["public_payload"].get("label") in {"first-race", "second-race"}
    ]
    assert len(racing) == 2
    for item in racing:
        baseline = next(
            run for run in snapshots[item["group_id"]]["runs"]
            if run["run_id"] == item["event"]["run_id"]
        )
        assert item["event"]["seq"] == baseline["last_seq"] + 1
        assert item["event"]["version"] == baseline["version"] + 1
    assert len([item for item in socket.messages if item.get("type") == "message"]) == 1

    # Once the fence is released the next event is direct-live and remains contiguous.
    await asyncio.to_thread(
        server.task_runtime.checkpoint, first.group_id, first_run.run_id,
        label="first-next", request_key="handoff-first-next",
    )
    await _wait_for(lambda: any(
        item.get("event", {}).get("public_payload", {}).get("label") == "first-next"
        for item in socket.messages
    ))
    first_events = [
        item["event"] for item in socket.messages
        if item.get("type") == "task_runtime_event"
        and item["event"]["run_id"] == first_run.run_id
    ]
    assert [item["seq"] for item in first_events] == sorted({item["seq"] for item in first_events})
    await _close(server)


@pytest.mark.asyncio
async def test_bridge_resources_are_bounded_and_poison_run_does_not_block_healthy_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    bad_group = server.groups.create_group("Bad", legacy_project_id="project_bridge_bad")
    good_group = server.groups.create_group("Good", legacy_project_id="project_bridge_good")
    server.hub.get_or_create("project_bridge_bad", "Bad")
    server.hub.get_or_create("project_bridge_good", "Good")
    bad = server.task_runtime.submit(bad_group.group_id, title="Bad", request_key="bounded-bad")
    good = server.task_runtime.submit(good_group.group_id, title="Good", request_key="bounded-good")

    # Constructor/pre-listen callbacks are represented by durable state only and never
    # accumulate an in-memory backlog.
    pre_activation = server.task_runtime.store.events_after(bad.run_id, 0)[0]
    for _ in range(server.TASK_RUNTIME_LIVE_QUEUE_MAX * 2):
        server._on_task_runtime_event(pre_activation)  # noqa: SLF001
    assert not server._task_runtime_event_bridge_pending  # noqa: SLF001

    server._start_task_runtime_event_bridge()  # noqa: SLF001
    # A same-turn burst is hard-capped even before the owning loop can drain it.
    for index in range(server.TASK_RUNTIME_LIVE_QUEUE_MAX * 2):
        queued_notification = replace(
            pre_activation,
            event_id=f"evt_{index:032x}", run_id=f"run_{index:032x}",
            seq=pre_activation.seq + 1, version=pre_activation.version + 1,
        )
        server._on_task_runtime_event(queued_notification)  # noqa: SLF001
    assert len(server._task_runtime_event_bridge_pending) == server.TASK_RUNTIME_LIVE_QUEUE_MAX  # noqa: SLF001
    assert len(server.task_runtime_bridge_recovery_markers()) <= server.TASK_RUNTIME_RECOVERY_MARKER_MAX
    await server._stop_task_runtime_event_bridge()  # noqa: SLF001
    assert not server._task_runtime_event_bridge_pending  # noqa: SLF001
    server._start_task_runtime_event_bridge()  # noqa: SLF001

    def high_volume() -> None:
        for index in range(300):
            server.task_runtime.checkpoint(
                good_group.group_id, good.run_id, label=f"volume-{index}",
                request_key=f"bounded-volume-{index}",
            )

    await asyncio.to_thread(high_volume)
    await _wait_for(lambda: (
        server._task_runtime_event_bridge_last_seq.get(good.run_id, (0, ""))[0]  # noqa: SLF001
        == server.task_runtime.store.get_run(good.run_id).last_seq  # type: ignore[union-attr]
    ))
    assert not hasattr(server, "_task_runtime_event_bridge_published")
    assert len(server._task_runtime_event_bridge_last_seq) <= server.TASK_RUNTIME_ACTIVE_CURSOR_MAX  # noqa: SLF001

    original_events_after = server.task_runtime.store.events_after
    bad_attempts = 0

    def failing_events_after(run_id: str, after_seq: int):
        nonlocal bad_attempts
        if run_id == bad.run_id:
            bad_attempts += 1
            raise OSError("permanent journal fault")
        return original_events_after(run_id, after_seq)

    monkeypatch.setattr(server.task_runtime.store, "events_after", failing_events_after)
    await asyncio.to_thread(
        server.task_runtime.checkpoint, bad_group.group_id, bad.run_id,
        label="missed-bad", request_key="bounded-bad-event",
    )
    await asyncio.to_thread(
        server.task_runtime.checkpoint, good_group.group_id, good.run_id,
        label="delivered-good", request_key="bounded-good-event",
    )
    await _wait_for(lambda: any(
        item.get("event", {}).get("public_payload", {}).get("label") == "delivered-good"
        for item in server.hub.projects["project_bridge_good"].ring.events()
    ))
    await _wait_for(lambda: any(
        marker["group_id"] == bad_group.group_id
        and marker["reason"] == "journal_read_failed"
        for marker in server.task_runtime_bridge_recovery_markers()
    ))
    assert bad_attempts == server.TASK_RUNTIME_DELIVERY_RETRIES + 1
    await asyncio.sleep(0.1)
    assert bad_attempts == server.TASK_RUNTIME_DELIVERY_RETRIES + 1
    assert not server._task_runtime_event_bridge_pending  # noqa: SLF001

    monkeypatch.setattr(server.task_runtime.store, "events_after", original_events_after)
    await server._stop_task_runtime_event_bridge()  # noqa: SLF001
    server._start_task_runtime_event_bridge()  # noqa: SLF001
    replay = server.task_runtime.replay(bad_group.group_id, bad.run_id, after_seq=0)
    assert len([
        item for item in replay["events"]
        if item["public_payload"].get("label") == "missed-bad"
    ]) == 1

    original_broadcast = server._broadcast_task_runtime_event  # noqa: SLF001
    broadcast_attempts = 0

    async def failing_broadcast(event: RunEvent, *, recovering: bool = False) -> bool:
        nonlocal broadcast_attempts
        if event.run_id == bad.run_id:
            broadcast_attempts += 1
            return False
        return await original_broadcast(event, recovering=recovering)

    monkeypatch.setattr(server, "_broadcast_task_runtime_event", failing_broadcast)
    await asyncio.to_thread(
        server.task_runtime.checkpoint, bad_group.group_id, bad.run_id,
        label="broadcast-bad", request_key="bounded-bad-broadcast",
    )
    await asyncio.to_thread(
        server.task_runtime.checkpoint, good_group.group_id, good.run_id,
        label="broadcast-good", request_key="bounded-good-broadcast",
    )
    await _wait_for(lambda: any(
        item.get("event", {}).get("public_payload", {}).get("label") == "broadcast-good"
        for item in server.hub.projects["project_bridge_good"].ring.events()
    ))
    await _wait_for(lambda: any(
        marker["group_id"] == bad_group.group_id
        and marker["reason"] == "hub_broadcast_failed"
        for marker in server.task_runtime_bridge_recovery_markers()
    ))
    assert broadcast_attempts == server.TASK_RUNTIME_DELIVERY_RETRIES + 1
    await asyncio.sleep(0.1)
    assert broadcast_attempts == server.TASK_RUNTIME_DELIVERY_RETRIES + 1
    monkeypatch.setattr(server, "_broadcast_task_runtime_event", original_broadcast)

    # Terminal cursors leave the active LRU, and deletion removes every remaining row.
    server.task_runtime.cancel(good_group.group_id, good.run_id, request_key="bounded-good-cancel")
    await _wait_for(lambda: good.run_id in server._task_runtime_event_bridge_terminal_seq)  # noqa: SLF001
    assert good.run_id not in server._task_runtime_event_bridge_last_seq  # noqa: SLF001
    server._prune_task_runtime_bridge_group(good_group.group_id)  # noqa: SLF001
    assert good.run_id not in server._task_runtime_event_bridge_terminal_seq  # noqa: SLF001
    assert all(
        marker["group_id"] != good_group.group_id
        for marker in server.task_runtime_bridge_recovery_markers()
    )
    assert len(server._task_runtime_event_bridge_last_seq) <= server.TASK_RUNTIME_ACTIVE_CURSOR_MAX  # noqa: SLF001
    assert len(server._task_runtime_event_bridge_terminal_seq) <= server.TASK_RUNTIME_TERMINAL_CURSOR_MAX  # noqa: SLF001

    delete_pid = "project_20260819020202"
    delete_group = server.groups.create_group("Delete", legacy_project_id=delete_pid)
    server.hub.get_or_create(delete_pid, "Delete")
    delete_run = server.task_runtime.submit(
        delete_group.group_id, title="Delete", request_key="bounded-delete-run",
    )
    await _wait_for(lambda: delete_run.run_id in server._task_runtime_event_bridge_last_seq)  # noqa: SLF001
    token = server.groups.delete_preview(delete_group.group_id).confirmation_token
    await server._run_confirmed_group_delete(  # noqa: SLF001
        delete_group.group_id, delete_pid, token,
    )
    assert delete_run.run_id not in server._task_runtime_event_bridge_last_seq  # noqa: SLF001
    assert delete_run.run_id not in server._task_runtime_event_bridge_terminal_seq  # noqa: SLF001
    assert all(
        marker["group_id"] != delete_group.group_id
        for marker in server.task_runtime_bridge_recovery_markers()
    )
    await _close(server)


@pytest.mark.asyncio
async def test_run_lifecycle_owns_bridge_loop_and_unregisters_on_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.task_runtime is not None
    entered_serve = asyncio.Event()

    class FakeServe:
        async def __aenter__(self):
            entered_serve.set()
            return self

        async def __aexit__(self, *_args):
            return False

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def no_op_async(*_args, **_kwargs) -> None:
        return None

    async def idle_loop() -> None:
        await asyncio.Event().wait()

    provenance = SimpleNamespace(
        build_id="test", git_commit="test", runtime_schema_version=1,
        harness_schema_version=1, prompt_bundle_version=1, startup_id="test",
        to_dict=lambda: {"build_id": "test"},
    )
    monkeypatch.setattr(
        server_module, "CONFIG",
        replace(server_module.CONFIG, runtime_token="a" * 64, health_port=0, ws_port=0),
    )
    monkeypatch.setattr(server_module, "activate_build", lambda *_args, **_kwargs: provenance)
    monkeypatch.setattr(server_module, "run_data_migrations", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server_module, "run_sqlite_migrations", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server_module, "serve", lambda *_args, **_kwargs: FakeServe())
    monkeypatch.setattr(server, "load_from_disk", lambda: None)
    monkeypatch.setattr(server.platform_manifest, "refresh", lambda: None)
    monkeypatch.setattr(server, "start_platform", lambda: None)
    monkeypatch.setattr(server, "welcome_if_needed", no_op_async)
    monkeypatch.setattr(server, "wake_projects", no_op_async)
    monkeypatch.setattr(server, "_resume_safe_executor_continuations_after_startup", lambda: None)
    monkeypatch.setattr(server, "_reconcile_task_runtime_group_deletions", no_op_async)
    monkeypatch.setattr(server, "_update_harness_now", no_op_async)
    monkeypatch.setattr(server, "_harness_memory_loop", idle_loop)
    monkeypatch.setattr(server, "_storage_maintenance_loop", idle_loop)
    monkeypatch.setattr(
        server_module.ProviderClient, "aclose_shared_clients", staticmethod(no_op_async),
    )

    owner_loop = asyncio.get_running_loop()
    run_task = asyncio.create_task(server.run())
    await asyncio.wait_for(entered_serve.wait(), timeout=1)
    await _wait_for(lambda: server._task_runtime_event_bridge_active)  # noqa: SLF001
    assert server._task_runtime_event_bridge_loop is owner_loop  # noqa: SLF001
    assert server._task_runtime_listener_service is server.task_runtime  # noqa: SLF001

    server._shutdown_event.set()  # noqa: SLF001
    await asyncio.wait_for(run_task, timeout=3)
    assert not server._task_runtime_event_bridge_active  # noqa: SLF001
    assert server._task_runtime_event_bridge_loop is None  # noqa: SLF001
    assert server._task_runtime_listener_service is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_cursor_capacity_uses_durable_publication_and_stable_hub_event_identity(
    tmp_path: Path,
) -> None:
    """A real retained run remains exact-once after active and terminal LRU eviction."""
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    project_id = "project_20260819030303"
    group = server.groups.create_group("Cursor cap", legacy_project_id=project_id)
    server.hub.get_or_create(project_id, "Cursor cap")
    retained = server.task_runtime.submit(
        group.group_id, title="Retained", request_key="cursor-cap-retained",
    )
    for index in range(server.TASK_RUNTIME_ACTIVE_CURSOR_MAX):
        server.task_runtime.submit(
            group.group_id, title=f"Queued {index}", request_key=f"cursor-cap-{index}",
        )

    server._start_task_runtime_event_bridge()  # noqa: SLF001
    assert retained.run_id not in server._task_runtime_event_bridge_last_seq  # noqa: SLF001
    socket = OpenCaptureSocket()
    client = Client(socket)
    server._admit_task_runtime_client(client)  # noqa: SLF001
    await server._do_replay(client, {  # noqa: SLF001
        "project_id": project_id, "since_seq": 0,
    })
    await client.release_live_replay_fence()
    socket.messages.clear()

    server.task_runtime.checkpoint(
        group.group_id, retained.run_id, label="after-eviction",
        request_key="cursor-cap-after-eviction",
    )
    next_event = server.task_runtime.store.events_after(retained.run_id, 1)[-1]
    await _wait_for(lambda: any(
        item.get("event", {}).get("event_id") == next_event.event_id
        for item in socket.messages
    ), timeout=5)
    live = [
        item["event"]["seq"] for item in socket.messages
        if item.get("type") == "task_runtime_event"
        and item.get("event", {}).get("run_id") == retained.run_id
    ]
    assert live == [2]

    first = server.task_runtime.store.events_after(retained.run_id, 0)[0]
    for index in range(server.TASK_RUNTIME_TERMINAL_CURSOR_MAX + 1):
        with server._task_runtime_event_bridge_lock:  # noqa: SLF001
            server._set_task_runtime_cursor_locked(  # noqa: SLF001
                f"run_{index + 10_000:032x}", group.group_id, 9, terminal=True,
            )
    server._on_task_runtime_event(first)  # noqa: SLF001
    await asyncio.sleep(0.1)
    assert [
        item for item in socket.messages
        if item.get("event", {}).get("event_id") == first.event_id
    ] == []

    assert server.store is not None
    server.store.flush(timeout=10)
    hub_rows = [
        item for item in server.store.load_all_events(project_id)
        if item.get("type") == "task_runtime_event"
    ]
    nested_ids = [item["event"]["event_id"] for item in hub_rows]
    assert nested_ids.count(next_event.event_id) == 1
    assert all(item.get("event_id") == item["event"]["event_id"] for item in hub_rows)
    assert all(
        len(cache) <= server.hub.IDEMPOTENCY_CACHE_MAX
        for cache in server.hub._idempotent_events.values()  # noqa: SLF001
    )

    token = server.groups.delete_preview(group.group_id).confirmation_token
    await server._run_confirmed_group_delete(group.group_id, project_id, token)  # noqa: SLF001
    assert server.task_runtime.store.live_publication_cursor(retained.run_id) is None
    recreated = server.groups.create_group("Recreated", legacy_project_id=project_id)
    server.hub.get_or_create(project_id, "Recreated")
    socket.messages.clear()
    fresh = server.task_runtime.submit(
        recreated.group_id, title="Fresh", request_key="cursor-cap-fresh",
    )
    await _wait_for(lambda: any(
        item.get("event", {}).get("run_id") == fresh.run_id for item in socket.messages
    ), timeout=5)
    assert [
        item["event"]["seq"] for item in socket.messages
        if item.get("event", {}).get("run_id") == fresh.run_id
    ] == [1]
    await _close(server)


@pytest.mark.asyncio
async def test_exhausted_delivery_directly_resyncs_affected_group_and_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    active = server.groups.create_group("Active", legacy_project_id="project_recovery_active")
    affected = server.groups.create_group(
        "Affected", legacy_project_id="project_recovery_background",
    )
    server.hub.get_or_create("project_recovery_active", "Active")
    server.hub.get_or_create("project_recovery_background", "Affected")
    active_run = server.task_runtime.submit(
        active.group_id, title="Healthy", request_key="recovery-healthy",
    )
    affected_run = server.task_runtime.submit(
        affected.group_id, title="Affected", request_key="recovery-affected",
    )
    socket = OpenCaptureSocket()
    client = Client(socket)
    server._admit_task_runtime_client(client)  # noqa: SLF001
    await client.release_live_replay_fence()
    server._start_task_runtime_event_bridge()  # noqa: SLF001
    socket.messages.clear()

    original_broadcast = server.hub.broadcast

    async def fail_affected(event: dict[str, Any], targets=None) -> None:
        if (
            event.get("type") == "task_runtime_event"
            and event.get("event", {}).get("public_payload", {}).get("label") == "unique-final"
        ):
            raise OSError("broadcast unavailable")
        await original_broadcast(event, targets)

    monkeypatch.setattr(server.hub, "broadcast", fail_affected)
    server.task_runtime.checkpoint(
        affected.group_id, affected_run.run_id, label="unique-final",
        request_key="recovery-unique-final",
    )
    missed = server.task_runtime.store.events_after(affected_run.run_id, 1)[-1]
    server.task_runtime.checkpoint(
        active.group_id, active_run.run_id, label="healthy-final",
        request_key="recovery-healthy-final",
    )
    healthy = server.task_runtime.store.events_after(active_run.run_id, 1)[-1]
    await _wait_for(lambda: any(
        item.get("event", {}).get("event_id") == healthy.event_id for item in socket.messages
    ), timeout=5)
    await _wait_for(lambda: any(
        item.get("type") == "resync_required"
        and item.get("project_id") == "project_recovery_background"
        for item in socket.messages
    ), timeout=5)
    assert not any(
        item.get("event", {}).get("event_id") == missed.event_id for item in socket.messages
    )
    recovery = next(
        item for item in socket.messages
        if item.get("type") == "resync_required"
        and item.get("project_id") == "project_recovery_background"
    )
    markers = server.task_runtime.store.live_recoveries()
    assert len(markers) == 1 and markers[0]["recovery_token"] == recovery["recovery_token"]

    await server._cmd_request_snapshot(client, {  # noqa: SLF001
        "project_id": recovery["project_id"],
        "recovery_token": recovery["recovery_token"],
        "recovery_generation": recovery["recovery_generation"],
    })
    snapshot = next(
        item for item in reversed(socket.messages)
        if item.get("type") == "task_runtime_result" and item.get("action") == "snapshot"
    )
    projected = next(
        item for item in snapshot["task_runtime"]["runs"]
        if item["run_id"] == affected_run.run_id
    )
    assert projected["last_seq"] == missed.seq
    assert projected["group_id"] == affected.group_id
    assert len(server.task_runtime.store.live_recoveries()) == 1
    await server._cmd_task_runtime_recovery_ack(client, {  # noqa: SLF001
        "project_id": recovery["project_id"],
        "group_id": affected.group_id,
        "recovery_token": recovery["recovery_token"],
        "recovery_generation": recovery["recovery_generation"],
        "snapshot_cursor": snapshot["snapshot_cursor"],
    })
    assert server.task_runtime.store.live_recoveries() == ()

    socket.messages.clear()
    monkeypatch.setattr(server.hub, "broadcast", original_broadcast)
    monkeypatch.setattr(server.hub, "broadcast", fail_affected)
    server.task_runtime.checkpoint(
        affected.group_id, affected_run.run_id, label="unique-final",
        request_key="recovery-after-ack",
    )
    await _wait_for(lambda: bool(server.task_runtime.store.live_recoveries()), timeout=5)
    persisted_token = str(server.task_runtime.store.live_recoveries()[0]["recovery_token"])
    monkeypatch.setattr(server.hub, "broadcast", original_broadcast)
    await _close(server)

    restarted = KnoweServer(str(tmp_path))
    assert restarted.task_runtime is not None
    restarted._start_task_runtime_event_bridge()  # noqa: SLF001
    restarted_socket = OpenCaptureSocket()
    restarted_client = Client(restarted_socket)
    restarted._admit_task_runtime_client(restarted_client)  # noqa: SLF001
    await restarted_client.release_live_replay_fence()
    await restarted._notify_task_runtime_recoveries(restarted_client)  # noqa: SLF001
    replayed_marker = next(
        item for item in restarted_socket.messages if item.get("type") == "resync_required"
    )
    assert replayed_marker["project_id"] == "project_recovery_background"
    assert replayed_marker["recovery_token"] == persisted_token
    await _close(restarted)
