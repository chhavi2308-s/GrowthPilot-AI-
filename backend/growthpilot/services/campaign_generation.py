"""Research-driven N-day campaign generation (default five days).

Stages, each stored before the next starts so nothing is lost on a failure or a worker restart:

1. **research**: 3–5 Brave searches for the whole campaign (deduplicated, cached, capped per campaign). Sources keep
   their URLs; without Brave storage rights the snippets are dropped once the strategy has used them.
2. **strategy**: one structured LLM call (Groq by default) for the whole plan, grounded in the research and the
   brand's own catalogue. Exactly N days, products only from the catalogue, sources only from the research.
3. **text**: one structured LLM call for every day's caption, hashtags, CTA and image prompt; one draft post per day.
4. **images**: one image per day, sequentially. Gemini (``CAMPAIGN_IMAGE_MODEL`` at ``CAMPAIGN_IMAGE_SIZE``) makes a
   background scene only; the poster renderer adds the real product photo and the text. Each day has its own
   status and is retried on its own; a failed day never re-runs the others.

The worker runs one unit of work at a time (a stage, or one day's image) and commits after each, claiming work with
atomic status updates. Nothing here publishes: posts go to the approval inbox.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import audit, usage
from ..catalogue import usable_products
from ..config import get_settings
from ..creative import imagegen
from ..llm import LLMError, LLMUnavailable, get_llm
from ..models import (
    Asset,
    Brand,
    Campaign,
    CampaignGeneration,
    CampaignGenerationDay,
    ContentItem,
    ContentStatus,
    Role,
    UsageRecord,
    utcnow,
)
from ..research import search as search_mod
from . import content as workflow
from .assets import store
from .errors import WorkflowError

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running")
TERMINAL = ("completed", "partial", "failed")
LEASE = timedelta(minutes=15)
STAGE_ATTEMPTS = 2  # an LLM stage is tried at most twice (invalid output or a transient error), then it fails
MAX_DAYS = 7
OCCASIONS = ("Diwali", "Deepavali", "Navratri", "Navaratri", "Dussehra", "Holi", "Eid", "Ramadan", "Christmas",
             "New Year", "Pongal", "Onam", "Raksha Bandhan", "Rakhi", "Ganesh Chaturthi", "Janmashtami", "Ugadi",
             "Valentine's Day", "Mother's Day", "Father's Day", "Black Friday", "Cyber Monday", "Thanksgiving",
             "Easter", "Lunar New Year", "Back to School", "Independence Day")
COUNTRIES = {"india": "IN", "united states": "US", "usa": "US", "united kingdom": "GB", "uk": "GB",
             "canada": "CA", "australia": "AU", "singapore": "SG", "uae": "AE", "united arab emirates": "AE"}

_sleep = time.sleep  # tests replace this


# ── structured outputs ─────────────────────────────────────────────────────────────────────────────────────────
class Finding(BaseModel):
    finding: str = Field(description="One sourced insight in your own words (no statistics unless in a source).")
    source_ids: list[str] = Field(description="Ids of the research sources (e.g. S1) that support it.")


class DayPlan(BaseModel):
    day: int
    objective: str
    concept: str = Field(description="The creative concept for this day's post.")
    caption_brief: str
    cta: str
    product_ids: list[int] = Field(description="Catalogue product ids to feature (only ids from the catalogue).")
    source_ids: list[str] = Field(description="Research source ids this day relies on, if any.")


class StrategyOut(BaseModel):
    title: str
    objective: str
    audience: str
    theme: str
    voice: str
    positioning: str
    platforms: list[str]
    product_ids: list[int]
    findings: list[Finding] = Field(description="Insights drawn only from the research sources, with their ids.")
    assumptions: list[str] = Field(description="Everything you assumed without a source.")
    days: list[DayPlan]


class ImagePrompt(BaseModel):
    subject: str = Field(description="The scene or background. Never the product itself, people's faces or text.")
    style: str
    composition: str = Field(description="Leave clear space where the real product photo and text will go.")
    palette: str
    negative: str


class DayCreative(BaseModel):
    day: int
    hook: str = Field(description="Poster headline, under 70 characters.")
    caption: str
    hashtags: list[str]
    cta: str = Field(description="Under 30 characters.")
    image: ImagePrompt


class CreativeOut(BaseModel):
    visual_style: str = Field(description="One visual style shared by every day, for consistency.")
    days: list[DayCreative]


SYSTEM = ("You are GrowthPilot's {role} for one brand. Use only the brand profile, the catalogue and the research "
          "sources given. Never invent prices, discounts, offers, stock, testimonials, product features or "
          "statistics. Separate what the sources say from your assumptions. Output must match the schema.")
NEGATIVE = ("no text, no letters, no logos, no watermarks, no products or product lookalikes, no people's faces, "
            "no deities or religious idols")


# ── starting, estimating and retrying ──────────────────────────────────────────────────────────────────────────
def image_mode() -> str:
    s = get_settings()
    return "ai" if (s.is_live and s.image_generation_configured) else "local"


def estimate(days: int) -> dict:
    """Requests and cost for one campaign before it starts (the budget check uses the expected cost)."""
    s = get_settings()
    mode = image_mode()
    searches = 0 if s.is_demo else s.campaign_max_searches
    price = imagegen.image_price(s.campaign_image_model, s.campaign_image_size) if mode == "ai" else 0.0
    brave = searches * usage.unit_cost("brave:web_search")
    llm_cost = 0.0
    if s.is_live and s.active_llm_provider == "groq" and s.groq_tier == "free":
        llm_cost = 0.0
    elif s.is_live and s.llm_configured:
        llm_cost = 2 * usage.estimate_llm_call(s.active_llm_model, 12000, 4000)
    expected = round(brave + llm_cost + days * price, 4)
    worst = round(brave + llm_cost + days * s.campaign_image_attempts * price, 4)
    return {"days": days, "image_mode": mode, "image_model": s.campaign_image_model if mode == "ai" else
            "local-poster-renderer", "image_size": s.campaign_image_size if mode == "ai" else "platform size",
            "brave_requests": searches, "llm_requests": 0 if s.is_demo else 2, "image_requests": days if mode == "ai"
            else 0, "max_image_requests": days * s.campaign_image_attempts if mode == "ai" else 0,
            "price_per_image_usd": price, "expected_cost_usd": expected, "max_cost_usd": worst,
            "budget_usd": s.campaign_budget_usd, "within_budget": expected <= s.campaign_budget_usd + 1e-9}


def start(db: Session, *, workspace_id: int, brand: Brand, user_id: int, role: str, goal: str, days: int = 5,
          platform: str = "instagram", aspect_ratio: str = "4:5") -> CampaignGeneration:
    workflow.require(role, Role.OWNER, Role.EDITOR)
    goal = " ".join(goal.split())[:1000]
    if len(goal) < 8:
        raise WorkflowError("Describe the campaign goal in a few words (e.g. a 5-day Diwali campaign).")
    if not 1 <= days <= MAX_DAYS:
        raise WorkflowError(f"A campaign can have 1–{MAX_DAYS} days.")
    if platform not in ("instagram", "facebook"):
        raise WorkflowError("Campaign generation supports Instagram and Facebook posts.")
    if aspect_ratio not in ("4:5", "1:1"):
        raise WorkflowError("Choose a 4:5 or 1:1 image.")
    if brand.workspace_id != workspace_id:
        raise WorkflowError("Brand not found.")
    active = db.execute(select(CampaignGeneration).where(CampaignGeneration.workspace_id == workspace_id,
                                                         CampaignGeneration.status.in_(ACTIVE))).scalars().first()
    if active is not None:
        raise WorkflowError(f"Campaign generation #{active.id} is still running. Wait for it to finish first.")
    est = estimate(days)
    if not est["within_budget"]:
        raise WorkflowError(f"This campaign is expected to cost ${est['expected_cost_usd']:.2f}, over the "
                            f"${est['budget_usd']:.2f} campaign budget (CAMPAIGN_BUDGET_USD). Lower the number of "
                            "days or raise the budget.")
    s = get_settings()
    today = datetime.now(UTC).date()
    camp = Campaign(workspace_id=workspace_id, brand_id=brand.id, name=_title_from(goal), objective=goal,
                    geography=brand.location or "", platforms=[platform], start_date=today + timedelta(days=1),
                    end_date=today + timedelta(days=days), budget_usd=s.campaign_budget_usd, status="draft",
                    created_by=user_id)
    db.add(camp)
    db.flush()
    gen = CampaignGeneration(workspace_id=workspace_id, brand_id=brand.id, campaign_id=camp.id, goal=goal, days=days,
                             platform=platform, aspect_ratio=aspect_ratio, status="queued", stage="research",
                             error="", image_mode=est["image_mode"], image_model=est["image_model"],
                             budget_usd=s.campaign_budget_usd, created_by=user_id)
    db.add(gen)
    db.flush()
    for d in range(1, days + 1):
        db.add(CampaignGenerationDay(generation_id=gen.id, workspace_id=workspace_id, day=d, status="pending",
                                     error=""))
    audit.record(db, "campaign_generation.started", workspace_id=workspace_id, user_id=user_id,
                 target_type="campaign", target_id=camp.id, generation_id=gen.id, days=days,
                 image_mode=gen.image_mode, expected_cost_usd=est["expected_cost_usd"])
    db.flush()
    return gen


def retry(db: Session, gen: CampaignGeneration, *, user_id: int, role: str) -> None:
    """Re-run the stage that failed. Stored research, strategy and finished days are kept."""
    workflow.require(role, Role.OWNER, Role.EDITOR)
    if gen.status != "failed":
        raise WorkflowError("Only a failed generation can be retried.")
    gen.status, gen.error = "queued", ""
    audit.record(db, "campaign_generation.retried", workspace_id=gen.workspace_id, user_id=user_id,
                 target_type="campaign", target_id=gen.campaign_id, stage=gen.stage)


def retry_day(db: Session, gen: CampaignGeneration, day: int, *, user_id: int, role: str,
              regenerate: bool = False) -> CampaignGenerationDay:
    """Queue one day's image again. ``regenerate`` replaces a finished image (a new billable request); otherwise a
    failed day reuses an AI image it already paid for and only re-composes the poster."""
    workflow.require(role, Role.OWNER, Role.EDITOR)
    row = _day(gen, day)
    if gen.stage not in ("images", "done"):
        raise WorkflowError("The posts for this campaign haven't been written yet.")
    if row.status in ("pending", "generating"):
        raise WorkflowError(f"Day {day} is already queued.")
    if row.status == "completed" and not regenerate:
        raise WorkflowError(f"Day {day} already has an image. Use regenerate to replace it.")
    item = db.get(ContentItem, row.content_id) if row.content_id else None
    if item is not None and item.status in workflow.FINAL | {ContentStatus.SCHEDULED}:
        raise WorkflowError(f"Day {day}'s post is {item.status}; unschedule it first.")
    if regenerate:
        row.ai_asset_id = None
    row.status, row.error, row.claimed_at = "pending", "", None
    if gen.status in TERMINAL:
        gen.stage, gen.status, gen.error = "images", "queued", ""
    audit.record(db, "campaign_generation.day_retried", workspace_id=gen.workspace_id, user_id=user_id,
                 target_type="campaign", target_id=gen.campaign_id, day=day, regenerate=regenerate)
    return row


def local_poster(db: Session, gen: CampaignGeneration, day: int, *, user_id: int, role: str) -> CampaignGenerationDay:
    """Use the local poster renderer for one day (no external call): the fallback when an AI image fails."""
    workflow.require(role, Role.OWNER, Role.EDITOR)
    row = _day(gen, day)
    if row.status in ("pending", "generating") or row.content_id is None:
        raise WorkflowError(f"Day {day} isn't ready for a poster yet.")
    item = db.get(ContentItem, row.content_id)
    if item is None or item.status in workflow.FINAL | {ContentStatus.SCHEDULED}:
        raise WorkflowError(f"Day {day}'s post can't be changed now.")
    _compose(db, gen, row, item, background=None)
    row.status, row.error = "completed", ""
    _finish_if_done(gen)
    audit.record(db, "campaign_generation.local_poster", workspace_id=gen.workspace_id, user_id=user_id,
                 target_type="campaign", target_id=gen.campaign_id, day=day)
    return row


def approve_all(db: Session, gen: CampaignGeneration, *, user_id: int, role: str, acknowledge_warnings: bool,
                ip: str | None = None) -> dict:
    """Approve every post of the campaign that's in review, each at its current version. Never publishes."""
    workflow.require(role, Role.OWNER)
    approved, skipped = [], []
    for row in gen.day_rows:
        item = db.get(ContentItem, row.content_id) if row.content_id else None
        if item is None or item.status != ContentStatus.IN_REVIEW:
            skipped.append({"day": row.day, "reason": f"not in review ({item.status if item else 'no post'})"})
            continue
        try:
            workflow.approve(db, item, user_id=user_id, role=role, version=item.version,
                             acknowledge_warnings=acknowledge_warnings, ip=ip)
            approved.append(row.day)
        except WorkflowError as exc:
            skipped.append({"day": row.day, "reason": str(exc)})
    return {"approved_days": approved, "skipped": skipped}


