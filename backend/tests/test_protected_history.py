from __future__ import annotations

from dataclasses import replace
import json
import os
import uuid

import pytest

from backend.groups import GroupRepository
from backend.knowe_rag.crypto import MasterKey
from backend.knowe_harness.completion import (SQLiteCompletionStore, CompletionStatus,
    CompletionAwareTaskRunRepository, DecisionEvent, DecisionType)
from backend.knowe_harness.store import TaskEnvelopeStore
from backend.knowe_storage.task_run_repository import SQLiteTaskRunRepository
from backend.protected_storage import ProtectedLineages, ProtectedStorageError, _ExistingRecordsRead
from backend.runtime import TaskEnvelope, TaskRun, TaskState, DeliveryRecord, BudgetSummary
from backend.task_runtime import TaskRuntimeService
from backend.task_runtime.card_state import CardProposal
from backend.task_runtime.contracts import TaskStatus, EvidenceRef

ANSWER = "已完成答案-ANSWER-184731"
REASONING = "内部推理-REASONING-71984"


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "authority"
    groups = GroupRepository(root)
    key = MasterKey(os.urandom(32))
    runtime = TaskRuntimeService(root, groups, card_master_key=key)
    yield root, groups, runtime, key
    runtime.close()


def seed(world, tmp_path, *, completed=True, group=None, worker_status="SUCCEEDED", decision=False):
    _, groups, runtime, _ = world
    group = group or groups.create_group("History", optional_roles=("planning",))
    owner = runtime.submit(group.group_id, title="History", request_key=uuid.uuid4().hex)
    runtime.schedule()
    card = runtime.cards.create(group.group_id, owner.run_id, task_id=owner.task_id,
        proposal=CardProposal("planning", ANSWER), request_key=uuid.uuid4().hex)
    registry = ProtectedLineages(runtime)
    access = registry.register(group.group_id, card.approval_id,
        generation=card.content_generation, digest=card.proposal_digest)
    runtime.cards.resolve(group.group_id, card.approval_id, expected_generation=1,
        expected_digest=card.proposal_digest, approved=True, request_key=uuid.uuid4().hex)
    runtime.schedule()
    identity = access.identity
    envelope = TaskEnvelope(task_id=identity["task_id"], attempt_id=identity["attempt_id"],
        project_id=identity["project_id"], worker_id=identity["worker_id"], goal=ANSWER)
    store = SQLiteCompletionStore(tmp_path / "shared.sqlite3", protected=access)
    run = TaskRun(envelope=envelope, state=TaskState.RUNNING, version=1)
    store.create_started_run(run)
    if worker_status == "RUNNING":
        store.close()
        return registry, access, envelope, run, None, owner, group
    run.state = TaskState.IDLE
    run.set_completion_status("SUCCEEDED")
    run.final_candidate = ANSWER
    run.metadata["reasoning"] = REASONING
    record = DeliveryRecord("p1_delivery_" + uuid.uuid4().hex, "p1_deliverykey_" + uuid.uuid4().hex,
        envelope.task_id, run.run_id, envelope.project_id, TaskState.IDLE, ANSWER, REASONING,
        (), BudgetSummary.from_mapping({}), 1, provenance=run.provenance)
    if worker_status == "WAITING":
        run.waiting_question = ANSWER
        run.set_completion_status("WAITING")
        event = store.commit_run(run, status=CompletionStatus.WAITING)
    else:
        event, _ = store.commit_success(run, record)
    if decision:
        store.record_decision(DecisionEvent("p1_decision_" + uuid.uuid4().hex, DecisionType.DELIVERY_ACCEPTED,
            envelope.project_id, "user", envelope.task_id, envelope.attempt_id,
            event.completion_id, reason=ANSWER))
        store.archive_run_for_resume(run)
    store.close()
    if completed:
        runtime.finalize_trusted_execution(group.group_id, owner.run_id,
            execution_key="history-fixture", outcome=TaskStatus.SUCCEEDED,
            evidence=(EvidenceRef("completion", event.completion_id, "1", event.completion_id),))
    return registry, access, envelope, run, event, owner, group


