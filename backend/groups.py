"""Versioned StudyCrew Group domain and durable repository.

The chat runtime still stores historical conversations under ``project_id``.  This
module is the compatibility seam: new code deals exclusively in opaque ``group_id``
values while ``legacy_project_id`` is retained as an adapter key until the old event
store has been retired.

The repository deliberately stores metadata separately from chat/source content.
Deletion receipts can therefore be kept for audit/idempotency without retaining a
group name, source path, prompt, or other user content.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


GROUP_SCHEMA_VERSION = 1
GROUP_ID_PATTERN = re.compile(r"^grp_[0-9a-f]{32}$")
AGENT_ID_PATTERN = re.compile(r"^gag_[0-9a-f]{32}$")
COURSE_BINDING_ID_PATTERN = re.compile(r"^gcb_[0-9a-f]{32}$")
SOURCE_ID_PATTERN = re.compile(r"^src_[0-9a-f]{32}$")
SOURCE_LINK_ID_PATTERN = re.compile(r"^gsl_[0-9a-f]{32}$")

MANDATORY_ROLES = frozenset({"coordinator", "verifier"})
OPTIONAL_BUILTIN_ROLES = frozenset({"knowledge", "planning", "coach"})
EXPERT_ROLES = frozenset({"expert", "legacy_expert"})
ALL_AGENT_ROLES = MANDATORY_ROLES | OPTIONAL_BUILTIN_ROLES | EXPERT_ROLES
EXPERT_FIELDS = frozenset({
    "name", "specialty", "responsibilities", "allowed_tools", "model_override", "budget",
})
FORBIDDEN_EXPERT_FIELDS = frozenset({
    "system_prompt", "policy", "platform_policy", "developer_prompt", "permissions",
    "unrestricted_tools", "tool_policy",
})
DEFAULT_EXPERT_TOOLS = frozenset({"read", "search", "calculate", "summarize"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def opaque_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class GroupDomainError(ValueError):
    """A caller supplied invalid Group-domain state or violated an invariant."""


class GroupNotFoundError(GroupDomainError):
    pass


class StaleDeletePreviewError(GroupDomainError):
    pass


class GroupTemplate(str, Enum):
    COURSE_STUDY = "course_study"
    EXAM_SPRINT = "exam_sprint"
    GENERAL_ASSISTANT = "general_assistant"
    CUSTOM = "custom"


class GroupLifecycle(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class ExpertTemplateConfig:
    """Constrained expert configuration; platform policy is never caller-writable."""

    name: str
    specialty: str
    responsibilities: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    model_override: str | None = None
    budget: int | None = None

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        platform_tools: Iterable[str] = DEFAULT_EXPERT_TOOLS,
    ) -> "ExpertTemplateConfig":
        unknown = set(payload) - EXPERT_FIELDS
        forbidden = set(payload) & FORBIDDEN_EXPERT_FIELDS
        if forbidden:
            raise GroupDomainError(
                "expert configuration cannot override platform policy: "
                + ", ".join(sorted(forbidden))
            )
        if unknown:
            raise GroupDomainError("unknown expert fields: " + ", ".join(sorted(unknown)))
        name = _bounded_text(payload.get("name"), "expert name", 80)
        specialty = _bounded_text(payload.get("specialty"), "expert specialty", 200)
        responsibilities = _bounded_text_list(
            payload.get("responsibilities", ()), "responsibilities", max_items=12, max_length=240,
        )
        requested_tools = _bounded_text_list(
            payload.get("allowed_tools", ()), "allowed_tools", max_items=32, max_length=80,
        )
        allowed = frozenset(str(item) for item in platform_tools)
        if not set(requested_tools).issubset(allowed):
            denied = sorted(set(requested_tools) - allowed)
            raise GroupDomainError("expert requested tools outside platform allowlist: " + ", ".join(denied))
        model = payload.get("model_override")
        if model is not None:
            model = _bounded_text(model, "model_override", 160)
        budget = payload.get("budget")
        if budget is not None and (not isinstance(budget, int) or isinstance(budget, bool) or budget < 0):
            raise GroupDomainError("expert budget must be a non-negative integer")
        return cls(name, specialty, responsibilities, requested_tools, model, budget)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "specialty": self.specialty,
            "responsibilities": list(self.responsibilities),
            "allowed_tools": list(self.allowed_tools),
            "model_override": self.model_override,
            "budget": self.budget,
            "platform_policy_locked": True,
        }


@dataclass
class GroupAgent:
    agent_id: str
    group_id: str
    role: str
    display_name: str
    template_key: str | None = None
    expert: ExpertTemplateConfig | None = None
    model_override: str | None = None
    tool_overrides: tuple[str, ...] = ()
    budget: int | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not AGENT_ID_PATTERN.fullmatch(self.agent_id):
            raise GroupDomainError("agent_id must be an opaque gag_<uuid> identifier")
        if not GROUP_ID_PATTERN.fullmatch(self.group_id):
            raise GroupDomainError("agent group_id must be an opaque Group identifier")
        if self.role not in ALL_AGENT_ROLES:
            raise GroupDomainError(f"unsupported group agent role: {self.role}")
        if self.role in EXPERT_ROLES and self.expert is None:
            raise GroupDomainError(f"{self.role} requires constrained expert configuration")
        if self.role not in EXPERT_ROLES and self.expert is not None:
            raise GroupDomainError("only expert roles may carry expert configuration")
        if self.role in EXPERT_ROLES and self.expert is not None:
            effective = tuple(dict.fromkeys(self.expert.allowed_tools))
            supplied = tuple(dict.fromkeys(self.tool_overrides))
            if supplied != effective:
                raise GroupDomainError(
                    "expert tool_overrides must exactly match constrained allowed_tools"
                )
            self.tool_overrides = effective

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["tool_overrides"] = list(self.tool_overrides)
        row["expert"] = self.expert.to_dict() if self.expert else None
        return row

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GroupAgent":
        expert_raw = raw.get("expert")
        expert = None
        if isinstance(expert_raw, Mapping):
            expert = ExpertTemplateConfig.from_payload({
                key: value for key, value in expert_raw.items()
                if key in EXPERT_FIELDS
            })
        return cls(
            agent_id=str(raw["agent_id"]), group_id=str(raw["group_id"]),
            role=str(raw["role"]), display_name=str(raw.get("display_name") or raw["role"]),
            template_key=_optional_text(raw.get("template_key")), expert=expert,
            model_override=_optional_text(raw.get("model_override")),
            tool_overrides=tuple(str(x) for x in raw.get("tool_overrides") or ()),
            budget=_optional_int(raw.get("budget")),
            created_at=str(raw.get("created_at") or utc_now()),
            updated_at=str(raw.get("updated_at") or raw.get("created_at") or utc_now()),
        )


@dataclass
class GroupCourseBinding:
    binding_id: str
    group_id: str
    course_id: str
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not COURSE_BINDING_ID_PATTERN.fullmatch(self.binding_id):
            raise GroupDomainError("binding_id must be an opaque gcb_<uuid> identifier")
        if not GROUP_ID_PATTERN.fullmatch(self.group_id):
            raise GroupDomainError("course binding group_id is invalid")


@dataclass
class GroupSourceLink:
    link_id: str
    group_id: str
    source_id: str
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not SOURCE_LINK_ID_PATTERN.fullmatch(self.link_id):
            raise GroupDomainError("link_id must be an opaque gsl_<uuid> identifier")
        if not GROUP_ID_PATTERN.fullmatch(self.group_id):
            raise GroupDomainError("source link group_id is invalid")
        if not SOURCE_ID_PATTERN.fullmatch(self.source_id):
            raise GroupDomainError("source_id must be an opaque src_<uuid> identifier")


@dataclass
class Group:
    group_id: str
    name: str
    template: GroupTemplate
    lifecycle: GroupLifecycle
    roster: list[GroupAgent]
    course_bindings: list[GroupCourseBinding] = field(default_factory=list)
    source_links: list[GroupSourceLink] = field(default_factory=list)
    budget: int | None = None
    model_overrides: dict[str, str] = field(default_factory=dict)
    tool_overrides: dict[str, list[str]] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    archived_at: str | None = None
    legacy_project_id: str | None = None
    schema_version: int = GROUP_SCHEMA_VERSION

    def validate(self) -> None:
        if not GROUP_ID_PATTERN.fullmatch(self.group_id):
            raise GroupDomainError("group_id must be an opaque grp_<uuid> identifier")
        _bounded_text(self.name, "group name", 160)
        roles = {agent.role for agent in self.roster}
        missing = MANDATORY_ROLES - roles
        if missing:
            raise GroupDomainError("group roster is missing mandatory roles: " + ", ".join(sorted(missing)))
        if sum(agent.role == "coordinator" for agent in self.roster) != 1:
            raise GroupDomainError("group roster must contain exactly one Coordinator")
        if sum(agent.role == "verifier" for agent in self.roster) != 1:
            raise GroupDomainError("group roster must contain exactly one Verifier")
        ids = [agent.agent_id for agent in self.roster]
        if len(ids) != len(set(ids)):
            raise GroupDomainError("group roster contains duplicate agent ids")
        if any(agent.group_id != self.group_id for agent in self.roster):
            raise GroupDomainError("group roster crosses group isolation boundary")
        if any(binding.group_id != self.group_id for binding in self.course_bindings):
            raise GroupDomainError("course binding crosses group isolation boundary")
        if any(link.group_id != self.group_id for link in self.source_links):
            raise GroupDomainError("source link crosses group isolation boundary")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "group_id": self.group_id,
            "name": self.name,
            "template": self.template.value,
            "lifecycle": self.lifecycle.value,
            "roster": [agent.to_dict() for agent in self.roster],
            "course_bindings": [asdict(item) for item in self.course_bindings],
            "source_links": [asdict(item) for item in self.source_links],
            "budget": self.budget,
            "model_overrides": dict(self.model_overrides),
            "tool_overrides": {key: list(value) for key, value in self.tool_overrides.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "archived_at": self.archived_at,
            "legacy_project_id": self.legacy_project_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Group":
        group = cls(
            group_id=str(raw["group_id"]), name=str(raw["name"]),
            template=GroupTemplate(str(raw.get("template") or GroupTemplate.CUSTOM.value)),
            lifecycle=GroupLifecycle(str(raw.get("lifecycle") or GroupLifecycle.ACTIVE.value)),
            roster=[GroupAgent.from_dict(row) for row in raw.get("roster") or ()],
            course_bindings=[GroupCourseBinding(**row) for row in raw.get("course_bindings") or ()],
            source_links=[GroupSourceLink(**row) for row in raw.get("source_links") or ()],
            budget=_optional_int(raw.get("budget")),
            model_overrides={str(k): str(v) for k, v in (raw.get("model_overrides") or {}).items()},
            tool_overrides={str(k): [str(x) for x in v] for k, v in (raw.get("tool_overrides") or {}).items()},
            created_at=str(raw.get("created_at") or utc_now()),
            updated_at=str(raw.get("updated_at") or raw.get("created_at") or utc_now()),
            archived_at=_optional_text(raw.get("archived_at")),
            legacy_project_id=_optional_text(raw.get("legacy_project_id")),
            schema_version=int(raw.get("schema_version") or GROUP_SCHEMA_VERSION),
        )
        group.validate()
        return group


@dataclass(frozen=True)
class GroupRuntimeSummary:
    group_id: str
    lifecycle: GroupLifecycle
    active_agent_count: int
    course_binding_count: int
    source_count: int
    shared_source_count: int
    budget: int | None
    updated_at: str
    active_primary_run_id: str | None = None
    primary_task_status: str | None = None
    queued_task_count: int = 0
    active_side_task_count: int = 0
    pending_approval_count: int = 0
    budget_usage: dict[str, float | int] = field(default_factory=lambda: {
        "wall_clock_seconds": 0.0, "model_turns": 0, "tool_calls": 0,
        "tokens": 0, "cost_usd": 0.0,
    })
    indexing_state: str = "idle"


@dataclass(frozen=True)
class GroupDeletePreview:
    group_id: str
    confirmation_token: str
    agent_count: int
    course_binding_count: int
    source_link_count: int
    exclusive_source_ids: tuple[str, ...]
    shared_source_ids: tuple[str, ...]
    source_reference_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "confirmation_token": self.confirmation_token,
            "agent_count": self.agent_count,
            "course_binding_count": self.course_binding_count,
            "source_link_count": self.source_link_count,
            "exclusive_source_ids": list(self.exclusive_source_ids),
            "shared_source_ids": list(self.shared_source_ids),
            "source_reference_counts": dict(self.source_reference_counts),
        }


@dataclass(frozen=True)
class GroupDeletionReceipt:
    """Content-free audit receipt.  Names, paths, prompts, and source bodies are excluded."""

    receipt_id: str
    group_id: str
    deleted_at: str
    removed_agent_count: int
    removed_course_binding_count: int
    removed_source_link_count: int
    deleted_source_ids: tuple[str, ...]
    retained_shared_source_ids: tuple[str, ...]
    schema_version: int = GROUP_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["deleted_source_ids"] = list(self.deleted_source_ids)
        row["retained_shared_source_ids"] = list(self.retained_shared_source_ids)
        return row


class GroupRepository:
    """Atomic JSON repository for Group metadata and source reference ownership."""

    def __init__(self, data_root: str | os.PathLike[str]) -> None:
        self.data_root = Path(data_root)
        self.root = self.data_root / "groups" / f"v{GROUP_SCHEMA_VERSION}"
        self.registry_path = self.root / "registry.json"
        self.receipts_path = self.root / "deletion_receipts.json"
        self.deletion_prepare_listener: Callable[[], None] | None = None
        self.root.mkdir(parents=True, exist_ok=True)
        self._normalize_duplicate_journal()

    def _load_payload(self) -> dict[str, Any]:
        if not self.registry_path.is_file():
            return {
                "schema_version": GROUP_SCHEMA_VERSION, "revision": 0,
                "groups": [], "sources": [], "deletion_transactions": [],
                "deletion_revision": 0,
                "deletion_receipts": [], "duplicate_transactions": [],
            }
        raw = json.loads(self.registry_path.read_text("utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != GROUP_SCHEMA_VERSION:
            raise GroupDomainError("unsupported or corrupt Group registry")
        if not isinstance(raw.get("groups"), list) or not isinstance(raw.get("sources"), list):
            raise GroupDomainError("corrupt Group registry collections")
        raw.setdefault("deletion_transactions", [])
        raw.setdefault("deletion_revision", 0)
        raw.setdefault("deletion_receipts", [])
        raw.setdefault("duplicate_transactions", [])
        if not all(isinstance(raw.get(key), list) for key in (
            "deletion_transactions", "deletion_receipts", "duplicate_transactions",
        )):
            raise GroupDomainError("corrupt Group transaction collections")
        return raw

    def deletion_revision(self) -> int:
        return max(0, int(self._load_payload().get("deletion_revision") or 0))

    def _save_payload(self, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        payload["schema_version"] = GROUP_SCHEMA_VERSION
        payload["revision"] = int(payload.get("revision") or 0) + 1
        _atomic_write_json(self.registry_path, payload)

    def _normalize_duplicate_journal(self) -> None:
        """Atomically scrub full snapshots retained by the pre-fence duplicate format."""
        if not self.registry_path.is_file():
            return
        payload = self._load_payload()
        allowed = {
            "transaction_id", "source_group_id", "group_id", "runtime_project_id",
            "request_id", "created_at", "stage", "updated_at",
        }
        changed = False
        normalized: list[dict[str, Any]] = []
        for row in payload["duplicate_transactions"]:
            if (
                isinstance(row, Mapping)
                and row.get("stage") in {"committed", "deleted"}
                and ("group" in row or set(row) - allowed)
            ):
                normalized.append(self._minimal_duplicate_transaction(
                    row, stage=str(row["stage"]),
                ))
                changed = True
            else:
                normalized.append(dict(row))
        if changed:
            payload["duplicate_transactions"] = normalized
            self._save_payload(payload)

    def list_groups(self, *, include_archived: bool = True) -> list[Group]:
        groups = [Group.from_dict(row) for row in self._load_payload()["groups"]]
        if include_archived:
            return groups
        return [group for group in groups if group.lifecycle is GroupLifecycle.ACTIVE]

    def get(self, group_id: str) -> Group:
        for group in self.list_groups():
            if group.group_id == group_id:
                return group
        raise GroupNotFoundError(f"group not found: {group_id}")

    def resolve_group_id(self, value: str) -> str:
        value = str(value or "").strip()
        for group in self.list_groups():
            if group.group_id == value or group.legacy_project_id == value:
                return group.group_id
        raise GroupNotFoundError(f"group not found: {value}")

    def resolve_identity(
        self, value: str, *, include_deleted: bool = False,
    ) -> tuple[str, str | None]:
        value = str(value or "").strip()
        payload = self._load_payload()
        for raw in payload["groups"]:
            if raw.get("group_id") == value or raw.get("legacy_project_id") == value:
                return str(raw["group_id"]), _optional_text(raw.get("legacy_project_id"))
        if include_deleted:
            for raw in payload["deletion_transactions"]:
                if raw.get("group_id") == value or raw.get("runtime_project_id") == value:
                    return str(raw["group_id"]), _optional_text(raw.get("runtime_project_id"))
        raise GroupNotFoundError(f"group not found: {value}")

    def runtime_project_id(self, value: str) -> str:
        group_id, runtime_id = self.resolve_identity(value)
        return runtime_id or group_id

    @staticmethod
    def _pending_delete(payload: Mapping[str, Any], group_id: str) -> dict[str, Any] | None:
        return next((
            row for row in payload.get("deletion_transactions") or ()
            if row.get("group_id") == group_id and row.get("stage") != "committed"
        ), None)

    def _assert_mutable(self, payload: Mapping[str, Any], group_id: str) -> None:
        if self._pending_delete(payload, group_id) is not None:
            raise GroupDomainError("group deletion is already in progress")

    @staticmethod
    def _assert_source_unlocked(payload: Mapping[str, Any], source_id: str) -> None:
        for row in payload.get("deletion_transactions") or ():
            if row.get("stage") == "committed":
                continue
            locked = set(row.get("exclusive_source_ids") or ()) | set(row.get("shared_source_ids") or ())
            if source_id in locked:
                raise GroupDomainError("source ownership is locked by a pending group deletion")

    def create_group(
        self,
        name: str,
        *,
        template: GroupTemplate | str = GroupTemplate.CUSTOM,
        optional_roles: Sequence[str] = (),
        experts: Sequence[Mapping[str, Any] | ExpertTemplateConfig] = (),
        budget: int | None = None,
        model_overrides: Mapping[str, str] | None = None,
        tool_overrides: Mapping[str, Sequence[str]] | None = None,
        group_id: str | None = None,
        legacy_project_id: str | None = None,
        created_at: str | None = None,
    ) -> Group:
        gid = group_id or opaque_id("grp")
        if not GROUP_ID_PATTERN.fullmatch(gid):
            raise GroupDomainError("group_id must be an opaque grp_<uuid> identifier")
        template_value = template if isinstance(template, GroupTemplate) else GroupTemplate(str(template))
        if budget is not None and (not isinstance(budget, int) or isinstance(budget, bool) or budget < 0):
            raise GroupDomainError("group budget must be a non-negative integer")
        roles = list(dict.fromkeys(str(role) for role in optional_roles))
        if not set(roles).issubset(OPTIONAL_BUILTIN_ROLES):
            raise GroupDomainError("optional roles are limited to knowledge, planning, and coach")
        roster = [
            self._builtin_agent(gid, "coordinator", "Coordinator"),
            self._builtin_agent(gid, "verifier", "Verifier"),
        ]
        roster.extend(self._builtin_agent(gid, role, role.title()) for role in roles)
        for expert_raw in experts:
            expert = expert_raw if isinstance(expert_raw, ExpertTemplateConfig) else ExpertTemplateConfig.from_payload(expert_raw)
            roster.append(GroupAgent(
                agent_id=opaque_id("gag"), group_id=gid, role="expert",
                display_name=expert.name, template_key="expert", expert=expert,
                model_override=expert.model_override, tool_overrides=expert.allowed_tools,
                budget=expert.budget,
            ))
        now = created_at or utc_now()
        group = Group(
            group_id=gid, name=_bounded_text(name, "group name", 160),
            template=template_value, lifecycle=GroupLifecycle.ACTIVE, roster=roster,
            budget=budget,
            model_overrides={str(k): str(v) for k, v in (model_overrides or {}).items()},
            tool_overrides={str(k): [str(item) for item in v] for k, v in (tool_overrides or {}).items()},
            created_at=now, updated_at=now, legacy_project_id=legacy_project_id,
        )
        group.validate()
        payload = self._load_payload()
        if any(row.get("group_id") == gid for row in payload["groups"]):
            raise GroupDomainError(f"group already exists: {gid}")
        if legacy_project_id and any(row.get("legacy_project_id") == legacy_project_id for row in payload["groups"]):
            raise GroupDomainError(f"legacy project already migrated: {legacy_project_id}")
        payload["groups"].append(group.to_dict())
        self._save_payload(payload)
        return group

    @staticmethod
    def _builtin_agent(group_id: str, role: str, display_name: str) -> GroupAgent:
        return GroupAgent(
            agent_id=opaque_id("gag"), group_id=group_id, role=role,
            display_name=display_name, template_key=role,
        )

    def _replace(self, updated: Group) -> Group:
        updated.validate()
        payload = self._load_payload()
        self._assert_mutable(payload, updated.group_id)
        for index, raw in enumerate(payload["groups"]):
            if raw.get("group_id") == updated.group_id:
                updated.updated_at = utc_now()
                payload["groups"][index] = updated.to_dict()
                self._save_payload(payload)
                return updated
        raise GroupNotFoundError(f"group not found: {updated.group_id}")

    def rename(self, group_id: str, name: str) -> Group:
        group = self.get(group_id)
        group.name = _bounded_text(name, "group name", 160)
        return self._replace(group)

    def archive(self, group_id: str) -> Group:
        group = self.get(group_id)
        if group.lifecycle is not GroupLifecycle.ARCHIVED:
            group.lifecycle = GroupLifecycle.ARCHIVED
            group.archived_at = utc_now()
            self._replace(group)
        return group

    def restore(self, group_id: str) -> Group:
        group = self.get(group_id)
        if group.lifecycle is not GroupLifecycle.ACTIVE:
            group.lifecycle = GroupLifecycle.ACTIVE
            group.archived_at = None
            self._replace(group)
        return group

    def duplicate(
        self, group_id: str, *, name: str | None = None,
        legacy_project_id: str | None = None,
    ) -> Group:
        transaction = self.prepare_duplicate(
            group_id, name=name,
            legacy_project_id=legacy_project_id or opaque_id("runtime"),
        )
        return self.commit_duplicate(str(transaction["transaction_id"]))

    def prepare_duplicate(
        self, group_id: str, *, name: str | None = None,
        legacy_project_id: str, request_id: str | None = None,
    ) -> dict[str, Any]:
        source = self.get(group_id)
        payload = self._load_payload()
        self._assert_mutable(payload, source.group_id)
        existing = next((
            row for row in payload["duplicate_transactions"]
            if row.get("source_group_id") == group_id
            and (
                (request_id and row.get("request_id") == request_id)
                or row.get("runtime_project_id") == legacy_project_id
            )
        ), None)
        if existing is not None:
            return dict(existing)
        if legacy_project_id and any(
            row.get("legacy_project_id") == legacy_project_id for row in payload["groups"]
        ):
            raise GroupDomainError(f"runtime project already belongs to a group: {legacy_project_id}")
        for link in source.source_links:
            self._assert_source_unlocked(payload, link.source_id)
        duplicate_id = opaque_id("grp")
        now = utc_now()
        roster = [
            GroupAgent(
                agent_id=opaque_id("gag"), group_id=duplicate_id, role=agent.role,
                display_name=agent.display_name, template_key=agent.template_key,
                expert=agent.expert, model_override=agent.model_override,
                tool_overrides=agent.tool_overrides, budget=agent.budget,
                created_at=now, updated_at=now,
            )
            for agent in source.roster
        ]
        duplicate = Group(
            group_id=duplicate_id,
            name=_bounded_text(name or f"{source.name} copy", "group name", 160),
            template=source.template, lifecycle=GroupLifecycle.ACTIVE, roster=roster,
            course_bindings=[
                GroupCourseBinding(opaque_id("gcb"), duplicate_id, item.course_id, now)
                for item in source.course_bindings
            ],
            source_links=[
                GroupSourceLink(opaque_id("gsl"), duplicate_id, item.source_id, now)
                for item in source.source_links
            ],
            budget=source.budget, model_overrides=dict(source.model_overrides),
            tool_overrides={key: list(value) for key, value in source.tool_overrides.items()},
            created_at=now, updated_at=now, legacy_project_id=legacy_project_id,
        )
        duplicate.validate()
        transaction = {
            "transaction_id": opaque_id("gdup"),
            "source_group_id": group_id,
            "group_id": duplicate_id,
            "runtime_project_id": legacy_project_id,
            "request_id": request_id,
            "stage": "prepared",
            "group": duplicate.to_dict(),
            "created_at": now,
            "updated_at": now,
        }
        payload["duplicate_transactions"].append(transaction)
        self._save_payload(payload)
        return dict(transaction)

    def commit_duplicate(
        self, transaction_id: str, *, require_runtime_provisioned: bool = False,
    ) -> Group:
        payload = self._load_payload()
        for index, row in enumerate(payload["duplicate_transactions"]):
            if row.get("transaction_id") != transaction_id:
                continue
            if row.get("stage") == "deleted":
                raise GroupDomainError("duplicate transaction target was deleted")
            if row.get("stage") == "committed":
                committed = next((
                    item for item in payload["groups"]
                    if item.get("group_id") == row.get("group_id")
                ), None)
                if not isinstance(committed, Mapping):
                    raise GroupNotFoundError(
                        f"committed duplicate group not found: {row.get('group_id')}"
                    )
                return Group.from_dict(committed)
            if require_runtime_provisioned and row.get("stage") != "runtime_provisioned":
                raise GroupDomainError("duplicate runtime provisioning is not durable")
            group_raw = row.get("group")
            if not isinstance(group_raw, Mapping):
                raise GroupDomainError("duplicate transaction is missing Group configuration")
            duplicate = Group.from_dict(group_raw)
            if not any(item.get("group_id") == duplicate.group_id for item in payload["groups"]):
                payload["groups"].append(duplicate.to_dict())
            payload["duplicate_transactions"][index] = self._minimal_duplicate_transaction(
                row, stage="committed",
            )
            self._save_payload(payload)
            return duplicate
        raise GroupNotFoundError(f"duplicate transaction not found: {transaction_id}")

    def mark_duplicate_runtime_provisioned(self, transaction_id: str) -> dict[str, Any]:
        payload = self._load_payload()
        for index, row in enumerate(payload["duplicate_transactions"]):
            if row.get("transaction_id") != transaction_id:
                continue
            if row.get("stage") in {"runtime_provisioned", "committed"}:
                return dict(row)
            if row.get("stage") != "prepared":
                raise GroupDomainError("duplicate transaction cannot be provisioned")
            updated = {**row, "stage": "runtime_provisioned", "updated_at": utc_now()}
            payload["duplicate_transactions"][index] = updated
            self._save_payload(payload)
            return dict(updated)
        raise GroupNotFoundError(f"duplicate transaction not found: {transaction_id}")

    @staticmethod
    def _minimal_duplicate_transaction(
        row: Mapping[str, Any], *, stage: str,
    ) -> dict[str, Any]:
        """Keep only opaque routing/idempotency metadata after publication or deletion."""
        return {
            key: row.get(key)
            for key in (
                "transaction_id", "source_group_id", "group_id", "runtime_project_id",
                "request_id", "created_at",
            )
        } | {"stage": stage, "updated_at": utc_now()}

    def duplicate_transaction(self, group_id: str, request_id: str) -> dict[str, Any] | None:
        return next((
            dict(row) for row in self._load_payload()["duplicate_transactions"]
            if row.get("source_group_id") == group_id and row.get("request_id") == request_id
        ), None)

    def uncommitted_duplicate_runtime_ids(self) -> set[str]:
        """Durable routing fence for every duplicate not yet canonically committed."""
        return {
            str(row["runtime_project_id"])
            for row in self._load_payload()["duplicate_transactions"]
            if row.get("stage") not in {"committed", "deleted"}
            and isinstance(row.get("runtime_project_id"), str)
            and str(row["runtime_project_id"]).strip()
        }

    def recover_duplicate_transactions(self, runtime_project_ids: Iterable[str]) -> list[str]:
        available = set(runtime_project_ids)
        recovered: list[str] = []
        for row in list(self._load_payload()["duplicate_transactions"]):
            if (
                row.get("stage") not in {"prepared", "runtime_provisioned"}
                or row.get("runtime_project_id") not in available
            ):
                continue
            if row.get("stage") == "prepared":
                row = self.mark_duplicate_runtime_provisioned(str(row["transaction_id"]))
            group = self.commit_duplicate(
                str(row["transaction_id"]), require_runtime_provisioned=True,
            )
            recovered.append(group.group_id)
        return recovered

    def bind_course(self, group_id: str, course_id: str) -> GroupCourseBinding:
        group = self.get(group_id)
        cid = _bounded_text(course_id, "course_id", 160)
        existing = next((item for item in group.course_bindings if item.course_id == cid), None)
        if existing:
            return existing
        binding = GroupCourseBinding(opaque_id("gcb"), group.group_id, cid)
        group.course_bindings.append(binding)
        self._replace(group)
        return binding

    def unbind_course(self, group_id: str, course_id: str) -> bool:
        group = self.get(group_id)
        kept = [item for item in group.course_bindings if item.course_id != course_id]
        changed = len(kept) != len(group.course_bindings)
        if changed:
            group.course_bindings = kept
            self._replace(group)
        return changed

    def update_roster(self, group_id: str, agents: Sequence[GroupAgent | Mapping[str, Any]]) -> Group:
        group = self.get(group_id)
        parsed: list[GroupAgent] = []
        for raw in agents:
            agent = raw if isinstance(raw, GroupAgent) else GroupAgent.from_dict(raw)
            if agent.group_id != group.group_id:
                raise GroupDomainError("roster update crosses group isolation boundary")
            parsed.append(agent)
        group.roster = parsed
        group.validate()
        return self._replace(group)

    def link_source(self, group_id: str, source_id: str | None = None) -> GroupSourceLink:
        group = self.get(group_id)
        sid = source_id or opaque_id("src")
        if not SOURCE_ID_PATTERN.fullmatch(sid):
            raise GroupDomainError("source_id must be an opaque src_<uuid> identifier")
        payload = self._load_payload()
        self._assert_mutable(payload, group.group_id)
        self._assert_source_unlocked(payload, sid)
        existing = next((item for item in group.source_links if item.source_id == sid), None)
        if existing:
            return existing
        link = GroupSourceLink(opaque_id("gsl"), group.group_id, sid)
        group.source_links.append(link)
        if not any(item.get("source_id") == sid for item in payload["sources"]):
            payload["sources"].append({"source_id": sid, "created_at": utc_now()})
        for index, raw in enumerate(payload["groups"]):
            if raw.get("group_id") == group.group_id:
                group.updated_at = utc_now()
                payload["groups"][index] = group.to_dict()
                self._save_payload(payload)
                return link
        raise GroupNotFoundError(f"group not found: {group_id}")

    def unlink_source(self, group_id: str, source_id: str) -> bool:
        group = self.get(group_id)
        payload = self._load_payload()
        self._assert_mutable(payload, group.group_id)
        self._assert_source_unlocked(payload, source_id)
        kept = [item for item in group.source_links if item.source_id != source_id]
        changed = len(kept) != len(group.source_links)
        if not changed:
            return False
        group.source_links = kept
        for index, raw in enumerate(payload["groups"]):
            if raw.get("group_id") == group.group_id:
                group.updated_at = utc_now()
                payload["groups"][index] = group.to_dict()
                break
        if self._reference_count_from_payload(payload, source_id) == 0:
            payload["sources"] = [item for item in payload["sources"] if item.get("source_id") != source_id]
        self._save_payload(payload)
        return True

    def source_reference_count(self, source_id: str) -> int:
        return self._reference_count_from_payload(self._load_payload(), source_id)

    @staticmethod
    def _reference_count_from_payload(payload: Mapping[str, Any], source_id: str) -> int:
        return sum(
            1 for group in payload.get("groups") or ()
            for link in group.get("source_links") or ()
            if link.get("source_id") == source_id
        )

    def runtime_summary(self, group_id: str) -> GroupRuntimeSummary:
        group = self.get(group_id)
        refs = {link.source_id: self.source_reference_count(link.source_id) for link in group.source_links}
        return GroupRuntimeSummary(
            group_id=group.group_id, lifecycle=group.lifecycle,
            active_agent_count=len(group.roster), course_binding_count=len(group.course_bindings),
            source_count=len(group.source_links), shared_source_count=sum(count > 1 for count in refs.values()),
            budget=group.budget, updated_at=group.updated_at,
        )

    def delete_preview(self, group_id: str) -> GroupDeletePreview:
        group = self.get(group_id)
        refs = {link.source_id: self.source_reference_count(link.source_id) for link in group.source_links}
        exclusive = tuple(sorted(source_id for source_id, count in refs.items() if count == 1))
        shared = tuple(sorted(source_id for source_id, count in refs.items() if count > 1))
        token_body = {
            "group_id": group.group_id, "updated_at": group.updated_at,
            "source_reference_counts": refs,
        }
        token = hashlib.sha256(
            json.dumps(token_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return GroupDeletePreview(
            group_id=group.group_id, confirmation_token=token,
            agent_count=len(group.roster), course_binding_count=len(group.course_bindings),
            source_link_count=len(group.source_links), exclusive_source_ids=exclusive,
            shared_source_ids=shared, source_reference_counts=refs,
        )

    def prepare_delete(
        self, group_id: str, confirmation_token: str, *, runtime_project_id: str | None = None,
    ) -> dict[str, Any]:
        """Durably record delete intent and its future content-free receipt.

        This is the first write in the cross-layer state machine.  Once present, all
        group/source mutations are fenced until the transaction commits or is retried.
        """
        payload = self._load_payload()
        existing = next((
            row for row in payload["deletion_transactions"]
            if row.get("group_id") == group_id
        ), None)
        if existing is not None:
            if existing.get("confirmation_token") != confirmation_token:
                raise StaleDeletePreviewError("delete preview is stale or was not confirmed")
            return dict(existing)

        preview = self.delete_preview(group_id)
        if not confirmation_token or confirmation_token != preview.confirmation_token:
            raise StaleDeletePreviewError("delete preview is stale or was not confirmed")
        for source_id in (*preview.exclusive_source_ids, *preview.shared_source_ids):
            self._assert_source_unlocked(payload, source_id)
        group = self.get(group_id)
        runtime_id = runtime_project_id or group.legacy_project_id or group.group_id
        receipt = GroupDeletionReceipt(
            receipt_id=opaque_id("gdr"), group_id=group_id, deleted_at=utc_now(),
            removed_agent_count=preview.agent_count,
            removed_course_binding_count=preview.course_binding_count,
            removed_source_link_count=preview.source_link_count,
            deleted_source_ids=preview.exclusive_source_ids,
            retained_shared_source_ids=preview.shared_source_ids,
        )
        transaction = {
            "transaction_id": opaque_id("gdt"),
            "group_id": group_id,
            "runtime_project_id": runtime_id,
            "confirmation_token": confirmation_token,
            "stage": "prepared",
            "exclusive_source_ids": list(preview.exclusive_source_ids),
            "shared_source_ids": list(preview.shared_source_ids),
            "source_reference_counts": dict(preview.source_reference_counts),
            "receipt": receipt.to_dict(),
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        payload = self._load_payload()
        if self._pending_delete(payload, group_id) is not None:
            return dict(self._pending_delete(payload, group_id) or transaction)
        payload["deletion_transactions"].append(transaction)
        payload["deletion_revision"] = int(payload.get("deletion_revision") or 0) + 1
        self._save_payload(payload)
        listener = self.deletion_prepare_listener
        if listener is not None:
            listener()
        return dict(transaction)

    def delete_transaction(self, identity: str) -> dict[str, Any] | None:
        value = str(identity or "").strip()
        for row in self._load_payload()["deletion_transactions"]:
            if row.get("group_id") == value or row.get("runtime_project_id") == value:
                return dict(row)
        return None

    def pending_delete_transactions(self) -> tuple[dict[str, Any], ...]:
        """Expose resumable, already-confirmed delete intents for startup recovery."""
        return tuple(
            dict(row) for row in self._load_payload()["deletion_transactions"]
            if row.get("stage") != "committed"
        )

    def pending_delete_transactions_page(
        self, *, cursor: str = "", limit: int = 64,
    ) -> dict[str, Any]:
        """Return one stable opaque-ID page for bounded startup reconciliation."""
        bounded = max(1, min(int(limit), 256))
        rows: list[dict[str, Any]] = []
        keys: list[str] = []
        after = str(cursor or "")
        for raw in self._load_payload()["deletion_transactions"]:
            key = str(raw.get("group_id") or "")
            if raw.get("stage") == "committed" or key <= after:
                continue
            index = bisect.bisect_left(keys, key)
            keys.insert(index, key)
            rows.insert(index, dict(raw))
            if len(rows) > bounded + 1:
                rows.pop()
                keys.pop()
        has_more = len(rows) > bounded
        visible = rows[:bounded]
        next_cursor = str(visible[-1]["group_id"]) if has_more and visible else ""
        return {
            "items": tuple(visible), "cursor": next_cursor,
            "has_more": has_more, "limit": bounded,
        }

    def mark_runtime_deleted(self, transaction_id: str) -> dict[str, Any]:
        payload = self._load_payload()
        for index, row in enumerate(payload["deletion_transactions"]):
            if row.get("transaction_id") != transaction_id:
                continue
            if row.get("stage") == "committed":
                return dict(row)
            updated = {**row, "stage": "runtime_deleted", "updated_at": utc_now()}
            payload["deletion_transactions"][index] = updated
            self._save_payload(payload)
            return dict(updated)
        raise GroupNotFoundError(f"delete transaction not found: {transaction_id}")

    def commit_delete(self, transaction_id: str) -> GroupDeletionReceipt:
        """Atomically commit Group/source removal and its durable receipt."""
        payload = self._load_payload()
        for index, row in enumerate(payload["deletion_transactions"]):
            if row.get("transaction_id") != transaction_id:
                continue
            receipt_raw = row.get("receipt")
            if not isinstance(receipt_raw, Mapping):
                raise GroupDomainError("delete transaction is missing its durable receipt")
            receipt = GroupDeletionReceipt(
                receipt_id=str(receipt_raw["receipt_id"]), group_id=str(receipt_raw["group_id"]),
                deleted_at=str(receipt_raw["deleted_at"]),
                removed_agent_count=int(receipt_raw["removed_agent_count"]),
                removed_course_binding_count=int(receipt_raw["removed_course_binding_count"]),
                removed_source_link_count=int(receipt_raw["removed_source_link_count"]),
                deleted_source_ids=tuple(str(x) for x in receipt_raw.get("deleted_source_ids") or ()),
                retained_shared_source_ids=tuple(str(x) for x in receipt_raw.get("retained_shared_source_ids") or ()),
                schema_version=int(receipt_raw.get("schema_version") or GROUP_SCHEMA_VERSION),
            )
            if row.get("stage") == "committed":
                return receipt
            if row.get("stage") != "runtime_deleted":
                raise GroupDomainError("runtime deletion has not completed")
            group_id = str(row["group_id"])
            payload["groups"] = [
                item for item in payload["groups"] if item.get("group_id") != group_id
            ]
            # Re-evaluate ownership at commit. Pending source locks make this stable,
            # while the guard prevents deleting a source newly referenced elsewhere.
            candidates = set(str(x) for x in row.get("exclusive_source_ids") or ())
            deletable = {
                source_id for source_id in candidates
                if self._reference_count_from_payload(payload, source_id) == 0
            }
            payload["sources"] = [
                item for item in payload["sources"] if item.get("source_id") not in deletable
            ]
            committed_receipt = receipt.to_dict()
            committed_receipt["deleted_source_ids"] = sorted(deletable)
            if not any(
                item.get("receipt_id") == receipt.receipt_id
                for item in payload["deletion_receipts"]
            ):
                payload["deletion_receipts"].append(committed_receipt)
            payload["deletion_transactions"][index] = {
                **row, "stage": "committed", "receipt": committed_receipt,
                "updated_at": utc_now(),
            }
            payload["duplicate_transactions"] = [
                self._minimal_duplicate_transaction(item, stage="deleted")
                if item.get("group_id") == group_id else item
                for item in payload["duplicate_transactions"]
            ]
            self._save_payload(payload)
            return GroupDeletionReceipt(
                **{
                    **receipt.__dict__,
                    "deleted_source_ids": tuple(sorted(deletable)),
                }
            )
        raise GroupNotFoundError(f"delete transaction not found: {transaction_id}")

    def confirmed_delete(self, group_id: str, confirmation_token: str) -> GroupDeletionReceipt:
        """Repository-only deletion uses the same resumable state machine."""
        transaction = self.prepare_delete(group_id, confirmation_token)
        if transaction.get("stage") == "prepared":
            transaction = self.mark_runtime_deleted(str(transaction["transaction_id"]))
        return self.commit_delete(str(transaction["transaction_id"]))


def migrate_legacy_projects(data_root: str | os.PathLike[str]) -> dict[str, Any]:
    """Run the Group migration with an outer rollback/report fence."""
    root = Path(data_root)
    registry_path = root / "groups" / f"v{GROUP_SCHEMA_VERSION}" / "registry.json"
    registry_existed = registry_path.is_file()
    registry_before: bytes | None = None
    source_before: dict[str, str | None] = {}
    before_errors: dict[str, str] = {}
    try:
        if registry_existed:
            registry_before = registry_path.read_bytes()
        GroupRepository(root)  # applies idempotent schema/journal normalization inside the fence
        source_before, before_errors = _safe_legacy_source_inventory(root)
        if before_errors:
            details = "; ".join(f"{path}: {error}" for path, error in sorted(before_errors.items()))
            raise OSError(f"legacy source inventory failed: {details}")
        return _migrate_legacy_projects_impl(root)
    except Exception as exc:
        if registry_before is not None:
            _atomic_write_bytes(registry_path, registry_before)
        elif not registry_existed:
            registry_path.unlink(missing_ok=True)
        report_path = root / "migration_reports" / "groups-v1.json"
        source_after, after_errors = _safe_legacy_source_inventory(root)
        all_paths = sorted(set(source_before) | set(source_after) | set(before_errors) | set(after_errors))
        evidence = [
            {
                "path": path,
                "before_sha256": digest,
                "after_sha256": source_after.get(path),
                "before_error": before_errors.get(path),
                "after_error": after_errors.get(path),
                "unchanged": bool(
                    digest and source_after.get(path) == digest
                    and path not in before_errors and path not in after_errors
                ),
            }
            for path in all_paths
            for digest in (source_before.get(path),)
        ]
        unchanged = bool(evidence) and all(item["unchanged"] for item in evidence)
        _atomic_write_json(report_path, {
            "version": 1,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
            "legacy_source_unchanged": unchanged,
            "legacy_source_evidence": evidence,
            "backup_manifest": "migration_backups/groups-v1/manifest.json",
            "failed_at": utc_now(),
        })
        raise


def _legacy_source_inventory(root: Path) -> tuple[dict[str, str | None], dict[str, str]]:
    """Inventory legacy inputs without allowing one unreadable file to hide the report."""
    registry = root / "projects.json"
    if not registry.is_file():
        return {}, {}
    paths = [registry]
    errors: dict[str, str] = {}
    try:
        projects = json.loads(registry.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors["projects.json"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        projects = []
    if isinstance(projects, list):
        for row in projects:
            if isinstance(row, Mapping) and isinstance(row.get("project_id"), str):
                roster = root / "events" / f"{_legacy_safe_name(row['project_id'])}_roster.jsonl"
                if roster.is_file():
                    paths.append(roster)
    inventory: dict[str, str | None] = {}
    for path in paths:
        relative = str(path.relative_to(root)).replace("\\", "/")
        try:
            inventory[relative] = _sha256(path)
        except Exception as exc:  # noqa: BLE001 - evidence must survive arbitrary hash/read faults
            inventory[relative] = None
            errors[relative] = f"{type(exc).__name__}: {str(exc)[:300]}"
    return inventory, errors


def _safe_legacy_source_inventory(
    root: Path,
) -> tuple[dict[str, str | None], dict[str, str]]:
    try:
        return _legacy_source_inventory(root)
    except Exception as exc:  # noqa: BLE001 - reporting must outlive inventory implementation faults
        return {}, {"<inventory>": f"{type(exc).__name__}: {str(exc)[:300]}"}


def _migrate_legacy_projects_impl(data_root: str | os.PathLike[str]) -> dict[str, Any]:
    """Backup-first, copy-only migration from ``projects.json`` to Group schema v1.

    Legacy files are opened read-only and checksummed before and after migration.  Any
    exception removes only the newly-created Group registry (when no registry existed)
    and writes a diagnostic report; the source registry and roster JSONL files are
    never renamed, rewritten, or deleted.
    """

    root = Path(data_root)
    source_registry = root / "projects.json"
    repo = GroupRepository(root)
    report_dir = root / "migration_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "groups-v1.json"
    if not source_registry.is_file():
        report = {"version": 1, "status": "not_needed", "migrated": 0, "completed_at": utc_now()}
        _atomic_write_json(report_path, report)
        return report

    source_files = [source_registry]
    raw_projects = json.loads(source_registry.read_text("utf-8"))
    if not isinstance(raw_projects, list):
        raise GroupDomainError("legacy projects.json is not an array")
    for row in raw_projects:
        if isinstance(row, Mapping) and isinstance(row.get("project_id"), str):
            roster = root / "events" / f"{_legacy_safe_name(row['project_id'])}_roster.jsonl"
            if roster.is_file():
                source_files.append(roster)
    before = {str(path.relative_to(root)): _sha256(path) for path in source_files}

    backup_dir = root / "migration_backups" / "groups-v1"
    backup_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, str]] = []
    for source in source_files:
        relative = source.relative_to(root)
        destination = backup_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        digest = _sha256(destination)
        if digest != before[str(relative)]:
            raise OSError(f"migration backup verification failed: {relative}")
        manifest.append({"path": str(relative).replace("\\", "/"), "sha256": digest})
    _atomic_write_json(backup_dir / "manifest.json", {"version": 1, "files": manifest})

    registry_existed = repo.registry_path.is_file()
    migrated: list[dict[str, str]] = []
    try:
        for raw in raw_projects:
            if not isinstance(raw, Mapping) or not isinstance(raw.get("project_id"), str):
                raise GroupDomainError("legacy project row is invalid")
            legacy_id = str(raw["project_id"])
            try:
                existing_id = repo.resolve_group_id(legacy_id)
            except GroupNotFoundError:
                group = repo.create_group(
                    str(raw.get("name") or legacy_id), template=GroupTemplate.CUSTOM,
                    legacy_project_id=legacy_id,
                    created_at=str(raw.get("created_at") or utc_now()),
                )
                _migrate_legacy_roster(repo, group, root, legacy_id)
                existing_id = group.group_id
            migrated.append({"legacy_project_id": legacy_id, "group_id": existing_id})

        after = {str(path.relative_to(root)): _sha256(path) for path in source_files}
        if after != before:
            raise OSError("legacy source changed during Group migration")
        report = {
            "version": 1, "status": "complete", "migrated": len(migrated),
            "groups": migrated, "backup_manifest": str((backup_dir / "manifest.json").relative_to(root)).replace("\\", "/"),
            "legacy_source_unchanged": True, "completed_at": utc_now(),
        }
        _atomic_write_json(report_path, report)
        return report
    except Exception as exc:
        if not registry_existed:
            repo.registry_path.unlink(missing_ok=True)
        failure = {
            "version": 1, "status": "failed", "error_type": type(exc).__name__,
            "error": str(exc)[:1000], "legacy_source_unchanged": all(
                path.is_file() and _sha256(path) == before.get(str(path.relative_to(root)))
                for path in source_files
            ), "failed_at": utc_now(),
        }
        _atomic_write_json(report_path, failure)
        raise


def _migrate_legacy_roster(repo: GroupRepository, group: Group, root: Path, legacy_id: str) -> None:
    path = root / "events" / f"{_legacy_safe_name(legacy_id)}_roster.jsonl"
    latest: dict[str, dict[str, Any]] = {}
    if path.is_file():
        for line in path.read_text("utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and isinstance(row.get("agent_id"), str):
                latest[str(row["agent_id"])] = row
    roster = list(group.roster)
    for legacy_agent_id, row in latest.items():
        if row.get("status", "active") != "active" or legacy_agent_id == "coordinator":
            continue
        legacy_role = str(row.get("role") or "Legacy Expert")
        matched = _match_legacy_role(legacy_role)
        if matched in OPTIONAL_BUILTIN_ROLES and not any(agent.role == matched for agent in roster):
            roster.append(GroupAgent(
                agent_id=opaque_id("gag"), group_id=group.group_id, role=matched,
                display_name=str(row.get("name") or legacy_role), template_key=matched,
            ))
        else:
            expert = ExpertTemplateConfig(
                name=_bounded_text(str(row.get("name") or legacy_role), "legacy expert name", 80),
                specialty=_bounded_text(legacy_role, "legacy expert specialty", 200),
                responsibilities=("Migrated legacy responsibilities",),
                allowed_tools=(),
            )
            roster.append(GroupAgent(
                agent_id=opaque_id("gag"), group_id=group.group_id, role="legacy_expert",
                display_name=expert.name, template_key="legacy_expert", expert=expert,
                tool_overrides=(),
            ))
    repo.update_roster(group.group_id, roster)


def _match_legacy_role(role: str) -> str | None:
    lowered = role.casefold()
    if any(token in lowered for token in ("knowledge", "research", "资料", "知识", "研究")):
        return "knowledge"
    if any(token in lowered for token in ("plan", "schedule", "规划", "计划")):
        return "planning"
    if any(token in lowered for token in ("coach", "tutor", "导师", "教练", "辅导")):
        return "coach"
    return None


def _bounded_text(value: Any, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GroupDomainError(f"{field_name} is required")
    result = value.strip()
    if len(result) > max_length:
        raise GroupDomainError(f"{field_name} exceeds {max_length} characters")
    return result


def _bounded_text_list(
    value: Any, field_name: str, *, max_items: int, max_length: int,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise GroupDomainError(f"{field_name} must be a list")
    if len(value) > max_items:
        raise GroupDomainError(f"{field_name} exceeds {max_items} items")
    return tuple(_bounded_text(item, field_name, max_length) for item in value)


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _legacy_safe_name(value: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in value)[:120] or "_"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


__all__ = [
    "ALL_AGENT_ROLES", "EXPERT_ROLES", "GROUP_SCHEMA_VERSION", "Group",
    "GroupAgent", "GroupCourseBinding", "GroupDeletePreview", "GroupDeletionReceipt",
    "GroupDomainError", "GroupLifecycle", "GroupNotFoundError", "GroupRepository",
    "GroupRuntimeSummary", "GroupSourceLink", "GroupTemplate", "ExpertTemplateConfig",
    "MANDATORY_ROLES", "OPTIONAL_BUILTIN_ROLES", "StaleDeletePreviewError",
    "migrate_legacy_projects", "opaque_id",
]
