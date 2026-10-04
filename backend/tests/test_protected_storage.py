from __future__ import annotations

from dataclasses import replace
import json
import os
import traceback
from pathlib import Path

import pytest

from backend.runtime import ArtifactRecord, BudgetSummary, DeliveryRecord, TaskEnvelope, TaskRun, TaskState
from backend.knowe_harness.completion import (
    SQLiteCompletionStore, CompletionStatus, CompletionAwareTaskRunRepository, DecisionEvent, DecisionType,
)
from backend.knowe_storage.task_run_repository import SQLiteTaskRunRepository
from backend.groups import GroupRepository
from backend.knowe_rag.crypto import MasterKey
from backend.task_runtime import TaskRuntimeService
from backend.task_runtime.card_state import CardProposal
from backend.knowe_harness.store import TaskEnvelopeStore
from backend.protected_storage import ProtectedLineages, ProtectedStorageError
from backend.task_runtime.contracts import TaskStatus

MARKER = "私密指令-ASCII-58192"


class PrivateFixture:
    def __init__(self, *values):
        self.values = values

    def __iter__(self):
        return iter(self.values)


@pytest.fixture
def protected(tmp_path):
    groups = GroupRepository(tmp_path / "authority")
    group = groups.create_group("Storage", optional_roles=("planning",))
    key = MasterKey(os.urandom(32))
    runtime = TaskRuntimeService(tmp_path / "authority", groups, card_master_key=key)
    owner = runtime.submit(group.group_id, title="Storage", request_key="owner")
    runtime.schedule()
    card = runtime.cards.create(group.group_id, owner.run_id, task_id=owner.task_id,
        proposal=CardProposal("planning", MARKER), request_key="card")
    registry = ProtectedLineages(runtime)
    access = registry.register(group.group_id, card.approval_id,
        generation=card.content_generation, digest=card.proposal_digest)
    identity = access.identity
    envelope = TaskEnvelope(task_id=identity["task_id"], attempt_id=identity["attempt_id"],
        project_id=identity["project_id"], worker_id=identity["worker_id"], goal=MARKER, title=MARKER)
    try:
        yield PrivateFixture(access, envelope, runtime, key)
    finally:
        runtime.close()


def test_protected_envelope_roundtrip_and_recommit(protected, tmp_path):
    access, envelope, runtime, key = protected
    store = TaskEnvelopeStore(tmp_path / "workspace", protected=access)
    committed, ref = store.commit(envelope)
    assert store.get(ref) == envelope
    assert store.commit(envelope) == (committed, ref)
    assert MARKER.encode() not in store.envelope_path(envelope.task_id, envelope.attempt_id).read_bytes()
    with pytest.raises(ProtectedStorageError):
        TaskEnvelopeStore(tmp_path / "workspace").get(ref)
    with pytest.raises(ProtectedStorageError):
        store.commit(replace(envelope, metadata={"attachments": [MARKER]}))


def test_protected_wait_outbox_and_reopen(protected, tmp_path):
    access, envelope, runtime, key = protected
    path = tmp_path / "protected.sqlite3"
    store = SQLiteCompletionStore(path, protected=access)
    try:
        run = TaskRun(envelope=envelope, state=TaskState.RUNNING, version=1,
            waiting_question=MARKER, dependency=MARKER, terminal_reason=MARKER,
            metadata={"reasoning": MARKER})
        store.create_started_run(run)
        event = store.commit_run(run, status=CompletionStatus.WAITING)
        assert store.get(event.completion_id).question == MARKER
        assert store.active_wait_for_task(envelope.task_id).question == MARKER
        entry = store.pending_outbox()[0]
        store.claim_outbox(entry.outbox_id)
        store.fail_outbox(entry.outbox_id, MARKER)
        assert store.pending_outbox()[0].last_error == MARKER
        with store.db.transaction(immediate=False) as conn:
            raw = "\n".join(conn.iterdump())
        assert MARKER not in raw
    finally:
        store.close()
    with SQLiteCompletionStore(path, protected=access) as reopened:
        assert reopened.get(event.completion_id).question == MARKER


def final_record(run):
    return DeliveryRecord("p1_delivery_" + "a" * 32, "p1_deliverykey_" + "b" * 32,
        run.envelope.task_id, run.run_id, run.envelope.project_id, TaskState.IDLE,
        MARKER, MARKER, (), BudgetSummary.from_mapping({}), 1, provenance=run.provenance)


def assert_sealed(store):
    with store.db.transaction(immediate=False) as conn:
        raw = "\n".join(conn.iterdump())
    assert MARKER not in raw
    database = Path(store.db.path)
    for path in database.parent.glob(database.name + "*"):
        assert MARKER.encode() not in path.read_bytes()


