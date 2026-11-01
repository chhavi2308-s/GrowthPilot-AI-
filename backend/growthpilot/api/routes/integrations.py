from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ... import audit
from ...adapters import AdapterError, all_adapters, available_adapters, get_adapter, live_adapter_for
from ...adapters.facebook import FacebookPageAdapter
from ...adapters.instagram import InstagramAdapter
from ...adapters.webhook import validate_url
from ...config import get_settings
from ...creative.canva_mcp import (
    CanvaMCPError,
    build_authorization_url,
    connected_workspace_client,
    exchange_authorization_code,
    oauth_metadata,
    pkce_pair,
    safe_canva_message,
)
from ...db import get_db
from ...models import (
    ChannelConnection,
    ConnectionStatus,
    ContentItem,
    ContentStatus,
    OAuthState,
    ProviderCredential,
    utcnow,
)
from ...platforms import ORDER, SPECS
from ...research import search as search_mod
from ...security import decrypt_str, encrypt_json, encrypt_str, hash_token, new_token, redact
from ...services.connections import context
from .. import serializers as S
from ..deps import ApiError, Ctx, get_ctx, rate_limit

router = APIRouter(prefix="/api", tags=["integrations"])


def _adapter_info(key: str) -> dict:
    a = all_adapters()[key]
    caps = a.capabilities
    return {"key": a.key, "label": a.label, "platform": a.platform, "external": a.external, "maturity": a.maturity,
            "capabilities": {"oauth": caps.oauth, "identity": caps.identity, "validation": caps.validation,
                             "publish": caps.publish, "scheduling": caps.scheduling,
                             "status_verification": caps.status_verification, "analytics": caps.analytics,
                             "content_types": list(caps.content_types)}}


def _conns(ctx: Ctx) -> list[ChannelConnection]:
    return list(ctx.db.execute(select(ChannelConnection).where(ChannelConnection.workspace_id == ctx.workspace.id)
                               .order_by(ChannelConnection.id)).scalars())


def _llm_cards(s) -> list[dict]:
    active = s.active_llm_provider
    def status(configured: bool) -> str:
        if s.is_demo:
            return ConnectionStatus.DEMO
        return "configured" if configured else ConnectionStatus.NOT_CONFIGURED
    return [
        {"key": "gemini", "group": "ai", "name": "Google Gemini (LLM)", "status": status(bool(s.gemini_api_key)),
         "active": active == "gemini", "detail": "Demo planner and generator (no LLM calls)" if s.is_demo else
         (f"Model {s.gemini_model}, thinking {s.gemini_thinking_level}" + (" — active" if active == "gemini" else
                                                                            " — configured, not active")
          if s.gemini_api_key else "Set GEMINI_API_KEY in .env (Google AI Studio). Consumer Gemini subscriptions "
          "don't include API credits."), "env": ["GEMINI_API_KEY", "GEMINI_MODEL", "LLM_PROVIDER"],
         "testable": s.is_live and bool(s.gemini_api_key), "maturity": "implemented, tested with a mocked API"},
        {"key": "anthropic", "group": "ai", "name": "Anthropic Claude (LLM)", "status": status(bool(s.llm_key)),
         "active": active == "anthropic", "detail": "Demo planner (no LLM calls)" if s.is_demo else
         (f"Model {s.llm_model}, effort {s.llm_effort}" + (" — active" if active == "anthropic" else
                                                          " — configured, not active")
          if s.llm_key else "Optional. Set LLM_API_KEY to use Claude instead of Gemini."),
         "env": ["LLM_API_KEY", "LLM_MODEL", "LLM_PROVIDER"], "testable": s.is_live and bool(s.llm_key),
         "maturity": "implemented, tested with a mocked model"},
        {"key": "search", "group": "ai", "name": "Brave Search (web research)", "status": status(s.search_configured),
         "detail": "Demo mode uses labelled fixture notes" if s.is_demo else
         ("Configured. “Test” runs one search (counts toward your quota; $5 per 1,000 requests after Brave's "
          "monthly credit)." if s.search_configured else "Set BRAVE_SEARCH_API_KEY in .env. Without it, research "
          "uses the brand's own pages and the seasonal calendar."), "env": ["BRAVE_SEARCH_API_KEY"],
         "testable": s.is_live and s.search_configured, "maturity": "implemented, tested with a mocked API"},
        {"key": "image", "group": "ai", "name": "AI backgrounds (Gemini image)",
         "status": status(s.image_generation_configured),
         "detail": "Optional. Posters render locally for free; an image model can add an abstract background "
                   "(labelled on the image). Product imagery is always real.", "env": [
             "IMAGE_GENERATION_PROVIDER=gemini", "IMAGE_GENERATION_MODEL"],
         "testable": False, "maturity": "implemented, tested with a mocked API"},
    ]


