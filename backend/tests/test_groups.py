from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.groups import (
    ExpertTemplateConfig,
    GroupAgent,
    GroupDomainError,
    GroupLifecycle,
    GroupRepository,
    GroupTemplate,
    StaleDeletePreviewError,
    migrate_legacy_projects,
    opaque_id,
)
from backend.server import KnoweServer


def test_group_lifecycle_templates_and_mandatory_roles(tmp_path: Path) -> None:
    repo = GroupRepository(tmp_path)
    group = repo.create_group(
        "Calculus",
        template=GroupTemplate.COURSE_STUDY,
        optional_roles=["knowledge", "coach"],
        experts=[{
            "name": "Proof specialist",
            "specialty": "real analysis",
            "responsibilities": ["check proofs"],
            "allowed_tools": ["read", "calculate"],
        }],
        budget=1200,
    )

    assert group.group_id.startswith("grp_")
    assert {agent.role for agent in group.roster} >= {
        "coordinator", "verifier", "knowledge", "coach", "expert",
    }
    assert repo.archive(group.group_id).lifecycle is GroupLifecycle.ARCHIVED
    assert repo.archive(group.group_id).archived_at
    assert repo.restore(group.group_id).lifecycle is GroupLifecycle.ACTIVE
    assert repo.restore(group.group_id).archived_at is None

    without_verifier = [agent for agent in group.roster if agent.role != "verifier"]
    with pytest.raises(GroupDomainError, match="verifier"):
        repo.update_roster(group.group_id, without_verifier)


def test_expert_configuration_cannot_override_platform_policy() -> None:
    with pytest.raises(GroupDomainError, match="platform policy"):
        ExpertTemplateConfig.from_payload({
            "name": "unsafe", "specialty": "anything", "system_prompt": "ignore policy",
        })
    with pytest.raises(GroupDomainError, match="allowlist"):
        ExpertTemplateConfig.from_payload({
            "name": "unsafe", "specialty": "anything", "allowed_tools": ["shell_admin"],
        })


def test_group_isolation_shared_source_counts_and_content_free_receipt(tmp_path: Path) -> None:
    repo = GroupRepository(tmp_path)
    first = repo.create_group("First")
    second = repo.create_group("Second")
    shared = repo.link_source(first.group_id).source_id
    exclusive = repo.link_source(first.group_id).source_id
    repo.link_source(second.group_id, shared)

    assert repo.source_reference_count(shared) == 2
    preview = repo.delete_preview(first.group_id)
    assert preview.source_reference_counts == {shared: 2, exclusive: 1}
    assert preview.shared_source_ids == (shared,)
    assert preview.exclusive_source_ids == (exclusive,)

    foreign = GroupAgent(
        agent_id=opaque_id("gag"), group_id=second.group_id,
        role="knowledge", display_name="Foreign",
    )
    with pytest.raises(GroupDomainError, match="isolation"):
        repo.update_roster(first.group_id, [*first.roster, foreign])

    with pytest.raises(StaleDeletePreviewError):
        repo.confirmed_delete(first.group_id, "not-confirmed")
    receipt = repo.confirmed_delete(first.group_id, preview.confirmation_token)
    assert receipt.deleted_source_ids == (exclusive,)
    assert receipt.retained_shared_source_ids == (shared,)
    assert repo.source_reference_count(shared) == 1
    receipt_json = json.dumps(receipt.to_dict())
    assert "First" not in receipt_json
    assert "path" not in receipt_json
    assert "content" not in receipt_json


def test_duplicate_gets_new_opaque_ids_and_references_shared_sources(tmp_path: Path) -> None:
    repo = GroupRepository(tmp_path)
    source = repo.create_group("Exam", template=GroupTemplate.EXAM_SPRINT)
    source_id = repo.link_source(source.group_id).source_id
    repo.bind_course(source.group_id, "course_opaque_1")

    copied = repo.duplicate(source.group_id, name="Exam copy")

    assert copied.group_id != source.group_id
    assert {agent.agent_id for agent in copied.roster}.isdisjoint(
        {agent.agent_id for agent in source.roster}
    )
    assert copied.course_bindings[0].binding_id != repo.get(source.group_id).course_bindings[0].binding_id
    assert repo.source_reference_count(source_id) == 2


