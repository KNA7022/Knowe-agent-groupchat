from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backend import server as server_module
from backend.commercial_gateway import GatewayDenied, derive_provider_credential_purpose
from backend.config import CONFIG
from backend.server import KnoweServer


BROKER_TOKEN = "b" * 64
RUNTIME_TOKEN = "a" * 64
RUNTIME_HEADER = "X-Knowe-Runtime-Token"
BROKER_HEADER = "X-Knowe-Secret-Broker-Token"


def _lease_payload(*, suffix: str = "0", purpose: str | None = None,
                   ttl_seconds: int = 30) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    slot = "main"
    provider_id = "deepseek"
    endpoint = "https://api.deepseek.com/v1"
    transport = "openai_chat"
    return {
        "lease_id": f"lease_{suffix * 32}",
        "secret_ref": f"secret_{suffix * 32}",
        "slot": slot,
        "kind": "provider_credential",
        "purpose": purpose or derive_provider_credential_purpose(
            slot=slot, provider_id=provider_id, endpoint=endpoint, transport=transport,
        ),
        "audience": "provider_gateway",
        "provider_id": provider_id,
        "endpoint": endpoint,
        "transport": transport,
        "value": f"sk-private-{suffix}",
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z"),
    }


def _request(payload: bytes, *, runtime_token: str | None = RUNTIME_TOKEN,
             broker_token: str | None = BROKER_TOKEN, target: str = "/internal/credential-lease") -> bytes:
    headers = [
        "Host: 127.0.0.1",
        "Content-Type: application/json",
        f"Content-Length: {len(payload)}",
    ]
    if runtime_token is not None:
        headers.append(f"{RUNTIME_HEADER}: {runtime_token}")
    if broker_token is not None:
        headers.append(f"{BROKER_HEADER}: {broker_token}")
    return (
        f"POST {target} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n"
    ).encode("latin-1") + payload


async def _raw_http(port: int, request: bytes) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), timeout=3)
    writer.close()
    await writer.wait_closed()
    head, body = raw.split(b"\r\n\r\n", 1)
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    response_headers = {
        name.strip().lower(): value.strip()
        for line in lines[1:]
        if ":" in line
        for name, value in [line.split(":", 1)]
    }
    return status, response_headers, body