def _platform_cards(ctx: Ctx, s, conns: list[ChannelConnection]) -> list[dict]:
    allowed = available_adapters()
    out = []
    for key in ORDER:
        sp = SPECS[key]
        mine = [c for c in conns if c.platform == key and c.adapter in allowed]
        live = live_adapter_for(key)
        if s.is_demo:
            status = ConnectionStatus.DEMO if mine else ConnectionStatus.NOT_CONFIGURED
        elif live is None:
            status = "not_implemented"
        elif not s.instagram_configured:  # both Meta adapters use the same app credentials
            status = ConnectionStatus.NOT_CONFIGURED
        elif not mine:
            status = ConnectionStatus.AWAITING_AUTHORIZATION
        else:
            status = mine[0].status
        out.append({"key": key, "group": "platform", "name": sp.label, "status": status,
                    "live_adapter": live.maturity if live else "not-implemented",
                    "connect": "oauth" if (live and not s.is_demo and key in ("instagram", "facebook")) else None,
                    "connections": [S.connection(c, _adapter_info(c.adapter)) for c in mine],
                    "spec": sp.as_dict()})
    return out


@router.get("/integrations")
def integrations(ctx: Ctx = Depends(get_ctx)):
    s = get_settings()
    conns = _conns(ctx)
    allowed = available_adapters()
    canva_credential = ctx.db.execute(select(ProviderCredential).where(
        ProviderCredential.workspace_id == ctx.workspace.id, ProviderCredential.provider == "canva_mcp"
    )).scalar_one_or_none()
    canva_configured = (bool(s.canva_client_id and s.canva_client_secret) and
                        _valid_canva_redirect(s.canva_callback_uri))
    canva_status = "misconfigured" if not canva_configured else (
        canva_credential.status if canva_credential and canva_credential.status in {"connected", "error"}
        else "disconnected"
    )
    webhook = {"key": "webhook", "group": "other", "name": "Signed webhook (Zapier / Make / n8n)",
               "status": ConnectionStatus.DEMO if s.is_demo else ("connected" if any(
                   c.adapter == "webhook" and c.status == "connected" for c in conns) else "not_configured"),
               "detail": "Live mode only." if s.is_demo else "Add a webhook URL below.", "env": [],
               "adapter": _adapter_info("webhook")}
    redirects = {"instagram": s.instagram_redirect_uri, "facebook": s.facebook_redirect}
    return {"mode": s.app_mode.value, "live_publishing_enabled": s.live_publishing_enabled,
            "ai": _llm_cards(s), "platforms": _platform_cards(ctx, s, conns), "other": [webhook],
            "canva": {"status": canva_status, "redirect_uri": s.canva_callback_uri,
                      "last_verified_at": canva_credential.last_verified_at.isoformat()
                      if canva_credential and canva_credential.last_verified_at else None},
            "redirect_uris": redirects if not s.is_demo else {},
            "connections": [S.connection(c, _adapter_info(c.adapter) if c.adapter in all_adapters() else None)
                            | {"available_in_mode": c.adapter in allowed} for c in conns],
            "config_problems": s.problems()}


