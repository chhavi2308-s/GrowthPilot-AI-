"""Research-driven campaign generation: start, follow progress per day, retry a day, approve the campaign."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from ... import worker
from ...models import Asset, Brand, CampaignGeneration, ContentItem
from ...services import campaign_generation as gen_service
from ...services.errors import PermissionDenied, WorkflowError
from .. import serializers as S
from ..deps import ApiError, Ctx, get_ctx, rate_limit
from .brand import current_brand

router = APIRouter(prefix="/api", tags=["campaign-generation"])


def _guard(fn):
    try:
        return fn()
    except PermissionDenied as exc:
        raise ApiError(403, str(exc), "forbidden") from exc
    except WorkflowError as exc:
        raise ApiError(400, str(exc), "workflow") from exc


def _asset(ctx: Ctx, aid: int | None) -> dict | None:
    a = ctx.db.get(Asset, aid) if aid else None
    return S.asset(a) if a is not None and a.workspace_id == ctx.workspace.id else None


def _detail(ctx: Ctx, gen: CampaignGeneration) -> dict:
    days = []
    for r in gen.day_rows:
        item = ctx.db.get(ContentItem, r.content_id) if r.content_id else None
        days.append({
            "day": r.day, "status": r.status, "attempts": r.attempts, "error": r.error,
            "image_prompt": r.image_prompt, "asset": _asset(ctx, r.asset_id), "ai_asset": _asset(ctx, r.ai_asset_id),
            "content": None if item is None else {
                "id": item.id, "status": item.status, "version": item.version,
                "approved_version": item.approved_version, "title": item.title, "hook": item.hook,
                "caption": item.caption, "hashtags": item.hashtags, "cta": item.cta,
                "planned_at": S.dt(item.planned_at), "review_note": item.review_note,
                "validation_failures": (item.validation or {}).get("failures"),
                "validation_warnings": (item.validation or {}).get("warnings")},
        })
    research = gen.research or {}
    return {"id": gen.id, "campaign_id": gen.campaign_id, "brand_id": gen.brand_id, "goal": gen.goal,
            "days": gen.days, "platform": gen.platform, "aspect_ratio": gen.aspect_ratio, "status": gen.status,
            "stage": gen.stage, "error": gen.error, "image_mode": gen.image_mode, "image_model": gen.image_model,
            "budget_usd": gen.budget_usd, "created_at": S.dt(gen.created_at), "updated_at": S.dt(gen.updated_at),
            "research": {k: research.get(k) for k in ("status", "mode", "queries", "sources", "errors", "assumptions",
                                                      "searched_at", "storage_rights")}
            | {"questions": (research.get("plan") or {}).get("questions", [])},
            "strategy": gen.strategy, "day_items": days, "usage": gen_service.usage_summary(ctx.db, gen)}


class GenerateIn(BaseModel):
    goal: str = Field(min_length=8, max_length=1000)
    days: int = Field(default=5, ge=1, le=gen_service.MAX_DAYS)
    platform: str = "instagram"
    aspect_ratio: str = "4:5"
    brand_id: int | None = None


@router.get("/campaign-generations/estimate")
def estimate(days: int = 5, ctx: Ctx = Depends(get_ctx)):
    if not 1 <= days <= gen_service.MAX_DAYS:
        raise ApiError(400, f"A campaign can have 1–{gen_service.MAX_DAYS} days.", "invalid")
    brands = ctx.db.execute(select(Brand).where(Brand.workspace_id == ctx.workspace.id).order_by(Brand.id)).scalars()
    return gen_service.estimate(days) | {"brands": [{"id": b.id, "name": b.name} for b in brands]}


@router.post("/campaign-generations")
def start(body: GenerateIn, ctx: Ctx = Depends(get_ctx)):
    ctx.require_editor()
    rate_limit(f"campaign-gen:{ctx.workspace.id}", 10, 3600)
    brand = ctx.get(Brand, body.brand_id) if body.brand_id else current_brand(ctx)
    gen = _guard(lambda: gen_service.start(ctx.db, workspace_id=ctx.workspace.id, brand=brand, user_id=ctx.user.id,
                                           role=ctx.role, goal=body.goal, days=body.days, platform=body.platform,
                                           aspect_ratio=body.aspect_ratio))
    ctx.db.commit()
    worker.notify()
    return _detail(ctx, gen)


@router.get("/campaign-generations/{gid}")
def get_generation(gid: int, ctx: Ctx = Depends(get_ctx)):
    return _detail(ctx, ctx.get(CampaignGeneration, gid))


@router.get("/campaigns/{cid}/generation")
def campaign_generation(cid: int, ctx: Ctx = Depends(get_ctx)):
    gen = ctx.db.execute(select(CampaignGeneration).where(CampaignGeneration.campaign_id == cid,
                                                          CampaignGeneration.workspace_id == ctx.workspace.id)
                         .order_by(CampaignGeneration.id.desc()).limit(1)).scalar_one_or_none()
    return {"generation": _detail(ctx, gen) if gen else None}


@router.post("/campaign-generations/{gid}/retry")
def retry(gid: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_editor()
    gen = ctx.get(CampaignGeneration, gid)
    _guard(lambda: gen_service.retry(ctx.db, gen, user_id=ctx.user.id, role=ctx.role))
    ctx.db.commit()
    worker.notify()
    return _detail(ctx, gen)


class DayRetryIn(BaseModel):
    regenerate: bool = False


@router.post("/campaign-generations/{gid}/days/{day}/retry")
def retry_day(gid: int, day: int, body: DayRetryIn, ctx: Ctx = Depends(get_ctx)):
    ctx.require_editor()
    rate_limit(f"campaign-gen-day:{ctx.workspace.id}", 30, 3600)
    gen = ctx.get(CampaignGeneration, gid)
    _guard(lambda: gen_service.retry_day(ctx.db, gen, day, user_id=ctx.user.id, role=ctx.role,
                                         regenerate=body.regenerate))
    ctx.db.commit()
    worker.notify()
    return _detail(ctx, gen)


@router.post("/campaign-generations/{gid}/days/{day}/local-poster")
def local_poster(gid: int, day: int, ctx: Ctx = Depends(get_ctx)):
    ctx.require_editor()
    gen = ctx.get(CampaignGeneration, gid)
    _guard(lambda: gen_service.local_poster(ctx.db, gen, day, user_id=ctx.user.id, role=ctx.role))
    ctx.db.commit()
    return _detail(ctx, gen)


class ApproveAllIn(BaseModel):
    acknowledge_warnings: bool = False


@router.post("/campaign-generations/{gid}/approve-all")
def approve_all(gid: int, body: ApproveAllIn, ctx: Ctx = Depends(get_ctx)):
    ctx.require_owner()
    gen = ctx.get(CampaignGeneration, gid)
    result = _guard(lambda: gen_service.approve_all(ctx.db, gen, user_id=ctx.user.id, role=ctx.role,
                                                    acknowledge_warnings=body.acknowledge_warnings, ip=ctx.ip))
    ctx.db.commit()
    return _detail(ctx, gen) | {"result": result}
