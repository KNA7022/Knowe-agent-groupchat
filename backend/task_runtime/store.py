from __future__ import annotations

"""SQLite journal for the authoritative Task Runtime.

Run snapshots and their public/private event payloads commit in one transaction.
The event journal is durable and append-only; reconnect never depends on a memory
ring and therefore can prove whether a client cursor is contiguous.
"""

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from knowe_storage._sqlite import SQLiteDatabase

from .contracts import (
    ApprovalRequest,
    Checkpoint,
    PermissionGrant,
    RiskLevel,
    RunEvent,
    TaskRun,
    TaskStatus,
    ToolInvocation,
    assert_public_payload,
    opaque_id,
    utc_now,
)


class RuntimeStoreError(RuntimeError):
    pass


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: str | None) -> dict[str, Any]:
    raw = json.loads(value or "{}")
    return raw if isinstance(raw, dict) else {}


class TaskRuntimeStore:
    def __init__(self, path: str | Path) -> None:
        self.db = SQLiteDatabase(path)
        self.db.initialize(
            """
            CREATE TABLE IF NOT EXISTS studycrew_task_runs (
                run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL UNIQUE,
                group_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                queue_class TEXT NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL,
                last_seq INTEGER NOT NULL,
                request_key TEXT UNIQUE,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_studycrew_runs_schedule
              ON studycrew_task_runs(status, queue_class, kind, created_at);
            CREATE INDEX IF NOT EXISTS idx_studycrew_runs_group
              ON studycrew_task_runs(group_id, status, updated_at);

            CREATE TABLE IF NOT EXISTS studycrew_run_events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES studycrew_task_runs(run_id) ON DELETE CASCADE,
                group_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                version INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                public_payload_json TEXT NOT NULL,
                sensitive_payload_json TEXT NOT NULL,
                sensitivity TEXT NOT NULL,
                idempotency_key TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, seq),
                UNIQUE(run_id, idempotency_key)
            );

            CREATE TABLE IF NOT EXISTS studycrew_approvals (
                approval_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT
                ,request_key TEXT UNIQUE
            );
            CREATE INDEX IF NOT EXISTS idx_studycrew_approvals_group
              ON studycrew_approvals(group_id, status, created_at);

            CREATE TABLE IF NOT EXISTS studycrew_permission_grants (
                grant_id TEXT PRIMARY KEY,
                approval_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS studycrew_tool_invocations (
                invocation_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                risk TEXT NOT NULL,
                status TEXT NOT NULL,
                replay_allowed INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(run_id, idempotency_key)
            );

            CREATE TABLE IF NOT EXISTS studycrew_checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                version INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL
                ,idempotency_key TEXT NOT NULL
                ,UNIQUE(run_id, idempotency_key)
            );

            CREATE TABLE IF NOT EXISTS studycrew_scheduler_state (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # Development snapshots may have been created by an earlier Task Runtime v1
        # build.  Keep this migration idempotent before enforcing new key indexes.
        self.db.ensure_columns("studycrew_approvals", {"request_key": "TEXT"})
        self.db.ensure_columns("studycrew_checkpoints", {"idempotency_key": "TEXT"})
        self.db.initialize(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_studycrew_approval_request_key
              ON studycrew_approvals(request_key) WHERE request_key IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_studycrew_checkpoint_request_key
              ON studycrew_checkpoints(run_id, idempotency_key)
              WHERE idempotency_key IS NOT NULL;
            """
        )

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _row_run(row: Any) -> TaskRun:
        return TaskRun.from_dict(_loads(str(row["payload_json"])))

    def create_run(self, run: TaskRun, *, request_key: str) -> tuple[TaskRun, RunEvent, bool]:
        if not request_key.strip():
            raise ValueError("request_key is required")
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT payload_json FROM studycrew_task_runs WHERE request_key=?", (request_key,),
            ).fetchone()
            if existing is not None:
                stored = self._row_run(existing)
                event_row = conn.execute(
                    "SELECT * FROM studycrew_run_events WHERE run_id=? AND seq=1", (stored.run_id,),
                ).fetchone()
                return stored, self._row_event(event_row), False
            run.last_seq = 1
            run.version = 1
            run.updated_at = utc_now()
            event = RunEvent(
                event_id=opaque_id("evt"), run_id=run.run_id, group_id=run.group_id,
                seq=1, version=1, event_type="run_queued",
                public_payload={"status": run.status.value, "kind": run.kind.value,
                                "route_label": run.route_label.value, "summary": run.public_summary},
            )
            conn.execute(
                """INSERT INTO studycrew_task_runs(
                       run_id, task_id, group_id, kind, queue_class, status, version,
                       last_seq, request_key, payload_json, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run.run_id, run.task_id, run.group_id, run.kind.value, run.queue_class.value,
                 run.status.value, run.version, run.last_seq, request_key, _dumps(run.to_dict()),
                 run.created_at, run.updated_at),
            )
            self._insert_event(conn, event, request_key=f"create:{request_key}")
        return run, event, True

    def get_run(self, run_id: str) -> TaskRun | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT payload_json FROM studycrew_task_runs WHERE run_id=?", (run_id,),
            ).fetchone()
        return None if row is None else self._row_run(row)

    def list_runs(
        self, *, group_id: str | None = None, statuses: set[TaskStatus] | None = None,
        limit: int = 10_000,
    ) -> tuple[TaskRun, ...]:
        clauses: list[str] = []
        args: list[Any] = []
        if group_id:
            clauses.append("group_id=?")
            args.append(group_id)
        if statuses:
            values = sorted(item.value for item in statuses)
            clauses.append(f"status IN ({','.join('?' for _ in values)})")
            args.extend(values)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        args.append(max(1, min(int(limit), 100_000)))
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM studycrew_task_runs" + where
                + " ORDER BY created_at, run_id LIMIT ?", tuple(args),
            ).fetchall()
        return tuple(self._row_run(row) for row in rows)

    def change_run(
        self,
        run_id: str,
        *,
        event_type: str,
        public_payload: Mapping[str, Any],
        idempotency_key: str,
        mutate: Callable[[TaskRun], None] | None = None,
        sensitive_payload: Mapping[str, Any] | None = None,
        sensitivity: str = "public",
        transaction_hook: Callable[[Any, TaskRun, RunEvent], None] | None = None,
    ) -> tuple[TaskRun, RunEvent, bool]:
        assert_public_payload(public_payload)
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        with self.db.transaction() as conn:
            duplicate = conn.execute(
                "SELECT * FROM studycrew_run_events WHERE run_id=? AND idempotency_key=?",
                (run_id, idempotency_key),
            ).fetchone()
            if duplicate is not None:
                run_row = conn.execute(
                    "SELECT payload_json FROM studycrew_task_runs WHERE run_id=?", (run_id,),
                ).fetchone()
                if run_row is None:
                    raise RuntimeStoreError(f"event exists without run: {run_id}")
                return self._row_run(run_row), self._row_event(duplicate), False
            row = conn.execute(
                "SELECT payload_json FROM studycrew_task_runs WHERE run_id=?", (run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeStoreError(f"run not found: {run_id}")
            run = self._row_run(row)
            if mutate is not None:
                mutate(run)
            run.version += 1
            run.last_seq += 1
            run.updated_at = utc_now()
            event = RunEvent(
                event_id=opaque_id("evt"), run_id=run.run_id, group_id=run.group_id,
                seq=run.last_seq, version=run.version, event_type=event_type,
                public_payload=dict(public_payload), sensitivity=sensitivity,
                sensitive_payload=dict(sensitive_payload or {}),
            )
            conn.execute(
                """UPDATE studycrew_task_runs SET status=?, version=?, last_seq=?,
                       payload_json=?, updated_at=? WHERE run_id=?""",
                (run.status.value, run.version, run.last_seq, _dumps(run.to_dict()),
                 run.updated_at, run.run_id),
            )
            self._insert_event(conn, event, request_key=idempotency_key)
            if transaction_hook is not None:
                transaction_hook(conn, run, event)
        return run, event, True

    @staticmethod
    def _insert_event(conn: Any, event: RunEvent, *, request_key: str) -> None:
        conn.execute(
            """INSERT INTO studycrew_run_events(
                   event_id, run_id, group_id, seq, version, event_type,
                   public_payload_json, sensitive_payload_json, sensitivity,
                   idempotency_key, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.event_id, event.run_id, event.group_id, event.seq, event.version,
             event.event_type, _dumps(event.public_payload), _dumps(event.sensitive_payload),
             event.sensitivity, request_key, event.created_at),
        )

    @staticmethod
    def _row_event(row: Any) -> RunEvent:
        if row is None:
            raise RuntimeStoreError("event not found")
        return RunEvent(
            event_id=str(row["event_id"]), run_id=str(row["run_id"]),
            group_id=str(row["group_id"]), seq=int(row["seq"]), version=int(row["version"]),
            event_type=str(row["event_type"]), public_payload=_loads(row["public_payload_json"]),
            sensitive_payload=_loads(row["sensitive_payload_json"]),
            sensitivity=str(row["sensitivity"]), created_at=str(row["created_at"]),
        )

    def events_after(self, run_id: str, after_seq: int) -> tuple[RunEvent, ...]:
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(
                "SELECT * FROM studycrew_run_events WHERE run_id=? AND seq>? ORDER BY seq",
                (run_id, max(0, int(after_seq))),
            ).fetchall()
        return tuple(self._row_event(row) for row in rows)

    def event_by_key(self, run_id: str, idempotency_key: str) -> RunEvent | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT * FROM studycrew_run_events WHERE run_id=? AND idempotency_key=?",
                (run_id, idempotency_key),
            ).fetchone()
        return None if row is None else self._row_event(row)

    def save_approval(self, approval: ApprovalRequest, *, request_key: str | None = None) -> None:
        with self.db.transaction() as conn:
            self.upsert_approval_in_transaction(conn, approval, request_key=request_key)

    @staticmethod
    def upsert_approval_in_transaction(
        conn: Any, approval: ApprovalRequest, *, request_key: str | None = None,
    ) -> None:
        conn.execute(
            """INSERT INTO studycrew_approvals(
                   approval_id, run_id, group_id, status, payload_json, created_at, resolved_at,
                   request_key
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(approval_id) DO UPDATE SET status=excluded.status,
                   payload_json=excluded.payload_json, resolved_at=excluded.resolved_at""",
            (approval.approval_id, approval.run_id, approval.group_id, approval.status,
             _dumps(approval.to_dict()), approval.created_at, approval.resolved_at, request_key),
        )

    def approval_by_request_key(self, request_key: str) -> ApprovalRequest | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT approval_id FROM studycrew_approvals WHERE request_key=?", (request_key,),
            ).fetchone()
        return None if row is None else self.get_approval(str(row["approval_id"]))

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT payload_json FROM studycrew_approvals WHERE approval_id=?", (approval_id,),
            ).fetchone()
        if row is None:
            return None
        raw = _loads(row["payload_json"])
        return ApprovalRequest(
            approval_id=str(raw["approval_id"]), run_id=str(raw["run_id"]),
            group_id=str(raw["group_id"]), kind=str(raw["kind"]), summary=str(raw["summary"]),
            risk=RiskLevel(str(raw["risk"])), status=str(raw.get("status") or "pending"),
            created_at=str(raw.get("created_at") or utc_now()), resolved_at=raw.get("resolved_at"),
        )

    def pending_approvals(self, group_id: str) -> tuple[ApprovalRequest, ...]:
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(
                "SELECT approval_id FROM studycrew_approvals WHERE group_id=? AND status='pending' ORDER BY created_at",
                (group_id,),
            ).fetchall()
        return tuple(item for row in rows if (item := self.get_approval(str(row["approval_id"]))) is not None)

    def save_grant(self, grant: PermissionGrant) -> None:
        with self.db.transaction() as conn:
            self.insert_grant_in_transaction(conn, grant)

    def grant_for_approval(self, approval_id: str) -> PermissionGrant | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT payload_json FROM studycrew_permission_grants WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
        if row is None:
            return None
        raw = _loads(row["payload_json"])
        return PermissionGrant(
            grant_id=str(raw["grant_id"]), approval_id=str(raw["approval_id"]),
            run_id=str(raw["run_id"]), group_id=str(raw["group_id"]), task_id=str(raw["task_id"]),
            capabilities=tuple(str(item) for item in raw.get("capabilities") or ()),
            expires_at=str(raw["expires_at"]) if raw.get("expires_at") else None,
            created_at=str(raw.get("created_at") or utc_now()),
        )

    def revoke_group_grants(self, group_id: str) -> int:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM studycrew_permission_grants WHERE group_id=?", (group_id,),
            )
        return int(cursor.rowcount)

    @staticmethod
    def insert_grant_in_transaction(conn: Any, grant: PermissionGrant) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO studycrew_permission_grants(
                   grant_id, approval_id, run_id, group_id, payload_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (grant.grant_id, grant.approval_id, grant.run_id, grant.group_id,
             _dumps(grant.to_dict()), grant.created_at),
        )

    def save_tool(self, invocation: ToolInvocation) -> tuple[ToolInvocation, bool]:
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT payload_json FROM studycrew_tool_invocations WHERE run_id=? AND idempotency_key=?",
                (invocation.run_id, invocation.idempotency_key),
            ).fetchone()
            if existing is not None:
                return self._tool_from_raw(_loads(existing["payload_json"])), False
            conn.execute(
                """INSERT INTO studycrew_tool_invocations(
                       invocation_id, run_id, group_id, idempotency_key, risk, status,
                       replay_allowed, payload_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (invocation.invocation_id, invocation.run_id, invocation.group_id,
                 invocation.idempotency_key, invocation.risk.value, invocation.status,
                 int(invocation.replay_allowed), _dumps(invocation.to_dict())),
            )
        return invocation, True

    @staticmethod
    def _tool_from_raw(raw: Mapping[str, Any]) -> ToolInvocation:
        return ToolInvocation(
            invocation_id=str(raw["invocation_id"]), run_id=str(raw["run_id"]),
            group_id=str(raw["group_id"]), tool_name=str(raw["tool_name"]),
            idempotency_key=str(raw["idempotency_key"]), risk=RiskLevel(str(raw["risk"])),
            status=str(raw.get("status") or "prepared"), replay_allowed=bool(raw.get("replay_allowed")),
            public_summary=str(raw.get("public_summary") or ""),
            created_at=str(raw.get("created_at") or utc_now()), completed_at=raw.get("completed_at"),
        )

    def incomplete_tools(self) -> tuple[ToolInvocation, ...]:
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM studycrew_tool_invocations WHERE status IN ('prepared','running')"
            ).fetchall()
        return tuple(self._tool_from_raw(_loads(row["payload_json"])) for row in rows)

    def save_checkpoint(
        self, checkpoint: Checkpoint, run: TaskRun, *, idempotency_key: str,
    ) -> None:
        with self.db.transaction() as conn:
            self.insert_checkpoint_in_transaction(
                conn, checkpoint, run, idempotency_key=idempotency_key,
            )

    @staticmethod
    def insert_checkpoint_in_transaction(
        conn: Any, checkpoint: Checkpoint, run: TaskRun, *, idempotency_key: str,
    ) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO studycrew_checkpoints(
                   checkpoint_id, run_id, group_id, seq, version, payload_json,
                   snapshot_json, created_at, idempotency_key
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (checkpoint.checkpoint_id, checkpoint.run_id, checkpoint.group_id,
             checkpoint.seq, checkpoint.version, _dumps(checkpoint.to_dict()),
             _dumps(run.to_dict()), checkpoint.created_at, idempotency_key),
        )

    def checkpoint_by_key(self, run_id: str, idempotency_key: str) -> Checkpoint | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT payload_json FROM studycrew_checkpoints WHERE run_id=? AND idempotency_key=?",
                (run_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        raw = _loads(row["payload_json"])
        return Checkpoint(
            checkpoint_id=str(raw["checkpoint_id"]), run_id=str(raw["run_id"]),
            group_id=str(raw["group_id"]), seq=int(raw["seq"]), version=int(raw["version"]),
            label=str(raw["label"]), resumable=bool(raw.get("resumable", True)),
            created_at=str(raw.get("created_at") or utc_now()),
        )

    def latest_checkpoint(self, run_id: str) -> tuple[Checkpoint, TaskRun] | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT * FROM studycrew_checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        raw = _loads(row["payload_json"])
        checkpoint = Checkpoint(
            checkpoint_id=str(raw["checkpoint_id"]), run_id=str(raw["run_id"]),
            group_id=str(raw["group_id"]), seq=int(raw["seq"]), version=int(raw["version"]),
            label=str(raw["label"]), resumable=bool(raw.get("resumable", True)),
            created_at=str(raw.get("created_at") or utc_now()),
        )
        return checkpoint, TaskRun.from_dict(_loads(row["snapshot_json"]))

    def scheduler_cursor(self) -> str:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT value FROM studycrew_scheduler_state WHERE name='group_cursor'"
            ).fetchone()
        return "" if row is None else str(row["value"])

    def set_scheduler_cursor(self, group_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO studycrew_scheduler_state(name, value) VALUES ('group_cursor', ?)
                   ON CONFLICT(name) DO UPDATE SET value=excluded.value""", (group_id,),
            )

    def delete_group(self, group_id: str) -> dict[str, int]:
        """Cascade runtime content while retaining no title/event/PII receipt."""
        with self.db.transaction() as conn:
            counts = {
                "runs": int(conn.execute(
                    "SELECT COUNT(*) FROM studycrew_task_runs WHERE group_id=?", (group_id,),
                ).fetchone()[0]),
                "events": int(conn.execute(
                    "SELECT COUNT(*) FROM studycrew_run_events WHERE group_id=?", (group_id,),
                ).fetchone()[0]),
                "approvals": int(conn.execute(
                    "SELECT COUNT(*) FROM studycrew_approvals WHERE group_id=?", (group_id,),
                ).fetchone()[0]),
                "grants": int(conn.execute(
                    "SELECT COUNT(*) FROM studycrew_permission_grants WHERE group_id=?", (group_id,),
                ).fetchone()[0]),
                "tools": int(conn.execute(
                    "SELECT COUNT(*) FROM studycrew_tool_invocations WHERE group_id=?", (group_id,),
                ).fetchone()[0]),
                "checkpoints": int(conn.execute(
                    "SELECT COUNT(*) FROM studycrew_checkpoints WHERE group_id=?", (group_id,),
                ).fetchone()[0]),
            }
            for table in (
                "studycrew_checkpoints", "studycrew_tool_invocations",
                "studycrew_permission_grants", "studycrew_approvals",
                "studycrew_run_events", "studycrew_task_runs",
            ):
                conn.execute(f"DELETE FROM {table} WHERE group_id=?", (group_id,))
        return counts


__all__ = ["RuntimeStoreError", "TaskRuntimeStore"]
