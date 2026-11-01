"""Instagram adapter against a mocked Meta API (no real network, never a real account)."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from growthpilot.adapters.base import AdapterError, ConnectionContext, PublishPayload
from growthpilot.adapters.instagram import InstagramAdapter
from growthpilot.db import session_scope
from growthpilot.models import ChannelConnection, ContentItem, utcnow
from growthpilot.research import net

from .conftest import Api, make_client, run_worker
from .test_workflow import _attach_photo, _new_item

IG = "https://graph.instagram.com/v26.0"


class FakeMeta:
    def __init__(self, account_type="BUSINESS", container_status="FINISHED", quota=0):
        self.account_type = account_type
        self.container_status = container_status
        self.quota = quota
        self.published: list[str] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        url = str(req.url)
        if url.startswith("https://api.instagram.com/oauth/access_token"):
            body = parse_qs(req.content.decode())
            assert body["grant_type"] == ["authorization_code"] and body["code"] == ["good-code"]
            return httpx.Response(200, json={"data": [{"access_token": "SHORT", "user_id": "1784",
                                                        "permissions": "instagram_business_basic,"
                                                        "instagram_business_content_publish,"
                                                        "instagram_business_manage_insights"}]})
        if url.startswith("https://graph.instagram.com/access_token"):
            return httpx.Response(200, json={"access_token": "LONG", "token_type": "bearer", "expires_in": 5183944})
        if url.startswith("https://graph.instagram.com/refresh_access_token"):
            return httpx.Response(200, json={"access_token": "REFRESHED", "expires_in": 5183944})
        assert req.headers.get("authorization", "").startswith("Bearer "), "token must go in the header"
        path = urlparse(url).path
        if path.endswith("/me"):
            return httpx.Response(200, json={"user_id": "1784", "username": "aranya.test", "account_type": self.account_type})
        if path.endswith("/content_publishing_limit"):
            return httpx.Response(200, json={"data": [{"quota_usage": self.quota, "config": {"quota_total": 100}}]})
        if path.endswith("/media") and req.method == "POST":
            return httpx.Response(200, json={"id": "C1"})
        if path.endswith("/C1"):
            return httpx.Response(200, json={"status_code": self.container_status})
        if path.endswith("/media_publish"):
            self.published.append("M1")
            return httpx.Response(200, json={"id": "M1"})
        if path.endswith("/M1/insights"):
            return httpx.Response(200, json={"data": [{"name": n, "period": "lifetime", "values": [{"value": v}]}
                                                      for n, v in [("reach", 1000), ("likes", 50), ("comments", 5),
                                                                   ("shares", 7), ("saved", 12), ("views", 1500)]]})
        if path.endswith("/M1"):
            return httpx.Response(200, json={"id": "M1", "permalink": "https://www.instagram.com/p/abc/",
                                             "timestamp": "2026-10-24T10:00:00+0000"})
        return httpx.Response(404, json={"error": {"message": "unknown"}})


def _ctx(creds=None):
    creds = creds or {"access_token": "LONG", "issued_at": int(time.time()) - 3 * 86400,
                      "expires_at": int(time.time()) + 50 * 86400,
                      "permissions": ["instagram_business_basic", "instagram_business_content_publish",
                                      "instagram_business_manage_insights"]}
    saved = {}
    return ConnectionContext(connection_id=1, account_id="1784", config={}, credentials=creds,
                             save_credentials=lambda c: saved.update(c)), saved


def _payload(urls=("https://app.example.com/api/public/assets/t.jpg",)):
    return PublishPayload(content_id=1, version=2, platform="instagram", content_type="post", caption="Hello",
                          image_urls=list(urls))


@pytest.fixture
def meta(monkeypatch, tmp_path):
    make_client(monkeypatch, tmp_path, "live", INSTAGRAM_APP_ID="ig-123", INSTAGRAM_APP_SECRET="ig-shh", META_APP_ID="123", META_APP_SECRET="shh", BASE_URL="https://app.example.com")
    fake = FakeMeta()
    net.set_transport(httpx.MockTransport(fake))
    return fake


def test_publish_flow_and_verification(meta):
    a = InstagramAdapter()
    ctx, _ = _ctx()
    res = a.publish(ctx, _payload())
    assert res.external_id == "M1" and meta.published == ["M1"]
    st = a.publication_status(ctx, "M1")
    assert st.state == "published" and st.url == "https://www.instagram.com/p/abc/"
    m = a.fetch_metrics(ctx, "M1", hours_since=24, hints={})
    assert (m.reach, m.likes, m.saves, m.views) == (1000, 50, 12, 1500)
    assert all("access_token=" not in str(r.url) for r in meta.requests if "graph.instagram.com/v" in str(r.url))


def test_publish_failures(meta):
    a = InstagramAdapter()
    ctx, _ = _ctx()
    with pytest.raises(AdapterError, match="public HTTPS"):
        a.publish(ctx, _payload(["http://localhost/x.jpg"]))
    meta.container_status = "ERROR"
    with pytest.raises(AdapterError, match="ERROR"):
        a.publish(ctx, _payload())
    meta.container_status = "IN_PROGRESS"
    with pytest.raises(AdapterError) as e:
        a.publish(ctx, _payload())
    assert e.value.retryable
    meta.container_status, meta.quota = "FINISHED", 100
    with pytest.raises(AdapterError, match="limit"):
        a.publish(ctx, _payload())
    assert meta.published == []


def test_token_expiry_and_refresh(meta):
    a = InstagramAdapter()
    expired, _ = _ctx({"access_token": "OLD", "expires_at": int(time.time()) - 10})
    with pytest.raises(AdapterError) as e:
        a.verify_connection(expired)
    assert e.value.reauth
    soon, saved = _ctx({"access_token": "LONG", "issued_at": int(time.time()) - 55 * 86400,
                        "expires_at": int(time.time()) + 2 * 86400, "permissions": []})
    a.verify_connection(soon)
    assert saved["access_token"] == "REFRESHED"


def test_personal_account_rejected(meta):
    meta.account_type = "PERSONAL"
    ctx, _ = _ctx()
    with pytest.raises(AdapterError, match="professional"):
        InstagramAdapter().verify_connection(ctx)


def test_oauth_state_validation_and_connect(monkeypatch, tmp_path):
    api = Api(make_client(monkeypatch, tmp_path, "live", INSTAGRAM_APP_ID="ig-123", INSTAGRAM_APP_SECRET="ig-shh",
                          META_APP_ID="123", META_APP_SECRET="shh",
                          BASE_URL="https://app.example.com"))
    api.register()
    fake = FakeMeta()
    net.set_transport(httpx.MockTransport(fake))
    # Wrong / missing state is rejected.
    r = api.c.get("/api/integrations/instagram/callback?state=forged&code=good-code", follow_redirects=False)
    assert r.status_code == 303 and "instagram=error" in r.headers["location"]
    start = api.post("/api/integrations/instagram/start").json()["authorize_url"]
    q = parse_qs(urlparse(start).query)
    assert urlparse(start).hostname == "www.instagram.com"
    assert q["client_id"] == ["ig-123"]
    assert q["scope"] == ["instagram_business_basic,instagram_business_content_publish,instagram_business_manage_insights"]
    assert q["enable_fb_login"] == ["0"]
    assert q["redirect_uri"] == ["https://app.example.com/api/integrations/instagram/callback"]
    state = q["state"][0]
    r = api.c.get(f"/api/integrations/instagram/callback?state={state}&code=good-code", follow_redirects=False)
    assert "instagram=connected" in r.headers["location"], r.headers["location"]
    # State is single-use.
    r = api.c.get(f"/api/integrations/instagram/callback?state={state}&code=good-code", follow_redirects=False)
    assert "instagram=error" in r.headers["location"]
    with session_scope() as db:
        conn = db.execute(select(ChannelConnection)).scalar_one()
        assert conn.status == "connected" and conn.account_label == "@aranya.test"
        assert "LONG" not in (conn.credentials_enc or "")  # encrypted at rest
    listing = api.get("/api/integrations").text
    assert "LONG" not in listing and "SHORT" not in listing
    facebook_start = api.post("/api/integrations/facebook/start").json()["authorize_url"]
    facebook_query = parse_qs(urlparse(facebook_start).query)
    assert urlparse(facebook_start).hostname == "www.facebook.com"
    assert facebook_query["redirect_uri"] == ["https://app.example.com/api/integrations/facebook/callback"]


def test_live_publish_end_to_end_with_mock_meta(monkeypatch, tmp_path):
    api = Api(make_client(monkeypatch, tmp_path, "live", INSTAGRAM_APP_ID="ig-123", INSTAGRAM_APP_SECRET="ig-shh",
                          META_APP_ID="123", META_APP_SECRET="shh",
                          BASE_URL="https://app.example.com", LIVE_PUBLISHING_ENABLED="true"))
    api.register()
    api.put("/api/brand", json={"name": "Live Brand", "location": "Bengaluru"})
    api.post("/api/products", json={"sku": "S1", "name": "Kabini 3-Seater Sofa", "category": "sofa", "price": "78500"})
    fake = FakeMeta()
    net.set_transport(httpx.MockTransport(fake))
    start = api.post("/api/integrations/instagram/start").json()["authorize_url"]
    state = parse_qs(urlparse(start).query)["state"][0]
    api.c.get(f"/api/integrations/instagram/callback?state={state}&code=good-code", follow_redirects=False)
    conn_id = api.get("/api/integrations").json()["connections"][0]["id"]
    item = _attach_photo(api, _new_item(api)["id"])
    api.post(f"/api/content/{item['id']}/submit")
    item = api.get(f"/api/content/{item['id']}").json()
    # Approval without publish authorisation → can't schedule to a live account.
    api.post(f"/api/content/{item['id']}/approve", json={"version": item["version"], "acknowledge_warnings": True})
    r = api.post(f"/api/content/{item['id']}/schedule", json={"connection_id": conn_id, "confirm": True})
    assert r.status_code == 400 and "authorise" in r.json()["message"]
    # Re-review with explicit authorisation; scheduling also needs confirmation.
    api.post(f"/api/content/{item['id']}/request-changes", json={"note": "authorise"})
    api.put(f"/api/content/{item['id']}", json={"caption": "Evening light on the Kabini 3-Seater Sofa."})
    api.post(f"/api/content/{item['id']}/submit")
    item = api.get(f"/api/content/{item['id']}").json()
    r = api.post(f"/api/content/{item['id']}/approve", json={"version": item["version"], "acknowledge_warnings": True,
                                                              "authorize_publish": True})
    assert r.status_code == 200, r.text
    r = api.post(f"/api/content/{item['id']}/schedule", json={"connection_id": conn_id})
    assert r.status_code == 400 and "Confirm" in r.json()["message"]
    r = api.post(f"/api/content/{item['id']}/schedule", json={"connection_id": conn_id, "confirm": True})
    assert r.status_code == 200, r.text
    with session_scope() as db:
        db.get(ContentItem, item["id"]).scheduled_at = utcnow()  # make it due now
    run_worker()
    done = api.get(f"/api/content/{item['id']}").json()
    assert done["status"] == "published" and done["external_id"] == "M1"
    assert done["publish_verified"] and done["external_url"].startswith("https://www.instagram.com/")
    assert done["attempts"][0]["live"] is True
    # The image URL handed to Meta is the public, unguessable asset URL.
    media_req = next(r for r in fake.requests if r.url.path.endswith("/media") and r.method == "POST")
    assert "https://app.example.com/api/public/assets/" in media_req.content.decode().replace("%3A", ":").replace("%2F", "/")
    run_worker()
    assert fake.published == ["M1"], "never published twice"
