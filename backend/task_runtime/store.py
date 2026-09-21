from __future__ import annotations

"""SQLite journal for the authoritative Task Runtime.

Run snapshots and their public/private event payloads commit in one transaction.
The event journal is durable and append-only; reconnect never depends on a memory
ring and therefore can prove whether a client cursor is contiguous.
"""

import hashlib
import json
import logging
import threading
from dataclasses import replace
from datetime import datetime, timezone
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


class RecoveryDirtyBudgetError(RuntimeStoreError):
    """A frozen recovery-update log reached its configured hard bound."""

    def __init__(self, group_id: str, *, global_count: int, group_count: int) -> None:
        super().__init__(f"recovery dirty-log budget exhausted for {group_id}")
        self.group_id = group_id
        self.global_count = max(0, int(global_count))
        self.group_count = max(0, int(group_count))


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: str | None) -> dict[str, Any]:
    raw = json.loads(value or "{}")
    return raw if isinstance(raw, dict) else {}


class TaskRuntimeStore:
    def __init__(self, path: str | Path) -> None:
        self.db = SQLiteDatabase(path)
        self.group_deletion_prepare_listener: Callable[[], None] | None = None
        self._event_listeners: list[Callable[[RunEvent], None]] = []
        self._event_listener_lock = threading.Lock()
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
                group_proof_digest TEXT NOT NULL DEFAULT '',
                confirmation_token TEXT NOT NULL DEFAULT '',
                runtime_project_id TEXT NOT NULL DEFAULT '',
                stage TEXT NOT NULL,
                audit_code TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS studycrew_live_recovery (
                group_id TEXT PRIMARY KEY,
                affected_run_id TEXT NOT NULL DEFAULT '',
                through_seq INTEGER NOT NULL,
                through_version INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                recovery_token TEXT NOT NULL UNIQUE,
                generation INTEGER NOT NULL,
                failure_count INTEGER NOT NULL,
                cause_digest TEXT NOT NULL,
                recovery_revision INTEGER NOT NULL DEFAULT 0,
                disconnect_retry INTEGER NOT NULL DEFAULT 0,
                disconnected_at_ns INTEGER NOT NULL DEFAULT 0,
                disconnect_debt_generation INTEGER NOT NULL DEFAULT 0,
                disconnect_fence_id TEXT NOT NULL DEFAULT '',
                disconnect_claim_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_studycrew_live_recovery_group
              ON studycrew_live_recovery(group_id, updated_at);
            CREATE TABLE IF NOT EXISTS studycrew_live_recovery_dirty (
                queue_revision INTEGER PRIMARY KEY,
                group_id TEXT NOT NULL,
                recovery_token TEXT NOT NULL,
                generation INTEGER NOT NULL,
                frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0, 1)),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_studycrew_live_recovery_dirty_queue
              ON studycrew_live_recovery_dirty(queue_revision, group_id);
            CREATE INDEX IF NOT EXISTS idx_studycrew_live_recovery_dirty_group
              ON studycrew_live_recovery_dirty(group_id, queue_revision);
            CREATE TABLE IF NOT EXISTS studycrew_runtime_counters (
                name TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS studycrew_runtime_authority (
                authority_key TEXT PRIMARY KEY,
                epoch TEXT NOT NULL,
                revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS studycrew_live_publication (
                run_id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                last_seq INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_studycrew_live_publication_group
              ON studycrew_live_publication(group_id);
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
        self.db.ensure_columns("studycrew_group_deletions", {
            "group_proof_digest": "TEXT NOT NULL DEFAULT ''",
            "confirmation_token": "TEXT NOT NULL DEFAULT ''",
            "runtime_project_id": "TEXT NOT NULL DEFAULT ''",
            "audit_code": "TEXT NOT NULL DEFAULT ''",
        })
        self._migrate_group_live_recovery()
        self.db.ensure_columns("studycrew_live_recovery", {
            "affected_run_id": "TEXT NOT NULL DEFAULT ''",
            "through_version": "INTEGER NOT NULL DEFAULT 0",
            "recovery_revision": "INTEGER NOT NULL DEFAULT 0",
            "disconnect_retry": "INTEGER NOT NULL DEFAULT 0",
            "disconnected_at_ns": "INTEGER NOT NULL DEFAULT 0",
            "disconnect_debt_generation": "INTEGER NOT NULL DEFAULT 0",
            "disconnect_fence_id": "TEXT NOT NULL DEFAULT ''",
            "disconnect_claim_id": "TEXT NOT NULL DEFAULT ''",
        })
        self._migrate_live_recovery_dirty_queue()
        self._backfill_live_recovery_revisions()
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
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO studycrew_runtime_authority(authority_key, epoch, revision)
                   VALUES ('global', ?, 0)""",
                (opaque_id("epoch"),),
            )

    @staticmethod
    def _bump_group_snapshot_revision_in_transaction(conn: Any, group_id: str) -> None:
        conn.execute(
            """INSERT INTO studycrew_runtime_authority(authority_key, epoch, revision)
               SELECT ?, epoch, 1 FROM studycrew_runtime_authority WHERE authority_key='global'
               ON CONFLICT(authority_key) DO UPDATE SET revision=revision + 1""",
            (f"group:{group_id}",),
        )

    def group_snapshot_authority(self, group_id: str) -> tuple[str, int]:
        with self.db.transaction() as conn:
            global_row = conn.execute(
                "SELECT epoch FROM studycrew_runtime_authority WHERE authority_key='global'",
            ).fetchone()
            if global_row is None:
                raise RuntimeStoreError("Task Runtime authority epoch is unavailable")
            conn.execute(
                """INSERT OR IGNORE INTO studycrew_runtime_authority(authority_key, epoch, revision)
                   VALUES (?, ?, 0)""",
                (f"group:{group_id}", str(global_row["epoch"])),
            )
            row = conn.execute(
                "SELECT epoch, revision FROM studycrew_runtime_authority WHERE authority_key=?",
                (f"group:{group_id}",),
            ).fetchone()
        return str(row["epoch"]), int(row["revision"])

    def group_snapshot_projection(self, group_id: str) -> dict[str, Any]:
        """Read the complete public Group projection at one SQLite consistency point.

        The authority cursor is intentionally selected in the same transaction as runs and
        approvals.  A service must never compose these rows with separate store calls: another
        process may commit a transition between those calls while legitimately advancing the
        same durable revision.
        """
        with self.db.transaction() as conn:
            global_row = conn.execute(
                "SELECT epoch FROM studycrew_runtime_authority WHERE authority_key='global'",
            ).fetchone()
            if global_row is None:
                raise RuntimeStoreError("Task Runtime authority epoch is unavailable")
            conn.execute(
                """INSERT OR IGNORE INTO studycrew_runtime_authority(authority_key, epoch, revision)
                   VALUES (?, ?, 0)""",
                (f"group:{group_id}", str(global_row["epoch"])),
            )
            authority = conn.execute(
                "SELECT epoch, revision FROM studycrew_runtime_authority WHERE authority_key=?",
                (f"group:{group_id}",),
            ).fetchone()
            run_rows = conn.execute(
                """SELECT payload_json FROM studycrew_task_runs
                   WHERE group_id=? ORDER BY created_at, run_id""",
                (group_id,),
            ).fetchall()
            approval_rows = conn.execute(
                """SELECT payload_json FROM studycrew_approvals
                   WHERE group_id=? AND status='pending' ORDER BY created_at, approval_id""",
                (group_id,),
            ).fetchall()
            runs = tuple(self._row_run(row) for row in run_rows)
            approvals = tuple(
                self._approval_from_raw(_loads(row["payload_json"])) for row in approval_rows
            )
            primary = next((
                run for run in reversed(runs)
                if run.kind.value == "primary" and not run.status.terminal
            ), None)
            executing = {"routing", "running", "verifying"}
            index_runs = tuple(run for run in runs if run.queue_class.value == "index")
            if any(run.status.value in executing for run in index_runs):
                indexing_state = "running"
            elif any(run.status is TaskStatus.QUEUED for run in index_runs):
                indexing_state = "queued"
            else:
                latest_index = max(index_runs, key=lambda run: run.updated_at, default=None)
                indexing_state = (
                    "failed" if latest_index and latest_index.status is TaskStatus.FAILED else "idle"
                )
            summary = {
                "active_primary_run_id": primary.run_id if primary else None,
                "primary_task_status": primary.status.value if primary else None,
                "queued_task_count": sum(run.status is TaskStatus.QUEUED for run in runs),
                "active_side_task_count": sum(
                    run.kind.value == "side" and run.queue_class.value == "foreground"
                    and run.status.value in executing for run in runs
                ),
                "pending_approval_count": len(approvals),
                "budget_usage": {
                    "wall_clock_seconds": sum(run.budget_usage.wall_clock_seconds for run in runs),
                    "model_turns": sum(run.budget_usage.model_turns for run in runs),
                    "tool_calls": sum(run.budget_usage.tool_calls for run in runs),
                    "tokens": sum(run.budget_usage.tokens for run in runs),
                    "cost_usd": sum(run.budget_usage.cost_usd for run in runs),
                },
                "indexing_state": indexing_state,
            }
            return {
                "schema_version": 1,
                "group_id": group_id,
                "authority_epoch": str(authority["epoch"]),
                "group_snapshot_revision": int(authority["revision"]),
                "runs": [run.to_public_dict() for run in runs],
                "pending_approvals": [approval.to_dict() for approval in approvals],
                "summary": summary,
            }

    def _migrate_group_live_recovery(self) -> None:
        """Collapse the preceding per-Run outbox markers into one lossless Group intent."""
        with self.db.transaction() as conn:
            columns = {
                str(row["name"]): row for row in conn.execute(
                    "PRAGMA table_info(studycrew_live_recovery)",
                ).fetchall()
            }
            if "generation" in columns and int(columns["group_id"]["pk"]) == 1:
                return
            legacy = conn.execute(
                "SELECT * FROM studycrew_live_recovery ORDER BY created_at, run_id",
            ).fetchall()
            conn.execute("DROP INDEX IF EXISTS idx_studycrew_live_recovery_group")
            conn.execute(
                "ALTER TABLE studycrew_live_recovery RENAME TO studycrew_live_recovery_run_scoped",
            )
            conn.execute(
                """CREATE TABLE studycrew_live_recovery (
                   group_id TEXT PRIMARY KEY,
                   affected_run_id TEXT NOT NULL DEFAULT '',
                   through_seq INTEGER NOT NULL,
                   through_version INTEGER NOT NULL DEFAULT 0,
                   reason TEXT NOT NULL,
                       attempts INTEGER NOT NULL,
                       recovery_token TEXT NOT NULL UNIQUE,
                       generation INTEGER NOT NULL,
                       failure_count INTEGER NOT NULL,
                   cause_digest TEXT NOT NULL,
                    recovery_revision INTEGER NOT NULL DEFAULT 0,
                    disconnect_retry INTEGER NOT NULL DEFAULT 0,
                   disconnected_at_ns INTEGER NOT NULL DEFAULT 0,
                   disconnect_debt_generation INTEGER NOT NULL DEFAULT 0,
                   disconnect_fence_id TEXT NOT NULL DEFAULT '',
                   disconnect_claim_id TEXT NOT NULL DEFAULT '',
                   created_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL
                   )""",
            )
            grouped: dict[str, list[Any]] = {}
            for row in legacy:
                grouped.setdefault(str(row["group_id"]), []).append(row)
            for group_id, rows in grouped.items():
                digest = ""
                for row in rows:
                    digest = hashlib.sha256(
                        f"{digest}|{row['run_id']}|{row['through_seq']}|{row['reason']}".encode(),
                    ).hexdigest()
                newest = rows[-1]
                conn.execute(
                    """INSERT INTO studycrew_live_recovery(
                           group_id, affected_run_id, through_seq, through_version,
                           reason, attempts, recovery_token,
                           generation, failure_count, cause_digest, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        group_id, str(newest["run_id"]), int(newest["through_seq"]),
                        int(newest["through_seq"]),
                        str(newest["reason"]), max(int(row["attempts"]) for row in rows),
                        str(rows[0]["recovery_token"]), len(rows), len(rows), digest,
                        str(rows[0]["created_at"]), str(newest["updated_at"]),
                    ),
                )
            conn.execute("DROP TABLE studycrew_live_recovery_run_scoped")
            conn.execute(
                "CREATE INDEX idx_studycrew_live_recovery_group "
                "ON studycrew_live_recovery(group_id, updated_at)",
            )

    def _backfill_live_recovery_revisions(self) -> None:
        """Give legacy rows a stable, strictly increasing durable pager identity."""
        with self.db.transaction() as conn:
            rows = conn.execute(
                """SELECT group_id FROM studycrew_live_recovery
                   WHERE recovery_revision <= 0
                   ORDER BY updated_at, group_id""",
            ).fetchall()
            current = int(conn.execute(
                "SELECT COALESCE(MAX(recovery_revision), 0) FROM studycrew_live_recovery",
            ).fetchone()[0])
            for row in rows:
                current += 1
                conn.execute(
                    "UPDATE studycrew_live_recovery SET recovery_revision=? WHERE group_id=?",
                    (current, str(row["group_id"])),
                )
            conn.execute(
                """INSERT INTO studycrew_runtime_counters(name, value)
                   VALUES ('live_recovery_revision', ?)
                   ON CONFLICT(name) DO UPDATE SET
                     value=MAX(studycrew_runtime_counters.value, excluded.value)""",
                (current,),
            )
            dirty_current = int(conn.execute(
                "SELECT COALESCE(MAX(queue_revision), 0) "
                "FROM studycrew_live_recovery_dirty",
            ).fetchone()[0])
            conn.execute(
                """INSERT INTO studycrew_runtime_counters(name, value)
                   VALUES ('live_recovery_dirty_revision', ?)
                   ON CONFLICT(name) DO UPDATE SET
                     value=MAX(studycrew_runtime_counters.value, excluded.value)""",
                (dirty_current,),
            )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_studycrew_live_recovery_revision
                   ON studycrew_live_recovery(recovery_revision)""",
            )

    def _migrate_live_recovery_dirty_queue(self) -> None:
        """Make already-issued dirty positions immutable across the schema upgrade."""
        with self.db.transaction() as conn:
            columns = {
                str(row["name"]): row for row in conn.execute(
                    "PRAGMA table_info(studycrew_live_recovery_dirty)",
                ).fetchall()
            }
            if (
                "frozen" in columns
                and int(columns["queue_revision"]["pk"]) == 1
                and int(columns["group_id"]["pk"]) == 0
            ):
                return
            rows = conn.execute(
                """SELECT group_id, recovery_token, generation, queue_revision,
                          created_at, updated_at
                   FROM studycrew_live_recovery_dirty
                   ORDER BY queue_revision, group_id""",
            ).fetchall()
            conn.execute("DROP INDEX IF EXISTS idx_studycrew_live_recovery_dirty_queue")
            conn.execute("DROP INDEX IF EXISTS idx_studycrew_live_recovery_dirty_group")
            conn.execute(
                "ALTER TABLE studycrew_live_recovery_dirty "
                "RENAME TO studycrew_live_recovery_dirty_moving",
            )
            conn.execute(
                """CREATE TABLE studycrew_live_recovery_dirty (
                       queue_revision INTEGER PRIMARY KEY,
                       group_id TEXT NOT NULL,
                       recovery_token TEXT NOT NULL,
                       generation INTEGER NOT NULL,
                       frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0, 1)),
                       created_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL
                   )""",
            )
            for row in rows:
                conn.execute(
                    """INSERT INTO studycrew_live_recovery_dirty(
                           queue_revision, group_id, recovery_token, generation,
                           frozen, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, 1, ?, ?)""",
                    (
                        int(row["queue_revision"]), str(row["group_id"]),
                        str(row["recovery_token"]), int(row["generation"]),
                        str(row["created_at"]), str(row["updated_at"]),
                    ),
                )
            conn.execute("DROP TABLE studycrew_live_recovery_dirty_moving")
            conn.execute(
                """CREATE INDEX idx_studycrew_live_recovery_dirty_queue
                   ON studycrew_live_recovery_dirty(queue_revision, group_id)""",
            )
            conn.execute(
                """CREATE INDEX idx_studycrew_live_recovery_dirty_group
                   ON studycrew_live_recovery_dirty(group_id, queue_revision)""",
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
                raw.setdefault("single_use", True)
                raw.setdefault("consumed_by_invocation_id", "")
                raw.setdefault("consumed_at", None)
                raw.setdefault("schema_version", 1)
                conn.execute(
                    "UPDATE studycrew_permission_grants SET payload_json=? WHERE grant_id=?",
                    (_dumps(raw), str(row["grant_id"])),
                )

            for row in conn.execute("SELECT * FROM studycrew_tool_invocations").fetchall():
                raw = _loads(row["payload_json"])
                owner = conn.execute(
                    "SELECT payload_json FROM studycrew_task_runs WHERE run_id=?",
                    (str(row["run_id"]),),
                ).fetchone()
                owner_raw = _loads(owner["payload_json"]) if owner is not None else {}
                raw.setdefault("task_id", str(owner_raw.get("task_id") or ""))
                raw.setdefault("grant_id", "")
                raw.setdefault("error_type", "")
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
        with self._event_listener_lock:
            if listener not in self._event_listeners:
                self._event_listeners.append(listener)

    def remove_event_listener(self, listener: Callable[[RunEvent], None]) -> None:
        with self._event_listener_lock:
            self._event_listeners = [item for item in self._event_listeners if item != listener]

    def _publish(self, event: RunEvent) -> None:
        with self._event_listener_lock:
            listeners = tuple(self._event_listeners)
        for listener in listeners:
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
            self._bump_group_snapshot_revision_in_transaction(conn, run.group_id)
        self._publish(event)
        return run, event, True

    def get_run(self, run_id: str) -> TaskRun | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT payload_json FROM studycrew_task_runs WHERE run_id=?", (run_id,),
            ).fetchone()
        return None if row is None else self._row_run(row)

    def prune_terminal_runs(self, *, group_id: str, keep: int = 50) -> int:
        """删除某 group 下**终态** run 的多余部分，只保留最近的 `keep` 条（I5）。

        用途：Settings 连接测试每次点击都会建一个短命 run 再取消，于是 run 数量随点击
        次数无界增长。这是**有界保留**而不是级联删除——它刻意做得很窄：

        · 只作用于调用方指定的**单个 group**；
        · 只删**终态** run（RUNNING/排队中的永不删）；
        · 只保留最近 `keep` 条，`keep` 有下限保护；
        · 依赖 `studycrew_run_events.run_id` 的外键 `ON DELETE CASCADE` 一并清事件。

        与 `delete_group` 不同，这里不要求"删除意图证明"：它删的是探针自己产生的、
        不含用户内容的终态运行记录，不是用户数据级联。
        """

        if keep < 1:
            raise RuntimeStoreError("prune must keep at least one run")
        with self.db.transaction() as conn:
            rows = conn.execute(
                """SELECT run_id FROM studycrew_task_runs
                   WHERE group_id=? AND status NOT IN (?, ?, ?, ?)
                   ORDER BY rowid DESC
                   LIMIT -1 OFFSET ?""",
                (
                    group_id,
                    TaskStatus.RUNNING.value,
                    TaskStatus.QUEUED.value,
                    TaskStatus.WAITING_APPROVAL.value,
                    TaskStatus.VERIFYING.value,
                    int(keep),
                ),
            ).fetchall()
            run_ids = [str(row[0]) for row in rows]
            if not run_ids:
                return 0
            placeholders = ",".join("?" for _ in run_ids)
            conn.execute(
                f"DELETE FROM studycrew_task_runs WHERE run_id IN ({placeholders})",
                tuple(run_ids),
            )
        return len(run_ids)

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
            self._bump_group_snapshot_revision_in_transaction(conn, run.group_id)
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
            self._bump_group_snapshot_revision_in_transaction(conn, approval.group_id)

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
                self._bump_group_snapshot_revision_in_transaction(conn, str(row["group_id"]))
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
                self._bump_group_snapshot_revision_in_transaction(conn, str(row["group_id"]))
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
        return self._grant_from_raw(raw)

    def grant_by_id(self, grant_id: str) -> PermissionGrant | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT payload_json FROM studycrew_permission_grants WHERE grant_id=?",
                (grant_id,),
            ).fetchone()
        return None if row is None else self._grant_from_raw(_loads(row["payload_json"]))

    @staticmethod
    def _grant_from_raw(raw: Mapping[str, Any]) -> PermissionGrant:
        return PermissionGrant(
            grant_id=str(raw["grant_id"]), approval_id=str(raw["approval_id"]),
            run_id=str(raw["run_id"]), group_id=str(raw["group_id"]), task_id=str(raw["task_id"]),
            capabilities=tuple(str(item) for item in raw.get("capabilities") or ()),
            tool_name=str(raw.get("tool_name") or ""),
            operation_scope={str(key): str(value) for key, value in dict(raw.get("operation_scope") or {}).items()},
            purpose=str(raw.get("purpose") or ""),
            expires_at=str(raw["expires_at"]) if raw.get("expires_at") else None,
            single_use=bool(raw.get("single_use", True)),
            consumed_by_invocation_id=str(raw.get("consumed_by_invocation_id") or ""),
            consumed_at=str(raw["consumed_at"]) if raw.get("consumed_at") else None,
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

    @classmethod
    def consume_grant_in_transaction(
        cls, conn: Any, *, grant_id: str, invocation_id: str,
        expected: PermissionGrant,
    ) -> PermissionGrant:
        row = conn.execute(
            "SELECT payload_json FROM studycrew_permission_grants WHERE grant_id=?",
            (grant_id,),
        ).fetchone()
        if row is None:
            raise RuntimeStoreError("permission grant is unavailable")
        grant = cls._grant_from_raw(_loads(row["payload_json"]))
        if grant != expected or grant.grant_id != grant_id:
            raise RuntimeStoreError("permission grant authority changed before consumption")
        approval_row = conn.execute(
            "SELECT status, payload_json FROM studycrew_approvals WHERE approval_id=?",
            (grant.approval_id,),
        ).fetchone()
        if approval_row is None or str(approval_row["status"]) != "approved":
            raise RuntimeStoreError("permission grant approval is no longer approved")
        approval = _loads(approval_row["payload_json"])
        if (
            str(approval.get("run_id") or "") != grant.run_id
            or str(approval.get("group_id") or "") != grant.group_id
            or str(approval.get("tool_name") or "") != grant.tool_name
            or str(approval.get("purpose") or "") != grant.purpose
            or {
                str(key): str(value)
                for key, value in dict(approval.get("operation_scope") or {}).items()
            } != grant.operation_scope
        ):
            raise RuntimeStoreError("permission grant no longer matches its approval")
        if grant.expires_at:
            try:
                expiry = datetime.fromisoformat(grant.expires_at.replace("Z", "+00:00"))
                if (
                    expiry.tzinfo is None
                    or expiry.utcoffset() is None
                    or expiry.astimezone(timezone.utc) <= datetime.now(timezone.utc)
                ):
                    raise ValueError
            except (OverflowError, TypeError, ValueError) as exc:
                raise RuntimeStoreError("permission grant expired before consumption") from exc
        if grant.single_use and grant.consumed_by_invocation_id:
            raise RuntimeStoreError("permission grant was already consumed")
        consumed = replace(
            grant,
            consumed_by_invocation_id=invocation_id,
            consumed_at=utc_now(),
        )
        cursor = conn.execute(
            "UPDATE studycrew_permission_grants SET payload_json=? WHERE grant_id=? AND payload_json=?",
            (_dumps(consumed.to_dict()), grant_id, str(row["payload_json"])),
        )
        if cursor.rowcount != 1:
            raise RuntimeStoreError("permission grant consumption lost an authority race")
        return consumed

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
            group_id=str(raw["group_id"]), task_id=str(raw["task_id"]),
            tool_name=str(raw["tool_name"]),
            idempotency_key=str(raw["idempotency_key"]), risk=RiskLevel(str(raw["risk"])),
            status=str(raw.get("status") or "prepared"), replay_allowed=bool(raw.get("replay_allowed")),
            public_summary=str(raw.get("public_summary") or ""),
            operation_scope={str(key): str(value) for key, value in dict(raw.get("operation_scope") or {}).items()},
            purpose=str(raw.get("purpose") or ""),
            grant_id=str(raw.get("grant_id") or ""),
            disposition=str(raw.get("disposition") or "created"),
            error_type=str(raw.get("error_type") or ""),
            created_at=str(raw.get("created_at") or utc_now()),
            started_at=raw.get("started_at"), completed_at=raw.get("completed_at"),
            schema_version=int(raw.get("schema_version", 1)),
        )

    def incomplete_tools(self) -> tuple[ToolInvocation, ...]:
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(
                "SELECT payload_json FROM studycrew_tool_invocations WHERE status IN ('prepared','running')"
            ).fetchall()
        return tuple(self._tool_from_raw(_loads(row["payload_json"])) for row in rows)

    @staticmethod
    def _tool_duration_ms(invocation: ToolInvocation) -> int:
        if not invocation.completed_at:
            return 0
        start_raw = invocation.started_at or invocation.created_at
        try:
            start = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(invocation.completed_at).replace("Z", "+00:00"))
            return max(0, int((end - start).total_seconds() * 1000))
        except (TypeError, ValueError, OverflowError):
            return 0

    def _transition_tool_with_event(
        self,
        invocation_id: str,
        *,
        expected_statuses: frozenset[str],
        status: str,
        event_type: str,
        error_type: str = "",
        disposition: str | None = None,
        replay_allowed: bool | None = None,
    ) -> tuple[ToolInvocation, bool]:
        """Commit an effect state transition and its public audit event together."""

        published: RunEvent | None = None
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT payload_json FROM studycrew_tool_invocations WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if row is None:
                raise RuntimeStoreError(f"tool invocation not found: {invocation_id}")
            current = self._tool_from_raw(_loads(row["payload_json"]))
            self._assert_group_mutable_in_transaction(conn, current.group_id)
            if current.status not in expected_statuses:
                return current, False

            now = utc_now()
            changes: dict[str, Any] = {
                "status": status,
                "error_type": error_type,
            }
            if status == "running":
                changes["started_at"] = now
            else:
                changes["completed_at"] = now
            if disposition is not None:
                changes["disposition"] = disposition
            if replay_allowed is not None:
                changes["replay_allowed"] = replay_allowed
            updated = replace(current, **changes)

            run_row = conn.execute(
                "SELECT payload_json FROM studycrew_task_runs WHERE run_id=?",
                (current.run_id,),
            ).fetchone()
            if run_row is None:
                raise RuntimeStoreError(f"run not found: {current.run_id}")
            run = self._row_run(run_row)
            expected_version = run.version
            run.version += 1
            run.last_seq += 1
            run.updated_at = now
            payload: dict[str, Any] = {
                "status": status,
                "invocation_id": updated.invocation_id,
                "tool_name": updated.tool_name,
                "purpose": updated.purpose,
                "risk": updated.risk.value,
                "started_at": updated.started_at,
                "usage": run.budget_usage.__dict__,
            }
            if status != "running":
                payload.update({
                    "completed_at": updated.completed_at,
                    "duration_ms": self._tool_duration_ms(updated),
                    "error_type": updated.error_type,
                })
            event = RunEvent(
                event_id=opaque_id("evt"), run_id=run.run_id, group_id=run.group_id,
                seq=run.last_seq, version=run.version, event_type=event_type,
                public_payload=payload,
            )
            tool_cursor = conn.execute(
                "UPDATE studycrew_tool_invocations SET status=?, replay_allowed=?, payload_json=? "
                "WHERE invocation_id=? AND status=?",
                (status, int(updated.replay_allowed), _dumps(updated.to_dict()),
                 invocation_id, current.status),
            )
            if tool_cursor.rowcount != 1:
                raise RuntimeStoreError("tool state transition lost an authority race")
            conn.execute(
                """UPDATE studycrew_task_runs SET status=?, version=?, last_seq=?,
                       payload_json=?, updated_at=? WHERE run_id=? AND version=?""",
                (run.status.value, run.version, run.last_seq, _dumps(run.to_dict()),
                 run.updated_at, run.run_id, expected_version),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise RuntimeStoreError("TaskRun compare-and-swap conflict")
            self._insert_event(
                conn, event, request_key=f"tool-transition:{invocation_id}:{status}",
            )
            self._bump_group_snapshot_revision_in_transaction(conn, run.group_id)
            published = event
        if published is not None:
            self._publish(published)
        return updated, True

    def complete_tool(
        self, invocation_id: str, *, status: str, error_type: str = "",
    ) -> ToolInvocation:
        if status not in {"completed", "failed"}:
            raise ValueError("tool terminal status is invalid")
        updated, _changed = self._transition_tool_with_event(
            invocation_id,
            expected_statuses=frozenset({"running"}),
            status=status,
            event_type="tool_completed" if status == "completed" else "tool_failed",
            error_type=error_type,
        )
        return updated

    def claim_tool_execution(self, invocation_id: str) -> tuple[ToolInvocation, bool]:
        """Atomically move exactly one prepared invocation to the effect boundary."""
        return self._transition_tool_with_event(
            invocation_id,
            expected_statuses=frozenset({"prepared"}),
            status="running",
            event_type="tool_claimed",
        )

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
        blocked, _changed = self._transition_tool_with_event(
            invocation_id,
            expected_statuses=frozenset({"prepared", "running"}),
            status="recovered_blocked",
            event_type="tool_recovered_blocked",
            error_type="RestartRecoveryBlocked",
            disposition="recovered_blocked",
            replay_allowed=False,
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

    def prepare_group_deletion(
        self, group_id: str, group_transaction_id: str, *,
        group_proof_digest: str, confirmation_token: str,
        runtime_project_id: str,
    ) -> dict[str, str]:
        if not group_proof_digest or not confirmation_token or not runtime_project_id:
            raise RuntimeStoreError("canonical Group deletion proof is required")
        now = utc_now()
        inserted = False
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO studycrew_group_deletions(
                       group_id, group_transaction_id, group_proof_digest,
                       confirmation_token, runtime_project_id, stage, audit_code,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, 'prepared', '', ?, ?)
                   ON CONFLICT(group_id) DO NOTHING""",
                (
                    group_id, group_transaction_id, group_proof_digest,
                    confirmation_token, runtime_project_id, now, now,
                ),
            )
            inserted = cursor.rowcount == 1
            if inserted:
                conn.execute(
                    """INSERT INTO studycrew_scheduler_state(name, value)
                       VALUES ('group_deletion_revision', '1')
                       ON CONFLICT(name) DO UPDATE SET
                         value=CAST(studycrew_scheduler_state.value AS INTEGER) + 1""",
                )
            row = conn.execute(
                "SELECT * FROM studycrew_group_deletions WHERE group_id=?", (group_id,),
            ).fetchone()
        if row is None or any((
            str(row["group_transaction_id"]) != group_transaction_id,
            str(row["group_proof_digest"]) != group_proof_digest,
            str(row["confirmation_token"]) != confirmation_token,
            str(row["runtime_project_id"]) != runtime_project_id,
        )):
            raise RuntimeStoreError("group deletion intent conflicts with existing transaction")
        if inserted and self.group_deletion_prepare_listener is not None:
            self.group_deletion_prepare_listener()
        return {key: str(row[key]) for key in row.keys()}

    def group_deletion_revision(self) -> int:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT value FROM studycrew_scheduler_state WHERE name='group_deletion_revision'",
            ).fetchone()
        return 0 if row is None else max(0, int(row["value"]))

    def bind_group_deletion_proof(
        self, group_id: str, group_transaction_id: str, *,
        group_proof_digest: str, confirmation_token: str,
        runtime_project_id: str,
    ) -> dict[str, str]:
        """Backfill proof only for an exact legacy runtime intent."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE studycrew_group_deletions
                   SET group_proof_digest=?, confirmation_token=?, runtime_project_id=?,
                       audit_code='', updated_at=?
                   WHERE group_id=? AND group_transaction_id=?
                     AND group_proof_digest='' AND confirmation_token=''
                     AND runtime_project_id=''""",
                (
                    group_proof_digest, confirmation_token, runtime_project_id,
                    utc_now(), group_id, group_transaction_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM studycrew_group_deletions WHERE group_id=?", (group_id,),
            ).fetchone()
        if cursor.rowcount != 1 or row is None:
            raise RuntimeStoreError("legacy Group deletion proof cannot be bound")
        return {key: str(row[key]) for key in row.keys()}

    def quarantine_group_deletion(self, group_id: str, audit_code: str) -> None:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE studycrew_group_deletions
                   SET stage='quarantined', audit_code=?, updated_at=?
                   WHERE group_id=?""",
                (str(audit_code)[:120], utc_now(), group_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeStoreError(f"group deletion intent not found: {group_id}")

    def set_group_deletion_stage(self, group_id: str, stage: str) -> None:
        if stage not in {"runtime_cleaned", "complete"}:
            raise RuntimeStoreError("invalid Task Runtime deletion stage")
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

    def group_deletion_intent(self, group_id: str) -> dict[str, str] | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT * FROM studycrew_group_deletions WHERE group_id=?",
                (group_id,),
            ).fetchone()
        return None if row is None else {key: str(row[key]) for key in row.keys()}

    def group_deletion_intents_page(
        self, *, cursor: str = "", limit: int = 64,
    ) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 256))
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(
                """SELECT * FROM studycrew_group_deletions
                   WHERE group_id>? ORDER BY group_id LIMIT ?""",
                (str(cursor or ""), bounded + 1),
            ).fetchall()
        has_more = len(rows) > bounded
        visible = rows[:bounded]
        next_cursor = str(visible[-1]["group_id"]) if has_more and visible else ""
        return {
            "items": tuple(
                {key: str(row[key]) for key in row.keys()} for row in visible
            ),
            "cursor": next_cursor,
            "has_more": has_more,
            "limit": bounded,
        }

    def deletion_reconcile_cursor(self, owner: str) -> str:
        name = f"deletion_cursor:{owner}"
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT value FROM studycrew_scheduler_state WHERE name=?", (name,),
            ).fetchone()
        return "" if row is None else str(row["value"])

    def set_deletion_reconcile_cursor(self, owner: str, cursor: str) -> None:
        name = f"deletion_cursor:{owner}"
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO studycrew_scheduler_state(name, value) VALUES (?, ?)
                   ON CONFLICT(name) DO UPDATE SET value=excluded.value""",
                (name, str(cursor or "")),
            )

    def set_deletion_reconcile_checkpoints(
        self, checkpoints: Mapping[str, str],
    ) -> None:
        """Atomically publish bounded source cursors, sweep debt, and next turn."""
        values = tuple(
            (f"deletion_cursor:{str(owner)}", str(value or ""))
            for owner, value in checkpoints.items()
        )
        if not values or len(values) > 8:
            raise RuntimeStoreError("invalid deletion reconciliation checkpoint")
        with self.db.transaction() as conn:
            conn.executemany(
                """INSERT INTO studycrew_scheduler_state(name, value) VALUES (?, ?)
                   ON CONFLICT(name) DO UPDATE SET value=excluded.value""",
                values,
            )

    def record_live_recovery(
        self, *, run_id: str, group_id: str, through_seq: int, reason: str,
        attempts: int, recovery_token: str, run_version: int | None = None,
        dirty_revision_floor: int = 0, generation_floor: int = 0,
        failure_count_floor: int = 0, dirty_global_limit: int | None = None,
        dirty_group_limit: int | None = None,
    ) -> dict[str, Any]:
        """Upsert a mutable generation without moving its immutable queue position."""
        now = utc_now()
        with self.db.transaction() as conn:
            previous = conn.execute(
                "SELECT * FROM studycrew_live_recovery WHERE group_id=?", (group_id,),
            ).fetchone()
            token = str(previous["recovery_token"]) if previous is not None else recovery_token
            generation = max(
                (int(previous["generation"]) if previous is not None else 0) + 1,
                max(0, int(generation_floor)),
            )
            failure_count = max(
                (int(previous["failure_count"]) if previous is not None else 0) + 1,
                max(0, int(failure_count_floor)),
            )
            old_digest = str(previous["cause_digest"]) if previous is not None else ""
            cause_digest = hashlib.sha256(
                f"{old_digest}|{run_id}|{through_seq}|{run_version or through_seq}|{reason}|{attempts}".encode(),
            ).hexdigest()
            created_at = str(previous["created_at"]) if previous is not None else now
            conn.execute(
                """INSERT INTO studycrew_runtime_counters(name, value)
                   VALUES ('live_recovery_revision', 0)
                   ON CONFLICT(name) DO NOTHING""",
            )
            if previous is None:
                conn.execute(
                    """UPDATE studycrew_runtime_counters SET value=value+1
                       WHERE name='live_recovery_revision'""",
                )
                recovery_revision = int(conn.execute(
                    """SELECT value FROM studycrew_runtime_counters
                       WHERE name='live_recovery_revision'""",
                ).fetchone()[0])
            else:
                recovery_revision = int(previous["recovery_revision"])
            conn.execute(
                """INSERT INTO studycrew_live_recovery(
                       group_id, affected_run_id, through_seq, through_version,
                       reason, attempts, recovery_token,
                       generation, failure_count, cause_digest, recovery_revision,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(group_id) DO UPDATE SET
                     affected_run_id=excluded.affected_run_id,
                     through_seq=excluded.through_seq,
                     through_version=excluded.through_version,
                     reason=excluded.reason,
                     attempts=MAX(studycrew_live_recovery.attempts, excluded.attempts),
                     generation=excluded.generation,
                     failure_count=excluded.failure_count,
                     cause_digest=excluded.cause_digest,
                     updated_at=excluded.updated_at""",
                (
                    group_id, run_id, through_seq, int(run_version or through_seq),
                    reason, attempts, token, generation,
                    failure_count, cause_digest, recovery_revision, created_at, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM studycrew_live_recovery WHERE group_id=?", (group_id,),
            ).fetchone()
            if previous is not None and row is not None:
                latest_dirty = conn.execute(
                    """SELECT * FROM studycrew_live_recovery_dirty
                       WHERE group_id=? ORDER BY queue_revision DESC LIMIT 1""",
                    (group_id,),
                ).fetchone()
                if latest_dirty is not None and int(latest_dirty["frozen"]) == 0:
                    conn.execute(
                        """UPDATE studycrew_live_recovery_dirty
                           SET recovery_token=?, generation=?, updated_at=?
                           WHERE queue_revision=? AND frozen=0""",
                        (
                            str(row["recovery_token"]), int(row["generation"]), now,
                            int(latest_dirty["queue_revision"]),
                        ),
                    )
                else:
                    global_count = int(conn.execute(
                        "SELECT COUNT(*) FROM studycrew_live_recovery_dirty",
                    ).fetchone()[0])
                    group_count = int(conn.execute(
                        """SELECT COUNT(*) FROM studycrew_live_recovery_dirty
                           WHERE group_id=?""",
                        (group_id,),
                    ).fetchone()[0])
                    if (
                        dirty_global_limit is not None
                        and global_count >= max(1, int(dirty_global_limit))
                    ) or (
                        dirty_group_limit is not None
                        and group_count >= max(1, int(dirty_group_limit))
                    ):
                        raise RecoveryDirtyBudgetError(
                            group_id, global_count=global_count,
                            group_count=group_count,
                        )
                    conn.execute(
                        """UPDATE studycrew_runtime_counters SET value=MAX(value, ?)
                           WHERE name='live_recovery_dirty_revision'""",
                        (max(0, int(dirty_revision_floor)),),
                    )
                    conn.execute(
                        """UPDATE studycrew_runtime_counters SET value=value+1
                           WHERE name='live_recovery_dirty_revision'""",
                    )
                    dirty_revision = int(conn.execute(
                        """SELECT value FROM studycrew_runtime_counters
                           WHERE name='live_recovery_dirty_revision'""",
                    ).fetchone()[0])
                    conn.execute(
                        """INSERT INTO studycrew_live_recovery_dirty(
                               queue_revision, group_id, recovery_token, generation,
                               frozen, created_at, updated_at
                           ) VALUES (?, ?, ?, ?, 0, ?, ?)""",
                        (
                            dirty_revision, group_id, str(row["recovery_token"]),
                            int(row["generation"]), now, now,
                        ),
                    )
        return {} if row is None else {key: row[key] for key in row.keys()}

    def live_recoveries(
        self, *, limit: int | None = None, after_group_id: str = "",
        after_revision: int | None = None,
    ) -> tuple[dict[str, Any], ...]:
        with self.db.transaction(immediate=False) as conn:
            if limit is None and after_revision is not None:
                rows = conn.execute(
                    """SELECT * FROM studycrew_live_recovery
                       WHERE recovery_revision > ?
                       ORDER BY recovery_revision, group_id""",
                    (max(0, int(after_revision)),),
                ).fetchall()
            elif limit is None:
                rows = conn.execute(
                    "SELECT * FROM studycrew_live_recovery ORDER BY recovery_revision, group_id",
                ).fetchall()
            elif after_revision is not None:
                rows = conn.execute(
                    """SELECT * FROM studycrew_live_recovery
                       WHERE recovery_revision > ?
                       ORDER BY recovery_revision, group_id LIMIT ?""",
                    (max(0, int(after_revision)), max(1, int(limit))),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM studycrew_live_recovery
                       WHERE group_id > ? ORDER BY group_id LIMIT ?""",
                    (after_group_id, max(1, int(limit))),
                ).fetchall()
        return tuple({key: row[key] for key in row.keys()} for row in rows)

    def live_recovery(self, group_id: str) -> dict[str, Any] | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT * FROM studycrew_live_recovery WHERE group_id=?", (group_id,),
            ).fetchone()
        return None if row is None else {key: row[key] for key in row.keys()}

    def clear_live_recovery(
        self, group_id: str, recovery_token: str, generation: int,
    ) -> bool:
        """Clear one exact, debt-free marker and its dirty suffix atomically.

        Disconnect debt/claims belong to an authenticated replacement and cannot
        be bypassed by an exact token. Intentional Group deletion uses the separate
        deletion-fenced ``delete_group``/``clear_group_live_recovery`` path.
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """DELETE FROM studycrew_live_recovery
                   WHERE group_id=? AND recovery_token=? AND generation=?
                     AND disconnect_retry=0 AND disconnect_claim_id=''""",
                (group_id, recovery_token, generation),
            )
            if cursor.rowcount == 1:
                conn.execute(
                    "DELETE FROM studycrew_live_recovery_dirty WHERE group_id=?",
                    (group_id,),
                )
        return cursor.rowcount == 1

    def live_recovery_dirty(
        self, *, limit: int | None = None, after_queue_revision: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        """Page immutable issued entries plus each Group's coalesced unissued tail."""
        sql = """SELECT r.*,
                        d.recovery_token AS dirty_recovery_token,
                        d.generation AS dirty_generation,
                        d.queue_revision AS dirty_queue_revision,
                        d.frozen AS dirty_frozen
                 FROM studycrew_live_recovery_dirty AS d
                 JOIN studycrew_live_recovery AS r ON r.group_id=d.group_id
                 WHERE d.queue_revision > ?
                 ORDER BY d.queue_revision, d.group_id"""
        params: tuple[Any, ...]
        if limit is None:
            params = (max(0, int(after_queue_revision)),)
        else:
            sql += " LIMIT ?"
            params = (
                max(0, int(after_queue_revision)), max(1, int(limit)),
            )
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(self._dirty_recovery_row(row) for row in rows)

    @staticmethod
    def _dirty_recovery_row(row: Any) -> dict[str, Any]:
        result = {key: row[key] for key in row.keys()}
        result["recovery_token"] = str(row["dirty_recovery_token"])
        result["generation"] = int(row["dirty_generation"])
        result["dirty_queue_revision"] = int(row["dirty_queue_revision"])
        result["dirty_frozen"] = int(row["dirty_frozen"])
        return result

    def live_recovery_dirty_marker(self, group_id: str) -> dict[str, Any] | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                """SELECT r.*,
                          d.recovery_token AS dirty_recovery_token,
                          d.generation AS dirty_generation,
                          d.queue_revision AS dirty_queue_revision,
                          d.frozen AS dirty_frozen
                   FROM studycrew_live_recovery_dirty AS d
                   JOIN studycrew_live_recovery AS r ON r.group_id=d.group_id
                   WHERE d.group_id=?
                   ORDER BY d.queue_revision DESC LIMIT 1""",
                (group_id,),
            ).fetchone()
        return None if row is None else self._dirty_recovery_row(row)

    def live_recovery_dirty_entry(self, queue_revision: int) -> dict[str, Any] | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                """SELECT r.*,
                          d.recovery_token AS dirty_recovery_token,
                          d.generation AS dirty_generation,
                          d.queue_revision AS dirty_queue_revision,
                          d.frozen AS dirty_frozen
                   FROM studycrew_live_recovery_dirty AS d
                   JOIN studycrew_live_recovery AS r ON r.group_id=d.group_id
                   WHERE d.queue_revision=?""",
                (max(0, int(queue_revision)),),
            ).fetchone()
        return None if row is None else self._dirty_recovery_row(row)

    def live_recovery_dirty_next(
        self, after_queue_revision: int,
    ) -> dict[str, Any] | None:
        rows = self.live_recovery_dirty(
            limit=1, after_queue_revision=after_queue_revision,
        )
        return None if not rows else rows[0]

    def freeze_live_recovery_dirty(
        self, queue_revision: int, *, group_id: str,
        recovery_token: str, generation: int,
    ) -> dict[str, Any] | None:
        """Freeze one exact update position before any client-visible reservation."""
        revision = max(0, int(queue_revision))
        with self.db.transaction() as conn:
            row = conn.execute(
                """SELECT recovery_token, generation FROM studycrew_live_recovery_dirty
                   WHERE queue_revision=? AND group_id=?""",
                (revision, group_id),
            ).fetchone()
            if row is None or (
                str(row["recovery_token"]), int(row["generation"])
            ) != (recovery_token, int(generation)):
                return None
            conn.execute(
                """UPDATE studycrew_live_recovery_dirty SET frozen=1, updated_at=?
                   WHERE queue_revision=? AND group_id=?""",
                (utc_now(), revision, group_id),
            )
            frozen = conn.execute(
                """SELECT r.*,
                          d.recovery_token AS dirty_recovery_token,
                          d.generation AS dirty_generation,
                          d.queue_revision AS dirty_queue_revision,
                          d.frozen AS dirty_frozen
                   FROM studycrew_live_recovery_dirty AS d
                   JOIN studycrew_live_recovery AS r ON r.group_id=d.group_id
                   WHERE d.queue_revision=? AND d.group_id=?""",
                (revision, group_id),
            ).fetchone()
        return None if frozen is None else self._dirty_recovery_row(frozen)

    def live_recovery_dirty_revision_high_water(self) -> int:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                """SELECT value FROM studycrew_runtime_counters
                   WHERE name='live_recovery_dirty_revision'""",
            ).fetchone()
        return 0 if row is None else max(0, int(row[0]))

    def live_recovery_dirty_count(self, group_id: str | None = None) -> int:
        """Return durable dirty-log occupancy without exposing recovery content."""
        with self.db.transaction(immediate=False) as conn:
            if group_id is None:
                row = conn.execute(
                    "SELECT COUNT(*) FROM studycrew_live_recovery_dirty",
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT COUNT(*) FROM studycrew_live_recovery_dirty
                       WHERE group_id=?""",
                    (group_id,),
                ).fetchone()
        return 0 if row is None else max(0, int(row[0]))

    def retire_live_recovery_dirty_through(
        self, queue_revision: int,
    ) -> tuple[dict[str, Any], ...]:
        """Retire covered dirty rows that have no durable disconnect debt."""
        frontier = max(0, int(queue_revision))
        if frontier <= 0:
            return ()
        with self.db.transaction() as conn:
            rows = conn.execute(
                """SELECT r.*, d.queue_revision AS dirty_queue_revision
                   FROM studycrew_live_recovery_dirty AS d
                   JOIN studycrew_live_recovery AS r ON r.group_id=d.group_id
                   WHERE d.queue_revision <= ? AND r.disconnect_retry=0
                   ORDER BY d.queue_revision, d.group_id""",
                (frontier,),
            ).fetchall()
            if rows:
                conn.execute(
                    """DELETE FROM studycrew_live_recovery_dirty
                       WHERE queue_revision <= ?
                         AND EXISTS (
                           SELECT 1 FROM studycrew_live_recovery AS recovery
                           WHERE recovery.group_id=studycrew_live_recovery_dirty.group_id
                             AND recovery.disconnect_retry=0
                         )""",
                    (frontier,),
                )
        return tuple({key: row[key] for key in row.keys()} for row in rows)

    def has_live_recovery_disconnect_debt(self) -> bool:
        """Whether any durable recovery row is reserved for a replacement session."""
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                """SELECT 1 FROM studycrew_live_recovery
                   WHERE disconnect_retry=1 LIMIT 1""",
            ).fetchone()
        return row is not None

    def retire_live_recoveries_through(
        self, recovery_revision: int,
    ) -> tuple[dict[str, Any], ...]:
        """Atomically retire rows covered by every live pager and no retry debt."""
        frontier = max(0, int(recovery_revision))
        if frontier <= 0:
            return ()
        with self.db.transaction() as conn:
            rows = conn.execute(
                """SELECT * FROM studycrew_live_recovery
                   WHERE recovery_revision <= ? AND disconnect_retry=0
                     AND NOT EXISTS (
                       SELECT 1 FROM studycrew_live_recovery_dirty AS dirty
                       WHERE dirty.group_id=studycrew_live_recovery.group_id
                     )
                   ORDER BY recovery_revision, group_id""",
                (frontier,),
            ).fetchall()
            if rows:
                conn.execute(
                    """DELETE FROM studycrew_live_recovery
                       WHERE recovery_revision <= ? AND disconnect_retry=0
                         AND NOT EXISTS (
                           SELECT 1 FROM studycrew_live_recovery_dirty AS dirty
                           WHERE dirty.group_id=studycrew_live_recovery.group_id
                         )""",
                    (frontier,),
                )
        return tuple({key: row[key] for key in row.keys()} for row in rows)

    def mark_live_recovery_disconnect(
        self, group_id: str, recovery_token: str, generation: int,
        *, disconnected_at_ns: int, fence_id: str = "legacy",
    ) -> bool:
        """Retain one explicitly fenced retry for a single recovery row."""
        with self.db.transaction() as conn:
            exists = conn.execute(
                """SELECT 1 FROM studycrew_live_recovery
                   WHERE group_id=? AND recovery_token=? AND generation=?""",
                (group_id, recovery_token, generation),
            ).fetchone()
            if exists is None:
                return False
            conn.execute(
                """INSERT INTO studycrew_runtime_counters(name, value)
                   VALUES ('live_recovery_disconnect_epoch', 0)
                   ON CONFLICT(name) DO NOTHING""",
            )
            conn.execute(
                """UPDATE studycrew_runtime_counters SET value=value+1
                   WHERE name='live_recovery_disconnect_epoch'""",
            )
            debt_generation = int(conn.execute(
                """SELECT value FROM studycrew_runtime_counters
                   WHERE name='live_recovery_disconnect_epoch'""",
            ).fetchone()[0])
            cursor = conn.execute(
                """UPDATE studycrew_live_recovery
                   SET disconnect_retry=1,
                       disconnected_at_ns=?,
                       disconnect_debt_generation=?,
                       disconnect_fence_id=?, disconnect_claim_id='',
                       updated_at=?
                   WHERE group_id=? AND recovery_token=? AND generation=?""",
                (
                    int(disconnected_at_ns), debt_generation, fence_id, utc_now(), group_id,
                    recovery_token, generation,
                ),
            )
        return cursor.rowcount == 1

    def mark_live_recoveries_disconnect_after(
        self, recovery_revision: int, *, update_revision: int = 0,
        fence_id: str, disconnected_at_ns: int,
    ) -> int:
        """Atomically transfer uncovered base or dirty obligations to debt."""
        if not fence_id:
            raise ValueError("fence_id is required")
        with self.db.transaction() as conn:
            exists = conn.execute(
                """SELECT 1 FROM studycrew_live_recovery
                   WHERE recovery_revision > ?
                      OR EXISTS (
                        SELECT 1 FROM studycrew_live_recovery_dirty AS dirty
                        WHERE dirty.group_id=studycrew_live_recovery.group_id
                          AND dirty.queue_revision > ?
                      )
                   LIMIT 1""",
                (
                    max(0, int(recovery_revision)),
                    max(0, int(update_revision)),
                ),
            ).fetchone()
            if exists is None:
                return 0
            conn.execute(
                """INSERT INTO studycrew_runtime_counters(name, value)
                   VALUES ('live_recovery_disconnect_epoch', 0)
                   ON CONFLICT(name) DO NOTHING""",
            )
            conn.execute(
                """UPDATE studycrew_runtime_counters SET value=value+1
                   WHERE name='live_recovery_disconnect_epoch'""",
            )
            debt_generation = int(conn.execute(
                """SELECT value FROM studycrew_runtime_counters
                   WHERE name='live_recovery_disconnect_epoch'""",
            ).fetchone()[0])
            cursor = conn.execute(
                """UPDATE studycrew_live_recovery
                    SET disconnect_retry=1,
                       disconnected_at_ns=?,
                       disconnect_debt_generation=?,
                       disconnect_fence_id=?, disconnect_claim_id='',
                       updated_at=?
                    WHERE recovery_revision > ?
                       OR EXISTS (
                         SELECT 1 FROM studycrew_live_recovery_dirty AS dirty
                         WHERE dirty.group_id=studycrew_live_recovery.group_id
                           AND dirty.queue_revision > ?
                       )""",
                (
                    int(disconnected_at_ns), debt_generation, fence_id, utc_now(),
                    max(0, int(recovery_revision)),
                    max(0, int(update_revision)),
                ),
            )
        return max(0, int(cursor.rowcount))

    def live_recovery_disconnect_epoch(self) -> int:
        """Return the durable debt epoch visible when a recovery session joins."""
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                """SELECT value FROM studycrew_runtime_counters
                   WHERE name='live_recovery_disconnect_epoch'""",
            ).fetchone()
        return 0 if row is None else max(0, int(row[0]))

    def consume_live_recovery_disconnect(
        self, group_id: str, recovery_token: str, generation: int,
        *, claim_id: str, claim_after_epoch: int,
    ) -> bool:
        """Atomically claim and consume the exact current disconnect debt."""
        if not claim_id:
            return False
        with self.db.transaction() as conn:
            claimed = conn.execute(
                """UPDATE studycrew_live_recovery
                   SET disconnect_claim_id=?, updated_at=?
                   WHERE group_id=? AND recovery_token=? AND generation=?
                     AND disconnect_retry=1
                     AND disconnect_debt_generation <= ?
                     AND (disconnect_claim_id='' OR disconnect_claim_id=?)""",
                (
                    claim_id, utc_now(), group_id, recovery_token, generation,
                    max(0, int(claim_after_epoch)), claim_id,
                ),
            )
            if claimed.rowcount != 1:
                return False
            consumed = conn.execute(
                """UPDATE studycrew_live_recovery
                   SET disconnect_retry=0, disconnect_claim_id='', updated_at=?
                   WHERE group_id=? AND recovery_token=? AND generation=?
                     AND disconnect_retry=1 AND disconnect_claim_id=?""",
                (utc_now(), group_id, recovery_token, generation, claim_id),
            )
        return consumed.rowcount == 1

    def clear_group_live_recovery(self, group_id: str) -> int:
        """Force-clear recovery only behind a durable Group deletion fence."""
        with self.db.transaction() as conn:
            deletion = conn.execute(
                """SELECT 1 FROM studycrew_group_deletions
                   WHERE group_id=? AND group_proof_digest!=''
                     AND stage IN ('prepared', 'runtime_cleaned', 'complete')
                   LIMIT 1""",
                (group_id,),
            ).fetchone()
            if deletion is None:
                raise RuntimeStoreError(
                    "Group deletion intent is required to force-clear recovery",
                )
            conn.execute(
                "DELETE FROM studycrew_live_recovery_dirty WHERE group_id=?", (group_id,),
            )
            cursor = conn.execute(
                "DELETE FROM studycrew_live_recovery WHERE group_id=?", (group_id,),
            )
        return max(0, int(cursor.rowcount))

    def record_live_publication(self, *, run_id: str, group_id: str, last_seq: int) -> None:
        """Durable per-run handoff cursor; memory LRUs are only an acceleration layer."""
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO studycrew_live_publication(run_id, group_id, last_seq, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                     group_id=excluded.group_id,
                     last_seq=MAX(studycrew_live_publication.last_seq, excluded.last_seq),
                     updated_at=excluded.updated_at""",
                (run_id, group_id, max(0, int(last_seq)), utc_now()),
            )

    def record_group_live_publication(self, group_id: str, cursors: Mapping[str, int]) -> None:
        if not cursors:
            return
        now = utc_now()
        with self.db.transaction() as conn:
            for run_id, last_seq in cursors.items():
                conn.execute(
                    """INSERT INTO studycrew_live_publication(run_id, group_id, last_seq, updated_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(run_id) DO UPDATE SET
                         group_id=excluded.group_id,
                         last_seq=MAX(studycrew_live_publication.last_seq, excluded.last_seq),
                         updated_at=excluded.updated_at""",
                    (run_id, group_id, max(0, int(last_seq)), now),
                )

    def live_publication_cursor(self, run_id: str) -> int | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute(
                "SELECT last_seq FROM studycrew_live_publication WHERE run_id=?", (run_id,),
            ).fetchone()
        return None if row is None else int(row["last_seq"])

    def clear_group_live_publication(self, group_id: str) -> int:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM studycrew_live_publication WHERE group_id=?", (group_id,),
            )
        return max(0, int(cursor.rowcount))

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
            deletion = conn.execute(
                """SELECT 1 FROM studycrew_group_deletions
                   WHERE group_id=? AND group_proof_digest!=''
                     AND stage IN ('prepared', 'runtime_cleaned', 'complete')
                   LIMIT 1""",
                (group_id,),
            ).fetchone()
            if deletion is None:
                raise RuntimeStoreError(
                    "Group deletion intent is required to cascade runtime content",
                )
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
                "live_recovery": int(conn.execute(
                    "SELECT COUNT(*) FROM studycrew_live_recovery WHERE group_id=?", (group_id,),
                ).fetchone()[0]),
                "live_publication": int(conn.execute(
                    "SELECT COUNT(*) FROM studycrew_live_publication WHERE group_id=?", (group_id,),
                ).fetchone()[0]),
            }
            for table in (
                "studycrew_checkpoints", "studycrew_tool_invocations",
                "studycrew_permission_grants", "studycrew_approvals",
                "studycrew_verifier_decisions", "studycrew_executor_continuations",
                "studycrew_live_recovery_dirty", "studycrew_live_recovery",
                "studycrew_live_publication",
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
            "live_recovery": "studycrew_live_recovery",
            "live_publication": "studycrew_live_publication",
        }
        with self.db.transaction(immediate=False) as conn:
            return {
                name: int(conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE group_id=?", (group_id,),
                ).fetchone()[0])
                for name, table in tables.items()
            }


__all__ = ["RecoveryDirtyBudgetError", "RuntimeStoreError", "TaskRuntimeStore"]
