from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update

from ... import audit, worker
from ... import catalogue as catalogue_mod
from ...adapters import available_adapters
from ...agent.generation import SlotContentOut, get_generator, product_dict
from ...agent.tools.common import brief_for
from ...config import get_settings
from ...llm import LLMError, get_llm
from ...models import (
    AgentRun,
    ApprovalRequest,
    Asset,
    Brand,
    Campaign,
    CanvaPosterJob,
    ChannelConnection,
    ContentItem,
    ContentStatus,
    Hypothesis,
    MetricSnapshot,
    Product,
    ResearchFinding,
    utcnow,
)
from ...platforms import SPECS
from ...services import content as workflow
from ...services import creative as creative_service
from ...services import publishing
from ...services import variants as variants_service
from ...services.errors import PermissionDenied, WorkflowError
from ...services.validation import final_caption, validate_item
from .. import serializers as S
from ..deps import ApiError, Ctx, get_ctx, rate_limit
from .agent import maybe_auto_resume
from .brand import current_brand

router = APIRouter(prefix="/api", tags=["content"])


class CanvaPosterIn(BaseModel):
    prompt: str | None = Field(default=None, max_length=5000)
    regenerate: bool = False
    reference_asset_id: int | None = Field(default=None, gt=0)


class CanvaChoiceIn(BaseModel):
    candidate_id: str = Field(min_length=1, max_length=300)
    request_key: str = Field(pattern=r"^[0-9a-f]{64}$")


def _poster_job_reply(job) -> dict:
    return {"job_id": job.id, "status": job.status, "calls_used": job.calls_used}


@router.post("/content/{cid}/canva-poster", status_code=202)
def enqueue_canva_poster(cid: int, ctx: Ctx = Depends(get_ctx), body: CanvaPosterIn | None = None):
    """Save the user's Image brief and queue options, without choosing a design."""
    from sqlalchemy.exc import IntegrityError

    from ...creative.canva_mcp import CanvaMCPError
    from ...services.canva_choices import image_prompt, request_key
    from ...services.canva_poster_pipeline import MAX_JOB_CALLS, prepare_canva_brief

    ctx.require_editor()
    rate_limit(f"canva-poster:{ctx.workspace.id}:{ctx.user.id}", 8, 3600)
    item = ctx.get(ContentItem, cid)
    if getattr(item, "status", ContentStatus.DRAFT) in workflow.FINAL:
        raise ApiError(400, "This post can no longer be edited.", "workflow")
    brand = ctx.get(Brand, item.brand_id)
    job = ctx.db.execute(select(CanvaPosterJob).where(CanvaPosterJob.workspace_id == ctx.workspace.id,
                                                       CanvaPosterJob.content_id == item.id)).scalar_one_or_none()
    if job and job.status in {"queued", "generating", "exporting", "qa"}:
        if body and body.prompt is not None and body.prompt.strip() != image_prompt(item):
            raise ApiError(409, "Wait for the current generation before changing the Image brief.", "busy")
        if (body and "reference_asset_id" in body.model_fields_set and body.reference_asset_id !=
                (item.canva_poster or {}).get("reference_asset_id")):
            raise ApiError(409, "Wait for the current generation before changing the reference image.", "busy")
        return _poster_job_reply(job)
    original_saved = dict(getattr(item, "canva_poster", None) or {})
    reference_id = original_saved.get("reference_asset_id")
    if body and "reference_asset_id" in body.model_fields_set:
        reference_id = body.reference_asset_id
    if reference_id:
        reference = ctx.get(Asset, reference_id)
        if reference.brand_id not in (None, item.brand_id) or reference.content_type not in {"image/png", "image/jpeg"}:
            raise ApiError(400, "Choose a PNG or JPEG reference image from this brand's assets.", "invalid_reference")
    item.canva_poster = {**original_saved, "reference_asset_id": reference_id}
    if body and body.prompt is not None:
        if not body.prompt.strip():
            raise ApiError(400, "Enter an Image brief to generate options.", "invalid_brief")
        _guard(lambda: workflow.update(ctx.db, item, {"image_brief": body.prompt}, user_id=ctx.user.id,
                                      role=ctx.role, note="Saved Image brief for Canva options."))
    try:
        prepare_canva_brief(ctx.db, item, brand)
    except CanvaMCPError as exc:
        raise ApiError(400, str(exc), "invalid_brief") from None
    saved = getattr(item, "canva_poster", None) or {}
    key = request_key(item)
    if (job and job.status in {"awaiting_selection", "completed"} and not (body and body.regenerate)
            and saved.get("request_key", key) == key):
        return _poster_job_reply(job)
    # A failed export can resume the selected design; a changed prompt or an
    # explicit regenerate request starts fresh options, never silently reuses it.
    resume = (job and job.status == "failed" and (saved.get("design_id") or saved.get("selected_candidate_id"))
              and saved.get("request_key", key) == key and not (body and body.regenerate))
    new_batch = bool(body and body.regenerate) or original_saved.get("request_key", key) != key
    if job and not resume and not new_batch and job.calls_used >= MAX_JOB_CALLS:
        raise ApiError(429, "This batch has reached its retry limit. Generate new options to start a fresh batch.",
                       "generation_limit")
    if job:
        claimed = ctx.db.execute(update(CanvaPosterJob).where(CanvaPosterJob.id == job.id,
            CanvaPosterJob.workspace_id == ctx.workspace.id, CanvaPosterJob.status == job.status)
            .values(status="queued", error=""))
        if claimed.rowcount != 1:
            ctx.db.rollback()
            raise ApiError(409, "Another generation or choice already started. Refresh this post.", "busy")
        if new_batch:
            job.calls_used = 0
    else:
        job = CanvaPosterJob(content_id=item.id, workspace_id=ctx.workspace.id, status="queued")
        ctx.db.add(job)
        try:
            ctx.db.flush()
        except IntegrityError:
            ctx.db.rollback()
            job = ctx.db.execute(select(CanvaPosterJob).where(CanvaPosterJob.content_id == item.id,
                CanvaPosterJob.workspace_id == ctx.workspace.id)).scalar_one()
            return _poster_job_reply(job)
    if not resume:
        item.canva_poster = {"job_id": job.id, "request_key": key, "prompt": image_prompt(item), "phase": "queued",
                             "reference_asset_id": reference_id}
    if getattr(item, "status", ContentStatus.DRAFT) in {ContentStatus.APPROVED, ContentStatus.SCHEDULED}:
        item.status, item.scheduled_at = ContentStatus.DRAFT, None
    workflow._clear_approval(item)
    try:
        ctx.db.commit()
    except IntegrityError:
        ctx.db.rollback()
        job = ctx.db.execute(select(CanvaPosterJob).where(CanvaPosterJob.content_id == item.id,
            CanvaPosterJob.workspace_id == ctx.workspace.id)).scalar_one()
    worker.notify()
    return _poster_job_reply(job)