# ── worker ─────────────────────────────────────────────────────────────────────────────────────────────────────
def process_queued(db: Session, *, max_units: int = 3) -> int:
    """Run up to ``max_units`` units of work (a stage, or one day's image). Returns how many ran."""
    done = 0
    while done < max_units:
        gen_id = db.execute(select(CampaignGeneration.id).where(CampaignGeneration.status == "queued")
                            .order_by(CampaignGeneration.updated_at, CampaignGeneration.id).limit(1)).scalar()
        if gen_id is None:
            break
        claimed = db.execute(update(CampaignGeneration).where(CampaignGeneration.id == gen_id,
                                                              CampaignGeneration.status == "queued")
                             .values(status="running", claimed_at=utcnow())).rowcount
        db.commit()
        if not claimed:
            continue  # another worker took it
        gen = db.get(CampaignGeneration, gen_id)
        try:
            more = run_unit(db, gen)
        except Exception:  # noqa: BLE001 - never leave a generation claimed after a crash
            log.exception("campaign generation %s crashed", gen_id)
            db.rollback()
            gen = db.get(CampaignGeneration, gen_id)
            gen.status, gen.error = "failed", "Internal error during generation; retry to continue."
            db.commit()
            more = False
        done += 1
        if more is None:
            break  # waiting for a free image slot: try again on the next regular tick, don't spin
        if more:
            from .. import worker

            worker.notify()
    return done


