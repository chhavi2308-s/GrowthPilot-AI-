"""Research-driven 5-day campaign generation. Brave, Groq and Gemini are mocked at the HTTP layer (Gemini through
the real google-genai SDK); nothing here proves the live APIs work."""

from __future__ import annotations

import base64
import io
import json
import re
from datetime import timedelta

import httpx
import pytest
from PIL import Image
from sqlalchemy import select, update

from growthpilot import config
from growthpilot.db import session_scope
from growthpilot.models import (
    Asset,
    CampaignGeneration,
    CampaignGenerationDay,
    ContentItem,
    UsageRecord,
    Workspace,
    utcnow,
)
from growthpilot.research import net
from growthpilot.services import campaign_generation as cg

from .conftest import Api, make_client

GOAL = "Create a 5-day Diwali marketing campaign for my store."


def png(w: int, h: int, color=(180, 60, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


IMG_2K = png(1638, 2048)  # 4:5 at 2K


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(cg, "_sleep", lambda _s: None)


class Fake:
    """Mocked Brave + Groq + Gemini. Each counts its requests; behaviour can be changed per test."""

    def __init__(self):
        self.brave: list[str] = []
        self.groq: list[dict] = []
        self.gemini: list[dict] = []
        self.gemini_headers: list[dict] = []
        self.brave_fail: Exception | int | None = None
        self.strategy_days: int | None = None  # None: as many days as requested
        self.image_for_day: dict[int, object] = {}  # day → bytes | int status | "blocked"
        self.image_default: object = IMG_2K
        self._image_calls_per_prompt: dict[str, int] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        host = req.url.host
        if host == "api.search.brave.com":
            q = req.url.params.get("q")
            self.brave.append(q)
            if isinstance(self.brave_fail, Exception):
                raise self.brave_fail
            if isinstance(self.brave_fail, int):
                return httpx.Response(self.brave_fail)
            n = len(self.brave)
            return httpx.Response(200, json={"web": {"results": [
                {"title": f"Diwali trends {n}", "url": f"https://news.example.org/diwali-{n}",
                 "description": f"Snippet {n} about Diwali shopping.", "age": "2026-10-01"}]}})
        if host == "api.groq.com":
            body = json.loads(req.content)
            self.groq.append(body)
            return httpx.Response(200, json=self._groq(body))
        if host == "generativelanguage.googleapis.com":
            body = json.loads(req.content)
            self.gemini.append(body)
            self.gemini_headers.append(dict(req.headers))
            prompt = body["input"][0]["content"][0]["text"] if "content" in body["input"][0] else ""
            day = int(re.search(r"DAY(\d)", prompt).group(1)) if re.search(r"DAY(\d)", prompt) else 0
            out = self.image_for_day.get(day, self.image_default)
            if isinstance(out, list):  # a sequence of outcomes, one per call
                out = out.pop(0) if len(out) > 1 else out[0]
            if isinstance(out, int):
                return httpx.Response(out, json={"error": {"code": out, "message": "provider says no"}})
            if out == "blocked":
                return httpx.Response(200, json={"id": "i", "status": "failed", "steps": []})
            return httpx.Response(200, json={"id": "i", "status": "completed", "steps": [
                {"type": "model_output", "content": [{"type": "image", "mime_type": "image/png",
                                                      "data": base64.b64encode(out).decode()}]}]})
        return httpx.Response(404)

    def _groq(self, body: dict) -> dict:
        name = body["response_format"]["json_schema"]["name"]
        data = json.loads(re.search(r"<data>\n(.*?)\n</data>", body["messages"][1]["content"], re.S).group(1))
        if name == "StrategyOut":
            pids = [p["id"] for p in data["catalogue"]]
            src = [s["id"] for s in data["research_sources"]]
            out = {"title": "Diwali Lights Week", "objective": "Grow festive sales", "audience": "Families",
                   "theme": "Festival of lights at home", "voice": "Warm", "positioning": "Handmade quality",
                   "platforms": ["instagram"], "product_ids": pids[:2] + [99999],
                   "findings": [{"finding": "Shoppers plan purchases early", "source_ids": src[:1]},
                                {"finding": "Unsourced claim", "source_ids": ["S99"]}],
                   "assumptions": ["Audience follows on Instagram"],
                   "days": [{"day": d, "objective": f"Objective {d}", "concept": f"Concept {d}",
                             "caption_brief": "Brief", "cta": "Shop now", "product_ids": [pids[0], 4242],
                             "source_ids": src[:1] + ["S77"]} for d in range(1, (self.strategy_days or data["days"]) + 1)]}
        else:
            out = {"visual_style": "Warm gold bokeh", "days": [
                {"day": d["day"], "hook": f"Day {d['day']} hook", "caption": f"Caption for day {d['day']}",
                 "hashtags": ["#Diwali", "Festive"], "cta": "Shop now",
                 "image": {"subject": f"DAY{d['day']} diyas on a table", "style": "photographic",
                           "composition": "space at bottom", "palette": "gold", "negative": "no text"}}
                for d in data["plan"]["days"]]}
        return {"choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": json.dumps(out)}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 600}}


