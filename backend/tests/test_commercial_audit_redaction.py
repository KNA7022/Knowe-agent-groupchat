from __future__ import annotations

import httpx
import pytest

from backend import tool_ledger
from backend.knowe_core.provider_client import ProviderClient
from knowe_core.errors import ProviderAuthError


@pytest.mark.asyncio
async def test_legacy_tool_audit_never_keeps_raw_args_results_or_errors() -> None:
    phone = "13800138000"
    credential = "sk-super-secret-audit-value"

    async def handler(args: dict[str, str]) -> dict[str, object]:
        return {
            "status": "error",
            "message": f"student phone={phone}",
            "api_key": credential,
            "echo": args,
        }

    wrapped = tool_ledger.instrument("web_search", handler)
    with tool_ledger.actor_scope() as audit:
        await wrapped({"query": f"find {phone}", "api_key": credential})

    serialized = str(audit.actions)
    assert phone not in serialized
    assert credential not in serialized
    assert audit.actions[0]["name"] == "web_search"
    assert "args_digest" in audit.actions[0]
    assert "result_digest" in audit.actions[0]


@pytest.mark.asyncio
async def test_provider_error_diagnostics_redact_secret_and_student_pii() -> None:
    phone = "13800138000"
    credential = "sk-super-secret-provider-value"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": f"Bearer {credential}; phone {phone}"}},
        )

    client = ProviderClient(
        base_url="https://provider.example/v1",
        api_key="test-only",
        model="model",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderAuthError) as caught:
        await client.chat([{"role": "user", "content": "hello"}])
    diagnostic = f"{caught.value} {caught.value.response_body}"
    assert credential not in diagnostic
    assert phone not in diagnostic
    await client.aclose()


def test_provider_transport_exception_detail_is_safe_for_retry_logs() -> None:
    detail = ProviderClient._exception_detail(RuntimeError(
        "Authorization: Bearer transport-secret 学号：2026123456 手机号 13800138000",
    ))

    assert "transport-secret" not in detail
    assert "2026123456" not in detail
    assert "13800138000" not in detail
    assert "[REDACTED" in detail