def test_success_repository_history_receipts_and_purge(protected, tmp_path):
    access, envelope, runtime, key = protected
    path = tmp_path / "success-protected.sqlite3"
    store = SQLiteCompletionStore(path, protected=access)
    repo = CompletionAwareTaskRunRepository(path, store)
    envelopes = TaskEnvelopeStore(tmp_path / "work", protected=access)
    envelopes.commit(envelope)
    try:
        run = TaskRun(envelope=envelope, state=TaskState.RUNNING, version=1)
        repo.create(run)
        assert repo.get(run.envelope.task_id).run_id == run.run_id
        run.version = 2
        run.terminal_reason = MARKER
        repo.save(run, expected_version=1)
        assert repo.get_by_run_id(run.run_id).terminal_reason == MARKER
        assert repo.list_by_state("RUNNING")[0].version == 2
        assert store.active_attempts()[0].reason == MARKER
        history = store.archive_run_for_resume(run, reason=MARKER)
        assert store.archive_run_for_resume(run, reason=MARKER) == history
        assert store.history_for_task(envelope.task_id)[0]["reason"] == MARKER
        run.state = TaskState.IDLE
        run.set_completion_status("SUCCEEDED")
        run.final_candidate = MARKER
        run.metadata["reasoning"] = MARKER
        record = final_record(run)
        event, stored = store.commit_success(run, record)
        assert stored.text == MARKER
        assert store.commit_success(run, record)[0].completion_id == event.completion_id
        assert store.task_result(task_id=envelope.task_id).summary == MARKER
        assert store.list_active()[0].metadata["reasoning"] == MARKER
        assert store.journal_for(envelope.task_id)
        decision = DecisionEvent("p1_decision_" + "c" * 32, DecisionType.DELIVERY_ACCEPTED,
            envelope.project_id, MARKER, envelope.task_id, envelope.attempt_id, event.completion_id,
            reason=MARKER, payload={"note": MARKER})
        assert store.record_decision(decision).reason == MARKER
        assert store.list_decisions(task_id=envelope.task_id)[0].actor == MARKER
        assert store.decisions_for_task(envelope.task_id)[0].payload["note"] == MARKER
        entry = store.pending_outbox()[0]
        store.claim_outbox(entry.outbox_id)
        store.acknowledge_outbox(entry.outbox_id, effect_ref=MARKER, effect_payload={"text": MARKER})
        receipt = store.projection_receipt(entry.outbox_id)
        assert receipt["effect_ref"] == MARKER
        assert receipt["payload"] == {"text": MARKER}
        assert entry.outbox_id not in {row.outbox_id for row in store.pending_outbox()}
        assert store.reconcile_terminal_runs()["already_consistent"]
        assert store.orphan_summary()["consistent"]
        assert_sealed(store)

        store.close()
        store = SQLiteCompletionStore(path, protected=access)
        assert store.projection_receipt(entry.outbox_id) == receipt
        assert store.get(event.completion_id).metadata["reasoning"] == MARKER
        assert store.purge_protected()["state"] == "purged"
        assert store.purge_protected()["state"] == "purged"
        assert envelopes.purge_protected()["state"] == "purged"
        with pytest.raises(ProtectedStorageError):
            store.get(event.completion_id)
        with pytest.raises(ProtectedStorageError):
            envelopes.commit(envelope)
        with store.db.transaction(immediate=False) as conn:
            assert conn.execute("SELECT COUNT(*) FROM completion_events_v2").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize("status", [CompletionStatus.FAILED, CompletionStatus.CANCELLED, CompletionStatus.BLOCKED])
def test_other_results_keep_reasoning_encrypted(protected, tmp_path, status):
    access, envelope, runtime, key = protected
    with SQLiteCompletionStore(tmp_path / "results.sqlite3", protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.RUNNING,
            final_candidate=MARKER, terminal_reason=MARKER, metadata={"reasoning": MARKER})
        event = store.commit_run(run, status=status)
        assert store.get(event.completion_id).terminal_reason == MARKER
        assert store.task_result(completion_id=event.completion_id).summary == MARKER
        assert store.get(event.completion_id).metadata["reasoning"] == MARKER
        assert_sealed(store)


@pytest.mark.parametrize("tamper", ["payload", "binary", "format", "state", "row", "claim", "binding", "sentinel"])
def test_tampered_rows_fail_closed(protected, tmp_path, tamper):
    access, envelope, runtime, key = protected
    with SQLiteCompletionStore(tmp_path / "tamper.sqlite3", protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.RUNNING, terminal_reason=MARKER)
        event = store.commit_run(run, status=CompletionStatus.FAILED)
        if tamper in {"claim", "binding", "sentinel"}:
            with runtime.store.db.transaction() as conn:
                if tamper == "claim":
                    conn.execute("DELETE FROM studycrew_protected_records WHERE table_name='completion_events_v2'")
                elif tamper == "binding":
                    conn.execute("DELETE FROM studycrew_protected_lineages WHERE task_id=?", (envelope.task_id,))
                else:
                    conn.execute("DELETE FROM studycrew_card_crypto")
        else:
            with store.db.transaction() as conn:
                if tamper == "payload":
                    conn.execute("UPDATE completion_events_v2 SET payload_json=substr(payload_json,1,length(payload_json)-6)||'AAAAAA'")
                elif tamper == "binary":
                    conn.execute("UPDATE completion_events_v2 SET payload_json=?", (b"bad",))
                elif tamper == "format":
                    conn.execute("UPDATE completion_events_v2 SET payload_json='{}'")
                elif tamper == "state":
                    conn.execute("UPDATE completion_events_v2 SET status='SUCCEEDED'")
                else:
                    conn.execute("UPDATE completion_events_v2 SET worker_id='forged'")
        with pytest.raises(ProtectedStorageError):
            store.get(event.completion_id)
        with pytest.raises(ProtectedStorageError):
            store.commit_run(run, status=CompletionStatus.FAILED)