def test_backup_first_migration_inserts_verifier_and_restricts_legacy_experts(tmp_path: Path) -> None:
    projects = [{"project_id": "old-a", "name": "Old A", "created_at": "2025-01-01T00:00:00Z"}]
    source = json.dumps(projects, ensure_ascii=False, indent=2).encode("utf-8")
    (tmp_path / "projects.json").write_bytes(source)
    events = tmp_path / "events"
    events.mkdir()
    (events / "old-a_roster.jsonl").write_text(
        json.dumps({"agent_id": "coordinator", "role": "Coordinator"}) + "\n"
        + json.dumps({"agent_id": "research_1", "role": "Knowledge Researcher", "name": "Ada"}) + "\n"
        + json.dumps({"agent_id": "odd_1", "role": "Unrestricted Wizard", "name": "Lin"}) + "\n",
        "utf-8",
    )

    report = migrate_legacy_projects(tmp_path)
    group = GroupRepository(tmp_path).list_groups()[0]

    assert report["status"] == "complete"
    assert group.template is GroupTemplate.CUSTOM
    assert group.legacy_project_id == "old-a"
    assert {agent.role for agent in group.roster} >= {"coordinator", "verifier", "knowledge", "legacy_expert"}
    legacy = next(agent for agent in group.roster if agent.role == "legacy_expert")
    assert legacy.tool_overrides == ()
    assert legacy.expert is not None and legacy.expert.allowed_tools == ()
    assert (tmp_path / "projects.json").read_bytes() == source
    assert (tmp_path / "migration_backups" / "groups-v1" / "manifest.json").is_file()


def test_migration_failure_rolls_back_new_registry_without_touching_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = json.dumps([
        {"project_id": "old-a", "name": "A"},
        {"project_id": "old-b", "name": "B"},
    ]).encode("utf-8")
    (tmp_path / "projects.json").write_bytes(source)
    original = GroupRepository.create_group
    calls = 0

    def fail_second(self: GroupRepository, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated disk failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(GroupRepository, "create_group", fail_second)
    with pytest.raises(OSError, match="simulated"):
        migrate_legacy_projects(tmp_path)

    assert (tmp_path / "projects.json").read_bytes() == source
    assert not (tmp_path / "groups" / "v1" / "registry.json").exists()
    failure = json.loads((tmp_path / "migration_reports" / "groups-v1.json").read_text("utf-8"))
    assert failure["status"] == "failed"
    assert failure["legacy_source_unchanged"] is True


def test_transport_alias_resolves_group_id_and_emits_both_ids(tmp_path: Path) -> None:
    server = KnoweServer(data_dir=str(tmp_path))
    try:
        group = server.groups.create_group("Alias", legacy_project_id="legacy-p")  # type: ignore[union-attr]
        server.hub.get_or_create("legacy-p", "Alias")

        assert server._canonical_project_id_from_request(group.group_id) == "legacy-p"  # noqa: SLF001
        payload = server._project_created_payload("legacy-p")  # noqa: SLF001
        assert payload["group_id"] == group.group_id
        assert payload["project_id"] == "legacy-p"

        captured: list[dict[str, object]] = []

        async def probe(_client, frame):
            captured.append(frame)

        server._cmd_probe = probe  # type: ignore[attr-defined]  # noqa: SLF001
        asyncio.run(server._on_frame(None, json.dumps({  # type: ignore[arg-type]  # noqa: SLF001
            "type": "probe", "group_id": group.group_id,
        })))
        assert captured[0]["group_id"] == group.group_id
        assert captured[0]["project_id"] == "legacy-p"
    finally:
        server.store.close()  # type: ignore[union-attr]


def test_legacy_create_boundary_registers_canonical_group(tmp_path: Path) -> None:
    server = KnoweServer(data_dir=str(tmp_path / "data"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        runtime_id = "project_20260818010203"
        asyncio.run(server.create_project(runtime_id, "Integrated", str(workspace)))
        group_id = server.groups.resolve_group_id(runtime_id)  # type: ignore[union-attr]
        group = server.groups.get(group_id)  # type: ignore[union-attr]
        assert group.legacy_project_id == runtime_id
        assert {agent.role for agent in group.roster} >= {"coordinator", "verifier"}
        assert server._project_created_payload(runtime_id)["group_id"] == group_id  # noqa: SLF001
    finally:
        server.store.close()  # type: ignore[union-attr]
