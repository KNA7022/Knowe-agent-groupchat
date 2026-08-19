from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.groups import GroupRepository
from backend.task_runtime import RuntimeConflictError, TaskRuntimeService


def _seed_authoritative_state(
    service: TaskRuntimeService, group_id: str, *, suffix: str,
) -> str:
    run = service.submit(
        group_id, title="Deletion authorization probe",
        request_key=f"delete-auth-{suffix}",
    )
    token = f"recovery_{suffix:0<32}"[:41]
    service.store.record_live_recovery(
        run_id=run.run_id, group_id=group_id, through_seq=1,
        reason="delete-auth-base", attempts=4, recovery_token=token,
    )
    service.store.record_live_recovery(
        run_id=run.run_id, group_id=group_id, through_seq=2,
        reason="delete-auth-dirty", attempts=4, recovery_token=token,
    )
    assert service.store.mark_live_recoveries_disconnect_after(
        0, update_revision=0, fence_id=f"fence_{suffix}",
        disconnected_at_ns=1,
    ) >= 1
    return run.run_id


def _authority_snapshot(
    service: TaskRuntimeService, group_id: str, run_id: str,
) -> str:
    run = service.store.get_run(run_id)
    return json.dumps({
        "run": None if run is None else run.to_dict(),
        "counts": service.store.group_content_counts(group_id),
        "base": service.store.live_recovery(group_id),
        "dirty": service.store.live_recovery_dirty_marker(group_id),
        "intents": service.store.group_deletion_intents(),
    }, sort_keys=True, separators=(",", ":"))


def _prepare_canonical_delete(
    groups: GroupRepository, group_id: str, *, runtime_project_id: str,
) -> dict[str, object]:
    token = groups.delete_preview(group_id).confirmation_token
    return groups.prepare_delete(
        group_id, token, runtime_project_id=runtime_project_id,
    )


def test_forged_runtime_deletion_id_preserves_active_group_authority(
    tmp_path: Path,
) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    try:
        group = groups.create_group("Forged deletion")
        run_id = _seed_authoritative_state(service, group.group_id, suffix="forged")
        before = _authority_snapshot(service, group.group_id, run_id)
        assert groups.delete_transaction(group.group_id) is None

        with pytest.raises(RuntimeConflictError, match="canonical Group deletion transaction"):
            service.prepare_group_deletion(
                group.group_id, "gdt_forged_without_user_confirmation",
            )
        with pytest.raises(RuntimeConflictError, match="canonical Group deletion transaction"):
            service.suspend_group_for_deletion(
                group.group_id, "gdt_forged_without_user_confirmation",
                reason="Forged delete",
            )
        for destructive_call in (
            lambda: service.delete_group_data(group.group_id),
            lambda: service.clean_prepared_group_deletion(group.group_id),
            lambda: service.complete_group_deletion(group.group_id),
        ):
            with pytest.raises(RuntimeConflictError, match="deletion intent"):
                destructive_call()

        assert groups.get(group.group_id).group_id == group.group_id
        assert _authority_snapshot(service, group.group_id, run_id) == before
    finally:
        service.close()


def test_other_group_and_stale_transaction_ids_cannot_authorize_deletion(
    tmp_path: Path,
) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    try:
        first = groups.create_group("First")
        second = groups.create_group("Second")
        run_id = _seed_authoritative_state(service, first.group_id, suffix="mismatch")
        second_tx = _prepare_canonical_delete(
            groups, second.group_id, runtime_project_id="project_second",
        )
        before = _authority_snapshot(service, first.group_id, run_id)

        for transaction_id in (
            str(second_tx["transaction_id"]),
            "gdt_stale_or_unknown_transaction",
        ):
            with pytest.raises(RuntimeConflictError, match="canonical Group deletion transaction"):
                service.prepare_group_deletion(first.group_id, transaction_id)
            assert _authority_snapshot(service, first.group_id, run_id) == before
    finally:
        service.close()


