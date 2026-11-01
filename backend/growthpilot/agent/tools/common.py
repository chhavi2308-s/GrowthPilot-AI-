from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

from ...catalogue import foreign_store_product
from ...llm import get_llm, provider_of
from ...models import Brand, ContentItem, Experiment, Persona, Product, Recommendation
from ..generation import Generator, get_generator
from ..registry import ToolBlocked, ToolContext, ToolError

BRAND_FIELDS = ("name", "description", "positioning", "voice", "tone", "audience", "location", "goals", "website",
                "colors", "visual_guidelines", "approved_claims", "restricted_claims", "prohibited_messaging",
                "constraints", "content_pillars", "languages", "marketing_objectives", "platform_preferences",
                "posting_constraints")


def brand(ctx: ToolContext) -> Brand:
    if not ctx.brand_id:
        raise ToolBlocked("No brand is set up for this workspace. Create one in Brand Brain first.",
                          blocker="brand_missing")
    b = ctx.db.get(Brand, ctx.brand_id)
    if b is None or b.workspace_id != ctx.workspace_id:
        raise ToolBlocked("Brand not found.", blocker="brand_missing")
    return b


def snapshot(b: Brand) -> dict:
    """The brand snapshot every role works from in this run (hash shown in the trace)."""
    data = {k: getattr(b, k) for k in BRAND_FIELDS}
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:12]
    return {"hash": digest, **data}


def generator(ctx: ToolContext) -> Generator:
    return get_generator(get_llm(provider_of(ctx.run.planner)))


def items(ctx: ToolContext, ids: list[int], *, key: str = "content_ids") -> list[ContentItem]:
    if not ids:
        ids = list(ctx.state.get(key, []))
    rows = ctx.db.execute(select(ContentItem).where(ContentItem.id.in_(ids or [0]),
                                                    ContentItem.workspace_id == ctx.workspace_id)).scalars().all()
    if not rows:
        raise ToolError("No matching content items in this workspace.")
    return sorted(rows, key=lambda i: (i.planned_at or i.created_at, i.id))


def brief_for(db, b: Brand, *, workspace_id: int, params: dict | None = None, state: dict | None = None) -> dict:
    """The brief every generator call receives (also used outside agent runs, e.g. by API regenerate buttons)."""
    params = params or {}
    state = state or {}
    learnings = [e.learnings for e in db.execute(
        select(Experiment).where(Experiment.workspace_id == workspace_id, Experiment.status == "evaluated")
        .order_by(Experiment.evaluated_at.desc()).limit(3)).scalars() if e.learnings]
    recs = [r.title for r in db.execute(select(Recommendation).where(
        Recommendation.workspace_id == workspace_id, Recommendation.status.in_(["open", "applied"])).limit(5)).scalars()]
    personas = db.execute(select(Persona).where(Persona.brand_id == b.id)).scalars().all()
    src = b.catalogue_source or {}
    festival = state.get("festival")
    if festival is None and params.get("occasion"):
        occ = params["occasion"].lower()[:5]
        festival = next((f for f in src.get("festivals") or [] if occ in f.get("name", "").lower()), None)
    return {
        "brand": {k: getattr(b, k) for k in BRAND_FIELDS},
        "personas": [{"name": p.name, "description": p.description, "goals": p.goals, "pain_points": p.pain_points}
                     for p in personas],
        "focus_category": params.get("category"), "geography": params.get("geography") or b.location,
        "occasion": params.get("occasion"), "learnings": learnings, "open_recommendations": recs,
        "festival": festival, "articles": (state.get("articles") or [
            {"title": a.get("title"), "excerpt": a.get("excerpt"), "slug": a.get("slug"), "body": (a.get("body") or "")[:1500]}
            for a in (src.get("articles") or [])])[:5],
    }


def brief(ctx: ToolContext) -> dict:
    return brief_for(ctx.db, brand(ctx), workspace_id=ctx.workspace_id, params=ctx.state.get("params", {}),
                     state=ctx.state)


def products(ctx: ToolContext, ids: list[int] | None = None, category: str | None = None) -> list[Product]:
    q = select(Product).where(Product.workspace_id == ctx.workspace_id, Product.brand_id == ctx.brand_id,
                              Product.is_active.is_(True))
    if ids:
        q = q.where(Product.id.in_(ids))
    elif category:
        q = q.where(Product.category.ilike(f"%{category}%"))
    b = brand(ctx)
    return [p for p in ctx.db.execute(q.order_by(Product.id)).scalars() if not foreign_store_product(b, p)]


def promotable(p: Product) -> tuple[bool, str]:
    """Whether a product can be promoted, and why not."""
    codes = {q.get("code") for q in p.data_quality or []}
    if "possible_test_listing" in codes:
        return False, "looks like a test listing"
    if "price_suspicious" in codes:
        return False, "price looks like a data error"
    if "stock_negative" in codes:
        return False, "negative stock at the source"
    if p.stock is not None and p.stock <= 0 and not p.allow_backorder:
        return False, "out of stock"
    if not p.is_active:
        return False, "inactive"
    return True, ""