def recover_stale(db: Session) -> int:
    """After a restart: release generations whose worker died. A day that was mid-request is marked failed, not
    re-sent, so a request that may already have been billed is never repeated without a person asking."""
    cutoff = utcnow() - LEASE
    n = db.execute(update(CampaignGenerationDay).where(CampaignGenerationDay.status == "generating",
                                                       CampaignGenerationDay.claimed_at < cutoff)
                   .values(status="failed", error="Interrupted while generating (the worker stopped). Retry this "
                           "day to try again.")).rowcount
    n += db.execute(update(CampaignGeneration).where(CampaignGeneration.status == "running",
                                                     CampaignGeneration.claimed_at < cutoff)
                    .values(status="queued")).rowcount
    db.commit()
    return n


def run_unit(db: Session, gen: CampaignGeneration) -> bool | None:
    """Do the next unit of work for a claimed generation and release it. Returns True if more work is queued, or
    None if nothing could run yet (another image request holds the only slot)."""
    scope = usage.UsageScope(db=db, workspace_id=gen.workspace_id, campaign_id=gen.campaign_id)
    token = usage.open_scope(scope)
    progressed = True
    try:
        if gen.stage == "research":
            _research(db, gen)
            gen.stage = "strategy"
        elif gen.stage == "strategy":
            _strategy(db, gen)
            gen.stage = "text"
        elif gen.stage == "text":
            _text(db, gen)
            gen.stage = "images"
        elif gen.stage == "images":
            progressed = _next_image(db, gen, scope)
        gen.error = ""
    except StageFailed as exc:
        db.rollback()
        usage.restore(scope)
        gen = db.get(CampaignGeneration, gen.id)
        gen.status, gen.error = "failed", str(exc)[:2000]
        audit.record(db, "campaign_generation.failed", workspace_id=gen.workspace_id, target_type="campaign",
                     target_id=gen.campaign_id, stage=gen.stage, error=gen.error[:300])
        db.commit()
        return False
    finally:
        usage.close_scope(token)
    _finish_if_done(gen)
    if gen.status == "running":
        gen.status = "queued"
    gen.claimed_at = None
    db.commit()
    if not progressed:
        return None
    return gen.status == "queued"


