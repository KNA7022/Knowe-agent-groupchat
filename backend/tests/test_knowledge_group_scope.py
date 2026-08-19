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
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = rows or []

    def snapshot(self, project_id: str, _workspace: Path, limit: int | None = None) -> dict[str, object]:
        del project_id, limit
        return {"revision": 1, "updated_at": None, "counts": {}, "nodes": self.rows}

    def search(self, project_id: str, _workspace: Path, query: str, *, limit: int) -> dict[str, object]:
        del project_id, query, limit
        return {"hits": list(self.rows)}

    def read_node(self, project_id: str, _workspace: Path, ref: str) -> dict[str, object]:
        del project_id
        row = next((item for item in self.rows if item["ref"] == ref), None)
        return {"found": bool(row), "node": row} if row else {"found": False, "ref": ref}


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


@pytest.mark.asyncio
async def test_every_non_health_route_requires_one_canonical_group_and_filters_each_row(tmp_path: Path) -> None:
    repo = GroupRepository(tmp_path / "data")
    own = repo.create_group("own", legacy_project_id="matrix-project")
    repo.bind_course(own.group_id, "course-own")
    source_id = repo.link_source(own.group_id).source_id
    foreign_source = "src_" + "f" * 32
    allowed = {
        "ref": "node-own", "source_ids": [source_id, foreign_source],
        "course_ids": ["course-own", "course-x"],
        "evidence": [
            {"evidence_id": "own", "source_id": source_id, "course_id": "course-own"},
            {"evidence_id": "foreign", "source_id": foreign_source, "course_id": "course-x"},
            {"evidence_id": "ownerless"},
        ],
        "related": [
            {"ref": "related-own", "source_ids": [source_id], "course_ids": ["course-own"]},
            {"ref": "related-foreign", "source_ids": [foreign_source], "course_ids": ["course-x"]},
        ],
    }
    foreign = {"ref": "node-foreign", "source_ids": ["src_" + "f" * 32], "course_ids": ["course-x"]}
    manager = _Manager([allowed, foreign])
    assets = _Assets("matrix-project", [{
        "asset_id": "asset-foreign", "scope": "project", "status": "validated",
        "source_ids": ["src_" + "f" * 32], "course_ids": ["course-x"],
    }])
    try:
        register_project("matrix-project", manager=manager, assets=assets, workspace_provider=lambda: tmp_path)
        health = await dispatch_knowledge_http("GET", "/api/knowledge/health", "", b"", repo)
        assert health.status == 200
        for method, path, body in (
            ("GET", "/api/knowledge/projects", b""),
            ("GET", "/api/knowledge/nodes", b""),
            ("GET", "/api/knowledge/assets", b""),
            ("GET", "/api/knowledge/skillpacks", b""),
            ("GET", "/api/knowledge/skillpacks/pack-matrix-project", b""),
            ("GET", "/api/knowledge/matrix-project/assets", b""),
            ("GET", "/api/knowledge/matrix-project/assets/asset-foreign", b""),
            ("GET", "/api/knowledge/matrix-project/profile", b""),
            ("GET", "/api/knowledge/matrix-project/nodes", b""),
            ("GET", "/api/knowledge/matrix-project/search", b""),
            ("GET", "/api/knowledge/matrix-project/node", b""),
            ("POST", "/api/knowledge/matrix-project/nodes/node-own", b'{"status":"validated"}'),
            ("POST", "/api/knowledge/matrix-project/assets/asset-foreign", b'{"status":"validated"}'),
            ("POST", "/api/knowledge/matrix-project/profile", b'{"text":"x"}'),
            ("POST", "/api/knowledge/skillpacks/pack-matrix-project", b'{"status":"active"}'),
        ):
            response = await dispatch_knowledge_http(method, path, "", body, repo)
            assert response.status == 403
            assert response.body["error"] == "exactly_one_canonical_group_required"

        base = f"group_id={own.group_id}&legacy_project_id=matrix-project"
        forged_group = await dispatch_knowledge_http(
            "GET", "/api/knowledge/nodes", "group_id=grp_" + "9" * 32, b"", repo,
        )
        assert forged_group.status == 403
        forged_project = await dispatch_knowledge_http(
            "GET", "/api/knowledge/nodes", f"group_id={own.group_id}&legacy_project_id=elsewhere", b"", repo,
        )
        assert forged_project.status == 403
        forged_course = await dispatch_knowledge_http(
            "GET", "/api/knowledge/nodes", f"group_id={own.group_id}&course_id=course-x", b"", repo,
        )
        assert forged_course.status == 403

        scoped = base + f"&source_id={source_id}&course_id=course-own"
        nodes = await dispatch_knowledge_http("GET", "/api/knowledge/nodes", scoped, b"", repo)
        assert [row["ref"] for row in nodes.body["nodes"]] == ["node-own"]
        assert nodes.body["nodes"][0]["source_ids"] == [source_id]
        assert nodes.body["nodes"][0]["course_ids"] == ["course-own"]
        assert [row["evidence_id"] for row in nodes.body["nodes"][0]["evidence"]] == ["own"]
        assert [row["ref"] for row in nodes.body["nodes"][0]["related"]] == ["related-own"]
        search = await dispatch_knowledge_http(
            "GET", "/api/knowledge/matrix-project/search", scoped + "&q=x", b"", repo,
        )
        assert [row["ref"] for row in search.body["hits"]] == ["node-own"]
        detail = await dispatch_knowledge_http(
            "GET", "/api/knowledge/matrix-project/node", scoped + "&ref=node-foreign", b"", repo,
        )
        assert detail.status == 403
        allowed_detail = await dispatch_knowledge_http(
            "GET", "/api/knowledge/matrix-project/node", scoped + "&ref=node-own", b"", repo,
        )
        assert allowed_detail.status == 200
        assert [row["evidence_id"] for row in allowed_detail.body["node"]["evidence"]] == ["own"]
        narrow_profile = await dispatch_knowledge_http(
            "GET", "/api/knowledge/matrix-project/profile", scoped, b"", repo,
        )
        assert narrow_profile.status == 403
        assert narrow_profile.body["error"] == "profile_outside_narrow_group_scope"
        foreign_asset = await dispatch_knowledge_http(
            "GET", "/api/knowledge/matrix-project/assets/asset-foreign", scoped, b"", repo,
        )
        assert foreign_asset.status == 403
        foreign_write = await dispatch_knowledge_http(
            "POST", "/api/knowledge/matrix-project/assets/asset-foreign/review", scoped,
            b'{"action":"approve"}', repo,
        )
        assert foreign_write.status == 403
    finally:
        unregister_project("matrix-project")
