from __future__ import annotations

"""Immutable local storage for the shared ``TaskEnvelope`` model."""

import json
import os
from pathlib import Path
from typing import Any, Mapping

from backend.runtime import TaskEnvelope
from backend.protected_storage import ProtectedStorageError, protected_id, storage_boundary, storage_write


class TaskEnvelopeStore:
    def __init__(self, internal_workspace: str | Path, *, protected=None) -> None:
        if protected is not None:
            protected.identity
        root = Path(internal_workspace).expanduser().resolve()
        self.root = root / "runtime" / "task-envelopes"
        if protected is not None and protected.read_only:
            if not self.root.is_dir():
                raise ProtectedStorageError("existing envelope store unavailable")
        else:
            self.root.mkdir(parents=True, exist_ok=True)
        self.protection = protected.for_store(self.root) if protected is not None else None

    def envelope_path(self, task_id: str, attempt_id: str) -> Path:
        path = self.root / str(task_id) / f"{attempt_id}.json"
        if path.resolve().parent.parent != self.root or path.parent.is_symlink() or path.is_symlink():
            raise ProtectedStorageError("invalid TaskEnvelope path")
        return path

    @staticmethod
    def _create_json_once(path: Path, value: Mapping[str, Any]) -> bool:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            return False
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        return True

    @storage_boundary
    @storage_write
    def commit(self, envelope: TaskEnvelope) -> tuple[TaskEnvelope, str]:
        if self.protection is not None:
            self.protection.validate_envelope(envelope.to_dict())
        path = self.envelope_path(envelope.task_id, envelope.attempt_id)
        ref = path.relative_to(self.root.parent.parent).as_posix()
        if path.exists() and protected_id(envelope.task_id):
            existing = self._read(path, envelope.task_id, envelope.attempt_id)
            if self.protection.digest("envelope-equality", existing.to_dict()) != self.protection.digest("envelope-equality", envelope.to_dict()):
                raise ProtectedStorageError("immutable protected TaskEnvelope collision")
            return existing, ref
        payload = envelope.to_dict()
        if protected_id(envelope.task_id):
            if self.protection is None:
                raise ProtectedStorageError("protected TaskEnvelope capability required")
            self.protection.validate_envelope(payload)
            payload = {"sealed": self.protection.seal("task_envelopes", envelope.attempt_id, payload)}
        if self._create_json_once(path, payload):
            return envelope, ref
        try:
            existing = self._read(path, envelope.task_id, envelope.attempt_id)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"stored TaskEnvelope is unreadable for {envelope.task_id}/{envelope.attempt_id}"
            ) from exc
        if existing.digest == envelope.digest:
            return existing, ref
        raise RuntimeError(
            f"immutable TaskEnvelope collision for {envelope.task_id}/{envelope.attempt_id}"
        )

    def _read(self, path, task_id, attempt_id):
        if self.protection is not None and task_id != self.protection.access.task_id:
            raise ProtectedStorageError("protected TaskEnvelope selection mismatch")
        if protected_id(task_id) and path.stat().st_size > 3 * 1024 * 1024:
            raise ProtectedStorageError("TaskEnvelope storage limit exceeded")
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError):
            if protected_id(task_id):
                raise ProtectedStorageError("unreadable protected TaskEnvelope") from None
            raise
        if protected_id(task_id) or isinstance(value, dict) and "sealed" in value:
            if self.protection is None or not protected_id(task_id):
                raise ProtectedStorageError("protected TaskEnvelope capability required")
            if not isinstance(value, dict) or set(value) != {"sealed"}:
                raise ProtectedStorageError("invalid protected TaskEnvelope format")
            value = self.protection.open("task_envelopes", attempt_id, value.get("sealed"))
            binding, _ = self.protection._binding()
            if task_id != binding["task_id"] or attempt_id != binding["attempt_id"]:
                raise ProtectedStorageError("protected TaskEnvelope reference mismatch")
        result = TaskEnvelope.from_dict(value)
        if (result.task_id, result.attempt_id) != (task_id, attempt_id):
            raise ProtectedStorageError("stored TaskEnvelope identity mismatch")
        return result

    def purge_protected(self):
        if self.protection is None:
            raise ProtectedStorageError("protected purge capability required")
        self.protection.access.require_write()
        access = self.protection.access
        # Resolve the trusted identity before revocation; on retry the private
        # task directory is fixed by the retained opaque capability identity.
        access.registry.revoke(access.group_id, access.task_id)
        directory = self.root / access.task_id
        if directory.resolve().parent != self.root or directory.is_symlink():
            raise ProtectedStorageError("invalid protected purge path")
        if directory.exists():
            for path in directory.iterdir():
                if not path.is_file() or path.is_symlink() or path.suffix != ".json":
                    raise ProtectedStorageError("unexpected protected envelope file")
                path.unlink()
            directory.rmdir()
        return {"task_id": access.task_id, "state": "purged"}

    @storage_boundary
    def get(self, task_id_or_ref: str, attempt_id: str = "") -> TaskEnvelope | None:
        raw = str(task_id_or_ref or "").strip().replace("\\", "/")
        if not raw:
            return None
        if ".." in Path(raw).parts or Path(raw).is_absolute():
            raise ProtectedStorageError("invalid TaskEnvelope reference")
        if raw.startswith("runtime/task-envelopes/") and raw.endswith(".json"):
            relative = raw.removeprefix("runtime/task-envelopes/")
            if self.protection is not None and Path(relative).parent.name != self.protection.access.task_id:
                raise ProtectedStorageError("protected TaskEnvelope selection mismatch")
            path = self.root / relative
        elif attempt_id:
            if self.protection is not None and raw != self.protection.access.task_id:
                raise ProtectedStorageError("protected TaskEnvelope selection mismatch")
            path = self.envelope_path(raw, attempt_id)
        else:
            if self.protection is not None and raw != self.protection.access.task_id:
                raise ProtectedStorageError("protected TaskEnvelope selection mismatch")
            task_dir = self.root / raw
            candidates = sorted(task_dir.glob("*.json"))
            path = candidates[-1] if candidates else Path("")
        if path.is_file() and (path.resolve().parent.parent != self.root or path.parent.is_symlink() or path.is_symlink()):
            raise ProtectedStorageError("invalid TaskEnvelope reference")
        if not path or not path.is_file():
            return None
        try:
            return self._read(path, path.parent.name, path.stem)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None


__all__ = ["TaskEnvelopeStore"]
