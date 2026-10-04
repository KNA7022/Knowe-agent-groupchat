from __future__ import annotations

import base64
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import uuid

import pytest

from backend.groups import GroupRepository
from backend.hub import Hub
from backend.persist import Store
from backend.protected_children import ProtectedChildren, TABLE
from backend.protected_events import ProtectedEvents
from backend.protected_storage import ProtectedLineages, ProtectedStorageError, ROW_KEYS, bounded_json
from backend.knowe_rag.crypto import MasterKey
from backend.knowe_harness.store import TaskEnvelopeStore
from backend.knowe_harness.completion import SQLiteCompletionStore, ProjectionKind
from backend.runtime import TaskEnvelope, TaskRun, TaskState, DeliveryRecord, BudgetSummary
from backend.task_runtime import TaskRuntimeService
from backend.task_runtime.card_state import CardProposal
from backend.task_runtime.contracts import TaskStatus, EvidenceRef, RiskLevel, opaque_id


INSTRUCTION = "私有指令-INSTRUCTION-487562"
ANSWER = "私有答案-ANSWER-719481"
REASONING = "私有推理-REASONING-917735"


@pytest.fixture
def world(tmp_path):
    groups = GroupRepository(tmp_path)
    key = MasterKey(os.urandom(32))
    runtime = TaskRuntimeService(tmp_path, groups, card_master_key=key)
    yield tmp_path, groups, runtime, key
    runtime.close()


def enrolled(world, *, group=None, project="project_20261004000001", enroll=True):
    root, groups, runtime, key = world
    group = group or groups.create_group("Canonical", optional_roles=("planning",), legacy_project_id=project)
    owner = runtime.submit(group.group_id, title="Protected execution", request_key=uuid.uuid4().hex)
    runtime.schedule()
    card = runtime.cards.create(group.group_id, owner.run_id, task_id=owner.task_id,
        proposal=CardProposal("planning", INSTRUCTION), request_key=uuid.uuid4().hex)
    registry = ProtectedLineages(runtime)
    access = registry.register(group.group_id, card.approval_id, generation=1, digest=card.proposal_digest)
    runtime.cards.resolve(group.group_id, card.approval_id, expected_generation=1,
        expected_digest=card.proposal_digest, approved=True, request_key=uuid.uuid4().hex)
    runtime.schedule()
    children = ProtectedChildren.existing(runtime) or ProtectedChildren(registry)
    location = children.register(access) if enroll else None
    binding = access.identity
    envelope = TaskEnvelope(task_id=access.task_id, attempt_id=binding["attempt_id"],
        project_id=binding["project_id"], worker_id=binding["worker_id"], goal=INSTRUCTION)
    return children, access, envelope, owner, group, location


def populate(item, stage):
    children, access, envelope, owner, group, location = item
    if stage == "enrolled":
        return None, None
    workspace = Path(location["completion"]).parent.parent
    TaskEnvelopeStore(workspace, protected=access).commit(envelope)
    if stage == "envelope":
        return None, None
    with SQLiteCompletionStore(location["completion"], protected=access) as store:
        run = TaskRun(envelope=envelope, state=TaskState.RUNNING, version=1)
        store.create_started_run(run)
        if stage == "started":
            return run, None
        run.state = TaskState.IDLE
        run.set_completion_status("SUCCEEDED")
        run.final_candidate = ANSWER
        run.metadata["reasoning"] = REASONING
        record = DeliveryRecord("p1_delivery_" + uuid.uuid4().hex, "p1_deliverykey_" + uuid.uuid4().hex,
            envelope.task_id, run.run_id, envelope.project_id, TaskState.IDLE, ANSWER, REASONING,
            (), BudgetSummary.from_mapping({}), 1, provenance=run.provenance)
        event, _ = store.commit_success(run, record)
        outbox = next(row for row in store.pending_outbox() if row.projection_kind is ProjectionKind.COORDINATOR)
        store.claim_outbox(outbox.outbox_id)
        store.acknowledge_outbox(outbox.outbox_id, effect_ref="local-fixture", effect_payload={"pending_review": True})
    return run, event