def _canva_back(status: str, message: str = "") -> RedirectResponse:
    return RedirectResponse(f"/integrations?canva={status}&message={quote(redact(message)[:250])}", status_code=303)


def _valid_canva_redirect(uri: str) -> bool:
    parsed = urlparse(uri)
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


async def _verify_canva(ctx: Ctx) -> dict:
    settings = get_settings()
    if not (settings.canva_client_id and settings.canva_client_secret and
            _valid_canva_redirect(settings.canva_callback_uri)):
        return {"status": "misconfigured", "message":
                "Set CANVA_CLIENT_ID, CANVA_CLIENT_SECRET and an HTTPS CANVA_REDIRECT_URI registered in Canva."}
    row = ctx.db.execute(select(ProviderCredential).where(
        ProviderCredential.workspace_id == ctx.workspace.id, ProviderCredential.provider == "canva_mcp"
    )).scalar_one_or_none()
    if row is None:
        return {"status": "disconnected", "message": "Connect a Canva account for this workspace."}
    try:
        async with connected_workspace_client(ctx.db, ctx.workspace.id, settings) as (client, credential):
            names = sorted(client.tools)
            required = {"generate-design", "create-design-from-candidate", "export-design"}
            missing = sorted(required - set(names))
            if missing:
                raise CanvaMCPError("Authenticated Canva MCP is missing required tools: " + ", ".join(missing))
            credential.status = "connected"
            credential.last_verified_at = utcnow()
            credential.last_error = None
            ctx.db.commit()
            return {"status": "connected", "tools": names}
    except Exception as exc:
        safe = safe_canva_message(str(exc))
        ctx.db.rollback()
        row = ctx.db.execute(select(ProviderCredential).where(
            ProviderCredential.workspace_id == ctx.workspace.id, ProviderCredential.provider == "canva_mcp"
        )).scalar_one_or_none()
        if row is None:
            return {"status": "error", "message": safe[:500] or type(exc).__name__}
        row.status = "error"
        row.last_error = safe[:500]
        ctx.db.commit()
        return {"status": "error", "message": safe[:500] or type(exc).__name__}


@router.get("/integrations/canva/status")
async def canva_status(ctx: Ctx = Depends(get_ctx)):
    rate_limit(f"canva-status:{ctx.workspace.id}:{ctx.user.id}", 12, 60)
    return await _verify_canva(ctx)


@router.get("/integrations/canva/connect")
async def canva_connect(ctx: Ctx = Depends(get_ctx)):
    ctx.require_owner()
    settings = get_settings()
    if not settings.canva_client_id or not settings.canva_client_secret:
        raise ApiError(400, "Set CANVA_CLIENT_ID and CANVA_CLIENT_SECRET.", "not_configured")
    redirect_uri = settings.canva_callback_uri
    if not _valid_canva_redirect(redirect_uri):
        raise ApiError(400, "CANVA_REDIRECT_URI must be an HTTPS callback URL registered exactly in Canva.",
                       "invalid_redirect")
    rate_limit(f"canva-connect:{ctx.workspace.id}:{ctx.user.id}", 5, 3600)
    verifier, challenge = pkce_pair()
    state = new_token(32)
    metadata = await oauth_metadata(settings.canva_mcp_url)
    ctx.db.add(OAuthState(state_hash=hash_token(state), provider="canva_mcp",
                          workspace_id=ctx.workspace.id, user_id=ctx.user.id,
                          code_verifier_enc=encrypt_str(verifier), expires_at=utcnow() + timedelta(minutes=10)))
    ctx.db.commit()
    return {"authorize_url": build_authorization_url(metadata, client_id=settings.canva_client_id,
              redirect_uri=redirect_uri, state=state, challenge=challenge)}


