"""Catalogue ingestion (mocked Poojaloka API), Facebook Pages adapter (mocked Graph API), creative rendering rules,
and platform-scoped experiment evaluation."""

from __future__ import annotations

import io
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from growthpilot.adapters.base import AdapterError, ConnectionContext, PublishPayload
from growthpilot.adapters.facebook import FacebookPageAdapter
from growthpilot.catalogue import poojaloka as pj
from growthpilot.creative.render import CreativeInput, Palette, render
from growthpilot.db import session_scope
from growthpilot.models import Asset, Brand, Product
from growthpilot.research import net
from growthpilot.services.validation import check_product_claims

from .conftest import Api, make_client, run_worker


def _jpeg(w=800, h=800) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 150, 60)).save(buf, "JPEG")
    return buf.getvalue()


def _raw(slug, name, price, mrp=None, stock=5, images=None, **kw):
    return {"id": slug, "slug": slug, "name": name, "nameKn": None, "price": price, "mrp": mrp or price,
            "images": images or ["https://cdn.pixabay.com/photo/x.jpg"], "stock": stock, "allowBackorder": True,
            "isBestSeller": False, "isDevoteeFavorite": False, "category": {"name": "Brass Collection"}, **kw}


PAGES = {1: [_raw("brass-lamp", "Brass Lamp 5 Face", 1200, 1500),
             _raw("own-photo", "Silver Kumkum Box", 900, images=["https://billing.poojaloka.com/uploads/a.jpeg"])],
         2: [_raw("bad-price", "White Gulganji 1 Kg", 1, stock=-2), _raw("chair", "chair", 230, 345, stock=0)]}


def _store_handler(state):
    def handler(req: httpx.Request):
        state["requests"].append(str(req.url))
        if req.url.host == "billing.poojaloka.com":
            return httpx.Response(200, content=_jpeg(), headers={"content-type": "image/jpeg"})
        path = req.url.path
        if path == "/api/products":
            page = int(req.url.params.get("page", "1"))
            return httpx.Response(200, json={"products": PAGES.get(page, []), "total": 4, "page": page,
                                             "pageSize": 100, "totalPages": 2})
        if path.startswith("/api/products/"):
            slug = path.rsplit("/", 1)[1]
            for rows in PAGES.values():
                for r in rows:
                    if r["slug"] == slug:
                        return httpx.Response(200, json={"product": r | {"price": state.get("new_price", r["price"])}})
            return httpx.Response(404, json={"error": "Not found"})
        if path == "/api/festivals":
            return httpx.Response(200, json={"festivals": [{"name": "Deepavali", "slug": "deepavali",
                                                            "itemsNeeded": ["Clay diyas", "Cotton wicks"]}]})
        return httpx.Response(404, json={"error": "Not found"})
    return handler


