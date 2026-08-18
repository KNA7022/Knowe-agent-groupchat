from __future__ import annotations

"""Versioned public contracts for the StudyCrew orchestration control plane.

These contracts intentionally do not replace ``backend.runtime.TaskRun``.  That
older type is the worker loop's execution record; this module is the durable,
group-scoped control plane consumed by the desktop UI.
"""

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


TASK_RUNTIME_SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def opaque_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {item.name: _jsonable(getattr(value, item.name)) for item in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


_PRIVATE_PUBLIC_KEYS = {
    "chain_of_thought", "reasoning_content", "raw_reasoning", "system_prompt",
    "api_key", "secret", "credential", "private_payload", "sensitive_payload",
}


def assert_public_payload(value: Mapping[str, Any]) -> None:
    """Reject model internals and credentials at the public event boundary."""

    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if str(key).casefold() in _PRIVATE_PUBLIC_KEYS:
                    raise ValueError(f"private field is forbidden in public payload: {key}")
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)

    walk(value)


class TaskStatus(str, Enum):
    QUEUED = "queued"
    ROUTING = "routing"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {self.SUCCEEDED, self.PARTIAL, self.FAILED, self.CANCELLED}


class AgentPhase(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    RETRIEVING = "retrieving"
    EXECUTING = "executing"
    WAITING = "waiting"
    VERIFYING = "verifying"
    RESPONDING = "responding"
    FAILED = "failed"


class RunKind(str, Enum):
    PRIMARY = "primary"
    SIDE = "side"


class QueueClass(str, Enum):
    FOREGROUND = "foreground"
    INDEX = "index"


class RouteLabel(str, Enum):
    SUPPLEMENT_CURRENT = "supplement_current"
    SIDE_ANSWER = "side_answer"
    QUEUE_NEW = "queue_new"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FailureType(str, Enum):
    PARAMETER_ERROR = "parameter_error"
    TRANSIENT_PROVIDER_FAILURE = "transient_provider_failure"
    PERMISSION_DENIED = "permission_denied"
    BUDGET_EXHAUSTED = "budget_exhausted"
    VERIFICATION_FAILED = "verification_failed"
    IRREVERSIBLE_SIDE_EFFECT = "irreversible_side_effect"

    @property
    def reversible(self) -> bool:
        return self in {self.PARAMETER_ERROR, self.TRANSIENT_PROVIDER_FAILURE}


@dataclass(frozen=True)
class BudgetLimits:
    wall_clock_seconds: int = 900
    model_turns: int = 20
    tool_calls: int = 30
    tokens: int = 100_000
    cost_usd: float = 5.0

    def __post_init__(self) -> None:
        for name in ("wall_clock_seconds", "model_turns", "tool_calls", "tokens"):
            if int(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.cost_usd < 0:
            raise ValueError("cost_usd must be non-negative")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "BudgetLimits":
        value = dict(raw or {})
        return cls(
            wall_clock_seconds=int(value.get("wall_clock_seconds", 900)),
            model_turns=int(value.get("model_turns", 20)),
            tool_calls=int(value.get("tool_calls", 30)),
            tokens=int(value.get("tokens", 100_000)),
            cost_usd=float(value.get("cost_usd", 5.0)),
        )


@dataclass(frozen=True)
class BudgetUsage:
    wall_clock_seconds: float = 0.0
    model_turns: int = 0
    tool_calls: int = 0
    tokens: int = 0
    cost_usd: float = 0.0

    def __post_init__(self) -> None:
        if any(float(getattr(self, name)) < 0 for name in dataclasses.asdict(self)):
            raise ValueError("budget usage must be non-negative")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "BudgetUsage":
        value = dict(raw or {})
        return cls(
            wall_clock_seconds=float(value.get("wall_clock_seconds") or 0),
            model_turns=int(value.get("model_turns") or 0),
            tool_calls=int(value.get("tool_calls") or 0),
            tokens=int(value.get("tokens") or 0),
            cost_usd=float(value.get("cost_usd") or 0),
        )

    def exceeded(self, limits: BudgetLimits) -> str | None:
        checks = (
            ("wall_clock_seconds", self.wall_clock_seconds, limits.wall_clock_seconds),
            ("model_turns", self.model_turns, limits.model_turns),
            ("tool_calls", self.tool_calls, limits.tool_calls),
            ("tokens", self.tokens, limits.tokens),
            ("cost_usd", self.cost_usd, limits.cost_usd),
        )
        return next((name for name, used, maximum in checks if used > maximum), None)


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    source_id: str
    version_id: str
    locator: str
    excerpt_ref: str = ""
    schema_version: int = TASK_RUNTIME_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EvidenceRef":
        return cls(**{name: raw.get(name, field.default) for name, field in cls.__dataclass_fields__.items()})


@dataclass(frozen=True)
class AgentRun:
    agent_run_id: str
    run_id: str
    agent_id: str
    role: str
    phase: AgentPhase = AgentPhase.IDLE
    operation_summary: str = ""
    blocking_reason: str = ""
    version: int = 1
    updated_at: str = field(default_factory=utc_now)
    schema_version: int = TASK_RUNTIME_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AgentRun":
        return cls(
            agent_run_id=str(raw["agent_run_id"]), run_id=str(raw["run_id"]),
            agent_id=str(raw["agent_id"]), role=str(raw["role"]),
            phase=AgentPhase(str(raw.get("phase") or AgentPhase.IDLE.value)),
            operation_summary=str(raw.get("operation_summary") or ""),
            blocking_reason=str(raw.get("blocking_reason") or ""),
            version=int(raw.get("version") or 1), updated_at=str(raw.get("updated_at") or utc_now()),
            schema_version=int(raw.get("schema_version") or TASK_RUNTIME_SCHEMA_VERSION),
        )


@dataclass(frozen=True)
class RunEvent:
    event_id: str
    run_id: str
    group_id: str
    seq: int
    version: int
    event_type: str
    public_payload: dict[str, Any]
    sensitivity: str = "public"
    created_at: str = field(default_factory=utc_now)
    schema_version: int = TASK_RUNTIME_SCHEMA_VERSION
    sensitive_payload: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.seq < 1 or self.version < 1:
            raise ValueError("event seq and version must be positive")
        assert_public_payload(self.public_payload)

    def to_public_dict(self) -> dict[str, Any]:
        value = _jsonable(self)
        value.pop("sensitive_payload", None)
        return value

    def to_storage_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    run_id: str
    group_id: str
    kind: str
    summary: str
    risk: RiskLevel
    status: str = "pending"
    created_at: str = field(default_factory=utc_now)
    resolved_at: str | None = None
    schema_version: int = TASK_RUNTIME_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True)
class PermissionGrant:
    grant_id: str
    approval_id: str
    run_id: str
    group_id: str
    task_id: str
    capabilities: tuple[str, ...]
    expires_at: str | None = None
    created_at: str = field(default_factory=utc_now)
    schema_version: int = TASK_RUNTIME_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True)