def test_manual_history_after_rename_and_reopen(world, tmp_path):
    registry, access, envelope, run, event, owner, group = seed(world, tmp_path)
    world[1].rename(group.group_id, "Renamed")
    with pytest.raises(ProtectedStorageError):
        access.identity
    reader = registry.completed_history(group.group_id, envelope.task_id,
        completion_path=tmp_path / "shared.sqlite3")
    with SQLiteCompletionStore(tmp_path / "shared.sqlite3", protected=reader) as reopened:
        assert reopened.task_result(completion_id=event.completion_id).summary == ANSWER


def test_selected_lists_do_not_decode_other_lineages(world, tmp_path):
    one = seed(world, tmp_path, completed=False)
    two = seed(world, tmp_path, completed=False)
    with SQLiteCompletionStore(tmp_path / "shared.sqlite3", protected=one[1]) as store:
        assert [event.task_id for event in store.list_active()] == [one[2].task_id]
        assert all(row.completion_id == one[4].completion_id for row in store.pending_outbox())


def snapshot(runtime, store, tmp_path):
    with runtime.store.db.transaction(immediate=False) as conn:
        authority = tuple(conn.iterdump())
    with store.db.transaction(immediate=False) as conn:
        child = tuple(conn.iterdump())
    files = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*.json")}
    files.update({str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*.sqlite3*") if not p.name.endswith("-shm")})
    return authority, child, files


@pytest.mark.parametrize("change", ["rename", "archive", "restore", "role", "course", "source"])
def test_completed_history_keeps_original_binding_through_group_changes(world, tmp_path, change):
    registry, access, envelope, run, event, owner, group = seed(world, tmp_path)
    root, groups, runtime, key = world
    original = access.identity
    if change == "rename":
        groups.rename(group.group_id, "New name")
    elif change in {"archive", "restore"}:
        groups.archive(group.group_id)
        if change == "restore":
            groups.restore(group.group_id)
    elif change == "role":
        groups.update_roster(group.group_id, [a for a in group.roster if a.role != "planning"])
    elif change == "course":
        groups.bind_course(group.group_id, "course-new")
    else:
        groups.link_source(group.group_id)
    with pytest.raises(ProtectedStorageError):
        access.identity
    runtime.close()
    reopened_runtime = TaskRuntimeService(root, GroupRepository(root), card_master_key=key)
    try:
        registry = ProtectedLineages(reopened_runtime)
        reader = registry.completed_history(group.group_id, envelope.task_id,
            completion_path=tmp_path / "shared.sqlite3")
        assert reader.identity == original
        with SQLiteCompletionStore(tmp_path / "shared.sqlite3", protected=reader) as store:
            before = snapshot(reopened_runtime, store, tmp_path)
            assert store.get(event.completion_id).completion_id == event.completion_id
            assert store.task_result(completion_id=event.completion_id).summary == ANSWER
            assert snapshot(reopened_runtime, store, tmp_path) == before
            assert ANSWER not in "\n".join(before[1])
            assert REASONING not in "\n".join(before[1])
    finally:
        reopened_runtime.close()


@pytest.mark.parametrize("mode", ["completed", "paused"])
def test_actual_readonly_repository_mutations_have_zero_effects(world, tmp_path, mode):
    registry, access, envelope, run, event, owner, group = seed(world, tmp_path, completed=False)
    with SQLiteCompletionStore(tmp_path / "shared.sqlite3", protected=access) as store:
        entry = store.pending_outbox()[0]
        _, ref = TaskEnvelopeStore(tmp_path / "work", protected=access).commit(envelope)
    runtime = world[2]
    if mode == "completed":
        runtime.finalize_trusted_execution(group.group_id, owner.run_id, execution_key="done",
            outcome=TaskStatus.PARTIAL, evidence=(EvidenceRef("completion", event.completion_id, "1", event.completion_id),))
        reader = registry.completed_history(group.group_id, envelope.task_id,
            completion_path=tmp_path / "shared.sqlite3")
    else:
        runtime.pause(group.group_id, owner.run_id, request_key="pause")
        reader = registry.paused_history(group.group_id, envelope.task_id)
    with SQLiteCompletionStore(tmp_path / "shared.sqlite3", protected=reader) as store:
        repo = CompletionAwareTaskRunRepository(store.db, store)
        files = TaskEnvelopeStore(tmp_path / "work", protected=reader)
        codec = store.protection
        before = snapshot(runtime, store, tmp_path)
        assert files.get(ref) == envelope
        assert repo.get(envelope.task_id).final_candidate == ANSWER
        assert store.orphan_summary()["consistent"]
        mutations = [
            lambda: files.commit(envelope), lambda: files.purge_protected(),
            lambda: store.create_started_run(run), lambda: store.mark_started(run),
            lambda: store.record_progress(run), lambda: store.commit_run(run),
            lambda: store.commit_success(run, None),
            lambda: store.transition_completion(event, CompletionStatus.CANCELLED),
            lambda: store.transition_with_decision(event, CompletionStatus.CANCELLED, None),
            lambda: store.claim_outbox(entry.outbox_id),
            lambda: store.acknowledge_outbox(entry.outbox_id),
            lambda: store.fail_outbox(entry.outbox_id, "fixed"),
            lambda: store.resume_wait("p1_wait_" + "a" * 32),
            lambda: store.resolve_wait("p1_wait_" + "a" * 32),
            lambda: store.record_decision(None), lambda: store.archive_run_for_resume(run),
            lambda: store.reconcile_terminal_runs(), lambda: store.purge_protected(),
            lambda: repo.create(run), lambda: repo.save(run, expected_version=1),
            lambda: repo.delegate.create(run), lambda: repo.delegate.save(run, expected_version=1),
            lambda: codec._claim("task_runs", envelope.task_id, create=True),
            lambda: codec.seal("task_runs", envelope.task_id, {}),
            lambda: codec._binding(write=True), lambda: codec.bind_worker_run(run.run_id),
            lambda: codec.validate_envelope(envelope.to_dict()),
            lambda: store.assert_worker_available(envelope.worker_id),
        ]
        for mutation in mutations:
            with pytest.raises(ProtectedStorageError, match="read-only"):
                mutation()
            assert snapshot(runtime, store, tmp_path) == before
        with pytest.raises(ProtectedStorageError):
            files.get("foreign-task", "foreign-attempt")
        for constructor in (SQLiteCompletionStore, SQLiteTaskRunRepository):
            with pytest.raises(ProtectedStorageError):
                constructor(tmp_path / "missing.sqlite3", protected=reader)
            assert not (tmp_path / "missing.sqlite3").exists()
        with pytest.raises(ProtectedStorageError):
            TaskEnvelopeStore(tmp_path / "missing-work", protected=reader)
        assert not (tmp_path / "missing-work").exists()


def test_paused_history_after_actual_restart_remains_same_scope(world, tmp_path):
    registry, access, envelope, run, event, owner, group = seed(world, tmp_path, completed=False)
    root, groups, runtime, key = world
    runtime.close()
    runtime = TaskRuntimeService(root, GroupRepository(root), card_master_key=key)
    try:
        assert runtime.store.get_run(owner.run_id).status is TaskStatus.PAUSED
        registry = ProtectedLineages(runtime)
        with pytest.raises(ProtectedStorageError):
            registry.access(group.group_id, envelope.task_id)
        reader = registry.paused_history(group.group_id, envelope.task_id)
        with SQLiteCompletionStore(tmp_path / "shared.sqlite3", protected=reader) as store:
            before = snapshot(runtime, store, tmp_path)
            assert store.pending_outbox(completion_id=event.completion_id)
            assert snapshot(runtime, store, tmp_path) == before
            runtime.groups.rename(group.group_id, "Changed paused scope")
            with pytest.raises(ProtectedStorageError):
                store.get(event.completion_id)
    finally:
        runtime.close()


@pytest.mark.parametrize("denial", ["foreign", "revoked", "missing", "pending_delete", "runtime_delete", "quarantine", "wrong_key", "missing_key", "mapping", "unknown_schema"])
def test_history_denials_are_checked_on_every_read(world, tmp_path, denial):
    registry, access, envelope, run, event, owner, group = seed(world, tmp_path)
    reader = registry.completed_history(group.group_id, envelope.task_id,
        completion_path=tmp_path / "shared.sqlite3")
    store = SQLiteCompletionStore(tmp_path / "shared.sqlite3", protected=reader)
    runtime = world[2]
    try:
        if denial == "foreign":
            other = world[1].create_group("Other")
            with pytest.raises(ProtectedStorageError):
                registry.completed_history(other.group_id, envelope.task_id, completion_path=tmp_path / "shared.sqlite3")
            return
        if denial == "revoked":
            registry.revoke(group.group_id, envelope.task_id)
        elif denial == "missing":
            payload = world[1]._load_payload()
            payload["groups"] = []
            world[1]._save_payload(payload)
        elif denial == "pending_delete":
            preview = world[1].delete_preview(group.group_id)
            world[1].prepare_delete(group.group_id, preview.confirmation_token)
        elif denial == "runtime_delete":
            preview = world[1].delete_preview(group.group_id)
            transaction = world[1].prepare_delete(group.group_id, preview.confirmation_token)
            runtime.prepare_group_deletion(group.group_id, transaction["transaction_id"])
            # Group JSON rollback cannot remove the durable runtime fence.
            payload = world[1]._load_payload()
            payload["deletion_transactions"] = []
            world[1]._save_payload(payload)
        elif denial == "quarantine":
            runtime._deferred_quarantines[group.group_id] = "test"
        elif denial in {"wrong_key", "missing_key"}:
            runtime.store.card_states._key = MasterKey(os.urandom(32)) if denial == "wrong_key" else None
        elif denial == "mapping":
            with registry.db.transaction() as conn:
                conn.execute("UPDATE studycrew_protected_lineages SET payload=? WHERE task_id=?",
                             (b"x" * 40, envelope.task_id))
        else:
            with registry.db.transaction() as conn:
                binding = registry._read(conn, envelope.task_id, group.group_id)
                binding["source_provenance"] = "unrecognized"
                meta = registry.crypto.verify(conn)
                from backend.protected_storage import bounded_json
                ciphertext = registry.crypto._key.seal(bounded_json(binding),
                    aad=registry._aad(meta, envelope.task_id, group.group_id, "active"))
                conn.execute("UPDATE studycrew_protected_lineages SET payload=? WHERE task_id=?",
                             (ciphertext, envelope.task_id))
        with pytest.raises(ProtectedStorageError):
            store.task_result(completion_id=event.completion_id)
    finally:
        store.close()


@pytest.mark.parametrize("status", [TaskStatus.CANCELLED, TaskStatus.FAILED])
def test_noneligible_owner_never_gets_completed_history(world, tmp_path, status):
    registry, access, envelope, run, event, owner, group = seed(world, tmp_path, completed=False)
    world[2].finalize_trusted_execution(group.group_id, owner.run_id,
        execution_key="failed", outcome=status)
    with pytest.raises(ProtectedStorageError):
        registry.completed_history(group.group_id, envelope.task_id, completion_path=tmp_path / "shared.sqlite3")


def test_history_requires_actual_completed_worker_record_and_known_mode(world, tmp_path):
    registry, access, envelope, run, event, owner, group = seed(world, tmp_path)
    with pytest.raises(ProtectedStorageError):
        _ExistingRecordsRead(registry, group.group_id, envelope.task_id, mode="resume")
    with pytest.raises(ProtectedStorageError):
        _ExistingRecordsRead(registry, group.group_id, envelope.task_id, mode="completed")
    import sqlite3
    with sqlite3.connect(tmp_path / "shared.sqlite3") as conn:
        conn.execute("DELETE FROM completion_events_v2")
    with pytest.raises(ProtectedStorageError):
        registry.completed_history(group.group_id, envelope.task_id, completion_path=tmp_path / "shared.sqlite3")


def test_mixed_selected_views_recovery_and_foreign_keys(world, tmp_path):
    one = seed(world, tmp_path, decision=True)
    two = seed(world, tmp_path, completed=False, group=one[6], decision=True)
    path = tmp_path / "shared.sqlite3"
    legacy_run = TaskRun(envelope=TaskEnvelope(task_id="legacy-task", attempt_id="legacy-attempt", worker_id="legacy-worker",
        project_id=one[2].project_id, goal="Legacy public instruction"), state=TaskState.IDLE, version=1)
    with SQLiteCompletionStore(path) as legacy:
        legacy_event = legacy.commit_run(legacy_run, status=CompletionStatus.FAILED)
    for current in (one, two):
        foreign = two if current is one else one
        with SQLiteCompletionStore(path, protected=current[1]) as store:
            assert {row.task_id for row in store.list_active()} == {current[2].task_id}
            assert {row.task_id for row in store.list_decisions()} == {current[2].task_id}
            assert store.active_attempts() == ()
            assert store.reconcile_terminal_runs()["scanned"] == 1
            assert store.orphan_summary()["active_completions"] == 1
            assert store.history_for_task(current[2].task_id)
            assert store.pending_outbox(completion_id=current[4].completion_id)
            repo = CompletionAwareTaskRunRepository(path, store)
            assert {row.envelope.task_id for row in repo.list_by_state("IDLE")} == {current[2].task_id}
            for operation in (
                lambda: store.get(foreign[4].completion_id),
                lambda: store.pending_outbox(completion_id=foreign[4].completion_id),
                lambda: store.list_decisions(completion_id=foreign[4].completion_id),
                lambda: store.active_wait_for_completion(foreign[4].completion_id),
                lambda: store.task_result(completion_id=foreign[4].completion_id),
                lambda: store.get(legacy_event.completion_id),
                lambda: repo.get(foreign[2].task_id),
                lambda: repo.get_by_run_id(foreign[3].run_id),
                lambda: repo.get(legacy_run.envelope.task_id),
                lambda: store.history_for_task(foreign[2].task_id),
                lambda: store.list_decisions(task_id=foreign[2].task_id),
            ):
                with pytest.raises(ProtectedStorageError):
                    operation()
    with SQLiteCompletionStore(path, legacy_lineages=one[0]) as legacy:
        assert [row.task_id for row in legacy.list_active()] == [legacy_run.envelope.task_id]
        assert legacy.list_decisions() == ()
        assert legacy.active_attempts() == ()
        assert legacy.reconcile_terminal_runs()["scanned"] == 1
        assert legacy.orphan_summary()["active_completions"] == 1
        assert all(row.completion_id == legacy_event.completion_id for row in legacy.pending_outbox())
        wrapper = CompletionAwareTaskRunRepository(path, legacy)
        assert [r.envelope.task_id for r in wrapper.list_by_state("IDLE")] == [legacy_run.envelope.task_id]
        with pytest.raises(ProtectedStorageError):
            legacy.get(one[4].completion_id)
        dump = snapshot(world[2], legacy, tmp_path)[1]
        assert ANSWER not in "\n".join(dump)
        assert REASONING not in "\n".join(dump)


@pytest.mark.parametrize("table", ["completion_events_v2", "completion_outbox_v2", "task_runs", "task_results_v1"])
def test_corrupt_selected_rows_fail_while_other_views_still_work(world, tmp_path, table):
    one = seed(world, tmp_path, completed=False)
    two = seed(world, tmp_path, completed=False)
    with SQLiteCompletionStore(tmp_path / "shared.sqlite3", protected=one[1]) as store:
        with store.db.transaction() as conn:
            if table == "completion_outbox_v2":
                conn.execute(f"UPDATE {table} SET payload_json='p1:broken' WHERE completion_id=?", (one[4].completion_id,))
            else:
                conn.execute(f"UPDATE {table} SET payload_json='p1:broken' WHERE task_id=?", (one[2].task_id,))
        operation = {
            "completion_events_v2": store.list_active,
            "completion_outbox_v2": store.pending_outbox,
            "task_runs": store.reconcile_terminal_runs,
            "task_results_v1": lambda: store.task_result(completion_id=one[4].completion_id),
        }[table]
        with pytest.raises(ProtectedStorageError):
            operation()
    with SQLiteCompletionStore(tmp_path / "shared.sqlite3", protected=two[1]) as store:
        assert store.list_active()[0].task_id == two[2].task_id
        assert store.pending_outbox()
        assert store.orphan_summary()["consistent"]


def test_selected_running_attempts_are_filtered_before_decode(world, tmp_path):
    one = seed(world, tmp_path, completed=False, worker_status="RUNNING")
    two = seed(world, tmp_path, completed=False, worker_status="RUNNING")
    for current in (one, two):
        with SQLiteCompletionStore(tmp_path / "shared.sqlite3", protected=current[1]) as store:
            assert [row.task_id for row in store.active_attempts()] == [current[2].task_id]
            assert store.worker_idle(current[2].worker_id) is False
            repo = CompletionAwareTaskRunRepository(store.db, store)
            assert [row.envelope.task_id for row in repo.list_by_state("RUNNING")] == [current[2].task_id]


def test_completed_owner_cannot_promote_waiting_worker_history(world, tmp_path):
    registry, access, envelope, run, event, owner, group = seed(world, tmp_path, worker_status="WAITING")
    with pytest.raises(ProtectedStorageError):
        registry.completed_history(group.group_id, envelope.task_id, completion_path=tmp_path / "shared.sqlite3")


def test_history_issuance_and_queries_preserve_database_and_file_bytes(world, tmp_path):
    registry, access, envelope, run, event, owner, group = seed(world, tmp_path)
    path = tmp_path / "shared.sqlite3"
    with SQLiteCompletionStore(path) as baseline:
        before = snapshot(world[2], baseline, tmp_path)
        reader = registry.completed_history(group.group_id, envelope.task_id, completion_path=path)
        with SQLiteCompletionStore(path, protected=reader) as store:
            repo = CompletionAwareTaskRunRepository(store.db, store)
            assert store.task_result(completion_id=event.completion_id).summary == ANSWER
            assert repo.get(envelope.task_id).final_candidate == ANSWER
            assert snapshot(world[2], store, tmp_path) == before
        assert snapshot(world[2], baseline, tmp_path) == before
        for raw in before[2].values():
            assert ANSWER.encode() not in raw
            assert REASONING.encode() not in raw


def test_legacy_classifier_is_bounded_authenticated_and_never_silently_truncates(world, tmp_path):
    one = seed(world, tmp_path, completed=False)
    two = seed(world, tmp_path, completed=False)
    with pytest.raises(ProtectedStorageError, match="capacity"):
        one[0].classified_task_ids(limit=1)
    one[0].revoke(one[6].group_id, one[2].task_id)
    assert set(one[0].classified_task_ids()) == {one[2].task_id, two[2].task_id}
    with SQLiteCompletionStore(tmp_path / "shared.sqlite3", legacy_lineages=one[0]) as legacy:
        assert legacy.list_active() == ()
        with one[0].db.transaction() as conn:
            conn.execute("UPDATE studycrew_protected_lineages SET receipt='corrupt' WHERE task_id=?", (one[2].task_id,))
        with pytest.raises(ProtectedStorageError):
            legacy.list_active()


@pytest.mark.parametrize("substitution", ["payload", "field", "missing_claim", "wrong_store"])
def test_completed_read_authenticates_original_existing_claims(world, tmp_path, substitution):
    one = seed(world, tmp_path)
    two = seed(world, tmp_path)
    path = tmp_path / "shared.sqlite3"
    reader = one[0].completed_history(one[6].group_id, one[2].task_id, completion_path=path)
    with SQLiteCompletionStore(path, protected=reader) as store:
        with store.db.transaction(immediate=False) as conn:
            row = conn.execute("SELECT * FROM completion_events_v2 WHERE completion_id=?", (one[4].completion_id,)).fetchone()
            foreign = conn.execute("SELECT * FROM completion_events_v2 WHERE completion_id=?", (two[4].completion_id,)).fetchone()
        if substitution == "payload":
            with store.db.transaction() as conn:
                conn.execute("UPDATE completion_events_v2 SET payload_json=? WHERE completion_id=?", (foreign["payload_json"], one[4].completion_id))
            operation = lambda: store.get(one[4].completion_id)
        elif substitution == "field":
            operation = lambda: store.protection.open("completion_events_v2", one[4].completion_id, row["payload_json"], field="answer")
        elif substitution == "missing_claim":
            with one[0].db.transaction() as conn:
                conn.execute("DELETE FROM studycrew_protected_records WHERE record_id=?", (one[4].completion_id,))
            operation = lambda: store.get(one[4].completion_id)
        else:
            operation = lambda: reader.for_store(tmp_path / "other.sqlite3").open("completion_events_v2", one[4].completion_id, row["payload_json"])
        before = snapshot(world[2], store, tmp_path)
        with pytest.raises(ProtectedStorageError):
            operation()
        assert snapshot(world[2], store, tmp_path) == before


def test_actual_completed_deletion_remains_denied_after_group_json_restore(world, tmp_path):
    registry, access, envelope, run, event, owner, group = seed(world, tmp_path)
    root, groups, runtime, key = world
    reader = registry.completed_history(group.group_id, envelope.task_id,
        completion_path=tmp_path / "shared.sqlite3")
    prior = groups._load_payload()
    preview = groups.delete_preview(group.group_id)
    transaction = groups.prepare_delete(group.group_id, preview.confirmation_token)
    transaction_id = transaction["transaction_id"]
    runtime.prepare_group_deletion(group.group_id, transaction_id)
    runtime.clean_prepared_group_deletion(group.group_id)
    groups.mark_runtime_deleted(transaction_id)
    groups.commit_delete(transaction_id)
    runtime.complete_group_deletion(group.group_id)
    with pytest.raises(ProtectedStorageError):
        reader.identity
    # Restore the old JSON intentionally to exercise the independent runtime
    # tombstone, without claiming a supported Group undelete operation.
    groups._save_payload(prior)
    with pytest.raises(ProtectedStorageError):
        reader.identity
    runtime.close()
    runtime = TaskRuntimeService(root, GroupRepository(root), card_master_key=key)
    try:
        with pytest.raises(ProtectedStorageError):
            ProtectedLineages(runtime).completed_history(group.group_id, envelope.task_id,
                completion_path=tmp_path / "shared.sqlite3")
    finally:
        runtime.close()


def test_history_rejects_nonterminal_worker_snapshot_beside_terminal_event(world, tmp_path):
    registry, access, envelope, run, event, owner, group = seed(world, tmp_path, completed=False)
    repo = SQLiteTaskRunRepository(tmp_path / "shared.sqlite3", protected=access)
    try:
        stored = repo.get(envelope.task_id)
        stored.version += 1
        stored.set_completion_status("WAITING")
        repo.save(stored, expected_version=stored.version - 1)
    finally:
        repo.db.close()
    world[2].finalize_trusted_execution(group.group_id, owner.run_id, execution_key="done",
        outcome=TaskStatus.SUCCEEDED, evidence=(EvidenceRef("completion", event.completion_id, "1", event.completion_id),))
    with pytest.raises(ProtectedStorageError):
        registry.completed_history(group.group_id, envelope.task_id, completion_path=tmp_path / "shared.sqlite3")
