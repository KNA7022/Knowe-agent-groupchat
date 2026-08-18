from __future__ import annotations

"""SQLite journal for the authoritative Task Runtime.

Run snapshots and their public/private event payloads commit in one transaction.
The event journal is durable and append-only; reconnect never depends on a memory
ring and therefore can prove whether a client cursor is contiguous.
"""

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from knowe_storage._sqlite import SQLiteDatabase

from .contracts import (
    ApprovalRequest,
    Checkpoint,
    EvidenceRef,
    PermissionGrant,
    RiskLevel,
    RunEvent,
    TaskRun,
    TaskStatus,
    ToolInvocation,
    VerifierDecision,
    classify_public_text,
    normalize_legacy_public_payload,
    opaque_id,
    utc_now,
)

log = logging.getLogger(__name__)


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
        self._event_listeners: list[Callable[[RunEvent], None]] = []
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
            CREATE TABLE IF NOT EXISTS studycrew_scheduler_lease (
                lease_name TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS studycrew_verifier_decisions (
                decision_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                run_version INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                approved INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, run_version)
            );
            CREATE TABLE IF NOT EXISTS studycrew_executor_continuations (
                continuation_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                approval_id TEXT,
                execution_key TEXT NOT NULL,
                route_request_key TEXT NOT NULL,
                route_version INTEGER NOT NULL,
                risk TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(run_id, execution_key),
                UNIQUE(approval_id)
            );
            CREATE TABLE IF NOT EXISTS studycrew_group_deletions (
                group_id TEXT PRIMARY KEY,
                group_transaction_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS studycrew_migration_tombstones (
                tombstone_id TEXT PRIMARY KEY,
                record_type TEXT NOT NULL,
                record_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                winner_id TEXT NOT NULL,
                resolution TEXT NOT NULL,
                migrated_at TEXT NOT NULL,
                UNIQUE(record_type, record_id)
            );
            """
        )
        # Development snapshots may have been created by an earlier Task Runtime v1
        # build.  Keep this migration idempotent before enforcing new key indexes.
        self.db.ensure_columns("studycrew_approvals", {"request_key": "TEXT"})
        self.db.ensure_columns("studycrew_checkpoints", {"idempotency_key": "TEXT"})
        self.db.ensure_columns("studycrew_migration_tombstones", {"winner_id": "TEXT NOT NULL DEFAULT ''"})
        self._migrate_v1_side_records()
        self.db.initialize(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_studycrew_one_pending_approval
              ON studycrew_approvals(run_id) WHERE status='pending';
            CREATE UNIQUE INDEX IF NOT EXISTS idx_studycrew_approval_request_key
              ON studycrew_approvals(request_key) WHERE request_key IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_studycrew_checkpoint_request_key
              ON studycrew_checkpoints(run_id, idempotency_key)
              WHERE idempotency_key IS NOT NULL;
            """
        )

    def _migrate_v1_side_records(self) -> None:
        """Normalize the immediately preceding Task Runtime v1 shape.

        The migration deliberately runs before the new unique pending-approval index.
        It is one SQLite transaction, uses deterministic winners, and retains only an
        opaque audit tombstone for superseded approvals.
        """
        migrated_at = utc_now()
        with self.db.transaction() as conn:
            for row in conn.execute("SELECT * FROM studycrew_run_events").fetchall():
                raw = _loads(row["public_payload_json"])
                scrubbed = normalize_legacy_public_payload(str(row["event_type"]), raw)
                conn.execute(
                    "UPDATE studycrew_run_events SET public_payload_json=? WHERE event_id=?",
                    (_dumps(scrubbed), str(row["event_id"])),
                )

            for row in conn.execute("SELECT * FROM studycrew_task_runs").fetchall():
                raw = _loads(row["payload_json"])
                labels: list[str] = list(raw.get("data_classification") or ())
                for field_name, fallback in (
                    ("title", "Protected task"),
                    ("public_summary", "Protected task status"),
                    ("blocking_reason", "Protected task is waiting"),
                ):
                    value = str(raw.get(field_name) or "")
                    found = classify_public_text(value)
                    if found:
                        raw[field_name] = fallback
                        labels.extend(found)
                for agent in raw.get("agent_runs") or ():
                    if not isinstance(agent, dict):
                        continue
                    for field_name, fallback in (
                        ("operation_summary", "Agent operation protected"),
                        ("blocking_reason", "Agent is waiting"),
                    ):
                        found = classify_public_text(str(agent.get(field_name) or ""))
                        if found:
                            agent[field_name] = fallback
                            labels.extend(found)
                raw["data_classification"] = list(dict.fromkeys(str(item) for item in labels))
                conn.execute(
                    "UPDATE studycrew_task_runs SET payload_json=? WHERE run_id=?",
                    (_dumps(raw), str(row["run_id"])),
                )

            approval_rows = conn.execute(
                "SELECT * FROM studycrew_approvals ORDER BY run_id, created_at, approval_id"
            ).fetchall()
            pending_winner: dict[str, str] = {}
            for row in approval_rows:
                raw = _loads(row["payload_json"])
                raw.setdefault("requested_capabilities", [])
                raw.setdefault("tool_name", "")
                raw.setdefault("operation_scope", {})
                raw.setdefault("purpose", "legacy_confirm_route")
                raw.setdefault("schema_version", 1)
                raw.setdefault("status", str(row["status"] or "pending"))
                if classify_public_text(str(raw.get("summary") or "")):
                    raw["summary"] = "Protected approval request"
                status = str(row["status"] or raw.get("status") or "pending")
                run_id = str(row["run_id"])
                if status == "pending" and run_id in pending_winner:
                    status = "rejected"
                    raw["status"] = status
                    raw["summary"] = "Superseded during Task Runtime migration"
                    raw["resolved_at"] = migrated_at
                    conn.execute(
                        """INSERT OR IGNORE INTO studycrew_migration_tombstones(
                               tombstone_id, record_type, record_id, run_id, winner_id,
                               resolution, migrated_at
                           ) VALUES (?, 'approval', ?, ?, ?, 'superseded_duplicate_pending', ?)""",
                        (f"mig_{str(row['approval_id']).replace('_', '')[:32]}",
                         str(row["approval_id"]), run_id, pending_winner[run_id], migrated_at),
                    )
                elif status == "pending":
                    pending_winner[run_id] = str(row["approval_id"])
                raw["status"] = status
                conn.execute(
                    """UPDATE studycrew_approvals
                       SET status=?, payload_json=?, resolved_at=? WHERE approval_id=?""",
                    (status, _dumps(raw), raw.get("resolved_at"), str(row["approval_id"])),
                )

            for row in conn.execute("SELECT * FROM studycrew_permission_grants").fetchall():
                raw = _loads(row["payload_json"])
                raw.setdefault("tool_name", "")
                raw.setdefault("operation_scope", {})
                raw.setdefault("purpose", "legacy_approved_operation")
                raw.setdefault("schema_version", 1)
                conn.execute(
                    "UPDATE studycrew_permission_grants SET payload_json=? WHERE grant_id=?",
                    (_dumps(raw), str(row["grant_id"])),
                )

            for row in conn.execute("SELECT * FROM studycrew_tool_invocations").fetchall():
                raw = _loads(row["payload_json"])
                raw.setdefault("operation_scope", {})
                raw.setdefault("purpose", "legacy_execute_tool")
                raw.setdefault(
                    "disposition",
                    "recovered_blocked" if str(row["status"]) == "recovered_blocked" else "created",
                )
                raw.setdefault("schema_version", 1)
                if classify_public_text(str(raw.get("public_summary") or "")):
                    raw["public_summary"] = "Protected tool operation"
                conn.execute(
                    "UPDATE studycrew_tool_invocations SET payload_json=? WHERE invocation_id=?",
                    (_dumps(raw), str(row["invocation_id"])),
                )

    @staticmethod
    def _assert_group_mutable_in_transaction(conn: Any, group_id: str) -> None:
        fenced = conn.execute(
            "SELECT 1 FROM studycrew_group_deletions WHERE group_id=? LIMIT 1", (group_id,),
        ).fetchone()
        if fenced is not None:
            raise RuntimeStoreError(f"Task Runtime group is fenced for deletion: {group_id}")

    def group_is_deleting(self, group_id: str) -> bool:
        with self.db.transaction(immediate=False) as conn:
            return conn.execute(
                "SELECT 1 FROM studycrew_group_deletions WHERE group_id=? LIMIT 1", (group_id,),
            ).fetchone() is not None

    def close(self) -> None:
        self.db.close()

    def add_event_listener(self, listener: Callable[[RunEvent], None]) -> None:
        self._event_listeners.append(listener)

    def _publish(self, event: RunEvent) -> None:
        for listener in tuple(self._event_listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 -- a transport subscriber cannot undo durable state
                log.exception("Task Runtime event listener failed after commit: %s", event.event_id)

    @staticmethod
    def _row_run(row: Any) -> TaskRun:
        return TaskRun.from_dict(_loads(str(row["payload_json"])))

    def create_run(self, run: TaskRun, *, request_key: str) -> tuple[TaskRun, RunEvent, bool]:
        if not request_key.strip():
            raise ValueError("request_key is required")
        with self.db.transaction() as conn:
            self._assert_group_mutable_in_transaction(conn, run.group_id)
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
                                "route_label": run.route_label.value, "summary": run.public_summary,
                                "classification": list(run.data_classification)},
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
        self._publish(event)
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
        public_payload: Mapping[str, Any] | Callable[[TaskRun], Mapping[str, Any]],
        idempotency_key: str,
        mutate: Callable[[TaskRun], None] | None = None,
        sensitive_payload: Mapping[str, Any] | None = None,
        sensitivity: str = "public",
        transaction_hook: Callable[[Any, TaskRun, RunEvent], None] | None = None,
    ) -> tuple[TaskRun, RunEvent, bool]:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        with self.db.transaction() as conn:
            owner_row = conn.execute(
                "SELECT group_id FROM studycrew_task_runs WHERE run_id=?", (run_id,),
            ).fetchone()
            if owner_row is None:
                raise RuntimeStoreError(f"run not found: {run_id}")
            self._assert_group_mutable_in_transaction(conn, str(owner_row["group_id"]))
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
            expected_version = run.version
            if mutate is not None:
                mutate(run)
            run.version += 1
            run.last_seq += 1
            run.updated_at = utc_now()
            event_payload = dict(public_payload(run) if callable(public_payload) else public_payload)
            event = RunEvent(
                event_id=opaque_id("evt"), run_id=run.run_id, group_id=run.group_id,
                seq=run.last_seq, version=run.version, event_type=event_type,
                public_payload=event_payload, sensitivity=sensitivity,
                sensitive_payload=dict(sensitive_payload or {}),
            )
            conn.execute(
                """UPDATE studycrew_task_runs SET status=?, version=?, last_seq=?,
                       payload_json=?, updated_at=? WHERE run_id=? AND version=?""",
                (run.status.value, run.version, run.last_seq, _dumps(run.to_dict()),
                 run.updated_at, run.run_id, expected_version),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise RuntimeStoreError("TaskRun compare-and-swap conflict")
            self._insert_event(conn, event, request_key=idempotency_key)
            if transaction_hook is not None:
                transaction_hook(conn, run, event)
        self._publish(event)
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
            self._assert_group_mutable_in_transaction(conn, approval.group_id)
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

    @staticmethod
    def resolve_pending_approval_in_transaction(conn: Any, approval: ApprovalRequest) -> None:
        cursor = conn.execute(
            """UPDATE studycrew_approvals
               SET status=?, payload_json=?, resolved_at=?
               WHERE approval_id=? AND run_id=? AND group_id=? AND status='pending'""",
            (approval.status, _dumps(approval.to_dict()), approval.resolved_at,
             approval.approval_id, approval.run_id, approval.group_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeStoreError("approval is no longer pending")

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
        return self._approval_from_raw(_loads(row["payload_json"]))

    @staticmethod
    def _approval_from_raw(raw: Mapping[str, Any]) -> ApprovalRequest:
        return ApprovalRequest(
            approval_id=str(raw["approval_id"]), run_id=str(raw["run_id"]),
            group_id=str(raw["group_id"]), kind=str(raw["kind"]), summary=str(raw["summary"]),
            risk=RiskLevel(str(raw["risk"])), status=str(raw.get("status") or "pending"),
            requested_capabilities=tuple(str(item) for item in raw.get("requested_capabilities") or ()),
            tool_name=str(raw.get("tool_name") or ""),
            operation_scope={str(key): str(value) for key, value in dict(raw.get("operation_scope") or {}).items()},
            purpose=str(raw.get("purpose") or ""), expires_at=raw.get("expires_at"),
            created_at=str(raw.get("created_at") or utc_now()), resolved_at=raw.get("resolved_at"),
            schema_version=int(raw.get("schema_version", 1)),
        )

    def pending_approvals(self, group_id: str) -> tuple[ApprovalRequest, ...]:
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(
                "SELECT approval_id FROM studycrew_approvals WHERE group_id=? AND status='pending' ORDER BY created_at",
                (group_id,),
            ).fetchall()
        return tuple(item for row in rows if (item := self.get_approval(str(row["approval_id"]))) is not None)

    def all_pending_approvals(self) -> tuple[ApprovalRequest, ...]:
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM studycrew_approvals WHERE status='pending' ORDER BY created_at"
            ).fetchall()
        return tuple(self._approval_from_raw(_loads(row["payload_json"])) for row in rows)

    def reject_pending_approval(
        self, approval_id: str, *, resolution: str, resolved_at: str,
    ) -> ApprovalRequest | None:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT group_id, payload_json FROM studycrew_approvals "
                "WHERE approval_id=? AND status='pending'",
                (approval_id,),
            ).fetchone()
            if row is not None:
                self._assert_group_mutable_in_transaction(conn, str(row["group_id"]))
                raw = _loads(row["payload_json"])
                raw.update({
                    "status": "rejected", "resolved_at": resolved_at,
                    "summary": "Approval is no longer actionable",
                    "terminal_resolution": resolution,
                })
                conn.execute(
                    "UPDATE studycrew_approvals SET status='rejected', payload_json=?, "
                    "resolved_at=? WHERE approval_id=? AND status='pending'",
                    (_dumps(raw), resolved_at, approval_id),
                )
        return self.get_approval(approval_id)

    def normalize_pending_approval_expiry(
        self, approval_id: str, *, expires_at: str,
    ) -> ApprovalRequest | None:
        """Canonicalize a legacy pending expiry without changing approval authority."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT group_id, payload_json FROM studycrew_approvals "
                "WHERE approval_id=? AND status='pending'",
                (approval_id,),
            ).fetchone()
            if row is not None:
                self._assert_group_mutable_in_transaction(conn, str(row["group_id"]))
                raw = _loads(row["payload_json"])
                raw["expires_at"] = expires_at
                raw["expiry_normalized"] = True
                conn.execute(
                    "UPDATE studycrew_approvals SET payload_json=? "
                    "WHERE approval_id=? AND status='pending'",
                    (_dumps(raw), approval_id),
                )
        return self.get_approval(approval_id)

    @staticmethod
    def reject_pending_approvals_in_transaction(
        conn: Any, *, run_id: str, resolution: str, resolved_at: str,
    ) -> tuple[str, ...]:
        """Atomically remove every actionable approval owned by a terminalized run."""
        rows = conn.execute(
            "SELECT approval_id, payload_json FROM studycrew_approvals "
            "WHERE run_id=? AND status='pending' ORDER BY created_at, approval_id",
            (run_id,),
        ).fetchall()
        changed: list[str] = []
        for row in rows:
            raw = _loads(row["payload_json"])
            raw["status"] = "rejected"
            raw["resolved_at"] = resolved_at
            # Resolution is an opaque lifecycle classification, never user content.
            raw["summary"] = "Approval is no longer actionable"
            raw["terminal_resolution"] = resolution
            conn.execute(
                "UPDATE studycrew_approvals SET status='rejected', payload_json=?, "
                "resolved_at=? WHERE approval_id=? AND status='pending'",
                (_dumps(raw), resolved_at, str(row["approval_id"])),
            )
            if conn.execute("SELECT changes()").fetchone()[0] == 1:
                changed.append(str(row["approval_id"]))
        return tuple(changed)

    def save_grant(self, grant: PermissionGrant) -> None:
        with self.db.transaction() as conn:
            self._assert_group_mutable_in_transaction(conn, grant.group_id)
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
            tool_name=str(raw.get("tool_name") or ""),
            operation_scope={str(key): str(value) for key, value in dict(raw.get("operation_scope") or {}).items()},
            purpose=str(raw.get("purpose") or ""),
            expires_at=str(raw["expires_at"]) if raw.get("expires_at") else None,
            created_at=str(raw.get("created_at") or utc_now()),
            schema_version=int(raw.get("schema_version", 1)),
        )

    def revoke_group_grants(self, group_id: str) -> int:
        with self.db.transaction() as conn:
            self._assert_group_mutable_in_transaction(conn, group_id)
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
            self._assert_group_mutable_in_transaction(conn, invocation.group_id)
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

    def tool_by_key(self, run_id: str, idempotency_key: str) -> ToolInvocation | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT payload_json FROM studycrew_tool_invocations WHERE run_id=? AND idempotency_key=?",
                (run_id, idempotency_key),
            ).fetchone()
        return None if row is None else self._tool_from_raw(_loads(row["payload_json"]))

    @staticmethod
    def _tool_from_raw(raw: Mapping[str, Any]) -> ToolInvocation:
        return ToolInvocation(
            invocation_id=str(raw["invocation_id"]), run_id=str(raw["run_id"]),
            group_id=str(raw["group_id"]), tool_name=str(raw["tool_name"]),
            idempotency_key=str(raw["idempotency_key"]), risk=RiskLevel(str(raw["risk"])),
            status=str(raw.get("status") or "prepared"), replay_allowed=bool(raw.get("replay_allowed")),
            public_summary=str(raw.get("public_summary") or ""),
            operation_scope={str(key): str(value) for key, value in dict(raw.get("operation_scope") or {}).items()},
            purpose=str(raw.get("purpose") or ""),
            disposition=str(raw.get("disposition") or "created"),
            created_at=str(raw.get("created_at") or utc_now()), completed_at=raw.get("completed_at"),
            schema_version=int(raw.get("schema_version", 1)),
        )

    def incomplete_tools(self) -> tuple[ToolInvocation, ...]:
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM studycrew_tool_invocations WHERE status IN ('prepared','running')"
            ).fetchall()
        return tuple(self._tool_from_raw(_loads(row["payload_json"])) for row in rows)

    @staticmethod
    def insert_tool_in_transaction(conn: Any, invocation: ToolInvocation) -> None:
        conn.execute(
            """INSERT INTO studycrew_tool_invocations(
                   invocation_id, run_id, group_id, idempotency_key, risk, status,
                   replay_allowed, payload_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (invocation.invocation_id, invocation.run_id, invocation.group_id,
             invocation.idempotency_key, invocation.risk.value, invocation.status,
             int(invocation.replay_allowed), _dumps(invocation.to_dict())),
        )

    def mark_tool_recovered_blocked(self, invocation_id: str) -> ToolInvocation:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM studycrew_tool_invocations WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if row is None:
                raise RuntimeStoreError(f"tool invocation not found: {invocation_id}")
            current = self._tool_from_raw(_loads(row["payload_json"]))
            self._assert_group_mutable_in_transaction(conn, current.group_id)
            blocked = replace(
                current, status="recovered_blocked", replay_allowed=False,
                disposition="recovered_blocked",
            )
            conn.execute(
                "UPDATE studycrew_tool_invocations SET status=?, replay_allowed=0, payload_json=? WHERE invocation_id=?",
                (blocked.status, _dumps(blocked.to_dict()), invocation_id),
            )
        return blocked

    def save_verifier_decision_in_transaction(
        self, conn: Any, decision: VerifierDecision,
    ) -> None:
        conn.execute(
            """INSERT INTO studycrew_verifier_decisions(
                   decision_id, run_id, task_id, group_id, run_version, outcome,
                   approved, payload_json, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (decision.decision_id, decision.run_id, decision.task_id, decision.group_id,
             decision.run_version, decision.outcome.value, int(decision.approved),
             _dumps(decision.to_dict()), decision.created_at),
        )

    @staticmethod
    def insert_executor_continuation_in_transaction(
        conn: Any, *, continuation_id: str, run: TaskRun, approval_id: str | None,
        execution_key: str, route_request_key: str, risk: RiskLevel,
        status: str, private_request: Mapping[str, Any],
        recovery_of_continuation_id: str | None = None,
        private_request_digest: str | None = None,
        inherited_purpose: str | None = None,
    ) -> None:
        now = utc_now()
        payload = {
            "continuation_id": continuation_id, "run_id": run.run_id,
            "task_id": run.task_id, "group_id": run.group_id,
            "approval_id": approval_id, "execution_key": execution_key,
            "route_request_key": route_request_key, "route_version": run.version,
            "risk": risk.value, "status": status,
            "private_request": dict(private_request), "created_at": now, "updated_at": now,
            "recovery_of_continuation_id": recovery_of_continuation_id,
            "private_request_digest": private_request_digest,
            "inherited_purpose": inherited_purpose,
        }
        conn.execute(
            """INSERT OR IGNORE INTO studycrew_executor_continuations(
                   continuation_id, run_id, task_id, group_id, approval_id,
                   execution_key, route_request_key, route_version, risk, status,
                   payload_json, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (continuation_id, run.run_id, run.task_id, run.group_id, approval_id,
             execution_key, route_request_key, run.version, risk.value, status,
             _dumps(payload), now, now),
        )

    @staticmethod
    def update_executor_continuation_in_transaction(
        conn: Any, *, approval_id: str, status: str, run: TaskRun,
    ) -> None:
        row = conn.execute(
            "SELECT payload_json FROM studycrew_executor_continuations WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
        if row is None:
            return
        raw = _loads(row["payload_json"])
        raw["status"] = status
        raw["route_version"] = run.version
        raw["updated_at"] = utc_now()
        conn.execute(
            """UPDATE studycrew_executor_continuations
               SET status=?, route_version=?, payload_json=?, updated_at=?
               WHERE approval_id=?""",
            (status, run.version, _dumps(raw), raw["updated_at"], approval_id),
        )

    def executor_continuation(
        self, *, continuation_id: str | None = None, approval_id: str | None = None,
        run_id: str | None = None, execution_key: str | None = None,
    ) -> dict[str, Any] | None:
        clauses: list[str] = []
        args: list[str] = []
        for column, value in (
            ("continuation_id", continuation_id), ("approval_id", approval_id),
            ("run_id", run_id), ("execution_key", execution_key),
        ):
            if value:
                clauses.append(f"{column}=?")
                args.append(value)
        if not clauses:
            raise ValueError("continuation identity is required")
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT payload_json FROM studycrew_executor_continuations WHERE "
                + " AND ".join(clauses), tuple(args),
            ).fetchone()
        return None if row is None else _loads(row["payload_json"])

    def executor_continuations_for_run(self, run_id: str) -> tuple[dict[str, Any], ...]:
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM studycrew_executor_continuations "
                "WHERE run_id=? ORDER BY created_at, continuation_id",
                (run_id,),
            ).fetchall()
        return tuple(_loads(row["payload_json"]) for row in rows)

    def claim_executor_continuation(self, continuation_id: str) -> tuple[dict[str, Any], bool]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM studycrew_executor_continuations WHERE continuation_id=?",
                (continuation_id,),
            ).fetchone()
            if row is None:
                raise RuntimeStoreError(f"executor continuation not found: {continuation_id}")
            self._assert_group_mutable_in_transaction(conn, str(row["group_id"]))
            claimed = str(row["status"]) == "ready"
            raw = _loads(row["payload_json"])
            if claimed:
                now = utc_now()
                raw["status"] = "claimed"
                raw["updated_at"] = now
                conn.execute(
                    """UPDATE studycrew_executor_continuations
                       SET status='claimed', payload_json=?, updated_at=?
                       WHERE continuation_id=? AND status='ready'""",
                    (_dumps(raw), now, continuation_id),
                )
                claimed = conn.execute("SELECT changes()").fetchone()[0] == 1
            winner = conn.execute(
                "SELECT payload_json FROM studycrew_executor_continuations WHERE continuation_id=?",
                (continuation_id,),
            ).fetchone()
        if winner is None:
            raise RuntimeStoreError(f"executor continuation not found: {continuation_id}")
        return _loads(winner["payload_json"]), claimed

    def finish_executor_continuation(
        self, continuation_id: str, *, status: str, failure_type: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled", "rejected", "recovered_blocked"}:
            raise ValueError("invalid executor continuation terminal status")
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM studycrew_executor_continuations WHERE continuation_id=?",
                (continuation_id,),
            ).fetchone()
            if row is None:
                raise RuntimeStoreError(f"executor continuation not found: {continuation_id}")
            raw = _loads(row["payload_json"])
            if str(row["status"]) in {"completed", "failed", "cancelled", "rejected", "recovered_blocked"}:
                return raw
            now = utc_now()
            raw.update({"status": status, "failure_type": failure_type, "updated_at": now})
            conn.execute(
                """UPDATE studycrew_executor_continuations
                   SET status=?, payload_json=?, updated_at=? WHERE continuation_id=?""",
                (status, _dumps(raw), now, continuation_id),
            )
        return raw

    def terminalize_executor_continuations(self, run_id: str, *, status: str) -> int:
        if status not in {"cancelled", "rejected", "recovered_blocked"}:
            raise ValueError("invalid executor continuation terminal status")
        with self.db.transaction() as conn:
            return self.terminalize_executor_continuations_in_transaction(
                conn, run_id=run_id, status=status,
            )

    @staticmethod
    def terminalize_executor_continuations_in_transaction(
        conn: Any, *, run_id: str, status: str,
    ) -> int:
        changed = 0
        rows = conn.execute(
            """SELECT continuation_id, payload_json
               FROM studycrew_executor_continuations
               WHERE run_id=? AND status IN ('waiting_approval','ready','claimed')""",
            (run_id,),
        ).fetchall()
        for row in rows:
            raw = _loads(row["payload_json"])
            raw["status"] = status
            raw["updated_at"] = utc_now()
            conn.execute(
                """UPDATE studycrew_executor_continuations
                   SET status=?, payload_json=?, updated_at=? WHERE continuation_id=?""",
                (status, _dumps(raw), raw["updated_at"], str(row["continuation_id"])),
            )
            changed += 1
        return changed

    def reconcile_executor_continuations(self) -> dict[str, list[str]]:
        """Never replay a request that may have crossed the executor boundary."""
        report: dict[str, list[str]] = {"ready": [], "recovered_blocked": []}
        with self.db.transaction() as conn:
            rows = conn.execute(
                """SELECT * FROM studycrew_executor_continuations
                   WHERE status IN ('ready','claimed') ORDER BY created_at"""
            ).fetchall()
            for row in rows:
                raw = _loads(row["payload_json"])
                continuation_id = str(row["continuation_id"])
                if str(row["status"]) == "claimed" or str(row["risk"]) == RiskLevel.HIGH.value:
                    raw["status"] = "recovered_blocked"
                    raw["updated_at"] = utc_now()
                    conn.execute(
                        """UPDATE studycrew_executor_continuations
                           SET status='recovered_blocked', payload_json=?, updated_at=?
                           WHERE continuation_id=?""",
                        (_dumps(raw), raw["updated_at"], continuation_id),
                    )
                    if row["approval_id"]:
                        # The uncertain execution boundary invalidates the old grant.
                        # Its ApprovalRequest remains as audit history; only a fresh
                        # linked approval may authorize another attempt.
                        conn.execute(
                            "DELETE FROM studycrew_permission_grants WHERE approval_id=?",
                            (str(row["approval_id"]),),
                        )
                    report["recovered_blocked"].append(continuation_id)
                else:
                    report["ready"].append(continuation_id)
        return report

    def latest_verifier_decision(self, run_id: str) -> VerifierDecision | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT payload_json FROM studycrew_verifier_decisions WHERE run_id=? ORDER BY run_version DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        raw = _loads(row["payload_json"])
        return VerifierDecision(
            decision_id=str(raw["decision_id"]), run_id=str(raw["run_id"]),
            task_id=str(raw["task_id"]), group_id=str(raw["group_id"]),
            verifier_agent_id=str(raw["verifier_agent_id"]), run_version=int(raw["run_version"]),
            outcome=TaskStatus(str(raw["outcome"])), approved=bool(raw["approved"]),
            evidence=tuple(EvidenceRef.from_dict(item) for item in raw.get("evidence") or ()),
            reason=str(raw.get("reason") or ""), created_at=str(raw.get("created_at") or utc_now()),
            schema_version=int(raw.get("schema_version", 1)),
        )

    def verifier_decision_by_request_key(
        self, run_id: str, request_key: str,
    ) -> VerifierDecision | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                """SELECT decision.payload_json
                   FROM studycrew_run_events AS event
                   JOIN studycrew_verifier_decisions AS decision
                     ON decision.run_id=event.run_id AND decision.run_version=event.version
                   WHERE event.run_id=? AND event.idempotency_key=?
                     AND event.event_type='verifier_decision'""",
                (run_id, request_key),
            ).fetchone()
        if row is None:
            return None
        raw = _loads(row["payload_json"])
        return VerifierDecision(
            decision_id=str(raw["decision_id"]), run_id=str(raw["run_id"]),
            task_id=str(raw["task_id"]), group_id=str(raw["group_id"]),
            verifier_agent_id=str(raw["verifier_agent_id"]), run_version=int(raw["run_version"]),
            outcome=TaskStatus(str(raw["outcome"])), approved=bool(raw["approved"]),
            evidence=tuple(EvidenceRef.from_dict(item) for item in raw.get("evidence") or ()),
            reason=str(raw.get("reason") or ""), created_at=str(raw.get("created_at") or utc_now()),
            schema_version=int(raw.get("schema_version", 1)),
        )

    @staticmethod
    def verifier_decision_for_version_in_transaction(
        conn: Any, run_id: str, run_version: int,
    ) -> VerifierDecision | None:
        row = conn.execute(
            """SELECT payload_json FROM studycrew_verifier_decisions
               WHERE run_id=? AND run_version=? ORDER BY created_at DESC LIMIT 1""",
            (run_id, run_version),
        ).fetchone()
        if row is None:
            return None
        raw = _loads(row["payload_json"])
        return VerifierDecision(
            decision_id=str(raw["decision_id"]), run_id=str(raw["run_id"]),
            task_id=str(raw["task_id"]), group_id=str(raw["group_id"]),
            verifier_agent_id=str(raw["verifier_agent_id"]), run_version=int(raw["run_version"]),
            outcome=TaskStatus(str(raw["outcome"])), approved=bool(raw["approved"]),
            evidence=tuple(EvidenceRef.from_dict(item) for item in raw.get("evidence") or ()),
            reason=str(raw.get("reason") or ""), created_at=str(raw.get("created_at") or utc_now()),
            schema_version=int(raw.get("schema_version") or 1),
        )

    def prepare_group_deletion(self, group_id: str, group_transaction_id: str) -> dict[str, str]:
        now = utc_now()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO studycrew_group_deletions(
                       group_id, group_transaction_id, stage, created_at, updated_at
                   ) VALUES (?, ?, 'prepared', ?, ?)
                   ON CONFLICT(group_id) DO NOTHING""",
                (group_id, group_transaction_id, now, now),
            )
            row = conn.execute(
                "SELECT * FROM studycrew_group_deletions WHERE group_id=?", (group_id,),
            ).fetchone()
        if row is None or str(row["group_transaction_id"]) != group_transaction_id:
            raise RuntimeStoreError("group deletion intent conflicts with existing transaction")
        return {key: str(row[key]) for key in row.keys()}

    def set_group_deletion_stage(self, group_id: str, stage: str) -> None:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE studycrew_group_deletions SET stage=?, updated_at=? WHERE group_id=?",
                (stage, utc_now(), group_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeStoreError(f"group deletion intent not found: {group_id}")

    def pending_group_deletions(self) -> tuple[dict[str, str], ...]:
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(
                "SELECT * FROM studycrew_group_deletions WHERE stage != 'complete' ORDER BY created_at"
            ).fetchall()
        return tuple({key: str(row[key]) for key in row.keys()} for row in rows)

    def group_deletion_intents(self) -> tuple[dict[str, str], ...]:
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(
                "SELECT * FROM studycrew_group_deletions ORDER BY created_at"
            ).fetchall()
        return tuple({key: str(row[key]) for key in row.keys()} for row in rows)

    def save_checkpoint(
        self, checkpoint: Checkpoint, run: TaskRun, *, idempotency_key: str,
    ) -> None:
        with self.db.transaction() as conn:
            self._assert_group_mutable_in_transaction(conn, checkpoint.group_id)
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
            schema_version=int(raw.get("schema_version", 1)),
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
            schema_version=int(raw.get("schema_version", 1)),
        )
        return checkpoint, TaskRun.from_dict(_loads(row["snapshot_json"]))

    def scheduler_cursor(self, lane: str) -> str:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT value FROM studycrew_scheduler_state WHERE name=?", (f"group_cursor:{lane}",),
            ).fetchone()
        return "" if row is None else str(row["value"])

    def set_scheduler_cursor(self, lane: str, group_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO studycrew_scheduler_state(name, value) VALUES (?, ?)
                   ON CONFLICT(name) DO UPDATE SET value=excluded.value""",
                (f"group_cursor:{lane}", group_id),
            )

    def acquire_scheduler_lease(self, owner: str, *, now: str, expires_at: str) -> bool:
        """CAS a short durable lease so independent service instances cannot overbook."""
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO studycrew_scheduler_lease(lease_name, owner, expires_at)
                   VALUES ('global', ?, ?)
                   ON CONFLICT(lease_name) DO UPDATE SET owner=excluded.owner,
                       expires_at=excluded.expires_at
                   WHERE studycrew_scheduler_lease.owner=excluded.owner
                      OR studycrew_scheduler_lease.expires_at<=?""",
                (owner, expires_at, now),
            )
            row = conn.execute(
                "SELECT owner FROM studycrew_scheduler_lease WHERE lease_name='global'",
            ).fetchone()
        return row is not None and str(row["owner"]) == owner

    def scheduler_lease_expiry(self) -> str | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT expires_at FROM studycrew_scheduler_lease WHERE lease_name='global'",
            ).fetchone()
        return None if row is None else str(row["expires_at"])

    def release_scheduler_lease(self, owner: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM studycrew_scheduler_lease WHERE lease_name='global' AND owner=?",
                (owner,),
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
                "verifier_decisions": int(conn.execute(
                    "SELECT COUNT(*) FROM studycrew_verifier_decisions WHERE group_id=?", (group_id,),
                ).fetchone()[0]),
                "executor_continuations": int(conn.execute(
                    "SELECT COUNT(*) FROM studycrew_executor_continuations WHERE group_id=?", (group_id,),
                ).fetchone()[0]),
            }
            for table in (
                "studycrew_checkpoints", "studycrew_tool_invocations",
                "studycrew_permission_grants", "studycrew_approvals",
                "studycrew_verifier_decisions", "studycrew_executor_continuations",
                "studycrew_run_events", "studycrew_task_runs",
            ):
                conn.execute(f"DELETE FROM {table} WHERE group_id=?", (group_id,))
        return counts

    def group_content_counts(self, group_id: str) -> dict[str, int]:
        tables = {
            "runs": "studycrew_task_runs", "events": "studycrew_run_events",
            "approvals": "studycrew_approvals", "grants": "studycrew_permission_grants",
            "tools": "studycrew_tool_invocations", "checkpoints": "studycrew_checkpoints",
            "verifier_decisions": "studycrew_verifier_decisions",
            "executor_continuations": "studycrew_executor_continuations",
        }
        with self.db.transaction(immediate=False) as conn:
            return {
                name: int(conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE group_id=?", (group_id,),
                ).fetchone()[0])
                for name, table in tables.items()
            }


__all__ = ["RuntimeStoreError", "TaskRuntimeStore"]
