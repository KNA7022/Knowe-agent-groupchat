"""Knowledge HTTP routes dispatched by the existing asyncio control server.

There is deliberately no listener, thread, wildcard CORS policy, or cross-thread coroutine bridge
in this module.  Engines register their project managers; ``dispatch_knowledge_http`` is called by
``backend.server`` after the shared Runtime Secret and ordinary HTTP limits have been verified.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine
from urllib.parse import parse_qs, unquote

from backend.groups import GroupLifecycle, GroupNotFoundError, GroupRepository

log = logging.getLogger("knowe.knowledge.api")

_OVERRIDE_TIMEOUT_S = 15.0
_MAX_BODY_BYTES = 64 * 1024


@dataclass
class _ProjectReg:
    project_id: str
    manager: Any
    workspace_provider: Callable[[], Path]
    loop: asyncio.AbstractEventLoop | None = None
    order: int = field(default=0)
    assets: Any | None = None


@dataclass(frozen=True)
class KnowledgeHttpResponse:
    status: int
    body: dict[str, Any]


@dataclass(frozen=True)
class _KnowledgeScope:
    group_ids: frozenset[str]
    project_ids: frozenset[str]
    allowed_source_ids: frozenset[str]
    allowed_course_ids: frozenset[str]
    source_ids: frozenset[str]
    course_id: str | None


class _KnowledgeScopeDenied(RuntimeError):
    pass


_lock = threading.Lock()
_projects: dict[str, _ProjectReg] = {}
_order_seq = 0


def register_project(
    project_id: str,
    *,
    manager: Any,
    workspace_provider: Callable[[], Path],
    loop: asyncio.AbstractEventLoop | None = None,
    assets: Any | None = None,
) -> None:
    """Register or refresh one project's managers.  This operation is idempotent."""
    global _order_seq
    if not project_id or project_id == "__platform__" or manager is None:
        return
    with _lock:
        existing = _projects.get(project_id)
        order = existing.order if existing else _order_seq
        if existing is None:
            _order_seq += 1
        _projects[project_id] = _ProjectReg(
            project_id=project_id,
            manager=manager,
            workspace_provider=workspace_provider,
            loop=loop,
            order=order,
            assets=assets,
        )


def unregister_project(project_id: str) -> None:
    with _lock:
        _projects.pop(project_id, None)


def project_count() -> int:
    with _lock:
        return len(_projects)


def _registrations() -> list[_ProjectReg]:
    with _lock:
        return sorted(_projects.values(), key=lambda row: row.order)


def _registration(project_id: str) -> _ProjectReg | None:
    with _lock:
        return _projects.get(project_id)


def _ok(payload: dict[str, Any], status: int = 200) -> KnowledgeHttpResponse:
    return KnowledgeHttpResponse(status, payload)


def _fail(status: int, error: str, **extra: Any) -> KnowledgeHttpResponse:
    return _ok({"ok": False, "error": error, **extra}, status)