def _live(monkeypatch, tmp_path, fake: Fake, **extra) -> Api:
    env = {"GROQ_API_KEY": "gsk-test", "BRAVE_SEARCH_API_KEY": "brave-test", "GEMINI_API_KEY": "gem-test",
           "IMAGE_GENERATION_PROVIDER": "gemini"} | extra
    api = Api(make_client(monkeypatch, tmp_path, "live", **env))
    api.register()
    api.put("/api/brand", json={"name": "Diya House", "location": "Bengaluru, India", "colors": ["#7A1F1F", "#D4A017"],
                                "description": "Handmade lamps and decor"})
    for sku, name in (("L1", "Festive Diya Set"), ("L2", "Garden Lantern")):
        api.post("/api/products", json={"sku": sku, "name": name, "category": "lamps"})
    net.set_resolver(lambda host, port, proto=0: [(2, 1, 6, "", ("93.184.216.34", 443))])
    net.set_transport(httpx.MockTransport(fake))
    return api


def drain(max_units: int = 100) -> int:
    with session_scope() as db:
        return cg.process_queued(db, max_units=max_units)


def gen_of(api: Api, gid: int) -> dict:
    r = api.get(f"/api/campaign-generations/{gid}")
    assert r.status_code == 200, r.text
    return r.json()


def gen_of_campaign(gid: int) -> int:
    with session_scope() as db:
        return db.get(CampaignGeneration, gid).campaign_id


def start(api: Api, **body) -> dict:
    r = api.post("/api/campaign-generations", json={"goal": GOAL} | body)
    assert r.status_code == 200, r.text
    return r.json()


# ── research ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_research_plan_queries_are_focused_and_deduplicated(monkeypatch, tmp_path):
    make_client(monkeypatch, tmp_path, "demo")
    from types import SimpleNamespace as NS

    brand = NS(location="Bengaluru, India", description="Lamps", name="Diya House")
    products = [NS(category="lamps"), NS(category="lamps"), NS(category="decor")]
    plan = cg.research_plan(GOAL, brand, products, 5, year=2026)
    queries = [q["query"] for q in plan["questions"]]
    assert plan["occasion"] == "Diwali" and plan["category"] == "lamps" and plan["country"] == "IN"
    assert 3 <= len(queries) <= 5 and len(set(q.lower() for q in queries)) == len(queries)
    assert all("Diwali" in q for q in queries) and any("2026" in q for q in queries)
    assert all(q["question"] for q in plan["questions"])
    monkeypatch.setenv("CAMPAIGN_MAX_SEARCHES", "3")
    config.get_settings.cache_clear()
    assert len(cg.research_plan(GOAL, brand, products, 5)["questions"]) == 3


