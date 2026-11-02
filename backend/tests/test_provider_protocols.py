"""Offline protocol wire fixtures: no network and no credentials."""
import json
import pytest
from backend.knowe_core.provider_protocols import request_body, normalize_response, NativeStream

@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages", "codex_responses"])
def test_distinct_paths_and_tool_history(protocol):
    body = {"model": "qwen3.8", "stream": True, "max_tokens": 120, "messages": [
        {"role": "system", "content": "instructions"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call1", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call1", "content": "first"},
        {"role": "tool", "tool_call_id": "call2", "content": "second"}],
        "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]}
    wire = request_body(body, protocol)
    assert wire["model"] == "qwen3.8"
    if protocol == "anthropic_messages":
        assert wire["max_tokens"] == 120
        assert len(wire["messages"][-1]["content"]) == 2
        assert wire["tools"][0]["input_schema"] == {"type": "object"}
    elif protocol == "codex_responses":
        assert wire["store"] is False and wire["max_output_tokens"] == 120
        assert wire["input"][0]["type"] == "function_call"
    else:
        assert wire is body

def test_responses_stream_contiguous_tool_indices_and_reasoning():
    stream = NativeStream("codex_responses")
    assert stream.events({"type": "response.reasoning_summary_text.delta", "delta": "consider"})[0]["type"] == "reasoning_delta"
    start = stream.events({"type": "response.output_item.added", "output_index": 3, "item": {"type": "function_call", "call_id": "c", "name": "lookup"}})
    assert start[0]["tool_call"]["index"] == 0
    assert stream.events({"type": "response.function_call_arguments.delta", "output_index": 3, "delta": "{}"})[0]["tool_call"]["index"] == 0
    assert stream.events({"type": "response.completed", "response": {"usage": {"input_tokens": 12}}})[-1]["reason"] == "tool_calls"
    assert stream.events({"type": "response.completed"}) == []

def test_messages_usage_is_cumulative_and_sparse_content_indices_normalize():
    stream = NativeStream("anthropic_messages")
    stream.events({"type": "message_start", "message": {"usage": {"input_tokens": 30}}})
    assert stream.events({"type": "content_block_start", "index": 4, "content_block": {"type": "tool_use", "id": "c", "name": "lookup"}})[0]["tool_call"]["index"] == 0
    end = stream.events({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}})
    assert end[0]["usage"] == {"input_tokens": 30, "output_tokens": 5}

@pytest.mark.parametrize("payload", [{"status": "failed", "error": {"message": "secret"}}, {"status": "in_progress"}])
def test_failed_responses_are_not_empty_success(payload):
    with pytest.raises(ValueError):
        normalize_response(payload, "codex_responses")