def assert_clean(item):
    children, access, envelope, owner, group, location = item
    assert children._read(access.task_id) is None
    assert not (Path(location["envelopes"]) / access.task_id).exists()
    if Path(location["completion"]).exists():
        with children.registry.db.transaction(immediate=False) as authority:
            claims = authority.execute("SELECT table_name,record_id FROM studycrew_protected_records WHERE task_id=? AND store_id=?",
                (access.task_id, location["completion_store"])).fetchall()
        with closing(sqlite3.connect(location["completion"])) as conn, conn:
            for table in ROW_KEYS:
                columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
                if "task_id" in columns:
                    assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE task_id=?", (access.task_id,)).fetchone()[0] == 0
            for claim in claims:
                table, record_id = claim
                keys = ROW_KEYS[table]
                assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE " + " AND ".join(f"{key}=?" for key in keys),
                    record_id.split(":")).fetchone()[0] == 0
    with pytest.raises(ProtectedStorageError):
        access.identity


@pytest.mark.parametrize("stage", ["enrolled", "envelope", "started", "completion"])
def test_each_child_checkpoint_revoke_and_real_reopen(world, stage):
    item = enrolled(world)
    populate(item, stage)
    children, access, envelope, owner, group, location = item
    assert children.register(access) == location
    if stage == "enrolled":
        assert not Path(location["completion"]).exists()
        assert not Path(location["envelopes"]).exists()
    def stop(where):
        if where == "after_revoke":
            raise OSError("injected interruption")
    children.fault = stop
    with pytest.raises(OSError, match="injected"):
        access.registry.revoke(group.group_id, access.task_id)
    with pytest.raises(ProtectedStorageError):
        TaskEnvelopeStore(Path(location["completion"]).parent.parent, protected=access).commit(envelope)
    world[2].close()
    runtime = TaskRuntimeService(world[0], GroupRepository(world[0]), card_master_key=world[3])
    try:
        reopened = ProtectedChildren.existing(runtime)
        assert runtime.recovery_report["protected_children"]["cleaned"] == 1
        assert_clean((reopened, access, envelope, owner, group, location))
        with runtime.store.db.transaction(immediate=False) as conn:
            assert conn.execute("SELECT state FROM studycrew_protected_lineages WHERE task_id=?", (access.task_id,)).fetchone()[0] == "revoked"
        assert reopened.retry_cleanup() == {"cleaned": 0, "retained": 0}
    finally:
        runtime.close()


@pytest.mark.parametrize("point", ["after_events", "after_rows", "after_envelopes", "before_retire"])
def test_interrupted_cleanup_never_retires_early(world, point):
    item = enrolled(world)
    populate(item, "completion")
    children, access, envelope, owner, group, location = item
    children.fault = lambda stage: (_ for _ in ()).throw(OSError("fault")) if stage == point else None
    with pytest.raises(OSError):
        access.registry.revoke(group.group_id, access.task_id)
    assert children._read(access.task_id) == location
    children.fault = None
    children.retry_cleanup()
    assert_clean(item)


def test_no_body_enrollment_is_encrypted_and_never_executes_on_restart(world):
    item = enrolled(world)
    children, access, envelope, owner, group, location = item
    with world[2].store.db.transaction(immediate=False) as conn:
        before_claims = tuple(conn.execute("SELECT * FROM studycrew_protected_records"))
        raw = conn.execute(f"SELECT wire FROM {TABLE}").fetchone()[0]
    assert str(world[0]).encode() not in raw
    assert group.group_id.encode() not in raw and envelope.project_id.encode() not in raw
    assert before_claims == ()
    world[2].close()
    runtime = TaskRuntimeService(world[0], GroupRepository(world[0]), card_master_key=world[3])
    try:
        assert runtime.store.get_run(owner.run_id).status is TaskStatus.PAUSED
        assert runtime.recovery_report["protected_children"] == {"cleaned": 0, "retained": 1}
        with runtime.store.db.transaction(immediate=False) as conn:
            assert tuple(conn.execute("SELECT * FROM studycrew_protected_records")) == ()
        assert not Path(location["completion"]).exists()
        assert not Path(location["envelopes"]).exists()
    finally:
        runtime.close()


