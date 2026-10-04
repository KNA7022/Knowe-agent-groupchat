"""v1.0.17 — conformance tests for the Runtime repositioning invariants.

These fitness functions retain no hidden intent inference, work-quality adjudication,
or output rewriting. The accepted Task Runtime policy adds hard budgets and bounded
recovery; governed behavioral tests cover those rules alongside the source guards.

Invariants (see design/2-PRD.md):
  I-1 no result adjudication      I-4 hard Task Runtime budgets
  I-2 no intent inference         I-5 no framework-authored context
  I-3 no output rewriting         I-6 provider-boundary well-formedness
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
import pytest

BACKEND = Path(__file__).resolve().parents[1]          # .../known_v1.0_React/backend
PKG = BACKEND                                           # main package
CORE = BACKEND / "knowe_core"
HARNESS = BACKEND / "knowe_harness"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── I-1 · no result adjudication ────────────────────────────────────────────

def test_runtime_emits_no_work_quality_verdict() -> None:
    text = _src(PKG / "runtime.py")
    for banned in (
        "completion_requirements_unsatisfied",
        "max_turns_exceeded",
        "repeated_tool_failure",
        "artifact_reverification_failed",
        "_validate_completion",
        "_reverify_artifacts",
    ):
        assert banned not in text, f"[I-1] runtime.py still references judge symbol: {banned}"


# ── I-2 · no intent inference ───────────────────────────────────────────────

def test_gateway_does_not_parse_the_goal() -> None:
    text = _src(PKG / "worker_gateway_runtime.py")
    # Symbols may be *named* in an explanatory comment, but must not be defined or used.
    for sym in ("_PATH_TOKEN_RE", "_MUTATION_RE", "_DELETE_RE", "_goal_paths", "_structured_paths"):
        assert f"{sym} =" not in text, f"[I-2] gateway still defines {sym}"
        assert f"{sym}(" not in text, f"[I-2] gateway still calls {sym}"
    assert ".findall(" not in text, "[I-2] gateway still regex-scans text"
    assert "re.compile(" not in text, "[I-2] gateway still compiles a goal-parsing regex"


def test_worker_prompt_carries_no_inferred_expectations() -> None:
    text = _src(PKG / "runtime.py")
    # The initial task payload must not inject inferred expected artifacts/deletions.
    assert '"expected_artifact_paths": list(expected)' not in text, \
        "[I-2] initial worker prompt still injects inferred expected_artifact_paths"


# ── I-4 · no effort adjudication by proxy ───────────────────────────────────

@pytest.mark.parametrize("dimension", ["wall_clock_seconds", "model_turns", "tool_calls", "tokens", "cost_usd"])
def test_task_runtime_enforces_explicit_hard_budgets(tmp_path, dimension) -> None:
    from backend.groups import GroupRepository
    from backend.task_runtime.service import TaskRuntimeService
    from backend.task_runtime.contracts import BudgetLimits, BudgetUsage, TaskStatus, FailureType

    groups = GroupRepository(tmp_path)
    service = TaskRuntimeService(tmp_path, groups)
    try:
        group = groups.create_group("Bounded task")
        run = service.submit(group.group_id, title="Budget", request_key="budget",
                             budget_limits=BudgetLimits(**{dimension: 1}))
        service.schedule()
        exact = service.consume_budget(group.group_id, run.run_id,
                                       BudgetUsage(**{dimension: 1}), request_key="exact")
        assert exact.status is TaskStatus.RUNNING
        exhausted = service.consume_budget(group.group_id, run.run_id,
                                           BudgetUsage(**{dimension: 1}), request_key="crossed")
        assert exhausted.status is TaskStatus.FAILED
        assert exhausted.failure_type is FailureType.BUDGET_EXHAUSTED
        events = service.replay(group.group_id, run.run_id, after_seq=0)["events"]
        assert events[-1]["public_payload"]["exhausted_dimension"] == dimension
    finally:
        service.close()


def test_exhausted_run_cannot_continue_governed_effects(tmp_path) -> None:
    from backend.commercial_gateway import GatewayDenied, LegacyToolBoundary
    from backend.knowe_core.tool_registry import ToolRegistry
    from backend.task_runtime.contracts import BudgetUsage, FailureType
    from tests.support_authority import AgentProviderAuthority

    owner = AgentProviderAuthority(tmp_path)
    effects = []
    registry = ToolRegistry()
    registry.register("safe_read_file", "read", {"type": "object"},
                      lambda args, **_: effects.append(args) or "private data")
    registry.set_execution_boundary(LegacyToolBoundary(
        owner.runtime, authority_provider=lambda: owner.authority,
        read_only_tools=frozenset({"safe_read_file"}),
    ))
    try:
        exhausted = owner.runtime.consume_budget(
            owner.group.group_id, owner.run.run_id, BudgetUsage(model_turns=21),
            request_key="exhaust",
        )
        assert exhausted.failure_type is FailureType.BUDGET_EXHAUSTED
        with pytest.raises(GatewayDenied):
            asyncio.run(registry.execute("safe_read_file", {"path": "secret"},
                                         tool_call_id="after-budget"))
        assert effects == []
    finally:
        owner.close()


# ── I-3 · no output rewriting ───────────────────────────────────────────────

def test_worker_text_is_passed_through_not_templated() -> None:
    engine = _src(PKG / "engine.py")
    assert "render_user_facing_completion(" not in engine, \
        "[I-3] engine still templates the Worker's final text"
    assert "rendered_text = raw_final or self._completion_message_text" in engine, \
        "[I-3] engine no longer passes the Worker's raw final text through"
    wc = _src(PKG / "worker_completion.py")
    assert "def render_user_facing_completion(" not in wc, \
        "[I-3] the templating renderer still exists"
    assert "本任务无文件产物" not in wc, "[I-3] placeholder sentence still present"


# ── I-5 · no framework-authored context ─────────────────────────────────────

def test_recovery_is_bounded_without_unauthorized_corrections() -> None:
    from backend.task_runtime.contracts import FailureType
    from backend.task_runtime.service import TaskRuntimeService

    assert TaskRuntimeService.recovery_action(FailureType.PARAMETER_ERROR,
                                             attempts=0, max_attempts=2) == "correct_parameters"
    for failure in FailureType:
        assert TaskRuntimeService.recovery_action(failure, attempts=2, max_attempts=2) == "stop"
    for failure in (FailureType.PERMISSION_DENIED, FailureType.BUDGET_EXHAUSTED,
                    FailureType.IRREVERSIBLE_SIDE_EFFECT):
        assert TaskRuntimeService.recovery_action(failure, attempts=0, max_attempts=2) == "stop"
    assert TaskRuntimeService.recovery_action(FailureType.TRANSIENT_PROVIDER_FAILURE,
        attempts=0, max_attempts=2, same_privacy=False, preauthorized_fallback=True) != "provider_fallback"
    assert TaskRuntimeService.recovery_action(FailureType.TRANSIENT_PROVIDER_FAILURE,
        attempts=0, max_attempts=2, same_privacy=True, preauthorized_fallback=False) != "provider_fallback"
    text = _src(PKG / "runtime.py")
    assert "_correction_message" not in text, "[I-5] runtime still injects framework corrections"


# ── completeness · single review channel ────────────────────────────────────

def test_single_coordinator_review_channel() -> None:
    proj = _src(HARNESS / "projections.py")
    # The harness free-text projection must no longer notify the coordinator.
    assert "notifier = getattr(self.engine, \"notify_coordinator\"" not in proj, \
        "[单通道] harness _coordinator still fires a second coordinator notice"
    engine = _src(PKG / "engine.py")
    assert 'notification_id = f"completion-review:{cid}:v{version}"' in engine, \
        "[单通道] notify_coordinator lacks the canonical (completion,version) identity"


# ── I-6 · provider-boundary well-formedness (functional unit) ───────────────

def _load_messages_module() -> types.ModuleType:
    """Load knowe_core.messages in isolation (its only dep is the light errors module),
    so the unit runs without provider/browser packages."""
    if "knowe_core" not in sys.modules:
        pkg = types.ModuleType("knowe_core")
        pkg.__path__ = [str(CORE)]
        sys.modules["knowe_core"] = pkg
    if "knowe_core.errors" not in sys.modules:
        espec = importlib.util.spec_from_file_location("knowe_core.errors", CORE / "errors.py")
        emod = importlib.util.module_from_spec(espec)
        assert espec and espec.loader
        espec.loader.exec_module(emod)
        sys.modules["knowe_core.errors"] = emod
    spec = importlib.util.spec_from_file_location("knowe_core.messages", CORE / "messages.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_sanitize_drops_malformed_empty_assistant_frame() -> None:
    messages = _load_messages_module()
    convo = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": ""},          # malformed empty frame
        {"role": "assistant", "content": None},        # malformed empty frame
        {"role": "assistant", "content": "  \n "},     # whitespace-only
        {"role": "assistant", "content": "real answer"},
    ]
    out = messages.sanitize_messages(convo)
    assistants = [m for m in out if m.get("role") == "assistant"]
    assert len(assistants) == 1, "[I-6] empty assistant frames were not dropped"
    assert assistants[0]["content"] == "real answer"


def test_sanitize_keeps_toolcall_only_assistant_frame() -> None:
    messages = _load_messages_module()
    convo = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "{}"},
    ]
    out = messages.sanitize_messages(convo)
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in out), \
        "[I-6] a valid tool_call-only assistant frame was wrongly dropped"


def test_actor_scope_breaks_inherited_visible_activity_emitter() -> None:
    from backend import tool_ledger

    parent_events: list[str] = []

    async def parent_emitter(text: str) -> None:
        parent_events.append(text)

    async def scenario() -> None:
        wrapped = tool_ledger.instrument("safe_read_file", lambda _args: {"status": "ok"})
        with tool_ledger.actor_scope(activity_emitter=parent_emitter) as parent:
            async def worker_child() -> tool_ledger.ToolAudit:
                # This is the Actor boundary used by WorkerRuntimeFactory.execution_context.
                with tool_ledger.actor_scope() as child:
                    await wrapped({"path": "report.md"})
                    return child

            child = await asyncio.create_task(worker_child())
            assert tool_ledger.current() is parent
            assert parent.actions == []
            assert child.actions and child.actions[0]["name"] == "safe_read_file"
        assert tool_ledger.current() is None

    asyncio.run(scenario())
    assert parent_events == [], "Worker tool activity leaked through the Coordinator emitter"


# ── standalone runner (offline: pytest may be unavailable) ──────────────────

if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
