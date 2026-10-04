"""v2.2 single Worker prompt and fixed tool-boundary tests."""
from __future__ import annotations

from pathlib import Path
import asyncio
from types import SimpleNamespace

import pytest

from backend import tools_knowe
from backend.runtime import WORKER_TOOL_NAMES

PROMPT = Path(tools_knowe.__file__).with_name("worker_prompt.md")
SOUL = Path(tools_knowe.__file__).parent / "souls" / "worker.txt"


class FakeEngine:
    project_id = "project_20260723002000"
    workspace_root = Path("/tmp/ws")
    internal_workspace = Path("/tmp/int")


def worker_registry_names() -> tuple[str, ...]:
    return tuple(tools_knowe.build_worker_registry(FakeEngine(), "be_1").names())


def test_registry_is_the_exact_fixed_19_without_completion_pseudotools() -> None:
    names = worker_registry_names()
    assert names == WORKER_TOOL_NAMES
    assert len(names) == 19
    assert "submit_report" not in names
    assert "speak" not in names
    assert "read_result_ref" not in names


def test_only_one_canonical_worker_prompt_path_exists() -> None:
    assert PROMPT.is_file()
    backend_root = Path(tools_knowe.__file__).resolve().parents[1]
    assert not (backend_root / "knowe_prompts").exists()
    assert not (Path(tools_knowe.__file__).parent / "knowe_prompts").exists()


def test_prompt_requires_native_provider_tool_calls() -> None:
    prompt = PROMPT.read_text("utf-8")
    assert "Provider's native tool-call interface" in prompt
    assert "<tool_call>" in prompt
    assert "Never print, imitate, or wrap" in prompt


def test_prompt_describes_fixed_visibility_and_stable_unavailability() -> None:
    prompt = PROMPT.read_text("utf-8")
    assert "same 19 tools remain available on every turn" in prompt
    assert "service can be unavailable" in prompt
    assert "Do not invent another tool" in prompt


def test_prompt_uses_same_tool_continuation_not_result_refs() -> None:
    prompt = PROMPT.read_text("utf-8")
    assert "continue with the same tool" in prompt
    assert "offset" in prompt and "limit" in prompt
    assert "no secondary result-reference protocol" in prompt


def test_prompt_requires_verified_file_and_delete_effects() -> None:
    prompt = PROMPT.read_text("utf-8")
    assert "verified size and SHA-256 digest" in prompt
    assert "verified absent" in prompt
    assert "do not claim an effect" in prompt


def test_prompt_keeps_final_response_user_facing() -> None:
    prompt = PROMPT.read_text("utf-8")
    assert "concise, factual result" in prompt
    assert "project-relative deliverable paths" in prompt
    assert "Do not paste full tool results" in prompt


@pytest.mark.parametrize(
    "retired",
    [
        "submit_report",
        "调用 speak",
        "ContextBundleV2",
        "capability lease",
        "surface plan",
        "result store",
        "read_result_ref",
    ],
)
def test_retired_control_language_stays_out_of_active_prompt(retired: str) -> None:
    assert retired not in PROMPT.read_text("utf-8")


def test_retained_soul_points_to_canonical_prompt_and_does_not_define_another_protocol() -> None:
    soul = SOUL.read_text("utf-8")
    assert "worker_prompt.md" in soul
    assert "must not define a second protocol" in soul
    assert "Provider-native tool calls" in soul


def test_canonical_prompt_defines_language_policy() -> None:
    prompt = PROMPT.read_text("utf-8")
    assert "explicit output-language requirement" in prompt
    assert "active system language" in prompt
    assert "Preserve project paths, code, commands" in prompt


def test_language_rule_exists_only_in_canonical_prompt_not_runtime() -> None:
    prompt = PROMPT.read_text("utf-8")
    runtime = (Path(tools_knowe.__file__).parent / "runtime.py").read_text("utf-8")
    assert "## Response language" in prompt and "active system language" in prompt
    for detector in ("detect_language", "language_regex", "translate_final"):
        assert detector not in runtime


@pytest.mark.parametrize("language, label", [("zh", "Chinese (zh)"), ("en", "English (en)")])
@pytest.mark.parametrize("override", ["", "请用中文完成分析", "Please answer in English",
                                      "Create multilingual English / Chinese deliverables."])
def test_complete_worker_prompt_uses_configured_language_and_preserves_override(
    tmp_path, monkeypatch, language, label, override,
) -> None:
    from backend import runtime_settings
    from backend.runtime import TaskEnvelope, RuntimeStatus
    from backend.worker_gateway_runtime import WorkerRuntimeFactory
    from backend.commercial_gateway import ProviderGatewayClient
    from backend.knowe_adapters.model_adapter import ProviderAdapterConfig
    from tests.support_authority import AgentProviderAuthority
    import httpx

    monkeypatch.setattr(runtime_settings, "language", lambda: language)
    captured = []

    def inert_transport(request):
        import json
        captured.extend(json.loads(request.content)["messages"])
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "原文 / raw output"}, "finish_reason": "stop"}]})

    owner = AgentProviderAuthority(tmp_path / "authority")
    client = ProviderGatewayClient(base_url=owner.endpoint, api_key="", model=owner.model,
        provider=owner.provider_id, transport=owner.transport,
        credential_provider=lambda: runtime_settings.resolve_provider_credential(owner.binding),
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(inert_transport)))
    agent = SimpleNamespace(_client=client, ephemeral_system_prompt="Stable Worker identity: be_1")

    envelope = TaskEnvelope(task_id="language-task", project_id="language-project",
                            worker_id="be_1", goal="中英混合 task " + override,
                            attempt_id="language-attempt", scope_root=str(tmp_path),
                            metadata={"agent_memory": "Mixed 历史"})
    engine = SimpleNamespace(workspace_root=tmp_path, internal_workspace=tmp_path / "internal")
    runtime = WorkerRuntimeFactory().create(engine, "be_1", envelope, agent)
    runtime.model.config = ProviderAdapterConfig(stream=False)

    async def scenario():
        try:
            return await runtime.run(envelope)
        finally:
            await client.aclose()
    try:
        with owner.active():
            result = asyncio.run(scenario())
        assert owner.runtime.store.get_run(owner.run.run_id).budget_usage.model_turns == 1
    finally:
        owner.close()
    system = captured[0]["content"]
    assert f"Active system language: {label}" in system
    assert "Otherwise, reply in English (the system's active language)" not in system
    assert "Stable Worker identity: be_1" in system
    assert "Follow an explicit output-language requirement" in system
    assert "requested language mix" in system
    assert "Preserve project paths, code, commands" in system
    assert override in captured[-1]["content"]
    assert result.status is RuntimeStatus.SUCCEEDED
    assert result.final_text == "原文 / raw output"
