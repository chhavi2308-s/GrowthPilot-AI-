from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from growthpilot import config, security
from growthpilot.api.routes import integrations
from growthpilot.creative import canva_mcp
from growthpilot.models import OAuthState, ProviderCredential
from growthpilot.security import decrypt_json, decrypt_str, encrypt_json, hash_token


def _settings(monkeypatch):
    key = Fernet.generate_key().decode()
    settings = SimpleNamespace(
        encryption_key=key,
        canva_client_id="test-client-id",
        canva_client_secret="test-client-secret",
        canva_redirect_uri="https://growthpilot.example/api/integrations/canva/callback",
        canva_callback_uri="https://growthpilot.example/api/integrations/canva/callback",
        canva_mcp_url="https://api.canva.com/connect/v1/mcp",
        canva_mcp_daily_cap=7,
    )
    monkeypatch.setattr(integrations, "get_settings", lambda: settings)
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    return settings


def _db():
    engine = create_engine("sqlite://")
    OAuthState.__table__.create(engine)
    ProviderCredential.__table__.create(engine)
    return engine


def _ctx(db, *, workspace_id=11, user_id=21):
    return SimpleNamespace(
        db=db,
        workspace=SimpleNamespace(id=workspace_id),
        user=SimpleNamespace(id=user_id),
        require_owner=lambda: None,
    )


