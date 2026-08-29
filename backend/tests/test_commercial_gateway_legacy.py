from __future__ import annotations

from pathlib import Path

import pytest

from backend.commercial_gateway import (
    GatewayDenied,
    LegacyToolAuthority,
    LegacyToolBoundary,
)
from backend.groups import GroupRepository
from backend.knowe_core.tool_registry import ToolRegistry
from backend.task_runtime.service import TaskRuntimeService


def _runtime(tmp_path: Path):
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Legacy gateway")
    queued = service.submit(group.group_id, title="Legacy", request_key="legacy")
    running = service.schedule()[0]
    assert queued.run_id == running.run_id
    return service, LegacyToolAuthority(
        group_id=group.group_id,
        task_id=running.task_id,
        run_id=running.run_id,
    )


@pytest.mark.asyncio
async def test_legacy_read_builtin_uses_gateway_and_ignores_forged_context(
    tmp_path: Path,
) -> None:
    service, authority = _runtime(tmp_path)
    calls: list[dict[str, object]] = []
    registry = ToolRegistry()
    registry.register(
        "safe_read_file", "read", {"type": "object"},
        lambda args, **_context: calls.append(dict(args)) or "ok",
    )
    registry.set_execution_boundary(LegacyToolBoundary(
        service,
        authority_provider=lambda: authority,
        read_only_tools=frozenset({"safe_read_file"}),
    ))

    result = await registry.execute(
        "safe_read_file", {"path": "notes.md"},
        tool_call_id="call-read-1",
        group_id="grp_" + "0" * 32,
        task_id="task_" + "0" * 32,
    )
    assert result == "ok"
    assert calls == [{"path": "notes.md"}]
    invocation = service.store.tool_by_key(authority.run_id, "legacy:call-read-1")
    assert invocation is not None
    assert invocation.group_id == authority.group_id
    assert invocation.task_id == authority.task_id
    assert invocation.status == "completed"


@pytest.mark.asyncio
async def test_legacy_tool_without_authoritative_context_fails_before_handler(
    tmp_path: Path,
) -> None:
    service, _authority = _runtime(tmp_path)
    calls = 0

    def handler(_args, **_context):
        nonlocal calls
        calls += 1
        return "unsafe"

    registry = ToolRegistry()
    registry.register("safe_read_file", "read", {"type": "object"}, handler)
    registry.set_execution_boundary(LegacyToolBoundary(
        service,
        authority_provider=lambda: None,
        read_only_tools=frozenset({"safe_read_file"}),
    ))
    with pytest.raises(GatewayDenied, match="authoritative.*context"):
        await registry.execute(
            "safe_read_file", {"path": "notes.md"}, tool_call_id="call-missing",
        )
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["safe_write_file", "safe_bash", "web_search"])
async def test_unmigrated_write_shell_and_network_tools_fail_closed(
    tmp_path: Path, tool_name: str,
) -> None:
    service, authority = _runtime(tmp_path)
    calls = 0

    def handler(_args, **_context):
        nonlocal calls
        calls += 1
        return "effect"

    registry = ToolRegistry()
    registry.register(tool_name, "effect", {"type": "object"}, handler)
    registry.set_execution_boundary(LegacyToolBoundary(
        service,
        authority_provider=lambda: authority,
        read_only_tools=frozenset({"safe_read_file"}),
    ))
    with pytest.raises(GatewayDenied, match="structured.*migration|blocked"):
        await registry.execute(
            tool_name, {"command": "echo bypass"}, tool_call_id=f"call-{tool_name}",
        )
    assert calls == 0


def test_natural_language_paths_never_self_authorize_external_roots() -> None:
    from backend.engine import _auto_external_roots

    assert _auto_external_roots("复制 D:/private/student-records.csv 到项目") == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("read_harness_memory", {}),
        ("web_search", {"query": "student records"}),
        ("create_project", {"project_name": "bypass"}),
    ],
)
async def test_platform_legacy_tools_without_task_runtime_authority_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, arguments: dict[str, object],
) -> None:
    from backend.agents import zinnia as zinnia_module

    effects = 0

    def read_harness() -> str:
        nonlocal effects
        effects += 1
        return "private harness"

    async def search(*_args, **_kwargs):
        nonlocal effects
        effects += 1
        return [{"title": "unexpected"}]

    class Gate:
        async def propose(self, **_kwargs):
            nonlocal effects
            effects += 1
            return "approved"

    monkeypatch.setattr(zinnia_module.web_tools, "search", search)
    agent = zinnia_module.ZinniaAgent(read_harness=read_harness)
    call = {
        "function": {
            "name": tool_name,
            "arguments": __import__("json").dumps(arguments),
        },
    }
    result = await agent._execute(call, lambda _payload: None, Gate())

    assert "Task Runtime" in result or "blocked" in result.casefold()
    assert effects == 0