@router.post("/content/{cid}/canva-poster/select", status_code=202)
def select_canva_candidate(cid: int, body: CanvaChoiceIn, ctx: Ctx = Depends(get_ctx)):
    from ...services.canva_choices import request_key

    ctx.require_editor()
    rate_limit(f"canva-select:{ctx.workspace.id}:{ctx.user.id}", 20, 3600)
    item = ctx.get(ContentItem, cid)
    if item.status in workflow.FINAL:
        raise ApiError(400, "This post can no longer be edited.", "workflow")
    job = ctx.db.execute(select(CanvaPosterJob).where(CanvaPosterJob.content_id == item.id,
        CanvaPosterJob.workspace_id == ctx.workspace.id)).scalar_one_or_none()
    saved = item.canva_poster or {}
    if (not job or saved.get("job_id") != job.id or saved.get("request_key") != body.request_key
            or request_key(item) != body.request_key):
        raise ApiError(409, "These options are out of date. Generate options from your current Image brief.", "stale_options")
    allowed = {row.get("id") for row in saved.get("candidates", [])}
    if body.candidate_id not in allowed:
        raise ApiError(400, "Choose one of this post's generated options.", "invalid_candidate")
    if (saved.get("selected_candidate_id") == body.candidate_id
            and job.status in {"queued", "generating", "exporting", "qa", "completed"}):
        return _poster_job_reply(job)
    if job.status != "awaiting_selection":
        raise ApiError(409, "This generation is no longer waiting for a choice.", "busy")
    claimed = ctx.db.execute(update(CanvaPosterJob).where(CanvaPosterJob.id == job.id,
        CanvaPosterJob.workspace_id == ctx.workspace.id, CanvaPosterJob.status == "awaiting_selection")
        .values(status="queued", error=""))
    if claimed.rowcount != 1:
        ctx.db.rollback()
        raise ApiError(409, "An option has already been selected. Refresh this post.", "busy")
    item.canva_poster = {**saved, "selected_candidate_id": body.candidate_id, "phase": "queued"}
    workflow._clear_approval(item)
    ctx.db.commit()
    worker.notify()
    return {"job_id": job.id, "status": "queued", "calls_used": job.calls_used}