def test_pkce_uses_s256_and_authorization_scope_only_when_discovered():
    verifier, challenge = canva_mcp.pkce_pair()
    assert 43 <= len(verifier) <= 128
    import base64
    import hashlib

    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected
    metadata = {"authorization_endpoint": "https://auth.example/authorize"}
    url = canva_mcp.build_authorization_url(metadata, client_id="client", redirect_uri="https://app/cb",
                                             state="state", challenge=challenge)
    query = parse_qs(urlparse(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert "scope" not in query
    metadata["required_scopes"] = ["profile:read"]
    metadata["resource"] = "https://api.canva.com/connect/v1/mcp"
    url = canva_mcp.build_authorization_url(metadata, client_id="client", redirect_uri="https://app/cb",
                                             state="state", challenge=challenge)
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == ["profile:read"]
    assert query["resource"] == ["https://api.canva.com/connect/v1/mcp"]


def test_canva_url_setting_rejects_non_https_and_embedded_credentials():
    with pytest.raises(ValueError, match="HTTPS"):
        config.Settings._canva_mcp_https("http://api.canva.com/mcp")
    with pytest.raises(ValueError, match="HTTPS"):
        config.Settings._canva_mcp_https("https://user:pass@api.canva.com/mcp")
    assert config.Settings._canva_mcp_https("https://api.canva.com/mcp/") == "https://api.canva.com/mcp"


def test_connect_stores_hashed_state_and_encrypted_verifier(monkeypatch):
    _settings(monkeypatch)
    monkeypatch.setattr(integrations, "oauth_metadata", _async_return({
        "authorization_endpoint": "https://auth.example/authorize", "required_scopes": []
    }))
    engine = _db()
    with Session(engine) as db:
        response = asyncio.run(integrations.canva_connect(_ctx(db)))
        query = parse_qs(urlparse(response["authorize_url"]).query)
        state = query["state"][0]
        saved = db.execute(select(OAuthState)).scalar_one()
        assert saved.provider == "canva_mcp"
        assert saved.state_hash == hash_token(state)
        assert saved.state_hash != state
        verifier = decrypt_str(saved.code_verifier_enc)
        assert query["code_challenge"][0] != verifier
        assert query["code_challenge_method"] == ["S256"]
        assert query["redirect_uri"] == ["https://growthpilot.example/api/integrations/canva/callback"]
        assert saved.expires_at > datetime.now(UTC).replace(tzinfo=None)


def test_callback_is_single_use_workspace_bound_and_persists_encrypted_tokens(monkeypatch):
    _settings(monkeypatch)
    monkeypatch.setattr(integrations, "oauth_metadata", _async_return({
        "authorization_endpoint": "https://auth/authorize", "token_endpoint": "https://auth/token"
    }))
    exchanged = []

    async def exchange(**kwargs):
        exchanged.append(kwargs)
        return {"access_token": "access-secret", "refresh_token": "refresh-secret"}

    monkeypatch.setattr(integrations, "exchange_authorization_code", exchange)
    monkeypatch.setattr(integrations, "_verify_canva", _async_return({"status": "connected"}))
    engine = _db()
    with Session(engine) as db:
        connect = asyncio.run(integrations.canva_connect(_ctx(db)))
        state = parse_qs(urlparse(connect["authorize_url"]).query)["state"][0]
        wrong_workspace = asyncio.run(integrations.canva_callback(state=state, code="one-time-code",
                                                                  ctx=_ctx(db, workspace_id=12)))
        assert "error" in wrong_workspace.headers["location"]
        assert not exchanged
        response = asyncio.run(integrations.canva_callback(state=state, code="one-time-code", ctx=_ctx(db)))
        assert "connected" in response.headers["location"]
        credential = db.execute(select(ProviderCredential)).scalar_one()
        assert credential.workspace_id == 11
        assert credential.status == "authorized"
        assert decrypt_json(credential.credentials_enc) == {
            "access_token": "access-secret", "refresh_token": "refresh-secret"
        }
        assert "access-secret" not in credential.credentials_enc
        second = asyncio.run(integrations.canva_callback(state=state, code="one-time-code", ctx=_ctx(db)))
        assert "error" in second.headers["location"]
        assert len(exchanged) == 1
        assert exchanged[0]["redirect_uri"] == "https://growthpilot.example/api/integrations/canva/callback"


def test_refresh_rotates_and_persists_both_tokens_before_return(monkeypatch):
    class Response:
        def __init__(self, status_code, payload=None, headers=None):
            self.status_code = status_code
            self._payload = payload or {}
            self.headers = headers or {}
            self.is_error = status_code >= 400
            self.text = ""

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.is_error:
                raise AssertionError("unexpected mocked HTTP error")

    class AsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            if url.endswith("/mcp"):
                return Response(401, headers={"www-authenticate": 'Bearer resource_metadata="https://auth/.well-known/resource"'})
            if url.endswith("resource"):
                return Response(200, {"authorization_servers": ["https://auth"],
                                      "resource": "https://api.canva.com/connect/v1/mcp"})
            return Response(200, {"token_endpoint": "https://auth/token"})

        async def post(self, url, **kwargs):
            assert url == "https://auth/token"
            assert kwargs["data"]["resource"] == "https://api.canva.com/connect/v1/mcp"
            return Response(200, {"access_token": "rotated-access", "refresh_token": "rotated-refresh"})

    monkeypatch.setitem(__import__("sys").modules, "httpx", SimpleNamespace(AsyncClient=AsyncClient))
    persisted = []

    def persist(access, refresh):
        persisted.append((access, refresh))

    result = asyncio.run(canva_mcp.refresh_mcp_tokens(client_id="client", client_secret="secret",
                 refresh_token="old-refresh", persist_tokens=persist))
    assert persisted == [("rotated-access", "rotated-refresh")]
    assert result == persisted[0]


def test_authorization_code_exchange_uses_discovered_resource_and_exact_redirect(monkeypatch):
    calls = []

    class Response:
        status_code = 200
        is_error = False

        def json(self):
            return {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}

    class AsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.setitem(__import__("sys").modules, "httpx", SimpleNamespace(AsyncClient=AsyncClient))
    result = asyncio.run(canva_mcp.exchange_authorization_code(
        metadata={"token_endpoint": "https://auth.example/token", "resource": "https://api.canva.com/connect/v1/mcp"},
        client_id="client-id", client_secret="client-secret",
        redirect_uri="https://growthpilot.example/api/integrations/canva/callback",
        code="authorization-code", verifier="pkce-verifier",
    ))
    url, kwargs = calls[0]
    assert url == "https://auth.example/token"
    assert kwargs["auth"] == ("client-id", "client-secret")
    assert kwargs["data"] == {
        "grant_type": "authorization_code", "code": "authorization-code",
        "redirect_uri": "https://growthpilot.example/api/integrations/canva/callback",
        "code_verifier": "pkce-verifier", "resource": "https://api.canva.com/connect/v1/mcp",
    }
    assert result["access_token"] == "access"


def test_workspace_credentials_are_encrypted_and_refresh_lock_is_workspace_scoped(monkeypatch):
    settings = _settings(monkeypatch)
    engine = _db()
    entered = []
    active = 0
    maximum_active = 0

    @asynccontextmanager
    async def fake_connected(**kwargs):
        nonlocal active, maximum_active
        assert isinstance(kwargs["db"], Session)
        assert kwargs["max_daily_generations"] == 7
        active += 1
        maximum_active = max(maximum_active, active)
        entered.append(kwargs["access_token"])
        await asyncio.sleep(0.01)
        kwargs["persist_tokens"]("new-" + kwargs["access_token"], "new-refresh")
        yield SimpleNamespace(tools={}), None
        active -= 1

    monkeypatch.setattr(canva_mcp, "connected_client", fake_connected)
    with Session(engine) as db:
        for workspace_id, token in ((31, "workspace-a-access"), (32, "workspace-b-access")):
            db.add(ProviderCredential(workspace_id=workspace_id, provider="canva_mcp",
                                      credentials_enc=encrypt_json({"access_token": token,
                                                                    "refresh_token": token + "-refresh"})))
        db.commit()

        async def run_pair():
            async def one(workspace_id):
                with Session(engine) as session:
                    async with canva_mcp.connected_workspace_client(session, workspace_id, settings):
                        await asyncio.sleep(0.01)

            await asyncio.gather(one(31), one(31))

        asyncio.run(run_pair())
        assert maximum_active == 1
        assert entered == ["workspace-a-access", "new-workspace-a-access"] or set(entered) == {
            "workspace-a-access", "new-workspace-a-access"
        }
        rows = db.execute(select(ProviderCredential).order_by(ProviderCredential.workspace_id)).scalars().all()
        values = [decrypt_json(row.credentials_enc) for row in rows]
        assert values[0]["refresh_token"] == "new-refresh"
        assert values[1]["access_token"] == "workspace-b-access"


def test_disconnect_deletes_only_current_workspace_credential(monkeypatch):
    _settings(monkeypatch)
    engine = _db()
    with Session(engine) as db:
        for workspace_id in (41, 42):
            db.add(ProviderCredential(workspace_id=workspace_id, provider="canva_mcp",
                                      credentials_enc=encrypt_json({"access_token": "opaque"})))
        db.commit()
        response = integrations.canva_disconnect(_ctx(db, workspace_id=41))
        assert response == {"status": "disconnected"}
        rows = db.execute(select(ProviderCredential)).scalars().all()
        assert [row.workspace_id for row in rows] == [42]


def test_status_reports_connected_only_after_required_authenticated_tools(monkeypatch):
    settings = _settings(monkeypatch)
    engine = _db()
    with Session(engine) as db:
        ctx = _ctx(db, workspace_id=50)
        settings.canva_client_id = ""
        assert asyncio.run(integrations._verify_canva(ctx))["status"] == "misconfigured"
        settings.canva_client_id = "test-client-id"
        assert asyncio.run(integrations._verify_canva(ctx))["status"] == "disconnected"
        db.add(ProviderCredential(workspace_id=51, provider="canva_mcp",
                                  credentials_enc=encrypt_json({"access_token": "opaque"})))
        db.commit()

        @asynccontextmanager
        async def fake_connected_workspace(_db, _workspace_id, _settings):
            yield SimpleNamespace(tools={"generate-design": {}, "create-design-from-candidate": {},
                                         "export-design": {}}), db.get(ProviderCredential, 1)

        monkeypatch.setattr(integrations, "connected_workspace_client", fake_connected_workspace)
        status = asyncio.run(integrations._verify_canva(_ctx(db, workspace_id=51)))
        assert status["status"] == "connected"
        assert status["tools"] == ["create-design-from-candidate", "export-design", "generate-design"]
        assert db.get(ProviderCredential, 1).last_verified_at is not None

        @asynccontextmanager
        async def missing_tools(_db, _workspace_id, _settings):
            yield SimpleNamespace(tools={"search-designs": {}}), db.get(ProviderCredential, 1)

        monkeypatch.setattr(integrations, "connected_workspace_client", missing_tools)
        failed = asyncio.run(integrations._verify_canva(_ctx(db, workspace_id=51)))
        assert failed["status"] == "error"
        assert "required tools" in failed["message"]
        assert db.get(ProviderCredential, 1).status == "error"


def test_redaction_removes_tokens_codes_and_signed_canva_urls():
    message = ("Authorization: Bearer token-value access_token=opaque-access "
               "https://design.canva.ai/api/design/ABC123?signature=signed-secret")
    safe = canva_mcp.safe_canva_message(message)
    assert "token-value" not in safe
    assert "opaque-access" not in safe
    assert "signed-secret" not in safe
    assert "[design:ABC123]" in safe or "[redacted-url]" in safe


def _async_return(value):
    async def result(*_args, **_kwargs):
        return value

    return result


def test_public_canva_callback_relays_only_to_configured_app_origin(monkeypatch):
    from starlette.requests import Request

    settings = _settings(monkeypatch)
    settings.base_url = "http://localhost:3000"
    request = Request({"type": "http", "scheme": "https", "method": "GET",
                       "server": ("growthpilot.example", 443), "path": "/api/integrations/canva/callback",
                       "query_string": b"code=mock-code&state=mock-state",
                       "headers": [(b"host", b"growthpilot.example")]})
    monkeypatch.setattr(integrations, "get_ctx", lambda *_: pytest.fail("Relay must not consume OAuth state"))
    response = asyncio.run(integrations.canva_oauth_return(request, db=None))
    assert response.status_code == 303
    assert response.headers["location"] == (
        "http://localhost:3000/api/integrations/canva/callback?code=mock-code&state=mock-state"
    )
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("incoming,base", [
    ("localhost:3000", "http://localhost:3000"),
    ("attacker.example", "http://localhost:3000"),
    ("growthpilot.example", "http://insecure.example"),
    ("growthpilot.example", "https://user:pass@app.example"),
])
def test_canva_callback_cannot_bypass_session_or_redirect_to_unsafe_origin(monkeypatch, incoming, base):
    from starlette.requests import Request

    settings = _settings(monkeypatch)
    settings.base_url = base
    request = Request({"type": "http", "scheme": "https", "method": "GET",
                       "server": (incoming, 443), "path": "/api/integrations/canva/callback",
                       "query_string": b"", "headers": [(b"host", incoming.encode())]})

    def require_session(*_args):
        raise RuntimeError("session still required")

    monkeypatch.setattr(integrations, "get_ctx", require_session)
    with pytest.raises(RuntimeError, match="session still required"):
        asyncio.run(integrations.canva_oauth_return(request, db=None))
