from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from datetime import datetime, timedelta, timezone

import pytest

from backend.groups import GroupRepository
from backend.knowe_rag.crypto import MasterKey
from backend.task_runtime import TaskRuntimeService
from backend.task_runtime.bound_cards import CardProposal, CardStateError
from backend.task_runtime.contracts import TaskStatus
from backend.task_runtime.contracts import RiskLevel


@pytest.fixture
def cards(tmp_path):
    groups = GroupRepository(tmp_path)
    group = groups.create_group("Cards", optional_roles=("planning",))
    key = MasterKey(os.urandom(32))
    runtime = TaskRuntimeService(tmp_path, groups, card_master_key=key)
    run = runtime.submit(group.group_id, title="Proposal", request_key="owner")
    runtime.schedule()
    try:
        yield groups, group, runtime, run, key
    finally:
        runtime.close()


def create(cards, **kwargs):
    _, group, runtime, run, _ = cards
    return runtime.cards.create(
        group.group_id, run.run_id, task_id=run.task_id,
        proposal=CardProposal(target_role="planning", instruction="秘密指令-ASCII-741"),
        request_key="create-card", **kwargs,
    )


def test_encrypted_atomic_pending_and_restart(cards, tmp_path, caplog):
    groups, group, runtime, run, key = cards
    events = []
    runtime.add_event_listener(events.append)
    card = create(cards)
    assert card.content_generation == card.state_version == 1
    assert runtime.store.get_run(run.run_id).status is TaskStatus.WAITING_APPROVAL
    approval = runtime.store.get_approval(card.approval_id)
    assert approval.purpose == "card_dispatch"
    public = json.dumps([approval.to_dict(), runtime.group_snapshot(group.group_id),
                         [event.public_payload for event in events]], ensure_ascii=False)
    assert card.proposal.instruction not in public + caplog.text
    for path in Path(runtime.store.db.path).parent.glob("task-runtime-v1.sqlite3*"):
        assert card.proposal.instruction.encode("utf-8") not in path.read_bytes()
    assert runtime.store.grant_for_approval(card.approval_id) is None
    runtime.close()
    restarted = TaskRuntimeService(tmp_path, groups, card_master_key=key)
    try:
        assert restarted.cards.read(group.group_id, card.approval_id) == card
    finally:
        restarted.close()


def test_amend_and_exact_resolve(cards):
    _, group, runtime, _, _ = cards
    old = create(cards)
    new = runtime.cards.amend(
        group.group_id, old.approval_id, expected_generation=1,
        expected_digest=old.proposal_digest, request_key="amend-one",
        proposal=CardProposal(target_role="planning", instruction="新秘密-NEW-482"),
    )
    assert new.content_generation == new.state_version == 2
    with pytest.raises(CardStateError):
        runtime.cards.resolve(group.group_id, old.approval_id, expected_generation=1,
                              expected_digest=old.proposal_digest, approved=True, request_key="stale")
    assert runtime.store.grant_for_approval(old.approval_id) is None
    result = runtime.cards.resolve(group.group_id, new.approval_id, expected_generation=2,
                                   expected_digest=new.proposal_digest, approved=True, request_key="approve")
    assert result.grant.operation_scope["proposal_digest"] == new.proposal_digest
    assert result.grant.single_use
    assert result.run.status is TaskStatus.QUEUED
    assert runtime.store.list_runs() == (result.run,)


def test_generic_resolver_cannot_approve(cards):
    _, group, runtime, _, _ = cards
    card = create(cards)
    with pytest.raises(Exception, match="version"):
        runtime.resolve_approval(group.group_id, card.approval_id, approved=True, request_key="generic")
    assert runtime.store.grant_for_approval(card.approval_id) is None


@pytest.mark.parametrize("field,value", [("instruction", "x" * 16385), ("instruction", ""),
    ("instruction", "\ud800"), ("target_role", "arbitrary-worker"), ("title", "x" * 257)])
def test_bounded_proposal_validation(field, value):
    with pytest.raises(CardStateError):
        CardProposal(**{"target_role": "planning", "instruction": "hello", field: value})


