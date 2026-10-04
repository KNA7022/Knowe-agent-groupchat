"""Private, explicit storage capability for instruction-only governed lineage.

No execution or approval authority is issued here. Repository entry points retain
the lifecycle guard before opening their own SQLite transaction. Ciphertext never
selects its own row identity, group, table, field or store.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager, nullcontext
from functools import wraps
import hmac
import json
from pathlib import Path
from datetime import datetime
import re
from typing import Any
import uuid

class ProtectedStorageError(RuntimeError):
    """Deliberately content-free protected storage failure."""


PREFIX = "p1_"
WIRE = "p1:"
MAX_BYTES = 2 * 1024 * 1024
_OPAQUE = r"p1_(?:task_|attempt_|journal_|result_|out_|aco_|wait_|cmp_|cmpkey_|effect_|dec_|decision_?|runhist_|delivery_|deliverykey_)(?:[0-9a-f]{24}|[0-9a-f]{32})"
_ID = re.compile(_OPAQUE + r"(?::" + _OPAQUE + r")?\Z")


def protected_id(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def bounded_json(value: Any) -> bytes:
    total = 0
    def validate(item, depth=0):
        nonlocal total
        total += 2
        if depth > 32:
            raise ProtectedStorageError("protected payload nesting exceeded")
        if isinstance(item, str):
            if len(item) > MAX_BYTES:
                raise ProtectedStorageError("protected payload limit exceeded")
            total += len(item.encode("utf-8", errors="strict"))
        elif isinstance(item, dict):
            if len(item) > 10000 or any(type(key) is not str for key in item):
                raise ProtectedStorageError("invalid protected object")
            for key, child in item.items():
                validate(key, depth + 1)
                validate(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            if len(item) > 10000:
                raise ProtectedStorageError("protected array limit exceeded")
            for child in item:
                validate(child, depth + 1)
        elif item is not None and type(item) not in (bool, int, float):
            raise ProtectedStorageError("invalid protected value type")
        if total > MAX_BYTES:
            raise ProtectedStorageError("protected payload limit exceeded")
    try:
        validate(value)
        raw = canonical(value).encode("utf-8", errors="strict")
    except (UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        raise ProtectedStorageError("invalid protected UTF-8 payload") from None
    if len(raw) > MAX_BYTES:
        raise ProtectedStorageError("protected payload limit exceeded")
    return raw


def storage_boundary(method):
    """Repository method decorator; acquire lifecycle locks before repository SQL."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        protection = getattr(self, "protection", None)
        with protection.guard() if protection is not None else nullcontext():
            return method(self, *args, **kwargs)
    return guarded


