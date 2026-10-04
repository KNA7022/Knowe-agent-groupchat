"""Fixed protected child ownership for discovery and deletion, never execution.

Enrollment precedes private child writes. The runtime database is the durable
cleanup obligation; SQLite/files are separate idempotent cleanup transactions.
"""
from __future__ import annotations

import base64
import hmac
import json
from pathlib import Path

from .knowe_rag.crypto import field_aad
from .knowe_storage._sqlite import SQLiteDatabase
from .protected_storage import (
    ProtectedAccess, ProtectedLineages, ProtectedStorageError, bounded_json,
    _purge_revoked_rows,
)
from .workspace_layout import internal_workspace_for


TABLE = "studycrew_protected_children_v1"
LIMIT = 10000


class ProtectedChildren:
    """Backend-only, one fixed SQLite path and envelope directory per lineage."""

    def __init__(self, registry: ProtectedLineages):
        if type(registry) is not ProtectedLineages:
            raise ProtectedStorageError("canonical child registry required")
        self.registry = registry
        self._closed = False
        self.fault = None
        self.db = SQLiteDatabase(registry.db.path)
        self.db.initialize(f"""
            PRAGMA synchronous=FULL;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS {TABLE} (
                task_id TEXT PRIMARY KEY, wire BLOB NOT NULL, mac TEXT NOT NULL
            );
        """)
        registry.runtime._protected_children = self

    @classmethod
    def existing(cls, runtime):
        current = getattr(runtime, "_protected_children", None)
        if current is not None:
            return current
        if not runtime.store.db.table_columns(TABLE):
            return None
        return cls(ProtectedLineages(runtime))

    def _check(self):
        if self._closed or self.registry.runtime._closed:
            raise ProtectedStorageError("protected child catalog closed")

    def _digest(self, domain, value):
        with self.registry.db.transaction(immediate=False) as conn:
            meta = self.registry.crypto.verify(conn)
        return self.registry.crypto.digest("protected-children-v1:" + domain,
            [meta["store_id"], meta["key_id"], value])

    def _aad(self, task_id):
        return field_aad(self._digest("descriptor", task_id), "locations")

    @staticmethod
    def _safe(path):
        candidate = Path(path).absolute()
        if any(p.is_symlink() or p.is_junction() for p in (candidate, *candidate.parents)):
            raise ProtectedStorageError("protected child reparse path denied")
        if candidate.resolve() != candidate:
            raise ProtectedStorageError("protected child location changed")
        return candidate

    def _locations(self, project_id):
        runtime_path = self._safe(self.registry.db.path)
        if runtime_path.name != "task-runtime-v1.sqlite3" or runtime_path.parent.name != "runtime":
            raise ProtectedStorageError("canonical runtime location required")
        try:
            root = internal_workspace_for(runtime_path.parent.parent, project_id)
        except ValueError:
            raise ProtectedStorageError("canonical protected project required") from None
        self._safe(runtime_path.parent.parent / project_id)
        completion = self._safe(root / "runtime" / "runtime.sqlite3")
        envelopes = self._safe(root / "runtime" / "task-envelopes")
        return completion, envelopes

    def register(self, access: ProtectedAccess):
        """Enroll backend-derived locations before any envelope/run body is written."""
        self._check()
        if type(access) is not ProtectedAccess or access.registry.runtime is not self.registry.runtime:
            raise ProtectedStorageError("live selected child capability required")
        with self.registry.runtime._lock, access.guard(write=True) as binding:
            group = self.registry.runtime.groups.get(access.group_id)
            if binding["project_id"] != (group.legacy_project_id or group.group_id):
                raise ProtectedStorageError("protected project binding changed")
            completion, envelopes = self._locations(binding["project_id"])
            value = {"version": 1, "task_id": access.task_id, "group_id": access.group_id,
                "project_id": binding["project_id"], "completion": str(completion),
                "envelopes": str(envelopes),
                "completion_store": access.for_store(completion).store_id,
                "envelope_store": access.for_store(envelopes).store_id}
            prior = self._read(access.task_id)
            if prior is not None:
                if prior != value:
                    raise ProtectedStorageError("protected child relocation denied")
                return dict(prior)
            with self.registry.db.transaction(immediate=False) as conn:
                if conn.execute("SELECT 1 FROM studycrew_protected_records WHERE task_id=? LIMIT 1",
                                (access.task_id,)).fetchone() is not None:
                    raise ProtectedStorageError("protected child enrollment must precede content")
            with self.db.transaction() as conn:
                if conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] >= LIMIT:
                    raise ProtectedStorageError("protected child capacity exceeded")
                wire = self.registry.crypto._key.seal(bounded_json(value), aad=self._aad(access.task_id))
                mac = self._digest("wire", [access.task_id, base64.b64encode(wire).decode()])
                conn.execute(f"INSERT INTO {TABLE} VALUES (?,?,?)", (access.task_id, wire, mac))
            return dict(value)

    def _read(self, task_id):
        self._check()
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(f"SELECT * FROM {TABLE} WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        try:
            wire = row["wire"]
            if not isinstance(wire, bytes) or not 28 <= len(wire) <= 16384:
                raise ValueError()
            if not hmac.compare_digest(self._digest("wire", [task_id, base64.b64encode(wire).decode()]), row["mac"]):
                raise ValueError()
            raw = self.registry.crypto._key.open(wire, aad=self._aad(task_id))
            value = json.loads(raw.decode("utf-8"))
            fields = {"version", "task_id", "group_id", "project_id", "completion", "envelopes",
                      "completion_store", "envelope_store"}
            if (set(value) != fields or type(value["version"]) is not int or value["version"] != 1
                    or any(type(v) is not str or not v or len(v) > 4096 for k, v in value.items() if k != "version")
                    or value["task_id"] != task_id or bounded_json(value) != raw):
                raise ValueError()
            with self.registry.db.transaction(immediate=False) as conn:
                self.registry._read(conn, task_id, value["group_id"], revoked=True)
            completion, envelopes = self._locations(value["project_id"])
            if (value["completion"] != str(completion) or value["envelopes"] != str(envelopes)
                    or value["completion_store"] != self.registry.crypto.digest("protected-store-path", str(completion))
                    or value["envelope_store"] != self.registry.crypto.digest("protected-store-path", str(envelopes))):
                raise ValueError()
            return value
        except Exception:
            raise ProtectedStorageError("protected child metadata unavailable") from None

    def _checkpoint(self, stage):
        if self.fault is not None:
            self.fault(stage)

    def cleanup_revoked(self, task_id):
        """Delete only authenticated original ownership after the durable fence."""
        self._check()
        with self.registry.runtime._lock:
            value = self._read(task_id)
            if value is None:
                return
            group_id = value["group_id"]
            with self.registry.db.transaction(immediate=False) as conn:
                self.registry._read(conn, task_id, group_id, revoked=True)
                if conn.execute("SELECT state FROM studycrew_protected_lineages WHERE task_id=?",
                                (task_id,)).fetchone()[0] != "revoked":
                    raise ProtectedStorageError("child cleanup requires durable revocation")
            self._checkpoint("after_revoke")
            from .protected_events import ProtectedEvents
            events = ProtectedEvents.existing(self.registry.runtime)
            if events is not None:
                events._purge_rows(task_id)
            self._checkpoint("after_events")
            path = self._safe(value["completion"])
            for suffix in ("-wal", "-shm", "-journal"):
                sidecar = self._safe(str(path) + suffix)
                if sidecar.exists() and (not path.exists() or not sidecar.is_file()):
                    raise ProtectedStorageError("protected database sidecar requires recovery")
            if path.exists():
                if not path.is_file():
                    raise ProtectedStorageError("protected child database unavailable")
                db = SQLiteDatabase(path)
                try:
                    db.initialize("PRAGMA synchronous=FULL; PRAGMA secure_delete=ON;")
                    _purge_revoked_rows(db, self.registry, group_id, task_id,
                                       value["completion_store"], strict=True)
                finally:
                    db.close()
            self._checkpoint("after_rows")
            self._purge_envelopes(value)
            self._checkpoint("after_envelopes")
            # A failure here retains all discovery information for an absent-safe retry.
            self._checkpoint("before_retire")
            with self.db.transaction() as conn:
                conn.execute(f"DELETE FROM {TABLE} WHERE task_id=?", (task_id,))

    def _purge_envelopes(self, value):
        task_id = value["task_id"]
        directory = self._safe(Path(value["envelopes"]) / task_id)
        if not directory.exists():
            return
        if not directory.is_dir():
            raise ProtectedStorageError("protected envelope directory unavailable")
        with self.registry.db.transaction(immediate=False) as conn:
            claims = conn.execute("SELECT * FROM studycrew_protected_records WHERE task_id=? AND store_id=?",
                (task_id, value["envelope_store"])).fetchall()
        owned = set()
        for claim in claims:
            expected = self.registry.crypto.digest("protected-record", [claim[k] for k in
                ("store_id", "table_name", "record_id", "task_id")])
            if claim["table_name"] != "task_envelopes" or not hmac.compare_digest(expected, claim["mac"]):
                raise ProtectedStorageError("protected envelope ownership unavailable")
            owned.add(claim["record_id"] + ".json")
        paths = list(directory.iterdir())
        for path in paths:
            self._safe(path)
            if not path.is_file() or path.name not in owned:
                raise ProtectedStorageError("unowned protected envelope artifact")
        for path in paths:
            path.unlink()
        directory.rmdir()

    def purge_group(self, group_id):
        for task_id in self._tasks():
            value = self._read(task_id)
            if value["group_id"] == group_id:
                self.registry.revoke(group_id, task_id)

    def _tasks(self):
        self._check()
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(f"SELECT task_id FROM {TABLE} LIMIT ?", (LIMIT + 1,)).fetchall()
        if len(rows) > LIMIT:
            raise ProtectedStorageError("protected child capacity exceeded")
        return tuple(row[0] for row in rows)

    def retry_cleanup(self):
        cleaned = 0
        for task_id in self._tasks():
            self._read(task_id)  # Authenticate even an unrevoked retained obligation.
            with self.registry.db.transaction(immediate=False) as conn:
                state = conn.execute("SELECT state FROM studycrew_protected_lineages WHERE task_id=?",
                                     (task_id,)).fetchone()[0]
            if state == "revoked":
                self.cleanup_revoked(task_id)
                cleaned += 1
        return {"cleaned": cleaned, "retained": len(self._tasks())}

    def close(self):
        if not self._closed:
            self._closed = True
            self.db.close()
