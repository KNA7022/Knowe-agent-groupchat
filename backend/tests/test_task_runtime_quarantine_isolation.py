from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend.groups import GroupNotFoundError, GroupRepository
from backend.server import KnoweServer
from backend.task_runtime import TaskRuntimeService
from backend.task_runtime.contracts import TaskStatus
from backend.task_runtime.store import RuntimeStoreError, TaskRuntimeStore


def _close_server(server: KnoweServer) -> None:
    if server.task_runtime is not None:
        server.task_runtime.close()
    if server.store is not None:
        server.store.close(timeout=5)


def _prepare_runtime_delete(
    service: TaskRuntimeService, groups: GroupRepository,
    group_id: str, runtime_project_id: str,
) -> tuple[str, str]:
    run = service.submit(
        group_id, title="Quarantine isolation", request_key=f"run-{runtime_project_id}",
    )
    preview = groups.delete_preview(group_id)
    transaction = groups.prepare_delete(
        group_id, preview.confirmation_token,
        runtime_project_id=runtime_project_id,
    )
    service.prepare_group_deletion(group_id, str(transaction["transaction_id"]))
    return run.run_id, str(transaction["transaction_id"])


def _tamper_transaction(groups: GroupRepository, group_id: str) -> None:
    registry = json.loads(groups.registry_path.read_text(encoding="utf-8"))
    transaction = next(
        item for item in registry["deletion_transactions"]
        if item["group_id"] == group_id
    )
    transaction["source_reference_counts"] = {"src_tampered_quarantine": 1}
    groups.registry_path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _wait_for(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not satisfied before timeout")


@pytest.mark.asyncio
async def test_full_server_excludes_quarantined_pending_group_and_recovers_other(
    tmp_path: Path,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    bad = server.groups.create_group("Tampered pending")
    healthy = server.groups.create_group("Healthy pending")
    bad_run, _bad_tx = _prepare_runtime_delete(
        server.task_runtime, server.groups, bad.group_id, "project_bad_pending",
    )
    healthy_run, _healthy_tx = _prepare_runtime_delete(
        server.task_runtime, server.groups, healthy.group_id, "project_healthy_pending",
    )
    _close_server(server)
    _tamper_transaction(server.groups, bad.group_id)

    restarted = KnoweServer(str(tmp_path))
    assert restarted.task_runtime is not None and restarted.groups is not None
    try:
        report = next(
            item for item in restarted.task_runtime.recovery_report["deletions"]
            if item.get("group_id") == bad.group_id
        )
        assert report["status"] == "quarantined"
        await restarted._reconcile_task_runtime_group_deletions()  # noqa: SLF001

        assert restarted.task_runtime.store.get_run(bad_run) is not None
        assert restarted.groups.get(bad.group_id).group_id == bad.group_id
        bad_transaction = restarted.groups.delete_transaction(bad.group_id)
        assert bad_transaction is not None and bad_transaction["stage"] == "prepared"
        assert restarted.task_runtime.store.get_run(healthy_run) is None
        with pytest.raises(GroupNotFoundError):
            restarted.groups.get(healthy.group_id)
    finally:
        _close_server(restarted)


@pytest.mark.asyncio
async def test_server_candidate_failure_isolated_from_later_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    groups = [server.groups.create_group(f"Candidate {index}") for index in range(2)]
    for index, group in enumerate(groups):
        _prepare_runtime_delete(
            server.task_runtime, server.groups, group.group_id,
            f"project_candidate_{index}",
        )
    _close_server(server)

    restarted = KnoweServer(str(tmp_path))
    assert restarted.groups is not None and restarted.task_runtime is not None
    failing_id, healthy_id = sorted(group.group_id for group in groups)
    original = restarted._run_confirmed_group_delete  # noqa: SLF001

    async def fail_one(group_id: str, *args, **kwargs):
        if group_id == failing_id:
            raise RuntimeStoreError("isolated candidate failure")
        return await original(group_id, *args, **kwargs)

    monkeypatch.setattr(restarted, "_run_confirmed_group_delete", fail_one)
    try:
        await restarted._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        diagnostics = restarted.task_runtime.recovery_report[
            "server_deletion_reconciliation"
        ]
        assert diagnostics == [{
            "group_id": failing_id,
            "status": "deferred",
            "reason": "RuntimeStoreError",
        }]
        assert restarted.groups.get(failing_id).group_id == failing_id
        with pytest.raises(GroupNotFoundError):
            restarted.groups.get(healthy_id)
    finally:
        _close_server(restarted)


def _seed_tampered_and_healthy(tmp_path: Path) -> tuple[GroupRepository, str, str, str]:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    bad = groups.create_group("Quarantine write")
    healthy = groups.create_group("Unrelated runnable")
    bad_run, _transaction_id = _prepare_runtime_delete(
        service, groups, bad.group_id, "project_quarantine_write",
    )
    healthy_run = service.submit(
        healthy.group_id, title="Healthy task", request_key="healthy-quarantine-task",
    ).run_id
    service.close()
    _tamper_transaction(groups, bad.group_id)
    return groups, bad.group_id, bad_run, healthy_run


def test_one_shot_quarantine_write_failure_retries_without_blocking_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    groups, bad_group_id, bad_run, healthy_run = _seed_tampered_and_healthy(tmp_path)
    original = TaskRuntimeStore.quarantine_group_deletion
    calls = {"count": 0}

    def fail_once(self: TaskRuntimeStore, group_id: str, audit_code: str) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeStoreError("one-shot quarantine audit failure")
        original(self, group_id, audit_code)

    monkeypatch.setattr(TaskRuntimeStore, "quarantine_group_deletion", fail_once)
    recovered = TaskRuntimeService(tmp_path, groups)
    try:
        initial = next(
            item for item in recovered.recovery_report["deletions"]
            if item.get("group_id") == bad_group_id
        )
        assert initial["status"] == "deferred_quarantine"
        assert recovered.store.get_run(bad_run) is not None
        assert recovered.store.get_run(healthy_run).status is TaskStatus.RUNNING  # type: ignore[union-attr]
        assert len(recovered._deferred_quarantines) == 1  # noqa: SLF001

        _wait_for(lambda: (
            next(
                item for item in recovered.store.group_deletion_intents()
                if item["group_id"] == bad_group_id
            )["stage"] == "quarantined"
        ))
        assert not recovered._deferred_quarantines  # noqa: SLF001
        assert recovered.store.get_run(bad_run) is not None
    finally:
        recovered.close()


def test_permanent_quarantine_write_failure_is_bounded_and_other_group_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    groups, bad_group_id, bad_run, healthy_run = _seed_tampered_and_healthy(tmp_path)

    def always_fail(
        _self: TaskRuntimeStore, _group_id: str, _audit_code: str,
    ) -> None:
        raise RuntimeStoreError("permanent quarantine audit failure")

    monkeypatch.setattr(TaskRuntimeStore, "quarantine_group_deletion", always_fail)
    recovered = TaskRuntimeService(tmp_path, groups)
    try:
        assert recovered.store.get_run(bad_run) is not None
        assert recovered.store.get_run(healthy_run).status is TaskStatus.RUNNING  # type: ignore[union-attr]
        assert recovered._deferred_quarantines == {  # noqa: SLF001
            bad_group_id: "RuntimeConflictError",
        }
        time.sleep(0.25)
        assert len(recovered._deferred_quarantines) == 1  # noqa: SLF001
        assert recovered._quarantine_retry_timer is not None  # noqa: SLF001
    finally:
        recovered.close()
    assert recovered._quarantine_retry_timer is None  # noqa: SLF001


def test_many_failed_quarantines_coalesce_at_fixed_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    bad_group_ids = [f"grp_{index:032x}" for index in range(7)]
    for index, group_id in enumerate(bad_group_ids):
        service.store.prepare_group_deletion(
            group_id, f"gdt_{index:032x}",
            group_proof_digest=f"{index + 1:064x}",
            confirmation_token=f"confirmation-{index}",
            runtime_project_id=f"project-{index}",
        )
    service.close()

    monkeypatch.setattr(TaskRuntimeService, "DEFERRED_QUARANTINE_LIMIT", 3)

    def always_fail(
        _self: TaskRuntimeStore, _group_id: str, _audit_code: str,
    ) -> None:
        raise RuntimeStoreError("permanent quarantine audit failure")

    monkeypatch.setattr(TaskRuntimeStore, "quarantine_group_deletion", always_fail)
    recovered = TaskRuntimeService(tmp_path, groups)
    try:
        reports = recovered.recovery_report["deletions"]
        assert len(reports) == len(bad_group_ids)
        assert all(item["status"] == "deferred_quarantine" for item in reports)
        assert len(recovered._deferred_quarantines) == 3  # noqa: SLF001
        assert recovered._deferred_quarantine_overflow  # noqa: SLF001
        _excluded, globally_blocked = recovered.deletion_recovery_exclusions()
        assert globally_blocked
        time.sleep(0.25)
        assert len(recovered._deferred_quarantines) <= 3  # noqa: SLF001
    finally:
        recovered.close()