@router.get("/content/{cid}/canva-poster")
def canva_poster_status(cid: int, ctx: Ctx = Depends(get_ctx)):
    from ...services.canva_choices import option_previews

    item = ctx.get(ContentItem, cid)
    job = ctx.db.execute(select(CanvaPosterJob).where(CanvaPosterJob.content_id == item.id,
        CanvaPosterJob.workspace_id == ctx.workspace.id)).scalar_one_or_none()
    poster = item.canva_poster
    if job and isinstance(poster, dict) and poster.get("job_id") == job.id and poster.get("candidates"):
        poster = option_previews(ctx.workspace.id, job.id, poster)
    return {"job": None if job is None else {"id": job.id, "status": job.status,
            "calls_used": job.calls_used, "error": job.error}, "poster": poster}


@router.get("/content/{cid}/canva-poster/edit-link")
async def canva_poster_edit_link(cid: int, ctx: Ctx = Depends(get_ctx)):
    """Fetch a short-lived Canva edit link on demand; it is never stored or logged."""
    from ...config import get_settings
    from ...creative.canva_mcp import CanvaMCPError, connected_workspace_client, safe_canva_message
    item = ctx.get(ContentItem, cid)
    design_id = (item.canva_poster or {}).get("design_id")
    if not design_id:
        raise ApiError(404, "No committed Canva design exists for this item.", "not_found")
    try:
        async with connected_workspace_client(ctx.db, ctx.workspace.id, get_settings()) as (client, _credential):
            if "get-design" not in client.tools:
                raise CanvaMCPError("Canva MCP did not expose get-design.")
            schema = client.schema("get-design")
            props = schema.get("properties", {})
            values = {"design_id": design_id, "user_intent": "Fetch this workspace design edit link."}
            args = {k: v for k, v in values.items() if k in props}
            if set(schema.get("required", [])) - set(args):
                raise CanvaMCPError("get-design has required inputs GrowthPilot cannot safely map.")
            result = await client.call("get-design", args)
            from ...creative.canva_mcp import _nested
            link = _nested(result, "edit_url", "editUrl")
            if not isinstance(link, str) or not link.startswith("https://"):
                raise CanvaMCPError("Canva did not return an HTTPS edit URL.")
            return {"edit_url": link}
    except Exception as exc:
        raise ApiError(502, safe_canva_message(str(exc)), "canva_error") from None


def _guard(fn):
    try:
        return fn()
    except PermissionDenied as exc:
        raise ApiError(403, str(exc), "forbidden") from exc
    except WorkflowError as exc:
        raise ApiError(400, str(exc), "workflow") from exc


def _to_utc(value: str, ctx: Ctx) -> datetime:
    try:
        d = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(400, "Invalid date/time.", "validation") from exc
    if d.tzinfo is None:
        d = d.replace(tzinfo=ctx.tz)
    return d.astimezone(UTC).replace(tzinfo=None)


def _detail(ctx: Ctx, item: ContentItem) -> dict:
    d = S.content(item, ctx.tz, full=True)
    assets = [ctx.db.get(Asset, i) for i in ([item.asset_id] if item.asset_id else []) + list(item.slide_asset_ids or [])]
    d["assets"] = [S.asset(a, full=True) for a in assets if a]
    prods = ctx.db.execute(select(Product).where(Product.id.in_(item.product_ids or [0]))).scalars().all()
    d["products"] = [S.product(p) for p in prods]
    d["final_caption"] = final_caption(item)
    sp = SPECS.get(item.platform)
    d["platform_spec"] = sp.as_dict() if sp else None
    d["approval_intact"] = workflow.approval_intact(ctx.db, item) if item.approved_hash else None
    master_id = item.master_id or item.id
    family = ctx.db.execute(select(ContentItem).where(or_(ContentItem.id == master_id,
                                                          ContentItem.master_id == master_id))
                            .order_by(ContentItem.id)).scalars().all()
    d["family"] = [S.content(f, ctx.tz) for f in family]
    camp = item.campaign
    if camp:
        h = ctx.db.execute(select(Hypothesis).where(Hypothesis.campaign_id == camp.id,
                                                    Hypothesis.selected.is_(True))).scalars().first()
        d["hypothesis"] = S.hypothesis(h) if h else None
        d["evidence"] = [S.finding(f) for f in ctx.db.execute(select(ResearchFinding).where(
            ResearchFinding.id.in_((h.evidence_ids if h else None) or [0]))).scalars()]
    return d