def test_full_live_campaign_with_sources_strategy_captions_and_2k_images(monkeypatch, tmp_path):
    fake = Fake()
    api = _live(monkeypatch, tmp_path, fake)
    gid = start(api)["id"]
    drain()
    g = gen_of(api, gid)
    assert g["status"] == "completed" and g["stage"] == "done", g["error"]
    # Research: at most five Brave requests for the whole campaign, sources keep their URLs.
    assert 3 <= len(fake.brave) <= 5
    sources = g["research"]["sources"]
    assert sources and all(s["url"].startswith("https://news.example.org/") for s in sources)
    assert all("snippet" not in s for s in sources)  # no Brave storage rights by default: snippets dropped
    # Strategy: one Groq call, exactly five days, only real products and real sources.
    names = [b["response_format"]["json_schema"]["name"] for b in fake.groq]
    assert names == ["StrategyOut", "CreativeOut"]
    plan = g["strategy"]
    assert [d["day"] for d in plan["days"]] == [1, 2, 3, 4, 5]
    assert 99999 not in plan["product_ids"] and all(4242 not in d["product_ids"] for d in plan["days"])
    assert all("S77" not in d["source_ids"] for d in plan["days"])
    assert [f["finding"] for f in plan["findings"]] == ["Shoppers plan purchases early"]
    assert "Unsourced claim" in plan["assumptions"]  # a "finding" without a real source is an assumption
    # Text: five posts in review with captions, hashtags and image prompts that exclude products and text.
    days = g["day_items"]
    assert [d["status"] for d in days] == ["completed"] * 5
    assert all(d["content"]["status"] == "in_review" and d["content"]["caption"] for d in days)
    assert all(d["content"]["hashtags"] == ["#Diwali", "#Festive"] for d in days)
    assert all("no products" in d["image_prompt"]["prompt"] for d in days)
    # Images: one Gemini request per day, 2K, 4:5, the verified model, through the SDK with the key in a header.
    assert len(fake.gemini) == 5
    for body, headers in zip(fake.gemini, fake.gemini_headers, strict=True):
        assert body["model"] == "gemini-nano-banana-2.1" and body["store"] is False
        assert body["response_format"] == {"type": "image", "mime_type": "image/png", "aspect_ratio": "4:5",
                                           "image_size": "2K"}
        assert headers["x-goog-api-key"] == "gem-test"
    with session_scope() as db:
        for d in days:
            ai = db.get(Asset, d["ai_asset"]["id"])
            final = db.get(Asset, d["asset"]["id"])
            assert ai.source == "ai_background" and ai.meta["image_size"] == "2K" and ai.meta["cost_usd"] == 0.0504
            assert (final.width, final.height) == (1638, 2048) and final.meta["background"] == "ai_generated"
            assert final.meta["generation_id"] == gid
    u = g["usage"]
    assert u["brave_requests"] == len(fake.brave) and u["llm_requests"] == 2 and u["llm_tokens"] == 3000
    assert u["image_requests"] == 5 and u["images_generated"] == 5
    assert u["cost_usd"] == pytest.approx(5 * 0.0504 + len(fake.brave) * 0.005)


def test_brave_request_budget_per_campaign(monkeypatch, tmp_path):
    fake = Fake()
    api = _live(monkeypatch, tmp_path, fake, CAMPAIGN_MAX_SEARCHES="2")
    start(api)
    drain(max_units=1)
    assert len(fake.brave) == 2
    # Regenerating a day never searches again.
    drain()
    gid = api.get("/api/campaigns").json()[0]["id"]
    g = api.get(f"/api/campaigns/{gid}/generation").json()["generation"]
    api.post(f"/api/campaign-generations/{g['id']}/days/2/retry", json={"regenerate": True})
    drain()
    assert len(fake.brave) == 2 and len(fake.gemini) == 6


