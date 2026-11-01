"""Offline transport diagnostics; run with --noconftest (no app or .env loading)."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest

from growthpilot.creative import canva_mcp


@pytest.mark.parametrize("status", [400, 403, 429, 500])
def test_real_sdk_preserves_http_error_before_generic_mcp_error(monkeypatch, caplog, status):
    # Import before replacing the client constructor used by the real MCP SDK.
    import mcp.client.streamable_http  # noqa: F401

    original = httpx2.AsyncClient
    body = ('{"error":"permission denied", "access_token":"access-value", '
            '"refresh_token":"refresh-value", "client_secret":"secret-value", '
            '"code":"oauth-value", "detail":"Bearer bearer-value", '
            '"link":"https://cdn.example/file?signature=signed-value"}')
    clients = []

    def factory(**kwargs):
        client = original(transport=httpx2.MockTransport(
            lambda request: httpx2.Response(status, text=body, headers={"content-type": "application/json"})
        ), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx2, "AsyncClient", factory)

    async def run():
        async with canva_mcp.connected_client(
            access_token="access-value", refresh_token="refresh-value", client_id="client",
            client_secret="secret-value", persist_tokens=lambda *_: None, timeout=2,
        ):
            pytest.fail("failed handshake must not connect")

    with caplog.at_level(logging.DEBUG), pytest.raises(canva_mcp.CanvaMCPError) as caught:
        asyncio.run(asyncio.wait_for(run(), timeout=5))
    error = caught.value
    assert error.http_status == status
    assert f"HTTP {status}" in str(error)
    assert "permission denied" in str(error)
    assert "HTTP status unavailable" not in str(error)
    logging.getLogger(__name__).error("%s", error)
    diagnostics = str(error) + error.redacted_message + caplog.text
    for secret in ("access-value", "refresh-value", "secret-value", "oauth-value",
                   "bearer-value", "signed-value"):
        assert secret not in diagnostics
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("body", [
    '{"access_token": "json secret with spaces", "code": "oauth secret"}',
    "access_token=form-secret&code=form-code&client_secret=form-client",
    "Authorization: Bearer header-secret refresh_token=refresh-secret",
    '{"client_secret": "unterminated secret',
    "https://cdn.example/file?X-Amz-Signature=signed-secret&code=url-code",
])
def test_redacts_json_form_headers_truncated_fields_and_signed_urls(body):
    safe = canva_mcp.safe_canva_message(body)
    for secret in ("json secret", "oauth secret", "form-secret", "form-code", "form-client",
                   "header-secret", "refresh-secret", "unterminated secret", "signed-secret", "url-code"):
        assert secret not in safe


def test_bounded_stream_and_safe_sdk_body(caplog):
    yielded = []

    class Stream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                yielded.append(1)
                yield b'access_token="stream-secret" ' + b"x" * 1000

    async def run():
        client = canva_mcp.CanvaMCPClient("token")
        response = httpx2.Response(502, stream=Stream(), request=httpx2.Request("POST", canva_mcp.MCP_URL))
        await client._capture_response(response)
        assert response.is_closed
        assert len(await response.aread()) <= 1200
        assert client._transport_error.http_status == 502
        assert "truncated" in str(client._transport_error)
        logging.getLogger(__name__).error("%s", client._transport_error)
        assert "stream-secret" not in (str(client._transport_error) + response.text + caplog.text)

    asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert len(yielded) <= 10


@pytest.mark.parametrize("status,method", [(200, "POST"), (202, "POST"), (204, "POST"), (405, "GET")])
def test_success_and_optional_get_are_not_consumed(status, method):
    async def run():
        client = canva_mcp.CanvaMCPClient("token")
        response = SimpleNamespace(status_code=status, request=SimpleNamespace(method=method))
        await client._capture_response(response)
        assert client._transport_error is None

    asyncio.run(asyncio.wait_for(run(), timeout=5))


def test_captured_401_refreshes_once_and_persists_rotation(monkeypatch):
    import mcp.client.streamable_http  # noqa: F401

    original = httpx2.AsyncClient
    calls, persisted = [], []

    def respond(request):
        calls.append(request.headers["authorization"])
        return httpx2.Response(401, text="unauthorized")

    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kwargs: original(
        transport=httpx2.MockTransport(respond), **kwargs,
    ))

    async def refresh(**kwargs):
        kwargs["persist_tokens"]("new-access", "new-refresh")
        return "new-access", "new-refresh"

    monkeypatch.setattr(canva_mcp, "refresh_mcp_tokens", refresh)

    async def run():
        async with canva_mcp.connected_client(
            access_token="old-access", refresh_token="old-refresh", client_id="client",
            client_secret="secret", persist_tokens=lambda *pair: persisted.append(pair), timeout=2,
        ):
            pytest.fail("second 401 must fail")

    with pytest.raises(canva_mcp.CanvaMCPError) as caught:
        asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert caught.value.http_status == 401
    # Auto negotiation can probe and then initialize with the same token.
    assert list(dict.fromkeys(calls)) == ["Bearer old-access", "Bearer new-access"]
    assert persisted == [("new-access", "new-refresh")]


def test_wrapped_status_is_preserved():
    original = canva_mcp.CanvaMCPError("safe", http_status=429)
    wrapped = canva_mcp.CanvaMCPError.from_exception("connection failed", ExceptionGroup("group", [original]))
    assert wrapped.http_status == 429


def test_call_uses_fresh_transport_error_and_clears_stale_error():
    client = canva_mcp.CanvaMCPClient("token")
    client._tools = {"search-designs": {}}
    client._transport_error = canva_mcp.CanvaMCPError("stale", http_status=401)

    async def call_tool(*_args):
        assert client._transport_error is None
        response = httpx2.Response(503, text="service unavailable", request=httpx2.Request("POST", canva_mcp.MCP_URL))
        await client._capture_response(response)
        raise RuntimeError("Server returned an error response")

    client._session = SimpleNamespace(call_tool=call_tool)
    with pytest.raises(canva_mcp.CanvaMCPError) as caught:
        asyncio.run(client.call("search-designs", {}))
    assert caught.value.http_status == 503
    assert "service unavailable" in str(caught.value)


def test_legacy_sdk_factory_also_captures_status(monkeypatch):
    import httpx
    import mcp.client.streamable_http as transport_module

    original = httpx.AsyncClient

    def factory(**kwargs):
        return original(transport=httpx.MockTransport(
            lambda request: httpx.Response(403, text='{"code":"legacy-code", "error":"denied"}')
        ), **kwargs)

    @asynccontextmanager
    async def legacy_transport(url, *, httpx_client_factory, **kwargs):
        async with httpx_client_factory(headers=kwargs["headers"], follow_redirects=True) as client:
            await client.post(url)
            # Model the older SDK reducing the HTTP error to a generic exception.
            raise RuntimeError("Server returned an error response")
            yield  # pragma: no cover

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(transport_module, "streamablehttp_client", legacy_transport, raising=False)
    monkeypatch.setitem(sys.modules, "httpx2", None)
    client = canva_mcp.CanvaMCPClient("access")
    with pytest.raises(canva_mcp.CanvaMCPError) as caught:
        asyncio.run(asyncio.wait_for(client.__aenter__(), timeout=5))
    assert caught.value.http_status == 403
    assert "denied" in str(caught.value)
    assert "legacy-code" not in str(caught.value)


def test_unlabelled_known_credentials_are_removed_before_length_limit(caplog):
    async def run():
        client = canva_mcp.CanvaMCPClient("access-secret")
        client._diagnostic_secrets = ("refresh-secret", "client-secret")
        response = httpx2.Response(
            500, text="echo access-secret refresh-secret client-secret " + "x" * 3000,
            request=httpx2.Request("POST", canva_mcp.MCP_URL),
        )
        await client._capture_response(response)
        logging.getLogger(__name__).error("%s", client._transport_error)
        for secret in ("access-secret", "refresh-secret", "client-secret"):
            assert secret not in (str(client._transport_error) + caplog.text + response.text)
        assert len(client._transport_error.redacted_message) <= 1200

    asyncio.run(asyncio.wait_for(run(), timeout=5))


def test_canva_initializes_directly_without_unsupported_discover(monkeypatch):
    import mcp.client.streamable_http  # noqa: F401

    original = httpx2.AsyncClient
    methods = []

    def respond(request):
        if request.method != "POST":
            return httpx2.Response(405)
        message = json.loads(request.content)
        method = message["method"]
        methods.append(method)
        if method == "server/discover":
            return httpx2.Response(500, json={"error": "internal_error", "message": "Unknown error occurred"})
        if method == "notifications/initialized":
            return httpx2.Response(202)
        if method == "initialize":
            result = {"protocolVersion": message["params"]["protocolVersion"],
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "canva-mock", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "inputSchema": {"type": "object", "properties": {}}}
                                for name in ("generate-design", "create-design-from-candidate", "export-design",
                                             "publish-design")]}
        else:
            pytest.fail(f"unexpected MCP method: {method}")
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    monkeypatch.setattr(httpx2, "AsyncClient", lambda **kwargs: original(
        transport=httpx2.MockTransport(respond), **kwargs,
    ))

    async def run():
        async with canva_mcp.connected_client(
            access_token="access", refresh_token="refresh", client_id="client", client_secret="secret",
            persist_tokens=lambda *_: None, timeout=2,
        ) as client:
            assert set(client.tools) == {"generate-design", "create-design-from-candidate", "export-design"}

    asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert methods == ["initialize", "notifications/initialized", "tools/list"]


def test_oauth_uses_resource_scopes_and_excludes_unneeded_permissions(monkeypatch):
    import httpx

    original = httpx.AsyncClient

    def respond(request):
        if request.url.path == "/mcp":
            return httpx.Response(401, headers={
                "www-authenticate": 'Bearer resource_metadata="https://auth.example/resource"',
            })
        if request.url.path == "/resource":
            return httpx.Response(200, json={
                "resource": canva_mcp.MCP_URL, "authorization_servers": ["https://auth.example"],
                "scopes_supported": ["profile:read", "design:content:write", "asset:write",
                                     "folder:write", "comment:write", "help:answers:write"],
            })
        return httpx.Response(200, json={
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://auth.example/token",
        })

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    metadata = asyncio.run(canva_mcp.oauth_metadata("https://api.example/mcp"))
    assert metadata["required_scopes"] == ["profile:read", "design:content:write", "asset:write"]
    url = canva_mcp.build_authorization_url(metadata, client_id="client", redirect_uri="https://app.example/cb",
                                             state="state", challenge="challenge")
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == ["profile:read design:content:write asset:write"]
    assert query["resource"] == [canva_mcp.MCP_URL]


def test_candidate_poll_preserves_generation_job_id(monkeypatch):
    client = canva_mcp.CanvaMCPClient("token")
    client._tools = {"get-design-candidates": SimpleNamespace(inputSchema={
        "properties": {"job_id": {"type": "string"}},
    })}

    async def no_delay(_seconds):
        pass

    async def call(name, args):
        assert name == "get-design-candidates"
        assert args == {"job_id": "generation-job"}
        return {"generated_designs": [{"candidate_id": "candidate"}]}

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    client.call = call
    result = asyncio.run(client.poll_design_job({"job": {"id": "generation-job"}}))
    assert result["job_id"] == "generation-job"
    assert result["generated_designs"] == [{"candidate_id": "candidate"}]