def _int_of(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _resolve_scope(
    query: dict[str, list[str]], groups: GroupRepository | None,
) -> _KnowledgeScope:
    requested_groups = frozenset(filter(None, query.get("group_id") or ()))
    if len(requested_groups) != 1:
        raise _KnowledgeScopeDenied("exactly_one_canonical_group_required")
    if groups is None:
        raise _KnowledgeScopeDenied("canonical_group_scope_unavailable")
    project_ids: set[str] = set()
    source_ids: set[str] = set()
    course_ids: set[str] = set()
    for group_id in requested_groups:
        try:
            group = groups.get(group_id)
        except GroupNotFoundError as exc:
            raise _KnowledgeScopeDenied("unknown_group") from exc
        if group.lifecycle not in {GroupLifecycle.ACTIVE, GroupLifecycle.ARCHIVED}:
            raise _KnowledgeScopeDenied("group_scope_unavailable")
        if group.legacy_project_id:
            project_ids.add(group.legacy_project_id)
        source_ids.update(link.source_id for link in group.source_links)
        course_ids.update(binding.course_id for binding in group.course_bindings)
    requested_projects = set(filter(None, query.get("legacy_project_id") or ()))
    if len(requested_projects) > 1:
        raise _KnowledgeScopeDenied("exactly_one_legacy_project_allowed")
    requested_sources = set(filter(None, query.get("source_id") or ()))
    requested_courses = set(filter(None, query.get("course_id") or ()))
    if len(requested_courses) > 1:
        raise _KnowledgeScopeDenied("exactly_one_course_scope_allowed")
    requested_course = next(iter(requested_courses), None)
    if requested_projects and not requested_projects.issubset(project_ids):
        raise _KnowledgeScopeDenied("project_outside_group_scope")
    requested_route_projects = set(filter(None, query.get("project") or ()))
    if requested_route_projects and not requested_route_projects.issubset(project_ids):
        raise _KnowledgeScopeDenied("project_outside_group_scope")
    if requested_sources and not requested_sources.issubset(source_ids):
        raise _KnowledgeScopeDenied("source_outside_group_scope")
    if requested_course and requested_course not in course_ids:
        raise _KnowledgeScopeDenied("course_outside_group_scope")
    return _KnowledgeScope(
        group_ids=requested_groups,
        project_ids=frozenset(requested_projects or project_ids),
        allowed_source_ids=frozenset(source_ids),
        allowed_course_ids=frozenset(course_ids),
        source_ids=frozenset(requested_sources),
        course_id=requested_course,
    )


def _structured_ids(row: dict[str, Any], key: str) -> frozenset[str]:
    raw = row.get(key)
    return frozenset(str(value) for value in raw if value) if isinstance(raw, list) else frozenset()


def _approved_global(row: dict[str, Any]) -> bool:
    return (
        row.get("scope") == "global"
        and row.get("scope_set_by") == "user"
        and row.get("status") in {"validated", "core"}
        and row.get("view_state") not in {"pending", "retired"}
    )


def _ownership_ids(value: Any) -> tuple[frozenset[str], frozenset[str]]:
    """Collect every structured contributor identity from an indivisible object.

    Knowledge bodies, summaries and skill instructions cannot be projected by contributor.
    Authorization therefore has to cover the complete object graph, not just the labels on its
    outer row.  Only explicit ownership keys are interpreted; free text is never treated as an
    authority signal.
    """
    sources: set[str] = set()
    courses: set[str] = set()

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            source = item.get("source_id")
            if source:
                sources.add(str(source))
            course = item.get("course_id")
            if course:
                courses.add(str(course))
            sources.update(_structured_ids(item, "source_ids"))
            courses.update(_structured_ids(item, "course_ids"))
            for nested in item.values():
                if isinstance(nested, (dict, list)):
                    walk(nested)
        elif isinstance(item, list):
            for nested in item:
                walk(nested)

    walk(value)
    return frozenset(sources), frozenset(courses)


def _contributors_allowed(value: Any, scope: _KnowledgeScope) -> bool:
    sources, courses = _ownership_ids(value)
    effective_sources = scope.source_ids or scope.allowed_source_ids
    effective_courses = frozenset({scope.course_id}) if scope.course_id else scope.allowed_course_ids

    # A narrow request is an exact data boundary: an ownerless or partially matching aggregate
    # cannot prove that its body belongs to the requested source/course.
    if scope.source_ids and (not sources or not sources.issubset(effective_sources)):
        return False
    if scope.course_id and (not courses or not courses.issubset(effective_courses)):
        return False
    # Broad Group access still must reject private contributors from other Groups.
    if sources and not sources.issubset(effective_sources):
        return False
    if courses and not courses.issubset(effective_courses):
        return False
    return True


def _row_allowed(row: dict[str, Any], project_id: str, scope: _KnowledgeScope) -> bool:
    if project_id not in scope.project_ids and not _approved_global(row):
        return False
    return _contributors_allowed(row, scope)


def _sanitize_scoped_row(row: dict[str, Any], scope: _KnowledgeScope) -> dict[str, Any]:
    """Remove ownership labels and nested evidence outside a requested narrow scope."""
    sanitized = dict(row)
    if scope.source_ids:
        sanitized["source_ids"] = sorted(_structured_ids(row, "source_ids") & scope.source_ids)
    if scope.course_id:
        sanitized["course_ids"] = [
            scope.course_id
        ] if scope.course_id in _structured_ids(row, "course_ids") else []
    narrow = bool(scope.source_ids or scope.course_id)
    if not narrow:
        return sanitized

    def nested_allowed(item: dict[str, Any]) -> bool:
        item_sources = set(_structured_ids(item, "source_ids"))
        if item.get("source_id"):
            item_sources.add(str(item["source_id"]))
        item_courses = set(_structured_ids(item, "course_ids"))
        if item.get("course_id"):
            item_courses.add(str(item["course_id"]))
        if scope.source_ids and not (item_sources & scope.source_ids):
            return False
        if scope.course_id and scope.course_id not in item_courses:
            return False
        return True

    for key in ("evidence", "related", "nodes", "sources"):
        raw_items = row.get(key)
        if not isinstance(raw_items, list):
            continue
        kept: list[Any] = []
        for item in raw_items:
            if isinstance(item, dict) and nested_allowed(item):
                kept.append(_sanitize_scoped_row(item, scope))
        sanitized[key] = kept
    return sanitized


def _sanitize_result_row(
    result: dict[str, Any], key: str, scope: _KnowledgeScope,
) -> dict[str, Any]:
    sanitized = dict(result)
    row = sanitized.get(key)
    if isinstance(row, dict):
        sanitized[key] = _sanitize_scoped_row(row, scope)
    return sanitized


def _project_rows(scope: _KnowledgeScope | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for reg in _registrations():
        if scope is not None and reg.project_id not in scope.project_ids:
            continue
        try:
            snap = reg.manager.snapshot(reg.project_id, reg.workspace_provider(), limit=1)
            rows.append({
                "project_id": reg.project_id,
                "revision": snap.get("revision", 0),
                "updated_at": snap.get("updated_at"),
                "node_count": (snap.get("counts") or {}).get("total", 0),
            })
        except Exception:
            log.exception("[%s] 项目行组装失败（跳过）", reg.project_id)
    return rows


def _merged_nodes(
    only_project: str | None, scope: _KnowledgeScope,
) -> dict[str, Any]:
    projects: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    for reg in _registrations():
        if only_project and reg.project_id != only_project:
            continue
        try:
            snap = reg.manager.snapshot(reg.project_id, reg.workspace_provider())
        except Exception:
            log.exception("[%s] 合并快照失败（跳过该项目）", reg.project_id)
            continue
        if reg.project_id in scope.project_ids:
            projects.append({
                "project_id": reg.project_id,
                "revision": snap.get("revision", 0),
                "updated_at": snap.get("updated_at"),
                "counts": snap.get("counts") or {},
            })
        for raw in snap.get("nodes") or []:
            if isinstance(raw, dict):
                row = {**raw, "project_id": reg.project_id}
                if _row_allowed(row, reg.project_id, scope):
                    nodes.append(_sanitize_scoped_row(row, scope))
    nodes.sort(key=lambda row: str(row.get("last_seen") or ""), reverse=True)
    return {"ok": True, "projects": projects, "nodes": nodes}


def _merged_assets(only_project: str | None, scope: _KnowledgeScope | None = None) -> dict[str, Any]:
    projects: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    for reg in _registrations():
        if only_project and reg.project_id != only_project:
            continue
        if reg.assets is None:
            continue
        try:
            snap = reg.assets.snapshot(reg.project_id, reg.workspace_provider())
        except Exception:
            log.exception("[%s] 资产合并快照失败（跳过该项目）", reg.project_id)
            continue
        if scope is None or reg.project_id in scope.project_ids:
            projects.append({
                "project_id": reg.project_id,
                "revision": snap.get("revision", 0),
                "updated_at": snap.get("updated_at"),
                "counts": snap.get("counts") or {},
                "profile_exists": bool(snap.get("profile_exists")),
            })
        for raw in snap.get("assets") or []:
            if isinstance(raw, dict):
                row = dict(raw)
                row["project_id"] = reg.project_id
                if _row_allowed(row, reg.project_id, scope):
                    assets.append(_sanitize_scoped_row(row, scope))
    assets.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    return {"ok": True, "projects": projects, "assets": assets}


def _merged_skillpacks(only_project: str | None, scope: _KnowledgeScope | None = None) -> dict[str, Any]:
    system_by_id: dict[str, dict[str, Any]] = {}
    experiences: list[dict[str, Any]] = []
    third_party_by_id: dict[str, dict[str, Any]] = {}
    for reg in _registrations():
        if only_project and reg.project_id != only_project:
            continue
        if reg.assets is None:
            continue
        try:
            packs = reg.assets.list_skillpacks(reg.project_id, reg.workspace_provider())
        except Exception:
            log.exception("[%s] 技能包列举失败（跳过该项目）", reg.project_id)
            continue
        for raw in packs.get("system_builtin") or []:
            if isinstance(raw, dict) and raw.get("pack_id"):
                row = dict(raw)
                row.setdefault("project_id", reg.project_id)
                if scope is None or _row_allowed(row, reg.project_id, scope):
                    system_by_id[str(row["pack_id"])] = _sanitize_scoped_row(row, scope) if scope else row
        for raw in packs.get("project_experience") or []:
            if isinstance(raw, dict) and raw.get("pack_id"):
                row = dict(raw)
                row.setdefault("project_id", reg.project_id)
                if _row_allowed(row, reg.project_id, scope):
                    experiences.append(_sanitize_scoped_row(row, scope))
        for raw in packs.get("third_party") or []:
            if isinstance(raw, dict) and raw.get("pack_id"):
                row = dict(raw)
                row.setdefault("project_id", reg.project_id)
                if _row_allowed(row, reg.project_id, scope):
                    third_party_by_id[str(row["pack_id"])] = _sanitize_scoped_row(row, scope)
    system = sorted(system_by_id.values(), key=lambda row: str(row.get("name") or "").casefold())
    experiences.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
    third_party = sorted(
        third_party_by_id.values(), key=lambda row: str(row.get("name") or "").casefold(),
    )
    return {
        "ok": True,
        "system_builtin": system,
        "project_experience": experiences,
        "third_party": third_party,
        "system": system + experiences,
    }


def _find_skillpack(
    pack_id: str, scope: _KnowledgeScope | None = None,
) -> tuple[_ProjectReg, dict[str, Any]] | None:
    wanted = str(pack_id or "").strip()
    if not wanted:
        return None
    for reg in _registrations():
        if reg.assets is None:
            continue
        try:
            result = reg.assets.read_skillpack(
                reg.project_id, reg.workspace_provider(), wanted, active_only=False,
            )
        except Exception:
            log.exception("[%s] 技能定位失败（继续找）：%s", reg.project_id, wanted)
            continue
        pack = result.get("pack") if isinstance(result.get("pack"), dict) else result
        if (result.get("found") and _row_allowed(pack, reg.project_id, scope)
                and _contributors_allowed(result, scope)):
            return reg, result
    return None


def _dispatch_get_sync(
    parts: list[str], query: dict[str, list[str]], groups: GroupRepository | None,
) -> KnowledgeHttpResponse:
    rest = parts[2:]
    if rest == ["health"]:
        return _ok({"ok": True, "projects": project_count()})
    scope = _resolve_scope(query, groups)
    if rest == ["projects"]:
        return _ok({"ok": True, "projects": _project_rows(scope)})
    if rest == ["nodes"]:
        return _ok(_merged_nodes((query.get("project") or [None])[0], scope))
    if rest == ["assets"]:
        return _ok(_merged_assets((query.get("project") or [None])[0], scope))
    if rest == ["skillpacks"]:
        return _ok(_merged_skillpacks((query.get("project") or [None])[0], scope))
    if len(rest) == 2 and rest[0] == "skillpacks":
        found = _find_skillpack(rest[1], scope)
        return (
            _fail(404, "skillpack_not_found", pack_id=rest[1])
            if found is None else _ok({"ok": True, **found[1]})
        )
    if len(rest) == 2 and rest[1] == "assets":
        reg = _registration(rest[0])
        if reg is not None and reg.project_id not in scope.project_ids:
            return _fail(403, "project_outside_group_scope", project_id=rest[0])
        if scope is not None and reg is not None and reg.project_id not in scope.project_ids:
            return _fail(403, "project_outside_group_scope", project_id=rest[0])
        if reg is None:
            return _fail(404, "unknown_project", project_id=rest[0])
        if reg.assets is None:
            return _fail(503, "assets_unavailable", project_id=rest[0])
        snap = reg.assets.snapshot(reg.project_id, reg.workspace_provider())
        assets = [_sanitize_scoped_row(dict(row), scope) for row in snap.get("assets") or []
                  if isinstance(row, dict) and _row_allowed(row, reg.project_id, scope)]
        return _ok({"ok": True, **snap, "assets": assets})
    if len(rest) == 3 and rest[1] == "assets":
        reg = _registration(rest[0])
        if reg is None:
            return _fail(404, "unknown_project", project_id=rest[0])
        if reg.assets is None:
            return _fail(503, "assets_unavailable", project_id=rest[0])
        result = reg.assets.read_asset(reg.project_id, reg.workspace_provider(), rest[2])
        row = result.get("asset") if isinstance(result.get("asset"), dict) else result
        if result.get("found") and not _row_allowed(row, reg.project_id, scope):
            return _fail(403, "asset_outside_group_scope", asset_id=rest[2])
        return _ok({"ok": True, **_sanitize_result_row(result, "asset", scope)})
    if len(rest) == 2 and rest[1] == "profile":
        reg = _registration(rest[0])
        if scope is not None and reg is not None and reg.project_id not in scope.project_ids:
            return _fail(403, "profile_outside_group_scope", project_id=rest[0])
        if reg is None:
            return _fail(404, "unknown_project", project_id=rest[0])
        if reg.assets is None:
            return _fail(503, "assets_unavailable", project_id=rest[0])
        if scope.source_ids or scope.course_id:
            return _fail(403, "profile_outside_narrow_group_scope", project_id=rest[0])
        text = reg.assets.profile_text(reg.workspace_provider())
        return _ok({"ok": True, "project_id": reg.project_id, "text": text})
    if len(rest) == 2 and rest[1] == "nodes":
        reg = _registration(rest[0])
        if reg is not None and reg.project_id not in scope.project_ids:
            return _fail(403, "project_outside_group_scope", project_id=rest[0])
        if reg is None:
            return _fail(404, "unknown_project", project_id=rest[0])
        snap = reg.manager.snapshot(reg.project_id, reg.workspace_provider())
        nodes = [_sanitize_scoped_row(dict(row), scope) for row in snap.get("nodes") or []
                 if isinstance(row, dict) and _row_allowed(row, reg.project_id, scope)]
        return _ok({"ok": True, **snap, "nodes": nodes})
    if len(rest) == 2 and rest[1] == "search":
        reg = _registration(rest[0])
        if reg is not None and reg.project_id not in scope.project_ids:
            return _fail(403, "project_outside_group_scope", project_id=rest[0])
        if reg is None:
            return _fail(404, "unknown_project", project_id=rest[0])
        q = (query.get("q") or [""])[0]
        limit = _int_of((query.get("limit") or ["6"])[0], 6)
        result = reg.manager.search(reg.project_id, reg.workspace_provider(), q, limit=limit)
        for key in ("results", "hits", "nodes"):
            if isinstance(result.get(key), list):
                result[key] = [_sanitize_scoped_row(row, scope) for row in result[key]
                               if isinstance(row, dict) and _row_allowed(row, reg.project_id, scope)]
        return _ok({"ok": True, **result})
    if len(rest) == 2 and rest[1] == "node":
        reg = _registration(rest[0])
        if reg is None:
            return _fail(404, "unknown_project", project_id=rest[0])
        ref = (query.get("ref") or [""])[0]
        if not ref:
            return _fail(400, "missing_ref")
        result = reg.manager.read_node(reg.project_id, reg.workspace_provider(), ref)
        row = result.get("node") if isinstance(result.get("node"), dict) else result
        if result.get("found") and not _row_allowed(row, reg.project_id, scope):
            return _fail(403, "node_outside_group_scope", ref=ref)
        return _ok({"ok": True, **_sanitize_result_row(result, "node", scope)})
    return _fail(404, "not_found")


async def _run_override(
    reg: _ProjectReg,
    operation: Coroutine[Any, Any, dict[str, Any]],
    *,
    not_found_reason: str,
) -> KnowledgeHttpResponse:
    current = asyncio.get_running_loop()
    if reg.loop is not None and (reg.loop.is_closed() or reg.loop is not current):
        operation.close()
        return _fail(503, "engine_loop_unavailable", project_id=reg.project_id)
    try:
        result = await asyncio.wait_for(operation, timeout=_OVERRIDE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return _fail(504, "override_timeout")
    code = 200 if result.get("ok") else (404 if result.get("reason") == not_found_reason else 400)
    return _ok(result, code)


async def _dispatch_post(
    parts: list[str], body: dict[str, Any], query: dict[str, list[str]],
    groups: GroupRepository | None,
) -> KnowledgeHttpResponse:
    scope = _resolve_scope(query, groups)
    if len(parts) < 4:
        return _fail(404, "not_found")
    if parts[2] == "skillpacks":
        pack_id = parts[3]
        found = await asyncio.to_thread(_find_skillpack, pack_id, scope)
        if found is None:
            return _fail(404, "skillpack_not_found", pack_id=pack_id)
        reg, _detail = found
        if scope is not None and reg.project_id not in scope.project_ids:
            return _fail(403, "skillpack_outside_group_scope", pack_id=pack_id)
        if reg.assets is None:
            return _fail(503, "assets_unavailable", project_id=reg.project_id)
        if len(parts) == 4:
            status = str(body.get("status") or "")
            if not status:
                return _fail(400, "empty_patch")
            return await _run_override(
                reg,
                reg.assets.set_skillpack_status(
                    reg.project_id, reg.workspace_provider(), pack_id, status,
                ),
                not_found_reason="skillpack_not_found",
            )
        if len(parts) == 5 and parts[4] == "review":
            return await _run_override(
                reg,
                reg.assets.review_skillpack(
                    reg.project_id, reg.workspace_provider(), pack_id,
                    str(body.get("action") or ""),
                ),
                not_found_reason="skillpack_not_found",
            )
        if len(parts) == 5 and parts[4] == "purge":
            return await _run_override(
                reg,
                reg.assets.purge_skillpack(reg.project_id, reg.workspace_provider(), pack_id),
                not_found_reason="skillpack_not_found",
            )
        return _fail(404, "not_found")

    project_id = parts[2]
    reg = _registration(project_id)
    if scope is not None and project_id not in scope.project_ids:
        return _fail(403, "project_outside_group_scope", project_id=project_id)
    if reg is None:
        return _fail(404, "unknown_project", project_id=project_id)
    if len(parts) == 5 and parts[3] == "nodes":
        existing = reg.manager.read_node(reg.project_id, reg.workspace_provider(), parts[4])
        existing_row = existing.get("node") if isinstance(existing.get("node"), dict) else existing
        if existing.get("found") and not _row_allowed(existing_row, reg.project_id, scope):
            return _fail(403, "node_outside_group_scope", ref=parts[4])
        patch = {
            key: body.get(key)
            for key in ("status", "scope", "title", "summary")
            if body.get(key) is not None
        }
        if not patch:
            return _fail(400, "empty_patch")
        return await _run_override(
            reg,
            reg.manager.apply_user_override(
                reg.project_id, reg.workspace_provider(), parts[4], **patch,
            ),
            not_found_reason="node_not_found",
        )
    if len(parts) >= 5 and parts[3] == "assets":
        if reg.assets is None:
            return _fail(503, "assets_unavailable", project_id=project_id)
        asset_id = parts[4]
        existing = reg.assets.read_asset(reg.project_id, reg.workspace_provider(), asset_id)
        existing_row = existing.get("asset") if isinstance(existing.get("asset"), dict) else existing
        if existing.get("found") and not _row_allowed(existing_row, reg.project_id, scope):
            return _fail(403, "asset_outside_group_scope", asset_id=asset_id)
        if len(parts) == 6 and parts[5] == "review":
            return await _run_override(
                reg,
                reg.assets.review(
                    reg.project_id, reg.workspace_provider(), asset_id,
                    str(body.get("action") or ""),
                ),
                not_found_reason="asset_not_found",
            )
        if len(parts) == 6 and parts[5] == "purge":
            return await _run_override(
                reg,
                reg.assets.purge(reg.project_id, reg.workspace_provider(), asset_id),
                not_found_reason="asset_not_found",
            )
        if len(parts) == 5:
            patch = {
                key: body.get(key)
                for key in ("title", "scope", "category", "status")
                if body.get(key) is not None
            }
            if not patch:
                return _fail(400, "empty_patch")
            return await _run_override(
                reg,
                reg.assets.apply_user_override(
                    reg.project_id, reg.workspace_provider(), asset_id, **patch,
                ),
                not_found_reason="asset_not_found",
            )
    if len(parts) == 4 and parts[3] == "profile":
        if reg.assets is None:
            return _fail(503, "assets_unavailable", project_id=project_id)
        if scope.source_ids or scope.course_id:
            return _fail(403, "profile_outside_narrow_group_scope", project_id=project_id)
        return await _run_override(
            reg,
            reg.assets.set_profile(
                reg.project_id, reg.workspace_provider(), str(body.get("text") or ""),
            ),
            not_found_reason="profile_not_found",
        )
    return _fail(404, "not_found")


async def dispatch_knowledge_http(
    method: str,
    path: str,
    query: str,
    body: bytes,
    groups: GroupRepository | None = None,
) -> KnowledgeHttpResponse:
    """Dispatch one already-authenticated request without opening another server."""
    try:
        parts = [unquote(part) for part in path.strip("/").split("/") if part]
        if parts[:2] != ["api", "knowledge"]:
            return _fail(404, "not_found")
        verb = method.upper()
        if verb == "GET":
            return await asyncio.to_thread(_dispatch_get_sync, parts, parse_qs(query), groups)
        if verb == "POST":
            if len(body) > _MAX_BODY_BYTES:
                return _fail(413, "body_too_large")
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                return _fail(400, "bad_json")
            if not isinstance(payload, dict):
                return _fail(400, "bad_json")
            return await _dispatch_post(parts, payload, parse_qs(query), groups)
        return _fail(405, "method_not_allowed")
    except _KnowledgeScopeDenied as exc:
        return _fail(403, str(exc))
    except Exception as exc:
        log.exception("知识库数据面处理异常：%s %s", method, path)
        return _fail(500, type(exc).__name__)


__all__ = [
    "KnowledgeHttpResponse",
    "dispatch_knowledge_http",
    "project_count",
    "register_project",
    "unregister_project",
]