# ── list / create / edit ────────────────────────────────────────────────


@router.get("/content")
def list_content(status: str = "", campaign_id: int | None = None, platform: str = "", content_type: str = "",
                 q: str = "", main_only: bool = False, ctx: Ctx = Depends(get_ctx)):
    query = select(ContentItem).where(ContentItem.workspace_id == ctx.workspace.id)
    if main_only:
        query = query.where(ContentItem.master_id.is_(None))
    if status:
        query = query.where(ContentItem.status.in_(status.split(",")))
    if campaign_id:
        query = query.where(ContentItem.campaign_id == campaign_id)
    if platform:
        query = query.where(ContentItem.platform == platform)
    if content_type:
        query = query.where(ContentItem.content_type == content_type)
    if q:
        like = f"%{q[:100]}%"
        query = query.where(or_(ContentItem.title.ilike(like), ContentItem.caption.ilike(like)))
    rows = ctx.db.execute(query.order_by(ContentItem.updated_at.desc()).limit(300)).scalars().all()
    return [S.content(i, ctx.tz) for i in rows]


class ContentIn(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    hook: str | None = Field(default=None, max_length=500)
    caption: str | None = Field(default=None, max_length=5000)
    hashtags: list[str] | None = Field(default=None, max_length=40)
    cta: str | None = Field(default=None, max_length=255)
    image_brief: str | None = Field(default=None, max_length=5000)
    carousel_outline: list[str] | None = Field(default=None, max_length=20)
    video_script: str | None = Field(default=None, max_length=10000)
    content_type: str | None = None
    pillar: str | None = Field(default=None, max_length=120)
    product_ids: list[int] | None = None
    asset_id: int | None = None
    slide_asset_ids: list[int] | None = None
    planned_at: str | None = None
    note: str = Field(default="", max_length=1000)


def _changes(ctx: Ctx, body: ContentIn, current_ids: list[int] | None = None) -> dict:
    data = body.model_dump(exclude_unset=True)
    data.pop("note", None)
    if "planned_at" in data:
        data["planned_at"] = _to_utc(data["planned_at"], ctx) if data["planned_at"] else None
    for key in ("asset_id",):
        if data.get(key):
            ctx.get(Asset, data[key])
    for aid in data.get("slide_asset_ids") or []:
        ctx.get(Asset, aid)
    new_ids = [pid for pid in data.get("product_ids") or [] if pid not in (current_ids or [])]
    if new_ids:
        b = current_brand(ctx)
        for pid in new_ids:
            p = ctx.get(Product, pid)
            if not catalogue_mod.usable_for_brand(b, p):
                raise ApiError(400, f"{p.name} is archived or comes from another store's catalogue; choose a "
                               "current product.", "validation")
    return data


class NewContentIn(ContentIn):
    platform: str = "instagram"
    campaign_id: int | None = None


@router.post("/content")
def create_content(body: NewContentIn, ctx: Ctx = Depends(get_ctx)):
    ctx.require_editor()
    b = current_brand(ctx)
    if body.campaign_id:
        ctx.get(Campaign, body.campaign_id)
    if body.platform not in SPECS:
        raise ApiError(400, "Unknown platform.", "validation")
    item = ContentItem(workspace_id=ctx.workspace.id, brand_id=b.id, campaign_id=body.campaign_id,
                       platform=body.platform, created_by=ctx.user.id, generated_by="human")
    ctx.db.add(item)
    ctx.db.flush()
    workflow.add_revision(ctx.db, item, "human", ctx.user.id, "Created")
    changes = _changes(ctx, body)
    _guard(lambda: workflow.update(ctx.db, item, changes, user_id=ctx.user.id, role=ctx.role, ip=ctx.ip))
    ctx.db.commit()
    return _detail(ctx, item)


@router.get("/content/{cid}")
def get_content(cid: int, ctx: Ctx = Depends(get_ctx)):
    return _detail(ctx, ctx.get(ContentItem, cid))


@router.put("/content/{cid}")
def edit_content(cid: int, body: ContentIn, ctx: Ctx = Depends(get_ctx)):
    item = ctx.get(ContentItem, cid)
    changes = _changes(ctx, body, current_ids=list(item.product_ids or []))
    _guard(lambda: workflow.update(ctx.db, item, changes, user_id=ctx.user.id, role=ctx.role, note=body.note,
                                   ip=ctx.ip))
    ctx.db.commit()
    return _detail(ctx, item)


class VersionIn(BaseModel):
    version: int


@router.post("/content/{cid}/restore")
def restore(cid: int, body: VersionIn, ctx: Ctx = Depends(get_ctx)):
    item = ctx.get(ContentItem, cid)
    _guard(lambda: workflow.restore_revision(ctx.db, item, body.version, user_id=ctx.user.id, role=ctx.role))
    ctx.db.commit()
    return _detail(ctx, item)


class RegenIn(BaseModel):
    instructions: str = Field(default="", max_length=2000)


@router.post("/content/{cid}/regenerate")
def regenerate(cid: int, body: RegenIn, ctx: Ctx = Depends(get_ctx)):
    """Regenerate the caption as a new version (approval is reset if it had been approved)."""
    ctx.require_editor()
    rate_limit(f"regen:{ctx.workspace.id}", 60, 3600)
    item = ctx.get(ContentItem, cid)
    try:
        gen = get_generator(get_llm())
    except LLMError as exc:
        raise ApiError(400, str(exc), "llm_unavailable") from exc
    brand = ctx.db.get(Brand, item.brand_id)
    prods = [product_dict(p) for p in catalogue_mod.usable_products(ctx.db, brand, list(item.product_ids or []))]
    run = ctx.db.get(AgentRun, item.run_id) if item.run_id else None
    brief = brief_for(ctx.db, brand, workspace_id=ctx.workspace.id, params=(run.state or {}).get("params") if run else None)
    current = {"title": item.title, "hook": item.hook, "caption": item.caption, "hashtags": item.hashtags,
               "cta": item.cta, "image_brief": item.image_brief, "carousel_outline": item.carousel_outline,
               "product_ids": item.product_ids}
    try:
        master = ctx.db.get(ContentItem, item.master_id) if item.master_id else None
        if master is not None:  # a platform variant: re-adapt from the main post, nothing else changes
            fields, _t, rationale = variants_service.adapted_fields(gen, brief, master, item.platform, prods)
            fields.pop("video_script", None)
            _guard(lambda: workflow.update(ctx.db, item, fields, user_id=ctx.user.id, role=ctx.role, source=gen.name,
                                           note=f"Regenerated the {item.platform} variant"))
            item.rationale = rationale[:2000]
            validate_item(ctx.db, item)
            ctx.db.commit()
            return _detail(ctx, item) | {"generator": gen.name, "is_mock": gen.is_mock}
        if body.instructions.strip() and not gen.is_mock:
            out, _ = gen.revise(brief, current, [{"message": body.instructions, "claim": ""}], prods)
        else:
            slot = {"index": item.version, "date": item.planned_at.isoformat() if item.planned_at else None,
                    "pillar": item.pillar or "Product spotlight", "content_type": item.content_type,
                    "variant": item.variant_label}
            out, _ = gen.slot_content(brief, slot, prods)
    except LLMError as exc:
        raise ApiError(502, str(exc), "llm_error") from exc
    assert isinstance(out, SlotContentOut)
    _guard(lambda: workflow.update(ctx.db, item, {"hook": out.hook, "caption": out.caption, "hashtags": out.hashtags,
                                                  "cta": out.cta, "image_brief": out.image_brief},
                                   user_id=ctx.user.id, role=ctx.role, source=gen.name,
                                   note=f"Regenerated{': ' + body.instructions[:200] if body.instructions else ''}"))
    item.rationale = out.rationale[:2000]
    validate_item(ctx.db, item)
    ctx.db.commit()
    return _detail(ctx, item) | {"generator": gen.name, "is_mock": gen.is_mock}


class CreativeIn(BaseModel):
    variation: int = Field(default=0, ge=0, le=5)
    template: str | None = None
    ai_background: bool = False


@router.post("/content/{cid}/creative")
def rerender(cid: int, body: CreativeIn, ctx: Ctx = Depends(get_ctx)):
    """Render this item's image(s) again (new version; any approval is reset). Other platforms are unaffected."""
    ctx.require_editor()
    rate_limit(f"render:{ctx.workspace.id}", 120, 3600)
    item = ctx.get(ContentItem, cid)
    if item.content_type not in ("post", "carousel"):
        raise ApiError(400, "Only image posts and carousels have rendered creatives.", "workflow")
    try:
        cover = creative_service.render_for_item(ctx.db, item, template=body.template, variation=body.variation,
                                                 ai_background=body.ai_background)
        slides = []
        if item.content_type == "carousel":
            outline = [x for x in (item.carousel_outline or [])[1:] if x.strip()][:5] or [item.cta or item.title]
            for i, line in enumerate(outline, start=2):
                slides.append(creative_service.render_for_item(ctx.db, item, slide=(i, len(outline) + 1),
                                                               headline=line, variation=body.variation).id)
    except WorkflowError as exc:
        raise ApiError(400, str(exc), "creative_check") from exc
    _guard(lambda: workflow.update(ctx.db, item, {"asset_id": cover.id, "slide_asset_ids": slides},
                                   user_id=ctx.user.id, role=ctx.role, source="creative-studio",
                                   note=f"Re-rendered ({cover.meta.get('template')}, variation {body.variation})"))
    validate_item(ctx.db, item)
    ctx.db.commit()
    return _detail(ctx, item)


class VariantsIn(BaseModel):
    platforms: list[str] = Field(min_length=1, max_length=7)


@router.post("/content/{cid}/variants")
def add_variants(cid: int, body: VariantsIn, ctx: Ctx = Depends(get_ctx)):
    """Create platform variants of a main post (each with its own approval and status)."""
    ctx.require_editor()
    item = ctx.get(ContentItem, cid)
    if item.master_id:
        raise ApiError(400, "Create variants from the main post.", "workflow")
    bad = [p for p in body.platforms if p not in SPECS]
    if bad:
        raise ApiError(400, f"Unknown platform(s): {', '.join(bad)}.", "validation")
    try:
        gen = get_generator(get_llm())
    except LLMError as exc:
        raise ApiError(400, str(exc), "llm_unavailable") from exc
    brand = ctx.db.get(Brand, item.brand_id)
    prods = [product_dict(p) for p in catalogue_mod.usable_products(ctx.db, brand, list(item.product_ids or []))]
    brief = brief_for(ctx.db, brand, workspace_id=ctx.workspace.id)
    created = []
    for p in body.platforms:
        if p == item.platform or variants_service.existing_variant(ctx.db, item, p):
            continue
        try:
            v, _ = variants_service.create_variant(ctx.db, item, p, gen, brief, prods, created_by=ctx.user.id)
        except LLMError as exc:
            raise ApiError(502, str(exc), "llm_error") from exc
        if v.content_type in ("post", "carousel"):
            try:
                cover = creative_service.render_for_item(ctx.db, v)
                v.asset_id = cover.id
            except WorkflowError:
                pass
        validate_item(ctx.db, v)
        created.append(v.id)
    ctx.db.commit()
    return {"created": created, "item": _detail(ctx, item)}


# ── workflow actions ─────────────────────────────────────────────────────


@router.post("/content/{cid}/validate")
def validate(cid: int, ctx: Ctx = Depends(get_ctx)):
    item = ctx.get(ContentItem, cid)
    validate_item(ctx.db, item)
    ctx.db.commit()
    return item.validation


@router.post("/content/{cid}/submit")
def submit(cid: int, ctx: Ctx = Depends(get_ctx)):
    item = ctx.get(ContentItem, cid)
    _guard(lambda: workflow.submit(ctx.db, item, user_id=ctx.user.id, role=ctx.role, ip=ctx.ip))
    ctx.db.commit()
    return _detail(ctx, item)


class ApproveIn(BaseModel):
    version: int
    acknowledge_warnings: bool = False
    authorize_publish: bool = False


@router.post("/content/{cid}/approve")
def approve(cid: int, body: ApproveIn, ctx: Ctx = Depends(get_ctx)):
    item = ctx.get(ContentItem, cid)
    _guard(lambda: workflow.approve(ctx.db, item, user_id=ctx.user.id, role=ctx.role, version=body.version,
                                    acknowledge_warnings=body.acknowledge_warnings,
                                    authorize_publish=body.authorize_publish, ip=ctx.ip))
    ctx.db.commit()
    maybe_auto_resume(ctx.db, item)
    ctx.db.commit()
    return _detail(ctx, item)


class AuthorizeIn(BaseModel):
    version: int


@router.post("/content/{cid}/authorize-publish")
def authorize_publish(cid: int, body: AuthorizeIn, ctx: Ctx = Depends(get_ctx)):
    item = ctx.get(ContentItem, cid)
    _guard(lambda: workflow.authorize_publish(ctx.db, item, user_id=ctx.user.id, role=ctx.role,
                                              version=body.version, ip=ctx.ip))
    ctx.db.commit()
    return _detail(ctx, item)


class NoteIn(BaseModel):
    note: str = Field(default="", max_length=2000)


@router.post("/content/{cid}/request-changes")
def request_changes(cid: int, body: NoteIn, ctx: Ctx = Depends(get_ctx)):
    item = ctx.get(ContentItem, cid)
    _guard(lambda: workflow.request_changes(ctx.db, item, user_id=ctx.user.id, role=ctx.role, note=body.note,
                                            ip=ctx.ip))
    ctx.db.commit()
    maybe_auto_resume(ctx.db, item)
    ctx.db.commit()
    return _detail(ctx, item)


@router.post("/content/{cid}/cancel")
def cancel(cid: int, ctx: Ctx = Depends(get_ctx)):
    item = ctx.get(ContentItem, cid)
    _guard(lambda: workflow.cancel(ctx.db, item, user_id=ctx.user.id, role=ctx.role, ip=ctx.ip))
    ctx.db.commit()
    maybe_auto_resume(ctx.db, item)
    ctx.db.commit()
    return _detail(ctx, item)


class ScheduleIn(BaseModel):
    connection_id: int
    when: str | None = None  # ISO; naive values are in the workspace time zone; empty = planned time
    confirm: bool = False


@router.post("/content/{cid}/schedule")
def schedule(cid: int, body: ScheduleIn, ctx: Ctx = Depends(get_ctx)):
    item = ctx.get(ContentItem, cid)
    conn = ctx.get(ChannelConnection, body.connection_id)
    when = _to_utc(body.when, ctx) if body.when else (item.planned_at if item.planned_at and item.planned_at > utcnow()
                                                      else utcnow() + timedelta(minutes=5))
    _guard(lambda: workflow.schedule(ctx.db, item, conn, when, user_id=ctx.user.id, role=ctx.role,
                                     confirmed=body.confirm, ip=ctx.ip))
    ctx.db.commit()
    worker.notify()
    return _detail(ctx, item)


@router.post("/content/{cid}/unschedule")
def unschedule(cid: int, ctx: Ctx = Depends(get_ctx)):
    item = ctx.get(ContentItem, cid)
    _guard(lambda: workflow.unschedule(ctx.db, item, user_id=ctx.user.id, role=ctx.role, ip=ctx.ip))
    ctx.db.commit()
    return _detail(ctx, item)


class PublishNowIn(BaseModel):
    confirm: bool = False


@router.post("/content/{cid}/publish-now")
def publish_now(cid: int, body: PublishNowIn, ctx: Ctx = Depends(get_ctx)):
    """Publish a scheduled item immediately (owner only; live adapters need explicit confirmation)."""
    ctx.require_owner()
    item = ctx.get(ContentItem, cid)
    if item.status != ContentStatus.SCHEDULED:
        raise ApiError(400, "Only scheduled items can be published now.", "workflow")
    adapter = available_adapters().get(item.connection.adapter) if item.connection else None
    if adapter and adapter.external and not body.confirm:
        raise ApiError(400, "Confirm that this will publish to a real account.", "confirm_required")
    item.scheduled_at = utcnow()
    audit.record(ctx.db, "content.publish_now", workspace_id=ctx.workspace.id, user_id=ctx.user.id,
                 target_type="content", target_id=item.id, ip=ctx.ip, live=bool(adapter and adapter.external))
    ctx.db.commit()
    publishing.publish_item(ctx.db, item.id, triggered_by=ctx.user.id)
    ctx.db.refresh(item)
    return _detail(ctx, item)


@router.post("/content/{cid}/verify")
def verify(cid: int, ctx: Ctx = Depends(get_ctx)):
    item = ctx.get(ContentItem, cid)
    state = publishing.verify(ctx.db, item)
    return {"state": state, "item": _detail(ctx, item)}


class ManualMetricsIn(BaseModel):
    reach: int | None = Field(default=None, ge=0)
    views: int | None = Field(default=None, ge=0)
    likes: int | None = Field(default=None, ge=0)
    comments: int | None = Field(default=None, ge=0)
    shares: int | None = Field(default=None, ge=0)
    saves: int | None = Field(default=None, ge=0)


@router.post("/content/{cid}/metrics")
def manual_metrics(cid: int, body: ManualMetricsIn, ctx: Ctx = Depends(get_ctx)):
    """Record metrics read off the platform by hand (live mode). Demo mode refuses, to keep data separate."""
    ctx.require_editor()
    if get_settings().is_demo:
        raise ApiError(400, "Manual metrics are only accepted in live mode; demo metrics are synthetic.", "demo")
    item = ctx.get(ContentItem, cid)
    if item.status != ContentStatus.PUBLISHED:
        raise ApiError(400, "Only published content has metrics.", "workflow")
    ctx.db.add(MetricSnapshot(content_id=item.id, source="manual", **body.model_dump()))
    audit.record(ctx.db, "metrics.manual", workspace_id=ctx.workspace.id, user_id=ctx.user.id, target_type="content",
                 target_id=item.id, ip=ctx.ip)
    ctx.db.commit()
    return _detail(ctx, item)


# ── approval inbox & calendar ────────────────────────────────────────────


@router.get("/approvals")
def approvals(ctx: Ctx = Depends(get_ctx)):
    items = ctx.db.execute(select(ContentItem).where(ContentItem.workspace_id == ctx.workspace.id,
                                                     ContentItem.status == ContentStatus.IN_REVIEW)
                           .order_by(ContentItem.planned_at)).scalars().all()
    conns = ctx.db.execute(select(ChannelConnection).where(ChannelConnection.workspace_id == ctx.workspace.id)
                           ).scalars().all()
    allowed = available_adapters()
    out = []
    for i in items:
        validate_item(ctx.db, i)
        d = _detail(ctx, i)
        camp = i.campaign
        evidence = []
        if camp:
            h = ctx.db.execute(select(Hypothesis).where(Hypothesis.campaign_id == camp.id,
                                                        Hypothesis.selected.is_(True))).scalar_one_or_none()
            if h:
                d["hypothesis"] = S.hypothesis(h)
                evidence = [S.finding(f) for f in ctx.db.execute(select(ResearchFinding).where(
                    ResearchFinding.id.in_(h.evidence_ids or [0]))).scalars()]
            d["campaign"] = {"id": camp.id, "name": camp.name, "objective": camp.objective,
                             "success_criteria": camp.success_criteria}
        d["evidence"] = evidence
        d["target_connections"] = [S.connection(c) for c in conns if c.adapter in allowed
                                   and allowed[c.adapter].platform in ("any", i.platform)]
        out.append(d)
    reqs = ctx.db.execute(select(ApprovalRequest).where(ApprovalRequest.workspace_id == ctx.workspace.id,
                                                        ApprovalRequest.status == "pending")
                          .order_by(ApprovalRequest.requested_at)).scalars().all()
    ctx.db.commit()
    return {"items": out, "requests": [S.approval_request(r) for r in reqs],
            "live_publishing_enabled": get_settings().live_publishing_enabled, "mode": get_settings().app_mode.value}


@router.get("/calendar")
def calendar(start: str, end: str, campaign_id: int | None = None, platform: str = "", status: str = "",
             content_type: str = "", ctx: Ctx = Depends(get_ctx)):
    s, e = _to_utc(start, ctx), _to_utc(end, ctx)
    if e <= s or e - s > timedelta(days=62):
        raise ApiError(400, "Choose a range of up to 62 days.", "validation")
    q = select(ContentItem).where(ContentItem.workspace_id == ctx.workspace.id, or_(
        ContentItem.planned_at.between(s, e), ContentItem.scheduled_at.between(s, e),
        ContentItem.published_at.between(s, e)))
    if campaign_id:
        q = q.where(ContentItem.campaign_id == campaign_id)
    if platform:
        q = q.where(ContentItem.platform == platform)
    if status:
        q = q.where(ContentItem.status.in_(status.split(",")))
    if content_type:
        q = q.where(ContentItem.content_type == content_type)
    rows = ctx.db.execute(q).scalars().all()
    return {"timezone": ctx.tz.key, "items": [S.content(i, ctx.tz) for i in rows]}
