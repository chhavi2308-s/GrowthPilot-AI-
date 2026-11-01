"""Mockable, workspace-scoped Canva poster job processing primitives."""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageStat
from sqlalchemy import select, update

from ..agent.briefs import ClaimAllowed, CreativeBrief, to_canva_handoff, validate_brief
from ..config import get_settings
from ..creative.canva_mcp import (
    CanvaMCPError,
    _candidate_rows,
    _nested,
    connected_workspace_client,
    generate_design_args,
    reserve_daily_generation,
    safe_canva_message,
)
from ..models import (
    Asset,
    Brand,
    CanvaPosterJob,
    ContentItem,
    ContentStatus,
    Product,
    utcnow,
)
from . import assets
from .canva_choices import _canva_image_url, image_prompt, prompt_query, request_key, save_options
from .canva_reference import edit_reference

MAX_JOB_CALLS = 3


def _claim_supported(claim: ClaimAllowed, product: Product | None) -> bool:
    if product is None:
        return False
    try:
        return {
            "name": claim.value == product.name[:160] and claim.text == product.name[:160],
            "category": claim.value == product.category and claim.text == f"Category: {product.category}",
            "materials": claim.value in (product.materials or []) and claim.text == f"Material: {claim.value}",
            "dimensions": claim.value == product.dimensions and claim.text == f"Size: {product.dimensions}",
            "price": (product.price is not None and Decimal(claim.value) == Decimal(product.price)
                      and claim.text == f"Price ₹{product.price:,.0f} (caption only; recheck before posting)"),
        }[claim.field]
    except (ArithmeticError, InvalidOperation, TypeError, ValueError):
        return False


def _schema_args(client, tool: str, values: dict[str, Any]) -> dict[str, Any]:
    schema = client.schema(tool)
    props = schema.get("properties", {})
    args = {k: v for k, v in values.items() if k in props and v is not None}
    missing = sorted(set(schema.get("required", [])) - set(args))
    if missing:
        raise CanvaMCPError(f"Unmapped required {tool} fields: {', '.join(missing)}")
    return args


def _png_format(spec: dict[str, Any]) -> Any:
    if spec.get("type") == "string" or "enum" in spec:
        choices = spec.get("enum", ["png"])
        return next((value for value in choices if isinstance(value, str) and value.casefold() == "png"), None)
    if spec.get("type") == "object":
        value = {key: _png_format(child) for key, child in spec.get("properties", {}).items()
                 if key in {"type", "format", "file_type"} and isinstance(child, dict)}
        value = {key: item for key, item in value.items() if item is not None}
        if value and not (set(spec.get("required", [])) - set(value)):
            return value
    for child in spec.get("anyOf", []) + spec.get("oneOf", []):
        value = _png_format(child)
        if value is not None:
            return value
    return None


def _export_args(client, design_id: str) -> dict[str, Any]:
    props = client.schema("export-design").get("properties", {})
    values: dict[str, Any] = {"design_id": design_id, "as_single_image": True,
                              "user_intent": "Export poster PNG for review."}
    formats = {name: _png_format(props[name]) for name in ("format", "export_format", "file_type")
               if isinstance(props.get(name), dict)}
    if not any(value is not None for value in formats.values()):
        raise CanvaMCPError("Canva export schema does not advertise a supported PNG format.")
    return _schema_args(client, "export-design", {**values, **formats})


