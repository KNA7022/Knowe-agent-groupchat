import json
import pytest
from backend.hub import Hub, Client
from backend.persist import Store
from backend.task_runtime.group_content import GroupContent, REF
from backend.protected_storage import ProtectedStorageError
from backend.tests.test_protected_children import world, INSTRUCTION, ANSWER, REASONING
from backend.tests.test_protected_events import admitted, Socket
from backend.task_runtime import TaskRuntimeService


@pytest.mark.asyncio
async def test_actual_authenticated_chat_event_storage_and_restart(world, monkeypatch):
    root, groups, runtime, key = world
    group = groups.create_group("Chat", legacy_project_id="project_20261011000002")
    store = Store(root / "events")
    hub = Hub(store)
    hub.group_content = GroupContent(runtime)
    server, client = await admitted(world, hub, monkeypatch)
    await client.release_live_replay_fence()
    await hub.emit(group.legacy_project_id, {"type": "user_echo", "content": INSTRUCTION, "client_msg_id": "demo"})
    await hub.emit(group.legacy_project_id, {"type": "message", "agent_id": "coordinator", "content": ANSWER, "reasoning": REASONING})
    assert client.ws.frames[-1]["content"] == ANSWER
    assert REASONING not in json.dumps(client.ws.frames)
    refs = hub.durable_conversation(group.legacy_project_id)
    assert all(REF in event for event in refs)
    assert INSTRUCTION not in repr(hub.projects[group.legacy_project_id].ring.events())
    assert ANSWER not in repr(hub._idempotent_events)
    denied = Client(Socket())
    server._admit_protected_client(denied)
    assert not await denied.send(refs[0])
    assert not await client.send({**refs[0], "group_id": "wrong"})
    await hub.snapshot(group.legacy_project_id)
    assert client.ws.frames[-1]["conversation"][0]["content"] == INSTRUCTION
    store.flush()
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in {".sqlite3", ".jsonl"}:
            data = path.read_bytes()
            for marker in (INSTRUCTION, ANSWER, REASONING):
                assert marker.encode() not in data
    store.close()
    runtime.close()
    reopened = TaskRuntimeService(root, groups, card_master_key=key)
    try:
        authority = GroupContent(reopened)
        assert authority.hydrate(refs[0])["content"] == INSTRUCTION
        assert authority.hydrate(refs[1])["content"] == ANSWER
        assert len(authority.references(group.legacy_project_id)) == 2
        groups.archive(group.group_id)
        assert authority.hydrate(refs[0])["content"] == INSTRUCTION
        with pytest.raises(Exception):
            authority.admit(group.group_id)
    finally:
        reopened.close()


def test_pre_echo_missing_key_and_scope_substitution(world):
    root, groups, runtime, _ = world
    group = groups.create_group("No-key chat", legacy_project_id="project_20261011000003")
    no_key = TaskRuntimeService(root / "nokey", groups)
    try:
        authority = GroupContent(no_key)
        with pytest.raises(Exception):
            authority.admit(group.group_id)
        with no_key.store.db.transaction(immediate=False) as conn:
            assert conn.execute("SELECT COUNT(*) FROM studycrew_group_content_v1").fetchone()[0] == 0
        assert not no_key.store.list_runs()
    finally:
        no_key.close()


@pytest.mark.asyncio
async def test_actual_user_command_waiting_approval_private_route_and_owner_binding(world, monkeypatch):
    root, groups, runtime, _ = world
    group = groups.create_group("Ingress", legacy_project_id="project_20261011000005")
    store = Store(root / 'events')
    hub = Hub(store)
    hub.group_content = GroupContent(runtime)
    server, client = await admitted(world, hub, monkeypatch)
    server.store = store
    server._closing_projects = set()
    server._deleted_project_ids = lambda: set()
    await client.release_live_replay_fence()
    await server._cmd_user_message(client, {'project_id': group.legacy_project_id,
        'content': INSTRUCTION, 'client_msg_id': 'private-turn', 'writes': True})
    assert client.ws.frames[-1]['content'] == INSTRUCTION
    owner = runtime.store.list_runs(group_id=group.group_id)[0]
    assert owner.status.value == 'waiting_approval'
    with runtime.store.db.transaction(immediate=False) as conn:
        row = conn.execute('SELECT owner_run_id,wire FROM studycrew_group_content_v1').fetchone()
        assert row['owner_run_id'] == owner.run_id
        assert INSTRUCTION.encode() not in row['wire']
        private_rows = conn.execute('SELECT sensitive_payload_json FROM studycrew_run_events').fetchall()
        assert INSTRUCTION not in repr([dict(item) for item in private_rows])
        continuation = conn.execute('SELECT payload_json FROM studycrew_executor_continuations').fetchone()
        assert INSTRUCTION not in continuation[0]
    store.flush()
    assert INSTRUCTION not in json.dumps(store.load_all_events(group.legacy_project_id))
    store.close()