def test_wrong_missing_key_and_cross_group_capability(protected, tmp_path):
    access, envelope, runtime, key = protected
    store = TaskEnvelopeStore(tmp_path / "envelopes", protected=access)
    _, ref = store.commit(envelope)
    other = runtime.groups.create_group("Other", optional_roles=("planning",))
    with pytest.raises(ProtectedStorageError):
        access.registry.access(other.group_id, envelope.task_id)
    original = runtime.store.card_states._key
    try:
        for bad in (None, MasterKey(os.urandom(32))):
            runtime.store.card_states._key = bad
            with pytest.raises(ProtectedStorageError):
                store.get(ref)
            with pytest.raises(ProtectedStorageError):
                ProtectedLineages(runtime)
    finally:
        runtime.store.card_states._key = original
    assert store.get(ref) == envelope


def test_registration_retries_bind_once_and_grant_nothing(protected):
    access, envelope, runtime, key = protected
    binding = access.identity
    duplicate = access.registry.register(access.group_id, binding["approval_id"],
        generation=binding["generation"], digest=binding["proposal_digest"])
    assert duplicate.identity == binding
    assert runtime.store.grant_for_approval(binding["approval_id"]) is None
    assert runtime.store.get_run(binding["owner_run_id"]).status is TaskStatus.WAITING_APPROVAL
    assert binding["owner_task_id"] != envelope.task_id
    assert binding["owner_run_id"] != envelope.attempt_id
    with pytest.raises(Exception):
        access.registry.register(access.group_id, binding["approval_id"], generation=2, digest=binding["proposal_digest"])


@pytest.mark.parametrize("swap", ["row", "table", "field", "version", "store"])
def test_actual_ciphertext_context_substitution(protected, tmp_path, swap):
    access, envelope, runtime, key = protected
    with SQLiteCompletionStore(tmp_path / "swap.sqlite3", protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.IDLE, terminal_reason=MARKER)
        first = store.commit_run(run, status=CompletionStatus.FAILED)
        second = store.transition_completion(first, CompletionStatus.CANCELLED, actor="user", reason=MARKER)
        with store.db.transaction(immediate=False) as conn:
            wire = conn.execute("SELECT payload_json FROM completion_events_v2 WHERE completion_id=?", (first.completion_id,)).fetchone()[0]
            result_id = conn.execute("SELECT result_id FROM task_results_v1 WHERE completion_id=?", (first.completion_id,)).fetchone()[0]
        if swap == "row":
            with store.db.transaction() as conn:
                conn.execute("UPDATE completion_events_v2 SET payload_json=? WHERE completion_id=?", (wire, second.completion_id))
            with pytest.raises(ProtectedStorageError):
                store.get(second.completion_id)
        elif swap == "table":
            with store.db.transaction() as conn:
                conn.execute("UPDATE task_results_v1 SET payload_json=? WHERE result_id=?", (wire, result_id))
            with pytest.raises(ProtectedStorageError):
                store.task_result(completion_id=first.completion_id)
        elif swap == "field":
            with pytest.raises(ProtectedStorageError):
                store.protection.open("completion_events_v2", first.completion_id, wire, field="question")
        elif swap == "version":
            with store.db.transaction() as conn:
                conn.execute("UPDATE completion_events_v2 SET payload_json=? WHERE completion_id=?", ("p2:" + wire[3:], first.completion_id))
            with pytest.raises(ProtectedStorageError):
                store.get(first.completion_id)
        else:
            with pytest.raises(ProtectedStorageError):
                access.for_store(tmp_path / "wrong.sqlite3").open("completion_events_v2", first.completion_id, wire)


def test_disk_scan_keys_and_legacy_compression(protected, tmp_path):
    from backend.knowe_storage._sqlite import json_loads
    access, envelope, runtime, key = protected
    with SQLiteCompletionStore(tmp_path / "scan.sqlite3", protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.IDLE,
            terminal_reason=MARKER, final_candidate=MARKER, metadata={"reasoning": MARKER})
        event = store.commit_run(run, status=CompletionStatus.FAILED)
        assert store.get(event.completion_id).terminal_reason == MARKER
        with store.db.transaction(immediate=False) as conn:
            tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            reached = []
            for table in tables:
                rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
                if rows:
                    reached.append(table)
                for row in rows:
                    for value in row:
                        if isinstance(value, str):
                            decoded = json_loads(value, {}) if value.startswith(("z1:", "{", "[")) else value
                            assert MARKER not in json.dumps(decoded, ensure_ascii=False)
            assert {"task_runs", "task_results_v1", "task_journal_v1", "completion_outbox_v2", "agent_completion_outcomes_v2"} <= set(reached)
        for path in tmp_path.rglob("*"):
            if path.is_file():
                raw = path.read_bytes()
                assert MARKER.encode() not in raw
                assert key.key_material not in raw