def test_brave_timeout_is_retried_once_then_fails_without_inventing_findings(monkeypatch, tmp_path):
    fake = Fake()
    fake.brave_fail = httpx.ReadTimeout("slow")
    api = _live(monkeypatch, tmp_path, fake)
    gid = start(api)["id"]
    drain()
    g = gen_of(api, gid)
    assert g["status"] == "failed" and g["stage"] == "research"
    assert "Research failed" in g["error"] and "no findings were invented" in g["error"]
    assert len(fake.brave) <= 2 * 5 and g["strategy"] is None and fake.groq == []
    # The user retries after Brave recovers; the campaign continues from research.
    fake.brave_fail = None
    assert api.post(f"/api/campaign-generations/{gid}/retry").status_code == 200
    drain()
    assert gen_of(api, gid)["status"] == "completed"


def test_brave_quota_exhausted_is_a_clear_error(monkeypatch, tmp_path):
    fake = Fake()
    api = _live(monkeypatch, tmp_path, fake)
    with session_scope() as db:
        db.execute(update(Workspace).values(search_requests_per_day=0))
    gid = start(api)["id"]
    drain()
    g = gen_of(api, gid)
    assert g["status"] == "failed" and "quota" in g["error"].lower() and fake.brave == []


def test_brave_storage_rights_keep_snippets(monkeypatch, tmp_path):
    fake = Fake()
    api = _live(monkeypatch, tmp_path, fake, BRAVE_STORAGE_RIGHTS="true")
    gid = start(api)["id"]
    drain()
    assert all(s["snippet"].startswith("Snippet") for s in gen_of(api, gid)["research"]["sources"])


# ── strategy and text ────────────────────────────────────────────────────────────────────────────────────────

def test_invalid_strategy_is_retried_once_then_fails_and_can_be_retried(monkeypatch, tmp_path):
    fake = Fake()
    fake.strategy_days = 4  # the model returns four days for a five-day request
    api = _live(monkeypatch, tmp_path, fake)
    gid = start(api)["id"]
    drain()
    g = gen_of(api, gid)
    assert g["status"] == "failed" and g["stage"] == "strategy" and "exactly 5 days" in g["error"]
    assert len(fake.groq) == cg.STAGE_ATTEMPTS  # never retried indefinitely
    brave_before = len(fake.brave)
    fake.strategy_days = None
    api.post(f"/api/campaign-generations/{gid}/retry")
    drain()
    assert gen_of(api, gid)["status"] == "completed" and len(fake.brave) == brave_before  # research reused


def test_malformed_strategy_json_fails_cleanly(monkeypatch, tmp_path):
    fake = Fake()
    api = _live(monkeypatch, tmp_path, fake)
    original = fake._groq
    fake._groq = lambda body: {"choices": [{"finish_reason": "stop", "message": {"content": "{not json"}}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 2}} \
        if body["response_format"]["json_schema"]["name"] == "StrategyOut" else original(body)
    gid = start(api)["id"]
    drain()
    g = gen_of(api, gid)
    assert g["status"] == "failed" and g["stage"] == "strategy" and "attempts" in g["error"]


# ── images: validation, partial success, retries, restarts ──────────────────────────────────────────────────

@pytest.mark.parametrize("bad,message", [(png(800, 1000), "smaller than the requested 2K"),
                                         (png(2048, 2048), "not the requested 4:5"),
                                         (b"not an image at all", "isn't a JPEG or PNG")])
def test_returned_images_are_validated(monkeypatch, tmp_path, bad, message):
    fake = Fake()
    fake.image_default = bad
    api = _live(monkeypatch, tmp_path, fake, CAMPAIGN_IMAGE_ATTEMPTS="1")
    gid = start(api, days=1)["id"]
    drain()
    d = gen_of(api, gid)["day_items"][0]
    assert d["status"] == "failed" and message in d["error"] and d["asset"] is None


