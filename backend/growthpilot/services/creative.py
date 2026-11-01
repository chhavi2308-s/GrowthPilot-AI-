"""Creative Studio service: render real image files for content items and record their provenance.

Inputs come only from (a) the item's validated text, (b) catalogue facts for the referenced products, and (c)
brand elements. On-image text is re-checked with the same claim validator as captions. Product photos are used
only when the catalogue has a real photo of that product; otherwise a typographic product card is drawn.
"""

from __future__ import annotations

import hashlib
import logging
import re

from sqlalchemy.orm import Session

from ..catalogue import usable_for_brand, usable_products
from ..config import get_settings
from ..creative import imagegen
from ..creative.render import CreativeInput, Palette, render
from ..models import Asset, Brand, ContentItem, Product
from ..platforms import SPECS
from .assets import store
from .errors import WorkflowError
from .validation import check_product_claims

log = logging.getLogger(__name__)

PILLAR_TEMPLATES = [
    (("festive", "festival offer", "deepavali", "diwali", "navaratri", "occasion"), "festive"),
    (("how-to", "how to", "guide", "vidhi", "education", "tips", "care", "checklist", "explainer"), "educational"),
    (("launch", "new arrival", "new"), "launch"),
    (("offer", "promo", "sale"), "promo_poster"),
    (("story", "awareness", "heritage", "brand", "community"), "awareness"),
]


def template_for(item: ContentItem) -> str:
    from ..agent.generation import pillar_kind

    kind = pillar_kind(item.pillar)
    if kind in ("guide", "know_how"):
        return "festive" if kind == "guide" and re.search(r"deepavali|diwali|navaratri|festival", item.hook or "", re.I) \
            and item.content_type != "carousel" else "educational"
    if kind == "offer":
        return "festive"
    key = f"{item.pillar} {item.title}".lower()
    for words, tpl in PILLAR_TEMPLATES:
        if any(w in key for w in words):
            return tpl
    return "product_showcase"


def inr(v) -> str:
    from ..agent.generation import inr as _inr

    return f"₹{_inr(float(v))}"


def _photo_bytes(db: Session, p: Product) -> tuple[bytes, Asset] | None:
    if not p.image_asset_id:
        return None
    a = db.get(Asset, p.image_asset_id)
    if a is None or a.source not in ("catalogue", "upload") or a.content_type not in ("image/jpeg", "image/png"):
        return None  # never a stock image, placeholder or rendered asset as "the product"
    try:
        return store().path(a.storage_key).read_bytes(), a
    except OSError:
        return None


def _logo(db: Session, brand: Brand) -> bytes | None:
    if not brand.logo_asset_id:
        return None
    a = db.get(Asset, brand.logo_asset_id)
    if a and a.content_type in ("image/png", "image/jpeg"):
        try:
            return store().path(a.storage_key).read_bytes()
        except OSError:
            return None
    return None


def _price_ok(p: Product) -> bool:
    codes = {q.get("code") for q in p.data_quality or []}
    return p.price is not None and "price_suspicious" not in codes and "possible_test_listing" not in codes


def _clean(text: str, limit: int) -> str:
    """Drop hashtags and shorten at a word boundary (never mid-word); "…" marks a shortened line."""
    text = re.sub(r"#\w+", "", text or "").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= limit:
        return text.rstrip(" ,;:-")
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{cut}…"


