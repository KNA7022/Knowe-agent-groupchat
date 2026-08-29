from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from backend.groups import GroupRepository
from backend.knowledge_api import (
    dispatch_knowledge_http,
    register_project,
    unregister_project,
)
from backend.knowledge_assets import KnowledgeAssetManager
from backend.knowledge_graph import KnowledgeGraphManager


def _seed_allowed_profile(
    tmp_path: Path, project_id: str,
) -> tuple[
    GroupRepository,
    KnowledgeGraphManager,
    KnowledgeAssetManager,
    Path,
    str,
    str,
]:
    repo = GroupRepository(tmp_path / "groups")
    group = repo.create_group("atomic-profile", legacy_project_id=project_id)
    repo.bind_course(group.group_id, "course-atomic")
    source_id = repo.link_source(group.group_id).source_id
    workspace = tmp_path / "workspace"
    graph_manager = KnowledgeGraphManager(tmp_path / "manager-data")
    graph_manager.ensure_project_graph(project_id, workspace)
    root = KnowledgeGraphManager.knowledge_dir(workspace)
    graph = graph_manager.load_graph(project_id, root)
    graph["revision"] = 1
    graph["updated_at"] = "2026-08-29T00:00:00Z"
    graph["profile_locked"] = False
    graph["assets"] = {
        "P-01": {
            "id": "P-01",
            "class": "preference",
            "title": "Atomic preference",
            "one_liner": "Use the allowed contributor",
            "applies_when": "always",
            "body_md": "Allowed preference body.",
            "status": "core",
            "scope": "project",
            "scope_set_by": "system",
            "needs_review": False,
            "source_ids": [source_id],
            "course_ids": ["course-atomic"],
            "evidence": [],
            "usage_events": [],
            "metrics": {},
            "created_at": "2026-08-29T00:00:00Z",
            "updated_at": "2026-08-29T00:00:00Z",
        },
    }
    graph_manager.save_graph(root, graph)
    assets = KnowledgeAssetManager(graph_manager)
    assets.profile_path(workspace).write_text(
        "# Profile\n\n- [P-01] Use the allowed contributor\n",
        encoding="utf-8",
    )
    query = (
        f"group_id={group.group_id}&legacy_project_id={project_id}"
        f"&source_id={source_id}&course_id=course-atomic"
    )
    return repo, graph_manager, assets, workspace, source_id, query


def _pause_first_profile_body_read(
    assets: KnowledgeAssetManager,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event]:
    captured = threading.Event()
    release = threading.Event()
    original_profile_text = assets.profile_text

    def pause_after_read(internal_workspace: Path | str) -> str:
        text = original_profile_text(internal_workspace)
        if not captured.is_set():
            captured.set()
            if not release.wait(timeout=5):
                raise AssertionError("profile request did not resume")
        return text

    monkeypatch.setattr(assets, "profile_text", pause_after_read)
    return captured, release


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route",
    [
        "/api/knowledge/assets",
        "/api/knowledge/atomic-profile-assets/assets",
    ],
    ids=["merged", "project"],
)
async def test_assets_profile_availability_reauthorizes_after_generation_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    project_id = "atomic-profile-assets"
    repo, graph_manager, assets, workspace, _source_id, query = _seed_allowed_profile(
        tmp_path, project_id,
    )
    captured, release = _pause_first_profile_body_read(assets, monkeypatch)
    try:
        register_project(
            project_id,
            manager=graph_manager,
            assets=assets,
            workspace_provider=lambda: workspace,
        )
        request = asyncio.create_task(
            dispatch_knowledge_http("GET", route, query, b"", repo),
        )
        assert await asyncio.to_thread(captured.wait, 5)

        replacement = await assets.set_profile(
            project_id, workspace, "PRIVATE OWNERLESS PROFILE",
        )
        assert replacement == {"ok": True}
        release.set()
        response = await request

        assert response.status == 200
        project = (
            response.body["projects"][0]
            if route == "/api/knowledge/assets"
            else response.body
        )
        assert project["profile_exists"] is False
        assert project["profile_source_ids"] == []
        assert project["profile_course_ids"] == []
        assert "PRIVATE OWNERLESS PROFILE" not in str(project)
    finally:
        release.set()
        unregister_project(project_id)