def test_one_failed_day_keeps_the_others_and_retries_alone(monkeypatch, tmp_path):
    fake = Fake()
    fake.image_for_day = {3: "blocked"}
    api = _live(monkeypatch, tmp_path, fake)
    gid = start(api)["id"]
    drain()
    g = gen_of(api, gid)
    assert g["status"] == "partial"
    assert [d["status"] for d in g["day_items"]] == ["completed", "completed", "failed", "completed", "completed"]
    assert "safety" in g["day_items"][2]["error"]
    assert g["day_items"][2]["content"]["status"] == "draft"  # its text is kept for the retry
    assert len(fake.gemini) == 5  # a safety block isn't retried automatically
    done = {d["day"]: d["asset"]["id"] for d in g["day_items"] if d["asset"]}
    fake.image_for_day = {}
    r = api.post(f"/api/campaign-generations/{gid}/days/3/retry", json={})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    drain()
    g = gen_of(api, gid)
    assert g["status"] == "completed" and len(fake.gemini) == 6  # only day 3 was requested again
    assert {d["day"]: d["asset"]["id"] for d in g["day_items"] if d["day"] != 3} == done


def test_transient_image_errors_are_retried_within_the_day_limit(monkeypatch, tmp_path):
    fake = Fake()
    fake.image_for_day = {1: [429, 503, IMG_2K]}
    api = _live(monkeypatch, tmp_path, fake, CAMPAIGN_IMAGE_ATTEMPTS="2")
    gid = start(api, days=1)["id"]
    drain()
    d = gen_of(api, gid)["day_items"][0]
    assert d["status"] == "failed" and "HTTP 503" in d["error"] and d["attempts"] == 2
    assert len(fake.gemini) == 2  # exactly one HTTP request per counted attempt: the SDK never retries by itself
    api.post(f"/api/campaign-generations/{gid}/days/1/retry", json={})
    drain()
    d = gen_of(api, gid)["day_items"][0]
    assert d["status"] == "completed" and d["attempts"] == 3


def test_failed_poster_reuses_the_paid_image_on_retry(monkeypatch, tmp_path):
    fake = Fake()
    api = _live(monkeypatch, tmp_path, fake)
    calls = {"n": 0}
    real = cg._compose

    def flaky(db, gen, row, item, background):
        calls["n"] += 1
        if calls["n"] == 1:
            raise cg.WorkflowError("On-image text fails product checks: test")
        return real(db, gen, row, item, background)

    monkeypatch.setattr(cg, "_compose", flaky)
    gid = start(api, days=1)["id"]
    drain()
    d = gen_of(api, gid)["day_items"][0]
    assert d["status"] == "failed" and d["ai_asset"] is not None and len(fake.gemini) == 1
    api.post(f"/api/campaign-generations/{gid}/days/1/retry", json={})
    drain()
    d = gen_of(api, gid)["day_items"][0]
    assert d["status"] == "completed" and len(fake.gemini) == 1  # no second billable request
    with session_scope() as db:
        assert db.execute(select(UsageRecord).where(UsageRecord.provider == "gemini-image")).scalars().all()


def test_restart_keeps_finished_days_and_never_resends_an_interrupted_request(monkeypatch, tmp_path):
    fake = Fake()
    api = _live(monkeypatch, tmp_path, fake)
    gid = start(api)["id"]
    drain(max_units=5)  # research, strategy, text, day 1, day 2
    with session_scope() as db:  # simulate a worker that died during day 3's request
        row = db.execute(select(CampaignGenerationDay).where(CampaignGenerationDay.generation_id == gid,
                                                             CampaignGenerationDay.day == 3)).scalar_one()
        row.status, row.claimed_at = "generating", utcnow() - timedelta(hours=1)
        gen = db.get(CampaignGeneration, gid)
        gen.status, gen.claimed_at = "running", utcnow() - timedelta(hours=1)
    assert drain() == 0  # nothing runs while the dead worker still holds it
    with session_scope() as db:
        assert cg.recover_stale(db) == 2
    drain()
    g = gen_of(api, gid)
    assert [d["status"] for d in g["day_items"]] == ["completed", "completed", "failed", "completed", "completed"]
    assert "Interrupted" in g["day_items"][2]["error"] and len(fake.gemini) == 4
    assert g["status"] == "partial"


