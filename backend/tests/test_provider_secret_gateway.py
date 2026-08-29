from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend import runtime_settings
from backend.knowe_core.provider_client import ProviderClient


@pytest.fixture(autouse=True)
def _reset_runtime_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from backend.config import CONFIG

    old_data_dir = CONFIG.data_dir
    object.__setattr__(CONFIG, "data_dir", str(tmp_path))
    monkeypatch.setattr(runtime_settings, "_state", runtime_settings._default_state())
    monkeypatch.setattr(runtime_settings, "_loaded", False)
    try:
        yield
    finally:
        object.__setattr__(CONFIG, "data_dir", old_data_dir)


def _leased_binding() -> dict[str, str]:
    return {
        "provider": "openai-api",
        "model": "gpt-test",
        "base_url": "https://provider.example/v1",
        "transport": "openai_chat",
        "secret_ref": "secret_" + "a" * 32,
        "credential_purpose": "provider:main",
    }


def test_settings_persist_only_secret_handle_and_quarantine_legacy_plaintext(
    tmp_path: Path,
) -> None:
    binding = _leased_binding()
    runtime_settings.apply({"main_model": binding})

    persisted = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert "sk-plaintext-must-never-persist" not in persisted
    assert '"api_key"' not in persisted
    assert binding["secret_ref"] in persisted

    # A pre-upgrade file is untrusted persisted input.  Loading it must remove the
    # credential instead of retaining plaintext in Python memory or writing it back.
    legacy = runtime_settings._default_state()
    legacy["main_model"] = {
        **binding,
        "api_key": "sk-plaintext-must-never-persist",
    }
    (tmp_path / "settings.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8",
    )
    runtime_settings._state = runtime_settings._default_state()
    runtime_settings._loaded = False
    loaded = runtime_settings.snapshot()
    assert "sk-plaintext-must-never-persist" not in str(loaded)
    assert loaded["main_model"]["secret_ref"] == binding["secret_ref"]
    migrated = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert "sk-plaintext-must-never-persist" not in migrated
    assert '"api_key"' not in migrated


def test_production_provider_egress_has_no_plaintext_environment_fallback() -> None:
    """Legacy provider paths must use a lease or fail closed, never DEEPSEEK_API_KEY."""

    backend_root = Path(__file__).resolve().parents[1]
    production_paths = (
        backend_root / "engine.py",
        backend_root / "server.py",
        backend_root / "knowledge_graph.py",
        backend_root / "memory_manager.py",
        backend_root / "tools_knowe.py",
        backend_root / "agents" / "zinnia.py",
        backend_root / "agents" / "deepseek.py",
        backend_root / "knowe_adapters" / "model_adapter.py",
    )
    forbidden = (
        "CONFIG.deepseek_api_key",
        'getattr(CONFIG, "deepseek_api_key"',
        "getattr(CONFIG, 'deepseek_api_key'",
        'api_key=str(getattr(source, "api_key"',
        "api_key=str(getattr(source, 'api_key'",
    )
    violations = [
        f"{path.relative_to(backend_root)}:{needle}"
        for path in production_paths
        for needle in forbidden
        if needle in path.read_text(encoding="utf-8")
    ]
    assert violations == []


@pytest.mark.asyncio
async def test_provider_resolves_credential_at_request_boundary_without_retaining_it() -> None:
    secret = "sk-request-scoped-secret"
    calls = 0

    def resolve() -> str:
        nonlocal calls
        calls += 1
        return secret

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {secret}"
        return httpx.Response(200, json={"choices": []})

    client = ProviderClient(
        base_url="https://provider.example/v1",
        api_key="",
        model="gpt-test",
        credential_provider=resolve,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    serialized_state = repr(client.__dict__)
    assert secret not in serialized_state
    assert "Authorization" not in client._headers

    await client.chat([{"role": "user", "content": "hello"}])
    assert calls == 1
    assert secret not in repr(client.__dict__)
    await client.aclose()


@pytest.mark.asyncio
async def test_provider_fails_before_network_when_credential_lease_is_unavailable() -> None:
    network_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(200, json={})

    client = ProviderClient(
        base_url="https://provider.example/v1",
        api_key="",
        model="gpt-test",
        credential_provider=lambda: "",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(Exception, match="credential lease|credential.*unavailable"):
        await client.chat([{"role": "user", "content": "hello"}])
    assert network_calls == 0
    await client.aclose()