class ToolInvocation:
    invocation_id: str
    run_id: str
    group_id: str
    tool_name: str
    idempotency_key: str
    risk: RiskLevel
    status: str = "prepared"
    replay_allowed: bool = True
    public_summary: str = ""
    created_at: str = field(default_factory=utc_now)
    completed_at: str | None = None
    schema_version: int = TASK_RUNTIME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.risk is RiskLevel.HIGH and self.replay_allowed:
            raise ValueError("high-risk invocations cannot be replayable")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    run_id: str
    group_id: str
    seq: int
    version: int
    label: str
    resumable: bool = True
    created_at: str = field(default_factory=utc_now)
    schema_version: int = TASK_RUNTIME_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass
class TaskRun:
    run_id: str
    task_id: str
    group_id: str
    kind: RunKind
    title: str
    status: TaskStatus = TaskStatus.QUEUED
    queue_class: QueueClass = QueueClass.FOREGROUND
    route_label: RouteLabel = RouteLabel.QUEUE_NEW
    read_only: bool = False
    risk: RiskLevel = RiskLevel.LOW
    parent_run_id: str | None = None
    version: int = 1
    last_seq: int = 0
    budget_limits: BudgetLimits = field(default_factory=BudgetLimits)
    budget_usage: BudgetUsage = field(default_factory=BudgetUsage)
    agent_runs: tuple[AgentRun, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()
    failure_type: FailureType | None = None
    public_summary: str = ""
    blocking_reason: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    schema_version: int = TASK_RUNTIME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.run_id or not self.task_id or not self.group_id or not self.title.strip():
            raise ValueError("run_id, task_id, group_id and title are required")
        if self.kind is RunKind.SIDE and (not self.read_only or self.risk is not RiskLevel.LOW):
            raise ValueError("side runs must be low-risk and read-only")
        if self.queue_class is QueueClass.INDEX and self.kind is not RunKind.SIDE:
            raise ValueError("index jobs use isolated side-run capacity")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)

    def to_public_dict(self) -> dict[str, Any]:
        return self.to_dict()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "TaskRun":
        return cls(
            run_id=str(raw["run_id"]), task_id=str(raw["task_id"]), group_id=str(raw["group_id"]),
            kind=RunKind(str(raw["kind"])), title=str(raw["title"]),
            status=TaskStatus(str(raw.get("status") or TaskStatus.QUEUED.value)),
            queue_class=QueueClass(str(raw.get("queue_class") or QueueClass.FOREGROUND.value)),
            route_label=RouteLabel(str(raw.get("route_label") or RouteLabel.QUEUE_NEW.value)),
            read_only=bool(raw.get("read_only")), risk=RiskLevel(str(raw.get("risk") or RiskLevel.LOW.value)),
            parent_run_id=str(raw["parent_run_id"]) if raw.get("parent_run_id") else None,
            version=int(raw.get("version") or 1), last_seq=int(raw.get("last_seq") or 0),
            budget_limits=BudgetLimits.from_dict(raw.get("budget_limits") if isinstance(raw.get("budget_limits"), Mapping) else None),
            budget_usage=BudgetUsage.from_dict(raw.get("budget_usage") if isinstance(raw.get("budget_usage"), Mapping) else None),
            agent_runs=tuple(AgentRun.from_dict(item) for item in raw.get("agent_runs") or ()),
            evidence=tuple(EvidenceRef.from_dict(item) for item in raw.get("evidence") or ()),
            failure_type=FailureType(str(raw["failure_type"])) if raw.get("failure_type") else None,
            public_summary=str(raw.get("public_summary") or ""),
            blocking_reason=str(raw.get("blocking_reason") or ""),
            created_at=str(raw.get("created_at") or utc_now()), updated_at=str(raw.get("updated_at") or utc_now()),
            started_at=str(raw["started_at"]) if raw.get("started_at") else None,
            finished_at=str(raw["finished_at"]) if raw.get("finished_at") else None,
            schema_version=int(raw.get("schema_version") or TASK_RUNTIME_SCHEMA_VERSION),
        )


__all__ = [
    "TASK_RUNTIME_SCHEMA_VERSION", "AgentPhase", "AgentRun", "ApprovalRequest",
    "BudgetLimits", "BudgetUsage", "Checkpoint", "EvidenceRef", "FailureType",
    "PermissionGrant", "QueueClass", "RiskLevel", "RouteLabel", "RunEvent",
    "RunKind", "TaskRun", "TaskStatus", "ToolInvocation", "assert_public_payload",
    "opaque_id", "utc_now",
]
