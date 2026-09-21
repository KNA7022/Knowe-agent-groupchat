# [v1.0.13][R1] Model fingerprint, explicit apply and readiness barrier tests.
from __future__ import annotations

import asyncio
import unittest

from backend import runtime_settings
from backend.commercial_gateway import derive_provider_credential_purpose


def binding(**changes: str) -> dict[str, str]:
    value = {
        "provider": "openai-api",
        "model": "gpt-test",
        "secret_ref": "secret_" + "a" * 32,
        "credential_slot": "main",
        "credential_kind": "provider_credential",
        "credential_audience": "provider_gateway",
        "base_url": "https://example.invalid/v1",
        "transport": "openai_chat",
    }
    value.update(changes)
    if "credential_purpose" not in changes:
        value["credential_purpose"] = derive_provider_credential_purpose(
            slot=value["credential_slot"],
            provider_id=value["provider"],
            endpoint=value["base_url"],
            transport=value["transport"],
        )
    return value


class RuntimeSettingsReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        runtime_settings._state.clear()  # noqa: SLF001 - isolated module-level authority
        runtime_settings._state.update(runtime_settings._default_state())  # noqa: SLF001
        runtime_settings._loaded = True  # noqa: SLF001
        runtime_settings._model_waiters.clear()  # noqa: SLF001

    def test_fingerprint_covers_every_binding_field_and_never_exposes_key(self) -> None:
        base = binding()
        fingerprint = runtime_settings.binding_fingerprint(base)
        self.assertIsNotNone(fingerprint)
        self.assertTrue(str(fingerprint).startswith("sha256:"))
        self.assertNotIn("sk-secret-value", str(fingerprint))
        for field, value in {
            "provider": "deepseek",
            "model": "another-model",
            "secret_ref": "secret_" + "b" * 32,
            "credential_purpose": "provider:alternate",
            "base_url": "https://other.invalid/api",
            "transport": "codex_responses",
        }.items():
            changed = dict(base)
            changed[field] = value
            self.assertNotEqual(fingerprint, runtime_settings.binding_fingerprint(changed), field)

    def test_api_snapshot_never_exposes_fingerprint_key_material(self) -> None:
        main = binding()
        runtime_settings.apply({
            "main_model": main,
            "expected_fingerprint": runtime_settings.binding_fingerprint(main),
        })
        value = runtime_settings.api_snapshot(welcome_state="shown")
        self.assertNotIn("fingerprint_salt", value)
        self.assertNotIn("active_model_fingerprint", value)
        self.assertTrue(str(value["applied_fingerprint"]).startswith("sha256:"))

    def test_ordinary_save_activates_immediately(self) -> None:
        # v1.0.19.5: 指纹闸门废除——保存完整绑定即激活，不再要求测试→应用仪式。
        main = binding()
        first = runtime_settings.apply({"main_model": main, "agent_models": {}})
        self.assertTrue(runtime_settings.model_ready_for("project-1", "coordinator"))
        self.assertEqual(
            runtime_settings.model_apply_ack()["applied_fingerprint"],
            runtime_settings.binding_fingerprint(main),
        )

        changed = binding(model="gpt-test-2")
        runtime_settings.apply({
            "main_model": changed,
            "agent_models": {},
            "expected_revision": first["settings_revision"],
        })
        self.assertTrue(runtime_settings.model_ready_for("project-1", "coordinator"))
        self.assertEqual(
            runtime_settings.model_apply_ack()["applied_fingerprint"],
            runtime_settings.binding_fingerprint(changed),
        )

    def test_legacy_fingerprint_field_is_ignored_and_save_still_applies(self) -> None:
        # v1.0.19.5: 旧版 expected_fingerprint 字段不再拦截保存；配置照常生效。
        main = binding()
        before = runtime_settings.apply({"main_model": main})
        applied = runtime_settings.apply({
            "main_model": binding(model="not-tested"),
            "expected_revision": before["settings_revision"],
            "expected_fingerprint": runtime_settings.binding_fingerprint(main),
        })
        self.assertTrue(runtime_settings.model_ready_for("project-1", "coordinator"))
        self.assertEqual(applied["main_model"]["model"], "not-tested")

    def test_zinnia_compatibility_is_separate_from_generic_model_readiness(self) -> None:
        anthropic = binding(transport="anthropic_messages")
        fingerprint = runtime_settings.binding_fingerprint(anthropic)
        runtime_settings.apply({
            "main_model": anthropic,
            "expected_fingerprint": fingerprint,
        })
        self.assertTrue(runtime_settings.model_ready_for("project-1", "coordinator"))
        self.assertFalse(runtime_settings.zinnia_binding_status()[0])
        self.assertFalse(runtime_settings.model_apply_ack()["zinnia_compatible"])


class RuntimeSettingsWaiterTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        runtime_settings._state.clear()  # noqa: SLF001
        runtime_settings._state.update(runtime_settings._default_state())  # noqa: SLF001
        runtime_settings._loaded = True  # noqa: SLF001
        runtime_settings._model_waiters.clear()  # noqa: SLF001

    async def test_waiter_returns_once_binding_is_complete(self) -> None:
        # v1.0.19.5: 无需指纹事务——完整绑定保存后 wait 立即放行。
        waiter = asyncio.create_task(
            runtime_settings.wait_for_model_ready("project-1", "coordinator", timeout=1.0)
        )
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        runtime_settings.apply({"main_model": binding()})
        self.assertTrue(await waiter)


class SettingsProbeAuthorityTest(unittest.TestCase):
    """I-R8：Settings 连接测试必须在一个 backend-authored 的短命 Task Runtime run 内跑。

    这条测试**不发明网络请求**（那会依赖真实 provider），只验证 authority 是否被正确
    建立：这是"连接测试必然失败"这个缺陷的直接修复点 —— Gateway 在没有 authority 时
    会在联网前就以 GatewayDenied 拒绝。
    """

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from backend.groups import GroupRepository
        from backend.task_runtime.service import TaskRuntimeService

        self._dir = tempfile.TemporaryDirectory()
        root = Path(self._dir.name)
        self.groups = GroupRepository(root)
        self.runtime = TaskRuntimeService(root, self.groups)

    def tearDown(self) -> None:
        self.runtime.close()
        self._dir.cleanup()

    def test_probe_authority_creates_a_running_run_that_the_gateway_accepts(self) -> None:
        from backend.task_runtime.contracts import TaskStatus

        authority, run_id = runtime_settings.probe_authority(self.groups, self.runtime)

        run = self.runtime.store.get_run(run_id)
        self.assertIsNotNone(run)
        # Gateway 的 authority 校验要求 run 处于 RUNNING。
        self.assertIs(run.status, TaskStatus.RUNNING)
        self.assertEqual(authority.group_id, run.group_id)
        self.assertEqual(authority.task_id, run.task_id)
        self.assertEqual(authority.run_id, run.run_id)
        self.assertIs(authority.runtime, self.runtime)

    def test_probe_run_is_short_lived_bounded_and_recoverable(self) -> None:
        """I5：探针 run 必须短命、**有界**，且旧 run 终结后能自动换新。

        复审实测过两轮问题：先是固定 request_key 复用且从不收尾，第 21 次点击撞上
        `model_turns=20` 被判 BUDGET_EXHAUSTED，此后永久失效并留下一个 RUNNING run；
        改成"每次新建 + 收尾"后，又变成每次点击留一条 CANCELLED 记录（无界增长）。
        现在是**有界保留**：只留最近 `_PROBE_RUN_RETENTION` 条。
        """

        from backend.task_runtime.contracts import TaskStatus

        keep = runtime_settings._PROBE_RUN_RETENTION
        clicks = keep + 15
        seen: list[str] = []
        for _ in range(clicks):
            _authority, run_id = runtime_settings.probe_authority(self.groups, self.runtime)
            seen.append(run_id)
            # `_probe_once` 在 finally 里做的事：探针结束即收尾（取消）。
            runtime_settings._finish_probe_run(self.groups, self.runtime, run_id)

        self.assertEqual(len(set(seen)), clicks, "每次探针都应当是独立的 run")
        group_id = self.groups.list_groups()[0].group_id
        surviving = self.runtime.store.list_runs(group_id=group_id)
        # 有界：数量不随点击次数线性增长。
        self.assertLessEqual(len(surviving), keep + 1, len(surviving))
        # 没有 RUNNING 残留（旧实现会留一个永不结束的）。
        for run in surviving:
            self.assertIsNot(run.status, TaskStatus.RUNNING)

        # 仍然可用 —— 这正是旧实现缺失的恢复路径。
        _authority, run_id = runtime_settings.probe_authority(self.groups, self.runtime)
        self.assertTrue(run_id)
        self.assertIs(self.runtime.store.get_run(run_id).status, TaskStatus.RUNNING)

        names = [group.name for group in self.groups.list_groups()]
        self.assertEqual(names.count("settings-probe"), 1)
        runtime_settings._finish_probe_run(self.groups, self.runtime, run_id)


if __name__ == "__main__":
    unittest.main()
