from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.groups import GroupNotFoundError
from backend.server import KnoweServer, ProjectIdResolutionError


class CaptureWriter:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, value: bytes) -> None:
        self.buffer.extend(value)

    def payload(self) -> tuple[int, dict]:
        head, body = bytes(self.buffer).split(b"\r\n\r\n", 1)
        status = int(head.split(b" ", 2)[1])
        return status, json.loads(body.decode("utf-8"))


@pytest.fixture
def group_server(tmp_path: Path):
    server = KnoweServer(data_dir=str(tmp_path / "data"))
    yield server
    server.store.close()  # type: ignore[union-attr]


async def test_http_rejects_conflicting_group_and_project_aliases(group_server) -> None:
    first = group_server.groups.create_group("First", legacy_project_id="project_first")
    second = group_server.groups.create_group("Second", legacy_project_id="project_second")
    group_server.hub.get_or_create("project_first", "First")
    group_server.hub.get_or_create("project_second", "Second")
    writer = CaptureWriter()

    await group_server._serve_group_operation(  # noqa: SLF001
        writer, "/groups/archive", json.dumps({
            "group_id": first.group_id, "project_id": second.legacy_project_id,
        }).encode(),
    )

    status, payload = writer.payload()
    assert status == 400
    assert "different groups" in payload["error"]
    assert group_server.groups.get(first.group_id).lifecycle.value == "active"

    delete_writer = CaptureWriter()
    token = group_server.groups.delete_preview(first.group_id).confirmation_token
    await group_server._serve_project_permanent_delete(  # noqa: SLF001
        delete_writer, json.dumps({
            "group_id": first.group_id,
            "project_id": second.legacy_project_id,
            "confirmation_token": token,
        }).encode(),
    )
    delete_status, delete_payload = delete_writer.payload()
    assert delete_status == 400
    assert "different groups" in delete_payload["error"]


async def test_roster_endpoint_rejects_expert_tool_override_bypass(group_server) -> None:
    group = group_server.groups.create_group("Safe", legacy_project_id="project_safe")
    roster = [agent.to_dict() for agent in group.roster]
    roster.append({
        "agent_id": "gag_" + "a" * 32,
        "group_id": group.group_id,
        "role": "expert",
        "display_name": "Unsafe",
        "expert": {
            "name": "Unsafe", "specialty": "ops", "responsibilities": [],
            "allowed_tools": ["read"], "platform_policy_locked": True,
        },
        "tool_overrides": ["shell_admin"],
        "created_at": "2026-08-18T00:00:00Z",
        "updated_at": "2026-08-18T00:00:00Z",
    })
    writer = CaptureWriter()

    await group_server._serve_group_operation(  # noqa: SLF001
        writer, "/groups/roster-update", json.dumps({
            "group_id": group.group_id, "roster": roster,
        }).encode(),
    )

    status, payload = writer.payload()
    assert status == 400
    assert "tool_overrides" in payload["error"]
    assert len(group_server.groups.get(group.group_id).roster) == 2


async def test_unbind_endpoints_return_addressed_group_identity_and_safe_count(group_server) -> None:
    group = group_server.groups.create_group("Bindings", legacy_project_id="project_bindings")
    group_server.groups.bind_course(group.group_id, "course_1")
    source_id = group_server.groups.link_source(group.group_id).source_id

    course_writer = CaptureWriter()
    await group_server._serve_group_operation(  # noqa: SLF001
        course_writer, "/groups/course-unbind", json.dumps({
            "group_id": group.group_id,
            "project_id": group.legacy_project_id,
            "course_id": "course_1",
        }).encode(),
    )
    course_status, course_payload = course_writer.payload()
    assert course_status == 200
    assert course_payload["group_id"] == group.group_id
    assert course_payload["project_id"] == group.legacy_project_id
    assert course_payload["removed"] is True

    source_writer = CaptureWriter()
    await group_server._serve_group_operation(  # noqa: SLF001
        source_writer, "/groups/source-unlink", json.dumps({
            "group_id": group.group_id,
            "project_id": group.legacy_project_id,
            "source_id": source_id,
        }).encode(),
    )
    source_status, source_payload = source_writer.payload()
    assert source_status == 200
    assert source_payload["group_id"] == group.group_id
    assert source_payload["reference_count"] == 0
    assert isinstance(source_payload["reference_count"], int)


