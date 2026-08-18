from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from websockets.protocol import State

from backend.hub import Client
from backend.server import KnoweServer
from backend.task_runtime.contracts import RiskLevel, TaskStatus


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
    server.hub.add_client(Client(socket))
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
    server.hub.add_client(Client(socket))
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
    server.hub.add_client(Client(socket))
    server._start_task_runtime_event_bridge()  # noqa: SLF001

    entered = asyncio.Event()
    release = asyncio.Event()
    original_emit = server.hub.emit

    async def delayed_emit(project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("event", {}).get("public_payload", {}).get("label") == "racing":
            entered.set()
            await release.wait()
        return await original_emit(project_id, payload)

    monkeypatch.setattr(server.hub, "emit", delayed_emit)
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