class StageFailed(RuntimeError):
    """A stage can't finish; the generation stops at it until a person retries."""


# ── stage 1: research ──────────────────────────────────────────────────────────────────────────────────────────
def research_plan(goal: str, brand: Brand, products: list, days: int, *, year: int | None = None) -> dict:
    """Research questions and 3–5 focused queries for the whole campaign (no per-day research)."""
    year = year or datetime.now(UTC).year
    occasion = next((o for o in OCCASIONS if re.search(rf"\b{re.escape(o)}\b", goal, re.I)), "")
    cats = [p.category for p in products if p.category]
    category = max(set(cats), key=cats.count) if cats else (brand.description or brand.name or "")[:40]
    category = category.strip() or "gifts"
    market = (brand.location or "").split(",")[-1].strip() or ""
    topic = occasion or _topic(goal)
    qs = [
        ("What are people shopping for this season?", f"{topic} {year} shopping trends {market}"),
        ("Which products and gifts are relevant to our category?", f"{topic} {category} ideas {year}"),
        ("What do customers in our market expect around this occasion?", f"{topic} {category} customers {market}"),
        ("What are the key dates for the campaign window?", f"{topic} {year} dates {market}" if occasion else
         f"{topic} marketing calendar {year}"),
        ("What content formats work for this occasion on social media?",
         f"{topic} {category} Instagram post ideas"),
    ]
    seen, out = set(), []
    for q, query in qs:
        query = " ".join(query.split())
        if query.lower() not in seen:
            seen.add(query.lower())
            out.append({"question": q, "query": query})
    country = next((code for name, code in COUNTRIES.items() if name in (brand.location or "").lower()), None)
    return {"occasion": occasion, "category": category, "market": market, "country": country,
            "questions": out[:get_settings().campaign_max_searches]}


def _research(db: Session, gen: CampaignGeneration) -> None:
    if gen.research and gen.research.get("status") == "ok":
        return  # reuse: research is done once per campaign
    s = get_settings()
    brand = db.get(Brand, gen.brand_id)
    products = usable_products(db, brand)
    plan = research_plan(gen.goal, brand, products, gen.days)
    queries = [q["query"] for q in plan["questions"]]
    base = {"status": "ok", "plan": plan, "queries": queries, "sources": [], "errors": [], "searched_at":
            utcnow().isoformat(), "storage_rights": s.brave_storage_rights}
    if s.is_demo:
        gen.research = base | {"mode": "demo", "assumptions": [
            "Demo mode makes no web searches: the plan uses only the brand profile and catalogue."]}
        return
    if not queries:
        gen.research = base | {"mode": "none", "assumptions": ["Web research is turned off (CAMPAIGN_MAX_SEARCHES=0)."]}
        return
    if not s.search_configured:
        raise StageFailed("Web research needs BRAVE_SEARCH_API_KEY. Add it and retry; no findings were invented.")
    results: dict[str, list] = {}
    errors: dict[str, Exception] = {}
    budget = s.campaign_max_searches
    todo = list(queries)
    for attempt in range(2):  # one bounded retry for transient failures, within the per-campaign cap
        if not todo or budget <= 0:
            break
        batch = todo[:budget]
        out = search_mod.search_many(batch, count=5, country=plan["country"], freshness="py")
        todo = []
        for q, res in out.items():
            if isinstance(res, Exception):
                errors[q] = res
                if getattr(res, "retryable", False):
                    todo.append(q)
            else:
                errors.pop(q, None)
                results[q] = res
        budget -= sum(1 for q in batch if not (q in results and results[q] and results[q][0].cached))
        if todo and attempt == 0:
            _sleep(2)
    sources, seen = [], set()
    for q in queries:
        for r in results.get(q, []):
            if r.url in seen:
                continue
            seen.add(r.url)
            sources.append({"id": f"S{len(sources) + 1}", "title": r.title[:200], "url": r.url, "snippet":
                            r.snippet[:300], "age": r.age, "query": q, "cached": r.cached})
    if not sources:
        first = next(iter(errors.values()), None)
        reason = str(first) if first else "the searches returned no results"
        if isinstance(first, usage.QuotaExceeded):
            reason = f"Brave search quota reached: {first}"
        raise StageFailed(f"Research failed: {reason}. Retry later; no findings were invented.")
    gen.research = base | {"mode": "brave", "sources": sources[:15],
                           "errors": [{"query": q, "error": str(e)[:200]} for q, e in errors.items()],
                           "assumptions": []}


