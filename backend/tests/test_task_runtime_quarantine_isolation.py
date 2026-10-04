from __future__ import annotations

import asyncio
import gc
import json
import sqlite3
import threading
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


async def _complete_runtime_delete(
    server: KnoweServer, group_id: str, runtime_project_id: str,
) -> None:
    assert server.groups is not None and server.task_runtime is not None
    _run_id, _transaction_id = _prepare_runtime_delete(
        server.task_runtime, server.groups, group_id, runtime_project_id,
    )
    transaction = server.groups.delete_transaction(group_id)
    assert transaction is not None
    await server._run_confirmed_group_delete(  # noqa: SLF001
        group_id, runtime_project_id, str(transaction["confirmation_token"]),
    )


async def _cancel_deletion_reconcile_task(server: KnoweServer) -> None:
    task = server._task_runtime_group_deletion_task  # noqa: SLF001
    if task is None:
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


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


@pytest.mark.parametrize("transient_head_read_failure", (False, True))
def test_overflow_retry_rediscovers_invalid_proofs_behind_current_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transient_head_read_failure: bool,
) -> None:
    """Drive the real bounded retry synchronously, without timer scheduling races."""
    monkeypatch.setattr(TaskRuntimeService, "DEFERRED_QUARANTINE_LIMIT", 2)
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    monkeypatch.setattr(TaskRuntimeService, "_arm_deferred_quarantine_retry_locked", lambda self: None)
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    bad_ids = ["grp_" + digit + "0" * 31 for digit in ("0", "1", "e", "f")]
    tail_id = "grp_" + "f" * 32
    original = service.store.quarantine_group_deletion
    try:
        for index, group_id in enumerate([*bad_ids, tail_id]):
            service.store.prepare_group_deletion(
                group_id, f"gdt_boundary_{index}", group_proof_digest=f"{index + 1:064x}",
                confirmation_token=f"fixture-{index}", runtime_project_id=f"project_boundary_{index}",
            )
        original(tail_id, "already_quarantined")

        def unavailable(_group_id, _audit_code):
            raise RuntimeStoreError("inert audit write failure")
        monkeypatch.setattr(service.store, "quarantine_group_deletion", unavailable)
        first = service.reconcile_group_deletions()
        overflow = service.reconcile_group_deletions()
        assert first["processed_count"] == overflow["processed_count"] == 2
        assert overflow["cursor"] == bad_ids[-1] and overflow["has_more"] is True
        assert len(service._deferred_quarantines) == 2
        assert service._deferred_quarantine_overflow is True

        recovered_writes = []
        def recovered_write(group_id, audit_code):
            recovered_writes.append(group_id)
            original(group_id, audit_code)
        monkeypatch.setattr(service.store, "quarantine_group_deletion", recovered_write)
        original_page = service.store.group_deletion_intents_page
        page_requests = []
        failed_head_reads = []

        def read_page(*, cursor, limit):
            page_requests.append((cursor, limit))
            if transient_head_read_failure and not failed_head_reads and cursor == "":
                failed_head_reads.append(cursor)
                raise RuntimeStoreError("inert single restarted-sweep read failure")
            return original_page(cursor=cursor, limit=limit)

        monkeypatch.setattr(service.store, "group_deletion_intents_page", read_page)
        for retry_index in range(5):
            service._retry_deferred_quarantines()
            assert len(service._deferred_quarantines) <= 2
            if transient_head_read_failure and retry_index == 0:
                assert service.recovery_report["deletions"]["view_unavailable"] is True
                assert not service._deferred_quarantines
                assert not service._deferred_quarantine_overflow
                assert service._deletion_reconcile_cursor == ""
                assert service._deletion_reconcile_has_more is True
                assert [service.store.group_deletion_intent(gid)["stage"] for gid in bad_ids] == [
                    "quarantined", "quarantined", "prepared", "prepared",
                ]
            if not (service._deferred_quarantines or service._deferred_quarantine_overflow
                    or service._deletion_reconcile_has_more):
                break
        assert all(service.store.group_deletion_intent(gid)["stage"] == "quarantined"
                   for gid in bad_ids)
        assert not service._deferred_quarantines and not service._deferred_quarantine_overflow
        assert not service._deletion_reconcile_has_more
        assert recovered_writes == bad_ids
        assert failed_head_reads == ([""] if transient_head_read_failure else [])
        assert all(limit == 2 for _, limit in page_requests)
        if transient_head_read_failure:
            assert [cursor for cursor, _ in page_requests[:2]] == ["", ""]
        settled_page_requests = list(page_requests)
        service._retry_deferred_quarantines()
        assert recovered_writes == bad_ids, "Idle recovery must not repeat settled writes"
        assert page_requests == settled_page_requests, "Idle recovery must not keep rescanning"
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", (1, 2))
async def test_complete_runtime_page_cannot_starve_canonical_only_prepared_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: int,
) -> None:
    """A durable terminal runtime page must not own the shared candidate budget."""
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", batch)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    completed = [server.groups.create_group(f"Completed {index}") for index in range(2)]
    pending = server.groups.create_group("Canonical crash boundary")
    for index, group in enumerate(completed):
        await _complete_runtime_delete(
            server, group.group_id, f"project_complete_receipt_{index}",
        )

    preview = server.groups.delete_preview(pending.group_id)
    pending_transaction = server.groups.prepare_delete(
        pending.group_id, preview.confirmation_token,
        runtime_project_id="project_canonical_only_pending",
    )
    assert pending_transaction["stage"] == "prepared"
    server.task_runtime.store.set_deletion_reconcile_cursor(
        "task_runtime_service", "",
    )
    server.task_runtime.store.set_deletion_reconcile_cursor(
        "server_task_runtime_source", "",
    )
    server.task_runtime.store.set_deletion_reconcile_cursor(
        "group_repository", "",
    )
    server.task_runtime._deletion_reconcile_cursor = None  # noqa: SLF001

    try:
        for _ in range(6):
            await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
            transaction = server.groups.delete_transaction(pending.group_id)
            if transaction is not None and transaction["stage"] == "committed":
                break
        else:
            raise AssertionError("canonical-only prepared deletion was starved")

        with pytest.raises(GroupNotFoundError):
            server.groups.get(pending.group_id)
        state = server.task_runtime.recovery_report[
            "server_deletion_reconciliation"
        ]
        assert state["processed_count"] <= batch
        assert len(state["sample"]) <= batch
        assert state["runtime_quota"] + state["group_quota"] <= batch
        deadline = time.monotonic() + 2
        while (
            server._task_runtime_group_deletion_task is not None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        assert server._task_runtime_group_deletion_task is None, (  # noqa: SLF001
            server.task_runtime.recovery_report["server_deletion_reconciliation"]
        )
        assert not server.task_runtime.recovery_report[
            "server_deletion_reconciliation"
        ]["has_more"]
    finally:
        task = server._task_runtime_group_deletion_task  # noqa: SLF001
        if task is not None:
            task.cancel()
        _close_server(server)


@pytest.mark.asyncio
async def test_blocked_canonical_page_cannot_starve_runtime_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    blocked = [server.groups.create_group(f"Blocked canonical {index}") for index in range(3)]
    for group in blocked:
        preview = server.groups.delete_preview(group.group_id)
        server.groups.prepare_delete(
            group.group_id, preview.confirmation_token,
            runtime_project_id=f"project_blocked_{group.group_id}",
        )
    actionable = server.groups.create_group("Runtime actionable")
    actionable_run, _transaction_id = _prepare_runtime_delete(
        server.task_runtime, server.groups, actionable.group_id,
        "project_runtime_actionable",
    )
    blocked_ids = {group.group_id for group in blocked}
    original_blocked = server.task_runtime.group_deletion_recovery_blocked

    def permanent_block(group_id: str) -> tuple[bool, bool]:
        if group_id in blocked_ids:
            return True, False
        return original_blocked(group_id)

    monkeypatch.setattr(
        server.task_runtime, "group_deletion_recovery_blocked", permanent_block,
    )
    server.task_runtime.store.set_deletion_reconcile_cursor(
        "task_runtime_service", "",
    )
    server.task_runtime.store.set_deletion_reconcile_cursor(
        "server_task_runtime_source", "",
    )
    server.task_runtime.store.set_deletion_reconcile_cursor(
        "group_repository", "",
    )
    server.task_runtime._deletion_reconcile_cursor = None  # noqa: SLF001

    try:
        await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        assert server.task_runtime.store.get_run(actionable_run) is None
        with pytest.raises(GroupNotFoundError):
            server.groups.get(actionable.group_id)
        assert all(
            server.groups.delete_transaction(group_id)["stage"] == "prepared"
            for group_id in blocked_ids
        )
    finally:
        await _cancel_deletion_reconcile_task(server)
        _close_server(server)


@pytest.mark.asyncio
async def test_dual_source_cursors_resume_after_restart_and_process_each_delete_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    complete_groups = [
        server.groups.create_group(f"Durable complete {index}") for index in range(4)
    ]
    for index, group in enumerate(complete_groups):
        await _complete_runtime_delete(
            server, group.group_id, f"project_durable_complete_{index}",
        )
    pending_groups = [
        server.groups.create_group(f"Paged canonical {index}") for index in range(4)
    ]
    for index, group in enumerate(pending_groups):
        _prepare_runtime_delete(
            server.task_runtime, server.groups, group.group_id,
            f"project_paged_canonical_{index}",
        )
    for owner in (
        "task_runtime_service", "server_task_runtime_source", "group_repository",
        "server_task_runtime_source_state", "server_group_repository_source_state",
        "server_deletion_source_turn",
    ):
        server.task_runtime.store.set_deletion_reconcile_cursor(owner, "")
    server.task_runtime._deletion_reconcile_cursor = None  # noqa: SLF001

    runtime_cursor = group_cursor = ""
    for _ in range(10):
        await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        await _cancel_deletion_reconcile_task(server)
        runtime_cursor = server.task_runtime.store.deletion_reconcile_cursor(
            "server_task_runtime_source",
        )
        group_cursor = server.task_runtime.store.deletion_reconcile_cursor(
            "group_repository",
        )
        if runtime_cursor and group_cursor:
            break
    assert runtime_cursor and group_cursor
    _close_server(server)

    restarted = KnoweServer(str(tmp_path))
    assert restarted.groups is not None and restarted.task_runtime is not None
    assert restarted.task_runtime.store.deletion_reconcile_cursor(
        "server_task_runtime_source",
    ) == runtime_cursor
    calls: dict[str, int] = {}
    original_run = restarted._run_confirmed_group_delete  # noqa: SLF001

    async def count_delete(group_id: str, *args, **kwargs):
        calls[group_id] = calls.get(group_id, 0) + 1
        return await original_run(group_id, *args, **kwargs)

    monkeypatch.setattr(restarted, "_run_confirmed_group_delete", count_delete)
    resumed_after_persisted_cursor = False
    try:
        for _ in range(40):
            await restarted._reconcile_task_runtime_group_deletions()  # noqa: SLF001
            runtime_sample = restarted.task_runtime.recovery_report["deletions"][
                "sample"
            ]
            if runtime_sample:
                resumed_after_persisted_cursor = (
                    str(runtime_sample[0]["group_id"]) > runtime_cursor
                )
            await _cancel_deletion_reconcile_task(restarted)
            if all(
                restarted.groups.delete_transaction(group.group_id)["stage"] == "committed"
                for group in pending_groups
            ):
                break
        else:
            raise AssertionError("paged canonical deletions did not converge after restart")

        assert all(count == 1 for count in calls.values())
        assert set(calls).issubset({group.group_id for group in pending_groups})
        assert resumed_after_persisted_cursor
    finally:
        await _cancel_deletion_reconcile_task(restarted)
        _close_server(restarted)


@pytest.mark.asyncio
async def test_terminal_only_continuation_uses_idle_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    groups = [server.groups.create_group(f"Idle receipt {index}") for index in range(4)]
    for index, group in enumerate(groups):
        await _complete_runtime_delete(
            server, group.group_id, f"project_idle_receipt_{index}",
        )
    for owner in (
        "task_runtime_service", "server_task_runtime_source", "group_repository",
        "server_task_runtime_source_state", "server_group_repository_source_state",
        "server_deletion_source_turn",
    ):
        server.task_runtime.store.set_deletion_reconcile_cursor(owner, "")
    server.task_runtime._deletion_reconcile_cursor = None  # noqa: SLF001

    try:
        await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        state = server.task_runtime.recovery_report[
            "server_deletion_reconciliation"
        ]
        assert state["has_more"]
        assert state["processed_count"] == 0
        assert state["continuation_delay_seconds"] >= (
            server.TASK_RUNTIME_DELETION_RECONCILE_IDLE_SECONDS
        )
        assert state["continuation_delay_seconds"] > (
            server.TASK_RUNTIME_DELETION_RECONCILE_ACTIVE_SECONDS
        )
        assert state["runtime_quota"] + state["group_quota"] <= 2
    finally:
        await _cancel_deletion_reconcile_task(server)
        _close_server(server)


@pytest.mark.asyncio
async def test_permanent_candidate_failure_does_not_pin_later_pages_or_spin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    groups = [server.groups.create_group(f"Fair failure {index}") for index in range(4)]
    for index, group in enumerate(groups):
        preview = server.groups.delete_preview(group.group_id)
        server.groups.prepare_delete(
            group.group_id, preview.confirmation_token,
            runtime_project_id=f"project_fair_failure_{index}",
        )
    poison_id = min(group.group_id for group in groups)
    healthy_ids = {group.group_id for group in groups} - {poison_id}
    original_run = server._run_confirmed_group_delete  # noqa: SLF001

    async def fail_one(group_id: str, *args, **kwargs):
        if group_id == poison_id:
            raise PermissionError("permanent synthetic candidate failure")
        return await original_run(group_id, *args, **kwargs)

    monkeypatch.setattr(server, "_run_confirmed_group_delete", fail_one)
    for owner in (
        "task_runtime_service", "server_task_runtime_source", "group_repository",
        "server_task_runtime_source_state", "server_group_repository_source_state",
        "server_deletion_source_turn",
    ):
        server.task_runtime.store.set_deletion_reconcile_cursor(owner, "")
    server.task_runtime._deletion_reconcile_cursor = None  # noqa: SLF001

    try:
        for _ in range(12):
            await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
            await _cancel_deletion_reconcile_task(server)
            if all(
                server.groups.delete_transaction(group_id)["stage"] == "committed"
                for group_id in healthy_ids
            ):
                break
        else:
            raise AssertionError("poison candidate pinned a later canonical page")

        assert server.groups.delete_transaction(poison_id)["stage"] == "prepared"
        for _ in range(12):
            await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
            await _cancel_deletion_reconcile_task(server)
            state = server.task_runtime.recovery_report[
                "server_deletion_reconciliation"
            ]
            if (
                state["sample"]
                and state["sample"][0].get("group_id") == poison_id
                and state["continuation_delay_seconds"]
                >= server.TASK_RUNTIME_DELETION_RECONCILE_IDLE_SECONDS
            ):
                break
        else:
            raise AssertionError("permanent candidate did not enter idle backoff")
        assert state["has_more"]
        assert state["processed_count"] <= 2
    finally:
        await _cancel_deletion_reconcile_task(server)
        _close_server(server)


@pytest.mark.asyncio
async def test_cancel_before_first_candidate_effect_replays_page_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    groups = [server.groups.create_group(f"Crash checkpoint {index}") for index in range(4)]
    for index, group in enumerate(groups):
        preview = server.groups.delete_preview(group.group_id)
        server.groups.prepare_delete(
            group.group_id, preview.confirmation_token,
            runtime_project_id=f"project_crash_checkpoint_{index}",
        )
    for owner in (
        "server_task_runtime_source", "group_repository",
        "server_task_runtime_source_state", "server_group_repository_source_state",
        "server_deletion_source_turn",
    ):
        server.task_runtime.store.set_deletion_reconcile_cursor(owner, "")

    async def cancel_before_effect(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(server, "_run_confirmed_group_delete", cancel_before_effect)
    with pytest.raises(asyncio.CancelledError):
        await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
    assert server.task_runtime.store.deletion_reconcile_cursor(
        "group_repository",
    ) == ""
    _close_server(server)

    restarted = KnoweServer(str(tmp_path))
    assert restarted.groups is not None and restarted.task_runtime is not None
    try:
        await restarted._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        deadline = time.monotonic() + 3
        while (
            restarted._task_runtime_group_deletion_task is not None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        assert all(
            restarted.groups.delete_transaction(group.group_id)["stage"] == "committed"
            for group in groups
        )
    finally:
        await _cancel_deletion_reconcile_task(restarted)
        _close_server(restarted)


@pytest.mark.asyncio
async def test_last_page_authority_view_outage_retries_without_foreground_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    monkeypatch.setattr(
        KnoweServer, "TASK_RUNTIME_DELETION_RECONCILE_IDLE_SECONDS", 0.01,
    )
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    group = server.groups.create_group("Transient authority outage")
    preview = server.groups.delete_preview(group.group_id)
    server.groups.prepare_delete(
        group.group_id, preview.confirmation_token,
        runtime_project_id="project_transient_authority_outage",
    )
    original_blocked = server.task_runtime.group_deletion_recovery_blocked
    calls = {"count": 0}

    def fail_exact_view_once(group_id: str) -> tuple[bool, bool]:
        calls["count"] += 1
        if calls["count"] == 1:
            return True, True
        return original_blocked(group_id)

    monkeypatch.setattr(
        server.task_runtime,
        "group_deletion_recovery_blocked",
        fail_exact_view_once,
    )
    try:
        await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            transaction = server.groups.delete_transaction(group.group_id)
            if transaction is not None and transaction["stage"] == "committed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("authority-view recovery required a foreground call")
        assert calls["count"] >= 2
        while (
            server._task_runtime_group_deletion_task is not None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        assert server._task_runtime_group_deletion_task is None  # noqa: SLF001
    finally:
        await _cancel_deletion_reconcile_task(server)
        _close_server(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_boundary", (
    "after_group_prepare",
    "after_runtime_suspend",
    "after_runtime_intent",
    "after_runtime_cleanup",
    "after_legacy_runtime_deleted",
    "after_group_commit",
    "after_runtime_complete",
))
async def test_each_delete_effect_boundary_replays_idempotently_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_boundary: str,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    group = server.groups.create_group(f"Boundary {fault_boundary}")
    preview = server.groups.delete_preview(group.group_id)
    server.groups.prepare_delete(
        group.group_id, preview.confirmation_token,
        runtime_project_id=f"project_{fault_boundary}",
    )
    fired = {"value": False}

    async def cancel_at_boundary(boundary: str, _group_id: str) -> None:
        if boundary == fault_boundary and not fired["value"]:
            fired["value"] = True
            raise asyncio.CancelledError

    server._task_runtime_delete_fault = cancel_at_boundary  # noqa: SLF001
    with pytest.raises(asyncio.CancelledError):
        await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
    assert server.task_runtime.store.deletion_reconcile_cursor(
        "group_repository",
    ) == ""
    _close_server(server)

    restarted = KnoweServer(str(tmp_path))
    assert restarted.groups is not None and restarted.task_runtime is not None
    try:
        await restarted._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        deadline = time.monotonic() + 3
        while (
            restarted._task_runtime_group_deletion_task is not None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        transaction = restarted.groups.delete_transaction(group.group_id)
        assert transaction is not None and transaction["stage"] == "committed"
        intent = restarted.task_runtime.store.group_deletion_intent(group.group_id)
        assert intent is not None and intent["stage"] == "complete"
    finally:
        await _cancel_deletion_reconcile_task(restarted)
        _close_server(restarted)


@pytest.mark.asyncio
async def test_page_replay_does_not_repeat_already_committed_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 4)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    groups = [server.groups.create_group(f"Mixed page {index}") for index in range(3)]
    for index, group in enumerate(groups):
        preview = server.groups.delete_preview(group.group_id)
        server.groups.prepare_delete(
            group.group_id, preview.confirmation_token,
            runtime_project_id=f"project_mixed_page_{index}",
        )
    ordered = sorted(group.group_id for group in groups)
    calls: dict[str, int] = {}
    original_run = server._run_confirmed_group_delete  # noqa: SLF001

    async def commit_then_cancel(group_id: str, *args, **kwargs):
        calls[group_id] = calls.get(group_id, 0) + 1
        if group_id == ordered[1]:
            raise asyncio.CancelledError
        return await original_run(group_id, *args, **kwargs)

    monkeypatch.setattr(server, "_run_confirmed_group_delete", commit_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
    assert calls == {ordered[0]: 1, ordered[1]: 1}
    assert server.groups.delete_transaction(ordered[0])["stage"] == "committed"
    assert server.task_runtime.store.deletion_reconcile_cursor(
        "group_repository",
    ) == ""
    _close_server(server)

    restarted = KnoweServer(str(tmp_path))
    assert restarted.groups is not None and restarted.task_runtime is not None
    replay_calls: dict[str, int] = {}
    restarted_run = restarted._run_confirmed_group_delete  # noqa: SLF001

    async def count_replay(group_id: str, *args, **kwargs):
        replay_calls[group_id] = replay_calls.get(group_id, 0) + 1
        return await restarted_run(group_id, *args, **kwargs)

    monkeypatch.setattr(restarted, "_run_confirmed_group_delete", count_replay)
    try:
        await restarted._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        deadline = time.monotonic() + 3
        while (
            restarted._task_runtime_group_deletion_task is not None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        assert all(
            restarted.groups.delete_transaction(group_id)["stage"] == "committed"
            for group_id in ordered
        )
        assert ordered[0] not in replay_calls
        assert all(count == 1 for count in replay_calls.values())
    finally:
        await _cancel_deletion_reconcile_task(restarted)
        _close_server(restarted)


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_commits_first", (False, True))
async def test_checkpoint_write_uncertainty_replays_without_duplicate_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    checkpoint_commits_first: bool,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    group = server.groups.create_group("Checkpoint uncertainty")
    preview = server.groups.delete_preview(group.group_id)
    server.groups.prepare_delete(
        group.group_id, preview.confirmation_token,
        runtime_project_id="project_checkpoint_uncertainty",
    )
    original_checkpoint = server.task_runtime.store.set_deletion_reconcile_checkpoints
    checkpoint_calls = {"count": 0}
    effect_calls = {"count": 0}
    original_run = server._run_confirmed_group_delete  # noqa: SLF001

    async def count_effect(*args, **kwargs):
        effect_calls["count"] += 1
        return await original_run(*args, **kwargs)

    def uncertain_checkpoint(values):
        checkpoint_calls["count"] += 1
        if checkpoint_calls["count"] == 1:
            if checkpoint_commits_first:
                original_checkpoint(values)
            raise RuntimeStoreError("synthetic checkpoint uncertainty")
        original_checkpoint(values)

    monkeypatch.setattr(server, "_run_confirmed_group_delete", count_effect)
    monkeypatch.setattr(
        server.task_runtime.store,
        "set_deletion_reconcile_checkpoints",
        uncertain_checkpoint,
    )
    try:
        await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        deadline = time.monotonic() + 2
        while (
            server._task_runtime_group_deletion_task is not None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        assert effect_calls["count"] == 1
        assert server.groups.delete_transaction(group.group_id)["stage"] == "committed"
        assert not server.task_runtime.recovery_report[
            "server_deletion_reconciliation"
        ]["has_more"]
    finally:
        await _cancel_deletion_reconcile_task(server)
        _close_server(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ("deferred", "last_error"))
async def test_runtime_source_failure_retries_without_foreground_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_kind: str,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    monkeypatch.setattr(
        KnoweServer, "TASK_RUNTIME_DELETION_RECONCILE_IDLE_SECONDS", 0.01,
    )
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    group = server.groups.create_group(f"Runtime {failure_kind}")
    run_id, _transaction_id = _prepare_runtime_delete(
        server.task_runtime, server.groups, group.group_id,
        f"project_runtime_{failure_kind}",
    )
    original_reconcile = server.task_runtime.reconcile_group_deletions
    original_group_page = server.groups.pending_delete_transactions_page
    calls = {"runtime": 0, "group": 0}

    def fail_runtime_once(*, limit=None, cursor_owner="task_runtime_service", **kwargs):
        if cursor_owner != "server_task_runtime_source":
            return original_reconcile(
                limit=limit, cursor_owner=cursor_owner, **kwargs,
            )
        calls["runtime"] += 1
        if calls["runtime"] == 1:
            return {
                "processed_count": 1 if failure_kind == "deferred" else 0,
                "deferred_count": 1 if failure_kind == "deferred" else 0,
                "sample": ([{
                    "group_id": group.group_id,
                    "status": "deferred",
                    "reason": "RuntimeStoreError",
                }] if failure_kind == "deferred" else []),
                "cursor": "", "has_more": False,
                "view_unavailable": False,
                "last_error": "RuntimeStoreError" if failure_kind == "last_error" else "",
                "limit": int(limit or 2),
            }
        return original_reconcile(
            limit=limit, cursor_owner=cursor_owner, **kwargs,
        )

    def hide_first_group_page(*, cursor="", limit=64):
        calls["group"] += 1
        if calls["group"] == 1:
            return {"items": (), "cursor": cursor, "has_more": False, "limit": limit}
        return original_group_page(cursor=cursor, limit=limit)

    monkeypatch.setattr(
        server.task_runtime, "reconcile_group_deletions", fail_runtime_once,
    )
    monkeypatch.setattr(
        server.groups, "pending_delete_transactions_page", hide_first_group_page,
    )
    try:
        await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            transaction = server.groups.delete_transaction(group.group_id)
            if transaction is not None and transaction["stage"] == "committed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("runtime source failure did not recover autonomously")
        assert calls["runtime"] >= 2
        assert server.task_runtime.store.get_run(run_id) is None
    finally:
        await _cancel_deletion_reconcile_task(server)
        _close_server(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", (1, 2))
async def test_restart_generation_invalidates_old_group_done_for_new_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: int,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", batch)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    server.task_runtime.store.set_deletion_reconcile_checkpoints({
        "server_task_runtime_source": "",
        "group_repository": "",
        "server_task_runtime_source_state": "retry",
        "server_group_repository_source_state": "done",
        "server_deletion_source_turn": "task_runtime_service",
    })
    group = server.groups.create_group("Prepared after old Group done")
    preview = server.groups.delete_preview(group.group_id)
    server.groups.prepare_delete(
        group.group_id, preview.confirmation_token,
        runtime_project_id="project_after_old_group_done",
    )
    _close_server(server)

    restarted = KnoweServer(str(tmp_path))
    assert restarted.groups is not None and restarted.task_runtime is not None
    try:
        await restarted._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        deadline = time.monotonic() + 3
        while (
            restarted._task_runtime_group_deletion_task is not None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        transaction = restarted.groups.delete_transaction(group.group_id)
        assert transaction is not None and transaction["stage"] == "committed"
    finally:
        await _cancel_deletion_reconcile_task(restarted)
        _close_server(restarted)


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", (1, 2))
async def test_restart_generation_invalidates_old_runtime_done_for_new_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: int,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", batch)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    server.task_runtime.store.set_deletion_reconcile_checkpoints({
        "server_task_runtime_source": "",
        "group_repository": "",
        "server_task_runtime_source_state": "done",
        "server_group_repository_source_state": "retry",
        "server_deletion_source_turn": "task_runtime_service",
    })
    group = server.groups.create_group("Intent after old runtime done")
    run_id, _transaction_id = _prepare_runtime_delete(
        server.task_runtime, server.groups, group.group_id,
        "project_after_old_runtime_done",
    )
    _close_server(server)

    restarted = KnoweServer(str(tmp_path))
    assert restarted.groups is not None and restarted.task_runtime is not None
    original_group_page = restarted.groups.pending_delete_transactions_page
    group_calls = {"count": 0}

    def hide_first_group_page(*, cursor="", limit=64):
        group_calls["count"] += 1
        if group_calls["count"] == 1:
            return {"items": (), "cursor": cursor, "has_more": False, "limit": limit}
        return original_group_page(cursor=cursor, limit=limit)

    monkeypatch.setattr(
        restarted.groups, "pending_delete_transactions_page", hide_first_group_page,
    )
    try:
        await restarted._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        first_state = restarted.task_runtime.recovery_report[
            "server_deletion_reconciliation"
        ]
        assert first_state["runtime_quota"] > 0
        deadline = time.monotonic() + 3
        while (
            restarted._task_runtime_group_deletion_task is not None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        assert restarted.task_runtime.store.get_run(run_id) is None
        assert restarted.groups.delete_transaction(group.group_id)["stage"] == "committed"
    finally:
        await _cancel_deletion_reconcile_task(restarted)
        _close_server(restarted)


@pytest.mark.asyncio
@pytest.mark.parametrize(("old_runtime_state", "old_group_state", "new_source"), (
    ("done", "pending", "runtime"),
    ("pending", "done", "group"),
    ("done", "done", "runtime"),
    ("done", "done", "group"),
))
async def test_new_source_revision_opens_coordinated_generation_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    old_runtime_state: str, old_group_state: str, new_source: str,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    initial_generation = 7
    server.task_runtime.store.set_deletion_reconcile_checkpoints({
        "server_task_runtime_source": "",
        "group_repository": "",
        "server_task_runtime_source_state": old_runtime_state,
        "server_group_repository_source_state": old_group_state,
        "server_deletion_source_turn": "task_runtime_service",
        "server_deletion_sweep_generation": str(initial_generation),
        "server_observed_runtime_deletion_revision": str(
            server.task_runtime.store.group_deletion_revision()
        ),
        "server_observed_group_deletion_revision": str(
            server.groups.deletion_revision()
        ),
    })
    server._task_runtime_group_deletion_generation_started = True  # noqa: SLF001
    group = server.groups.create_group(f"In-process new {new_source}")
    preview = server.groups.delete_preview(group.group_id)
    transaction = server.groups.prepare_delete(
        group.group_id, preview.confirmation_token,
        runtime_project_id=f"project_in_process_{new_source}",
    )
    if new_source == "runtime":
        server.task_runtime.prepare_group_deletion(
            group.group_id, str(transaction["transaction_id"]),
        )

    try:
        await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        state = server.task_runtime.recovery_report[
            "server_deletion_reconciliation"
        ]
        assert state["generation"] > initial_generation
        assert state["runtime_quota"] > 0
        assert state["group_quota"] > 0
        deadline = time.monotonic() + 3
        while (
            server._task_runtime_group_deletion_task is not None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        assert server.groups.delete_transaction(group.group_id)["stage"] == "committed"
        assert int(server.task_runtime.store.deletion_reconcile_cursor(
            "server_deletion_sweep_generation",
        )) >= state["generation"]
    finally:
        await _cancel_deletion_reconcile_task(server)
        _close_server(server)


def test_source_revisions_advance_once_per_new_durable_intent_and_survive_restart(
    tmp_path: Path,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    initial_group_revision = server.groups.deletion_revision()
    initial_runtime_revision = server.task_runtime.store.group_deletion_revision()
    group = server.groups.create_group("Durable source revisions")
    preview = server.groups.delete_preview(group.group_id)
    transaction = server.groups.prepare_delete(
        group.group_id, preview.confirmation_token,
        runtime_project_id="project_durable_source_revisions",
    )
    # Idempotent replays retain the same source identity and must not continuously
    # invalidate a clean coordinated sweep.
    replay = server.groups.prepare_delete(
        group.group_id, preview.confirmation_token,
        runtime_project_id="project_durable_source_revisions",
    )
    assert replay["transaction_id"] == transaction["transaction_id"]
    assert server.groups.deletion_revision() == initial_group_revision + 1
    server.task_runtime.prepare_group_deletion(
        group.group_id, str(transaction["transaction_id"]),
    )
    server.task_runtime.prepare_group_deletion(
        group.group_id, str(transaction["transaction_id"]),
    )
    assert (
        server.task_runtime.store.group_deletion_revision()
        == initial_runtime_revision + 1
    )
    _close_server(server)

    restarted = KnoweServer(str(tmp_path))
    assert restarted.groups is not None and restarted.task_runtime is not None
    try:
        assert restarted.groups.deletion_revision() == initial_group_revision + 1
        assert (
            restarted.task_runtime.store.group_deletion_revision()
            == initial_runtime_revision + 1
        )
    finally:
        _close_server(restarted)


def test_generation_checkpoint_is_atomic_and_persists_all_source_proofs(
    tmp_path: Path,
) -> None:
    store = TaskRuntimeStore(tmp_path / "runtime.sqlite3")
    owners = (
        "server_task_runtime_source",
        "group_repository",
        "server_task_runtime_source_state",
        "server_group_repository_source_state",
        "server_deletion_source_turn",
        "server_deletion_sweep_generation",
        "server_observed_runtime_deletion_revision",
        "server_observed_group_deletion_revision",
    )
    before = {owner: f"old-{index}" for index, owner in enumerate(owners)}
    after = {owner: f"new-{index}" for index, owner in enumerate(owners)}
    store.set_deletion_reconcile_checkpoints(before)
    with store.db.transaction() as conn:
        conn.execute(
            """CREATE TRIGGER fail_generation_checkpoint
               BEFORE UPDATE OF value ON studycrew_scheduler_state
               WHEN NEW.name='deletion_cursor:server_deletion_sweep_generation'
               BEGIN SELECT RAISE(ABORT, 'synthetic generation checkpoint failure'); END""",
        )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="synthetic generation"):
            store.set_deletion_reconcile_checkpoints(after)
        assert {
            owner: store.deletion_reconcile_cursor(owner) for owner in owners
        } == before
        with store.db.transaction() as conn:
            conn.execute("DROP TRIGGER fail_generation_checkpoint")
        store.set_deletion_reconcile_checkpoints(after)
    finally:
        store.close()

    restarted = TaskRuntimeStore(tmp_path / "runtime.sqlite3")
    try:
        assert {
            owner: restarted.deletion_reconcile_cursor(owner) for owner in owners
        } == after
    finally:
        restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("insert_before_checkpoint_commit", (True, False))
async def test_prepare_during_checkpoint_opens_followup_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    insert_before_checkpoint_commit: bool,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    group = server.groups.create_group("Prepare during generation checkpoint")
    preview = server.groups.delete_preview(group.group_id)
    original_checkpoint = server.task_runtime.store.set_deletion_reconcile_checkpoints
    checkpoint_calls = {"count": 0}

    def insert_prepare() -> None:
        server.groups.prepare_delete(
            group.group_id, preview.confirmation_token,
            runtime_project_id="project_prepare_during_checkpoint",
        )

    def checkpoint_with_new_work(values):
        checkpoint_calls["count"] += 1
        if checkpoint_calls["count"] != 1:
            original_checkpoint(values)
            return
        if insert_before_checkpoint_commit:
            insert_prepare()
            original_checkpoint(values)
        else:
            original_checkpoint(values)
            insert_prepare()

    monkeypatch.setattr(
        server.task_runtime.store,
        "set_deletion_reconcile_checkpoints",
        checkpoint_with_new_work,
    )
    try:
        await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        deadline = time.monotonic() + 3
        while (
            server._task_runtime_group_deletion_task is not None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        transaction = server.groups.delete_transaction(group.group_id)
        assert transaction is not None and transaction["stage"] == "committed"
        assert checkpoint_calls["count"] >= 2
    finally:
        await _cancel_deletion_reconcile_task(server)
        _close_server(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_boundary", (
    "after_group_prepare",
    "after_runtime_intent",
))
async def test_live_prepare_failure_wakes_idle_reconciliation_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_boundary: str,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
    assert server._task_runtime_group_deletion_task is None  # noqa: SLF001
    initial_generation = int(server.task_runtime.recovery_report[
        "server_deletion_reconciliation"
    ]["generation"])
    loop = asyncio.get_running_loop()
    original_call_soon_threadsafe = loop.call_soon_threadsafe
    wake_calls = {"count": 0}

    def count_wake(callback, *args, **kwargs):
        wake_calls["count"] += 1
        return original_call_soon_threadsafe(callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_soon_threadsafe", count_wake)
    group = server.groups.create_group(f"Autonomous {fault_boundary}")
    preview = server.groups.delete_preview(group.group_id)

    def fail_once(boundary: str, _group_id: str) -> None:
        if boundary == fault_boundary:
            raise RuntimeError(f"synthetic {fault_boundary} request failure")

    server._task_runtime_delete_fault = fail_once  # noqa: SLF001
    with pytest.raises(RuntimeError, match=fault_boundary):
        await server._run_confirmed_group_delete(  # noqa: SLF001
            group.group_id, f"project_live_{fault_boundary}",
            preview.confirmation_token,
        )
    server._task_runtime_delete_fault = None  # noqa: SLF001

    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            transaction = server.groups.delete_transaction(group.group_id)
            if transaction is not None and transaction["stage"] == "committed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("durable request failure did not wake reconciliation")
        while (
            server._task_runtime_group_deletion_task is not None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        assert server._task_runtime_group_deletion_task is None  # noqa: SLF001
        assert int(server.task_runtime.recovery_report[
            "server_deletion_reconciliation"
        ]["generation"]) > initial_generation
        assert wake_calls["count"] == 1
    finally:
        await _cancel_deletion_reconcile_task(server)
        _close_server(server)


@pytest.mark.asyncio
async def test_prepare_after_terminal_reconcile_return_wakes_without_foreground_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
    assert not server.task_runtime.recovery_report[
        "server_deletion_reconciliation"
    ]["has_more"]
    assert server._task_runtime_group_deletion_task is None  # noqa: SLF001
    group = server.groups.create_group("Prepare after terminal return")
    preview = server.groups.delete_preview(group.group_id)
    server.groups.prepare_delete(
        group.group_id, preview.confirmation_token,
        runtime_project_id="project_after_terminal_return",
    )

    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            transaction = server.groups.delete_transaction(group.group_id)
            if transaction is not None and transaction["stage"] == "committed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("post-return prepare required foreground reconciliation")
        while (
            server._task_runtime_group_deletion_task is not None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        assert server._task_runtime_group_deletion_task is None  # noqa: SLF001
    finally:
        await _cancel_deletion_reconcile_task(server)
        _close_server(server)


@pytest.mark.asyncio
async def test_runtime_intent_from_foreign_thread_wakes_loop_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
    group = server.groups.create_group("Foreign-thread runtime intent")
    preview = server.groups.delete_preview(group.group_id)
    listener = server.groups.deletion_prepare_listener
    server.groups.deletion_prepare_listener = None
    transaction = server.groups.prepare_delete(
        group.group_id, preview.confirmation_token,
        runtime_project_id="project_foreign_thread_intent",
    )
    server.groups.deletion_prepare_listener = listener

    try:
        await asyncio.to_thread(
            server.task_runtime.prepare_group_deletion,
            group.group_id,
            str(transaction["transaction_id"]),
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = server.groups.delete_transaction(group.group_id)
            if current is not None and current["stage"] == "committed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("foreign-thread runtime intent did not converge")
        while (
            server._task_runtime_group_deletion_task is not None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        assert server._task_runtime_group_deletion_task is None  # noqa: SLF001
    finally:
        await _cancel_deletion_reconcile_task(server)
        _close_server(server)


@pytest.mark.asyncio
@pytest.mark.parametrize(("runtime_state", "group_state", "new_source"), (
    ("pending", "pending", "group"),
    ("pending", "done", "group"),
    ("done", "pending", "runtime"),
))
async def test_new_source_wake_reopens_pending_and_one_done_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    runtime_state: str, group_state: str, new_source: str,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
    initial_generation = int(server.task_runtime.recovery_report[
        "server_deletion_reconciliation"
    ]["generation"])
    server.task_runtime.store.set_deletion_reconcile_checkpoints({
        "server_task_runtime_source": "",
        "group_repository": "",
        "server_task_runtime_source_state": runtime_state,
        "server_group_repository_source_state": group_state,
        "server_deletion_source_turn": "task_runtime_service",
        "server_deletion_sweep_generation": str(initial_generation),
        "server_observed_runtime_deletion_revision": str(
            server.task_runtime.store.group_deletion_revision()
        ),
        "server_observed_group_deletion_revision": str(
            server.groups.deletion_revision()
        ),
    })
    group = server.groups.create_group(
        f"Autonomous {runtime_state} {group_state} {new_source}",
    )
    preview = server.groups.delete_preview(group.group_id)
    group_listener = server.groups.deletion_prepare_listener
    if new_source == "runtime":
        server.groups.deletion_prepare_listener = None
    transaction = server.groups.prepare_delete(
        group.group_id, preview.confirmation_token,
        runtime_project_id=f"project_autonomous_{new_source}",
    )
    server.groups.deletion_prepare_listener = group_listener
    if new_source == "runtime":
        server.task_runtime.prepare_group_deletion(
            group.group_id, str(transaction["transaction_id"]),
        )

    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = server.groups.delete_transaction(group.group_id)
            if current is not None and current["stage"] == "committed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("source wake did not reopen coordinated generation")
        while (
            server._task_runtime_group_deletion_task is not None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        assert server._task_runtime_group_deletion_task is None  # noqa: SLF001
        assert int(server.task_runtime.recovery_report[
            "server_deletion_reconciliation"
        ]["generation"]) > initial_generation
    finally:
        await _cancel_deletion_reconcile_task(server)
        _close_server(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("burst_size", (100, 10_000))
async def test_foreign_thread_wake_burst_is_coalesced_to_one_loop_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, burst_size: int,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
    loop = asyncio.get_running_loop()
    original_call_soon_threadsafe = loop.call_soon_threadsafe
    wake_calls = {"count": 0}

    def count_wake(callback, *args, **kwargs):
        wake_calls["count"] += 1
        return original_call_soon_threadsafe(callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_soon_threadsafe", count_wake)
    listener = server.groups.deletion_prepare_listener
    assert listener is not None
    def produce_burst() -> None:
        for _ in range(burst_size):
            listener()

    producer = threading.Thread(target=produce_burst, daemon=True)
    producer.start()
    producer.join(timeout=5)
    assert not producer.is_alive()
    assert wake_calls["count"] == 1

    try:
        await asyncio.sleep(0.05)
        deadline = time.monotonic() + 2
        while (
            server._task_runtime_group_deletion_task is not None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)
        assert server._task_runtime_group_deletion_task is None  # noqa: SLF001
        assert not server._task_runtime_group_deletion_wake_scheduled  # noqa: SLF001
    finally:
        await _cancel_deletion_reconcile_task(server)
        _close_server(server)


@pytest.mark.asyncio
async def test_shutdown_fences_queued_foreign_thread_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
    listener = server.task_runtime.store.group_deletion_prepare_listener
    assert listener is not None
    producer = threading.Thread(target=listener, daemon=True)
    producer.start()
    producer.join(timeout=2)
    assert not producer.is_alive()
    assert server._task_runtime_group_deletion_wake_scheduled  # noqa: SLF001
    server._shutdown_event.set()  # noqa: SLF001
    await server._stop_task_runtime_group_deletion_reconcile()  # noqa: SLF001
    listener()
    await asyncio.sleep(0.05)
    assert server._task_runtime_group_deletion_task is None  # noqa: SLF001
    assert not server._task_runtime_group_deletion_wake_scheduled  # noqa: SLF001
    assert server.task_runtime._quarantine_retry_timer is None  # noqa: SLF001
    _close_server(server)


@pytest.mark.asyncio
async def test_transient_owner_exception_retries_durable_wake_with_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(TaskRuntimeService, "DELETION_RECONCILE_BATCH_SIZE", 2)
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
    original_reconcile = server._reconcile_task_runtime_group_deletions  # noqa: SLF001
    calls = {"count": 0}

    async def fail_first_owner() -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("synthetic transient deletion owner failure")
        await original_reconcile()

    monkeypatch.setattr(
        server, "_reconcile_task_runtime_group_deletions", fail_first_owner,
    )
    group = server.groups.create_group("Transient owner failure")
    preview = server.groups.delete_preview(group.group_id)
    server.groups.prepare_delete(
        group.group_id, preview.confirmation_token,
        runtime_project_id="project_transient_owner_failure",
    )

    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            transaction = server.groups.delete_transaction(group.group_id)
            if transaction is not None and transaction["stage"] == "committed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("transient owner exception consumed durable wake")
        assert calls["count"] >= 2
    finally:
        await _cancel_deletion_reconcile_task(server)
        _close_server(server)


@pytest.mark.asyncio
async def test_owner_exception_before_later_prepare_keeps_one_backoff_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        KnoweServer, "TASK_RUNTIME_DELETION_ERROR_RETRY_BASE_SECONDS", 0.05,
    )
    monkeypatch.setattr(
        KnoweServer, "TASK_RUNTIME_DELETION_ERROR_RETRY_MAX_SECONDS", 0.1,
    )
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
    original_reconcile = server._reconcile_task_runtime_group_deletions  # noqa: SLF001
    calls = {"count": 0}

    async def fail_before_work_once() -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("synthetic owner failure before durable work")
        await original_reconcile()

    monkeypatch.setattr(
        server, "_reconcile_task_runtime_group_deletions", fail_before_work_once,
    )
    listener = server.groups.deletion_prepare_listener
    assert listener is not None
    listener()
    deadline = time.monotonic() + 2
    while (
        server._task_runtime_group_deletion_error_retry_handle is None  # noqa: SLF001
        and time.monotonic() < deadline
    ):
        await asyncio.sleep(0.005)
    handle = server._task_runtime_group_deletion_error_retry_handle  # noqa: SLF001
    assert handle is not None and calls["count"] == 1
    assert server._task_runtime_group_deletion_task is None  # noqa: SLF001
    loop = asyncio.get_running_loop()
    original_call_soon_threadsafe = loop.call_soon_threadsafe
    wake_calls = {"count": 0}

    def count_wake(callback, *args, **kwargs):
        wake_calls["count"] += 1
        return original_call_soon_threadsafe(callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_soon_threadsafe", count_wake)

    def produce_settle_burst() -> None:
        for _ in range(10_000):
            listener()

    producer = threading.Thread(target=produce_settle_burst, daemon=True)
    producer.start()
    producer.join(timeout=5)
    assert not producer.is_alive()
    assert wake_calls["count"] == 0
    assert server._task_runtime_group_deletion_error_retry_handle is handle  # noqa: SLF001

    group = server.groups.create_group("Work inserted during owner backoff")
    preview = server.groups.delete_preview(group.group_id)
    server.groups.prepare_delete(
        group.group_id, preview.confirmation_token,
        runtime_project_id="project_inserted_during_owner_backoff",
    )
    assert server._task_runtime_group_deletion_error_retry_handle is handle  # noqa: SLF001
    assert server._task_runtime_group_deletion_task is None  # noqa: SLF001

    try:
        while time.monotonic() < deadline:
            transaction = server.groups.delete_transaction(group.group_id)
            if transaction is not None and transaction["stage"] == "committed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("later prepare was lost behind error retry handle")
        assert calls["count"] >= 2
    finally:
        await _cancel_deletion_reconcile_task(server)
        _close_server(server)


@pytest.mark.asyncio
async def test_foreign_prepare_during_failing_owner_survives_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        KnoweServer, "TASK_RUNTIME_DELETION_ERROR_RETRY_BASE_SECONDS", 0.01,
    )
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
    first = server.groups.create_group("Wake before failing owner")
    second = server.groups.create_group("Wake during failing owner")
    first_preview = server.groups.delete_preview(first.group_id)
    second_preview = server.groups.delete_preview(second.group_id)
    original_reconcile = server._reconcile_task_runtime_group_deletions  # noqa: SLF001
    calls = {"count": 0}

    async def insert_foreign_then_fail() -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            await asyncio.to_thread(
                server.groups.prepare_delete,
                second.group_id,
                second_preview.confirmation_token,
                runtime_project_id="project_wake_during_failing_owner",
            )
            raise RuntimeError("synthetic settle-race owner failure")
        await original_reconcile()

    monkeypatch.setattr(
        server, "_reconcile_task_runtime_group_deletions", insert_foreign_then_fail,
    )
    server.groups.prepare_delete(
        first.group_id, first_preview.confirmation_token,
        runtime_project_id="project_wake_before_failing_owner",
    )

    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            transactions = (
                server.groups.delete_transaction(first.group_id),
                server.groups.delete_transaction(second.group_id),
            )
            if all(item is not None and item["stage"] == "committed" for item in transactions):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("wake during failing owner settlement was lost")
        assert calls["count"] >= 2
    finally:
        await _cancel_deletion_reconcile_task(server)
        _close_server(server)


@pytest.mark.asyncio
async def test_permanent_owner_exception_backs_off_and_shutdown_drains_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        KnoweServer, "TASK_RUNTIME_DELETION_ERROR_RETRY_BASE_SECONDS", 0.01,
    )
    monkeypatch.setattr(
        KnoweServer, "TASK_RUNTIME_DELETION_ERROR_RETRY_MAX_SECONDS", 0.04,
    )
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    unhandled_contexts: list[dict[str, object]] = []
    loop.set_exception_handler(
        lambda _loop, context: unhandled_contexts.append(dict(context)),
    )
    calls: list[float] = []

    async def fail_permanently() -> None:
        calls.append(time.monotonic())
        raise RuntimeError("synthetic permanent deletion owner failure")

    monkeypatch.setattr(
        server, "_reconcile_task_runtime_group_deletions", fail_permanently,
    )
    group = server.groups.create_group("Permanent owner failure")
    preview = server.groups.delete_preview(group.group_id)
    server.groups.prepare_delete(
        group.group_id, preview.confirmation_token,
        runtime_project_id="project_permanent_owner_failure",
    )

    try:
        await asyncio.sleep(0.22)
        assert 3 <= len(calls) <= 7
        assert calls[-1] - calls[-2] >= 0.035
        task = server._task_runtime_group_deletion_task  # noqa: SLF001
        handle = server._task_runtime_group_deletion_error_retry_handle  # noqa: SLF001
        assert not (
            task is not None and not task.done()
            and handle is not None and not handle.cancelled()
        )
        state = server.task_runtime.recovery_report[
            "server_deletion_reconciliation"
        ]
        assert state["owner_error"] == "RuntimeError"
        assert state["owner_error_retry_delay_seconds"] == pytest.approx(0.04)
        assert state["has_more"]
        assert not unhandled_contexts
        assert server.task_runtime._quarantine_retry_timer is None  # noqa: SLF001
        server._shutdown_event.set()  # noqa: SLF001
        await server._stop_task_runtime_group_deletion_reconcile()  # noqa: SLF001
        stopped_count = len(calls)
        await asyncio.sleep(0.08)
        assert len(calls) == stopped_count
        assert server._task_runtime_group_deletion_task is None  # noqa: SLF001
        assert server._task_runtime_group_deletion_error_retry_handle is None  # noqa: SLF001
        assert server.groups.deletion_prepare_listener is None
        assert server.task_runtime.store.group_deletion_prepare_listener is None
    finally:
        loop.set_exception_handler(previous_exception_handler)
        await server._stop_task_runtime_group_deletion_reconcile()  # noqa: SLF001
        _close_server(server)


@pytest.mark.asyncio
async def test_stale_failed_owner_never_arms_retry_beside_replacement_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        KnoweServer, "TASK_RUNTIME_DELETION_ERROR_RETRY_BASE_SECONDS", 0.05,
    )
    simultaneous_count = 0
    for iteration in range(10):
        server = KnoweServer(str(tmp_path / f"stale-owner-{iteration}"))
        assert server.groups is not None and server.task_runtime is not None
        await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
        listener = server.groups.deletion_prepare_listener
        assert listener is not None
        original_reconcile = server._reconcile_task_runtime_group_deletions  # noqa: SLF001
        release_replacement = asyncio.Event()
        replacement_started = asyncio.Event()
        calls = {"count": 0}

        async def controlled_owner() -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                # Stable-exit invalidation requests a replacement owner.
                listener()
                return
            if calls["count"] == 2:
                # Queue a real foreign-thread wake immediately before this owner
                # fails; its loop callback may run before this Task's settled callback.
                producer = threading.Thread(target=listener, daemon=True)
                producer.start()
                producer.join(timeout=2)
                assert not producer.is_alive()
                raise RuntimeError("synthetic stale owner settlement failure")
            replacement_started.set()
            await release_replacement.wait()
            await original_reconcile()

        monkeypatch.setattr(
            server, "_task_runtime_group_deletion_reconcile_loop", controlled_owner,
        )
        listener()
        try:
            await asyncio.wait_for(replacement_started.wait(), timeout=1)
            await asyncio.sleep(0)
            task = server._task_runtime_group_deletion_task  # noqa: SLF001
            handle = server._task_runtime_group_deletion_error_retry_handle  # noqa: SLF001
            active_owner = task is not None and not task.done()
            active_handle = handle is not None and not handle.cancelled()
            simultaneous_count += int(active_owner and active_handle)
            assert int(active_owner) + int(active_handle) <= 1
            release_replacement.set()
            deadline = time.monotonic() + 1
            while (
                server._task_runtime_group_deletion_task is not None  # noqa: SLF001
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.08)
            assert calls["count"] == 3
            assert server._task_runtime_group_deletion_error_retry_handle is None  # noqa: SLF001
        finally:
            release_replacement.set()
            await server._stop_task_runtime_group_deletion_reconcile()  # noqa: SLF001
            _close_server(server)
    assert simultaneous_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("burst_size", "replacement_fails"), (
    (100, False),
    (1_000, True),
))
async def test_replacement_owner_interleavings_keep_single_lifecycle_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    burst_size: int, replacement_fails: bool,
) -> None:
    monkeypatch.setattr(
        KnoweServer, "TASK_RUNTIME_DELETION_ERROR_RETRY_BASE_SECONDS", 0.01,
    )
    server = KnoweServer(str(tmp_path))
    assert server.groups is not None and server.task_runtime is not None
    await server._reconcile_task_runtime_group_deletions()  # noqa: SLF001
    listener = server.task_runtime.store.group_deletion_prepare_listener
    assert listener is not None
    original_reconcile = server._reconcile_task_runtime_group_deletions  # noqa: SLF001
    replacement_started = asyncio.Event()
    release_replacement = asyncio.Event()
    calls = {"count": 0}

    def active_resource_count() -> int:
        with server._task_runtime_group_deletion_wake_lock:  # noqa: SLF001
            task = server._task_runtime_group_deletion_task  # noqa: SLF001
            handle = server._task_runtime_group_deletion_error_retry_handle  # noqa: SLF001
            return int(task is not None and not task.done()) + int(
                handle is not None and not handle.cancelled()
            )

    async def controlled_owner() -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            listener()
            return
        if calls["count"] == 2:
            replacement_started.set()
            await release_replacement.wait()
            if replacement_fails:
                raise RuntimeError("synthetic replacement owner failure")
        await original_reconcile()

    monkeypatch.setattr(
        server, "_task_runtime_group_deletion_reconcile_loop", controlled_owner,
    )
    listener()
    try:
        await asyncio.wait_for(replacement_started.wait(), timeout=1)
        assert active_resource_count() == 1

        def produce_interleavings() -> None:
            for _ in range(burst_size):
                listener()

        producer = threading.Thread(target=produce_interleavings, daemon=True)
        producer.start()
        producer.join(timeout=5)
        assert not producer.is_alive()
        assert active_resource_count() == 1
        release_replacement.set()

        expected_calls = 3 if replacement_fails else 2
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            assert active_resource_count() <= 1
            task = server._task_runtime_group_deletion_task  # noqa: SLF001
            handle = server._task_runtime_group_deletion_error_retry_handle  # noqa: SLF001
            if calls["count"] >= expected_calls and task is None and handle is None:
                break
            await asyncio.sleep(0.002)
        else:
            raise AssertionError("replacement lifecycle did not settle")
        await asyncio.sleep(0.05)
        assert calls["count"] == expected_calls
        assert active_resource_count() == 0
    finally:
        release_replacement.set()
        server._shutdown_event.set()  # noqa: SLF001
        await server._stop_task_runtime_group_deletion_reconcile()  # noqa: SLF001
        assert active_resource_count() == 0
        _close_server(server)


@pytest.mark.parametrize("source", ("canonical", "runtime", "both"))
def test_closed_loop_rebind_retires_retry_and_preserves_automatic_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    """A closed loop's 60s retry must never fence recovery on its successor."""
    monkeypatch.setattr(
        KnoweServer, "TASK_RUNTIME_DELETION_ERROR_RETRY_BASE_SECONDS", 60.0,
    )
    monkeypatch.setattr(
        KnoweServer, "TASK_RUNTIME_DELETION_ERROR_RETRY_MAX_SECONDS", 60.0,
    )
    server = KnoweServer(str(tmp_path / source))
    assert server.groups is not None and server.task_runtime is not None
    original_reconcile = server._reconcile_task_runtime_group_deletions  # noqa: SLF001
    first_loop = asyncio.new_event_loop()
    second_loop = asyncio.new_event_loop()

    async def arm_old_generation_retry():
        await original_reconcile()
        old_listener = server.groups.deletion_prepare_listener
        assert old_listener is not None

        async def fail_owner() -> None:
            raise RuntimeError("synthetic loop-1 owner failure")

        monkeypatch.setattr(
            server, "_reconcile_task_runtime_group_deletions", fail_owner,
        )
        old_listener()
        deadline = time.monotonic() + 1
        while (
            server._task_runtime_group_deletion_error_retry_handle is None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.005)
        handle = server._task_runtime_group_deletion_error_retry_handle  # noqa: SLF001
        assert handle is not None and not handle.cancelled()
        return (
            old_listener,
            server._task_runtime_group_deletion_lifecycle_generation,  # noqa: SLF001
            server._task_runtime_group_deletion_error_retry_owner_token,  # noqa: SLF001
            server._task_runtime_group_deletion_error_retry_token,  # noqa: SLF001
            handle,
        )

    old_listener, old_generation, old_owner_token, old_retry_token, old_handle = (
        first_loop.run_until_complete(arm_old_generation_retry())
    )
    assert old_owner_token is not None and old_retry_token is not None
    first_loop.close()
    monkeypatch.setattr(
        server, "_reconcile_task_runtime_group_deletions", original_reconcile,
    )

    async def migrate_and_recover() -> None:
        await original_reconcile()
        assert server._task_runtime_group_deletion_loop is second_loop  # noqa: SLF001
        assert (  # noqa: SLF001
            server._task_runtime_group_deletion_lifecycle_generation
            > old_generation
        )
        assert old_handle.cancelled()
        assert server._task_runtime_group_deletion_error_retry_handle is None  # noqa: SLF001
        assert server._task_runtime_group_deletion_error_retry_token is None  # noqa: SLF001
        assert server._task_runtime_group_deletion_error_retry_owner_token is None  # noqa: SLF001

        # A callback captured from loop 1 is stale after publication of loop 2.
        before_stale_listener = (
            server._task_runtime_group_deletion_invalidation_epoch,  # noqa: SLF001
            server._task_runtime_group_deletion_generation_invalidated,  # noqa: SLF001
            server._task_runtime_group_deletion_wake_scheduled,  # noqa: SLF001
            server._task_runtime_group_deletion_error_attempt,  # noqa: SLF001
        )
        old_listener()
        assert before_stale_listener == (
            server._task_runtime_group_deletion_invalidation_epoch,  # noqa: SLF001
            server._task_runtime_group_deletion_generation_invalidated,  # noqa: SLF001
            server._task_runtime_group_deletion_wake_scheduled,  # noqa: SLF001
            server._task_runtime_group_deletion_error_attempt,  # noqa: SLF001
        )

        group = server.groups.create_group(f"Closed-loop rebind {source}")
        preview = server.groups.delete_preview(group.group_id)
        current_group_listener = server.groups.deletion_prepare_listener
        if source == "runtime":
            server.groups.deletion_prepare_listener = None
        transaction = server.groups.prepare_delete(
            group.group_id,
            preview.confirmation_token,
            runtime_project_id=f"project_closed_loop_rebind_{source}",
        )
        server.groups.deletion_prepare_listener = current_group_listener
        if source in {"runtime", "both"}:
            server.task_runtime.prepare_group_deletion(
                group.group_id, str(transaction["transaction_id"]),
            )

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = server.groups.delete_transaction(group.group_id)
            if current is not None and current["stage"] == "committed":
                break
            task = server._task_runtime_group_deletion_task  # noqa: SLF001
            handle = server._task_runtime_group_deletion_error_retry_handle  # noqa: SLF001
            assert int(task is not None and not task.done()) + int(
                handle is not None and not handle.cancelled()
            ) <= 1
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("loop-2 recovery did not converge automatically")

        # A matching old token callback cannot clear or replace loop-2 state.
        before_stale_timer = (
            server._task_runtime_group_deletion_task,  # noqa: SLF001
            server._task_runtime_group_deletion_owner_token,  # noqa: SLF001
            server._task_runtime_group_deletion_error_retry_handle,  # noqa: SLF001
            server._task_runtime_group_deletion_error_retry_token,  # noqa: SLF001
            server._task_runtime_group_deletion_error_retry_owner_token,  # noqa: SLF001
            server._task_runtime_group_deletion_wake_scheduled,  # noqa: SLF001
        )
        server._fire_task_runtime_group_deletion_error_retry(  # noqa: SLF001
            old_generation, old_owner_token, old_retry_token,
        )
        assert before_stale_timer == (
            server._task_runtime_group_deletion_task,  # noqa: SLF001
            server._task_runtime_group_deletion_owner_token,  # noqa: SLF001
            server._task_runtime_group_deletion_error_retry_handle,  # noqa: SLF001
            server._task_runtime_group_deletion_error_retry_token,  # noqa: SLF001
            server._task_runtime_group_deletion_error_retry_owner_token,  # noqa: SLF001
            server._task_runtime_group_deletion_wake_scheduled,  # noqa: SLF001
        )
        server._shutdown_event.set()  # noqa: SLF001
        await server._stop_task_runtime_group_deletion_reconcile()  # noqa: SLF001

    try:
        second_loop.run_until_complete(migrate_and_recover())
    finally:
        if not second_loop.is_closed():
            second_loop.close()
        _close_server(server)


@pytest.mark.parametrize("burst_size", (100, 1_000))
def test_stopped_loop_pending_owner_is_fenced_across_loop2_and_loop3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, burst_size: int,
) -> None:
    """A stopped-loop owner and its callbacks cannot mutate later generations."""
    server = KnoweServer(str(tmp_path / f"pending-{burst_size}"))
    assert server.groups is not None and server.task_runtime is not None
    original_reconcile = server._reconcile_task_runtime_group_deletions  # noqa: SLF001
    original_owner_loop = server._task_runtime_group_deletion_reconcile_loop  # noqa: SLF001
    first_loop = asyncio.new_event_loop()
    second_loop = asyncio.new_event_loop()
    third_loop = asyncio.new_event_loop()
    first_owner_started: asyncio.Event | None = None

    async def bind_first_loop() -> None:
        nonlocal first_owner_started
        await original_reconcile()
        first_owner_started = asyncio.Event()

    first_loop.run_until_complete(bind_first_loop())
    assert first_owner_started is not None
    old_listener = server.groups.deletion_prepare_listener
    assert old_listener is not None

    async def controlled_owner() -> None:
        if asyncio.get_running_loop() is first_loop:
            assert first_owner_started is not None
            first_owner_started.set()
            await asyncio.Event().wait()
        else:
            await original_owner_loop()

    monkeypatch.setattr(
        server, "_task_runtime_group_deletion_reconcile_loop", controlled_owner,
    )
    old_listener()
    first_loop.run_until_complete(first_owner_started.wait())
    old_task = server._task_runtime_group_deletion_task  # noqa: SLF001
    old_owner_token = server._task_runtime_group_deletion_owner_token  # noqa: SLF001
    old_generation = server._task_runtime_group_deletion_lifecycle_generation  # noqa: SLF001
    assert old_task is not None and not old_task.done() and old_owner_token is not None
    assert not first_loop.is_running() and not first_loop.is_closed()

    async def recover_on_second_loop() -> tuple[object, ...]:
        await original_reconcile()
        assert server._task_runtime_group_deletion_loop is second_loop  # noqa: SLF001
        assert server._task_runtime_group_deletion_task is None  # noqa: SLF001
        assert server._task_runtime_group_deletion_owner_token is None  # noqa: SLF001
        assert old_task.cancelling()
        listener = server.groups.deletion_prepare_listener
        assert listener is not None and listener is not old_listener
        group = server.groups.create_group(f"Stopped loop burst {burst_size}")
        preview = server.groups.delete_preview(group.group_id)
        await asyncio.to_thread(
            server.groups.prepare_delete,
            group.group_id,
            preview.confirmation_token,
            runtime_project_id=f"project_stopped_loop_{burst_size}",
        )

        foreign_group_ids = [
            f"grp_{index + 1:032x}" for index in range(burst_size)
        ]

        def produce_foreign_inserts() -> None:
            for index, group_id in enumerate(foreign_group_ids):
                server.task_runtime.store.prepare_group_deletion(
                    group_id,
                    f"gdt_{index + 1:028x}",
                    group_proof_digest=f"{index + 1:064x}",
                    confirmation_token=f"foreign-confirmation-{index}",
                    runtime_project_id=f"foreign-project-{index}",
                )

        producer = threading.Thread(target=produce_foreign_inserts, daemon=True)
        producer.start()
        producer.join(timeout=15)
        assert not producer.is_alive()

        def pending_foreign_insert_count() -> int:
            with sqlite3.connect(server.task_runtime.store.db.path) as conn:
                row = conn.execute(
                    """SELECT COUNT(*) FROM studycrew_group_deletions
                       WHERE stage='prepared' AND runtime_project_id LIKE 'foreign-project-%'""",
                ).fetchone()
            return 0 if row is None else int(row[0])

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            transaction = server.groups.delete_transaction(group.group_id)
            task = server._task_runtime_group_deletion_task  # noqa: SLF001
            handle = server._task_runtime_group_deletion_error_retry_handle  # noqa: SLF001
            assert int(task is not None and not task.done()) + int(
                handle is not None and not handle.cancelled()
            ) <= 1
            if (
                transaction is not None
                and transaction["stage"] == "committed"
                and await asyncio.to_thread(pending_foreign_insert_count) == 0
            ):
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("stopped-loop foreign inserts did not converge")
        with sqlite3.connect(server.task_runtime.store.db.path) as conn:
            quarantined_count = int(conn.execute(
                """SELECT COUNT(*) FROM studycrew_group_deletions
                   WHERE stage='quarantined'
                     AND runtime_project_id LIKE 'foreign-project-%'""",
            ).fetchone()[0])
        assert quarantined_count == burst_size
        while (
            server._task_runtime_group_deletion_task is not None  # noqa: SLF001
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.005)
        return (
            server._task_runtime_group_deletion_lifecycle_generation,  # noqa: SLF001
            server._task_runtime_group_deletion_task,  # noqa: SLF001
            server._task_runtime_group_deletion_owner_token,  # noqa: SLF001
            server._task_runtime_group_deletion_error_retry_handle,  # noqa: SLF001
            server._task_runtime_group_deletion_error_attempt,  # noqa: SLF001
            server._task_runtime_group_deletion_invalidation_epoch,  # noqa: SLF001
        )

    second_state = second_loop.run_until_complete(recover_on_second_loop())
    # Deliver cancellation and the old done callback on loop 1 after loop 2 has
    # published and settled. The generation fence makes the callback zero-mutation.
    first_loop.run_until_complete(asyncio.sleep(0))
    assert old_task.done() and old_task.cancelled()
    assert second_state == (
        server._task_runtime_group_deletion_lifecycle_generation,  # noqa: SLF001
        server._task_runtime_group_deletion_task,  # noqa: SLF001
        server._task_runtime_group_deletion_owner_token,  # noqa: SLF001
        server._task_runtime_group_deletion_error_retry_handle,  # noqa: SLF001
        server._task_runtime_group_deletion_error_attempt,  # noqa: SLF001
        server._task_runtime_group_deletion_invalidation_epoch,  # noqa: SLF001
    )

    loop2_listener = server.groups.deletion_prepare_listener
    assert loop2_listener is not None

    async def recover_on_third_loop_and_close() -> None:
        await original_reconcile()
        assert server._task_runtime_group_deletion_loop is third_loop  # noqa: SLF001
        assert (  # noqa: SLF001
            server._task_runtime_group_deletion_lifecycle_generation
            > old_generation
        )
        before = (
            server._task_runtime_group_deletion_invalidation_epoch,  # noqa: SLF001
            server._task_runtime_group_deletion_generation_invalidated,  # noqa: SLF001
            server._task_runtime_group_deletion_wake_scheduled,  # noqa: SLF001
        )
        loop2_listener()
        assert before == (
            server._task_runtime_group_deletion_invalidation_epoch,  # noqa: SLF001
            server._task_runtime_group_deletion_generation_invalidated,  # noqa: SLF001
            server._task_runtime_group_deletion_wake_scheduled,  # noqa: SLF001
        )
        server._settle_task_runtime_group_deletion_owner(  # noqa: SLF001
            old_task, first_loop, old_generation, old_owner_token,
        )
        assert before == (
            server._task_runtime_group_deletion_invalidation_epoch,  # noqa: SLF001
            server._task_runtime_group_deletion_generation_invalidated,  # noqa: SLF001
            server._task_runtime_group_deletion_wake_scheduled,  # noqa: SLF001
        )
        server._shutdown_event.set()  # noqa: SLF001
        await server._stop_task_runtime_group_deletion_reconcile()  # noqa: SLF001

    try:
        third_loop.run_until_complete(recover_on_third_loop_and_close())
    finally:
        first_loop.close()
        second_loop.close()
        third_loop.close()
        _close_server(server)


@pytest.mark.parametrize("owner_raises", (False, True))
def test_rebind_clears_cancelled_handle_and_retrieves_done_owner(
    tmp_path: Path, owner_raises: bool,
) -> None:
    """Cancelled handles and done/exception owners are retired without leakage."""
    server = KnoweServer(str(tmp_path / f"done-owner-{owner_raises}"))
    assert server.groups is not None and server.task_runtime is not None
    original_reconcile = server._reconcile_task_runtime_group_deletions  # noqa: SLF001
    first_loop = asyncio.new_event_loop()
    second_loop = asyncio.new_event_loop()
    first_loop.run_until_complete(original_reconcile())
    old_generation = server._task_runtime_group_deletion_lifecycle_generation  # noqa: SLF001

    async def finish_owner() -> None:
        if owner_raises:
            raise RuntimeError("synthetic retired owner exception")

    retired_task = first_loop.create_task(finish_owner())
    first_loop.run_until_complete(asyncio.sleep(0))
    assert retired_task.done()
    cancelled_handle = first_loop.call_later(60, lambda: None)
    cancelled_handle.cancel()
    with server._task_runtime_group_deletion_wake_lock:  # noqa: SLF001
        server._task_runtime_group_deletion_owner_serial += 1  # noqa: SLF001
        owner_token = server._task_runtime_group_deletion_owner_serial  # noqa: SLF001
        server._task_runtime_group_deletion_task = retired_task  # noqa: SLF001
        server._task_runtime_group_deletion_owner_token = owner_token  # noqa: SLF001
        server._task_runtime_group_deletion_error_retry_handle = cancelled_handle  # noqa: SLF001
        server._task_runtime_group_deletion_error_retry_token = 999  # noqa: SLF001
        server._task_runtime_group_deletion_error_retry_owner_token = owner_token  # noqa: SLF001
        server._task_runtime_group_deletion_wake_scheduled = True  # noqa: SLF001

    async def rebind_and_close() -> None:
        await original_reconcile()
        assert server._task_runtime_group_deletion_loop is second_loop  # noqa: SLF001
        assert server._task_runtime_group_deletion_task is None  # noqa: SLF001
        assert server._task_runtime_group_deletion_owner_token is None  # noqa: SLF001
        assert server._task_runtime_group_deletion_error_retry_handle is None  # noqa: SLF001
        assert server._task_runtime_group_deletion_error_retry_token is None  # noqa: SLF001
        before = (
            server._task_runtime_group_deletion_task,  # noqa: SLF001
            server._task_runtime_group_deletion_owner_token,  # noqa: SLF001
            server._task_runtime_group_deletion_error_retry_handle,  # noqa: SLF001
            server._task_runtime_group_deletion_wake_scheduled,  # noqa: SLF001
            server._task_runtime_group_deletion_error_attempt,  # noqa: SLF001
        )
        server._settle_task_runtime_group_deletion_owner(  # noqa: SLF001
            retired_task, first_loop, old_generation, owner_token,
        )
        assert before == (
            server._task_runtime_group_deletion_task,  # noqa: SLF001
            server._task_runtime_group_deletion_owner_token,  # noqa: SLF001
            server._task_runtime_group_deletion_error_retry_handle,  # noqa: SLF001
            server._task_runtime_group_deletion_wake_scheduled,  # noqa: SLF001
            server._task_runtime_group_deletion_error_attempt,  # noqa: SLF001
        )
        server._shutdown_event.set()  # noqa: SLF001
        await server._stop_task_runtime_group_deletion_reconcile()  # noqa: SLF001

    try:
        second_loop.run_until_complete(rebind_and_close())
    finally:
        first_loop.close()
        second_loop.close()
        _close_server(server)


@pytest.mark.parametrize("boundary", ("after_group_prepare", "after_runtime_intent"))
@pytest.mark.parametrize("close_first_loop", (False, True), ids=("stopped", "closed"))
def test_rebind_replaces_lock_held_by_real_stale_reconciliation(
    tmp_path: Path,
    boundary: str,
    close_first_loop: bool,
) -> None:
    """Loop 2 must not inherit a lock held inside loop 1's real delete path."""
    server = KnoweServer(str(tmp_path / f"held-lock-{close_first_loop}-{boundary}"))
    assert server.groups is not None and server.task_runtime is not None
    original_reconcile = server._reconcile_task_runtime_group_deletions  # noqa: SLF001
    first_loop = asyncio.new_event_loop()
    second_loop = asyncio.new_event_loop()
    first_contexts: list[dict[str, object]] = []
    second_contexts: list[dict[str, object]] = []
    first_loop.set_exception_handler(
        lambda _loop, context: first_contexts.append(dict(context)),
    )
    second_loop.set_exception_handler(
        lambda _loop, context: second_contexts.append(dict(context)),
    )
    entered_boundary: asyncio.Event | None = None

    async def bind_first_loop() -> None:
        nonlocal entered_boundary
        await original_reconcile()
        entered_boundary = asyncio.Event()

    first_loop.run_until_complete(bind_first_loop())
    assert entered_boundary is not None

    async def suspend_inside_real_reconciliation(
        current_boundary: str, _group_id: str,
    ) -> None:
        if current_boundary == boundary:
            assert entered_boundary is not None
            entered_boundary.set()
            await asyncio.Event().wait()

    server._task_runtime_delete_fault = suspend_inside_real_reconciliation  # noqa: SLF001
    group = server.groups.create_group(f"Held lock {close_first_loop} {boundary}")
    _run_id, _transaction_id = _prepare_runtime_delete(
        server.task_runtime,
        server.groups,
        group.group_id,
        f"project_held_lock_{close_first_loop}_{boundary}",
    )
    first_loop.run_until_complete(entered_boundary.wait())
    old_task = server._task_runtime_group_deletion_task  # noqa: SLF001
    old_lock = server._task_runtime_group_deletion_lock  # noqa: SLF001
    old_generation = server._task_runtime_group_deletion_lifecycle_generation  # noqa: SLF001
    assert old_task is not None and not old_task.done()
    assert old_lock.locked()
    assert not first_loop.is_running()
    if close_first_loop:
        first_loop.close()

    # Loop 2 must finish through a newly published generation lock without manually
    # reviving loop 1. The stale fault is generation-scoped by the recovery caller.
    server._task_runtime_delete_fault = None  # noqa: SLF001

    async def recover_on_second_loop() -> tuple[object, ...]:
        await asyncio.wait_for(original_reconcile(), timeout=3)
        transaction = server.groups.delete_transaction(group.group_id)
        assert transaction is not None and transaction["stage"] == "committed"
        assert server._task_runtime_group_deletion_loop is second_loop  # noqa: SLF001
        assert server._task_runtime_group_deletion_lock is not old_lock  # noqa: SLF001
        assert (  # noqa: SLF001
            server._task_runtime_group_deletion_lifecycle_generation
            > old_generation
        )
        lifecycle_report = server.task_runtime.recovery_report[
            "server_deletion_reconciliation_lifecycle"
        ]
        assert lifecycle_report["status"] == (
            "detached_closed_loop"
            if close_first_loop
            else "cancel_requested_stopped_loop"
        )
        assert lifecycle_report["retired_generation"] == old_generation
        return (
            server._task_runtime_group_deletion_lifecycle_generation,  # noqa: SLF001
            server._task_runtime_group_deletion_task,  # noqa: SLF001
            server._task_runtime_group_deletion_owner_token,  # noqa: SLF001
            server._task_runtime_group_deletion_error_retry_handle,  # noqa: SLF001
            server._task_runtime_group_deletion_error_attempt,  # noqa: SLF001
            server._task_runtime_group_deletion_invalidation_epoch,  # noqa: SLF001
            server.groups.delete_transaction(group.group_id),
        )

    try:
        second_state = second_loop.run_until_complete(recover_on_second_loop())
        if not close_first_loop:
            # Deliver the already-requested cancellation and late settlement only
            # after loop 2 has committed. It must release only the retired lock.
            first_loop.run_until_complete(asyncio.sleep(0))
            assert old_task.done() and old_task.cancelled()
            assert not old_lock.locked()
            assert second_state == (
                server._task_runtime_group_deletion_lifecycle_generation,  # noqa: SLF001
                server._task_runtime_group_deletion_task,  # noqa: SLF001
                server._task_runtime_group_deletion_owner_token,  # noqa: SLF001
                server._task_runtime_group_deletion_error_retry_handle,  # noqa: SLF001
                server._task_runtime_group_deletion_error_attempt,  # noqa: SLF001
                server._task_runtime_group_deletion_invalidation_epoch,  # noqa: SLF001
                server.groups.delete_transaction(group.group_id),
            )
        server._shutdown_event.set()  # noqa: SLF001
        second_loop.run_until_complete(
            server._stop_task_runtime_group_deletion_reconcile(),  # noqa: SLF001
        )
    finally:
        if not first_loop.is_closed():
            first_loop.close()
        if not second_loop.is_closed():
            second_loop.close()
        del old_task
        gc.collect()
        _close_server(server)
    assert not [
        context for context in first_contexts + second_contexts
        if "never retrieved" in str(context.get("message", "")).lower()
        or "destroyed but it is pending" in str(context.get("message", "")).lower()
    ]


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


def test_ten_thousand_canonical_transactions_keep_page_materialization_bounded(
    tmp_path: Path,
) -> None:
    groups = GroupRepository(tmp_path)
    groups.create_group("Registry initializer")
    registry = json.loads(groups.registry_path.read_text(encoding="utf-8"))
    registry["deletion_transactions"] = [
        {
            "group_id": f"grp_{index:032x}",
            "stage": "prepared",
        }
        for index in range(10_000)
    ]
    groups.registry_path.write_text(
        json.dumps(registry, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    first = groups.pending_delete_transactions_page(limit=2)
    second = groups.pending_delete_transactions_page(
        cursor=str(first["cursor"]), limit=2,
    )
    assert len(first["items"]) == len(second["items"]) == 2
    assert first["limit"] == second["limit"] == 2
    assert first["has_more"] and second["has_more"]
    assert first["cursor"] < second["cursor"]
