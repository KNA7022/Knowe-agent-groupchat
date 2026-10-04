# -*- coding: utf-8 -*-
"""外部路径权限：提示不授予权限，明确的结构化用户授权才能执行。"""
import json
import sys
from pathlib import Path

import pytest

from backend.engine import _auto_external_roots


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", [False, True])
async def test_explicit_user_grant_authorizes_only_the_structured_external_read(tmp_path, revoke):
    """Positive filesystem permission reaches the real gateway and inert executor."""
    from backend.commercial_gateway import (
        GatewayDenied, ShellSandboxAttestation, ToolGateway, ToolExecutionRequest,
    )
    from backend.task_runtime.contracts import ToolDescriptor, RiskLevel
    from tests.test_commercial_security_gateway import _running_service, _approved_shell_grant

    service, group_id, run = _running_service(tmp_path / "authority")
    root = tmp_path / "external"
    root.mkdir()
    source = root / "用户确认.txt"
    source.write_text("approved fixture content", encoding="utf-8")
    effects = []

    def inert_read(request):
        effects.append(request.read_paths)
        return Path(request.read_paths[0]).read_text(encoding="utf-8")

    gateway = ToolGateway(service)
    gateway.register(ToolDescriptor(
        tool_name="shell", kind="shell", risk=RiskLevel.HIGH,
        executable_allowlist=(sys.executable,), cwd_roots=(str(root),),
        read_roots=(str(root),), write_roots=(),
    ), inert_read, attestation=ShellSandboxAttestation("inert-fixture", inert_read))
    request = ToolExecutionRequest(
        group_id=group_id, task_id=run.task_id, run_id=run.run_id,
        tool_name="shell", idempotency_key="external-read", purpose="run_shell",
        executable=sys.executable, cwd=str(root), read_paths=(str(source),),
    )
    try:
        with pytest.raises(GatewayDenied):
            await gateway.invoke(request)
        assert effects == []
        scope = request.operation_scope(gateway.descriptor("shell"))
        approval_id, grant_id = _approved_shell_grant(service, group_id, run, scope)
        approved = request.replace(approval_id=approval_id, grant_id=grant_id,
                                   idempotency_key="external-read-approved")
        for attack in (
            approved.replace(task_id="task_" + "0" * 32, idempotency_key="wrong-task"),
            approved.replace(read_paths=(str(tmp_path / "outside.txt"),), idempotency_key="outside"),
        ):
            with pytest.raises(GatewayDenied):
                await gateway.invoke(attack)
        assert effects == []
        if revoke:
            assert service.store.revoke_group_grants(group_id) == 1
            with pytest.raises(GatewayDenied):
                await gateway.invoke(approved)
            assert effects == []
            return
        assert await gateway.invoke(approved) == "approved fixture content"
        assert effects == [(str(source),)]
        assert service.store.tool_by_key(run.run_id, approved.idempotency_key).status == "completed"
    finally:
        service.close()


class TestAutoExternalRoots:
    """自然语言中的任何路径都不能授予外部文件权限。"""

    def test_extracts_file_parent_from_goal(self) -> None:
        goal = "用 copy_external_file 将 D:/Projects/knowe/测试/测试5/qwen3.8-max_analysis_report.md 复制到项目根目录"
        roots = _auto_external_roots(goal)
        assert roots == ()

    def test_accepts_backslash_paths(self) -> None:
        goal = "复制 D:\\data\\input\\report.md 到根目录"
        roots = _auto_external_roots(goal)
        assert roots == ()

    def test_directory_path_kept_as_is(self) -> None:
        goal = "读取 D:/shared/docs 下所有文件"
        roots = _auto_external_roots(goal)
        assert roots == ()

    def test_drive_root_not_authorized(self) -> None:
        goal = "访问 D:/ 根目录"
        assert _auto_external_roots(goal) == ()

    def test_empty_goal(self) -> None:
        assert _auto_external_roots("") == ()
        assert _auto_external_roots(None) == ()