# ── stage 2: strategy ──────────────────────────────────────────────────────────────────────────────────────────
def _brand_brief(brand: Brand) -> dict:
    return {"name": brand.name, "description": (brand.description or "")[:500],
            "positioning": (brand.positioning or "")[:300], "voice": brand.voice, "tone": brand.tone,
            "audience": (brand.audience or "")[:300], "location": brand.location, "website": brand.website,
            "colors": brand.colors or [], "content_pillars": brand.content_pillars or [],
            "approved_claims": brand.approved_claims or [], "restricted_claims": brand.restricted_claims or [],
            "prohibited_messaging": brand.prohibited_messaging or []}


def _catalogue(products: list) -> list[dict]:
    out = []
    for p in products[:20]:
        out.append({"id": p.id, "name": p.name, "category": p.category, "description": (p.description or "")[:160],
                    "has_real_photo": p.image_kind == "product_photo",
                    "data_quality": [q.get("code") for q in p.data_quality or [] if isinstance(q, dict)]})
    return out


def _strategy(db: Session, gen: CampaignGeneration) -> None:
    if gen.strategy:
        return
    brand = db.get(Brand, gen.brand_id)
    products = usable_products(db, brand)
    research = gen.research or {}
    sources = research.get("sources") or []
    llm = _llm()
    if llm is None:
        plan = _demo_strategy(gen, brand, products)
    else:
        payload = {"goal": gen.goal, "days": gen.days, "platform": gen.platform, "brand": _brand_brief(brand),
                   "catalogue": _catalogue(products),
                   "research_sources": [{"id": x["id"], "title": x["title"], "snippet": x.get("snippet", ""),
                                         "age": x.get("age")} for x in sources]}
        task = (f"Write ONE {gen.days}-day campaign plan for this goal. Exactly {gen.days} entries in days, numbered 1 "
                f"to {gen.days}, building on each other. Recommend products only by their catalogue ids. Every "
                "finding must cite source ids from research_sources; anything without a source goes in assumptions.")
        plan = _structured(llm, "Strategy Agent", payload, task, StrategyOut,
                           lambda out: _check_strategy(out, gen.days, products, sources))
    gen.strategy = plan
    if sources and not get_settings().brave_storage_rights:
        # Brave's terms allow keeping results only with storage rights: keep titles and URLs, drop the snippets.
        gen.research = {**research, "sources": [{k: v for k, v in x.items() if k != "snippet"} for x in sources]}


def _check_strategy(out: StrategyOut, days: int, products: list, sources: list) -> dict:
    nums = sorted(d.day for d in out.days)
    if nums != list(range(1, days + 1)):
        raise ValueError(f"expected exactly {days} days numbered 1–{days}, got {nums}")
    ids = {p.id for p in products}
    src = {x["id"] for x in sources}
    plan = out.model_dump()
    plan["product_ids"] = [i for i in plan["product_ids"] if i in ids]
    for d in plan["days"]:
        d["product_ids"] = [i for i in d["product_ids"] if i in ids][:2]
        d["source_ids"] = [i for i in d["source_ids"] if i in src]
    plan["findings"] = [f | {"source_ids": [i for i in f["source_ids"] if i in src]} for f in plan["findings"]]
    # A "finding" with no real source is an assumption, not research.
    plan["assumptions"] = plan["assumptions"] + [f["finding"] for f in plan["findings"] if not f["source_ids"]]
    plan["findings"] = [f for f in plan["findings"] if f["source_ids"]]
    plan["days"].sort(key=lambda d: d["day"])
    return plan


def _demo_strategy(gen: CampaignGeneration, brand: Brand, products: list) -> dict:
    """Deterministic plan from the brand profile and catalogue only (demo mode: no LLM, no invented facts)."""
    occasion = (gen.research or {}).get("plan", {}).get("occasion") or "the season"
    concepts = [("Announce the campaign", f"{occasion} is coming: introduce {brand.name}'s collection"),
                ("Spotlight a product", "A close look at one product from the catalogue"),
                ("Share a tradition or tip", f"How customers prepare for {occasion}"),
                ("Gifting ideas", f"Products from the catalogue that suit {occasion} gifting"),
                ("Countdown", f"Last days before {occasion}: a warm reminder"),
                ("Behind the brand", f"What {brand.name} stands for"),
                ("Thank you", "Thank customers and invite them to visit")]
    picks = [p.id for p in products[:gen.days]]
    days = []
    for d in range(1, gen.days + 1):
        objective, concept = concepts[(d - 1) % len(concepts)]
        pid = [picks[(d - 1) % len(picks)]] if picks else []
        days.append({"day": d, "objective": objective, "concept": concept,
                     "caption_brief": f"{concept}. Use only catalogue facts.", "cta": "Explore the collection",
                     "product_ids": pid, "source_ids": []})
    return {"title": _title_from(gen.goal), "objective": gen.goal, "audience": brand.audience or "",
            "theme": f"{occasion} with {brand.name}", "voice": brand.voice or "warm and clear",
            "positioning": brand.positioning or "", "platforms": [gen.platform], "product_ids": picks,
            "findings": [], "assumptions": ["Demo mode: no web research or AI model was used."], "days": days}