def test_lost_envelope_claim_does_not_recreate_on_recommit(protected, tmp_path):
    access, envelope, runtime, key = protected
    store = TaskEnvelopeStore(tmp_path / "lost", protected=access)
    store.commit(envelope)
    with runtime.store.db.transaction() as conn:
        conn.execute("DELETE FROM studycrew_protected_records WHERE table_name='task_envelopes'")
    with pytest.raises(ProtectedStorageError):
        store.commit(envelope)


def test_runtime_and_file_reopen_uses_persisted_authenticated_binding(protected, tmp_path):
    access, envelope, runtime, key = protected
    envelopes = TaskEnvelopeStore(tmp_path / "restart", protected=access)
    _, ref = envelopes.commit(envelope)
    path = tmp_path / "restart.sqlite3"
    with SQLiteCompletionStore(path, protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.IDLE, terminal_reason=MARKER)
        event = store.commit_run(run, status=CompletionStatus.FAILED)
    groups = runtime.groups
    runtime.close()
    restarted = TaskRuntimeService(tmp_path / "authority", groups, card_master_key=key)
    try:
        capability = ProtectedLineages(restarted).access(access.group_id, envelope.task_id)
        assert TaskEnvelopeStore(tmp_path / "restart", protected=capability).get(ref) == envelope
        with SQLiteCompletionStore(path, protected=capability) as store:
            assert store.get(event.completion_id).terminal_reason == MARKER
            assert store.list_for_task(envelope.task_id)[0].completion_id == event.completion_id
    finally:
        restarted.close()


def test_purge_preserves_unrelated_legacy_records(protected, tmp_path):
    access, envelope, runtime, key = protected
    path = tmp_path / "mixed.sqlite3"
    with SQLiteCompletionStore(path) as legacy:
        old = legacy.commit_run(sample_run(), status=CompletionStatus.FAILED)
    with SQLiteCompletionStore(path, protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.IDLE, terminal_reason=MARKER)
        store.commit_run(run, status=CompletionStatus.FAILED)
        store.purge_protected()
    with SQLiteCompletionStore(path) as legacy:
        assert legacy.get(old.completion_id).task_id == "legacy-task"


def test_completed_owner_history_survives_but_cannot_write(protected, tmp_path):
    access, envelope, runtime, key = protected
    with SQLiteCompletionStore(tmp_path / "history.sqlite3", protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.IDLE, terminal_reason=MARKER)
        event = store.commit_run(run, status=CompletionStatus.SUCCEEDED)
        owner_id = access.identity["owner_run_id"]
        runtime.store.change_run(owner_id, event_type="status_changed", public_payload={"status": "succeeded"},
            idempotency_key="terminal-test", mutate=lambda owner: setattr(owner, "status", TaskStatus.SUCCEEDED))
        assert store.get(event.completion_id).terminal_reason == MARKER
        with pytest.raises(ProtectedStorageError):
            store.commit_run(run, status=CompletionStatus.SUCCEEDED)


@pytest.mark.parametrize("mutation", ["cancel", "archive", "source", "course", "remove"])
def test_scope_revocation_blocks_existing_objects(protected, tmp_path, mutation):
    access, envelope, runtime, key = protected
    store = TaskEnvelopeStore(tmp_path / "scope", protected=access)
    _, ref = store.commit(envelope)
    binding = access.identity
    if mutation == "cancel":
        runtime.cancel(access.group_id, binding["owner_run_id"], request_key="cancel")
    elif mutation == "archive":
        runtime.groups.archive(access.group_id)
    elif mutation == "source":
        runtime.groups.link_source(access.group_id)
    elif mutation == "course":
        runtime.groups.bind_course(access.group_id, "private-course")
    else:
        group = runtime.groups.get(access.group_id)
        runtime.groups.update_roster(access.group_id, tuple(agent for agent in group.roster if agent.agent_id != envelope.worker_id))
    with pytest.raises(ProtectedStorageError):
        store.get(ref)
    with pytest.raises(ProtectedStorageError):
        store.commit(envelope)


def test_content_bearing_record_identity_is_rejected(protected, tmp_path):
    access, envelope, runtime, key = protected
    with SQLiteCompletionStore(tmp_path / "opaque.sqlite3", protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.IDLE)
        record = replace(final_record(run), delivery_id="p1_private_ascii_secret")
        with pytest.raises(ProtectedStorageError):
            store.commit_success(run, record)
        assert store.list_active() == ()


@pytest.mark.parametrize("field", ["version", "route"])
def test_review_private_clear_fields_denied_before_storage(protected, tmp_path, field):
    access, envelope, runtime, key = protected
    path = tmp_path / "clear-fields.sqlite3"
    with SQLiteCompletionStore(path, protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.IDLE)
        if field == "version":
            repo = SQLiteTaskRunRepository(path, protected=access)
            run.version = MARKER
            try:
                with pytest.raises(ProtectedStorageError):
                    repo.create(run)
            finally:
                repo.db.close()
        else:
            event = store.commit_run(run, status=CompletionStatus.FAILED)
            changed = replace(event, delivery_intent={"audience": "user", "channel": MARKER})
            with pytest.raises(ProtectedStorageError, match="routing"):
                store.transition_completion(changed, CompletionStatus.CANCELLED, actor="user", reason="")
        assert_sealed(store)


