from __future__ import annotations

from pathlib import Path

import pytest

from backend.groups import GroupRepository
from backend.knowledge_api import (
    dispatch_knowledge_http,
    register_project,
    unregister_project,
)


class _Assets:
    def __init__(self, project_id: str, rows: list[dict[str, object]]) -> None:
        self.project_id = project_id
        self.rows = rows

    def snapshot(self, project_id: str, _workspace: Path) -> dict[str, object]:
        assert project_id == self.project_id
        return {
            "revision": 1, "updated_at": "2026-08-19T00:00:00Z", "counts": {},
            "profile_exists": True, "assets": self.rows,
        }

    def read_asset(self, project_id: str, _workspace: Path, asset_id: str) -> dict[str, object]:
        row = next((item for item in self.rows if item["asset_id"] == asset_id), None)
        return {"found": bool(row), "asset": row} if row else {"found": False, "asset_id": asset_id}

    def profile_text(self, _workspace: Path) -> str:
        return f"profile:{self.project_id}"

    def list_skillpacks(self, project_id: str, _workspace: Path) -> dict[str, object]:
        return {
            "system_builtin": [],
            "project_experience": [{
                "pack_id": f"pack-{project_id}", "name": project_id, "kind": "project_experience",
                "project_id": project_id, "scope": "project", "status": "active",
            }],
            "third_party": [],
        }

    def read_skillpack(
        self, project_id: str, _workspace: Path, pack_id: str, *, active_only: bool = False,
    ) -> dict[str, object]:
        del active_only
        if pack_id != f"pack-{project_id}":
            return {"found": False, "pack_id": pack_id}
        return {"found": True, "pack": {
            "pack_id": pack_id, "name": project_id, "kind": "project_experience",
            "project_id": project_id, "scope": "project", "status": "active",
        }, "body_md": "# pack"}


class _Manager:
    def snapshot(self, project_id: str, _workspace: Path, limit: int | None = None) -> dict[str, object]:
        del project_id, limit
        return {"revision": 1, "updated_at": None, "counts": {}, "nodes": []}


@pytest.mark.asyncio
async def test_canonical_group_scope_filters_assets_profiles_and_skillpacks(tmp_path: Path) -> None:
    repo = GroupRepository(tmp_path / "data")
    own = repo.create_group("own", legacy_project_id="project-a")
    repo.bind_course(own.group_id, "course-a")
    source_id = repo.link_source(own.group_id).source_id
    other = repo.create_group("other", legacy_project_id="project-b")
    own_asset = {
        "asset_id": "asset-a", "scope": "project", "scope_set_by": "system",
        "status": "validated", "view_state": "ok", "source_ids": [source_id],
        "course_ids": ["course-a"],
    }
    private_other = {
        "asset_id": "asset-b-private", "scope": "project", "scope_set_by": "system",
        "status": "validated", "view_state": "ok", "source_ids": [], "course_ids": [],
    }
    pending_global = {
        "asset_id": "asset-b-pending", "scope": "global", "scope_set_by": "user",
        "status": "candidate", "view_state": "pending", "source_ids": [], "course_ids": [],
    }
    approved_global = {
        "asset_id": "asset-b-global", "scope": "global", "scope_set_by": "user",
        "status": "validated", "view_state": "ok", "source_ids": [], "course_ids": [],
    }
    try:
        register_project("project-a", manager=_Manager(), assets=_Assets("project-a", [own_asset]), workspace_provider=lambda: tmp_path)
        register_project("project-b", manager=_Manager(), assets=_Assets("project-b", [private_other, pending_global, approved_global]), workspace_provider=lambda: tmp_path)
        query = f"group_id={own.group_id}&legacy_project_id=project-a"

        assets = await dispatch_knowledge_http("GET", "/api/knowledge/assets", query, b"", repo)
        assert assets.status == 200
        assert {row["asset_id"] for row in assets.body["assets"]} == {"asset-a", "asset-b-global"}
        assert {row["project_id"] for row in assets.body["projects"]} == {"project-a"}

        denied_preview = await dispatch_knowledge_http(
            "GET", "/api/knowledge/project-b/assets/asset-b-private", query, b"", repo,
        )
        assert denied_preview.status == 403
        allowed_global = await dispatch_knowledge_http(
            "GET", "/api/knowledge/project-b/assets/asset-b-global", query, b"", repo,
        )
        assert allowed_global.status == 200
        denied_profile = await dispatch_knowledge_http(
            "GET", "/api/knowledge/project-b/profile", query, b"", repo,
        )
        assert denied_profile.status == 403

        packs = await dispatch_knowledge_http("GET", "/api/knowledge/skillpacks", query, b"", repo)
        assert {row["pack_id"] for row in packs.body["project_experience"]} == {"pack-project-a"}
        denied_pack = await dispatch_knowledge_http(
            "GET", "/api/knowledge/skillpacks/pack-project-b", query, b"", repo,
        )
        assert denied_pack.status == 404

        forged = await dispatch_knowledge_http(
            "GET", "/api/knowledge/assets",
            f"group_id={own.group_id}&source_id={'src_' + 'f' * 32}", b"", repo,
        )
        assert forged.status == 403
        assert other.group_id != own.group_id
    finally:
        unregister_project("project-a")
        unregister_project("project-b")

