from __future__ import annotations

from pathlib import Path

import pytest

from backend.groups import GroupRepository
from backend.knowledge_assets import KnowledgeAssetManager
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
        source_ids = sorted({
            str(value) for row in self.rows for value in row.get("source_ids", [])
        })
        course_ids = sorted({
            str(value) for row in self.rows for value in row.get("course_ids", [])
        })
        return {
            "system_builtin": [],
            "project_experience": [{
                "pack_id": f"pack-{project_id}", "name": project_id, "kind": "project_experience",
                "project_id": project_id, "scope": "project", "status": "active",
                "source_ids": source_ids, "course_ids": course_ids,
            }],
            "third_party": [],
        }

    def read_skillpack(
        self, project_id: str, _workspace: Path, pack_id: str, *, active_only: bool = False,
    ) -> dict[str, object]:
        del active_only
        if pack_id != f"pack-{project_id}":
            return {"found": False, "pack_id": pack_id}
        packs = self.list_skillpacks(project_id, _workspace)
        rows = packs["project_experience"]
        assert isinstance(rows, list) and isinstance(rows[0], dict)
        return {"found": True, "pack": dict(rows[0]), "body_md": "# pack"}


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
        "ref": "node-own", "source_ids": [source_id],
        "course_ids": ["course-own"],
        "evidence": [
            {"evidence_id": "own", "source_id": source_id, "course_id": "course-own"},
        ],
        "related": [
            {"ref": "related-own", "source_ids": [source_id], "course_ids": ["course-own"]},
        ],
    }
    mixed = {
        "ref": "node-mixed", "source_ids": [source_id, foreign_source],
        "course_ids": ["course-own", "course-x"], "summary": "OWN + FOREIGN-NODE-SECRET",
        "evidence": [{"source_id": foreign_source, "course_id": "course-x"}],
    }
    foreign = {"ref": "node-foreign", "source_ids": ["src_" + "f" * 32], "course_ids": ["course-x"]}
    manager = _Manager([allowed, mixed, foreign])
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
        mixed_detail = await dispatch_knowledge_http(
            "GET", "/api/knowledge/matrix-project/node", scoped + "&ref=node-mixed", b"", repo,
        )
        assert mixed_detail.status == 403
        mixed_write = await dispatch_knowledge_http(
            "POST", "/api/knowledge/matrix-project/nodes/node-mixed", scoped,
            b'{"summary":"changed"}', repo,
        )
        assert mixed_write.status == 403
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