class ProtectedLineages:
    def __init__(self, runtime):
        from backend.task_runtime.service import TaskRuntimeService
        if not isinstance(runtime, TaskRuntimeService):
            raise ProtectedStorageError("canonical runtime required")
        self.runtime = runtime
        self.db = runtime.store.db
        self.crypto = runtime.store.card_states
        try:
            with self.db.transaction(immediate=False) as conn:
                self.crypto.verify(conn)
        except (CardStateError, MasterKeyError):
            raise ProtectedStorageError("protected encryption capability unavailable") from None
        self.db.initialize("""
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS studycrew_protected_lineages (
                task_id TEXT PRIMARY KEY, group_id TEXT NOT NULL,
                state TEXT NOT NULL, payload BLOB, receipt TEXT NOT NULL,
                approval_id TEXT NOT NULL, generation INTEGER NOT NULL,
                UNIQUE(approval_id,generation)
            );
            CREATE TABLE IF NOT EXISTS studycrew_protected_records (
                store_id TEXT NOT NULL, table_name TEXT NOT NULL,
                record_id TEXT NOT NULL, task_id TEXT NOT NULL, mac TEXT NOT NULL,
                PRIMARY KEY(store_id, table_name, record_id)
            );
        """)

    def _aad(self, meta, task_id, group_id, state):
        return field_aad(canonical(["studycrew.protected-lineage", 1,
            meta["store_id"], meta["key_id"], task_id, group_id, state]), "binding")

    def _read(self, conn, task_id, group_id, *, revoked=False):
        meta = self.crypto.verify(conn)
        row = conn.execute("SELECT * FROM studycrew_protected_lineages WHERE task_id=?", (task_id,)).fetchone()
        if row is None or row["group_id"] != group_id:
            raise ProtectedStorageError("protected lineage unavailable")
        expected = self.crypto.digest("lineage-receipt", [task_id, group_id, row["state"]])
        if not hmac.compare_digest(expected, row["receipt"]):
            raise ProtectedStorageError("protected lineage authentication failed")
        if row["state"] != "active":
            if revoked and row["state"] == "revoked" and row["payload"] is None:
                return None
            raise ProtectedStorageError("protected lineage revoked")
        blob = row["payload"]
        if not isinstance(blob, bytes) or not 28 <= len(blob) <= 65536:
            raise ProtectedStorageError("invalid protected binding")
        try:
            raw = self.crypto._key.open(blob, aad=self._aad(meta, task_id, group_id, "active"))
            data = json.loads(raw.decode("utf-8", errors="strict"))
            if bounded_json(data) != raw or data["task_id"] != task_id or data["group_id"] != group_id:
                raise ProtectedStorageError("protected binding mismatch")
            if (data["approval_id"], data["generation"]) != (row["approval_id"], row["generation"]):
                raise ProtectedStorageError("protected card binding mismatch")
        except (MasterKeyError, UnicodeError, ValueError, KeyError, TypeError):
            raise ProtectedStorageError("protected binding authentication failed") from None
        return data

    def register(self, group_id: str, approval_id: str, *, generation: int, digest: str):
        cards = self.runtime.cards
        with cards._admission(group_id, approval_id) as (group, revision):
            if group.source_links or group.course_bindings:
                raise ProtectedStorageError("protected storage requires a source-free group")
            with self.db.transaction() as conn:
                row, owner, approval, card = cards._current(conn, group, approval_id, revision)
                cards._match(card, generation, digest)
                if card.proposal.target_role not in {"knowledge", "planning", "coach"}:
                    raise ProtectedStorageError("unsupported protected Worker target")
                prior = conn.execute("SELECT task_id FROM studycrew_protected_lineages WHERE approval_id=? AND generation=?",
                    (approval_id, generation)).fetchone()
                if prior is not None:
                    self._read(conn, prior["task_id"], group_id)
                    return ProtectedAccess(self, group_id, prior["task_id"])
                # Registration allocates storage identities only; no approval is resolved.
                task_id = PREFIX + "task_" + uuid.uuid4().hex
                payload = {
                    "task_id": task_id, "attempt_id": PREFIX + "attempt_" + uuid.uuid4().hex,
                    "group_id": group_id, "project_id": group.legacy_project_id or group_id,
                    "owner_task_id": owner.task_id, "owner_run_id": owner.run_id,
                    "approval_id": approval_id, "generation": generation,
                    "proposal_digest": digest, "scope_revision": revision,
                    "worker_id": card.proposal.target_agent_id, "worker_role": card.proposal.target_role,
                    "goal_digest": self.crypto.digest("protected-goal", card.proposal.instruction),
                    "worker_run_id": "",
                }
                meta = self.crypto.verify(conn)
                blob = self.crypto._key.seal(bounded_json(payload), aad=self._aad(meta, task_id, group_id, "active"))
                receipt = self.crypto.digest("lineage-receipt", [task_id, group_id, "active"])
                conn.execute("INSERT INTO studycrew_protected_lineages VALUES (?,?,?,?,?,?,?)",
                    (task_id, group_id, "active", blob, receipt, approval_id, generation))
        return self.access(group_id, task_id)

    def access(self, group_id: str, task_id: str):
        access = ProtectedAccess(self, group_id, task_id)
        with access.guard():
            pass
        return access

    def revoke(self, group_id: str, task_id: str):
        # No content reads are allowed after this durable fence. Purging dependent
        # stores is a separate idempotent step, so interrupted cleanup stays denied.
        with self.runtime._lock:
            with self.db.transaction() as conn:
                self._read(conn, task_id, group_id, revoked=True)
                receipt = self.crypto.digest("lineage-receipt", [task_id, group_id, "revoked"])
                conn.execute("UPDATE studycrew_protected_lineages SET state='revoked',payload=NULL,receipt=? WHERE task_id=?",
                    (receipt, task_id))
        return {"task_id": task_id, "state": "revoked"}