def _urls(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _urls(child)
    elif isinstance(value, list):
        for child in value:
            yield from _urls(child)
    elif isinstance(value, str) and value.startswith("https://"):
        yield value


def _select_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prefer Canva's own numeric quality/relevance score; keep stable API order on ties."""
    if not candidates:
        return None
    scored = [(index, row.get("quality_score", row.get("relevance_score", row.get("score"))))
              for index, row in enumerate(candidates)]
    numeric = [(index, score) for index, score in scored if isinstance(score, (float, int))]
    return candidates[max(numeric, key=lambda pair: pair[1])[0]] if numeric else candidates[0]


def _design_id(payload: dict[str, Any]) -> str | None:
    if isinstance(payload.get("design_id"), str):
        return payload["design_id"]
    if isinstance(payload.get("designId"), str):
        return payload["designId"]
    design = payload.get("design")
    if isinstance(design, dict) and isinstance(design.get("id"), str):
        return design["id"]
    summary = payload.get("design_summary")
    if isinstance(summary, dict) and isinstance(summary.get("id"), str):
        return summary["id"]
    for value in payload.values():
        if isinstance(value, dict) and (found := _design_id(value)):
            return found
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and (found := _design_id(item)):
                    return found
    return None


def _qa_verdict(brief: CreativeBrief | None, brand: Brand, product: Product | None, *, image_used: bool,
                image_skip_reason: str | None) -> dict[str, Any]:
    """Run deterministic text/product guards; visual checks remain pending human review."""
    if brief is None:
        return {"passed": False, "status": "REQUIRES_HUMAN_REVIEW",
                "issues": ["Review the AI-generated design against your Image brief before approving.",
                           "On-image text, brand, factual claims and visual content require human verification."],
                "checks": {"visual_review": "pending", "claims_supported_by_product_data": "not_verified"}}
    unsupported = [claim.text for claim in brief.claims_allowed if not _claim_supported(claim, product)]
    copy = " ".join([brief.headline, brief.tagline, brief.cta]).casefold()
    religious = any(word in copy for word in ("mantra", "sanskrit", "ॐ", "swastika", "deity"))
    product_prices = {str(value) for value in (getattr(product, "price", None), getattr(product, "mrp", None))
                      if value is not None}
    found_prices = [m.group(0).strip() for m in re.finditer(r"(?:₹|Rs\.?\s*)\s?\d+(?:\.\d{1,2})?", copy, re.I)]
    bad_prices = [price for price in found_prices if not any(re.sub(r"\s", "", price).casefold()
                    in re.sub(r"\s", "", allowed).casefold() for allowed in product_prices)]
    required_text_issue = "Required text, brand, and CTA are not OCR-verified in the exported image."
    issues = [required_text_issue, "Export contrast and product-photo match require human visual review."]
    if unsupported:
        issues.append("Claims need review against product facts.")
    if bad_prices:
        issues.append("Price does not exactly match catalogue data.")
    if image_skip_reason:
        issues.append(image_skip_reason)
    if religious or brief.needs_human_review:
        issues.append("Religious imagery/text or brief signal requires human review.")
    issues.append("Religious imagery, symbols, mantra, and Sanskrit screening requires human review.")
    return {"passed": False, "status": "REQUIRES_HUMAN_REVIEW", "issues": issues,
            "checks": {"required_text": "not_verified", "brand": "not_verified", "cta": "not_verified",
                       "claims_supported_by_product_data": not unsupported,
                       "price_accuracy_from_brief": not bad_prices,
                       "product_photo_policy": brief.image_policy == "text_only" or image_used or
                       brief.image_policy == "may_use_images",
                       "visual_review": "pending"},
            "religious_content": religious or brief.needs_human_review,
            "religious_visual_check": "requires_human_review"}


def _persist_poster(db, item: ContentItem, job: CanvaPosterJob, brand: Brand, png: bytes, design_id: str,
                    qa: dict[str, Any], *, image_trust: str, image_skip_reason: str | None,
                    edit_link_available: bool) -> None:
    asset = assets.save_upload(db, workspace_id=job.workspace_id, brand_id=brand.id,
        filename=f"canva-poster-{item.id}-{job.id}.png", data=png, alt_text="Canva poster, awaiting human review.")
    asset.source = "rendered"
    from . import content as content_workflow
    content_workflow.update(db, item, {"asset_id": asset.id}, user_id=None, role="editor", source="canva",
                            note="Attached exported Canva poster for human review.")
    content_workflow._clear_approval(item)
    item.status = ContentStatus.IN_REVIEW
    choice = {key: value for key, value in (item.canva_poster or {}).items()
              if key in {"request_key", "prompt", "candidates", "generation_job_id", "selected_candidate_id",
                         "reference_asset_id"}}
    item.canva_poster = {**choice, "phase": "completed", "job_id": job.id, "design_id": design_id, "asset_id": asset.id,
                         "mode": (item.canva_poster or {}).get("mode", "image-based" if image_trust == "uploaded" else "text-only"),
                         "image_trust": image_trust, "image_skip_reason": image_skip_reason, "qa": qa,
                         "edit_link_available": edit_link_available}
    job.status = "completed"
    job.error = "Poster is stored and awaiting human visual review."


def _overlay_brand_mark(png: bytes, brand_name: str, logo_bytes: bytes | None = None) -> bytes:
    """Overlay a corner logo, or a contrast-safe text wordmark, before asset hashing/storage."""
    from io import BytesIO

    with Image.open(BytesIO(png)) as source:
        poster = source.convert("RGBA")
    width, height = poster.size
    if not width or not height:
        raise CanvaMCPError("Exported poster has invalid dimensions.")
    margin = max(8, round(width * 0.025))
    max_mark_width = max(1, round(width * 0.12))
    max_mark_height = max(1, round(height * 0.12))

    logo = None
    if logo_bytes:
        try:
            with Image.open(BytesIO(logo_bytes)) as source_logo:
                logo = source_logo.convert("RGBA")
            logo.thumbnail((max_mark_width, max_mark_height), Image.Resampling.LANCZOS)
        except (OSError, ValueError):
            logo = None

    if logo is not None:
        x = width - margin - logo.width
        y = margin
        poster.alpha_composite(logo, (x, y))
    else:
        name = (brand_name or "Brand").strip()[:120] or "Brand"
        max_text_width = max(1, min(max_mark_width, width - margin * 2))
        font_size = max(10, round(width * 0.035))
        while font_size > 8:
            font = ImageFont.load_default(size=font_size)
            box = ImageDraw.Draw(poster).textbbox((0, 0), name, font=font, stroke_width=1)
            if box[2] - box[0] <= max_text_width:
                break
            font_size -= 1
        font = ImageFont.load_default(size=font_size)
        x = max(margin, width - margin - (ImageDraw.Draw(poster).textbbox((0, 0), name, font=font)[2]))
        y = margin
        # Sample the underlay to select a readable fill and contrasting outline.
        sample_box = poster.crop((x, y, min(width, x + max_text_width), min(height, y + font_size * 2)))
        brightness = sum(ImageStat.Stat(sample_box.convert("RGB")).mean) / 3
        fill, stroke = ((30, 20, 20, 255), (255, 248, 235, 255)) if brightness > 150 else (
            (255, 248, 235, 255), (40, 20, 24, 255))
        ImageDraw.Draw(poster).text((x, y), name, font=font, fill=fill, stroke_width=max(1, font_size // 12),
                                    stroke_fill=stroke)

    output = BytesIO()
    poster.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _brand_logo_bytes(db, brand: Brand, workspace_id: int) -> bytes | None:
    """Load only this workspace brand's raster logo; invalid/missing files use the wordmark fallback."""
    if not brand.logo_asset_id:
        return None
    logo = db.get(Asset, brand.logo_asset_id)
    if (logo is None or logo.workspace_id != workspace_id or logo.brand_id not in (None, brand.id)
            or logo.kind != "logo" or logo.content_type not in {"image/png", "image/jpeg"}):
        return None
    try:
        return assets.store().path(logo.storage_key).read_bytes()
    except (OSError, ValueError):
        return None


def validate_brief_for_item(db, item: ContentItem, brand: Brand) -> tuple[CreativeBrief, dict[str, Any]]:
    if not item.creative_brief:
        raise CanvaMCPError("Create and save a CreativeBrief before requesting a Canva poster.")
    try:
        brief = validate_brief(item.creative_brief, brand.name)
    except Exception as exc:
        raise CanvaMCPError(f"Stored CreativeBrief failed validation ({type(exc).__name__}).") from None
    if brief.content_id != item.id or brief.platform != item.platform:
        raise CanvaMCPError("CreativeBrief does not match this content item.")
    if brand.workspace_id != item.workspace_id:
        raise CanvaMCPError("Brand is unavailable in this workspace.")
    products = db.execute(select(Product).where(Product.id.in_(item.product_ids or [-1]),
        Product.workspace_id == item.workspace_id, Product.brand_id == brand.id)).scalars().all()
    by_sku = {product.sku: product for product in products}
    for claim in brief.claims_allowed:
        product = by_sku.get(claim.product_sku)
        if product is None:
            raise CanvaMCPError("CreativeBrief claim refers to a product outside this content item.")
        if not _claim_supported(claim, product):
            raise CanvaMCPError("CreativeBrief contains a claim that does not match verified product data.")
    return brief, to_canva_handoff(brief)


def prepare_canva_brief(db, item: ContentItem, brand: Brand) -> tuple[CreativeBrief | None, dict[str, Any]]:
    if item.workspace_id != brand.workspace_id:
        raise CanvaMCPError("Brand is unavailable in this workspace.")
    prompt = image_prompt(item)
    if prompt:
        return None, {"canva_query": prompt_query(prompt, brand)}
    if not getattr(item, "creative_brief", None):
        raise CanvaMCPError("Enter an Image brief to describe the images you want, then generate options.")
    return validate_brief_for_item(db, item, brand)


async def _upload_trusted_images(client, db, workspace_id: int, brand: Brand, brief: CreativeBrief) -> list[str]:
    ids = []
    for ref in to_canva_handoff(brief).get("image_refs", []):
        asset = db.get(Asset, ref.get("asset_id")) if ref.get("asset_id") else None
        if not asset or asset.workspace_id != workspace_id or asset.brand_id not in (None, brand.id):
            continue
        trust = ref.get("trust", "unknown")
        if asset.source not in {"catalogue", "upload"} or trust not in {"real_match", "uploaded"}:
            continue
        ids.append(await _upload_asset(client, asset, f"Upload verified product photo for {brand.name}."))
    return ids


async def _upload_asset(client, asset: Asset, user_intent: str) -> str:
    data = assets.store().path(asset.storage_key).read_bytes()
    if len(data) > assets.MAX_UPLOAD_BYTES:
        raise CanvaMCPError("Brand or product image exceeds Canva upload limit.")
    schema = client.schema("create-upload-url")
    values = {"filename": asset.filename, "name": asset.filename, "content_type": asset.content_type,
              "mime_type": asset.content_type, "file_size": len(data), "size_bytes": len(data),
              "user_intent": user_intent[:255]}
    args = {k: v for k, v in values.items() if k in schema.get("properties", {})}
    if set(schema.get("required", [])) - set(args):
        raise CanvaMCPError("Canva upload schema has required fields GrowthPilot cannot safely map.")
    result = await client.call("create-upload-url", args)
    upload_url = _nested(result, "upload_url", "uploadUrl")
    media_id = _nested(result, "asset_id", "media_id", "assetId", "mediaId")
    method = result.get("httpMethod", "POST")
    if not isinstance(upload_url, str) or not _canva_image_url(upload_url) or method not in {"POST", "PUT"}:
        raise CanvaMCPError("Canva upload response omitted a supported single-use upload URL or method.")
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as http:
            response = await http.request(method, upload_url, content=data,
                headers={"Content-Type": "application/octet-stream" if method == "POST" else asset.content_type})
    except httpx.HTTPError:
        raise CanvaMCPError("Canva reference image upload failed; no upload URL was reused.") from None
    if response.status_code >= 300:
        raise CanvaMCPError(f"Canva image upload failed with HTTP {response.status_code}.")
    if not media_id:
        try:
            media_id = _nested(response.json(), "mediaId", "media_id", "assetId", "asset_id")
        except ValueError:
            media_id = None
    if not isinstance(media_id, str) or not media_id:
        raise CanvaMCPError("Canva upload completed without a media id.")
    return media_id


async def _upload_brand_logo(client, db, workspace_id: int, brand: Brand) -> str | None:
    if not brand.logo_asset_id or "create-upload-url" not in client.tools:
        return None
    asset = db.get(Asset, brand.logo_asset_id)
    if (asset is None or asset.workspace_id != workspace_id or asset.brand_id not in (None, brand.id)
            or asset.kind != "logo" or asset.content_type not in {"image/png", "image/jpeg"}):
        return None
    return await _upload_asset(client, asset, f"Upload the official {brand.name} brand logo for poster identity.")


def _brand_prompt(query: str, brand: Brand, logo_attached: bool, brief: CreativeBrief,
                  image_skip_reason: str | None = None) -> str:
    if image_skip_reason:
        sentences = re.split(r"(?<=[.!?])\s+", query.strip())
        query = " ".join(sentence for sentence in sentences
                         if not re.search(r"\b(supplied|attached)\s+(?:product\s+)?photo\b", sentence, re.I))
        query += " Text-led layout only; do not depict or redraw a specific product."
    palette = ", ".join(str(color)[:24] for color in (brand.colors or [])[:5]) or "use the brand profile palette"
    tone = (brand.tone or brand.voice or "clear and consistent with the brand profile")[:120]
    identity = (f"Brand identity: show the name \"{brand.name}\" exactly. Tone: {tone}. Palette: {palette}. "
                + ("Use the attached official brand logo without changing it. " if logo_attached
                   else f"No logo file is available; render \"{brand.name}\" as a text wordmark. "))
    allowed = "; ".join(claim.text for claim in brief.claims_allowed)[:500]
    guardrails = "Use no claims beyond the validated brief and product records. Do not invent prices, discounts, "
    guardrails += "materials, origin, health, or quality claims."
    if allowed:
        guardrails += f" Approved product facts: {allowed}."
    return f"{identity}{query.strip()}\n{guardrails}"[:3500]


async def rest_export(db, workspace_id: int, design_id: str, *, client_factory=httpx.AsyncClient,
                      sleep=asyncio.sleep) -> bytes:
    """Development-only token fallback; isolated behind a mockable REST interface."""
    from .canva_rest_dev import export_png
    return await export_png(design_id, client_factory=client_factory, sleep=sleep)


async def process_job(db, job: CanvaPosterJob, *, rest_exporter=rest_export) -> None:
    item = db.get(ContentItem, job.content_id)
    if item is None or item.workspace_id != job.workspace_id:
        raise CanvaMCPError("Content item is unavailable in this workspace.")
    if item.status in {ContentStatus.PUBLISHED, ContentStatus.PUBLISHING, ContentStatus.CANCELLED}:
        raise CanvaMCPError("A poster cannot be attached to content that is already final.")
    brand = db.get(Brand, item.brand_id)
    if brand is None or brand.workspace_id != job.workspace_id:
        raise CanvaMCPError("Brand is unavailable in this workspace.")
    brief, handoff = prepare_canva_brief(db, item, brand)
    product = None
    if item.product_ids:
        product = db.execute(select(Product).where(Product.id.in_(item.product_ids),
            Product.workspace_id == job.workspace_id, Product.brand_id == brand.id)).scalars().first()
    saved = item.canva_poster if isinstance(item.canva_poster, dict) else {}
    snapshot = request_key(item) if saved.get("request_key") else None
    if snapshot and saved["request_key"] != snapshot:
        raise CanvaMCPError("The Image brief changed. Generate new options before choosing a design.")
    selected = saved.get("selected_candidate_id")
    design_id = saved.get("design_id") if saved.get("job_id") == job.id else None
    design_id = design_id if isinstance(design_id, str) and design_id else None
    image_trust = saved.get("image_trust", "none")
    image_skip_reason = saved.get("image_skip_reason")
    edit_link_available = bool(saved.get("edit_link_available"))
    if not design_id and not selected and job.calls_used >= MAX_JOB_CALLS:
        raise CanvaMCPError("Per-job generation call cap reached.")
    settings = get_settings()
    async with connected_workspace_client(db, job.workspace_id, settings) as (client, _credential):
        if not design_id and not selected:
            if not {"generate-design", "create-design-from-candidate"}.issubset(client.tools):
                raise CanvaMCPError("Authenticated Canva MCP is missing required allow-listed tools.")
            cap = getattr(settings, "canva_mcp_daily_cap", 10)
            generate_schema = client.schema("generate-design")
            can_attach_assets = "asset_ids" in generate_schema.get("properties", {})
            if brief and brief.image_policy == "must_use_images" and not can_attach_assets:
                raise CanvaMCPError("Canva generate-design schema cannot accept the required verified image asset.")
            uploaded_ids = await _upload_trusted_images(client, db, job.workspace_id, brand, brief) \
                if brief and brief.image_policy != "text_only" and can_attach_assets and "create-upload-url" in client.tools else []
            reference_id = saved.get("reference_asset_id")
            if reference_id:
                reference = db.get(Asset, reference_id)
                if (not reference or reference.workspace_id != job.workspace_id or
                        reference.brand_id not in (None, brand.id) or reference.content_type not in {"image/png", "image/jpeg"}):
                    raise CanvaMCPError("The reference image is unavailable in this workspace.")
                if not can_attach_assets or not {"create-upload-url", "generate-image", "get-generate-image-job"}.issubset(client.tools):
                    raise CanvaMCPError("This Canva connection cannot use reference images; the reference was not ignored.")
                edited_media = saved.get("edited_media_id")
                if not edited_media:
                    reference_media = saved.get("reference_media_id") or await _upload_asset(
                        client, reference, "Upload the user-selected reference image for a private AI variation.")
                    item.canva_poster = {**item.canva_poster, "reference_media_id": reference_media}
                    db.commit()
                    job.calls_used += 1
                    reserve_daily_generation(db, limit=cap, now=utcnow(), operation="generate-image")
                    db.commit()
                    edited_media = await edit_reference(client, reference_media, image_prompt(item))
                    item.canva_poster = {**item.canva_poster, "edited_media_id": edited_media}
                    db.commit()
                uploaded_ids = [edited_media]
            logo_id = await _upload_brand_logo(client, db, job.workspace_id, brand) if brief and can_attach_assets else None
            image_trust = "reference" if reference_id else "uploaded" if uploaded_ids else "none"
            if brief and brief.image_policy == "must_use_images" and not uploaded_ids:
                raise CanvaMCPError("Required image policy has no verified real-match or uploaded image available.")
            image_skip_reason = None
            if brief and brief.image_policy != "text_only" and not uploaded_ids:
                image_skip_reason = ("The generate-design schema cannot attach product images." if not can_attach_assets
                                     else "No verified product image was attachable; poster was generated text-only.")
            q = handoff.get("canva_query")
            if not q:
                raise CanvaMCPError("CreativeBrief has no Canva query.")
            query = _brand_prompt(q, brand, logo_id is not None, brief, image_skip_reason) if brief else q
            if reference_id:
                query += " Use the supplied edited reference image as the main visual; preserve its identity when creating the layout options."
            max_query = generate_schema.get("properties", {}).get("query", {}).get("maxLength")
            if isinstance(max_query, int) and len(query) > max_query:
                raise CanvaMCPError("This Image brief exceeds Canva's prompt limit. Shorten it and try again.")
            all_asset_ids = [*uploaded_ids, *([logo_id] if logo_id else [])]
            args = generate_design_args(client.tools["generate-design"], query=query,
                user_intent="Generate different private design options for the user to choose from.",
                asset_ids=all_asset_ids or None)
            if job.calls_used >= MAX_JOB_CALLS:
                raise CanvaMCPError("This batch reached its retry limit. Generate new options to start a fresh batch.")
            job.calls_used += 1
            reserve_daily_generation(db, limit=cap, now=utcnow())
            db.commit()
            generated = await client.call("generate-design", args, reservation_made=True)
            db.flush()
            generated = await client.poll_design_job(generated)
            candidates = _candidate_rows(generated)
            if not candidates:
                raise CanvaMCPError("Canva returned no design candidates.")
            generation_id = _nested(generated, "job_id", "jobId")
            if not generation_id and isinstance(generated.get("job"), dict):
                generation_id = generated["job"].get("id")
            if not isinstance(generation_id, str) or not generation_id:
                raise CanvaMCPError("Canva omitted the generation job id required to select a design.")
            options = await save_options(db, job, brand, candidates)
            if snapshot:
                db.refresh(item)
                if request_key(item) != snapshot:
                    raise CanvaMCPError("The Image brief changed while generating. Generate new options.")
            item.canva_poster = {"job_id": job.id, "phase": "awaiting_selection",
                "request_key": snapshot or request_key(item), "prompt": image_prompt(item),
                "generation_job_id": generation_id, "candidates": options, "selected_candidate_id": None,
                "mode": "image-to-image" if reference_id else "prompt-led" if brief is None else "image-based" if uploaded_ids else "text-only",
                "reference_asset_id": reference_id,
                "image_trust": image_trust, "image_skip_reason": image_skip_reason}
            job.status, job.error = "awaiting_selection", ""
            db.commit()
            return
        if not design_id:
            if not selected or selected not in {row.get("id") for row in saved.get("candidates", [])}:
                raise CanvaMCPError("Choose one of the generated options before creating the design.")
            committed = await client.call("create-design-from-candidate", _schema_args(client,
                "create-design-from-candidate", {"candidate_id": selected,
                "job_id": saved.get("generation_job_id"),
                "user_intent": "Create the exact design option selected by the user for review."}))
            design_id = _design_id(committed)
            if not isinstance(design_id, str):
                raise CanvaMCPError("Canva commit returned no design id.")
            edit_link_available = bool(_nested(committed, "edit_url", "editUrl"))
            item.canva_poster = {**saved, "phase": "exporting", "job_id": job.id, "design_id": design_id, "asset_id": None,
                                 "mode": saved.get("mode", "text-only"),
                                 "image_trust": image_trust, "image_skip_reason": image_skip_reason,
                                 "edit_link_available": edit_link_available}
            db.commit()
        job.status = "exporting"
        db.commit()
        png = None
        export_error = None
        if "export-design" in client.tools:
            try:
                exported = await client.call("export-design", _export_args(client, design_id))
                for url in _urls(exported):
                    if urlparse(url).scheme != "https":
                        continue
                    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as downloader:
                        try:
                            r = await downloader.get(url)
                        except httpx.HTTPError:
                            continue
                    if r.status_code == 200 and r.content.startswith(b"\x89PNG\r\n\x1a\n"):
                        png = r.content
                        break
            except CanvaMCPError as exc:
                # REST export is the only fallback when MCP export is absent or unusable.
                export_error = safe_canva_message(str(exc))
                png = None
        if png is None:
            try:
                png = await rest_exporter(db, job.workspace_id, design_id)
            except CanvaMCPError:
                if export_error:
                    raise CanvaMCPError(f"Canva MCP export failed: {export_error}; REST fallback unavailable.") from None
                raise
    if len(png) > assets.MAX_UPLOAD_BYTES:
        raise CanvaMCPError("Exported poster exceeds the supported 8 MB asset limit.")
    png = _overlay_brand_mark(png, brand.name, _brand_logo_bytes(db, brand, job.workspace_id))
    if len(png) > assets.MAX_UPLOAD_BYTES:
        raise CanvaMCPError("Branded poster exceeds the supported 8 MB asset limit.")
    qa = _qa_verdict(brief, brand, product, image_used=image_trust == "uploaded",
                     image_skip_reason=image_skip_reason)
    if snapshot:
        db.refresh(item)
        if (request_key(item) != snapshot or item.status in
                {ContentStatus.PUBLISHED, ContentStatus.PUBLISHING, ContentStatus.CANCELLED}):
            raise CanvaMCPError("The post changed while exporting. Refresh it before generating a new design.")
    job.status = "qa"
    db.commit()
    _persist_poster(db, item, job, brand, png, design_id, qa,
                    image_trust=image_trust, image_skip_reason=image_skip_reason,
                    edit_link_available=edit_link_available)


def process_queued(db, *, limit=1):
    jobs = db.execute(select(CanvaPosterJob.id).where(CanvaPosterJob.status == "queued")
                      .order_by(CanvaPosterJob.created_at).limit(limit)).scalars().all()
    processed = 0
    for job_id in jobs:
        claimed = db.execute(update(CanvaPosterJob).where(CanvaPosterJob.id == job_id,
            CanvaPosterJob.status == "queued").values(status="generating"))
        if claimed.rowcount != 1:
            continue
        db.commit()
        job = db.get(CanvaPosterJob, job_id)
        try:
            asyncio.run(process_job(db, job))
            db.commit()
        except Exception as exc:
            db.rollback()
            job = db.get(CanvaPosterJob, job.id)
            if job:
                job.status = "failed"
                job.error = safe_canva_message(str(exc))[:500] or type(exc).__name__
                item = db.get(ContentItem, job.content_id)
                poster = getattr(item, "canva_poster", None) if item else None
                if isinstance(poster, dict) and poster.get("job_id") == job.id:
                    item.canva_poster = {**poster, "error": job.error}
                db.commit()
        processed += 1
    return processed