def _live_brand(monkeypatch, tmp_path):
    api = Api(make_client(monkeypatch, tmp_path, "live", LLM_API_KEY="x"))
    api.register()
    api.put("/api/brand", json={"name": "Poojaloka", "location": "Bengaluru", "website": "https://poojaloka.com"})
    with session_scope() as db:
        b = db.execute(select(Brand)).scalars().first()
        b.catalogue_source = {"kind": "poojaloka_store_api", "base_url": "https://api.poojaloka.com/api"}
    net.set_resolver(lambda host, port, proto=0: [(2, 1, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(pj, "_sleep", lambda _s: None)
    return api


def test_catalogue_sync_provenance_quality_and_photos(monkeypatch, tmp_path):
    api = _live_brand(monkeypatch, tmp_path)
    state = {"requests": []}
    net.set_transport(httpx.MockTransport(_store_handler(state)))
    r = api.post("/api/catalogue/sync", json={})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["created"] == 4 and res["photos_downloaded"] == 1 and not res["snapshot"]
    assert any("page=2" in u for u in state["requests"])  # pagination
    assert not any("pixabay" in u for u in state["requests"])  # stock images are never downloaded
    prods = {p["sku"]: p for p in api.get("/api/products").json()}
    lamp = prods["brass-lamp"]
    assert lamp["source_url"] == "https://poojaloka.com/product/brass-lamp" and lamp["retrieved_at"]
    assert lamp["freshness"]["state"] == "fresh" and lamp["materials"] == ["brass"] and lamp["mrp"] == 1500
    assert lamp["image_kind"] == "stock_photo" and lamp["image_asset_id"] is None
    codes = lambda sku: {q["code"] for q in prods[sku]["data_quality"]}  # noqa: E731
    assert {"mrp_above_price", "no_product_photo"} <= codes("brass-lamp")
    assert {"price_suspicious", "stock_negative"} <= codes("bad-price")
    assert "possible_test_listing" in codes("chair")
    own = prods["own-photo"]
    assert own["image_kind"] == "product_photo" and own["image_asset_id"]
    with session_scope() as db:
        a = db.get(Asset, own["image_asset_id"])
        assert a.source == "catalogue" and a.origin_url.startswith("https://billing.poojaloka.com/") and a.sha256

    # Refresh before promotion detects a price change at the source.
    state["new_price"] = 1100
    out = api.post("/api/catalogue/refresh", json={"product_ids": [lamp["id"]]}).json()
    assert out["refreshed"] == ["brass-lamp"]
    assert {"field": "price", "old": "1200.00", "new": "1100.00", "sku": "brass-lamp"} in out["changed"]

    # A full sync deactivates products no longer listed.
    PAGES_BACKUP = dict(PAGES)
    try:
        PAGES[2] = []
        res = api.post("/api/catalogue/sync", json={"full": True}).json()
        assert res["deactivated"] == 2
    finally:
        PAGES.update(PAGES_BACKUP)


def test_demo_catalogue_sync_uses_snapshot_without_network(poojaloka_seeded):
    api = poojaloka_seeded

    def boom(req):
        raise AssertionError(f"demo mode made an external request to {req.url}")

    net.set_transport(httpx.MockTransport(boom))
    r = api.post("/api/catalogue/sync", json={})
    assert r.status_code == 200 and r.json()["snapshot"] is True and "snapshot" in r.json()["note"]


def test_quoted_festival_items_are_not_material_claims():
    b = Brand(name="P", approved_claims=[], restricted_claims=[], prohibited_messaging=[],
              catalogue_source={"festivals": [{"itemsNeeded": ["Cotton wicks"]}]})
    diya = Product(sku="d", name="Clay Diya", category="Mud Items", materials=["clay"], finishes=[], price=None,
                   verified_attributes={}, variants=[], data_quality=[])
    ok = check_product_claims("You'll need: • Cotton wicks • Clay diyas", b, [diya], [diya])
    assert not [c for c in ok if c.status == "fail"]
    bad = check_product_claims("Our cotton diyas", b, [diya], [diya])
    assert any(c.code == "material" and c.status == "fail" for c in bad)
    health = check_product_claims("Kumkumadi taila cures skin problems.", b, [diya], [diya])
    assert any(c.code == "health_claim" and c.status == "fail" for c in health)


def test_renderer_sizes_glyphs_and_text_fit():
    pal = Palette.from_colors(["#6B1420", "#C99A3D", "#FBF3E7", "#2B1810"])
    for (w, h) in ((1080, 1350), (1000, 1500), (1600, 900), (1200, 1200)):
        r = render(CreativeInput(template="product_showcase", width=w, height=h, headline="Brass Pooja Petti",
                                 subline="Brass Collection", price_text="₹4,600", cta="View the product",
                                 product_name="Brass Pooja Petti Size 6", brand_name="Poojaloka", palette=pal))
        img = Image.open(io.BytesIO(r.data))
        assert img.size == (w, h) and img.format == "JPEG" and r.ok, r.checks
    kn = render(CreativeInput(template="awareness", width=1080, height=1350, headline="ದೀಪಾವಳಿ ಶುಭಾಶಯಗಳು",
                              palette=pal))
    assert any(c["code"] == "glyphs" and c["status"] == "fail" for c in kn.checks)
    long = render(CreativeInput(template="awareness", width=1080, height=1350, headline="word " * 80, palette=pal))
    assert any(c["code"] == "text_fit" and c["status"] == "fail" for c in long.checks)
    photo = render(CreativeInput(template="product_showcase", width=1080, height=1350, headline="Silver Kumkum Box",
                                 photo=_jpeg(), palette=pal))
    assert "Real product photo from the catalogue." in photo.notes and photo.ok


def _fb_ctx(saved: list) -> ConnectionContext:
    return ConnectionContext(connection_id=1, account_id="111", config={},
                             credentials={"user_token": "u", "pages": [{"id": "111", "name": "Poojaloka",
                                                                        "access_token": "PAGE-TOKEN"}]},
                             save_credentials=saved.append)


def test_facebook_adapter_publish_verify_metrics(monkeypatch, tmp_path):
    make_client(monkeypatch, tmp_path, "live", META_APP_ID="app", META_APP_SECRET="secret",
                BASE_URL="https://app.example.com")
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, dict(parse_qs(req.content.decode())) if req.content else {}))
        assert req.headers.get("authorization") == "Bearer PAGE-TOKEN"
        p = req.url.path
        if p.endswith("/111/photos"):
            body = parse_qs(req.content.decode())
            if body.get("published") == ["false"]:
                return httpx.Response(200, json={"id": f"ph{len(calls)}"})
            return httpx.Response(200, json={"id": "ph1", "post_id": "111_999"})
        if p.endswith("/111/feed"):
            return httpx.Response(200, json={"id": "111_555"})
        if p.endswith("/111_999"):
            if "reactions" in req.url.params.get("fields", ""):
                return httpx.Response(200, json={"reactions": {"summary": {"total_count": 12}},
                                                 "comments": {"summary": {"total_count": 3}}, "shares": {"count": 2}})
            return httpx.Response(200, json={"id": "111_999", "permalink_url": "https://facebook.com/111/posts/999",
                                             "created_time": "2026-10-25T04:00:00+0000"})
        if p.endswith("/111"):
            return httpx.Response(200, json={"id": "111", "name": "Poojaloka", "link": "https://facebook.com/111"})
        return httpx.Response(404, json={"error": {"message": "nope", "code": 100}})

    net.set_transport(httpx.MockTransport(handler))
    net.set_resolver(lambda host, port, proto=0: [(2, 1, 6, "", ("93.184.216.34", 443))])
    fb = FacebookPageAdapter()
    ctx = _fb_ctx([])
    assert fb.verify_connection(ctx).label == "Poojaloka"
    single = fb.publish(ctx, PublishPayload(content_id=1, version=1, platform="facebook", content_type="post",
                                            caption="Hello", image_urls=["https://app.example.com/a.jpg"]))
    assert single.external_id == "111_999"
    multi = fb.publish(ctx, PublishPayload(content_id=2, version=1, platform="facebook", content_type="carousel",
                                           caption="Hi", image_urls=["https://app.example.com/a.jpg",
                                                                     "https://app.example.com/b.jpg"]))
    assert multi.external_id == "111_555"
    feed = [c for c in calls if c[1].endswith("/111/feed")][0]
    assert "attached_media[0]" in feed[2] and json.loads(feed[2]["attached_media[1]"][0])["media_fbid"]
    st = fb.publication_status(ctx, "111_999")
    assert st.state == "published" and st.url.startswith("https://facebook.com/")
    m = fb.fetch_metrics(ctx, "111_999", hours_since=5, hints={})
    assert m.likes == 12 and m.comments == 3 and m.shares == 2 and m.reach is None
    with pytest.raises(AdapterError, match="public HTTPS"):
        fb.publish(ctx, PublishPayload(content_id=3, version=1, platform="facebook", content_type="post",
                                       caption="x", image_urls=["http://localhost/a.jpg"]))


def test_facebook_oauth_exchange_keeps_only_pages_with_create_content(monkeypatch, tmp_path):
    make_client(monkeypatch, tmp_path, "live", META_APP_ID="app", META_APP_SECRET="secret",
                BASE_URL="https://app.example.com")

    def handler(req: httpx.Request):
        if req.url.path.endswith("/oauth/access_token"):
            if req.url.params.get("grant_type") == "fb_exchange_token":
                return httpx.Response(200, json={"access_token": "LONG", "expires_in": 5_000_000})
            assert req.url.params["redirect_uri"] == "https://app.example.com/api/integrations/facebook/callback"
            return httpx.Response(200, json={"access_token": "SHORT"})
        if req.url.path.endswith("/me/accounts"):
            return httpx.Response(200, json={"data": [
                {"id": "1", "name": "Mine", "access_token": "P1", "tasks": ["CREATE_CONTENT", "MANAGE"]},
                {"id": "2", "name": "Read only", "access_token": "P2", "tasks": ["ANALYZE"]}]})
        return httpx.Response(404)

    net.set_transport(httpx.MockTransport(handler))
    net.set_resolver(lambda host, port, proto=0: [(2, 1, 6, "", ("93.184.216.34", 443))])
    creds = FacebookPageAdapter().exchange_code("code")
    assert [p["id"] for p in creds["pages"]] == ["1"] and creds["expires_at"] > time.time()
    assert "scope=pages_show_list" in FacebookPageAdapter().authorize_url("st")


def test_experiment_is_evaluated_within_one_platform(poojaloka_seeded):
    api = poojaloka_seeded
    exps = api.get("/api/experiments").json()
    hist = next(e for e in exps if "synthetic history" in e["name"])
    r = api.post(f"/api/experiments/{hist['id']}/evaluate")
    assert r.status_code == 200
    e = r.json()
    assert e["result"]["platform"] == "instagram"  # Facebook copies of the same posts are excluded from the A/B
    assert e["result"]["per_variant"]["A"]["n"] == 4 and e["result"]["per_variant"]["B"]["n"] == 4
    assert e["confidence"] in ("low", "medium", "high")
    assert "permutation test" in e["comparison_method"]
    run_worker()


def test_renderer_omits_unsupported_emoji_without_drawing_missing_glyph_boxes():
    result = render(CreativeInput(template="festive", width=1080, height=1350,
                                  headline="Light up Deepavali \U0001fa94\ufe0f"))
    headline = next(layer for layer in result.layers if layer.kind == "headline")
    assert headline.text == "Light up Deepavali"
    assert any(check["code"] == "unsupported_emoji" and check["status"] == "warn" for check in result.checks)
    assert not any(check["code"] == "glyphs" and check["status"] == "fail" for check in result.checks)
