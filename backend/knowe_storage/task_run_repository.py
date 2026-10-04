from __future__ import annotations

from pathlib import Path

from knowe_provenance import assert_provenance_matches
from backend.runtime import TaskRun, utc_now
from backend.protected_storage import StoragePayloads, protected_id, storage_boundary

from ._sqlite import (
    PROVENANCE_COLUMN_DEFS,
    SQLiteDatabase,
    compress_json_dumps,
    payload_sha256,
    json_dumps,
    json_loads,
    provenance_sql_values,
)


class OptimisticLockError(RuntimeError):
    pass


class TaskRunAlreadyExists(RuntimeError):
    pass


class SQLiteTaskRunRepository(StoragePayloads):
    """Persists authoritative TaskRun snapshots with compare-and-swap versions."""

    def __init__(self, path: str | Path | SQLiteDatabase, *, protected=None) -> None:
        if protected is not None:
            protected.identity
        self.db = path if isinstance(path, SQLiteDatabase) else SQLiteDatabase(path)
        self.protection = protected.for_store(self.db.path) if protected is not None else None
        self.db.initialize(
            """
            CREATE TABLE IF NOT EXISTS task_runs (
                task_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                version INTEGER NOT NULL,
                state TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_task_runs_state ON task_runs(state, updated_at);
            """
        )
        self.db.ensure_columns("task_runs", PROVENANCE_COLUMN_DEFS)
        self.db.ensure_columns("task_runs", {"payload_sha256": "TEXT"})  # [v1.0.31 R1] 压缩指纹
        self.db.register_schema("runtime.task_run")

    @storage_boundary
    def create(self, run: TaskRun) -> TaskRun:
        payload = (self._write_payload("task_runs", run.to_dict()) if self.protection is not None or protected_id(run.envelope.task_id)
                   else compress_json_dumps(run.to_dict()))  # [v1.0.31 R1] 压缩存储
        payload_digest = (self.protection.digest("task-run", run.to_dict()) if protected_id(run.envelope.task_id) and self.protection is not None
                          else payload_sha256(run.to_dict()))
        provenance = self._provenance(run.envelope.task_id, run.provenance)
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM task_runs WHERE task_id=?",
                (run.envelope.task_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["run_id"]) == run.run_id:
                    stored = TaskRun.from_dict(self._read_payload("task_runs", existing))
                    try:
                        assert_provenance_matches(stored.provenance, run.provenance, context="TaskRun create")
                    except ValueError as exc:
                        if self.protection is not None:
                            raise TaskRunAlreadyExists("protected TaskRun provenance mismatch") from None
                        raise TaskRunAlreadyExists(str(exc)) from exc
                    return stored
                raise TaskRunAlreadyExists(f"task already has a different run: {run.envelope.task_id}")
            conn.execute(
                """
                INSERT INTO task_runs(
                    task_id, run_id, version, state, payload_json, created_at, updated_at,
                    provenance_json, build_id, git_commit, runtime_schema_version,
                    harness_schema_version, prompt_bundle_version, migration_epoch,
                    payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.envelope.task_id,
                    run.run_id,
                    run.version,
                    run.state.value,
                    payload,
                    run.started_at,
                    run.updated_at,
                    *provenance,
                    payload_digest,
                ),
            )
        return run

    @storage_boundary
    def save(self, run: TaskRun, *, expected_version: int) -> TaskRun:
        if expected_version < 0:
            raise ValueError("expected_version must be non-negative")
        if run.version <= expected_version:
            raise ValueError("saved TaskRun version must advance beyond expected_version")
        payload = (self._write_payload("task_runs", run.to_dict()) if self.protection is not None or protected_id(run.envelope.task_id)
                   else compress_json_dumps(run.to_dict()))  # [v1.0.31 R1] 压缩存储
        payload_digest = (self.protection.digest("task-run", run.to_dict()) if protected_id(run.envelope.task_id) and self.protection is not None
                          else payload_sha256(run.to_dict()))
        provenance = self._provenance(run.envelope.task_id, run.provenance)
        with self.db.transaction() as conn:
            current = conn.execute(
                "SELECT * FROM task_runs WHERE task_id=? AND run_id=?",
                (run.envelope.task_id, run.run_id),
            ).fetchone()
            if current is not None:
                stored = TaskRun.from_dict(self._read_payload("task_runs", current))
                try:
                    assert_provenance_matches(stored.provenance, run.provenance, context="TaskRun save")
                except ValueError as exc:
                    if self.protection is not None:
                        raise OptimisticLockError("protected TaskRun provenance mismatch") from None
                    raise OptimisticLockError(str(exc)) from exc
            cursor = conn.execute(
                """
                UPDATE task_runs
                  SET version=?, state=?, payload_json=?, updated_at=?,
                      provenance_json=?, build_id=?, git_commit=?, runtime_schema_version=?,
                      harness_schema_version=?, prompt_bundle_version=?, migration_epoch=?,
                      payload_sha256=?
                WHERE task_id=? AND run_id=? AND version=?
                """,
                (
                    run.version,
                    run.state.value,
                    payload,
                    run.updated_at or utc_now(),
                    *provenance,
                    payload_digest,
                    run.envelope.task_id,
                    run.run_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                actual = conn.execute(
                    "SELECT version, run_id FROM task_runs WHERE task_id=?",
                    (run.envelope.task_id,),
                ).fetchone()
                actual_version = None if actual is None else int(actual["version"])
                raise OptimisticLockError(
                    f"task {run.envelope.task_id} expected version {expected_version}, actual {actual_version}"
                )
        return run

    @storage_boundary
    def get(self, task_id: str) -> TaskRun | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute("SELECT * FROM task_runs WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        return TaskRun.from_dict(self._read_payload("task_runs", row))

    @storage_boundary
    def get_by_run_id(self, run_id: str) -> TaskRun | None:
        with self.db.transaction(immediate=False) as conn:
            row = conn.execute("SELECT * FROM task_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        return TaskRun.from_dict(self._read_payload("task_runs", row))

    @storage_boundary
    def list_by_state(self, state: str, *, limit: int = 100) -> tuple[TaskRun, ...]:
        with self.db.transaction(immediate=False) as conn:
            rows = conn.execute(
                "SELECT * FROM task_runs WHERE state=? ORDER BY updated_at DESC LIMIT ?",
                (str(state), max(1, min(int(limit), 10_000))),
            ).fetchall()
        return tuple(TaskRun.from_dict(self._read_payload("task_runs", row)) for row in rows)


TaskRunRepository = SQLiteTaskRunRepository

__all__ = [
    "OptimisticLockError",
    "SQLiteTaskRunRepository",
    "TaskRunAlreadyExists",
    "TaskRunRepository",
]