def test_owner_identity_not_backfilled(cards):
    _, group, runtime, run, _ = cards
    with pytest.raises(CardStateError):
        runtime.cards.create(group.group_id, run.run_id, task_id="tsk_" + "0" * 32,
                             proposal=CardProposal("planning", "hello"), request_key="bad-owner")
    assert runtime.store.pending_approvals(group.group_id) == ()


def test_amend_approval_race(cards):
    _, group, runtime, _, _ = cards
    card = create(cards)
    barrier = threading.Barrier(3)
    outcomes = []
    def change():
        barrier.wait()
        try:
            runtime.cards.amend(group.group_id, card.approval_id, expected_generation=1,
                expected_digest=card.proposal_digest, request_key="race-amend",
                proposal=CardProposal("planning", "changed"))
            outcomes.append("amended")
        except CardStateError:
            outcomes.append("amend-denied")
    def approve():
        barrier.wait()
        try:
            runtime.cards.resolve(group.group_id, card.approval_id, expected_generation=1,
                expected_digest=card.proposal_digest, request_key="race-approve", approved=True)
            outcomes.append("approved")
        except CardStateError:
            outcomes.append("approve-denied")
    workers = [threading.Thread(target=change), threading.Thread(target=approve)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(5)
        assert not worker.is_alive()
    assert sorted(outcomes) in [["amend-denied", "approved"], ["amended", "approve-denied"]]


def raw_card(runtime, card):
    with runtime.store.db.transaction() as conn:
        return dict(conn.execute("SELECT * FROM studycrew_approvals WHERE approval_id=?",
                                 (card.approval_id,)).fetchone())


def assert_no_effect(runtime, card):
    assert runtime.store.grant_for_approval(card.approval_id) is None
    with runtime.store.db.transaction() as conn:
        for table in ("studycrew_tool_invocations", "studycrew_executor_continuations"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert len(runtime.store.list_runs()) == 1


@pytest.mark.parametrize("key_mode", ["missing", "wrong"])
def test_key_failure_never_rewrites_ciphertext(cards, tmp_path, key_mode):
    groups, group, runtime, _, _ = cards
    card = create(cards)
    before = raw_card(runtime, card)
    reopened = TaskRuntimeService(tmp_path, groups, card_master_key=None if key_mode == "missing" else MasterKey(os.urandom(32)))
    try:
        for operation in [
            lambda: reopened.cards.read(group.group_id, card.approval_id),
            lambda: reopened.cards.amend(group.group_id, card.approval_id, expected_generation=1,
                expected_digest=card.proposal_digest, proposal=CardProposal("planning", "overwrite"), request_key="bad-key"),
            lambda: reopened.cards.resolve(group.group_id, card.approval_id, expected_generation=1,
                expected_digest=card.proposal_digest, approved=True, request_key="bad-approve"),
        ]:
            with pytest.raises(CardStateError):
                operation()
        assert raw_card(runtime, card) == before
        assert_no_effect(runtime, card)
    finally:
        reopened.close()


@pytest.mark.parametrize("tamper", ["sentinel_missing", "sentinel_corrupt", "store_id", "key_id",
    "version", "generation", "epoch", "envelope", "field", "ciphertext", "expiry", "task"])
def test_authenticated_metadata_tampering_denies_without_overwrite(cards, tamper):
    _, group, runtime, run, key = cards
    card = create(cards)
    with runtime.store.db.transaction() as conn:
        if tamper == "sentinel_missing":
            conn.execute("DELETE FROM studycrew_card_crypto")
        elif tamper == "sentinel_corrupt":
            conn.execute("UPDATE studycrew_card_crypto SET sentinel=?", (b"broken",))
        elif tamper == "store_id":
            conn.execute("UPDATE studycrew_card_crypto SET store_id='other-store'")
        elif tamper in {"version", "generation", "epoch", "envelope"}:
            column = {"version": "card_state_version", "generation": "card_generation", "epoch": "card_epoch", "envelope": "card_crypto_version"}[tamper]
            conn.execute(f"UPDATE studycrew_approvals SET {column}={column}+1")
        elif tamper == "key_id":
            conn.execute("UPDATE studycrew_approvals SET card_key_id='other-key'")
        elif tamper == "field":
            conn.execute("UPDATE studycrew_approvals SET card_state_ciphertext=(SELECT sentinel FROM studycrew_card_crypto)")
        elif tamper == "ciphertext":
            conn.execute("UPDATE studycrew_approvals SET card_state_ciphertext=?", (key.seal(b"{}", aad=b"wrong-field"),))
        elif tamper == "task":
            conn.execute("UPDATE studycrew_task_runs SET task_id=? WHERE run_id=?", ("tsk_" + "f" * 32, run.run_id))
        else:
            raw = json.loads(conn.execute("SELECT payload_json FROM studycrew_approvals").fetchone()[0])
            raw["expires_at"] = "2099-01-01T00:00:00Z"
            conn.execute("UPDATE studycrew_approvals SET payload_json=?", (json.dumps(raw),))
    before = raw_card(runtime, card)
    with pytest.raises(CardStateError):
        runtime.cards.read(group.group_id, card.approval_id)
    with pytest.raises(CardStateError):
        runtime.cards.resolve(group.group_id, card.approval_id, expected_generation=1,
            expected_digest=card.proposal_digest, approved=True, request_key="tampered")
    assert raw_card(runtime, card) == before
    assert_no_effect(runtime, card)


def test_cross_row_and_group_substitution(cards):
    groups, group, runtime, _, _ = cards
    first = create(cards)
    second_group = groups.create_group("Second", optional_roles=("planning",))
    owner = runtime.submit(second_group.group_id, title="Second", request_key="second-owner")
    runtime.schedule()
    second = runtime.cards.create(second_group.group_id, owner.run_id, task_id=owner.task_id,
        proposal=CardProposal("planning", "second private text"), request_key="second-card")
    before = raw_card(runtime, first)
    with pytest.raises(CardStateError):
        runtime.cards.read(second_group.group_id, first.approval_id)
    with runtime.store.db.transaction() as conn:
        conn.execute("UPDATE studycrew_approvals SET card_state_ciphertext=? WHERE approval_id=?",
                     (before["card_state_ciphertext"], second.approval_id))
    with pytest.raises(CardStateError):
        runtime.cards.read(second_group.group_id, second.approval_id)
    assert runtime.cards.read(group.group_id, first.approval_id) == first
    assert runtime.store.grant_for_approval(second.approval_id) is None


def test_fresh_nonces_duplicate_conflicts_and_bounded_receipts(cards):
    _, group, runtime, run, _ = cards
    original = create(cards)
    assert create(cards) == original
    before = raw_card(runtime, original)
    proposal = CardProposal("planning", "new private text")
    changed = runtime.cards.amend(group.group_id, original.approval_id, expected_generation=1,
        expected_digest=original.proposal_digest, proposal=proposal, request_key="once")
    assert before["card_state_ciphertext"][:12] != raw_card(runtime, changed)["card_state_ciphertext"][:12]
    assert runtime.cards.amend(group.group_id, original.approval_id, expected_generation=1,
        expected_digest=original.proposal_digest, proposal=proposal, request_key="once") == changed
    with pytest.raises(CardStateError, match="conflicting"):
        runtime.cards.amend(group.group_id, original.approval_id, expected_generation=1,
            expected_digest=original.proposal_digest, proposal=CardProposal("planning", "different"), request_key="once")
    with pytest.raises(CardStateError, match="conflicting"):
        runtime.cards.create(group.group_id, run.run_id, task_id=run.task_id,
            proposal=CardProposal("planning", "different"), request_key="create-card")
    for index in range(61):
        changed = runtime.cards.amend(group.group_id, changed.approval_id,
            expected_generation=changed.content_generation, expected_digest=changed.proposal_digest,
            proposal=CardProposal("planning", str(index)), request_key=f"bounded-{index}")
    assert len(changed.receipts) == 63
    with pytest.raises(CardStateError, match="capacity"):
        runtime.cards.amend(group.group_id, changed.approval_id, expected_generation=changed.content_generation,
            expected_digest=changed.proposal_digest, proposal=proposal, request_key="overflow")
    assert_no_effect(runtime, changed)
    assert runtime.cards.resolve(group.group_id, changed.approval_id,
        expected_generation=changed.content_generation, expected_digest=changed.proposal_digest,
        approved=True, request_key="last-resolution").grant is not None


@pytest.mark.parametrize("status", [state for state in TaskStatus if state is not TaskStatus.WAITING_APPROVAL])
def test_each_invalid_pending_owner_state_denies(cards, status):
    _, group, runtime, run, _ = cards
    card = create(cards)
    runtime.store.change_run(run.run_id, event_type="status_changed", public_payload={"status": status.value},
        idempotency_key="state-fixture", mutate=lambda item: setattr(item, "status", status))
    with pytest.raises(CardStateError):
        runtime.cards.read(group.group_id, card.approval_id)
    with pytest.raises(CardStateError):
        runtime.cards.amend(group.group_id, card.approval_id, expected_generation=1,
            expected_digest=card.proposal_digest, proposal=CardProposal("planning", "denied"), request_key="denied")
    with pytest.raises(CardStateError):
        runtime.cards.resolve(group.group_id, card.approval_id, expected_generation=1,
            expected_digest=card.proposal_digest, approved=True, request_key="denied-approve")
    if status.terminal:
        assert raw_card(runtime, card)["card_state_ciphertext"] is None
    assert_no_effect(runtime, card)


@pytest.mark.parametrize("status", [state for state in TaskStatus if state not in {TaskStatus.ROUTING, TaskStatus.RUNNING}])
def test_invalid_creation_owner_states(cards, status):
    _, group, runtime, run, _ = cards
    runtime.store.change_run(run.run_id, event_type="status_changed", public_payload={"status": status.value},
        idempotency_key="state-fixture", mutate=lambda item: setattr(item, "status", status))
    with pytest.raises(CardStateError):
        create(cards)
    assert runtime.store.pending_approvals(group.group_id) == ()


@pytest.mark.parametrize("revoke", ["archive", "delete", "source", "course", "target", "quarantine"])
def test_real_scope_revocation_scrubs_and_denies(cards, revoke):
    groups, group, runtime, _, _ = cards
    card = create(cards)
    if revoke == "archive":
        groups.archive(group.group_id)
    elif revoke == "delete":
        groups.confirmed_delete(group.group_id, groups.delete_preview(group.group_id).confirmation_token)
    elif revoke == "source":
        groups.link_source(group.group_id)
    elif revoke == "course":
        groups.bind_course(group.group_id, "course-one")
    elif revoke == "target":
        groups.update_roster(group.group_id, [agent for agent in group.roster if agent.role != "planning"])
    else:
        runtime._deferred_quarantines[group.group_id] = "test_quarantine"
    with pytest.raises(CardStateError):
        runtime.cards.read(group.group_id, card.approval_id)
    assert raw_card(runtime, card)["card_state_ciphertext"] is None
    with pytest.raises(CardStateError):
        runtime.cards.resolve(group.group_id, card.approval_id, expected_generation=1,
            expected_digest=card.proposal_digest, approved=True, request_key="revoked")
    assert_no_effect(runtime, card)


def test_reject_duplicate_is_idempotent_and_plaintext_removed(cards):
    _, group, runtime, _, _ = cards
    card = create(cards)
    kwargs = dict(expected_generation=1, expected_digest=card.proposal_digest, approved=False, request_key="reject")
    first = runtime.cards.resolve(group.group_id, card.approval_id, **kwargs)
    second = runtime.cards.resolve(group.group_id, card.approval_id, **kwargs)
    assert first == second
    assert raw_card(runtime, card)["card_state_ciphertext"] is None
    assert_no_effect(runtime, card)


def test_expiry_enforced_on_read_and_resolution(cards, monkeypatch):
    _, group, runtime, _, _ = cards
    card = create(cards)
    # Move only the clock, retaining the real expiry policy and all storage checks.
    from backend.task_runtime import service as service_module
    future = datetime.now(timezone.utc) + timedelta(seconds=601)
    class FutureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future.astimezone(tz) if tz else future.replace(tzinfo=None)
    monkeypatch.setattr(service_module, "datetime", FutureClock)
    with pytest.raises(CardStateError):
        runtime.cards.read(group.group_id, card.approval_id)
    assert raw_card(runtime, card)["card_state_ciphertext"] is None
    with pytest.raises(CardStateError):
        runtime.cards.resolve(group.group_id, card.approval_id, expected_generation=1,
            expected_digest=card.proposal_digest, approved=True, request_key="expired")
    assert_no_effect(runtime, card)


@pytest.mark.parametrize("expiry", ["9999-12-31T00:00:00Z", "2000-01-01T00:00:00Z", "not-a-date"])
def test_finite_expiry_policy(cards, expiry):
    with pytest.raises(CardStateError):
        create(cards, expires_at=expiry)


@pytest.mark.parametrize("failure", ["closed", "missing_table"])
def test_storage_failures_are_typed(cards, failure):
    _, group, runtime, _, _ = cards
    card = create(cards)
    if failure == "closed":
        runtime.close()
    else:
        with runtime.store.db.transaction() as conn:
            conn.execute("DROP TABLE studycrew_card_crypto")
    with pytest.raises(CardStateError):
        runtime.cards.read(group.group_id, card.approval_id)


@pytest.mark.parametrize("reserved", ["approval_id", "group_id", "owner_task_id", "purpose", "state_version", "feedback_epoch", "operation_scope"])
def test_input_cannot_overwrite_private_authority(reserved):
    with pytest.raises(CardStateError):
        CardProposal.from_payload({"target_role": "planning", "instruction": "hello", reserved: "forged"})


@pytest.mark.parametrize("mutation", ["archive", "delete", "unlink", "unbind"])
def test_real_group_repositories_share_admission_fence(tmp_path, mutation):
    one = GroupRepository(tmp_path)
    group = one.create_group("Fence")
    link = one.link_source(group.group_id)
    one.bind_course(group.group_id, "course")
    two = GroupRepository(tmp_path / ".")
    attempted, finished = threading.Event(), threading.Event()
    errors = []
    def change():
        attempted.set()
        try:
            if mutation == "archive":
                two.archive(group.group_id)
            elif mutation == "delete":
                two.prepare_delete(group.group_id, two.delete_preview(group.group_id).confirmation_token)
            elif mutation == "unlink":
                two.unlink_source(group.group_id, link.source_id)
            else:
                two.unbind_course(group.group_id, "course")
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()
    with one.admission_guard():
        worker = threading.Thread(target=change)
        worker.start()
        assert attempted.wait(2)
        assert not finished.wait(0.05)
        assert one.get(group.group_id).lifecycle.value == "active"
        assert one.get(group.group_id).source_links
        assert one.get(group.group_id).course_bindings
    worker.join(5)
    assert finished.is_set() and not errors


def test_deletion_listener_runs_after_registry_guard_release(tmp_path):
    one, two = GroupRepository(tmp_path), GroupRepository(tmp_path)
    group = one.create_group("Listener")
    observed = []
    def listener():
        acquired = threading.Event()
        def read():
            with two.admission_guard():
                acquired.set()
        thread = threading.Thread(target=read)
        thread.start()
        observed.append(acquired.wait(2))
        thread.join(2)
    one.deletion_prepare_listener = listener
    one.prepare_delete(group.group_id, one.delete_preview(group.group_id).confirmation_token)
    assert observed == [True]


@pytest.mark.parametrize("winner", ["amend", "approve"])
def test_transaction_barrier_deterministically_selects_one_winner(cards, tmp_path, monkeypatch, winner):
    groups, group, runtime, _, key = cards
    card = create(cards)
    other = TaskRuntimeService(tmp_path, groups, card_master_key=key)
    entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
    real_write = runtime.store.card_states.write
    def barrier_write(*args):
        real_write(*args)
        entered.set()
        assert release.wait(4)
    monkeypatch.setattr(runtime.store.card_states, "write", barrier_write)
    outcomes = []
    def action(service, which):
        try:
            if service is other:
                attempted.set()
            kwargs = dict(expected_generation=1, expected_digest=card.proposal_digest, request_key=which)
            if which == "amend":
                service.cards.amend(group.group_id, card.approval_id, proposal=CardProposal("planning", "race-new"), **kwargs)
            else:
                service.cards.resolve(group.group_id, card.approval_id, approved=True, **kwargs)
            outcomes.append(which)
        except CardStateError:
            outcomes.append(which + "-denied")
    loser = "approve" if winner == "amend" else "amend"
    first = threading.Thread(target=action, args=(runtime, winner))
    second = threading.Thread(target=action, args=(other, loser))
    try:
        first.start()
        assert entered.wait(3)
        second.start()
        assert attempted.wait(3)
        release.set()
        first.join(5)
        second.join(5)
        assert not first.is_alive() and not second.is_alive()
        assert set(outcomes) == {winner, loser + "-denied"}
        current = runtime.cards.read(group.group_id, card.approval_id)
        grant = runtime.store.grant_for_approval(card.approval_id)
        if winner == "amend":
            assert current.content_generation == 2 and grant is None
        else:
            assert current.content_generation == 1
            assert grant.operation_scope["proposal_digest"] == card.proposal_digest
    finally:
        release.set()
        other.close()


def test_card_publication_occurs_after_all_admission_locks_release(cards, tmp_path):
    _, group, runtime, _, _ = cards
    second_repo = GroupRepository(tmp_path)
    acquired = []
    def listener(event):
        observed = threading.Event()
        def inspect():
            with runtime._lock, second_repo.admission_guard():
                observed.set()
        worker = threading.Thread(target=inspect)
        worker.start()
        acquired.append(observed.wait(1))
        worker.join(1)
    runtime.add_event_listener(listener)
    create(cards)
    assert acquired == [True]


def test_unbound_legacy_never_backfills_and_old_runtime_needs_no_key(tmp_path):
    groups = GroupRepository(tmp_path)
    group = groups.create_group("Legacy", optional_roles=("planning",))
    runtime = TaskRuntimeService(tmp_path, groups)
    try:
        run = runtime.submit(group.group_id, title="Legacy", request_key="legacy-owner")
        runtime.schedule()
        before = runtime.store.get_run(run.run_id)
        with pytest.raises(CardStateError):
            runtime.cards.create(group.group_id, run.run_id, task_id=run.task_id,
                proposal=CardProposal("planning", "no-key"), request_key="denied")
        assert runtime.store.get_run(run.run_id) == before
        approval = runtime.request_approval(group.group_id, run.run_id, kind="legacy", summary="Legacy proposal",
            risk=RiskLevel.HIGH, request_key="legacy-approval")
        with pytest.raises(CardStateError):
            runtime.cards.read(group.group_id, approval.approval_id)
        assert not runtime.store.approval_has_card_state(approval.approval_id)
        assert runtime.resolve_approval(group.group_id, approval.approval_id, approved=True, request_key="legacy-approve")[1]
    finally:
        runtime.close()


def test_create_rollback_emits_nothing_and_retains_owner(cards, monkeypatch):
    _, group, runtime, run, _ = cards
    before = runtime.store.get_run(run.run_id)
    events = []
    runtime.add_event_listener(events.append)
    def unavailable(*args):
        raise CardStateError("injected encryption write failure")
    monkeypatch.setattr(runtime.store.card_states, "write", unavailable)
    with pytest.raises(CardStateError):
        create(cards)
    assert runtime.store.get_run(run.run_id) == before
    assert runtime.store.pending_approvals(group.group_id) == ()
    assert events == []
    with runtime.store.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM studycrew_card_crypto").fetchone()[0] == 0


def test_approved_retry_is_single_grant_and_cannot_amend(cards):
    _, group, runtime, _, _ = cards
    card = create(cards)
    kwargs = dict(expected_generation=1, expected_digest=card.proposal_digest, approved=True, request_key="approve-once")
    one = runtime.cards.resolve(group.group_id, card.approval_id, **kwargs)
    assert runtime.cards.resolve(group.group_id, card.approval_id, **kwargs) == one
    with pytest.raises(CardStateError):
        runtime.cards.amend(group.group_id, card.approval_id, expected_generation=1,
            expected_digest=card.proposal_digest, proposal=CardProposal("planning", "cannot-reuse-grant"), request_key="after-approved")
    assert runtime.store.grant_for_approval(card.approval_id) == one.grant
    with runtime.store.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM studycrew_permission_grants").fetchone()[0] == 1


def test_current_amended_generation_survives_restart(cards, tmp_path):
    groups, group, runtime, _, key = cards
    old = create(cards)
    latest = runtime.cards.amend(group.group_id, old.approval_id, expected_generation=1,
        expected_digest=old.proposal_digest, proposal=CardProposal("planning", "重新启动后的私密指令-29"), request_key="new-generation")
    runtime.close()
    restarted = TaskRuntimeService(tmp_path, groups, card_master_key=key)
    try:
        assert restarted.cards.read(group.group_id, latest.approval_id) == latest
        assert_no_effect(restarted, latest)
    finally:
        restarted.close()


def test_cancel_scrubs_card_and_never_grants(cards):
    _, group, runtime, run, _ = cards
    card = create(cards)
    runtime.cancel(group.group_id, run.run_id, request_key="stop")
    assert raw_card(runtime, card)["card_state_ciphertext"] is None
    with pytest.raises(CardStateError):
        runtime.cards.read(group.group_id, card.approval_id)
    assert_no_effect(runtime, card)


@pytest.mark.parametrize("scope", ["source", "course"])
def test_source_backed_creation_fails_without_real_content_version_fence(cards, scope):
    groups, group, runtime, _, _ = cards
    if scope == "source":
        groups.link_source(group.group_id)
    else:
        groups.bind_course(group.group_id, "course")
    with pytest.raises(CardStateError, match="source version fence"):
        create(cards)
    assert runtime.store.pending_approvals(group.group_id) == ()


@pytest.mark.parametrize("digest,generation", [(None, 1), ("旧摘要", 1), ("0" * 64, 1), ("0" * 64, True), ("0" * 64, None)])
def test_missing_and_invalid_versions_deny_no_grants(cards, digest, generation):
    _, group, runtime, _, _ = cards
    card = create(cards)
    with pytest.raises(CardStateError):
        runtime.cards.resolve(group.group_id, card.approval_id, expected_generation=generation,
            expected_digest=digest, approved=True, request_key="invalid-version")
    assert_no_effect(runtime, card)


@pytest.mark.parametrize("archive_first", [True, False])
def test_real_archive_and_card_transaction_ordering(cards, tmp_path, monkeypatch, archive_first):
    groups, group, runtime, _, _ = cards
    card = create(cards)
    other_groups = GroupRepository(tmp_path)
    entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
    outcomes = []
    if archive_first:
        actual = other_groups._save_payload
        def barrier_save(payload):
            actual(payload)
            entered.set()
            assert release.wait(4)
        monkeypatch.setattr(other_groups, "_save_payload", barrier_save)
    else:
        actual = runtime.store.card_states.write
        def barrier_write(*args):
            actual(*args)
            entered.set()
            assert release.wait(4)
        monkeypatch.setattr(runtime.store.card_states, "write", barrier_write)
    def archive():
        attempted.set()
        other_groups.archive(group.group_id)
        outcomes.append("archived")
    def amend():
        attempted.set()
        try:
            runtime.cards.amend(group.group_id, card.approval_id, expected_generation=1,
                expected_digest=card.proposal_digest, proposal=CardProposal("planning", "new"), request_key="archive-race")
            outcomes.append("amended")
        except CardStateError:
            outcomes.append("denied")
    first = threading.Thread(target=archive if archive_first else amend)
    second = threading.Thread(target=amend if archive_first else archive)
    first.start()
    assert entered.wait(3)
    attempted.clear()
    second.start()
    assert attempted.wait(3)
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert set(outcomes) == {"archived", "denied" if archive_first else "amended"}
    with pytest.raises(CardStateError):
        runtime.cards.read(group.group_id, card.approval_id)
    assert_no_effect(runtime, card)


def test_all_private_generations_absent_from_storage_events_and_logs(cards, caplog):
    _, group, runtime, _, key = cards
    old = create(cards)
    markers = [old.proposal.instruction, "修改后密文-only-NEW-921", "私密展示标题-SHOW-481"]
    proposal = CardProposal("planning", markers[1], title=markers[2])
    new = runtime.cards.amend(group.group_id, old.approval_id, expected_generation=1,
        expected_digest=old.proposal_digest, proposal=proposal, request_key="private-amend")
    assert runtime.cards.read(group.group_id, old.approval_id).proposal.instruction == markers[1]
    records = []
    with runtime.store.db.transaction() as conn:
        for table in ("studycrew_run_events", "studycrew_task_runs", "studycrew_approvals"):
            for row in conn.execute(f"SELECT * FROM {table}").fetchall():
                records.extend(str(value) for value in row if not isinstance(value, bytes))
    public = json.dumps(runtime.group_snapshot(group.group_id), ensure_ascii=False)
    for marker in markers:
        assert marker not in public + caplog.text + "".join(records) + repr(new) + repr(proposal)
        for path in Path(runtime.store.db.path).parent.glob("task-runtime-v1.sqlite3*"):
            data = path.read_bytes()
            assert marker.encode("utf-8") not in data
            assert key.key_material not in data
    assert_no_effect(runtime, new)


def test_generic_resolution_denies_even_if_clear_purpose_tampered(cards):
    _, group, runtime, _, _ = cards
    card = create(cards)
    with runtime.store.db.transaction() as conn:
        raw = json.loads(conn.execute("SELECT payload_json FROM studycrew_approvals").fetchone()[0])
        raw["purpose"] = "confirm_route"
        conn.execute("UPDATE studycrew_approvals SET payload_json=?", (json.dumps(raw),))
    with pytest.raises(Exception, match="version"):
        runtime.resolve_approval(group.group_id, card.approval_id, approved=True, request_key="tampered-generic")
    assert_no_effect(runtime, card)


def test_existing_master_key_alias_is_accepted(tmp_path):
    from knowe_rag.crypto import MasterKey as ExistingMemoryKey
    groups = GroupRepository(tmp_path)
    group = groups.create_group("Existing key", optional_roles=("planning",))
    runtime = TaskRuntimeService(tmp_path, groups, card_master_key=ExistingMemoryKey(os.urandom(32)))
    try:
        run = runtime.submit(group.group_id, title="Key", request_key="key-owner")
        runtime.schedule()
        state = runtime.cards.create(group.group_id, run.run_id, task_id=run.task_id,
            proposal=CardProposal("planning", "same existing key capability"), request_key="alias-key")
        assert runtime.cards.read(group.group_id, state.approval_id) == state
    finally:
        runtime.close()


@pytest.mark.parametrize("field,expected_opens", [("sentinel", 0), ("card_state_ciphertext", 1)])
def test_corrupt_scalar_storage_is_rejected_before_blob_conversion(cards, monkeypatch, field, expected_opens):
    _, group, runtime, _, _ = cards
    card = create(cards)
    with runtime.store.db.transaction() as conn:
        table = "studycrew_card_crypto" if field == "sentinel" else "studycrew_approvals"
        conn.execute(f"UPDATE {table} SET {field}=64")
    calls = []
    real_open = MasterKey.open
    def observed_open(self, blob, *, aad):
        calls.append(len(blob))
        return real_open(self, blob, aad=aad)
    monkeypatch.setattr(MasterKey, "open", observed_open)
    with pytest.raises(CardStateError):
        runtime.cards.read(group.group_id, card.approval_id)
    assert len(calls) == expected_opens


@pytest.mark.parametrize("text", ["14143976714", "gag_931dd5c0d0d14143976714bcd4f4cbd6 phone 14143976714",
    "gag_931dd5c0d0d14143976714bcd4f4cbd6text", "gag_931dd5c0d0d14143976714bcd4f4cbdZ"])
def test_group_agent_opaque_id_exception_does_not_admit_phone_text(text):
    from backend.task_runtime.contracts import assert_public_payload
    with pytest.raises(ValueError, match="PII"):
        assert_public_payload("agent_phase_changed", {"agent_id": text})