# ── stage 3: captions, hashtags, CTAs and image prompts ───────────────────────────────────────────────────────
def _text(db: Session, gen: CampaignGeneration) -> None:
    brand = db.get(Brand, gen.brand_id)
    plan = gen.strategy or {}
    rows = {r.day: r for r in gen.day_rows}
    if all(r.content_id for r in rows.values()):
        return  # already written (resumed after a restart)
    llm = _llm()
    if llm is None:
        creative = _demo_creative(gen, brand, plan)
    else:
        products = {p.id: p for p in usable_products(db, brand)}
        payload = {"brand": _brand_brief(brand), "platform": gen.platform, "aspect_ratio": gen.aspect_ratio,
                   "plan": {k: plan.get(k) for k in ("title", "objective", "audience", "theme", "voice", "positioning")}
                   | {"days": plan.get("days", [])},
                   "products": [{"id": p.id, "name": p.name, "category": p.category,
                                 "description": (p.description or "")[:160]} for p in products.values()
                                if p.id in {i for d in plan.get("days", []) for i in d.get("product_ids", [])}]}
        task = (f"For each of the {gen.days} days write: a poster hook (under 70 characters), one polished "
                f"{gen.platform} caption, 5–10 relevant hashtags, a CTA under 30 characters, and an image prompt for "
                "an AI background scene. The real product photo is added on top later, so the image must never show "
                "the product, a product lookalike, text, logos or people's faces. Keep one visual style across all "
                f"days, using the brand colours. Exactly {gen.days} entries in days, numbered 1 to {gen.days}.")
        creative = _structured(llm, "Creative Agent", payload, task, CreativeOut,
                               lambda out: _check_creative(out, gen.days))
    _store_posts(db, gen, brand, plan, creative, generated_by=llm.name if llm else "demo-generator")
    _update_campaign(db, gen, plan, creative)


def _check_creative(out: CreativeOut, days: int) -> dict:
    nums = sorted(d.day for d in out.days)
    if nums != list(range(1, days + 1)):
        raise ValueError(f"expected exactly {days} days numbered 1–{days}, got {nums}")
    data = out.model_dump()
    data["days"].sort(key=lambda d: d["day"])
    return data


def _demo_creative(gen: CampaignGeneration, brand: Brand, plan: dict) -> dict:
    tag = re.sub(r"[^A-Za-z0-9]", "", brand.name) or "Brand"
    occasion = (gen.research or {}).get("plan", {}).get("occasion") or ""
    colors = ", ".join((brand.colors or [])[:3]) or "warm brand colours"
    days = []
    for d in plan.get("days", []):
        days.append({"day": d["day"], "hook": d["concept"][:68],
                     "caption": f"{d['concept']}. {brand.name} — {brand.description or ''}".strip()[:600],
                     "hashtags": [f"#{tag}"] + ([f"#{occasion.replace(' ', '')}"] if occasion else []),
                     "cta": d["cta"][:30],
                     "image": {"subject": f"a softly lit festive setting for {occasion or 'the season'}",
                               "style": "warm, premium product-photography backdrop",
                               "composition": "clear space in the lower half for a product photo",
                               "palette": colors, "negative": NEGATIVE}})
    return {"visual_style": f"Warm, consistent backdrops in {colors}", "days": days}


def prompt_text(image: dict, visual_style: str, aspect_ratio: str) -> str:
    negative = image.get("negative") or ""
    if NEGATIVE not in negative:  # always: the model's own list can't drop the product/face/text exclusions
        negative = f"{negative}; {NEGATIVE}".strip("; ")
    return (f"{image.get('subject', '')}. Style: {image.get('style', '')}; campaign style: {visual_style}. "
            f"Composition: {image.get('composition', '')}. Palette: {image.get('palette', '')}. Aspect ratio "
            f"{aspect_ratio}. Background scene for a marketing poster. Avoid: {negative}.")[:1800]