@pytest.mark.parametrize("invalid", [True, 1.5, "2", -1, 2**63])
def test_review_version_requires_bounded_integer(protected, tmp_path, invalid):
    access, envelope, runtime, key = protected
    repo = SQLiteTaskRunRepository(tmp_path / "integer.sqlite3", protected=access)
    try:
        run = TaskRun(envelope=envelope, version=invalid, state=TaskState.IDLE)
        with pytest.raises(ProtectedStorageError):
            repo.create(run)
        assert repo.get(envelope.task_id) is None
    finally:
        repo.db.close()


@pytest.mark.parametrize("field", ["runtime_state", "created_at"])
def test_review_transition_clear_types_rejected(protected, tmp_path, field):
    access, envelope, runtime, key = protected
    with SQLiteCompletionStore(tmp_path / "transition-types.sqlite3", protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.IDLE)
        event = store.commit_run(run, status=CompletionStatus.FAILED)
        if field == "created_at":
            from backend.knowe_harness.completion import DecisionEvent, DecisionType
            decision = DecisionEvent("p1_decision_" + "e" * 32, DecisionType.TASK_CANCELLED,
                envelope.project_id, "user", envelope.task_id, envelope.attempt_id, event.completion_id,
                created_at=MARKER)
            with pytest.raises(ProtectedStorageError):
                store.record_decision(decision)
        else:
            changed = replace(event, runtime_state=MARKER)
            with pytest.raises(ProtectedStorageError):
                store.transition_completion(changed, CompletionStatus.CANCELLED, actor="user", reason="")
        assert_sealed(store)


@pytest.mark.parametrize("status", [MARKER, "unexpected", 1])
def test_review_wait_status_is_closed_enum(protected, tmp_path, status):
    access, envelope, runtime, key = protected
    with SQLiteCompletionStore(tmp_path / "wait-status.sqlite3", protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.IDLE)
        store.commit_run(run, status=CompletionStatus.WAITING)
        token = store.active_wait_for_task(envelope.task_id)
        with pytest.raises(ProtectedStorageError):
            store.resolve_wait(token.wait_token_id, status=status)
        assert store.get_wait_token(token.wait_token_id).status == "open"
        assert_sealed(store)


@pytest.mark.parametrize("operation,status", [
    ("resolve", "cancelled"), ("completion", "resolved"),
    ("resolve", "resolved"), ("resolve", "superseded"),
])
def test_review_supported_wait_closure_survives_reopen(protected, tmp_path, operation, status):
    access, envelope, runtime, key = protected
    path = tmp_path / "wait-closure.sqlite3"
    with SQLiteCompletionStore(path, protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.IDLE,
            waiting_question=MARKER, dependency=MARKER)
        event = store.commit_run(run, status=CompletionStatus.WAITING)
        token = store.active_wait_for_task(envelope.task_id)
        if operation == "resolve":
            assert store.resolve_wait(token.wait_token_id, status=status).status == status
        else:
            # Existing completion supersession closes the old WAITING token as
            # resolved before its generic close pass; do not redesign that here.
            store.transition_completion(event, CompletionStatus.CANCELLED, actor="user", reason=MARKER)
        closed = store.get_wait_token(token.wait_token_id)
        assert closed.status == status
        assert closed.question == MARKER
        assert store.active_wait_for_task(envelope.task_id) is None
        assert_sealed(store)
    with SQLiteCompletionStore(path, protected=access) as reopened:
        assert reopened.get_wait_token(token.wait_token_id) == closed
        assert reopened.active_wait_for_task(envelope.task_id) is None


def private_provenance(identity):
    from knowe_provenance import recorded_provenance
    return recorded_provenance({"build_id": "build", "git_commit": "a" * 40,
        "runtime_schema_version": "1", "harness_schema_version": "1", "prompt_bundle_version": "1",
        "migration_epoch": 1, "provenance_id": identity}).to_dict()