def test_canonical_proof_tamper_blocks_every_destructive_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    try:
        group = groups.create_group("Tampered transaction")
        run_id = _seed_authoritative_state(service, group.group_id, suffix="tamper")
        transaction = _prepare_canonical_delete(
            groups, group.group_id, runtime_project_id="project_tamper",
        )
        transaction_id = str(transaction["transaction_id"])
        service.prepare_group_deletion(group.group_id, transaction_id)
        before = _authority_snapshot(service, group.group_id, run_id)
        original = groups.delete_transaction

        def tampered(identity: str):
            result = original(identity)
            if result is not None and result.get("group_id") == group.group_id:
                result["exclusive_source_ids"] = ["src_forged_scope"]
            return result

        monkeypatch.setattr(groups, "delete_transaction", tampered)
        destructive_calls = (
            lambda: service.suspend_group_for_deletion(
                group.group_id, transaction_id, reason="Delete",
            ),
            lambda: service.clean_prepared_group_deletion(group.group_id),
            lambda: service.delete_group_data(group.group_id),
            lambda: service.complete_group_deletion(group.group_id),
        )
        for destructive_call in destructive_calls:
            with pytest.raises(RuntimeConflictError, match="proof|stage"):
                destructive_call()
            assert _authority_snapshot(service, group.group_id, run_id) == before
    finally:
        service.close()


def test_real_canonical_transaction_authorizes_suspend_cleanup_and_commit(
    tmp_path: Path,
) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    try:
        group = groups.create_group("Confirmed transaction")
        run_id = _seed_authoritative_state(service, group.group_id, suffix="confirmed")
        transaction = _prepare_canonical_delete(
            groups, group.group_id, runtime_project_id="project_confirmed",
        )
        transaction_id = str(transaction["transaction_id"])
        assert service.suspend_group_for_deletion(
            group.group_id, transaction_id, reason="Confirmed delete",
        ) == (run_id,)
        service.prepare_group_deletion(group.group_id, transaction_id)
        service.clean_prepared_group_deletion(group.group_id)
        groups.mark_runtime_deleted(transaction_id)
        groups.commit_delete(transaction_id)
        service.complete_group_deletion(group.group_id)

        assert not any(service.store.group_content_counts(group.group_id).values())
        intent = next(
            item for item in service.store.group_deletion_intents()
            if item["group_id"] == group.group_id
        )
        assert intent["stage"] == "complete"
    finally:
        service.close()


def test_startup_quarantines_orphan_runtime_intent_without_deleting_content(
    tmp_path: Path,
) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Orphan intent")
    run_id = _seed_authoritative_state(service, group.group_id, suffix="orphan")
    service.store.prepare_group_deletion(
        group.group_id, "gdt_orphan",
        group_proof_digest="f" * 64,
        confirmation_token="forged-confirmation",
        runtime_project_id="project_orphan",
    )
    before = _authority_snapshot(service, group.group_id, run_id)
    service.close()

    recovered = TaskRuntimeService(tmp_path, groups)
    try:
        report = next(
            item for item in recovered.recovery_report["deletions"]["sample"]
            if item["group_id"] == group.group_id
        )
        assert report["status"] == "quarantined"
        assert recovered.store.get_run(run_id) is not None
        assert recovered.store.live_recovery(group.group_id) is not None
        assert recovered.store.live_recovery_dirty_count(group.group_id) == 1
        intent = next(
            item for item in recovered.store.group_deletion_intents()
            if item["group_id"] == group.group_id
        )
        assert intent["stage"] == "quarantined"
        # Quarantine changes only deletion audit metadata; all authority/content
        # captured before startup remains present.
        after = json.loads(_authority_snapshot(recovered, group.group_id, run_id))
        before_raw = json.loads(before)
        after["intents"] = before_raw["intents"]
        assert after == before_raw
    finally:
        recovered.close()


def test_startup_quarantines_tampered_canonical_transaction(
    tmp_path: Path,
) -> None:
    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    group = groups.create_group("Tampered restart")
    run_id = _seed_authoritative_state(service, group.group_id, suffix="restart-tamper")
    transaction = _prepare_canonical_delete(
        groups, group.group_id, runtime_project_id="project_restart_tamper",
    )
    service.prepare_group_deletion(
        group.group_id, str(transaction["transaction_id"]),
    )
    service.close()

    registry = json.loads(groups.registry_path.read_text(encoding="utf-8"))
    stored = next(
        item for item in registry["deletion_transactions"]
        if item["group_id"] == group.group_id
    )
    stored["source_reference_counts"] = {"src_tampered": 1}
    groups.registry_path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    reopened_groups = GroupRepository(tmp_path)
    recovered = TaskRuntimeService(tmp_path, reopened_groups)
    try:
        report = next(
            item for item in recovered.recovery_report["deletions"]["sample"]
            if item["group_id"] == group.group_id
        )
        assert report["status"] == "quarantined"
        assert recovered.store.get_run(run_id) is not None
        assert recovered.store.live_recovery(group.group_id) is not None
        assert recovered.store.live_recovery_dirty_count(group.group_id) == 1
    finally:
        recovered.close()