def test_multimodal_messages_and_responses_images():
    body = {"model": "qwen3.8", "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}]}]}
    assert request_body(body, "anthropic_messages")["messages"][0]["content"][0]["source"]["media_type"] == "image/png"
    assert request_body(body, "codex_responses")["input"][0]["content"][0]["type"] == "input_image"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol,path", [("openai_chat", "/v1/chat/completions"), ("anthropic_messages", "/v1/messages"), ("codex_responses", "/v1/responses")])
async def test_real_http_transport_native_path_headers_response_and_stream(protocol, path):
    import httpx
    from backend.knowe_core.provider_client import ProviderClient
    requests = []
    def handler(request):
        requests.append(request)
        wire = json.loads(request.content)
        if wire.get("stream"):
            events = ([{"choices": [{"delta": {"content": "fixture"}, "finish_reason": "stop"}]}] if protocol == "openai_chat" else
                [{"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "fixture"}}, {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}] if protocol == "anthropic_messages" else
                [{"type": "response.output_text.delta", "delta": "fixture"}, {"type": "response.completed", "response": {"usage": {"output_tokens": 1}}}])
            raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
            if protocol == "openai_chat":
                raw += "data: [DONE]\n\n"
            return httpx.Response(200, content=raw.encode("utf-8"), headers={"content-type": "text/event-stream"})
        native = ({"choices": [{"message": {"content": "fixture"}}]} if protocol == "openai_chat" else
            {"type": "message", "content": [{"type": "text", "text": "fixture"}]} if protocol == "anthropic_messages" else
            {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "fixture"}]}]})
        return httpx.Response(200, json=native)
    client = ProviderClient(base_url="https://fixture.invalid/v1", api_key="fixture-key", model="qwen3.8", transport=protocol,
        _gateway_managed=True, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        response = await client.chat([{"role": "user", "content": "fixture"}], max_tokens=10)
        assert response["choices"][0]["message"]["content"] == "fixture"
        events = [event async for event in client.chat_stream([{"role": "user", "content": "fixture"}], max_tokens=10)]
        assert any(event.get("content") == "fixture" for event in events)
        assert any(event["type"] == "finish" for event in events)
        assert all(request.url.path == path for request in requests)
        if protocol == "anthropic_messages":
            assert requests[0].headers["x-api-key"] == "fixture-key"
            assert "anthropic-version" in requests[0].headers
    finally:
        await client.aclose()


def test_production_unknown_model_tool_and_vision_fail_closed():
    from backend.commercial_gateway import production_model_capability_registry, GatewayDenied
    registry = production_model_capability_registry()
    for capability in ("tools", "vision"):
        with pytest.raises(GatewayDenied, match="not verified"):
            registry.authorize("alibaba", model="unknown-not-verified", endpoint="https://dashscope.aliyuncs.com", transport="openai_chat", required_capabilities={capability})
    assert registry.authorize("alibaba", model="qwen3.8-max", endpoint="https://dashscope.aliyuncs.com", transport="openai_chat", required_capabilities={"tools", "vision"})


def test_verified_model_limits_clamp_output_and_reject_context_overflow():
    from backend.commercial_gateway import _enforce_model_limits, verified_model_limits, GatewayDenied
    assert _enforce_model_limits("alibaba", "qwen3.8-max", "small", 200_000) == 131_072
    assert _enforce_model_limits("openai-api", "gpt-4o-mini", "small", 100_000) == 16_384
    with pytest.raises(GatewayDenied, match="context limits"):
        _enforce_model_limits("alibaba", "qwen3.8-max", "x" * 900_000, 131_072)
    with pytest.raises(GatewayDenied, match="context limits"):
        _enforce_model_limits("openai-api", "gpt-4o-mini", "中" * 45_000, 100)
    assert verified_model_limits("alibaba", "qwen3.8-max")["verified"]
    assert not verified_model_limits("alibaba", "unknown")["verified"]
    assert _enforce_model_limits("alibaba", "unknown", "small", 4096) == 4096


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai_chat", "anthropic_messages", "codex_responses"])
@pytest.mark.parametrize("stream", [False, True])
async def test_gateway_reserved_default_output_cap_reaches_nonmock_wire(protocol, stream, tmp_path):
    """Actual gateway/runtime plus non-Mock transport; inert fixture never opens sockets."""
    import httpx
    from backend.commercial_gateway import ProviderGatewayClient
    from backend.groups import GroupRepository
    from backend.task_runtime.service import TaskRuntimeService
    from backend.runtime_settings import probe_authority, _finish_probe_run
    bodies = []
    class InertWireTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            bodies.append(json.loads(request.content))
            if stream:
                event = ({"choices": [{"delta": {"content": "fixture"}, "finish_reason": "stop"}]} if protocol == "openai_chat" else
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}} if protocol == "anthropic_messages" else
                    {"type": "response.completed", "response": {"usage": {"output_tokens": 1}}})
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="data: " + json.dumps(event) + "\n\n")
            native = ({"choices": [{"message": {"content": "fixture"}}]} if protocol == "openai_chat" else
                {"type": "message", "content": [{"type": "text", "text": "fixture"}]} if protocol == "anthropic_messages" else
                {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "fixture"}]}]})
            return httpx.Response(200, json=native)
    groups = GroupRepository(tmp_path)
    runtime = TaskRuntimeService(tmp_path, groups)
    authority, run_id = probe_authority(groups, runtime)
    client = ProviderGatewayClient(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="fixture", model="qwen3.8-max", provider="alibaba", transport=protocol,
        authority_provider=lambda: authority,
        client_factory=lambda: httpx.AsyncClient(transport=InertWireTransport()))
    try:
        messages = [{"role": "user", "content": "Synthetic fixture"}]
        if stream:
            async for _ in client.chat_stream(messages):
                pass
        else:
            await client.chat(messages)
        key = "max_output_tokens" if protocol == "codex_responses" else "max_tokens"
        assert bodies[0][key] == 4096
    finally:
        await client.aclose()
        _finish_probe_run(groups, runtime, run_id)
        runtime.close()


def test_exact_model_operation_declarations_do_not_grant_other_operations():
    from backend.commercial_gateway import production_model_capability_registry, GatewayDenied
    registry=production_model_capability_registry()
    for model in ('deepseek-chat','deepseek-flash','deepseek-v4-pro'):
        registry.authorize('deepseek',model=model,endpoint='https://api.deepseek.com',transport='openai_chat',required_capabilities={'tools'})
    registry.authorize('alibaba',model='text-embedding-v4',endpoint='https://dashscope.aliyuncs.com',transport='openai_chat',required_capabilities={'embedding'})
    for model, operation in [('text-embedding-v4','tools'),('qwen3.8-max','embedding'),('unknown-embedding','embedding')]:
        with pytest.raises(GatewayDenied):
            registry.authorize('alibaba',model=model,endpoint='https://dashscope.aliyuncs.com',transport='openai_chat',required_capabilities={operation})