class ProtectedAccess:
    """Explicit same-group private capability, rechecked for every repository call."""
    def __init__(self, registry, group_id, task_id):
        self.registry, self.group_id, self.task_id = registry, group_id, task_id

    @contextmanager
    def guard(self, *, write=False):
        registry = self.registry
        try:
            with registry.runtime.cards._admission(self.group_id) as (group, revision):
                if group.source_links or group.course_bindings:
                    raise ProtectedStorageError("protected storage requires a source-free group")
                with registry.db.transaction(immediate=False) as conn:
                    data = registry._read(conn, self.task_id, self.group_id)
                    owner = registry.runtime.cards._owner(conn, data["owner_run_id"], self.group_id)
                    if owner.task_id != data["owner_task_id"] or revision != data["scope_revision"]:
                        raise ProtectedStorageError("protected scope changed")
                    if owner.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
                        raise ProtectedStorageError("protected owner revoked")
                    if write and owner.status in {TaskStatus.SUCCEEDED, TaskStatus.PARTIAL}:
                        raise ProtectedStorageError("protected owner is terminal")
                    if owner.status not in {TaskStatus.SUCCEEDED, TaskStatus.PARTIAL}:
                        _, _, _, card = registry.runtime.cards._current(conn, group, data["approval_id"], revision)
                        registry.runtime.cards._match(card, data["generation"], data["proposal_digest"])
                yield data
        except (CardStateError, MasterKeyError):
            raise ProtectedStorageError("protected storage authority unavailable") from None

    @property
    def identity(self):
        with self.guard() as data:
            return dict(data)

    def for_store(self, path):
        store_id = self.registry.crypto.digest("protected-store-path", str(Path(path).resolve()))
        return PayloadCodec(self, store_id)


