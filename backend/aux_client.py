# knowe v0.20 — Batch 4：辅助 LLM 调用
"""Auxiliary model facade backed exclusively by the commercial Provider Gateway.

Callers pass an opaque, main-derived runtime binding. This module never accepts a
plaintext credential, constructs an independent HTTP client, or uses an environment
fallback. Missing Task Runtime authority intentionally fails closed for legacy or
background auxiliary work.
"""

from __future__ import annotations

from typing import Any, Mapping

from . import runtime_settings
from .agent_runtime import ToolError
from .commercial_gateway import ProviderGatewayClient


async def chat(
    messages: list[dict[str, Any]],
    *,
    binding: Mapping[str, Any],
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    timeout_s: float = 40.0,
    what: str = "辅助模型",
) -> str:
    """Run one governed auxiliary chat and return provider-neutral message text."""

    selected = dict(binding) if isinstance(binding, Mapping) else {}
    provider = str(selected.get("provider") or "").strip()
    base_url = str(selected.get("base_url") or "").strip().rstrip("/")
    transport = str(selected.get("transport") or "openai_chat").strip()
    selected_model = str(model or selected.get("model") or "").strip()
    if not (
        provider and base_url and selected_model
        and selected.get("secret_ref") and selected.get("credential_purpose")
    ):
        raise ToolError(f"{what}的受管 Provider 绑定不可用")

    client = ProviderGatewayClient(
        base_url=base_url,
        api_key="",
        model=selected_model,
        provider=provider,
        transport=transport,
        timeout=timeout_s,
        max_retries=0,
        credential_provider=(
            lambda exact=selected: runtime_settings.resolve_provider_credential(exact)
        ),
    )
    try:
        data = await client.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        text = data["choices"][0]["message"]["content"]
        return str(text or "").strip()
    except ToolError:
        raise
    except Exception as exc:
        # Public auxiliary failures expose only a typed category, never response bodies.
        raise ToolError(f"{what}调用失败：{type(exc).__name__}") from None
    finally:
        await client.aclose()


__all__ = ["chat"]