def test_real_group_deletion_cleans_all_artifacts(world):
    item = enrolled(world)
    populate(item, "completion")
    children, access, envelope, owner, group, location = item
    groups, runtime = world[1:3]
    tx = groups.prepare_delete(group.group_id, groups.delete_preview(group.group_id).confirmation_token)
    runtime.prepare_group_deletion(group.group_id, tx["transaction_id"])
    runtime.clean_prepared_group_deletion(group.group_id)
    groups.mark_runtime_deleted(tx["transaction_id"])
    groups.commit_delete(tx["transaction_id"])
    runtime.complete_group_deletion(group.group_id)
    assert_clean(item)


@pytest.mark.parametrize("field", ["wire", "mac", "task_id"])
def test_corrupt_catalog_keeps_failure_explicit(world, field):
    children, access, envelope, owner, group, location = enrolled(world)
    with children.db.transaction() as conn:
        conn.execute(f"UPDATE {TABLE} SET {field}=?", (b"x" * 40 if field == "wire" else "forged",))
    with pytest.raises(ProtectedStorageError):
        children.retry_cleanup()


def test_missing_key_and_changed_path_do_not_retire(world, monkeypatch):
    item = enrolled(world)
    populate(item, "envelope")
    children, access, envelope, owner, group, location = item
    monkeypatch.setattr(children.registry.crypto, "_key", None)
    with pytest.raises(Exception):
        children.retry_cleanup()
    with children.db.transaction(immediate=False) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 1


def test_unclaimed_envelope_keeps_cleanup_pending(world):
    item = enrolled(world)
    populate(item, "envelope")
    children, access, envelope, owner, group, location = item
    other = Path(location["envelopes"]) / access.task_id / "unowned.json"
    other.write_text("unrelated", encoding="utf-8")
    with pytest.raises(ProtectedStorageError, match="unowned"):
        access.registry.revoke(group.group_id, access.task_id)
    assert other.read_text(encoding="utf-8") == "unrelated"
    assert children._read(access.task_id) == location


def test_failed_enrollment_has_no_child_body_or_claim(world):
    children, access, envelope, owner, group, _ = enrolled(world, enroll=False)
    children.db.initialize("PRAGMA query_only=ON")
    with pytest.raises(ProtectedStorageError):
        children.register(access)
    completion, envelopes = children._locations(envelope.project_id)
    assert not completion.exists() and not envelopes.exists()
    with world[2].store.db.transaction(immediate=False) as conn:
        assert conn.execute("SELECT COUNT(*) FROM studycrew_protected_records").fetchone()[0] == 0
    children.db.initialize("PRAGMA query_only=OFF")
    assert children.register(access)["completion"] == str(completion)


@pytest.mark.parametrize("field", ["group_id", "project_id", "completion", "completion_store", "envelopes"])
def test_forged_authenticated_descriptor_cannot_redirect_cleanup(world, field):
    item = enrolled(world)
    children, access, envelope, owner, group, location = item
    forged = {**location, field: "foreign"}
    wire = children.registry.crypto._key.seal(bounded_json(forged), aad=children._aad(access.task_id))
    mac = children._digest("wire", [access.task_id, base64.b64encode(wire).decode()])
    with children.db.transaction() as conn:
        conn.execute(f"UPDATE {TABLE} SET wire=?,mac=? WHERE task_id=?", (wire, mac, access.task_id))
    with pytest.raises(ProtectedStorageError):
        children.retry_cleanup()


