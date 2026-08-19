from __future__ import annotations

import asyncio
import json
import sqlite3
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
            item for item in restarted.task_runtime.recovery_report["deletions"]["sample"]
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
        assert diagnostics["sample"] == [{
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
            item for item in recovered.recovery_report["deletions"]["sample"]
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
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 3)

    def always_fail(
        _self: TaskRuntimeStore, _group_id: str, _audit_code: str,
    ) -> None:
        raise RuntimeStoreError("permanent quarantine audit failure")

    monkeypatch.setattr(TaskRuntimeStore, "quarantine_group_deletion", always_fail)
    recovered = TaskRuntimeService(tmp_path, groups)
    try:
        reports = recovered.recovery_report["deletions"]
        assert reports["processed_count"] == 3
        assert len(reports["sample"]) == 3
        assert reports["has_more"]
        assert all(
            item["status"] == "deferred_quarantine"
            for item in reports["sample"]
        )
        assert len(recovered._deferred_quarantines) == 3  # noqa: SLF001
        time.sleep(0.25)
        assert len(recovered._deferred_quarantines) <= 3  # noqa: SLF001
        assert recovered._deferred_quarantine_overflow  # noqa: SLF001
    finally:
        recovered.close()


@pytest.mark.asyncio
async def test_limit_two_overflow_never_starves_valid_confirmed_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    healthy = server.groups.create_group("Healthy among overflow")
    unrelated = server.groups.create_group("Unrelated runnable")
    healthy_run, _transaction_id = _prepare_runtime_delete(
        server.task_runtime, server.groups, healthy.group_id,
        "project_healthy_overflow",
    )
    unrelated_run = server.task_runtime.submit(
        unrelated.group_id, title="Unaffected", request_key="unrelated-overflow",
    ).run_id
    bad_ids = [
        "grp_00000000000000000000000000000000",
        "grp_10000000000000000000000000000000",
        "grp_e0000000000000000000000000000000",
        "grp_f0000000000000000000000000000000",
    ]
    for index, group_id in enumerate(bad_ids):
        server.task_runtime.store.prepare_group_deletion(
            group_id, f"gdt_bad_{index:028x}",
            group_proof_digest=f"{index + 1:064x}",
            confirmation_token=f"bad-confirmation-{index}",
            runtime_project_id=f"bad-project-{index}",
        )
    _close_server(server)

    monkeypatch.setattr(TaskRuntimeService, "DEFERRED_QUARANTINE_LIMIT", 2)
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    monkeypatch.setattr(
        TaskRuntimeService, "DEFERRED_QUARANTINE_RETRY_BASE_SECONDS", 0.01,
    )
    original = TaskRuntimeStore.quarantine_group_deletion

    def permanent_failure(
        _self: TaskRuntimeStore, _group_id: str, _audit_code: str,
    ) -> None:
        raise RuntimeStoreError("permanent bad-Group audit failure")

    monkeypatch.setattr(
        TaskRuntimeStore, "quarantine_group_deletion", permanent_failure,
    )
    restarted = KnoweServer(str(tmp_path))
    assert restarted.groups is not None and restarted.task_runtime is not None
    try:
        await restarted._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if restarted.groups.delete_transaction(healthy.group_id)["stage"] == "committed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("healthy confirmed deletion was starved")

        assert restarted.task_runtime.store.get_run(healthy_run) is None
        assert restarted.task_runtime.store.get_run(unrelated_run) is not None
        runtime_report = restarted.task_runtime.recovery_report["deletions"]
        server_report = restarted.task_runtime.recovery_report[
            "server_deletion_reconciliation"
        ]
        assert len(runtime_report["sample"]) <= 2
        assert runtime_report["processed_count"] <= 2
        assert len(server_report["sample"]) <= 2
        assert server_report["processed_count"] <= 2
        assert len(restarted.task_runtime._deferred_quarantines) <= 2  # noqa: SLF001

        monkeypatch.setattr(
            TaskRuntimeStore, "quarantine_group_deletion", original,
        )
        _wait_for(lambda: all(
            restarted.task_runtime.store.group_deletion_intent(group_id)["stage"]
            == "quarantined"
            for group_id in bad_ids
        ), timeout=4)
    finally:
        task = restarted._task_runtime_group_deletion_task  # noqa: SLF001
        if task is not None:
            task.cancel()
        _close_server(restarted)


def test_ten_thousand_intent_page_and_restart_cursor_stay_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    database = Path(service.store.db.path)
    service.close()
    rows = [(
        f"grp_{index:032x}", f"gdt_{index:032x}", f"{index + 1:064x}",
        f"confirmation-{index}", f"project-{index}",
        "prepared", "", "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z",
    ) for index in range(10_000)]
    with sqlite3.connect(database) as conn:
        conn.executemany(
            """INSERT INTO studycrew_group_deletions(
                   group_id, group_transaction_id, group_proof_digest,
                   confirmation_token, runtime_project_id, stage, audit_code,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()

    store = TaskRuntimeStore(database)
    try:
        first = store.group_deletion_intents_page(limit=2)
        second = store.group_deletion_intents_page(
            cursor=str(first["cursor"]), limit=2,
        )
        assert len(first["items"]) == len(second["items"]) == 2
        assert first["has_more"] and second["has_more"]
        assert first["cursor"] < second["cursor"]
    finally:
        store.close()

    monkeypatch.setattr(TaskRuntimeService, "DEFERRED_QUARANTINE_LIMIT", 2)
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)

    def fail_audit(
        _self: TaskRuntimeStore, _group_id: str, _audit_code: str,
    ) -> None:
        raise RuntimeStoreError("synthetic audit outage")

    monkeypatch.setattr(TaskRuntimeStore, "quarantine_group_deletion", fail_audit)
    first_service = TaskRuntimeService(tmp_path, groups)
    first_report = first_service.recovery_report["deletions"]
    first_cursor = str(first_report["cursor"])
    assert first_report["processed_count"] == len(first_report["sample"]) == 2
    assert len(first_service._deferred_quarantines) <= 2  # noqa: SLF001
    first_service.close()

    second_service = TaskRuntimeService(tmp_path, groups)
    try:
        second_report = second_service.recovery_report["deletions"]
        assert second_report["processed_count"] == len(second_report["sample"]) == 2
        assert str(second_report["sample"][0]["group_id"]) > first_cursor
        assert len(second_service._deferred_quarantines) <= 2  # noqa: SLF001
    finally:
        second_service.close()
