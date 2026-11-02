"""Wire protocol conversion. Input and output remain chat-completions shaped.

References: platform.claude.com/docs/en/build-with-claude/streaming and
developers.openai.com/api/docs/guides/streaming-responses.
No credentials or network access belong in this module.
"""
from __future__ import annotations

import json
from typing import Any

PROTOCOL_PATHS = {"openai_chat": "/chat/completions", "anthropic_messages": "/messages", "codex_responses": "/responses"}


def _parts(content: Any, protocol: str, *, assistant: bool = False) -> list[dict]:
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    result = []
    for part in content or []:
        kind = part.get("type")
        if kind == "text":
            result.append({"type": "text" if protocol == "anthropic_messages" else ("output_text" if assistant else "input_text"), "text": part["text"]})
        elif kind == "image_url":
            image = part["image_url"]
            url = image if isinstance(image, str) else image["url"]
            if protocol == "anthropic_messages":
                if url.startswith("data:"):
                    header, data = url.split(",", 1)
                    if not header.endswith(";base64"):
                        raise ValueError("image data must be base64")
                    source = {"type": "base64", "media_type": header[5:-7], "data": data}
                else:
                    source = {"type": "url", "url": url}
                result.append({"type": "image", "source": source})
            else:
                result.append({"type": "input_image", "image_url": url, "detail": image.get("detail", "auto") if isinstance(image, dict) else "auto"})
        else:
            raise ValueError(f"unsupported message content type: {kind}")
    return result


