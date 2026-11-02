"""Group-scoped encrypted chat records, independent of Worker/task identities."""
import uuid
import json
from backend.knowe_rag.crypto import field_aad
from backend.protected_storage import bounded_json, ProtectedStorageError
from .card_state import canonical

REF = "_group_content"
TABLE = "studycrew_group_content_v1"
# Explicit reached content-bearing event boundaries; provider reasoning is discarded.
EVENT_TYPES = frozenset({"user_echo", "message", "error", "stream_delta", "approval_card",
    "approval_update", "task_suggestions", "thinking_delta", "tool_result", "tool_call",
    "approval_resolved", "instruction_injected", "report_submitted", "recovery_notice",
    "task_contract_blocked", "completion_status"})


class GroupContent:
    def __init__(self, runtime):
        self.runtime = runtime
        self.db = runtime.store.db
        self.crypto = runtime.store.card_states
        self.db.initialize(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            event_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, project_id TEXT NOT NULL,
            seq INTEGER NOT NULL, owner_run_id TEXT, wire BLOB NOT NULL, mac TEXT NOT NULL
        ); CREATE INDEX IF NOT EXISTS group_content_scope ON {TABLE}(group_id,project_id,seq);""")
        self.db.ensure_columns(TABLE, {"event_type": "TEXT NOT NULL DEFAULT ''"})

    def group_for_channel(self, channel):
        project = channel.split(":", 2)[1] if channel.startswith("dm:") else channel
        try:
            return self.runtime.groups.resolve_group_id(project)
        except Exception:
            return None

    def admit(self, group_id):
        with self.runtime._lock, self.runtime.groups.admission_guard():
            self.runtime._canonical_group(group_id, require_active=True)
            self.runtime._assert_group_mutable(group_id)
            with self.db.transaction() as conn:
                self.crypto.verify(conn, initialize=True)

    def _aad(self, meta, row):
        return field_aad(canonical(["studycrew.group-content", 1, meta["store_id"], meta["key_id"],
            row["group_id"], row["project_id"], row["event_id"], row["seq"]]), "event")

    def seal_event(self, event):
        group_id = self.group_for_channel(event.get("project_id", ""))
        if group_id is None or event.get("type") not in EVENT_TYPES:
            return event
        if REF in event:
            raise ProtectedStorageError("Incoming Group content references cannot grant publication")
        with self.runtime._lock, self.runtime.groups.admission_guard():
            self.admit(group_id)
            body = {k: v for k, v in event.items() if k not in {"reasoning", "reasoning_content"}}
            body["event_id"] = body.get("event_id") or "gce_" + uuid.uuid4().hex
            body["group_id"] = group_id
            row = {k: body[k] for k in ("event_id", "project_id", "seq", "group_id")}
            with self.db.transaction() as conn:
                meta = self.crypto.verify(conn)
                if conn.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE group_id=?", (group_id,)).fetchone()[0] >= 10000:
                    raise ProtectedStorageError("Group content retention capacity reached")
                wire = self.crypto._key.seal(bounded_json(body), aad=self._aad(meta, row))
                mac = self.crypto.digest("group-content-reference", row)
                conn.execute(f"INSERT INTO {TABLE}(event_id,group_id,project_id,seq,wire,mac,event_type) VALUES(?,?,?,?,?,?,?)",
                    (row["event_id"], group_id, row["project_id"], row["seq"], wire, mac, body["type"]))
            # No plaintext body enters Hub rings, idempotency caches or JSONL queues.
            return {"type": body["type"], **row, REF: 1, "mac": mac, "ts": body["ts"]}

    def references(self, channel):
        group_id = self.group_for_channel(channel)
        if group_id is None:
            return []
        self.runtime._assert_group_mutable(group_id)
        from backend.contract import PERSISTABLE_EVENT_TYPES
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(f"SELECT event_id,group_id,project_id,seq,mac,event_type FROM {TABLE} WHERE group_id=? AND project_id=? ORDER BY seq LIMIT 10000",
                (group_id, channel)).fetchall()
        return [{"type": row["event_type"], **{k: row[k] for k in ("event_id", "group_id", "project_id", "seq", "mac")}, REF: 1}
                for row in rows if row["event_type"] in PERSISTABLE_EVENT_TYPES]

    def head(self, channel):
        with self.db.transaction(immediate=False) as conn:
            return conn.execute(f"SELECT COALESCE(MAX(seq),0) FROM {TABLE} WHERE project_id=?", (channel,)).fetchone()[0]

    def hydrate(self, reference):
        group_id = reference.get("group_id")
        with self.runtime._lock, self.runtime.groups.admission_guard():
            # Archive retains authorized history; deletion fences all hydration.
            self.runtime._canonical_group(group_id)
            self.runtime._assert_group_mutable(group_id)
            if self.group_for_channel(reference.get("project_id", "")) != group_id:
                raise ProtectedStorageError("Group content channel mismatch")
            with self.db.transaction(immediate=False) as conn:
                meta = self.crypto.verify(conn)
                row = conn.execute(f"SELECT * FROM {TABLE} WHERE event_id=? AND group_id=?",
                    (reference.get("event_id"), group_id)).fetchone()
                if row is None or any(reference.get(k) != row[k] for k in ("project_id", "seq", "mac")):
                    raise ProtectedStorageError("Group content reference unavailable")
                try:
                    body = json.loads(self.crypto._key.open(row["wire"], aad=self._aad(meta, row)).decode("utf-8"))
                except Exception:
                    raise ProtectedStorageError("Group content authentication failed") from None
                if body["type"] != reference.get("type") or body["event_id"] != row["event_id"]:
                    raise ProtectedStorageError("Group content envelope mismatch")
                return body

    def bind_owner(self, reference, run_id):
        # Called only after actual backend routing, never from a client run field.
        if REF not in reference:
            return
        with self.runtime._lock:
            run = self.runtime._owned_run(run_id, reference["group_id"])
            with self.db.transaction() as conn:
                conn.execute(f"UPDATE {TABLE} SET owner_run_id=? WHERE event_id=? AND group_id=? AND (owner_run_id IS NULL OR owner_run_id=?)",
                    (run.run_id, reference["event_id"], run.group_id, run.run_id))