def test_actual_reparse_path_denies_before_enrollment_and_on_reopen(world):
    children, access, envelope, owner, group, _ = enrolled(world, enroll=False)
    link = world[0] / envelope.project_id
    target = world[0] / "unrelated-target"
    target.mkdir()
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        with pytest.raises(ProtectedStorageError):
            children.register(access)
        assert list(target.iterdir()) == []
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()
    location = children.register(access)
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        with pytest.raises(ProtectedStorageError):
            children.retry_cleanup()
        with children.db.transaction(immediate=False) as conn:
            assert conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 1
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


@pytest.mark.parametrize("key_kind", ["wrong", "missing"])
def test_reopen_with_unavailable_key_is_explicit_recovery_requirement(world, key_kind):
    item = enrolled(world)
    populate(item, "started")
    world[2].close()
    key = MasterKey(os.urandom(32)) if key_kind == "wrong" else None
    with pytest.raises(ProtectedStorageError):
        TaskRuntimeService(world[0], GroupRepository(world[0]), card_master_key=key)
    runtime = TaskRuntimeService(world[0], GroupRepository(world[0]), card_master_key=world[3])
    try:
        assert ProtectedChildren.existing(runtime)._read(item[1].task_id) == item[5]
    finally:
        runtime.close()


def test_quarantine_retains_unrevoked_body_and_revoked_cleanup_retries(world, caplog):
    item = enrolled(world)
    populate(item, "started")
    children, access, envelope, owner, group, location = item
    runtime = world[2]
    # Existing invalid-proof quarantine is a fence, never authorization to erase.
    runtime.store.prepare_group_deletion(group.group_id, "invalid-proof",
        group_proof_digest="invalid", confirmation_token="invalid", runtime_project_id=envelope.project_id)
    runtime.reconcile_group_deletions()
    assert runtime.store.group_deletion_intent(group.group_id)["stage"] == "quarantined"
    assert children.retry_cleanup() == {"cleaned": 0, "retained": 1}
    assert (Path(location["envelopes"]) / access.task_id).exists()
    with pytest.raises(ProtectedStorageError):
        access.identity
    children.fault = lambda stage: (_ for _ in ()).throw(OSError("fault")) if stage == "after_rows" else None
    with pytest.raises(OSError):
        access.registry.revoke(group.group_id, access.task_id)
    runtime.close()
    reopened = TaskRuntimeService(world[0], GroupRepository(world[0]), card_master_key=world[3])
    try:
        assert ProtectedChildren.existing(reopened)._read(access.task_id) is None
        assert not (Path(location["envelopes"]) / access.task_id).exists()
        assert reopened.store.group_deletion_intent(group.group_id)["stage"] == "quarantined"
        assert reopened.store.get_run(owner.run_id).status is TaskStatus.RUNNING
        assert reopened.recovery_report["fenced_runs"] == [
            {"run_id": owner.run_id, "reason": "group_quarantine_fence"}]
        assert owner.run_id not in reopened.recovery_report["paused_runs"]
        assert "group_quarantine_fence" in caplog.text
        assert all(marker not in caplog.text for marker in (INSTRUCTION, ANSWER, REASONING))
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization,with_event", [("revoked", False), ("revoked", True), ("canonical", True)])
async def test_restart_cleanup_cannot_revoke_other_quarantined_group(world, monkeypatch, authorization, with_event):
    first = enrolled(world)
    populate(first, "completion" if with_event else "started")
    second = enrolled(world, project="project_20261004000002")
    populate(second, "started")
    children, access, envelope, owner, group, location = first
    runtime, groups = world[2], world[1]
    authority = ProtectedEvents(access.registry)  # Persist even an empty catalog.
    if with_event:
        authority.attach(access, location["completion"])
        disk = Store(world[0] / "events")
        try:
            hub = Hub(disk)
            hub.bind_protected_events(authority)
            with SQLiteCompletionStore(location["completion"], protected=access) as selected:
                outbox = next(row for row in selected.pending_outbox() if row.projection_kind is ProjectionKind.DELIVERY)
            published = await hub.emit_protected(access, outbox.outbox_id)
        finally:
            disk.close()
    retained_task, retained_group, retained_location = second[1].task_id, second[4], second[5]
    runtime.store.prepare_group_deletion(retained_group.group_id, "invalid-proof",
        group_proof_digest="invalid", confirmation_token="invalid", runtime_project_id=second[2].project_id)
    runtime.reconcile_group_deletions()
    assert runtime.store.group_deletion_intent(retained_group.group_id)["stage"] == "quarantined"
    with closing(sqlite3.connect(retained_location["completion"])) as conn:
        retained_rows = tuple(conn.iterdump())
    retained_files = {p.name: p.read_bytes() for p in (Path(retained_location["envelopes"]) / retained_task).iterdir()}
    with runtime.store.db.transaction(immediate=False) as conn:
        retained_claims = tuple(conn.execute("SELECT * FROM studycrew_protected_records WHERE task_id=?", (retained_task,)))
    if authorization == "revoked":
        def interrupted(_task):
            raise OSError("after durable revoke before event cleanup")
        with monkeypatch.context() as patch:
            patch.setattr(authority, "_purge_rows", interrupted)
            with pytest.raises(OSError, match="after durable revoke"):
                access.registry.revoke(group.group_id, access.task_id)
    else:
        tx = groups.prepare_delete(group.group_id, groups.delete_preview(group.group_id).confirmation_token)
        runtime.prepare_group_deletion(group.group_id, tx["transaction_id"])
    runtime.close()
    reopened = TaskRuntimeService(world[0], GroupRepository(world[0]), card_master_key=world[3])
    try:
        catalog = ProtectedChildren.existing(reopened)
        with reopened.store.db.transaction(immediate=False) as conn:
            assert conn.execute("SELECT state FROM studycrew_protected_lineages WHERE task_id=?", (retained_task,)).fetchone()[0] == "active"
            assert tuple(conn.execute("SELECT * FROM studycrew_protected_records WHERE task_id=?", (retained_task,))) == retained_claims
        assert catalog._read(retained_task) == retained_location
        with closing(sqlite3.connect(retained_location["completion"])) as conn:
            assert tuple(conn.iterdump()) == retained_rows
        assert {p.name: p.read_bytes() for p in (Path(retained_location["envelopes"]) / retained_task).iterdir()} == retained_files
        assert reopened.store.get_run(second[3].run_id).status is TaskStatus.RUNNING
        with pytest.raises(ProtectedStorageError):
            ProtectedLineages(reopened).access(retained_group.group_id, retained_task).identity
        assert_clean((catalog, access, envelope, owner, group, location))
        if with_event:
            recovered_events = ProtectedEvents.existing(reopened)
            assert recovered_events.references(envelope.project_id) == []
            assert recovered_events.head(envelope.project_id) == published.reference["seq"]
            with pytest.raises(ProtectedStorageError):
                recovered_events.hydrate(published.reference)
    finally:
        reopened.close()


