"""Real gateway SSE probe; caller exclusively owns credentials and budget transport.

No automatic CLI/network entry point. Import and await run_protocol_probe(binding,
credential, client_factory). Factory takes no arguments and returns AsyncClient.
Reports contain synthetic output statistics only, never credentials/raw exceptions.
"""
from __future__ import annotations

import tempfile
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from backend.commercial_gateway import ProviderGatewayClient
from backend.groups import GroupRepository
from backend.runtime_settings import probe_authority, _finish_probe_run
from backend.task_runtime.service import TaskRuntimeService
from backend.knowe_core.provider_client import normalize_usage_buckets


async def run_protocol_probe(binding, credential, client_factory):
    """Three sequential actual SSE requests, no retries, max128 output tokens each.

binding must explicitly contain provider/model/base_url; only the human-confirmed
Alibaba workspace is accepted. Caller factory must enforce shared paid-call budget.
Failures are returned honestly and never replaced with simulated success.
"""
    if (binding.get("provider") != "alibaba" or not binding.get("model")
            or not callable(client_factory) or not credential):
        raise ValueError("explicit binding, credential and bounded client factory required")
    host = "ws-1y8ivqouamht78in.cn-beijing.maas.aliyuncs.com"
    parsed = urlsplit(binding.get("base_url", ""))
    if (parsed.scheme != "https" or parsed.netloc != host or parsed.query
            or parsed.fragment or parsed.path.rstrip("/") != "/compatible-mode/v1"):
        raise ValueError("probe requires the exact approved Alibaba workspace binding")
    reports = []
    for protocol, base, path in (
        ("openai_chat", "/compatible-mode/v1", "/chat/completions"),
        ("anthropic_messages", "/apps/anthropic/v1", "/messages"),
        ("codex_responses", "/compatible-mode/v1", "/responses"),
    ):
        report = {"protocol": protocol, "path": base + path, "pass": False,
                  "scope": "synthetic real ProviderGateway SSE, not desktop end-to-end"}
        with tempfile.TemporaryDirectory(prefix="studycrew-protocol-probe-") as directory:
            groups = GroupRepository(directory)
            runtime = TaskRuntimeService(directory, groups)
            gateway = None
            run_id = ""
            try:
                authority, run_id = probe_authority(groups, runtime)
                gateway = ProviderGatewayClient(base_url="https://" + host + base,
                    api_key="", credential_provider=lambda: credential,
                    provider=binding["provider"], model=binding["model"],
                    transport=protocol, authority_provider=lambda: authority,
                    max_retries=0, client_factory=client_factory)
                counts = {}; usage = None; finish = None; text_size = 0
                async for event in gateway.chat_stream(
                    [{"role": "user", "content": "Synthetic connectivity test. Reply exactly READY."}],
                    max_tokens=128, temperature=0):
                    kind = event.get("type", "unknown")
                    counts[kind] = counts.get(kind, 0) + 1
                    if kind == "usage":
                        usage = event.get("usage")
                    if kind == "finish":
                        finish = event.get("reason")
                    if kind == "delta":
                        text_size += len(event.get("content", ""))
                buckets = normalize_usage_buckets(usage)
                report.update(event_counts=counts, usage=usage, usage_buckets=buckets,
                              finish_reason=finish, text_characters=text_size)
                report["pass"] = bool(text_size and finish in {"stop", "end_turn"}
                                      and buckets and buckets["cache_hit_input"] + buckets["cache_miss_input"] > 0
                                      and buckets["output"] > 0)
                if not report["pass"]:
                    report["error"] = "missing_text_terminal_finish_or_real_usage"
            except Exception as exc:
                report["error"] = type(exc).__name__
            finally:
                if gateway is not None:
                    await gateway.aclose()
                if run_id:
                    _finish_probe_run(groups, runtime, run_id)
                runtime.close()
        reports.append(report)
    return reports