def _store_posts(db: Session, gen: CampaignGeneration, brand: Brand, plan: dict, creative: dict,
                 generated_by: str) -> None:
    plans = {d["day"]: d for d in plan.get("days", [])}
    start_day = (gen.created_at or utcnow()).date() + timedelta(days=1)
    for c in creative["days"]:
        row = _day(gen, c["day"])
        if row.content_id:
            continue
        p = plans.get(c["day"], {})
        hashtags = workflow.normalise_hashtags(c.get("hashtags") or [])
        item = ContentItem(
            workspace_id=gen.workspace_id, brand_id=brand.id, campaign_id=gen.campaign_id, platform=gen.platform,
            content_type="post", pillar=(plan.get("theme") or "")[:120],
            title=f"Day {c['day']}: {p.get('concept', '')}"[:255], hook=(c.get("hook") or "")[:90],
            caption=c.get("caption") or "", hashtags=hashtags, cta=(c.get("cta") or "")[:30],
            image_brief=prompt_text(c["image"], creative.get("visual_style", ""), gen.aspect_ratio),
            product_ids=p.get("product_ids") or [], generated_by=generated_by[:80],
            rationale=f"{p.get('objective', '')}. Sources: {', '.join(p.get('source_ids') or []) or 'none'}."[:2000],
            planned_at=datetime.combine(start_day + timedelta(days=c["day"] - 1), datetime.min.time())
            + timedelta(hours=12), status=ContentStatus.DRAFT, created_by=gen.created_by,
            is_synthetic=get_settings().is_demo and brand.is_synthetic)
        db.add(item)
        db.flush()
        workflow.add_revision(db, item, "campaign_generation", None, f"Generated for day {c['day']}")
        row.content_id = item.id
        row.image_prompt = c["image"] | {"visual_style": creative.get("visual_style", ""),
                                         "prompt": item.image_brief, "aspect_ratio": gen.aspect_ratio}


def _update_campaign(db: Session, gen: CampaignGeneration, plan: dict, creative: dict) -> None:
    camp = db.get(Campaign, gen.campaign_id)
    camp.name = (plan.get("title") or camp.name)[:160]
    camp.objective = plan.get("objective") or camp.objective
    camp.audience = plan.get("audience") or ""
    camp.pillars = [plan.get("theme")] if plan.get("theme") else []
    camp.ctas = list(dict.fromkeys(d.get("cta", "") for d in plan.get("days", []) if d.get("cta")))[:5]
    camp.product_ids = plan.get("product_ids") or []
    camp.creative_direction = creative.get("visual_style", "")
    camp.assumptions = plan.get("assumptions") or []
    camp.strategy = {"source": "campaign_generation", "generation_id": gen.id, "theme": plan.get("theme"),
                     "voice": plan.get("voice"), "positioning": plan.get("positioning")}
    camp.status = "active"


# ── stage 4: one image per day ─────────────────────────────────────────────────────────────────────────────────
def _next_image(db: Session, gen: CampaignGeneration, scope: usage.UsageScope) -> bool:
    """Generate the next pending day's creative. Returns False if it had to wait for a free image slot."""
    row = next((r for r in gen.day_rows if r.status == "pending"), None)
    if row is None:
        return True
    s = get_settings()
    busy = db.execute(select(func.count(CampaignGenerationDay.id)).where(
        CampaignGenerationDay.status == "generating")).scalar_one()
    if busy >= s.campaign_image_concurrency:
        return False  # another image request is in flight
    if not db.execute(update(CampaignGenerationDay).where(CampaignGenerationDay.id == row.id,
                                                          CampaignGenerationDay.status == "pending")
                      .values(status="generating", claimed_at=utcnow())).rowcount:
        return False
    db.commit()
    db.refresh(row)
    item = db.get(ContentItem, row.content_id) if row.content_id else None
    if item is None:
        _fail_day(db, row, "This day's post is missing; regenerate the campaign text.")
        return True
    try:
        background = None
        if gen.image_mode == "ai":
            background = _ai_background(db, gen, row, scope)
        _compose(db, gen, row, item, background=background)
        row.status, row.error, row.claimed_at = "completed", "", None
        db.commit()
    except (imagegen.ImageGenError, usage.BudgetExceeded, WorkflowError) as exc:
        db.rollback()
        usage.restore(scope)
        row = db.get(CampaignGenerationDay, row.id)
        msg = str(exc)
        if isinstance(exc, usage.BudgetExceeded):
            msg = f"Budget reached, so no image was requested: {exc}"
        _fail_day(db, row, msg)
    return True


def _ai_background(db: Session, gen: CampaignGeneration, row: CampaignGenerationDay,
                   scope: usage.UsageScope) -> tuple[bytes, Asset]:
    if row.ai_asset_id:  # already paid for: reuse it rather than request again
        a = db.get(Asset, row.ai_asset_id)
        if a is not None:
            return store().path(a.storage_key).read_bytes(), a
    s = get_settings()
    prompt = (row.image_prompt or {}).get("prompt") or ""
    last: imagegen.ImageGenError | None = None
    for attempt in range(1, s.campaign_image_attempts + 1):
        row.attempts += 1
        db.commit()  # count the billable attempt before making it, so a crash can't hide it
        try:
            raw, ctype, info = imagegen.generate_image(prompt, aspect_ratio=gen.aspect_ratio,
                                                       size=s.campaign_image_size, model=s.campaign_image_model)
        except imagegen.ImageGenError as exc:
            last = exc
            if exc.retryable and attempt < s.campaign_image_attempts:
                _sleep(min(2 ** attempt * 2, 30))
                continue
            raise
        ext = ".png" if ctype == "image/png" else ".jpg"
        key = store().save(raw, ext)
        a = Asset(workspace_id=gen.workspace_id, brand_id=gen.brand_id, kind="background", source="ai_background",
                  filename=f"campaign-{gen.campaign_id}-day{row.day}-{s.campaign_image_size}{ext}", content_type=ctype,
                  size_bytes=len(raw), width=info["width"], height=info["height"], storage_key=key,
                  sha256=hashlib.sha256(raw).hexdigest(), alt_text="AI-generated background scene (no product)",
                  meta=info | {"generation_id": gen.id, "day": row.day, "attempt": row.attempts},
                  campaign_id=gen.campaign_id)
        db.add(a)
        db.flush()
        row.ai_asset_id = a.id
        db.commit()  # keep the paid-for image even if composing the poster fails
        scope.kept.clear()  # its usage record is committed now; nothing to restore after a later rollback
        return raw, a
    raise last or imagegen.ImageGenError("No image was generated.")