def test_archive_fences_without_purging_and_markers_remain_encrypted(world):
    item = enrolled(world)
    populate(item, "completion")
    children, access, envelope, owner, group, location = item
    for path in world[0].rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            assert all(marker.encode("utf-8") not in raw for marker in (INSTRUCTION, ANSWER, REASONING))
    world[1].archive(group.group_id)
    with pytest.raises(ProtectedStorageError):
        access.identity
    assert children.retry_cleanup() == {"cleaned": 0, "retained": 1}
    assert (Path(location["envelopes"]) / access.task_id).exists()


def test_late_writer_loses_to_durable_revoke(world):
    item = enrolled(world)
    children, access, envelope, owner, group, location = item
    selected = SQLiteCompletionStore(location["completion"], protected=access)
    waiting, release = threading.Event(), threading.Event()
    errors = []
    def writer():
        waiting.set()
        release.wait(5)
        try:
            selected.create_started_run(TaskRun(envelope=envelope, state=TaskState.RUNNING, version=1))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=writer)
    thread.start()
    assert waiting.wait(5)
    access.registry.revoke(group.group_id, access.task_id)
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ProtectedStorageError)
    selected.close()
    assert_clean(item)


def test_private_rows_cannot_be_backfilled_into_catalog(world):
    children, access, envelope, owner, group, _ = enrolled(world, enroll=False)
    completion, envelopes = children._locations(envelope.project_id)
    TaskEnvelopeStore(completion.parent.parent, protected=access).commit(envelope)
    with pytest.raises(ProtectedStorageError, match="precede"):
        children.register(access)
    assert children._read(access.task_id) is None