class PayloadCodec:
    def __init__(self, access: ProtectedAccess, store_id: str):
        self.access, self.store_id = access, store_id

    @contextmanager
    def guard(self):
        with self.access.guard():
            yield

    def _binding(self, *, write=False):
        registry = self.access.registry
        with registry.db.transaction(immediate=False) as conn:
            data = registry._read(conn, self.access.task_id, self.access.group_id)
            owner = registry.runtime.cards._owner(conn, data["owner_run_id"], self.access.group_id)
            if write and owner.status in {TaskStatus.SUCCEEDED, TaskStatus.PARTIAL, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                raise ProtectedStorageError("protected owner cannot write")
            meta = dict(registry.crypto.verify(conn))
        return data, meta

    def bind_worker_run(self, run_id):
        registry = self.access.registry
        with registry.db.transaction() as conn:
            data = registry._read(conn, self.access.task_id, self.access.group_id)
            if data["worker_run_id"] not in ("", run_id):
                raise ProtectedStorageError("protected Worker run identity changed")
            if not data["worker_run_id"]:
                data["worker_run_id"] = run_id
                meta = registry.crypto.verify(conn)
                blob = registry.crypto._key.seal(bounded_json(data), aad=registry._aad(
                    meta, self.access.task_id, self.access.group_id, "active"))
                conn.execute("UPDATE studycrew_protected_lineages SET payload=? WHERE task_id=?", (blob, self.access.task_id))

    def validate_envelope(self, payload):
        data, _ = self._binding(write=True)
        if any(payload.get(name) != data[name] for name in ("task_id", "attempt_id", "project_id", "worker_id")):
            raise ProtectedStorageError("protected envelope identity mismatch")
        if payload.get("worker_role") not in (data["worker_role"], "worker"):
            raise ProtectedStorageError("protected Worker role mismatch")
        if not hmac.compare_digest(self.digest("protected-goal", payload.get("goal")), data["goal_digest"]):
            raise ProtectedStorageError("protected instruction mismatch")
        delivery = payload.get("delivery") or {}
        if (payload.get("context_refs") or payload.get("scope_root") or payload.get("instruction_ref")
            or payload.get("metadata") or delivery.get("metadata")
            or delivery.get("channel") not in (None, "", data["project_id"])):
            raise ProtectedStorageError("protected instruction-only envelope required")
        bounded_json(payload)

    def digest(self, domain, value):
        bounded_json(value)
        return self.access.registry.crypto.digest(domain, value)

    def _claim(self, table, record_id, *, create):
        if not isinstance(record_id, str) or not _ID.fullmatch(record_id) or not protected_id(record_id):
            raise ProtectedStorageError("protected record identity required")
        registry = self.access.registry
        material = [self.store_id, table, record_id, self.access.task_id]
        mac = registry.crypto.digest("protected-record", material)
        with registry.db.transaction() as conn:
            registry._read(conn, self.access.task_id, self.access.group_id)
            row = conn.execute("SELECT * FROM studycrew_protected_records WHERE store_id=? AND table_name=? AND record_id=?",
                material[:3]).fetchone()
            if row is None:
                if not create:
                    raise ProtectedStorageError("protected record claim unavailable")
                conn.execute("INSERT INTO studycrew_protected_records VALUES (?,?,?,?,?)", (*material, mac))
            elif row["task_id"] != self.access.task_id or not hmac.compare_digest(row["mac"], mac):
                raise ProtectedStorageError("protected record binding mismatch")

    def _aad(self, meta, table, record_id, field):
        return field_aad(canonical(["studycrew.protected-payload", 1, meta["store_id"],
            meta["key_id"], self.store_id, self.access.group_id, self.access.task_id,
            table, record_id]), field)

    def seal(self, table, record_id, payload, *, field="payload_json", create=True):
        raw = bounded_json(payload)
        data, meta = self._binding(write=True)
        self._claim(table, record_id, create=create)
        blob = self.access.registry.crypto._key.seal(raw, aad=self._aad(meta, table, record_id, field))
        return WIRE + base64.b64encode(blob).decode("ascii")

    def open(self, table, record_id, wire, *, field="payload_json"):
        if not isinstance(wire, str) or not wire.startswith(WIRE) or len(wire) > (MAX_BYTES + 28) * 4 // 3 + 16:
            raise ProtectedStorageError("invalid protected payload format")
        _, meta = self._binding()
        self._claim(table, record_id, create=False)
        try:
            blob = base64.b64decode(wire[len(WIRE):], validate=True)
            if not 28 <= len(blob) <= MAX_BYTES + 28:
                raise ProtectedStorageError("protected ciphertext limit exceeded")
            raw = self.access.registry.crypto._key.open(blob, aad=self._aad(meta, table, record_id, field))
            payload = json.loads(raw.decode("utf-8", errors="strict"))
            if bounded_json(payload) != raw:
                raise ProtectedStorageError("noncanonical protected payload")
            return payload
        except (MasterKeyError, ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
            raise ProtectedStorageError("protected payload authentication failed") from None


# Exact repository schemas, never a generic JSON/SQLite interceptor. Each caller
# selects its table explicitly; payload identity is checked against independent SQL.
ROW_KEYS = {
    "task_runs": ("task_id",), "task_journal_v1": ("journal_entry_id",),
    "task_attempts_v1": ("task_id", "attempt_id"), "completion_events_v2": ("completion_id",),
    "task_results_v1": ("result_id",), "agent_completion_outcomes_v2": ("outcome_id",),
    "completion_outbox_v2": ("outbox_id",), "completion_projection_receipts_v2": ("effect_id",),
    "wait_tokens_v2": ("wait_token_id",), "deliveries": ("delivery_id",),
    "decision_events_v2": ("decision_id",), "task_run_history_v2": ("history_id",),
}
CONTENT_FIELDS = {
    "task_journal_v1": ("reason",), "task_attempts_v1": ("reason",),
    "agent_completion_outcomes_v2": ("terminal_reason",),
    "completion_outbox_v2": ("last_error",),
    "completion_projection_receipts_v2": ("effect_ref",),
    "wait_tokens_v2": ("question", "dependencies_json", "answer", "answer_ref"),
    "decision_events_v2": ("actor",), "task_run_history_v2": ("reason",),
}
QUERY_FIELDS = (
    "task_id", "attempt_id", "run_id", "project_id", "worker_id", "completion_id",
    "status", "state", "runtime_state", "active", "terminal", "version", "sequence",
    "projection_kind", "route_key", "attempts", "result_id", "decision_type",
    "outbox_id", "idempotency_key", "supersedes_completion_id", "rollback_of_completion_id",
    "retry_of_completion_id", "created_at", "updated_at", "heartbeat_at", "effect_ref",
)


class StoragePayloads:
    """Explicit reusable serializers called at each repository row boundary."""
    protection: PayloadCodec | None

    def _write_payload(self, table, value, *, identity=None, clear=None):
        from backend.knowe_storage._sqlite import json_dumps
        flat = dict(value)
        if table == "task_runs":
            flat.update(value["envelope"])
            flat.update({k: value[k] for k in ("state", "version", "run_id", "updated_at")})
            flat["created_at"] = value["started_at"]
        flat.update(clear or {})
        record_id = identity or ":".join(str(flat[k]) for k in ROW_KEYS[table])
        is_protected = protected_id(record_id) or protected_id(flat.get("task_id"))
        if not is_protected:
            if self.protection is not None:
                raise ProtectedStorageError("protected repository cannot downgrade a write")
            return json_dumps(value)
        if self.protection is None:
            raise ProtectedStorageError("protected repository capability required")
        binding, _ = self.protection._binding(write=True)
        for name in ("task_id", "attempt_id", "project_id", "worker_id"):
            if name in flat and flat[name] != binding[name]:
                raise ProtectedStorageError("protected row lineage mismatch")
        if table == "task_runs":
            self.protection.validate_envelope(value["envelope"])
            if not re.fullmatch(r"[0-9a-f]{32}", value["run_id"]):
                raise ProtectedStorageError("invalid Worker run identity")
            self.protection.bind_worker_run(value["run_id"])
        elif "run_id" in flat and flat["run_id"] != binding["worker_run_id"]:
            raise ProtectedStorageError("protected Worker run mismatch")
        if table in ("completion_events_v2", "deliveries"):
            if value.get("artifact_manifest") or value.get("artifacts"):
                raise ProtectedStorageError("protected file artifacts are unavailable")
        query = {k: flat[k] for k in QUERY_FIELDS if k in flat}
        for name, item in query.items():
            if name in {"created_at", "updated_at", "heartbeat_at"}:
                try:
                    datetime.fromisoformat(item.replace("Z", "+00:00"))
                except (AttributeError, ValueError, TypeError):
                    raise ProtectedStorageError("invalid protected timestamp") from None
            if name in {"completion_id", "result_id", "outbox_id", "supersedes_completion_id",
                        "rollback_of_completion_id", "retry_of_completion_id", "idempotency_key"} and item:
                if not isinstance(item, str) or re.fullmatch(r"(?:completion:)?" + _OPAQUE, item) is None:
                    raise ProtectedStorageError("invalid protected opaque identity")
        # Child claims commit in runtime SQLite before this store's write. They are
        # identity reservations, not evidence that this SQL transaction committed.
        with self.db._lock:
            exists = self.db._connection.execute(
                f"SELECT 1 FROM {table} WHERE " + " AND ".join(f"{key}=?" for key in ROW_KEYS[table]),
                record_id.split(":"),
            ).fetchone()
        return self.protection.seal(table, record_id, {"query": query, "value": value}, create=exists is None)

    def _read_payload(self, table, row):
        from backend.knowe_storage._sqlite import json_loads
        record_id = ":".join(str(row[k]) for k in ROW_KEYS[table])
        raw = row["payload_json"]
        is_protected = protected_id(record_id) or ("task_id" in row.keys() and protected_id(row["task_id"]))
        if not is_protected:
            if isinstance(raw, str) and raw.startswith(WIRE):
                raise ProtectedStorageError("protected record identity changed")
            return json_loads(raw, {})
        if self.protection is None:
            raise ProtectedStorageError("protected repository capability required")
        opened = self.protection.open(table, record_id, raw)
        if not isinstance(opened, dict) or set(opened) != {"query", "value"}:
            raise ProtectedStorageError("invalid protected record schema")
        for name, expected in opened["query"].items():
            if name in CONTENT_FIELDS.get(table, ()):
                continue
            if name in row.keys() and row[name] not in (expected, None if expected == "" else expected):
                raise ProtectedStorageError("protected clear row field mismatch")
        for name in CONTENT_FIELDS.get(table, ()):
            if name in row.keys() and row[name] != WIRE + name:
                raise ProtectedStorageError("protected scalar reference mismatch")
        return opened["value"]

    def _scalar(self, record_id, field, value):
        if protected_id(record_id):
            if self.protection is None:
                raise ProtectedStorageError("protected repository capability required")
            return WIRE + field
        return value

    def _provenance(self, record_id, value):
        from backend.knowe_storage._sqlite import provenance_sql_values
        if protected_id(record_id):
            return (WIRE + "provenance", None, None, None, None, None, None)
        return provenance_sql_values(value)

    def purge_protected(self):
        if self.protection is None:
            raise ProtectedStorageError("protected purge capability required")
        access = self.protection.access
        registry = access.registry
        registry.revoke(access.group_id, access.task_id)
        with registry.db.transaction(immediate=False) as conn:
            registry._read(conn, access.task_id, access.group_id, revoked=True)
            claims = conn.execute("SELECT * FROM studycrew_protected_records WHERE task_id=? AND store_id=?",
                (access.task_id, self.protection.store_id)).fetchall()
        # Dependents first: completion/result foreign keys survive outbox deletion.
        order = ("completion_projection_receipts_v2", "completion_outbox_v2", "wait_tokens_v2",
                 "task_journal_v1", "task_attempts_v1", "agent_completion_outcomes_v2", "task_results_v1",
                 "decision_events_v2", "task_run_history_v2", "deliveries", "completion_events_v2", "task_runs")
        with self.db.transaction() as conn:
            # Use independently stored task relations as well as authenticated
            # claims: a missing child claim must not leave content behind on purge.
            completion_ids = [row[0] for row in conn.execute(
                "SELECT completion_id FROM completion_events_v2 WHERE task_id=?", (access.task_id,)
            ).fetchall()] if self.db.table_columns("completion_events_v2") else []
            for table in ("completion_projection_receipts_v2", "completion_outbox_v2"):
                if self.db.table_columns(table):
                    for completion_id in completion_ids:
                        conn.execute(f"DELETE FROM {table} WHERE completion_id=?", (completion_id,))
            for claim in sorted(claims, key=lambda row: order.index(row["table_name"]) if row["table_name"] in order else -1):
                table = claim["table_name"]
                if table not in ROW_KEYS:
                    raise ProtectedStorageError("invalid purge table")
                mac = registry.crypto.digest("protected-record", [claim[k] for k in ("store_id", "table_name", "record_id", "task_id")])
                if not hmac.compare_digest(mac, claim["mac"]):
                    raise ProtectedStorageError("invalid purge claim")
                keys = ROW_KEYS[table]
                if self.db.table_columns(table):
                    conn.execute(f"DELETE FROM {table} WHERE " + " AND ".join(f"{k}=?" for k in keys),
                        claim["record_id"].split(":"))
            for table in order:
                if "task_id" in self.db.table_columns(table):
                    conn.execute(f"DELETE FROM {table} WHERE task_id=?", (access.task_id,))
        return {"task_id": access.task_id, "state": "purged"}


# RAG package initialization imports the task-run repository. Define the explicit
# storage interfaces first, then import the existing crypto/domain capabilities.
from backend.knowe_rag.crypto import MasterKeyError, field_aad  # noqa: E402
from backend.task_runtime.card_state import CardStateError, canonical  # noqa: E402
from backend.task_runtime.contracts import TaskStatus  # noqa: E402