@pytest.mark.asyncio
async def test_profile_post_never_overwrites_a_newer_ownerless_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = "atomic-profile-post"
    repo, graph_manager, assets, workspace, _source_id, query = _seed_allowed_profile(
        tmp_path, project_id,
    )
    captured, release = _pause_first_profile_body_read(assets, monkeypatch)
    try:
        register_project(
            project_id,
            manager=graph_manager,
            assets=assets,
            workspace_provider=lambda: workspace,
        )
        request = asyncio.create_task(dispatch_knowledge_http(
            "POST",
            f"/api/knowledge/{project_id}/profile",
            query,
            b'{"text":"STALE AUTHORIZED EDIT"}',
            repo,
        ))
        assert await asyncio.to_thread(captured.wait, 5)

        replacement = asyncio.create_task(
            assets.set_profile(project_id, workspace, "PRIVATE OWNERLESS PROFILE"),
        )
        await asyncio.sleep(0)
        replacement_was_blocked = not replacement.done()
        release.set()
        response = await request
        assert await replacement == {"ok": True}

        final_snapshot = await assets.profile_snapshot(project_id, workspace)
        assert final_snapshot["profile_text"] == "PRIVATE OWNERLESS PROFILE\n"
        assert final_snapshot["profile_source_ids"] == []
        assert final_snapshot["profile_course_ids"] == []
        if not replacement_was_blocked:
            assert response.status == 403
    finally:
        release.set()
        unregister_project(project_id)


@pytest.mark.asyncio
async def test_profile_bundle_tracks_auto_compilation_and_deletion_generations(
    tmp_path: Path,
) -> None:
    project_id = "profile-bundle-lifecycle"
    _repo, graph_manager, assets, workspace, source_id, _query = _seed_allowed_profile(
        tmp_path, project_id,
    )
    root = KnowledgeGraphManager.knowledge_dir(workspace)
    graph = graph_manager.load_graph(project_id, root)
    graph["assets"]["P-01"]["status"] = "candidate"
    graph_manager.save_graph(root, graph)
    assets.profile_path(workspace).unlink()

    missing = await assets.profile_bundle(project_id, workspace)
    assert missing.available is False
    assert missing.body == ""
    assert missing.contributors == {"source_ids": (), "course_ids": ()}
    assert missing.locked is False

    approved = await assets.review(project_id, workspace, "P-01", "approve")
    assert approved["ok"] is True
    compiled = await assets.profile_bundle(project_id, workspace)
    assert compiled.available is True
    assert "- [P-01] Use the allowed contributor" in compiled.body
    assert compiled.source_ids == (source_id,)
    assert compiled.course_ids == ("course-atomic",)
    assert compiled.locked is False
    assert compiled.generation != missing.generation

    assert await assets.purge(project_id, workspace, "P-01") == {
        "ok": True,
        "asset_id": "P-01",
    }
    deleted = await assets.profile_bundle(project_id, workspace)
    assert deleted.available is False
    assert deleted.body == ""
    assert deleted.source_ids == ()
    assert deleted.course_ids == ()
    assert deleted.locked is False
    assert deleted.generation not in {missing.generation, compiled.generation}


@pytest.mark.asyncio
async def test_profile_lock_is_never_entered_from_a_foreign_registered_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = "profile-loop-owner"
    repo, graph_manager, assets, workspace, _source_id, query = _seed_allowed_profile(
        tmp_path, project_id,
    )
    foreign_loop = asyncio.new_event_loop()

    def reject_lock_entry(_workspace: Path | str) -> asyncio.Lock:
        raise AssertionError("foreign event loop attempted to enter root_lock")

    monkeypatch.setattr(graph_manager, "root_lock", reject_lock_entry)
    try:
        register_project(
            project_id,
            manager=graph_manager,
            assets=assets,
            workspace_provider=lambda: workspace,
            loop=foreign_loop,
        )
        direct = await dispatch_knowledge_http(
            "GET", f"/api/knowledge/{project_id}/assets", query, b"", repo,
        )
        detail = await dispatch_knowledge_http(
            "GET", f"/api/knowledge/{project_id}/profile", query, b"", repo,
        )
        post = await dispatch_knowledge_http(
            "POST",
            f"/api/knowledge/{project_id}/profile",
            query,
            b'{"text":"must not run"}',
            repo,
        )
        merged = await dispatch_knowledge_http(
            "GET", "/api/knowledge/assets", query, b"", repo,
        )

        for response in (direct, detail, post):
            assert response.status == 503
            assert response.body["error"] == "engine_loop_unavailable"
        assert merged.status == 200
        assert merged.body["projects"][0]["profile_exists"] is False
        assert merged.body["projects"][0]["profile_source_ids"] == []
        assert merged.body["projects"][0]["profile_course_ids"] == []
    finally:
        unregister_project(project_id)
        foreign_loop.close()