def test_unclaimed_sql_row_keeps_obligation_for_explicit_repair(world):
    item = enrolled(world)
    populate(item, "started")
    children, access, envelope, owner, group, location = item
    with children.registry.db.transaction() as conn:
        conn.execute("DELETE FROM studycrew_protected_records WHERE task_id=? AND table_name='task_runs'",
                     (access.task_id,))
    with pytest.raises(ProtectedStorageError, match="ownership"):
        access.registry.revoke(group.group_id, access.task_id)
    assert children._read(access.task_id) == location
    with closing(sqlite3.connect(location["completion"])) as conn, conn:
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (access.task_id,)).fetchone()[0] == 1


def test_partial_initialization_preserves_unrelated_sql_and_no_body_metadata(world):
    item = enrolled(world)
    children, access, envelope, owner, group, location = item
    path = Path(location["completion"])
    path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE unrelated (content TEXT)")
        conn.execute("INSERT INTO unrelated VALUES ('Legacy')")
    access.registry.revoke(group.group_id, access.task_id)
    assert_clean(item)
    with closing(sqlite3.connect(path)) as conn, conn:
        assert conn.execute("SELECT content FROM unrelated").fetchone()[0] == "Legacy"


def test_orphan_sidecar_requires_recovery_instead_of_false_retirement(world):
    item = enrolled(world)
    children, access, envelope, owner, group, location = item
    path = Path(location["completion"])
    path.parent.mkdir(parents=True)
    sidecar = Path(str(path) + "-wal")
    sidecar.write_bytes(b"unrecovered")
    with pytest.raises(ProtectedStorageError, match="sidecar"):
        access.registry.revoke(group.group_id, access.task_id)
    assert children._read(access.task_id) == location
    assert sidecar.read_bytes() == b"unrecovered"


@pytest.mark.parametrize("stage", ["started", "completion"])
def test_unrevoked_reopen_only_reads_existing_children(world, stage):
    item = enrolled(world)
    populate(item, stage)
    children, access, envelope, owner, group, location = item
    with closing(sqlite3.connect(location["completion"])) as conn, conn:
        before = tuple(conn.iterdump())
    with world[2].store.db.transaction(immediate=False) as conn:
        before_claims = [tuple(row) for row in conn.execute("SELECT * FROM studycrew_protected_records ORDER BY store_id,table_name,record_id")]
    world[2].close()
    runtime = TaskRuntimeService(world[0], GroupRepository(world[0]), card_master_key=world[3])
    try:
        assert runtime.store.get_run(owner.run_id).status is TaskStatus.PAUSED
        registry = ProtectedLineages(runtime)
        reader = registry.paused_history(group.group_id, access.task_id)
        envelopes = TaskEnvelopeStore(Path(location["completion"]).parent.parent, protected=reader)
        assert envelopes.get(access.task_id, envelope.attempt_id).goal == INSTRUCTION
        with SQLiteCompletionStore(location["completion"], protected=reader) as selected:
            if stage == "completion":
                assert selected.list_active()[0].delivery_record["text"] == ANSWER
        with closing(sqlite3.connect(location["completion"])) as conn, conn:
            assert tuple(conn.iterdump()) == before
        with runtime.store.db.transaction(immediate=False) as conn:
            assert [tuple(row) for row in conn.execute("SELECT * FROM studycrew_protected_records ORDER BY store_id,table_name,record_id")] == before_claims
        assert runtime.recovery_report["protected_children"] == {"cleaned": 0, "retained": 1}
    finally:
        runtime.close()