def request_body(body: dict, protocol: str) -> dict:
    if protocol not in PROTOCOL_PATHS:
        raise ValueError("unsupported provider transport")
    if protocol == "openai_chat":
        return body
    # Only known optional controls cross protocols; native body fields cannot override
    # model, input, tools or output budget already pinned by the gateway.
    result = {k: body[k] for k in ("model", "stream", "temperature", "top_p") if k in body}
    messages, system = [], []
    for message in body["messages"]:
        role = message["role"]
        if role in ("system", "developer"):
            system.extend(_parts(message.get("content"), "anthropic_messages"))
            continue
        if role == "tool":
            if protocol == "anthropic_messages":
                messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": message.get("content") or ""}]})
            else:
                messages.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": message.get("content") or ""})
            continue
        parts = _parts(message.get("content"), protocol, assistant=role == "assistant")
        calls = message.get("tool_calls") or []
        if protocol == "anthropic_messages":
            for call in calls:
                function = call["function"]
                parts.append({"type": "tool_use", "id": call["id"], "name": function["name"], "input": json.loads(function.get("arguments") or "{}")})
            if parts:
                messages.append({"role": role, "content": parts})
        else:
            if parts:
                messages.append({"role": role, "content": parts})
            for call in calls:
                messages.append({"type": "function_call", "call_id": call["id"], "name": call["function"]["name"], "arguments": call["function"].get("arguments") or "{}"})
    if protocol == "anthropic_messages":
        # Messages requires max_tokens. Never introduce a provider default above the
        # caller's bound. Gateway supplies the same explicit default for accounting.
        result.update(messages=messages, max_tokens=body.get("max_tokens", 4096))
        if system:
            result["system"] = system
        # Anthropic requires alternating logical turns; consecutive user tool results
        # belong in a single user message.
        merged = []
        for message in messages:
            if merged and merged[-1]["role"] == message["role"]:
                merged[-1]["content"].extend(message["content"])
            else:
                merged.append(message)
        result["messages"] = merged
        if body.get("tools"):
            result["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""), "input_schema": t["function"].get("parameters", {"type": "object", "properties": {}})} for t in body["tools"]]
    else:
        result.update(input=messages, store=False)
        if system:
            result["instructions"] = "\n".join(p["text"] for p in system)
        if "max_tokens" in body:
            result["max_output_tokens"] = body["max_tokens"]
        if body.get("tools"):
            result["tools"] = [{"type": "function", **t["function"]} for t in body["tools"]]
        if "reasoning" in body:
            result["reasoning"] = body["reasoning"]
        elif "reasoning_effort" in body:
            result["reasoning"] = {"effort": body["reasoning_effort"]}
        if "parallel_tool_calls" in body:
            result["parallel_tool_calls"] = bool(body["parallel_tool_calls"])
    choice = body.get("tool_choice")
    if choice is not None:
        if protocol == "anthropic_messages":
            result["tool_choice"] = {"type": {"required": "any"}.get(choice, choice)} if isinstance(choice, str) else {"type": "tool", "name": choice["function"]["name"]}
        else:
            result["tool_choice"] = choice if isinstance(choice, str) else {"type": "function", "name": choice["function"]["name"]}
    return result


def normalize_response(data: dict, protocol: str) -> dict:
    if protocol == "openai_chat":
        return data
    if protocol not in PROTOCOL_PATHS or not isinstance(data, dict):
        raise ValueError("invalid provider response")
    if protocol == "anthropic_messages" and data.get("type") != "message":
        raise ValueError("invalid messages response")
    if protocol == "codex_responses" and data.get("status") not in ("completed", "incomplete"):
        raise ValueError("response did not complete")
    text, calls = [], []
    items = data.get("content", []) if protocol == "anthropic_messages" else data.get("output", [])
    for item in items:
        kind = item.get("type")
        if kind == "text":
            text.append(item.get("text", ""))
        elif kind == "message":
            text.extend(p.get("text", "") for p in item.get("content", []) if p.get("type") == "output_text")
        elif kind in ("tool_use", "function_call"):
            calls.append({"id": item.get("call_id", item.get("id")), "type": "function", "function": {"name": item["name"], "arguments": json.dumps(item.get("input", {}), ensure_ascii=False) if kind == "tool_use" else item.get("arguments", "{}")}})
    message = {"role": "assistant", "content": "".join(text)}
    if calls:
        message["tool_calls"] = calls
    reason = "tool_calls" if calls else "stop"
    if data.get("stop_reason") == "max_tokens" or data.get("status") == "incomplete":
        reason = "length"
    if data.get("error") or data.get("status") in ("failed", "cancelled"):
        raise ValueError("provider response failed")
    return {"id": data.get("id"), "choices": [{"message": message, "finish_reason": reason}], "usage": data.get("usage", {})}


class NativeStream:
    """Per-request state, especially cumulative Anthropic usage and tool indices."""
    def __init__(self, protocol: str):
        self.protocol = protocol
        self.usage = {}
        self.has_tools = False
        self.tool_indices = {}
        self.finished = False

    def events(self, data: dict) -> list[dict]:
        if self.finished:
            return []
        kind = data.get("type", "")
        if data.get("error") or kind in ("error", "response.failed"):
            return [{"type": "error", "message": "Provider stream reported an error"}]
        if self.protocol == "anthropic_messages":
            if kind == "message_start":
                self.usage.update(data.get("message", {}).get("usage", {}))
            elif kind == "content_block_start":
                block = data["content_block"]
                if block.get("type") == "tool_use":
                    self.has_tools = True
                    self.tool_indices[data["index"]] = len(self.tool_indices)
                    return [{"type": "tool_call", "tool_call": {"index": self.tool_indices[data["index"]], "id": block["id"], "type": "function", "function": {"name": block["name"], "arguments": ""}}}]
                if block.get("type") == "text" and block.get("text"):
                    return [{"type": "delta", "content": block["text"]}]
            elif kind == "content_block_delta":
                delta = data["delta"]
                if delta["type"] == "text_delta":
                    return [{"type": "delta", "content": delta["text"]}]
                if delta["type"] == "thinking_delta":
                    return [{"type": "reasoning_delta", "content": delta["thinking"]}]
                if delta["type"] == "input_json_delta":
                    return [{"type": "tool_call", "tool_call": {"index": self.tool_indices[data["index"]], "function": {"arguments": delta["partial_json"]}}}]
            elif kind == "message_delta":
                self.usage.update(data.get("usage", {}))
                reason = data.get("delta", {}).get("stop_reason")
                if not reason:
                    return [{"type": "usage", "usage": dict(self.usage)}]
                self.finished = True
                return [{"type": "usage", "usage": dict(self.usage)}, {"type": "finish", "reason": "length" if reason == "max_tokens" else "tool_calls" if reason == "tool_use" else "stop"}]
        else:
            if kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                return [{"type": "reasoning_delta", "content": data["delta"]}]
            if kind == "response.output_text.delta":
                return [{"type": "delta", "content": data["delta"]}]
            if kind == "response.output_item.added" and data.get("item", {}).get("type") == "function_call":
                self.has_tools = True
                item = data["item"]
                self.tool_indices[data["output_index"]] = len(self.tool_indices)
                return [{"type": "tool_call", "tool_call": {"index": self.tool_indices[data["output_index"]], "id": item["call_id"], "type": "function", "function": {"name": item["name"], "arguments": item.get("arguments", "")}}}]
            if kind == "response.function_call_arguments.delta":
                return [{"type": "tool_call", "tool_call": {"index": self.tool_indices[data["output_index"]], "function": {"arguments": data["delta"]}}}]
            if kind in ("response.completed", "response.incomplete"):
                self.finished = True
                return [{"type": "usage", "usage": data.get("response", {}).get("usage", {})}, {"type": "finish", "reason": "length" if kind == "response.incomplete" else "tool_calls" if self.has_tools else "stop"}]
        return []