def test_duplicate_generation_is_refused(monkeypatch, tmp_path):
    fake = Fake()
    api = _live(monkeypatch, tmp_path, fake)
    gid = start(api)["id"]
    r = api.post("/api/campaign-generations", json={"goal": GOAL})
    assert r.status_code == 400 and f"#{gid}" in r.json()["message"]
    with session_scope() as db:  # two workers can't claim the same generation
        db.execute(update(CampaignGeneration).values(status="running", claimed_at=utcnow()))
    assert drain() == 0


def test_budget_is_checked_before_starting_and_before_each_image(monkeypatch, tmp_path):
    fake = Fake()
    api = _live(monkeypatch, tmp_path, fake, CAMPAIGN_BUDGET_USD="0.10")
    r = api.post("/api/campaign-generations", json={"goal": GOAL})
    assert r.status_code == 400 and "budget" in r.json()["message"] and fake.brave == []
    est = api.get("/api/campaign-generations/estimate?days=1").json()
    assert est["within_budget"] and est["image_requests"] == 1 and est["price_per_image_usd"] == 0.0504
    # Within budget at the start, but the campaign budget is used up before day 2's image.
    monkeypatch.setenv("CAMPAIGN_BUDGET_USD", "0.10")
    gid = start(api, days=1)["id"]
    with session_scope() as db:
        from growthpilot.models import Campaign

        camp = db.get(Campaign, db.get(CampaignGeneration, gid).campaign_id)
        camp.budget_usd = 0.03  # less than one 2K image
    drain()
    d = gen_of(api, gid)["day_items"][0]
    assert d["status"] == "failed" and "Budget reached" in d["error"] and fake.gemini == []


def test_local_poster_fallback_after_a_failed_day(monkeypatch, tmp_path):
    fake = Fake()
    fake.image_default = 400
    api = _live(monkeypatch, tmp_path, fake)
    gid = start(api, days=2)["id"]
    drain()
    assert [d["status"] for d in gen_of(api, gid)["day_items"]] == ["failed", "failed"]
    r = api.post(f"/api/campaign-generations/{gid}/days/1/local-poster")
    assert r.status_code == 200, r.text
    d = r.json()["day_items"][0]
    assert d["status"] == "completed" and d["asset"]["background"] == "template"
    assert d["content"]["status"] == "in_review"


def test_without_image_provider_live_uses_the_local_renderer_and_says_so(monkeypatch, tmp_path):
    fake = Fake()
    api = _live(monkeypatch, tmp_path, fake, IMAGE_GENERATION_PROVIDER="none")
    est = api.get("/api/campaign-generations/estimate?days=5").json()
    assert est["image_mode"] == "local" and est["image_requests"] == 0
    gid = start(api)["id"]
    drain()
    g = gen_of(api, gid)
    assert g["image_mode"] == "local" and g["status"] == "completed" and fake.gemini == []


# ── approvals, editing, security, demo ───────────────────────────────────────────────────────────────────────

def test_edit_caption_reject_one_post_and_approve_the_campaign(monkeypatch, tmp_path):
    fake = Fake()
    api = _live(monkeypatch, tmp_path, fake)
    gid = start(api)["id"]
    drain()
    days = gen_of(api, gid)["day_items"]
    first = days[0]["content"]
    r = api.put(f"/api/content/{first['id']}", json={"caption": "Edited caption for day one."})
    assert r.status_code == 200 and r.json()["version"] == first["version"] + 1  # a new version, still in review
    second = days[1]["content"]
    r = api.post(f"/api/content/{second['id']}/request-changes", json={"note": "Warmer tone"})
    assert r.status_code == 200
    r = api.post(f"/api/campaign-generations/{gid}/approve-all", json={"acknowledge_warnings": True})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert sorted(res["approved_days"]) == [1, 3, 4, 5] and [s["day"] for s in res["skipped"]] == [2]
    with session_scope() as db:
        items = db.execute(select(ContentItem).where(ContentItem.id.in_([d["content"]["id"] for d in days])))
        statuses = {i.id: i.status for i in items.scalars()}
    assert statuses[second["id"]] == "changes_requested"
    assert "scheduled" not in statuses.values() and "published" not in statuses.values()  # approval never publishes