@pytest.mark.parametrize("table", ["receipt", "outbox", "journal", "history", "outcome", "wait"])
def test_review_retry_authenticates_existing_records(protected, tmp_path, table):
    access, envelope, runtime, key = protected
    with SQLiteCompletionStore(tmp_path / "retry.sqlite3", protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.IDLE, terminal_reason=MARKER)
        if table == "journal":
            store.create_started_run(run)
            sql_table = "task_journal_v1"
            retry = lambda: store.create_started_run(run)
        elif table == "history":
            store.create_started_run(run)
            store.archive_run_for_resume(run, reason=MARKER)
            sql_table = "task_run_history_v2"
            retry = lambda: store.archive_run_for_resume(run, reason=MARKER)
        elif table == "wait":
            event = store.commit_run(run, status=CompletionStatus.WAITING)
            token = store.active_wait_for_task(envelope.task_id)
            store.resolve_wait(token.wait_token_id)
            sql_table = "wait_tokens_v2"
            retry = lambda: store.commit_run(run, status=CompletionStatus.WAITING)
        else:
            event = store.commit_run(run, status=CompletionStatus.FAILED)
            entry = store.pending_outbox()[0]
            if table == "receipt":
                store.acknowledge_outbox(entry.outbox_id, effect_ref=MARKER, effect_payload={"text": MARKER})
                sql_table = "completion_projection_receipts_v2"
                store.commit_run(run, status=CompletionStatus.FAILED)
                assert entry.outbox_id in {item.outbox_id for item in store.pending_outbox()}
                retry = lambda: store.acknowledge_outbox(entry.outbox_id, effect_ref=MARKER, effect_payload={"text": MARKER})
            else:
                sql_table = "agent_completion_outcomes_v2" if table == "outcome" else "completion_outbox_v2"
                retry = lambda: store.commit_run(run, status=CompletionStatus.FAILED)
        with store.db.transaction() as conn:
            conn.execute(f"UPDATE {sql_table} SET payload_json='{{}}'")
        with pytest.raises(ProtectedStorageError):
            retry()
        if table == "receipt":
            assert entry.outbox_id in {item.outbox_id for item in store.pending_outbox()}
        with store.db.transaction(immediate=False) as conn:
            assert conn.execute(f"SELECT payload_json FROM {sql_table}").fetchone()[0] == "{}"


@pytest.mark.parametrize("collision", ["receipt_body", "receipt_ref", "journal_body", "history_body"])
def test_review_retry_rejects_different_authenticated_content(protected, tmp_path, collision):
    access, envelope, runtime, key = protected
    with SQLiteCompletionStore(tmp_path / "different.sqlite3", protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.RUNNING, terminal_reason=MARKER)
        if collision == "journal_body":
            store.create_started_run(run)
            store.record_progress(run)
            run.terminal_reason = "different"
            action = lambda: store.record_progress(run)
        elif collision == "history_body":
            store.create_started_run(run)
            store.archive_run_for_resume(run)
            run.final_candidate = "different"
            action = lambda: store.archive_run_for_resume(run)
        else:
            store.commit_run(run, status=CompletionStatus.FAILED)
            entry = store.pending_outbox()[0]
            store.acknowledge_outbox(entry.outbox_id, effect_ref=MARKER, effect_payload={"body": MARKER})
            store.commit_run(run, status=CompletionStatus.FAILED)
            action = lambda: store.acknowledge_outbox(entry.outbox_id,
                effect_ref="different" if collision == "receipt_ref" else MARKER,
                effect_payload={"body": "different" if collision == "receipt_body" else MARKER})
        with pytest.raises(ProtectedStorageError, match="immutable retry"):
            action()
        if collision.startswith("receipt"):
            assert entry.outbox_id in {row.outbox_id for row in store.pending_outbox()}
            assert store.projection_receipt(entry.outbox_id)["payload"] == {"body": MARKER}
        assert_sealed(store)


def test_review_valid_retry_preserves_outbox_state_and_receipt(protected, tmp_path):
    access, envelope, runtime, key = protected
    with SQLiteCompletionStore(tmp_path / "valid-retry.sqlite3", protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.RUNNING, terminal_reason=MARKER)
        store.create_started_run(run)
        store.create_started_run(run)
        store.commit_run(run, status=CompletionStatus.FAILED)
        entry = store.pending_outbox()[0]
        store.claim_outbox(entry.outbox_id)
        failed = store.fail_outbox(entry.outbox_id, MARKER)
        store.commit_run(run, status=CompletionStatus.FAILED)
        assert next(row for row in store.pending_outbox() if row.outbox_id == entry.outbox_id) == failed
        store.acknowledge_outbox(entry.outbox_id, effect_ref=MARKER, effect_payload={"body": MARKER})
        receipt = store.projection_receipt(entry.outbox_id)
        store.commit_run(run, status=CompletionStatus.FAILED)
        store.acknowledge_outbox(entry.outbox_id, effect_ref=MARKER, effect_payload={"body": MARKER})
        assert store.projection_receipt(entry.outbox_id) == receipt
        assert entry.outbox_id not in {row.outbox_id for row in store.pending_outbox()}
        assert_sealed(store)


@pytest.mark.parametrize("field", ["effect_sha256", "outbox_id", "created_at"])
def test_review_receipt_retry_authenticates_clear_identity_and_digest(protected, tmp_path, field):
    access, envelope, runtime, key = protected
    with SQLiteCompletionStore(tmp_path / "receipt-columns.sqlite3", protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.IDLE)
        store.commit_run(run, status=CompletionStatus.FAILED)
        entry = store.pending_outbox()[0]
        store.acknowledge_outbox(entry.outbox_id, effect_ref=MARKER, effect_payload={"body": MARKER})
        store.commit_run(run, status=CompletionStatus.FAILED)
        with store.db.transaction() as conn:
            conn.execute(f"UPDATE completion_projection_receipts_v2 SET {field}=?", ("tampered",))
        with pytest.raises(ProtectedStorageError):
            store.acknowledge_outbox(entry.outbox_id, effect_ref=MARKER, effect_payload={"body": MARKER})
        assert entry.outbox_id in {row.outbox_id for row in store.pending_outbox()}