@pytest.mark.asyncio
async def test_mixed_indivisible_objects_fail_closed_across_read_and_write_matrix(tmp_path: Path) -> None:
    repo = GroupRepository(tmp_path / "data")
    group = repo.create_group("own", legacy_project_id="mixed-project")
    repo.bind_course(group.group_id, "course-own")
    source_id = repo.link_source(group.group_id).source_id
    foreign_source = "src_" + "e" * 32
    mixed_asset = {
        "asset_id": "asset-mixed", "scope": "project", "scope_set_by": "system",
        "status": "validated", "view_state": "ok",
        "source_ids": [source_id, foreign_source], "course_ids": ["course-own", "course-x"],
        "summary": "OWN + FOREIGN-ASSET-SECRET", "body_md": "FOREIGN-ASSET-BODY",
    }
    clean_global = {
        "asset_id": "global-clean", "scope": "global", "scope_set_by": "user",
        "status": "validated", "view_state": "ok", "source_ids": [], "course_ids": [],
        "summary": "clean approved global",
    }
    foreign_global = {
        "asset_id": "global-foreign", "scope": "global", "scope_set_by": "user",
        "status": "core", "view_state": "ok", "source_ids": [foreign_source],
        "course_ids": ["course-x"], "summary": "FOREIGN-GLOBAL-SECRET",
    }
    pending_own_global = {
        "asset_id": "global-own-pending", "scope": "global", "scope_set_by": "system",
        "status": "candidate", "view_state": "pending", "source_ids": [], "course_ids": [],
        "summary": "PENDING-OWN-GLOBAL-MARKER",
    }

    class _MixedAssets(_Assets):
        def list_skillpacks(self, project_id: str, _workspace: Path) -> dict[str, object]:
            return {"system_builtin": [], "third_party": [], "project_experience": [{
                "pack_id": "pack-mixed", "name": "mixed", "kind": "project_experience",
                "project_id": project_id, "scope": "project", "status": "active",
                "source_ids": [source_id, foreign_source],
                "course_ids": ["course-own", "course-x"],
                "description": "FOREIGN-PACK-SECRET", "instructions": "FOREIGN-PACK-INSTRUCTIONS",
            }]}

        def read_skillpack(
            self, project_id: str, _workspace: Path, pack_id: str, *, active_only: bool = False,
        ) -> dict[str, object]:
            del active_only
            if pack_id != "pack-mixed":
                return {"found": False, "pack_id": pack_id}
            return {"found": True, "pack": self.list_skillpacks(project_id, _workspace)["project_experience"][0],
                    "body_md": "FOREIGN-PACK-BODY"}

    manager = _Manager([{
        "ref": "node-mixed", "source_ids": [source_id, foreign_source],
        "course_ids": ["course-own", "course-x"], "summary": "FOREIGN-NODE-SECRET",
    }])
    assets = _MixedAssets(
        "mixed-project", [mixed_asset, clean_global, foreign_global, pending_own_global],
    )
    try:
        register_project(
            "mixed-project", manager=manager, assets=assets, workspace_provider=lambda: tmp_path,
        )
        scoped = (f"group_id={group.group_id}&legacy_project_id=mixed-project"
                  f"&source_id={source_id}&course_id=course-own")

        asset_list = await dispatch_knowledge_http("GET", "/api/knowledge/assets", scoped, b"", repo)
        assert asset_list.status == 200
        serialized_assets = str(asset_list.body)
        assert "asset-mixed" not in serialized_assets
        assert "FOREIGN" not in serialized_assets
        asset_detail = await dispatch_knowledge_http(
            "GET", "/api/knowledge/mixed-project/assets/asset-mixed", scoped, b"", repo,
        )
        assert asset_detail.status == 403
        asset_write = await dispatch_knowledge_http(
            "POST", "/api/knowledge/mixed-project/assets/asset-mixed", scoped,
            b'{"title":"changed"}', repo,
        )
        assert asset_write.status == 403

        pack_list = await dispatch_knowledge_http("GET", "/api/knowledge/skillpacks", scoped, b"", repo)
        assert "pack-mixed" not in str(pack_list.body)
        assert "FOREIGN" not in str(pack_list.body)
        pack_detail = await dispatch_knowledge_http(
            "GET", "/api/knowledge/skillpacks/pack-mixed", scoped, b"", repo,
        )
        assert pack_detail.status == 404
        pack_write = await dispatch_knowledge_http(
            "POST", "/api/knowledge/skillpacks/pack-mixed", scoped,
            b'{"status":"active"}', repo,
        )
        assert pack_write.status == 404

        nodes = await dispatch_knowledge_http("GET", "/api/knowledge/nodes", scoped, b"", repo)
        search = await dispatch_knowledge_http(
            "GET", "/api/knowledge/mixed-project/search", scoped + "&q=x", b"", repo,
        )
        node_detail = await dispatch_knowledge_http(
            "GET", "/api/knowledge/mixed-project/node", scoped + "&ref=node-mixed", b"", repo,
        )
        node_write = await dispatch_knowledge_http(
            "POST", "/api/knowledge/mixed-project/nodes/node-mixed", scoped,
            b'{"summary":"changed"}', repo,
        )
        assert nodes.body["nodes"] == []
        assert search.body["hits"] == []
        assert node_detail.status == 403
        assert node_write.status == 403
        profile = await dispatch_knowledge_http(
            "GET", "/api/knowledge/mixed-project/profile", scoped, b"", repo,
        )
        assert profile.status == 403
        profile_write = await dispatch_knowledge_http(
            "POST", "/api/knowledge/mixed-project/profile", scoped,
            b'{"text":"changed"}', repo,
        )
        assert profile_write.status == 403

        broad = f"group_id={group.group_id}&legacy_project_id=mixed-project"
        globals_result = await dispatch_knowledge_http("GET", "/api/knowledge/assets", broad, b"", repo)
        global_ids = {row["asset_id"] for row in globals_result.body["assets"]}
        assert "global-clean" in global_ids
        assert "global-foreign" not in global_ids
        assert "global-own-pending" not in global_ids
        assert "FOREIGN-GLOBAL-SECRET" not in str(globals_result.body)
        assert "PENDING-OWN-GLOBAL-MARKER" not in str(globals_result.body)
        pending_detail = await dispatch_knowledge_http(
            "GET", "/api/knowledge/mixed-project/assets/global-own-pending", broad, b"", repo,
        )
        pending_write = await dispatch_knowledge_http(
            "POST", "/api/knowledge/mixed-project/assets/global-own-pending", broad,
            b'{"title":"changed"}', repo,
        )
        assert pending_detail.status == 403
        assert pending_write.status == 403
    finally:
        unregister_project("mixed-project")