@router.get("/integrations/canva/callback")
async def canva_oauth_return(request: Request, state: str = "", code: str = "", error: str = "",
                             error_description: str = "", db: Session = Depends(get_db)):
    settings = get_settings()
    callback = urlparse(settings.canva_callback_uri)
    base = urlparse(settings.base_url)
    incoming_host = request.headers.get("x-forwarded-host", request.url.netloc).split(",")[0].strip().casefold()
    safe_base = (base.scheme == "https" or
                 (base.scheme == "http" and base.hostname in {"localhost", "127.0.0.1", "::1"}))
    if (safe_base and base.hostname and not base.username and not base.password
            and callback.netloc.casefold() == incoming_host
            and base.netloc.casefold() != incoming_host):
        # A localhost login cookie cannot accompany Canva's public tunnel return.
        # Relay only to the configured app origin; the normal callback below still
        # requires its session and atomically checks user/workspace-bound state.
        target = f"{base.scheme}://{base.netloc}/api/integrations/canva/callback?{request.url.query}"
        return RedirectResponse(target, status_code=303,
                                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    return await canva_callback(state=state, code=code, error=error, error_description=error_description,
                                ctx=get_ctx(request, db))


async def canva_callback(state: str = "", code: str = "", error: str = "", error_description: str = "",
                         ctx: Ctx = Depends(get_ctx)):
    verifier_enc = ctx.db.execute(delete(OAuthState).where(
        OAuthState.state_hash == hash_token(state or "-"), OAuthState.provider == "canva_mcp",
        OAuthState.workspace_id == ctx.workspace.id, OAuthState.user_id == ctx.user.id,
        OAuthState.expires_at > utcnow(),
    ).returning(OAuthState.code_verifier_enc)).scalar_one_or_none()
    ctx.db.commit()  # atomic, single-use claim; consume even when exchange is denied or fails
    if verifier_enc is None:
        return _canva_back("error", "Authorization state expired or did not match this user and workspace.")
    verifier = decrypt_str(verifier_enc)
    if error or not code:
        return _canva_back("error", "Canva authorization was cancelled or denied.")
    settings = get_settings()
    try:
        metadata = await oauth_metadata(settings.canva_mcp_url)
        tokens = await exchange_authorization_code(metadata=metadata, client_id=settings.canva_client_id,
                    client_secret=settings.canva_client_secret, redirect_uri=settings.canva_callback_uri,
                    code=code, verifier=verifier)
        credential = ctx.db.execute(select(ProviderCredential).where(
            ProviderCredential.workspace_id == ctx.workspace.id, ProviderCredential.provider == "canva_mcp"
        )).scalar_one_or_none()
        if credential is None:
            credential = ProviderCredential(workspace_id=ctx.workspace.id, provider="canva_mcp",
                                             credentials_enc=encrypt_json(tokens), status="authorized")
            ctx.db.add(credential)
        else:
            credential.credentials_enc = encrypt_json(tokens)
            credential.status, credential.last_error = "authorized", None
        ctx.db.commit()
        checked = await _verify_canva(ctx)
        return _canva_back("connected" if checked["status"] == "connected" else "error",
                           "Canva connected and MCP tools verified." if checked["status"] == "connected"
                           else checked.get("message", "MCP verification failed."))
    except Exception as exc:
        ctx.db.rollback()
        return _canva_back("error", safe_canva_message(str(exc)) or type(exc).__name__)


@router.post("/integrations/canva/disconnect")
def canva_disconnect(ctx: Ctx = Depends(get_ctx)):
    ctx.require_owner()
    credential = ctx.db.execute(select(ProviderCredential).where(
        ProviderCredential.workspace_id == ctx.workspace.id, ProviderCredential.provider == "canva_mcp"
    )).scalar_one_or_none()
    if credential:
        ctx.db.delete(credential)
    for oauth in ctx.db.execute(select(OAuthState).where(OAuthState.workspace_id == ctx.workspace.id,
                                                         OAuthState.provider == "canva_mcp")).scalars():
        ctx.db.delete(oauth)
    ctx.db.commit()
    return {"status": "disconnected"}


@router.post("/integrations/{key}/test")
def test_integration(key: str, ctx: Ctx = Depends(get_ctx)):
    """Minimal real connection tests. Each makes at most one small request; secrets are never echoed."""
    ctx.require_owner()
    s = get_settings()
    if s.is_demo:
        raise ApiError(400, "Demo mode makes no external calls.", "demo")
    rate_limit(f"test:{ctx.workspace.id}:{key}", 10, 3600)
    if key == "gemini":
        from ...llm.gemini_llm import check_key

        if not s.gemini_api_key:
            return {"ok": False, "message": "GEMINI_API_KEY isn't set."}
        ok, msg = check_key(s.gemini_api_key, s.gemini_model)
        return {"ok": ok, "message": msg}
    if key in ("anthropic", "llm"):
        import anthropic

        if not s.llm_key:
            return {"ok": False, "message": "LLM_API_KEY isn't set."}
        try:
            anthropic.Anthropic(api_key=s.llm_key, timeout=20).models.retrieve(s.llm_model)
        except anthropic.AuthenticationError:
            return {"ok": False, "message": "Anthropic rejected the API key."}
        except anthropic.NotFoundError:
            return {"ok": False, "message": f"Model {s.llm_model} not available to this key."}
        except anthropic.APIError as exc:
            return {"ok": False, "message": f"Anthropic error: {redact(str(exc))[:200]}"}
        return {"ok": True, "message": f"Key works and {s.llm_model} is available."}
    if key == "search":
        from ... import usage

        try:
            results = search_mod.search("pooja essentials Bengaluru", count=1, use_cache=False)
        except (search_mod.SearchUnavailable, search_mod.SearchError) as exc:
            return {"ok": False, "message": str(exc)}
        usage.record(provider="brave", operation="connection_test", units=1,
                     cost_usd=usage.unit_cost("brave:web_search"), db=ctx.db, workspace_id=ctx.workspace.id)
        ctx.db.commit()
        return {"ok": True, "message": f"Search works ({len(results)} result returned)."}
    raise ApiError(404, "Unknown integration.", "not_found")


@router.post("/integrations/instagram/start")
def instagram_start(ctx: Ctx = Depends(get_ctx)):
    ctx.require_owner()
    s = get_settings()
    if s.is_demo:
        raise ApiError(400, "Demo mode uses the demo Instagram adapter; switch to live mode to connect an account.",
                       "demo")
    if not s.instagram_configured:
        raise ApiError(400, "Set META_APP_ID and META_APP_SECRET in .env first.", "not_configured")
    state = new_token(24)
    ctx.db.add(OAuthState(state_hash=hash_token(state), provider="instagram", workspace_id=ctx.workspace.id,
                          user_id=ctx.user.id, code_verifier_enc=encrypt_str("-"),
                          expires_at=utcnow() + timedelta(minutes=10)))
    ctx.db.commit()
    return {"authorize_url": InstagramAdapter().authorize_url(state)}


def _back(status: str, message: str = "", provider: str = "instagram") -> RedirectResponse:
    return RedirectResponse(f"/integrations?{provider}={status}&message={quote(message[:300])}", status_code=303)


@router.get("/integrations/instagram/callback")
def instagram_callback(state: str = "", code: str = "", error: str = "", error_description: str = "",
                       ctx: Ctx = Depends(get_ctx)):
    row = ctx.db.execute(select(OAuthState).where(OAuthState.state_hash == hash_token(state or "-"))).scalar_one_or_none()
    valid = (row is not None and row.provider == "instagram" and row.user_id == ctx.user.id
             and row.workspace_id == ctx.workspace.id and row.expires_at > utcnow())
    if row is not None:
        ctx.db.delete(row)  # single use
        ctx.db.commit()
    if not valid:
        return _back("error", "Authorisation link expired or invalid. Start again.")
    if error or not code:
        return _back("error", f"Authorisation cancelled: {redact(error_description or error)}")
    adapter = InstagramAdapter()
    try:
        creds = adapter.exchange_code(code)
    except AdapterError as exc:
        return _back("error", f"Token exchange failed: {redact(str(exc))}")
    conn = ChannelConnection(workspace_id=ctx.workspace.id, adapter="instagram", platform="instagram",
                             name="Instagram", account_id=creds.get("user_id", ""), credentials_enc=encrypt_json(creds),
                             token_expires_at=datetime.fromtimestamp(creds["expires_at"], UTC).replace(tzinfo=None) if creds.get("expires_at") else None,
                             status=ConnectionStatus.AWAITING_AUTHORIZATION, created_by=ctx.user.id)
    ctx.db.add(conn)
    ctx.db.flush()
    try:
        info = adapter.verify_connection(context(ctx.db, conn))
        conn.account_id, conn.account_label = info.account_id, info.label
        conn.status, conn.last_verified_at = ConnectionStatus.CONNECTED, utcnow()
        msg = f"Connected {info.label}."
    except AdapterError as exc:
        conn.status, conn.last_error = ConnectionStatus.FAILED, redact(str(exc))
        msg = f"Authorised, but verification failed: {conn.last_error}"
    audit.record(ctx.db, "connection.created", workspace_id=ctx.workspace.id, user_id=ctx.user.id,
                 target_type="connection", target_id=conn.id, ip=ctx.ip, adapter="instagram", status=conn.status)
    ctx.db.commit()
    return _back("connected" if conn.status == ConnectionStatus.CONNECTED else "error", msg)


@router.post("/integrations/facebook/start")
def facebook_start(ctx: Ctx = Depends(get_ctx)):
    ctx.require_owner()
    s = get_settings()
    if s.is_demo:
        raise ApiError(400, "Demo mode uses the demo Facebook adapter; switch to live mode to connect a Page.", "demo")
    if not s.instagram_configured:
        raise ApiError(400, "Set META_APP_ID and META_APP_SECRET in .env first.", "not_configured")
    state = new_token(24)
    ctx.db.add(OAuthState(state_hash=hash_token(state), provider="facebook", workspace_id=ctx.workspace.id,
                          user_id=ctx.user.id, code_verifier_enc=encrypt_str("-"),
                          expires_at=utcnow() + timedelta(minutes=10)))
    ctx.db.commit()
    return {"authorize_url": FacebookPageAdapter().authorize_url(state)}


@router.get("/integrations/facebook/callback")
def facebook_callback(state: str = "", code: str = "", error: str = "", error_description: str = "",
                      ctx: Ctx = Depends(get_ctx)):
    row = ctx.db.execute(select(OAuthState).where(OAuthState.state_hash == hash_token(state or "-"))).scalar_one_or_none()
    valid = (row is not None and row.provider == "facebook" and row.user_id == ctx.user.id
             and row.workspace_id == ctx.workspace.id and row.expires_at > utcnow())
    if row is not None:
        ctx.db.delete(row)
        ctx.db.commit()
    if not valid:
        return _back("error", "Authorisation link expired or invalid. Start again.", "facebook")
    if error or not code:
        return _back("error", f"Authorisation cancelled: {redact(error_description or error)}", "facebook")
    adapter = FacebookPageAdapter()
    try:
        creds = adapter.exchange_code(code)
    except AdapterError as exc:
        return _back("error", f"Token exchange failed: {redact(str(exc))}", "facebook")
    page = creds["pages"][0]
    conn = ChannelConnection(workspace_id=ctx.workspace.id, adapter="facebook", platform="facebook",
                             name="Facebook Page", account_id=page["id"], credentials_enc=encrypt_json(creds),
                             status=ConnectionStatus.AWAITING_AUTHORIZATION, created_by=ctx.user.id)
    ctx.db.add(conn)
    ctx.db.flush()
    try:
        info = adapter.verify_connection(context(ctx.db, conn))
        conn.account_id, conn.account_label = info.account_id, info.label
        conn.status, conn.last_verified_at = ConnectionStatus.CONNECTED, utcnow()
        msg = f"Connected {info.label}." + (f" {len(creds['pages']) - 1} other Page(s) were also shared."
                                            if len(creds["pages"]) > 1 else "")
    except AdapterError as exc:
        conn.status, conn.last_error = ConnectionStatus.FAILED, redact(str(exc))
        msg = f"Authorised, but verification failed: {conn.last_error}"
    audit.record(ctx.db, "connection.created", workspace_id=ctx.workspace.id, user_id=ctx.user.id,
                 target_type="connection", target_id=conn.id, ip=ctx.ip, adapter="facebook", status=conn.status)
    ctx.db.commit()
    return _back("connected" if conn.status == ConnectionStatus.CONNECTED else "error", msg, "facebook")


class WebhookIn(BaseModel):
    name: str = Field(default="Webhook", max_length=120)
    url: str = Field(max_length=1000)


@router.post("/connections/webhook")
def add_webhook(body: WebhookIn, ctx: Ctx = Depends(get_ctx)):
    ctx.require_owner()
    try:
        get_adapter("webhook")
        validate_url(body.url)
    except AdapterError as exc:
        raise ApiError(400, str(exc), "validation") from exc
    secret = new_token(32)
    conn = ChannelConnection(workspace_id=ctx.workspace.id, adapter="webhook", platform="any", name=body.name.strip()
                             or "Webhook", config={"url": body.url}, credentials_enc=encrypt_json({"signing_secret": secret}),
                             status=ConnectionStatus.AWAITING_AUTHORIZATION, created_by=ctx.user.id)
    ctx.db.add(conn)
    ctx.db.flush()
    audit.record(ctx.db, "connection.created", workspace_id=ctx.workspace.id, user_id=ctx.user.id,
                 target_type="connection", target_id=conn.id, ip=ctx.ip, adapter="webhook")
    ctx.db.commit()
    return {"connection": S.connection(conn), "signing_secret": secret,
            "note": "Copy the signing secret now; it won't be shown again."}


@router.post("/connections/{conn_id}/verify")
def verify_connection(conn_id: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_owner()
    conn = ctx.get(ChannelConnection, conn_id)
    try:
        adapter = get_adapter(conn.adapter)
        info = adapter.verify_connection(context(ctx.db, conn))
    except AdapterError as exc:
        conn.status = ConnectionStatus.EXPIRED if exc.reauth else ConnectionStatus.FAILED
        conn.last_error = redact(str(exc))
        ctx.db.commit()
        return S.connection(conn)
    conn.account_id = info.account_id or conn.account_id
    conn.account_label = info.label
    conn.status = ConnectionStatus.DEMO if not adapter.external else ConnectionStatus.CONNECTED
    conn.last_verified_at, conn.last_error = utcnow(), None
    audit.record(ctx.db, "connection.verified", workspace_id=ctx.workspace.id, user_id=ctx.user.id,
                 target_type="connection", target_id=conn.id, ip=ctx.ip)
    ctx.db.commit()
    return S.connection(conn)


@router.delete("/connections/{conn_id}")
def delete_connection(conn_id: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_owner()
    conn = ctx.get(ChannelConnection, conn_id)
    scheduled = ctx.db.execute(select(ContentItem).where(ContentItem.connection_id == conn.id,
                                                         ContentItem.status == ContentStatus.SCHEDULED)).scalars().all()
    for i in scheduled:
        i.status, i.scheduled_at = ContentStatus.APPROVED, None
    audit.record(ctx.db, "connection.deleted", workspace_id=ctx.workspace.id, user_id=ctx.user.id,
                 target_type="connection", target_id=conn.id, ip=ctx.ip, unscheduled=len(scheduled))
    ctx.db.delete(conn)
    ctx.db.commit()
    return {"ok": True, "unscheduled": len(scheduled)}