def test_running_quarantine_keeps_unrevoked_content_healthy_group_recovers(world, caplog):
    item = enrolled(world)
    populate(item, "started")
    children, access, envelope, owner, group, location = item
    healthy = world[1].create_group("Healthy")
    healthy_owner = world[2].submit(healthy.group_id, title="Healthy", request_key="healthy")
    world[2].schedule()
    assert world[2].store.get_run(healthy_owner.run_id).status is TaskStatus.RUNNING
    world[2].store.prepare_group_deletion(group.group_id, "invalid-proof",
        group_proof_digest="invalid", confirmation_token="invalid", runtime_project_id=envelope.project_id)
    with closing(sqlite3.connect(location["completion"])) as conn, conn:
        before = tuple(conn.iterdump())
    world[2].close()
    reopened = TaskRuntimeService(world[0], GroupRepository(world[0]), card_master_key=world[3])
    try:
        assert reopened.store.get_run(owner.run_id).status is TaskStatus.RUNNING
        assert reopened.store.get_run(healthy_owner.run_id).status is TaskStatus.PAUSED
        assert reopened.recovery_report["fenced_runs"] == [{"run_id": owner.run_id, "reason": "group_quarantine_fence"}]
        assert reopened.recovery_report["paused_runs"] == [healthy_owner.run_id]
        with closing(sqlite3.connect(location["completion"])) as conn, conn:
            assert tuple(conn.iterdump()) == before
        assert ProtectedChildren.existing(reopened)._read(access.task_id) == location
        with pytest.raises(ProtectedStorageError):
            ProtectedLineages(reopened).access(group.group_id, access.task_id)
        assert "group_quarantine_fence" in caplog.text
    finally:
        reopened.close()


def test_recovery_new_fence_race_is_reported_unknown_store_error_propagates(world, monkeypatch):
    from backend.task_runtime.store import RuntimeStoreError
    item = enrolled(world)
    children, access, envelope, owner, group, location = item
    runtime = world[2]
    original = runtime.store.change_run
    def raced(*args, **kwargs):
        runtime.store.prepare_group_deletion(group.group_id, "race-proof",
            group_proof_digest="invalid", confirmation_token="invalid", runtime_project_id=envelope.project_id)
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime.store, "change_run", raced)
    report = runtime.recover_after_restart()
    assert report["paused_runs"] == []
    assert report["fenced_runs"] == [{"run_id": owner.run_id, "reason": "group_deletion_fence"}]
    healthy = world[1].create_group("Other")
    monkeypatch.setattr(runtime.store, "change_run", original)
    healthy_owner = runtime.submit(healthy.group_id, title="Other", request_key="other")
    runtime.schedule()
    def unavailable(*args, **kwargs):
        raise RuntimeStoreError("unclassified diagnostic")
    monkeypatch.setattr(runtime.store, "change_run", unavailable)
    with pytest.raises(RuntimeStoreError, match="unclassified"):
        runtime.recover_after_restart()


def test_enrollment_rejects_read_only_and_foreign_runtime(world):
    item = enrolled(world)
    populate(item, "started")
    children, access, envelope, owner, group, location = item
    world[2].pause(group.group_id, owner.run_id, request_key="pause")
    reader = access.registry.paused_history(group.group_id, access.task_id)
    with pytest.raises(ProtectedStorageError, match="live selected"):
        children.register(reader)
    with pytest.raises(ProtectedStorageError, match="live selected"):
        children.register(object())


def test_queued_continuation_recovery_respects_quarantine_fence(world):
    groups, runtime = world[1:3]
    group = groups.create_group("Queued")
    owner = runtime.submit(group.group_id, title="Queued", request_key="queued")
    continuation_id = opaque_id("continuation")
    with runtime.store.db.transaction() as conn:
        runtime.store.insert_executor_continuation_in_transaction(conn,
            continuation_id=continuation_id, run=owner, approval_id=None,
            execution_key="seeded", route_request_key="seeded", risk=RiskLevel.HIGH,
            status="claimed", private_request={})
    runtime.store.prepare_group_deletion(group.group_id, "invalid-proof",
        group_proof_digest="invalid", confirmation_token="invalid", runtime_project_id="project_20261004000001")
    runtime.reconcile_group_deletions()
    report = runtime.recover_after_restart()
    assert report["executor_continuations"]["recovered_blocked"] == [continuation_id]
    assert report["fenced_runs"] == [{"run_id": owner.run_id, "reason": "group_quarantine_fence"}]
    assert runtime.store.get_run(owner.run_id).status is TaskStatus.QUEUED