def render_for_item(db: Session, item: ContentItem, *, platform: str | None = None, template: str | None = None,
                    variation: int = 0, ai_background: bool = False, show_price: bool | None = None,
                    slide: tuple[int, int] | None = None, headline: str | None = None,
                    body: list[str] | None = None, background: tuple[bytes, Asset] | None = None,
                    size: tuple[int, int] | None = None, background_strength: float = 0.4) -> Asset:
    """``background``: an already generated AI background and its asset (no new image request is made);
    ``size``: render at this size instead of the platform format (e.g. the 2K size of that background)."""
    brand = db.get(Brand, item.brand_id)
    sp = SPECS.get(platform or item.platform) or SPECS["instagram"]
    fmt = sp.render
    width, height = size or (fmt.width, fmt.height)
    prods = [p for p in (db.get(Product, pid) for pid in item.product_ids or []) if usable_for_brand(brand, p)]
    p = prods[0] if prods else None
    tpl = template or ("carousel_slide" if slide else template_for(item))
    photo = _photo_bytes(db, p) if p else None
    if show_price is None:
        show_price = tpl in ("product_showcase", "promo_poster", "launch", "festive")
    price = inr(p.price) if (p and show_price and _price_ok(p)) else ""
    head = _clean(headline or item.hook or item.title, 90)
    sub = ""
    if p and tpl not in ("educational", "carousel_slide", "awareness"):
        sub = p.name if p.name.lower() not in head.lower() else (p.category or "")
    lines = [_clean(x, 110) for x in (body or []) if x.strip()]
    if not lines and tpl == "educational" and item.carousel_outline:
        lines = [_clean(x, 110) for x in item.carousel_outline[1:4]]
    badge = ""
    occ = re.search(r"\b(Deepavali|Diwali|Navaratri|Dussehra|Janmashtami|Ganesh Chaturthi|Karthika|Tulsi Vivah|"
                    r"Sankranti|Ugadi|Varamahalakshmi)\b", f"{item.title} {item.hook} {item.caption}", re.I)
    if occ and tpl in ("festive", "educational", "promo_poster"):
        badge = occ.group(1).title()
    cta = _clean(item.cta, 34) if item.cta and len(item.cta) <= 34 else ""

    on_image = "\n".join(x for x in [head, sub, *lines, price, cta] if x)
    catalogue = usable_products(db, brand)
    claim_checks = [c.__dict__ for c in check_product_claims(on_image, brand, prods, catalogue)]
    claim_fail = [c for c in claim_checks if c["status"] == "fail"]
    if claim_fail:
        raise WorkflowError("On-image text fails product checks: " + "; ".join(c["message"] for c in claim_fail[:3]))

    bg_bytes, bg_asset = background if background else (None, None)
    if ai_background and not background:
        try:
            bg_bytes, ctype, info = imagegen.generate_background(f"{item.pillar} {badge}".strip() or brand.name,
                                                                 brand.colors or [], fmt.width, fmt.height)
            key = store().save(bg_bytes, ".jpg" if ctype == "image/jpeg" else ".png")
            bg_asset = Asset(workspace_id=item.workspace_id, brand_id=brand.id, kind="background",
                             source="ai_background", filename=f"ai-background-{item.id}.jpg", content_type=ctype,
                             size_bytes=len(bg_bytes), width=fmt.width, height=fmt.height, storage_key=key,
                             sha256=hashlib.sha256(bg_bytes).hexdigest(), alt_text="AI-generated abstract background",
                             meta=info, campaign_id=item.campaign_id)
            db.add(bg_asset)
            db.flush()
        except imagegen.ImageGenError as exc:
            log.info("AI background skipped: %s", exc)
            bg_bytes = None

    inp = CreativeInput(template=tpl, width=width, height=height, headline=head, subline=sub, body=lines,
                        price_text=price, cta=cta, badge=badge, product_name=p.name if p else "",
                        product_category=p.category if p else "", brand_name=brand.name,
                        website=(brand.website or "").replace("https://", "").replace("http://", "").strip("/"),
                        palette=Palette.from_colors(brand.colors), photo=photo[0] if photo else None,
                        background=bg_bytes, background_strength=background_strength, logo=_logo(db, brand),
                        variation=variation, slide=slide, fmt="JPEG")
    res = render(inp)
    key = store().save(res.data, ".jpg")
    meta = {
        "template": tpl, "platform": sp.key, "format": {"width": width, "height": height, "label": fmt.label},
        "variation": variation, "layers": [{"kind": ly.kind, "text": ly.text, "box": ly.box, "size": ly.size,
                                            "color": ly.color, "contrast": ly.contrast} for ly in res.layers],
        "checks": res.checks, "notes": res.notes, "claim_checks": claim_checks,
        "product_ids": [x.id for x in prods], "photo_asset_id": photo[1].id if photo else None,
        "photo_kind": "product_photo" if photo else ("none" if not p else p.image_kind),
        "background": "ai_generated" if bg_asset else "template",
        "background_asset_id": bg_asset.id if bg_asset else None,
        "text_sources": {"headline": "content hook (validated)", "subline": "catalogue product name/category",
                         "price": "catalogue price" if price else None, "cta": "content CTA (validated)"},
        "content_id": item.id, "content_version": item.version, "slide": list(slide) if slide else None,
        "ok": res.ok, "synthetic_brand": brand.is_synthetic,
    }
    a = Asset(workspace_id=item.workspace_id, brand_id=brand.id, kind="image", source="rendered",
              filename=f"{sp.key}-{tpl}-{item.id}-v{item.version}{'-s' + str(slide[0]) if slide else ''}.jpg",
              content_type=res.content_type, size_bytes=len(res.data), width=res.width, height=res.height,
              storage_key=key, sha256=hashlib.sha256(res.data).hexdigest(),
              alt_text=f"{head}. {sub}".strip(". ")[:900], meta=meta, campaign_id=item.campaign_id,
              is_synthetic=get_settings().is_demo and brand.is_synthetic)
    db.add(a)
    db.flush()
    return a


def creative_ok(a: Asset) -> tuple[bool, list[dict]]:
    checks = list((a.meta or {}).get("checks", [])) + [c for c in (a.meta or {}).get("claim_checks", [])
                                                       if c.get("status") != "pass"]
    return not any(c.get("status") == "fail" for c in checks), checks