def test_review_unbound_new_clear_column_fails_closed(protected, tmp_path):
    access, envelope, runtime, key = protected
    repo = SQLiteTaskRunRepository(tmp_path / "schema.sqlite3", protected=access)
    try:
        with repo.db.transaction() as conn:
            conn.execute("ALTER TABLE task_runs ADD COLUMN unbound_label TEXT")
        with pytest.raises(ProtectedStorageError, match="unbound"):
            repo.create(TaskRun(envelope=envelope, version=1, state=TaskState.IDLE))
        assert repo.get(envelope.task_id) is None
        assert access.identity["worker_run_id"] == ""
    finally:
        repo.db.close()


@pytest.mark.parametrize("boundary", ["delegate_create", "delegate_save", "completion_run", "completion_event", "delivery"])
def test_review_provenance_collision_diagnostics_are_content_free(protected, tmp_path, boundary):
    access, envelope, runtime, key = protected
    path = tmp_path / "diagnostics.sqlite3"
    run = TaskRun(envelope=envelope, version=1, state=TaskState.IDLE,
        provenance=private_provenance(MARKER))
    different = private_provenance("different-provenance")
    with SQLiteCompletionStore(path, protected=access) as store:
        repo = SQLiteTaskRunRepository(path, protected=access)
        try:
            if boundary.startswith("delegate"):
                repo.create(run)
                run.provenance = different
                if boundary == "delegate_save":
                    run.version = 2
                action = (lambda: repo.save(run, expected_version=1)) if boundary == "delegate_save" else lambda: repo.create(run)
            elif boundary == "completion_run":
                store.commit_run(run, status=CompletionStatus.FAILED)
                run.provenance = different
                action = lambda: store.commit_run(run, status=CompletionStatus.FAILED)
            elif boundary == "completion_event":
                event = store.commit_run(run, status=CompletionStatus.FAILED)
                store.transition_completion(event, CompletionStatus.CANCELLED, actor="user", reason="reason")
                altered = replace(event, provenance=different)
                action = lambda: store.transition_completion(altered, CompletionStatus.CANCELLED, actor="user", reason="reason")
            else:
                record = final_record(run)
                store.commit_success(run, record)
                altered = replace(record, provenance=different)
                action = lambda: store.commit_success(run, altered)
            with pytest.raises(RuntimeError) as caught:
                action()
            assert MARKER not in str(caught.value)
            assert MARKER not in "".join(traceback.format_exception(caught.value))
            assert_sealed(store)
        finally:
            repo.db.close()


@pytest.mark.parametrize("role", ["coordinator", "verifier", "expert"])
def test_registration_rejects_unsupported_canonical_targets(tmp_path, role):
    from backend.groups import GroupAgent, ExpertTemplateConfig
    from backend.task_runtime.card_state import CardStateError
    groups = GroupRepository(tmp_path / "authority")
    group = groups.create_group("Targets")
    if role == "expert":
        groups.update_roster(group.group_id, [*group.roster, GroupAgent(
            "gag_" + "1" * 32, group.group_id, "expert", "Private", expert=ExpertTemplateConfig("Private", "math"))])
    runtime = TaskRuntimeService(tmp_path / "authority", groups, card_master_key=MasterKey(os.urandom(32)))
    try:
        owner = runtime.submit(group.group_id, title="Targets", request_key="owner")
        runtime.schedule()
        # Mandatory roles are rejected by canonical card admission itself;
        # supported cards targeting experts must still be denied by this slice.
        with pytest.raises((ProtectedStorageError, CardStateError)):
            card = runtime.cards.create(group.group_id, owner.run_id, task_id=owner.task_id,
                proposal=CardProposal(role, MARKER), request_key="card")
            ProtectedLineages(runtime).register(group.group_id, card.approval_id,
                generation=card.content_generation, digest=card.proposal_digest)
    finally:
        runtime.close()


def test_strict_envelope_payload_refs_and_file_denial(protected, tmp_path):
    access, envelope, runtime, key = protected
    envelopes = TaskEnvelopeStore(tmp_path / "strict", protected=access)
    for changed in (replace(envelope, task_id="downgraded"), replace(envelope, goal="wrong"), replace(envelope, title="x" * (3 * 1024 * 1024)),
        replace(envelope, goal="\ud800"), replace(envelope, scope_root=str(tmp_path))):
        with pytest.raises(ProtectedStorageError):
            envelopes.commit(changed)
    for reference in ("../escape", "runtime/task-envelopes/../../../escape.json", str(tmp_path)):
        with pytest.raises(ProtectedStorageError):
            envelopes.get(reference)
    with SQLiteCompletionStore(tmp_path / "files.sqlite3", protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.IDLE)
        record = replace(final_record(run), artifacts=(ArtifactRecord(MARKER, "a" * 64, 1),))
        with pytest.raises(ProtectedStorageError):
            store.commit_success(run, record)
        assert store.list_active() == ()