def test_recovery_never_logs_malformed_runtime_identity(world, caplog):
    import json
    children, access, envelope, owner, group, location = enrolled(world)
    raw = world[2].store.get_run(owner.run_id).to_dict()
    raw["run_id"] = "malformed-log-identity-UNTRUSTED"
    with world[2].store.db.transaction() as conn:
        conn.execute("UPDATE studycrew_task_runs SET payload_json=? WHERE run_id=?",
                     (json.dumps(raw), owner.run_id))
    with pytest.raises(ValueError):
        world[2].recover_after_restart()
    assert "malformed-log-identity-UNTRUSTED" not in caplog.text


@pytest.mark.asyncio
async def test_shared_store_events_legacy_and_highwater_survive_selected_cleanup(world):
    first = enrolled(world)
    _, completion = populate(first, "completion")
    children, access, envelope, owner, group, location = first
    authority = ProtectedEvents(access.registry)
    authority.attach(access, location["completion"])
    disk = Store(world[0] / "events")
    hub = Hub(disk)
    hub.bind_protected_events(authority)
    with SQLiteCompletionStore(location["completion"], protected=access) as selected:
        delivery_id = next(row.outbox_id for row in selected.pending_outbox() if row.projection_kind is ProjectionKind.DELIVERY)
    first_event = await hub.emit_protected(access, delivery_id)
    world[2].finalize_trusted_execution(group.group_id, owner.run_id, execution_key="fixture", outcome=TaskStatus.SUCCEEDED,
        evidence=(EvidenceRef("completion", completion.completion_id, "1", completion.completion_id),))
    second = enrolled(world, group=group)
    _, second_completion = populate(second, "completion")
    authority.attach(second[1], location["completion"])
    with SQLiteCompletionStore(location["completion"], protected=second[1]) as selected:
        next_id = next(row.outbox_id for row in selected.pending_outbox() if row.projection_kind is ProjectionKind.DELIVERY)
    legacy_envelope = TaskEnvelope(task_id="legacy-task", attempt_id="legacy-attempt",
        project_id=envelope.project_id, goal="Legacy retained", worker_id="legacy")
    with SQLiteCompletionStore(location["completion"], legacy_lineages=access.registry) as legacy:
        legacy.create_started_run(TaskRun(envelope=legacy_envelope, state=TaskState.RUNNING, version=1))
    legacy_event = await hub.emit(envelope.project_id, {"type": "message", "content": "Legacy retained"})
    last_event = await hub.emit_protected(second[1], next_id)
    assert [first_event.reference["seq"], legacy_event["seq"], last_event.reference["seq"]] == [1, 2, 3]
    second[1].registry.revoke(group.group_id, second[1].task_id)
    assert_clean(second)
    with pytest.raises(ProtectedStorageError):
        authority.hydrate(last_event.reference)
    assert authority.hydrate(first_event.reference)["content"] == ANSWER
    with SQLiteCompletionStore(location["completion"], legacy_lineages=access.registry) as legacy:
        assert legacy.active_attempts()[0].task_id == legacy_envelope.task_id
    disk.close()
    world[2].close()
    runtime = TaskRuntimeService(world[0], GroupRepository(world[0]), card_master_key=world[3])
    try:
        disk = Store(world[0] / "events")
        next_hub = Hub(disk)
        next_hub.bind_protected_events(ProtectedEvents.existing(runtime))
        assert (await next_hub.emit(envelope.project_id, {"type": "message", "content": "Later"}))["seq"] == 4
        disk.close()
    finally:
        runtime.close()