@pytest.mark.asyncio
async def test_broad_ownerless_objects_fail_closed_except_strict_clean_global(tmp_path: Path) -> None:
    repo = GroupRepository(tmp_path / "data")
    group = repo.create_group("broad", legacy_project_id="broad-project")
    repo.bind_course(group.group_id, "course-broad")
    source_id = repo.link_source(group.group_id).source_id
    matching_asset = {
        "asset_id": "asset-matching", "scope": "project", "scope_set_by": "system",
        "status": "validated", "view_state": "ok",
        "source_ids": [source_id], "course_ids": ["course-broad"],
    }
    ownerless_asset = {
        "asset_id": "asset-ownerless", "scope": "project", "scope_set_by": "system",
        "status": "validated", "view_state": "ok", "source_ids": [], "course_ids": [],
    }
    clean_global_asset = {
        "asset_id": "asset-global-clean", "scope": "global", "scope_set_by": "user",
        "status": "core", "view_state": "ok", "source_ids": [], "course_ids": [],
    }

    class _BroadAssets(_Assets):
        profile_source_ids: list[str] = []
        profile_course_ids: list[str] = []

        def snapshot(self, project_id: str, workspace: Path) -> dict[str, object]:
            result = super().snapshot(project_id, workspace)
            result["profile_source_ids"] = list(self.profile_source_ids)
            result["profile_course_ids"] = list(self.profile_course_ids)
            return result

        def list_skillpacks(self, project_id: str, _workspace: Path) -> dict[str, object]:
            common = {"project_id": project_id, "status": "active", "view_state": "ok"}
            return {
                "project_experience": [
                    {
                        **common, "pack_id": "pack-matching", "name": "matching",
                        "kind": "project_experience", "scope": "project",
                        "source_ids": [source_id], "course_ids": ["course-broad"],
                    },
                    {
                        **common, "pack_id": "pack-ownerless", "name": "ownerless",
                        "kind": "project_experience", "scope": "project",
                        "source_ids": [], "course_ids": [],
                    },
                    {
                        **common, "pack_id": "pack-global-unapproved", "name": "unapproved",
                        "kind": "project_experience", "scope": "global",
                        "source_ids": [], "course_ids": [],
                    },
                    {
                        **common, "pack_id": "pack-global-clean", "name": "clean",
                        "kind": "project_experience", "scope": "global",
                        "scope_set_by": "user", "approval_status": "core",
                        "source_ids": [], "course_ids": [],
                    },
                ],
                "system_builtin": [{
                    "pack_id": "pack-system", "name": "system", "kind": "system_builtin",
                    "project_id": "", "scope": "global", "status": "active",
                    "view_state": "ok", "immutable": True, "source_ids": [], "course_ids": [],
                }],
                "third_party": [],
            }

        def read_skillpack(
            self, project_id: str, workspace: Path, pack_id: str, *, active_only: bool = False,
        ) -> dict[str, object]:
            del active_only
            for bucket in ("system_builtin", "project_experience", "third_party"):
                rows = self.list_skillpacks(project_id, workspace)[bucket]
                assert isinstance(rows, list)
                for row in rows:
                    if isinstance(row, dict) and row.get("pack_id") == pack_id:
                        return {"found": True, "pack": row, "body_md": f"# {pack_id}"}
            return {"found": False, "pack_id": pack_id}

    assets = _BroadAssets(
        "broad-project", [matching_asset, ownerless_asset, clean_global_asset],
    )
    try:
        register_project(
            "broad-project", manager=_Manager(), assets=assets, workspace_provider=lambda: tmp_path,
        )
        broad = f"group_id={group.group_id}&legacy_project_id=broad-project"

        asset_list = await dispatch_knowledge_http("GET", "/api/knowledge/assets", broad, b"", repo)
        assert {row["asset_id"] for row in asset_list.body["assets"]} == {
            "asset-matching", "asset-global-clean",
        }
        project_row = asset_list.body["projects"][0]
        assert project_row["profile_exists"] is False
        assert project_row["profile_source_ids"] == []
        assert project_row["profile_course_ids"] == []
        ownerless_asset_detail = await dispatch_knowledge_http(
            "GET", "/api/knowledge/broad-project/assets/asset-ownerless", broad, b"", repo,
        )
        assert ownerless_asset_detail.status == 403

        pack_list = await dispatch_knowledge_http("GET", "/api/knowledge/skillpacks", broad, b"", repo)
        assert {row["pack_id"] for row in pack_list.body["project_experience"]} == {
            "pack-matching", "pack-global-clean",
        }
        assert pack_list.body["system_builtin"] == []
        ownerless_pack_detail = await dispatch_knowledge_http(
            "GET", "/api/knowledge/skillpacks/pack-ownerless", broad, b"", repo,
        )
        assert ownerless_pack_detail.status == 404
        clean_pack_detail = await dispatch_knowledge_http(
            "GET", "/api/knowledge/skillpacks/pack-global-clean", broad, b"", repo,
        )
        assert clean_pack_detail.status == 200

        ownerless_profile = await dispatch_knowledge_http(
            "GET", "/api/knowledge/broad-project/profile", broad, b"", repo,
        )
        assert ownerless_profile.status == 403
        assets.profile_source_ids = [source_id]
        assets.profile_course_ids = ["course-broad"]
        matching_profile_list = await dispatch_knowledge_http(
            "GET", "/api/knowledge/assets", broad, b"", repo,
        )
        matching_project_row = matching_profile_list.body["projects"][0]
        assert matching_project_row["profile_exists"] is True
        assert matching_project_row["profile_source_ids"] == [source_id]
        assert matching_project_row["profile_course_ids"] == ["course-broad"]
        matching_profile = await dispatch_knowledge_http(
            "GET", "/api/knowledge/broad-project/profile", broad, b"", repo,
        )
        assert matching_profile.status == 200
        assert matching_profile.body["source_ids"] == [source_id]
        assert matching_profile.body["course_ids"] == ["course-broad"]
    finally:
        unregister_project("broad-project")