def _compose(db: Session, gen: CampaignGeneration, row: CampaignGenerationDay, item: ContentItem,
             background: tuple[bytes, Asset] | None) -> None:
    from .creative import render_for_item

    size = (background[1].width, background[1].height) if background and background[1].width else None
    asset = render_for_item(db, item, background=background, size=size, background_strength=0.85)
    asset.meta = {**(asset.meta or {}), "generation_id": gen.id, "day": row.day,
                  "image_mode": "ai" if background else "local"}
    workflow.update(db, item, {"asset_id": asset.id}, user_id=None, role=Role.EDITOR.value,
                    source="campaign_generation", note=f"Day {row.day} creative")
    if item.status in (ContentStatus.DRAFT, ContentStatus.CHANGES_REQUESTED):
        workflow.submit(db, item, user_id=None, role=Role.EDITOR.value)
    row.asset_id = asset.id


def _fail_day(db: Session, row: CampaignGenerationDay, message: str) -> None:
    row.status, row.error, row.claimed_at = "failed", message[:2000], None
    audit.record(db, "campaign_generation.day_failed", workspace_id=row.workspace_id, target_type="campaign_day",
                 target_id=row.id, day=row.day, error=message[:300])
    db.commit()


def _finish_if_done(gen: CampaignGeneration) -> None:
    if gen.stage != "images" or any(r.status in ("pending", "generating") for r in gen.day_rows):
        return
    gen.stage = "done"
    gen.status = "completed" if all(r.status == "completed" for r in gen.day_rows) else "partial"


# ── helpers ────────────────────────────────────────────────────────────────────────────────────────────────────
def _llm():
    try:
        return get_llm()
    except LLMUnavailable as exc:
        raise StageFailed(str(exc)) from exc


def _structured(llm, role: str, payload: dict, task: str, schema, check) -> dict:
    """One structured call with at most STAGE_ATTEMPTS tries; invalid output is never retried indefinitely."""
    import json

    prompt = "<data>\n" + json.dumps(payload, ensure_ascii=False, default=str) + "\n</data>\n\n" + task
    last = ""
    for attempt in range(1, STAGE_ATTEMPTS + 1):
        try:
            out, _tokens = llm.structured(SYSTEM.format(role=role), prompt, schema)
            return check(out)
        except (ValueError, ValidationError) as exc:
            last = f"the model's output was invalid ({str(exc)[:200]})"
        except LLMError as exc:
            if not exc.retryable:
                raise StageFailed(f"{role} failed: {exc}") from exc
            last = str(exc)
        if attempt < STAGE_ATTEMPTS:
            _sleep(min(2 ** attempt * 2, 20))
    raise StageFailed(f"{role} failed after {STAGE_ATTEMPTS} attempts: {last}")


def _day(gen: CampaignGeneration, day: int) -> CampaignGenerationDay:
    row = next((r for r in gen.day_rows if r.day == day), None)
    if row is None:
        raise WorkflowError(f"Day {day} not found.")
    return row


def _title_from(goal: str) -> str:
    g = re.sub(r"^(please\s+)?(create|make|plan|generate|build)\s+(me\s+)?(a|an)?\s*", "", goal.strip(), flags=re.I)
    return (g[:1].upper() + g[1:])[:160] or "Generated campaign"


def _topic(goal: str) -> str:
    words = re.sub(r"\b(\d+[- ]day|campaign|marketing|create|plan|make|for|my|our|store|shop|a|an|the)\b", " ",
                   goal, flags=re.I)
    return " ".join(words.split()[:4]) or "seasonal"


def usage_summary(db: Session, gen: CampaignGeneration) -> dict[str, Any]:
    rows = db.execute(select(UsageRecord).where(UsageRecord.campaign_id == gen.campaign_id)).scalars().all()
    brave = [r for r in rows if r.provider == "brave"]
    llm = [r for r in rows if r.operation.startswith(("structured", "tool_turn"))]
    images = [r for r in rows if r.provider == "gemini-image"]
    return {"brave_requests": sum(1 for r in brave if not r.cached), "brave_cached": sum(1 for r in brave if r.cached),
            "llm_requests": len(llm), "llm_tokens": sum(r.input_tokens + r.output_tokens for r in llm),
            "image_requests": sum(r.attempts for r in gen.day_rows),
            "images_generated": sum(r.units for r in images if r.operation == "campaign_image"),
            "cost_usd": round(sum(r.cost_usd or 0.0 for r in rows), 4)}
