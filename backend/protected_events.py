"""Authoritative, completion-derived protected messages and opaque delivery refs.

This is a private backend adapter, not a producer or execution capability.  The
completion database is the only protected event authority; JSONL is a cache.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hmac
import json
from pathlib import Path
from typing import Any

from .knowe_rag.crypto import field_aad
from .knowe_storage._sqlite import SQLiteDatabase
from .protected_storage import ProtectedLineages, ProtectedStorageError, bounded_json
from .task_runtime.contracts import TaskStatus


REF = "_protected_event"
TABLE = "studycrew_protected_events_v1"
CATALOG = "studycrew_protected_event_stores_v1"
HEADS = "studycrew_protected_event_heads_v1"
LIMIT = 10000


@dataclass(frozen=True)
class ProtectedEventCommit:
    """A committed SQLite event, not a recipient ACK or notification receipt."""

    reference: dict[str, Any]
    created: bool


class ProtectedEvents:
    def __init__(self, registry: ProtectedLineages):
        if not isinstance(registry, ProtectedLineages):
            raise ProtectedStorageError("canonical event registry required")
        self.registry = registry
        with registry.db.transaction(immediate=False) as conn:
            meta = registry.crypto.verify(conn)
            self._crypto_context = [meta["store_id"], meta["key_id"]]
        self._stores: dict[str, SQLiteDatabase] = {}
        self._invalidators = []
        self._closed = False
        self.fault = None
        self._catalog_db = self._connect(registry.db.path)
        self._catalog_db.initialize(f"""
            CREATE TABLE IF NOT EXISTS {CATALOG} (
                project_id TEXT PRIMARY KEY, path_wire BLOB NOT NULL, mac TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS {HEADS} (
                project_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, mac TEXT NOT NULL
            );
        """)
        with self._catalog_db.transaction(immediate=False) as conn:
            rows = conn.execute(f"SELECT * FROM {CATALOG} LIMIT ?", (LIMIT + 1,)).fetchall()
        try:
            if len(rows) > LIMIT:
                raise ProtectedStorageError("protected event capacity exceeded")
            for row in rows:
                project = row["project_id"]
                path = self._open_path(project, row["path_wire"], row["mac"])
                self._stores[project] = self._connect(path)
            registry.runtime._protected_events = self
            self.retry_cleanup()
        except BaseException:
            registry.runtime._protected_events = None
            self.close()
            raise

    @classmethod
    def existing(cls, runtime):
        current = getattr(runtime, "_protected_events", None)
        if current is not None:
            return current
        if not runtime.store.db.table_columns(CATALOG):
            return None
        return cls(ProtectedLineages(runtime))

    def _check(self):
        if self._closed or self.registry.runtime._closed:
            raise ProtectedStorageError("protected event authority closed")

    @staticmethod
    def _safe_path(path):
        candidate = Path(path).absolute()
        if any(part.is_symlink() or part.is_junction() for part in (candidate, *candidate.parents)):
            raise ProtectedStorageError("protected store reparse target denied")
        if not candidate.is_file() or str(candidate.resolve()) != str(candidate):
            raise ProtectedStorageError("protected existing path unavailable")
        return str(candidate)

    def _digest(self, domain, value):
        return self.registry.crypto.digest("protected-events-v1:" + domain,
            [self._crypto_context, value])

    def _path_aad(self, project):
        return field_aad(self._digest("catalog", project), "path")

    def _open_path(self, project, wire, mac):
        if not isinstance(wire, bytes) or not hmac.compare_digest(
            self._digest("catalog-wire", [project, base64.b64encode(wire).decode()]), mac
        ):
            raise ProtectedStorageError("protected event catalog authentication failed")
        try:
            raw = self.registry.crypto._key.open(wire, aad=self._path_aad(project))
            value = json.loads(raw.decode("utf-8"))
            if (set(value) != {"path", "project_id", "group_id", "task_id", "store_id"}
                    or value["project_id"] != project or bounded_json(value) != raw):
                raise ValueError()
            with self.registry.db.transaction(immediate=False) as conn:
                self.registry._read(conn, value["task_id"], value["group_id"], revoked=True)
            path = value["path"]
            if value["store_id"] != self.registry.crypto.digest("protected-store-path", path):
                raise ValueError()
            if self._safe_path(path) != path:
                raise ValueError()
            return path
        except Exception:
            raise ProtectedStorageError("protected event catalog unavailable") from None

    @staticmethod
    def _connect(path):
        db = SQLiteDatabase(path)
        db.initialize("PRAGMA synchronous=FULL; PRAGMA secure_delete=ON;")
        return db

    def attach(self, access, completion_path):
        """Register an existing independently authenticated child store, never relocate."""
        from .knowe_harness.completion import SQLiteCompletionStore
        self._check()
        if access.registry.runtime is not self.registry.runtime:
            raise ProtectedStorageError("foreign event runtime")
        path = self._safe_path(completion_path)
        with access.guard() as binding:
            project = binding["project_id"]
            with SQLiteCompletionStore(path, protected=access) as selected:
                if not selected.list_active():
                    raise ProtectedStorageError("committed completion required")
            if project in self._stores:
                if self._stores[project].path != path:
                    raise ProtectedStorageError("protected event relocation denied")
                return
            db = self._connect(path)
            db.initialize(f"""
                CREATE TABLE IF NOT EXISTS {TABLE} (
                    event_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    group_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    completion_id TEXT NOT NULL, outbox_id TEXT NOT NULL,
                    seq INTEGER NOT NULL, event_type TEXT NOT NULL,
                    semantic_digest TEXT NOT NULL, wire BLOB NOT NULL, mac TEXT NOT NULL,
                    UNIQUE(project_id,seq), UNIQUE(outbox_id)
                );
                CREATE TABLE IF NOT EXISTS {HEADS} (
                    project_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, mac TEXT NOT NULL
                );
            """)
            descriptor = {"path": path, "project_id": project, "group_id": binding["group_id"],
                "task_id": binding["task_id"], "store_id": access.for_store(path).store_id}
            wire = self.registry.crypto._key.seal(bounded_json(descriptor), aad=self._path_aad(project))
            mac = self._digest("catalog-wire", [project, base64.b64encode(wire).decode()])
            try:
                with self._catalog_db.transaction() as conn:
                    conn.execute(f"INSERT INTO {CATALOG} VALUES (?,?,?)", (project, wire, mac))
            except BaseException:
                db.close()
                raise
            self._stores[project] = db

    def _metadata(self, row):
        return {key: row[key] for key in ("event_id", "project_id", "group_id", "task_id",
            "completion_id", "outbox_id", "seq", "event_type", "semantic_digest")}

    def _aad(self, db, metadata):
        return field_aad(self._digest("aad", [str(Path(db.path).resolve()), metadata]), "body")

    def _reference(self, row):
        metadata = self._metadata(row)
        wire = row["wire"]
        if (not isinstance(wire, bytes) or not 28 <= len(wire) <= 2 * 1024 * 1024 + 28
                or type(metadata["seq"]) is not int or metadata["seq"] <= 0
                or metadata["event_type"] != "message"
                or not hmac.compare_digest(row["mac"], self._digest("record", [
                    metadata, base64.b64encode(wire).decode()]))):
            raise ProtectedStorageError("protected event authentication failed")
        return {"type": "message", "event_id": row["event_id"], "project_id": row["project_id"],
            "group_id": row["group_id"], "task_id": row["task_id"],
            "seq": row["seq"], REF: 1, "mac": row["mac"]}

    def _read_access(self, row, *, history=False):
        registry = self.registry
        runtime = registry.runtime
        with registry.db.transaction(immediate=False) as conn:
            binding = registry._read(conn, row["task_id"], row["group_id"])
        owner = runtime.store.get_run(binding["owner_run_id"])
        if history:
            return registry.completed_history(row["group_id"], row["task_id"],
                completion_path=self._stores[row["project_id"]].path)
        if owner is not None and owner.status is TaskStatus.PAUSED:
            return registry.paused_history(row["group_id"], row["task_id"])
        return registry.access(row["group_id"], row["task_id"])

    def head(self, project):
        self._check()
        db = self._stores.get(project)
        value = 0
        for selected in ([db] if db is not None else []) + [self._catalog_db]:
            self._safe_path(selected.path)
            with selected.transaction(immediate=False) as conn:
                row = conn.execute(f"SELECT * FROM {HEADS} WHERE project_id=?", (project,)).fetchone()
            if row is not None:
                if not hmac.compare_digest(row["mac"], self._digest("head", [project, row["seq"]])):
                    raise ProtectedStorageError("protected stream head authentication failed")
                value = max(value, row["seq"])
        return value

    def references(self, project):
        self._check()
        db = self._stores.get(project)
        if db is None:
            return []
        self._safe_path(db.path)
        with db.transaction(immediate=False) as conn:
            rows = conn.execute(f"SELECT * FROM {TABLE} WHERE project_id=? ORDER BY seq LIMIT ?",
                (project, LIMIT + 1)).fetchall()
        if len(rows) > LIMIT:
            raise ProtectedStorageError("protected event capacity exceeded")
        return [self._reference(row) for row in rows]

    def lookup(self, project, event_id):
        self._check()
        db = self._stores.get(project)
        if db is None:
            raise ProtectedStorageError("protected event store unavailable")
        self._safe_path(db.path)
        with db.transaction(immediate=False) as conn:
            row = conn.execute(f"SELECT * FROM {TABLE} WHERE project_id=? AND event_id=?",
                (project, event_id)).fetchone()
        if row is None:
            raise ProtectedStorageError("protected event unavailable")
        self._reference(row)
        return row

    def _fault(self, phase):
        if self.fault is not None:
            self.fault(phase)

    def commit(self, access, outbox_id: str, seq: int):
        from .knowe_harness.completion import SQLiteCompletionStore, ProjectionKind
        self._check()
        if type(seq) is not int or seq <= 0:
            raise ProtectedStorageError("positive protected sequence required")
        if access.registry.runtime is not self.registry.runtime:
            raise ProtectedStorageError("foreign event runtime")
        access.require_write()
        with access.guard(write=True) as binding:
            owner = self.registry.runtime.store.get_run(binding["owner_run_id"])
            if owner is None or owner.status is not TaskStatus.RUNNING:
                raise ProtectedStorageError("running event owner required")
            project = binding["project_id"]
            db = self._stores.get(project)
            if db is None:
                raise ProtectedStorageError("selected event store required")
            self._safe_path(db.path)
            event_id = "p2evt_" + self._digest("identity", [binding["task_id"], outbox_id])[:32]
            with db.transaction(immediate=False) as conn:
                prior = conn.execute(f"SELECT * FROM {TABLE} WHERE event_id=?", (event_id,)).fetchone()
            # The prior immutable event is its original authenticated outbox relation,
            # including after that outbox has been removed by the real projector ACK.
            if prior is not None:
                ref = self._reference(prior)
                if prior["task_id"] != binding["task_id"] or prior["outbox_id"] != outbox_id:
                    raise ProtectedStorageError("protected event identity mismatch")
                return ProtectedEventCommit(ref, False)
            with SQLiteCompletionStore(db.path, protected=access) as selected:
                rows = [row for row in selected.pending_outbox() if row.outbox_id == outbox_id]
                if len(rows) != 1 or rows[0].projection_kind is not ProjectionKind.DELIVERY:
                    raise ProtectedStorageError("committed delivery outbox required")
                completion = selected.get(rows[0].completion_id)
                result = selected.task_result(completion_id=completion.completion_id)
                if completion.task_id != binding["task_id"] or completion.project_id != project:
                    raise ProtectedStorageError("protected completion relation mismatch")
                # Closed public schema. Do not serialize CompletionEvent/TaskRun or
                # arbitrary metadata (in particular their private reasoning fields).
                content = result.summary if result is not None else ""
                if completion.status.value == "WAITING":
                    content = completion.question
                if not content:
                    content = completion.terminal_reason or completion.status.value
                body = {"type": "message", "event_id": event_id, "project_id": project,
                    "agent_id": binding["worker_id"],
                    "task_id": binding["task_id"], "attempt_id": binding["attempt_id"],
                    "completion_id": completion.completion_id, "status": completion.status.value,
                    "ts": completion.created_at,
                    "content": content, "metadata": {"evidence": [
                        {"kind": "completion", "ref": completion.completion_id}]}}
                raw = bounded_json(body)
                metadata = {"event_id": event_id, "project_id": project, "group_id": binding["group_id"],
                    "task_id": binding["task_id"], "completion_id": completion.completion_id,
                    "outbox_id": outbox_id, "seq": seq, "event_type": "message",
                    "semantic_digest": self._digest("semantic", {k: v for k, v in body.items() if k != "ts"})}
            wire = self.registry.crypto._key.seal(raw, aad=self._aad(db, metadata))
            mac = self._digest("record", [metadata, base64.b64encode(wire).decode()])
            reference = self._reference({**metadata, "wire": wire, "mac": mac})
            self._fault("before_commit")
            with db.transaction() as conn:
                current = conn.execute(f"SELECT * FROM {HEADS} WHERE project_id=?", (project,)).fetchone()
                if current is not None:
                    if not hmac.compare_digest(current["mac"], self._digest("head", [project, current["seq"]])):
                        raise ProtectedStorageError("protected stream head authentication failed")
                    if seq <= current["seq"]:
                        raise ProtectedStorageError("protected sequence did not advance")
                conn.execute(f"INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (*metadata.values(), wire, mac))
                conn.execute(f"INSERT OR REPLACE INTO {HEADS} VALUES (?,?,?)",
                    (project, seq, self._digest("head", [project, seq])))
                self._fault("inside_commit")
            # No fallible work after the durable commit: callers own enqueue recovery.
            return ProtectedEventCommit(reference, True)

    def hydrate(self, reference, *, history=False):
        self._check()
        row = self.lookup(reference.get("project_id"), reference.get("event_id"))
        if reference != self._reference(row):
            raise ProtectedStorageError("protected reference substitution denied")
        access = self._read_access(row, history=history)
        with access.guard() as binding:
            if (binding["project_id"], binding["group_id"], binding["task_id"]) != (
                    row["project_id"], row["group_id"], row["task_id"]):
                raise ProtectedStorageError("protected event lineage mismatch")
            try:
                raw = self.registry.crypto._key.open(row["wire"], aad=self._aad(
                    self._stores[row["project_id"]], self._metadata(row)))
                body = json.loads(raw.decode("utf-8"))
                if bounded_json(body) != raw or not hmac.compare_digest(
                        self._digest("semantic", {k: v for k, v in body.items() if k != "ts"}), row["semantic_digest"]):
                    raise ValueError()
            except Exception:
                raise ProtectedStorageError("protected event body unavailable") from None
            if set(body) != {"type", "event_id", "project_id", "agent_id", "task_id",
                    "attempt_id", "completion_id", "status", "content", "metadata", "ts"}:
                raise ProtectedStorageError("unsupported protected public schema")
            return {**body, "seq": row["seq"]}

    def purge(self, group_id, task_id):
        self.registry.revoke(group_id, task_id)

    def purge_group(self, group_id):
        with self.registry.db.transaction(immediate=False) as conn:
            tasks = conn.execute("SELECT task_id FROM studycrew_protected_lineages WHERE group_id=?",
                (group_id,)).fetchall()
        for task in tasks:
            self.purge(group_id, task["task_id"])

    def _purge_rows(self, task_id):
        for project, db in tuple(self._stores.items()):
            self._safe_path(db.path)
            with db.transaction() as conn:
                conn.execute(f"DELETE FROM {TABLE} WHERE task_id=?", (task_id,))
                remaining = conn.execute(f"SELECT 1 FROM {TABLE} WHERE project_id=? LIMIT 1", (project,)).fetchone()
            if remaining is None:
                # Copy only the content-free high watermark before dropping the
                # encrypted path. Crash before this transaction keeps the old catalog
                # available for an idempotent cleanup retry.
                head = self.head(project)
                with self._catalog_db.transaction() as conn:
                    conn.execute(f"INSERT OR REPLACE INTO {HEADS} VALUES (?,?,?)",
                        (project, head, self._digest("head", [project, head])))
                    conn.execute(f"DELETE FROM {CATALOG} WHERE project_id=?", (project,))
                del self._stores[project]
                db.close()
        for invalidate in tuple(self._invalidators):
            invalidate(task_id)

    def retry_cleanup(self):
        with self.registry.db.transaction(immediate=False) as conn:
            rows = conn.execute("SELECT task_id,group_id,state FROM studycrew_protected_lineages").fetchall()
            if len(rows) > LIMIT:
                raise ProtectedStorageError("protected cleanup capacity exceeded")
            for row in rows:
                self.registry._read(conn, row["task_id"], row["group_id"], revoked=True)
        for row in rows:
            deletion = self.registry.runtime.store.group_deletion_intent(row["group_id"])
            if row["state"] == "revoked" or deletion is not None:
                if row["state"] != "revoked":
                    self.registry.revoke(row["group_id"], row["task_id"])
                self._purge_rows(row["task_id"])

    def close(self):
        if self._closed:
            return
        self._closed = True
        for db in self._stores.values():
            db.close()
        self._catalog_db.close()
        self._invalidators.clear()