def test_real_profile_and_project_skill_rows_preserve_contributor_proof(tmp_path: Path) -> None:
    source_id = "src_" + "a" * 32
    graph: dict[str, object] = {
        "revision": 1,
        "updated_at": "2026-08-29T00:00:00Z",
        "sources": {},
        "assets": {
            "P-01": {
                "id": "P-01", "class": "preference", "title": "Preference",
                "one_liner": "Use the proved source", "applies_when": "always",
                "body_md": "Follow the proved source.", "status": "core",
                "scope": "project", "scope_set_by": "system", "needs_review": False,
                "source_ids": [source_id], "course_ids": ["course-real"],
                "evidence": [], "usage_events": [], "metrics": {},
                "created_at": "2026-08-29T00:00:00Z",
                "updated_at": "2026-08-29T00:00:00Z",
            },
        },
    }

    class _GraphStub:
        def load_graph(self, project_id: str, _root: Path) -> dict[str, object]:
            assert project_id == "real-project"
            return graph

    manager = KnowledgeAssetManager(_GraphStub())  # type: ignore[arg-type]
    profile_path = manager.profile_path(tmp_path)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text("# Profile\n\n- [P-01] Use the proved source\n", encoding="utf-8")

    snapshot = manager.snapshot("real-project", tmp_path)
    assert snapshot["profile_exists"] is True
    assert snapshot["profile_source_ids"] == [source_id]
    assert snapshot["profile_course_ids"] == ["course-real"]
    assert snapshot["assets"][0]["source_ids"] == [source_id]
    assert snapshot["assets"][0]["course_ids"] == ["course-real"]

    packs = manager._sync_project_experience_skills("real-project", tmp_path, graph)  # noqa: SLF001
    assert len(packs) == 1
    assert packs[0]["source_ids"] == [source_id]
    assert packs[0]["course_ids"] == ["course-real"]
    assert packs[0]["approval_status"] == "core"
    assert packs[0]["scope_set_by"] == "system"

    graph["profile_locked"] = True
    locked = manager.snapshot("real-project", tmp_path)
    assert locked["profile_exists"] is True
    assert locked["profile_source_ids"] == []
    assert locked["profile_course_ids"] == []
