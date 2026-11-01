"""Poojaloka demo workspace: REAL catalogue data from a dated snapshot, plus clearly labelled demo fixtures.

What is real (source-reported, with URLs and the retrieval time):
  * products, prices, MRPs, stock, variants and image URLs from the public storefront API (40-product subset);
  * the brand's location, colours, fonts, tagline and homepage copy from poojaloka.com;
  * the store's own festival guides (items needed, pooja vidhi) and articles.
What is NOT real and is labelled so everywhere: the two competitors (fictional), the trend notes (fixtures),
the past campaign and every metric (synthetic). Brand voice, audience and goals are GrowthPilot suggestions for
the owner to edit.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..adapters.base import ConnectionContext
from ..adapters.demo_instagram import DemoAdapter
from ..catalogue import sync
from ..catalogue.poojaloka import SnapshotSource
from ..models import (
    Brand,
    Campaign,
    ChannelConnection,
    Competitor,
    CompetitorObservation,
    ConnectionStatus,
    ContentItem,
    ContentStatus,
    EvidenceClass,
    Experiment,
    Hypothesis,
    MetricSnapshot,
    Persona,
    Product,
    PublishAttempt,
    ResearchFinding,
    utcnow,
)
from ..platforms import ORDER, SPECS
from ..services.content import add_revision

log = logging.getLogger(__name__)
SNAPSHOT_PATH = Path(__file__).parent / "poojaloka_snapshot.json"
SITE = "https://poojaloka.com"


def load_snapshot() -> dict:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _when(snap: dict) -> str:
    return snap["meta"]["retrieved_at"]


def brand_profile(snap: dict) -> dict:
    hero = snap.get("hero") or {}
    at = _when(snap)
    return dict(
        name="Poojaloka",
        description="Poojaloka sells pooja essentials, brass idols and spiritual decor online and from its store in "
                    "Rajajinagar, Bengaluru (as described on poojaloka.com).",
        positioning=hero.get("subtext") or "Authentic pooja essentials, delivered home.",
        voice="Warm, devotional and respectful. Clear and practical. Never pushy. (Suggested — edit to taste.)",
        tone="Reverent but friendly, like a knowledgeable family shopkeeper. (Suggested.)",
        audience="Families in Bengaluru and across Karnataka preparing for daily pooja and festivals; people setting "
                 "up a first home altar. (Suggested from the catalogue and site; confirm with real customer data.)",
        location="Bengaluru, Karnataka",
        goals="Grow festival-season orders and repeat buyers; earn saves and shares with useful festival guides. "
              "(Suggested.)",
        website=SITE,
        colors=["#6B1420", "#C99A3D", "#FBF3E7", "#2B1810", "#D9722A"],
        fonts=["Cormorant Garamond", "Poppins", "Mukta", "Noto Sans Kannada"],
        visual_guidelines="Deep maroon and temple gold on cream; Cormorant Garamond headings with Poppins text; lamps, "
                          "rangoli and brass textures; real product photos only — never a stock image as a product; "
                          "no AI images of deities.",
        approved_claims=[],
        restricted_claims=["Since 1980", "Trusted by 10,000+ Devotees", "Fast & Safe Delivery",
                           "Own delivery in Bengaluru", "curated by artisans"],
        prohibited_messaging=["guaranteed blessings", "miracle", "100% pure", "lowest price in India"],
        constraints="No discount or MRP comparisons unless an approved offer exists. No health or medicinal claims "
                    "for Ayurvedic items. No AI-generated images of deities. Refresh prices within 24 hours of "
                    "publishing.",
        permitted_sources=["poojaloka.com", "api.poojaloka.com"],
        content_pillars=["Festival guide", "Product showcase", "Devotional know-how", "Festival offers"],
        languages=["English", "Kannada"],
        marketing_objectives=["Festival-season orders", "Repeat buyers", "Saves and shares on festival guides"],
        platform_preferences=list(ORDER),
        posting_constraints={"max_posts_per_day": 2, "posting_times": ["09:30", "19:00"],
                             "notes": "Morning and evening, when families plan their pooja."},
        campaign_budget_usd=2.0,
        profile_sources=[
            {"field": "description, location", "source_url": f"{SITE}/", "retrieved_at": at,
             "note": "Site footer: Bengaluru, Karnataka; Rajajinagar address."},
            {"field": "positioning", "source_url": "https://api.poojaloka.com/api/hero", "retrieved_at": at,
             "note": "Homepage hero subtext."},
            {"field": "colors, fonts", "source_url": f"{SITE}/", "retrieved_at": at,
             "note": "Extracted from the site's stylesheet (maroon, gold, cream, ink, saffron; Cormorant Garamond, "
                     "Poppins, Mukta, Noto Sans Kannada)."},
            {"field": "restricted_claims", "source_url": f"{SITE}/", "retrieved_at": at,
             "note": "Shown on the homepage (“Since 1980”, “Trusted by 10,000+ Devotees”, delivery). The owner must "
                     "confirm before they can be used."},
            {"field": "voice, tone, audience, goals", "source_url": None, "retrieved_at": at,
             "note": "GrowthPilot suggestions, not facts. Edit in Brand Brain."},
        ],
    )


PERSONAS = [
    dict(name="Festival planner (family)", description="Parents in Bengaluru preparing for Deepavali, Navaratri and "
         "other festivals; buy several items at once.", goals="Have everything ready on the day.",
         pain_points="Forgetting items; last-minute shop visits.", preferred_platforms=["instagram", "facebook"]),
    dict(name="First home altar", description="Young couples setting up a pooja corner in a new flat.",
         goals="A complete, correct set without overspending.", pain_points="Not sure what's essential.",
         preferred_platforms=["instagram", "pinterest"]),
    dict(name="Daily devotee", description="Regular buyers of agarbatti, camphor, kumkum and oils.",
         goals="Reliable supply at fair prices.", pain_points="Running out; inconsistent quality.",
         preferred_platforms=["facebook", "threads"]),
]

TRENDS = [
    dict(topic="Festival demand", statement="Fixture note: festival-week searches for diyas, pooja kits and brass "
         "lamps typically rise in the two to three weeks before Deepavali."),
    dict(topic="Checklists", statement="Fixture note: checklist-style festival posts (what you need, step by step) "
         "are commonly saved for later."),
    dict(topic="Brass care", statement="Fixture note: buyers of brass items often ask how to clean and maintain them."),
]

COMPETITORS = [
    dict(name="Devalaya Mart (fictional)", notes="Fictional competitor for the demo. Observations are synthetic; no "
         "engagement figures are shown because none were observed.", obs=[
             ("festival sale", "post", "Big Deepavali sale on brass lamps — limited stock!", "Shop the sale", ["lamps"]),
             ("discount", "reel", "Flat 25% off pooja kits this week.", "Link in bio", ["pooja kits"]),
             ("product spotlight", "post", "New arrival: five-face brass samai.", "Order now", ["brass"]),
         ]),
    dict(name="Mandir Essentials Co. (fictional)", notes="Fictional competitor for the demo (synthetic observations).",
         obs=[
             ("festival greeting", "post", "Wishing you a bright and joyful Deepavali.", "", []),
             ("festival sale", "carousel", "Festive combo offers on diyas and rangoli colours.", "Shop now",
              ["diyas", "rangoli"]),
         ]),
]

HISTORY = [  # (variant, content_type, pillar, slug, days_ago)
    ("A", "post", "Product showcase", "meru-brass-solid-2x2-id0007", 30),
    ("B", "carousel", "Festival guide", "matki-diya-1-box-125-md0044", 29),
    ("A", "post", "Product showcase", "glass-deepa-lamp-3-br0791", 27),
    ("B", "carousel", "Festival guide", "smokeless-camphor-50-gms-pj0162", 26),
    ("A", "post", "Product showcase", "brass-puttu-parat-s-12-br1797", 24),
    ("B", "carousel", "Festival guide", "kashi-ashtagandha-chandan-8905694503566", 23),
    ("A", "post", "Product showcase", "brass-pooja-petti-size-6-br2657", 21),
    ("B", "carousel", "Festival guide", "camphor-gede-karpura-40-1-pkt-pj0154", 20),
]


def seed(db: Session, workspace_id: int, user_id: int | None) -> dict:
    snap = load_snapshot()
    now = utcnow()
    at = datetime.fromisoformat(_when(snap).replace("Z", "+00:00")).replace(tzinfo=None)
    profile = brand_profile(snap)
    brand = Brand(workspace_id=workspace_id, is_synthetic=False, **profile)
    brand.catalogue_source = {
        "kind": "poojaloka_store_api", "base_url": "https://api.poojaloka.com/api",
        "label": SnapshotSource(snap).label, "snapshot": True, "last_synced_at": _when(snap),
        "festivals": snap.get("festivals") or [], "articles": snap.get("articles") or [],
        "categories": [c.get("name") for c in snap.get("categories") or []],
        "image_audit": snap["meta"].get("image_audit"), "catalogue_total": snap["meta"].get("catalogue_total")}
    db.add(brand)
    db.flush()
    for p in PERSONAS:
        db.add(Persona(workspace_id=workspace_id, brand_id=brand.id, **p))
    res = sync(db, brand, SnapshotSource(snap), records=SnapshotSource(snap).list_products(), retrieved_at=at,
               download_photos=False, user_id=user_id, snapshot=True)

    # The store's own festival guides and articles: real, source-reported content.
    for fest in snap.get("festivals") or []:
        db.add(ResearchFinding(
            workspace_id=workspace_id, brand_id=brand.id, topic=f"Festival: {fest.get('name')}",
            statement=f"{fest.get('name')}: {fest.get('importance', '')} Items the store lists as needed: "
                      f"{', '.join(fest.get('itemsNeeded') or [])}.",
            classification=EvidenceClass.SOURCE_REPORTED, source_type="brand_site",
            source_url=f"{SITE}/festivals/{fest.get('slug')}", source_title=f"Poojaloka festival guide — {fest.get('name')}",
            evidence_excerpt=(fest.get("poojaVidhi") or "")[:1000], retrieved_at=at, is_synthetic=False))
    for art in snap.get("articles") or []:
        db.add(ResearchFinding(
            workspace_id=workspace_id, brand_id=brand.id, topic=f"Article: {art.get('title')}"[:160],
            statement=art.get("excerpt") or art.get("title"), classification=EvidenceClass.SOURCE_REPORTED,
            source_type="brand_site", source_url=f"{SITE}/articles/{art.get('slug')}", source_title=art.get("title", ""),
            evidence_excerpt=(art.get("body") or "")[:1000], retrieved_at=at, is_synthetic=False))
    for t in TRENDS:
        db.add(ResearchFinding(workspace_id=workspace_id, brand_id=brand.id, topic=t["topic"], statement=t["statement"],
                               classification=EvidenceClass.SOURCE_REPORTED, source_type="fixture", source_url=None,
                               source_title="Demo fixture — synthetic trend note (not a real source)",
                               evidence_excerpt=t["statement"], is_synthetic=True))
    for c in COMPETITORS:
        comp = Competitor(workspace_id=workspace_id, brand_id=brand.id, name=c["name"], notes=c["notes"],
                          is_synthetic=True)
        db.add(comp)
        db.flush()
        for i, (theme, fmt, excerpt, cta, cats) in enumerate(c["obs"]):
            db.add(CompetitorObservation(
                workspace_id=workspace_id, competitor_id=comp.id, source_type="fixture", source_url=None,
                observed_at=now - timedelta(days=1), published_at=now - timedelta(days=2 + i * 3),
                content_format=fmt, theme=theme, excerpt=excerpt, cta=cta, product_categories=cats,
                visible_engagement=None, is_synthetic=True))
    conns: dict[str, ChannelConnection] = {}
    for key in ORDER:
        a = DemoAdapter(key)
        conn = ChannelConnection(workspace_id=workspace_id, adapter=a.key, platform=key,
                                 name=f"{SPECS[key].label} (demo adapter)", account_id=f"demo-{key}",
                                 account_label=f"poojaloka on {SPECS[key].label} (simulated)",
                                 status=ConnectionStatus.DEMO, last_verified_at=now, created_by=user_id)
        db.add(conn)
        conns[key] = conn
    db.flush()

    hist = _history(db, brand, conns, user_id, now)
    audit.record(db, "demo.seeded", workspace_id=workspace_id, user_id=user_id, brand="poojaloka")
    db.flush()
    return {"brand_id": brand.id, "products": res.created, "history_campaign_id": hist["campaign_id"],
            "experiment_id": hist["experiment_id"], "brand": "poojaloka"}


def _history(db: Session, brand: Brand, conns: dict, user_id: int | None, now: datetime) -> dict:
    """A labelled synthetic past campaign with an A/B test, so the learning loop has something to evaluate."""
    from ..services.creative import render_for_item

    camp = Campaign(workspace_id=brand.workspace_id, brand_id=brand.id, name="Navaratri essentials (synthetic history)",
                    objective="Test whether festival-guide carousels earn more saves than product showcases.",
                    audience=brand.audience, geography="Bengaluru", platforms=["instagram", "facebook"],
                    pillars=["Product showcase", "Festival guide"], status="completed", is_synthetic=True,
                    objective_metric="saves_per_reach", evaluation_window_days=7,
                    start_date=(now - timedelta(days=31)).date(), end_date=(now - timedelta(days=19)).date(),
                    success_criteria=[{"metric": "saves_per_reach", "comparator": "relative_lift_gte", "target": 0.15,
                                       "variant": "B", "baseline": "A"}], created_by=user_id)
    db.add(camp)
    db.flush()
    hyp = Hypothesis(workspace_id=brand.workspace_id, campaign_id=camp.id,
                     statement="Festival-guide carousels earn more saves per reach than single product showcases.",
                     mechanism="Checklists are useful to keep.", uncertainty="Synthetic history.",
                     validation_method="A/B on Instagram, 4 posts per variant.", status="selected", selected=True,
                     rank=1)
    db.add(hyp)
    db.flush()
    exp = Experiment(workspace_id=brand.workspace_id, campaign_id=camp.id, hypothesis_id=hyp.id,
                     name="Festival-guide carousels vs product showcases (synthetic history)",
                     variants=[{"label": "A", "description": "Product showcase post"},
                               {"label": "B", "description": "Festival-guide carousel"}],
                     primary_metric="saves_per_reach", success_criteria=camp.success_criteria,
                     min_sample_per_variant=3, status="running", intervention=hyp.statement,
                     baseline={"metric": "saves_per_reach", "value": None, "n": 0, "source": "none",
                               "filters": {"platform": "instagram"}, "note": "No earlier posts before this test."},
                     comparison_method="Within Instagram: permutation test on saves per reach.", is_synthetic=True)
    db.add(exp)
    db.flush()
    by_slug = {p.source_ref: p for p in db.execute(select(Product).where(Product.brand_id == brand.id)).scalars()}
    idx = 0
    for variant, ctype, pillar, slug, days_ago in HISTORY:
        p = by_slug.get(slug)
        if p is None:
            continue
        for platform in ("instagram", "facebook") if variant == "B" or days_ago % 2 == 0 else ("instagram",):
            conn = conns[platform]
            adapter = DemoAdapter(platform)
            when = datetime.combine((now - timedelta(days=days_ago)).date(), time(4, 0))  # 09:30 IST
            guide = pillar == "Festival guide"
            item = ContentItem(
                workspace_id=brand.workspace_id, brand_id=brand.id, campaign_id=camp.id, experiment_id=exp.id,
                variant_label=variant, platform=platform, content_type=ctype if platform == "instagram" or guide
                else "post", pillar=pillar,
                title=f"{pillar}: {p.name}", hook=("What you need for Navaratri" if guide else p.name),
                caption=("Kalash, mango leaves, coconut and kumkum — the essentials from our Navaratri guide."
                         if guide else f"{p.name} · {p.category}"), hashtags=["#Poojaloka", "#Navaratri"],
                cta="Save this list" if guide else "View the product", product_ids=[p.id],
                carousel_outline=["Cover", "Kalash", "Mango leaves", "Coconut and kumkum"] if guide else [],
                status=ContentStatus.DRAFT, planned_at=when, generated_by="demo-fixture", is_synthetic=True,
                created_by=user_id)
            db.add(item)
            db.flush()
            try:
                cover = render_for_item(db, item, show_price=False)
                item.asset_id = cover.id
            except Exception as exc:  # noqa: BLE001 - history images are decorative; never block seeding
                log.info("history image skipped for %s: %s", item.id, exc)
            item.status, item.scheduled_at, item.published_at = ContentStatus.PUBLISHED, when, when
            item.connection_id, item.external_id = conn.id, f"demo-{platform}-hist-{idx}"
            item.publish_verified, item.approved_version, item.approved_by = True, 1, user_id
            item.approved_at = when - timedelta(days=1)
            db.flush()
            add_revision(db, item, "demo-fixture", user_id)
            m = adapter.fetch_metrics(ConnectionContext(connection_id=conn.id, account_id="demo", config={},
                                                        credentials={}), item.external_id,
                                      hours_since=days_ago * 24, hints={"content_type": item.content_type,
                                                                        "pillar": pillar})
            db.add(MetricSnapshot(content_id=item.id, captured_at=when + timedelta(days=7), reach=m.reach,
                                  views=m.views, likes=m.likes, comments=m.comments, shares=m.shares, saves=m.saves,
                                  profile_visits=m.profile_visits, link_clicks=m.link_clicks, source="synthetic",
                                  raw=m.raw))
            db.add(PublishAttempt(content_id=item.id, connection_id=conn.id, adapter=conn.adapter, live=False,
                                  idempotency_key=f"{item.id}:v{item.version}", success=True,
                                  external_id=item.external_id, provider_status="SIMULATED_PUBLISHED",
                                  summary="Synthetic history.", attempted_at=when))
            idx += 1
    return {"campaign_id": camp.id, "experiment_id": exp.id}


DEMO_GOAL = ("Create a seven-day Deepavali campaign for Poojaloka's diyas, brass items and pooja essentials across "
             "Instagram, Facebook, Pinterest and Threads, aimed at families in Bengaluru.")
SEVEN_PLATFORM_GOAL = ("Plan a five-day Deepavali campaign for Poojaloka across all seven platforms, aimed at families "
                       "in Bengaluru.")
EVAL_GOAL = "Evaluate last month's results and recommend what to change next."