async def test_duplicate_http_provisions_distinct_runtime_and_returns_duplicate_ids(
    group_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = group_server.groups.create_group("Source", legacy_project_id="project_source")
    group_server.groups.bind_course(source.group_id, "course_1")
    source_id = group_server.groups.link_source(source.group_id).source_id
    provisioned: list[tuple[str, str, bool]] = []

    async def provision(project_id, project_name, project_dir=None, **kwargs):
        provisioned.append((project_id, project_dir, kwargs.get("register_group_metadata")))
        group_server.hub.get_or_create(project_id, project_name)
        return project_id

    monkeypatch.setattr(group_server, "create_project", provision)
    workspace = tmp_path / "copy-workspace"
    workspace.mkdir()
    writer = CaptureWriter()
    body = {
        "group_id": source.group_id,
        "project_id": source.legacy_project_id,
        "name": "Copy",
        "project_dir": str(workspace),
        "request_id": "gdup_duplicate_test_001",
    }

    await group_server._serve_group_operation(  # noqa: SLF001
        writer, "/groups/duplicate", json.dumps(body).encode(),
    )

    status, payload = writer.payload()
    assert status == 200
    duplicate = group_server.groups.get(payload["group_id"])
    assert payload["group_id"] == payload["group"]["group_id"]
    assert payload["project_id"] == duplicate.legacy_project_id
    assert payload["group_id"] != source.group_id
    assert payload["project_id"] != source.legacy_project_id
    assert provisioned == [(payload["project_id"], str(workspace), False)]
    assert duplicate.source_links[0].source_id == source_id
    assert group_server.groups.source_reference_count(source_id) == 2

    # Same request id is an idempotent response retry, not a third Group/runtime.
    retry = CaptureWriter()
    await group_server._serve_group_operation(  # noqa: SLF001
        retry, "/groups/duplicate", json.dumps(body).encode(),
    )
    _, retried = retry.payload()
    assert retried["group_id"] == payload["group_id"]
    assert len(provisioned) == 1


async def test_duplicate_runtime_stays_fenced_until_retry_commits_canonical_group(
    group_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = group_server.groups.create_group("Private source", legacy_project_id="project_private")
    workspace = tmp_path / "private-copy"
    workspace.mkdir()
    emitted: list[dict] = []

    async def provision(project_id, project_name, project_dir=None, **kwargs):
        assert kwargs["register_group_metadata"] is False
        assert kwargs["publish_events"] is False
        group_server.hub.get_or_create(project_id, project_name)
        return project_id

    async def capture_event(event):
        emitted.append(dict(event))

    monkeypatch.setattr(group_server, "create_project", provision)
    monkeypatch.setattr(group_server.hub, "emit_no_seq", capture_event)
    original_commit = group_server.groups.commit_duplicate
    commit_calls = 0

    def fail_once(transaction_id):
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            raise OSError("canonical duplicate commit interrupted")
        return original_commit(transaction_id)

    monkeypatch.setattr(group_server.groups, "commit_duplicate", fail_once)
    body = {
        "group_id": source.group_id,
        "project_id": source.legacy_project_id,
        "name": "Private copy",
        "project_dir": str(workspace),
        "request_id": "gdup_publication_fence_001",
    }
    failed_writer = CaptureWriter()
    await group_server._serve_group_operation(  # noqa: SLF001
        failed_writer, "/groups/duplicate", json.dumps(body).encode(),
    )
    failed_status, _ = failed_writer.payload()
    assert failed_status == 500
    transaction = group_server.groups.duplicate_transaction(
        source.group_id, body["request_id"],
    )
    assert transaction is not None
    runtime_id = str(transaction["runtime_project_id"])
    duplicate_id = str(transaction["group_id"])
    assert runtime_id in group_server.hub.projects
    assert runtime_id in group_server._provisional_project_ids  # noqa: SLF001
    assert runtime_id not in {
        row["project_id"] for row in group_server._conversation_state_rows()  # noqa: SLF001
    }
    assert emitted == []
    with pytest.raises(GroupNotFoundError):
        group_server.groups.get(duplicate_id)
    with pytest.raises(ValueError, match="not canonically published"):
        group_server._project_created_payload(runtime_id)  # noqa: SLF001
    with pytest.raises(ProjectIdResolutionError, match="not canonically published"):
        group_server._canonical_project_id_from_request(runtime_id)  # noqa: SLF001

    retry_writer = CaptureWriter()
    await group_server._serve_group_operation(  # noqa: SLF001
        retry_writer, "/groups/duplicate", json.dumps(body).encode(),
    )
    retry_status, retried = retry_writer.payload()
    assert retry_status == 200
    assert retried["group_id"] == duplicate_id
    assert retried["project_id"] == runtime_id
    assert runtime_id not in group_server._provisional_project_ids  # noqa: SLF001
    assert runtime_id in {
        row["project_id"] for row in group_server._conversation_state_rows()  # noqa: SLF001
    }
    assert group_server.groups.get(duplicate_id).legacy_project_id == runtime_id
    assert [event["type"] for event in emitted] == ["project_created"]
    assert emitted[0]["group_id"] == duplicate_id


async def test_real_runtime_provisioning_emits_nothing_while_provisional(
    group_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_id = group_server._allocate_canonical_project_id()  # noqa: SLF001
    workspace = tmp_path / "provisional-runtime"
    workspace.mkdir()
    emitted: list[dict] = []

    async def capture_event(event):
        emitted.append(dict(event))

    monkeypatch.setattr(group_server.hub, "emit_no_seq", capture_event)
    group_server._provisional_project_ids.add(runtime_id)  # noqa: SLF001
    await group_server.create_project(
        runtime_id, "Hidden runtime", str(workspace),
        register_group_metadata=False, publish_events=False,
    )

    assert runtime_id in group_server.hub.projects
    assert runtime_id in group_server._persisted_project_ids  # noqa: SLF001
    assert emitted == []
    assert runtime_id not in {
        row["project_id"] for row in group_server._conversation_state_rows()  # noqa: SLF001
    }
    with pytest.raises(ValueError, match="not canonically published"):
        group_server._project_created_payload(runtime_id)  # noqa: SLF001


async def test_delete_state_machine_resumes_runtime_and_registry_interruptions(
    group_server, monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = group_server.groups.create_group("Delete", legacy_project_id="project_delete")
    group_server.hub.get_or_create("project_delete", "Delete")
    token = group_server.groups.delete_preview(group.group_id).confirmation_token
    runtime_calls = 0

    async def runtime_delete(*_args, **_kwargs):
        nonlocal runtime_calls
        runtime_calls += 1
        return {"project_id": "project_delete", "deleted": True, "cleanup_pending": False}

    monkeypatch.setattr(group_server, "_delete_project_permanently", runtime_delete)
    original_mark = group_server.groups.mark_runtime_deleted

    def interrupted_mark(_transaction_id):
        raise OSError("runtime-to-registry interruption")

    monkeypatch.setattr(group_server.groups, "mark_runtime_deleted", interrupted_mark)
    with pytest.raises(OSError, match="runtime-to-registry"):
        await group_server._run_confirmed_group_delete(  # noqa: SLF001
            group.group_id, "project_delete", token,
        )
    assert group_server.groups.get(group.group_id)
    assert group_server.groups.delete_transaction(group.group_id)["stage"] == "prepared"

    monkeypatch.setattr(group_server.groups, "mark_runtime_deleted", original_mark)

    async def resume_runtime(*_args, **_kwargs):
        return {"project_id": "project_delete", "deleted": True, "cleanup_pending": False}

    monkeypatch.setattr(group_server, "_resume_committed_project_delete", resume_runtime)
    result, receipt = await group_server._run_confirmed_group_delete(  # noqa: SLF001
        group.group_id, "project_delete", token, runtime_already_deleted=True,
    )
    assert result["deleted"] is True
    assert runtime_calls == 1
    with pytest.raises(GroupNotFoundError):
        group_server.groups.get(group.group_id)

    # Lost HTTP acknowledgement/replayed confirmation returns the one durable receipt.
    replay_result, replay_receipt = await group_server._run_confirmed_group_delete(  # noqa: SLF001
        group.group_id, "project_delete", token, runtime_already_deleted=True,
    )
    assert replay_result["deleted"] is True
    assert replay_receipt == receipt
    assert len(group_server.groups._load_payload()["deletion_receipts"]) == 1  # noqa: SLF001


async def test_delete_state_machine_retries_a_runtime_failure(
    group_server, monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = group_server.groups.create_group("Runtime fault", legacy_project_id="project_fault")
    group_server.hub.get_or_create("project_fault", "Runtime fault")
    token = group_server.groups.delete_preview(group.group_id).confirmation_token

    async def fail_runtime(*_args, **_kwargs):
        raise OSError("runtime deletion interrupted")

    monkeypatch.setattr(group_server, "_delete_project_permanently", fail_runtime)
    with pytest.raises(OSError, match="runtime deletion"):
        await group_server._run_confirmed_group_delete(  # noqa: SLF001
            group.group_id, "project_fault", token,
        )
    assert group_server.groups.delete_transaction(group.group_id)["stage"] == "prepared"
    assert group_server.groups.get(group.group_id).group_id == group.group_id

    async def finish_runtime(*_args, **_kwargs):
        return {"project_id": "project_fault", "deleted": True, "cleanup_pending": False}

    monkeypatch.setattr(group_server, "_delete_project_permanently", finish_runtime)
    result, receipt = await group_server._run_confirmed_group_delete(  # noqa: SLF001
        group.group_id, "project_fault", token,
    )
    assert result["deleted"] is True
    assert receipt.group_id == group.group_id
    assert group_server.groups.delete_transaction(group.group_id)["stage"] == "committed"


async def test_legacy_delete_path_requires_client_preview_token(group_server) -> None:
    writer = CaptureWriter()
    await group_server._serve_project_permanent_delete(  # noqa: SLF001
        writer, json.dumps({"project_id": "project_missing"}).encode(),
    )
    status, payload = writer.payload()
    assert status == 400
    assert "confirmation_token" in payload["error"]


async def test_legacy_delete_path_rejects_stale_and_accepts_current_client_token(
    group_server, monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = group_server.groups.create_group("Legacy", legacy_project_id="project_legacy")
    group_server.hub.get_or_create("project_legacy", "Legacy")
    stale_token = group_server.groups.delete_preview(group.group_id).confirmation_token
    group_server.groups.rename(group.group_id, "Changed")

    stale_writer = CaptureWriter()
    await group_server._serve_project_permanent_delete(  # noqa: SLF001
        stale_writer, json.dumps({
            "project_id": "project_legacy", "confirmation_token": stale_token,
        }).encode(),
    )
    stale_status, stale_payload = stale_writer.payload()
    assert stale_status == 400
    assert "stale" in stale_payload["error"]

    async def runtime_delete(*_args, **_kwargs):
        return {"project_id": "project_legacy", "deleted": True, "cleanup_pending": False}

    monkeypatch.setattr(group_server, "_delete_project_permanently", runtime_delete)
    current_token = group_server.groups.delete_preview(group.group_id).confirmation_token
    success_writer = CaptureWriter()
    await group_server._serve_project_permanent_delete(  # noqa: SLF001
        success_writer, json.dumps({
            "project_id": "project_legacy", "confirmation_token": current_token,
        }).encode(),
    )
    success_status, success_payload = success_writer.payload()
    assert success_status == 200
    assert success_payload["group_id"] == group.group_id
    assert success_payload["deletion_receipt"]["group_id"] == group.group_id