def test_protected_completion_transaction_failure_and_partial_claim(protected, tmp_path, monkeypatch):
    access, envelope, runtime, key = protected
    with SQLiteCompletionStore(tmp_path / "rollback.sqlite3", protected=access) as store:
        run = TaskRun(envelope=envelope, version=1, state=TaskState.IDLE, terminal_reason=MARKER)
        record = final_record(run)
        original = store._insert_agent_completion_outcome
        def fail_after_writes(conn, event):
            original(conn, event)
            raise RuntimeError("controlled rollback")
        monkeypatch.setattr(store, "_insert_agent_completion_outcome", fail_after_writes)
        with pytest.raises(RuntimeError, match="controlled rollback"):
            store.commit_success(run, record)
        with store.db.transaction(immediate=False) as conn:
            for table in ("completion_events_v2", "task_results_v1", "task_journal_v1", "completion_outbox_v2", "agent_completion_outcomes_v2"):
                assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        with runtime.store.db.transaction(immediate=False) as conn:
            assert conn.execute("SELECT COUNT(*) FROM studycrew_protected_records WHERE task_id=?", (envelope.task_id,)).fetchone()[0] > 0
        monkeypatch.setattr(store, "_insert_agent_completion_outcome", original)
        event, _ = store.commit_success(run, record)
        assert store.get(event.completion_id).terminal_reason == MARKER
        assert_sealed(store)


def test_wait_answers_transitions_and_retained_versions(protected, tmp_path):
    access, envelope, runtime, key = protected
    with SQLiteCompletionStore(tmp_path / "wait.sqlite3", protected=access) as store:
        run = TaskRun(envelope=envelope, state=TaskState.RUNNING, version=1,
            waiting_question=MARKER, dependency=MARKER, terminal_reason=MARKER)
        store.create_started_run(run)
        event = store.commit_run(run, status=CompletionStatus.WAITING)
        token = store.active_wait_for_completion(event.completion_id)
        answered = store.resume_wait(token.wait_token_id, answer=MARKER, answer_ref=MARKER, actor=MARKER)
        assert answered.answer == MARKER
        assert store.get_wait_token(token.wait_token_id).answer_ref == MARKER
        decision = DecisionEvent("p1_decision_" + "d" * 32, DecisionType.TASK_CANCELLED,
            envelope.project_id, MARKER, envelope.task_id, envelope.attempt_id, event.completion_id, reason=MARKER)
        changed, decision = store.transition_with_decision(event, CompletionStatus.CANCELLED,
            decision=decision, actor=MARKER, reason=MARKER)
        assert changed.completion_id != event.completion_id
        assert store.get(event.completion_id).active is False
        assert store.active_for(envelope.task_id).status is CompletionStatus.CANCELLED
        assert store.get_wait_token(token.wait_token_id).status == "resolved"
        assert len(store.list_for_task(envelope.task_id)) == 2
        assert store.resolve_wait(token.wait_token_id).answer == MARKER
        assert_sealed(store)


def sample_run():
    return TaskRun(envelope=TaskEnvelope(
        task_id="legacy-task", attempt_id="legacy-attempt", project_id="legacy-project",
        worker_id="legacy-worker", goal="Storage transaction marker",
    ), state=TaskState.RUNNING, version=1)


def test_delegate_create_and_save_use_existing_compression(tmp_path):
    repo = SQLiteTaskRunRepository(tmp_path / "delegate.sqlite3")
    try:
        run = sample_run()
        repo.create(run)
        run.version = 2
        repo.save(run, expected_version=1)
        assert repo.get(run.envelope.task_id).version == 2
    finally:
        repo.db.close()


def test_completion_fault_rolls_back_run_and_journal(tmp_path, monkeypatch):
    store = SQLiteCompletionStore(tmp_path / "completion.sqlite3")
    try:
        run = sample_run()
        original = store._insert_journal_entry

        def fail_after_insert(conn, entry):
            original(conn, entry)
            raise RuntimeError("injected transaction failure")

        monkeypatch.setattr(store, "_insert_journal_entry", fail_after_insert)
        with pytest.raises(RuntimeError, match="injected"):
            store.create_started_run(run)
        with store.db.transaction(immediate=False) as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_runs'").fetchone()
            assert not exists or conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM task_journal_v1").fetchone()[0] == 0
    finally:
        store.close()


def test_success_fault_rolls_back_run_before_delivery(tmp_path, monkeypatch):
    store = SQLiteCompletionStore(tmp_path / "success.sqlite3")
    try:
        run = sample_run()
        record = DeliveryRecord("delivery", "delivery-key", run.envelope.task_id,
            run.run_id, run.envelope.project_id, TaskState.IDLE, "result", "done",
            (), BudgetSummary.from_mapping({}), 1)

        def fail_event(*args):
            raise RuntimeError("injected event failure")

        monkeypatch.setattr(store, "_insert_event", fail_event)
        with pytest.raises(RuntimeError, match="injected"):
            store.commit_success(run, record)
        with store.db.transaction(immediate=False) as conn:
            for table in ("task_runs", "deliveries"):
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
                assert not exists or conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        store.close()