def test_other_workspaces_and_viewers_cannot_touch_a_generation(monkeypatch, tmp_path):
    fake = Fake()
    owner = _live(monkeypatch, tmp_path, fake)
    gid = start(owner)["id"]
    drain()
    asset_id = gen_of(owner, gid)["day_items"][0]["asset"]["id"]
    # Missing CSRF header.
    r = owner.c.post("/api/campaign-generations", json={"goal": GOAL})
    assert r.status_code == 403
    # A viewer in the same workspace can read but not start, retry or approve.
    link = owner.post("/api/settings/invites", json={"email": "v@example.com", "role": "viewer"}).json()["link"]
    owner.post("/api/auth/logout")
    viewer = Api(owner.c)
    viewer.register(email="v@example.com", name="Viewer", invite=link.split("invite=")[1])
    assert viewer.get(f"/api/campaign-generations/{gid}").status_code == 200
    assert viewer.post("/api/campaign-generations", json={"goal": GOAL}).status_code == 403
    assert viewer.post(f"/api/campaign-generations/{gid}/days/1/retry", json={}).status_code == 403
    assert viewer.post(f"/api/campaign-generations/{gid}/approve-all", json={}).status_code == 403
    viewer.post("/api/auth/logout")
    # An owner of another workspace sees nothing: not the generation, its research, strategy or images.
    from growthpilot.models import Membership, User
    from growthpilot.security import hash_password

    from .conftest import PASSWORD

    with session_scope() as db:
        ws = Workspace(name="Other WS")
        user = User(email="other@example.com", name="Other", password_hash=hash_password(PASSWORD))
        db.add_all([ws, user])
        db.flush()
        db.add(Membership(user_id=user.id, workspace_id=ws.id, role="owner"))
    other = Api(owner.c)
    assert other.login("other@example.com").status_code == 200
    assert other.get(f"/api/campaign-generations/{gid}").status_code == 404
    assert other.get(f"/api/campaigns/{gen_of_campaign(gid)}/generation").json()["generation"] is None
    assert other.post(f"/api/campaign-generations/{gid}/days/1/retry", json={}).status_code == 404
    assert other.get(f"/api/assets/{asset_id}/file").status_code == 404


def test_demo_mode_generates_a_full_campaign_without_any_api_keys(monkeypatch, tmp_path, demo_seeded):
    def no_network(req):
        raise AssertionError(f"demo mode made an external call to {req.url}")

    net.set_transport(httpx.MockTransport(no_network))
    est = demo_seeded.get("/api/campaign-generations/estimate?days=5").json()
    assert est["image_mode"] == "local" and est["expected_cost_usd"] == 0 and est["brave_requests"] == 0
    gid = start(demo_seeded)["id"]
    drain()
    g = gen_of(demo_seeded, gid)
    assert g["status"] == "completed" and g["research"]["mode"] == "demo" and g["research"]["sources"] == []
    assert len(g["strategy"]["days"]) == 5 and g["strategy"]["findings"] == []
    assert all(d["status"] == "completed" and d["content"]["status"] == "in_review" for d in g["day_items"])
    assert g["usage"]["cost_usd"] == 0


def test_a_busy_image_slot_waits_instead_of_spinning(monkeypatch, tmp_path):
    fake = Fake()
    api = _live(monkeypatch, tmp_path, fake)  # CAMPAIGN_IMAGE_CONCURRENCY defaults to 1
    gid = start(api, days=2)["id"]
    assert drain(max_units=3) == 3  # research, strategy, text
    with session_scope() as db:  # another request holds the only image slot
        row = db.execute(select(CampaignGenerationDay).where(CampaignGenerationDay.generation_id == gid,
                                                             CampaignGenerationDay.day == 2)).scalar_one()
        row.status, row.claimed_at = "generating", utcnow()
    assert drain() == 1 and fake.gemini == []  # one check, then it waits for the next tick
    assert gen_of(api, gid)["status"] == "queued"