@pytest.fixture
async def lease_server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KNOWE_SECRET_BROKER_TOKEN", BROKER_TOKEN)
    monkeypatch.setattr(server_module, "CONFIG", replace(CONFIG, runtime_token=RUNTIME_TOKEN))
    server = KnoweServer(data_dir="")
    listener = await asyncio.start_server(server._health_conn, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        yield server, port
    finally:
        listener.close()
        await listener.wait_closed()
        server.secret_leases.close()


@pytest.mark.asyncio
async def test_secret_lease_ingress_requires_runtime_and_independent_broker_auth(
    lease_server, caplog: pytest.LogCaptureFixture,
) -> None:
    server, port = lease_server
    private_value = "sk-never-log-this-value"
    payload = _lease_payload()
    payload["value"] = private_value
    body = json.dumps(payload).encode("utf-8")

    status, headers, response = await _raw_http(
        port, _request(body, runtime_token=None, broker_token=BROKER_TOKEN),
    )
    assert status == 401 and b"runtime_auth_required" in response
    assert "access-control-allow-origin" not in headers

    for broker_token, target in (
        (None, "/internal/credential-lease"),
        ("c" * 64, "/internal/credential-lease"),
        (None, f"/internal/credential-lease?broker_token={BROKER_TOKEN}"),
    ):
        status, headers, response = await _raw_http(
            port, _request(body, broker_token=broker_token, target=target),
        )
        assert status == 403 and b"secret_broker_auth_required" in response
        assert "access-control-allow-origin" not in headers

    assert server.secret_leases.public_snapshot() == ()
    assert BROKER_TOKEN not in caplog.text
    assert private_value not in caplog.text


@pytest.mark.asyncio
async def test_secret_lease_ingress_rejects_reused_runtime_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KNOWE_SECRET_BROKER_TOKEN", RUNTIME_TOKEN)
    monkeypatch.setattr(server_module, "CONFIG", replace(CONFIG, runtime_token=RUNTIME_TOKEN))
    server = KnoweServer(data_dir="")
    listener = await asyncio.start_server(server._health_conn, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    try:
        body = json.dumps(_lease_payload(suffix="a")).encode("utf-8")
        status, _, response = await _raw_http(
            port, _request(body, broker_token=RUNTIME_TOKEN),
        )
        assert status == 403 and b"secret_broker_auth_required" in response
        assert server.secret_leases.public_snapshot() == ()
    finally:
        listener.close()
        await listener.wait_closed()
        server.secret_leases.close()


@pytest.mark.asyncio
async def test_credential_broker_enabled_on_non_loopback_listener_fails_before_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KNOWE_SECRET_BROKER_TOKEN", BROKER_TOKEN)
    monkeypatch.setattr(server_module, "CONFIG", replace(
        CONFIG, runtime_token=RUNTIME_TOKEN, health_host="0.0.0.0",
    ))
    server = KnoweServer(data_dir="")
    try:
        with pytest.raises(RuntimeError, match="loopback"):
            await server.run()
    finally:
        server.secret_leases.close()


@pytest.mark.asyncio
async def test_secret_lease_ingress_is_strict_private_and_purpose_bound(lease_server) -> None:
    server, port = lease_server
    payload = _lease_payload(suffix="1")
    status, headers, response = await _raw_http(
        port, _request(json.dumps(payload).encode("utf-8")),
    )
    assert status == 202
    assert headers.get("cache-control") == "no-store"
    assert "access-control-allow-origin" not in headers
    public = json.loads(response)
    assert public["ok"] is True
    assert payload["lease_id"] in json.dumps(public)
    assert payload["value"] not in json.dumps(public)

    with pytest.raises(GatewayDenied, match="purpose"):
        server.secret_leases.redeem(payload["lease_id"], purpose="provider:embeddings")
    assert server.secret_leases.redeem(
        payload["lease_id"], purpose=payload["purpose"],
    ) == payload["value"]

    status, _, response = await _raw_http(
        port,
        _request(
            json.dumps({"secret_ref": payload["secret_ref"]}).encode("utf-8"),
            target="/internal/credential-revoke",
        ),
    )
    assert status == 202 and b'"ok":true' in response
    with pytest.raises(GatewayDenied, match="unavailable"):
        server.secret_leases.redeem(
            payload["lease_id"], purpose=payload["purpose"],
        )


@pytest.mark.asyncio
async def test_secret_lease_ingress_rejects_schema_smuggling_and_policy_expansion(
    lease_server,
) -> None:
    server, port = lease_server
    attacks: list[bytes] = []
    extra = _lease_payload(suffix="2")
    extra["forward_to"] = "https://attacker.invalid"  # type: ignore[index]
    attacks.append(json.dumps(extra).encode("utf-8"))
    attacks.append(json.dumps(_lease_payload(
        suffix="3", purpose="diagnostics:export",
    )).encode("utf-8"))
    attacks.append(json.dumps(_lease_payload(
        suffix="4", ttl_seconds=3_600,
    )).encode("utf-8"))
    duplicate = _lease_payload(suffix="5")
    attacks.append((
        "{" + ",".join(
            f"{json.dumps(key)}:{json.dumps(value)}" for key, value in duplicate.items()
        ) + ',"purpose":"provider:embeddings"}'
    ).encode("utf-8"))

    for body in attacks:
        status, headers, response = await _raw_http(port, _request(body))
        assert status == 400 and b"invalid_secret_lease" in response
        assert "access-control-allow-origin" not in headers
    assert server.secret_leases.public_snapshot() == ()


@pytest.mark.asyncio
async def test_secret_lease_ingress_never_logs_private_body_on_handler_failure(
    lease_server, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    server, port = lease_server
    payload = _lease_payload(suffix="6")
    private_value = payload["value"]

    def fail_provision(**_kwargs: object) -> None:
        raise RuntimeError("forced-secret-handler-failure")

    monkeypatch.setattr(server.secret_leases, "provision", fail_provision)
    caplog.clear()
    status, _, response = await _raw_http(
        port, _request(json.dumps(payload).encode("utf-8")),
    )
    assert status == 500 and b"internal_error" in response
    assert "forced-secret-handler-failure" in caplog.text
    assert private_value not in caplog.text
    assert BROKER_TOKEN not in caplog.text
    assert RUNTIME_TOKEN not in caplog.text
