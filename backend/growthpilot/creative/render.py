"""Deterministic poster renderer (Pillow). Zero API keys needed.

Every poster is composed from:
  * brand elements (colours, fonts, wordmark) from the brand profile;
  * text layers that come verbatim from validated content and catalogue facts (never invented here);
  * a real product photo when one exists — otherwise a typographic product card (generic stock images are
    never shown as the product);
  * optionally a background from an image provider, labelled "AI-generated background" on the image itself.

``render`` returns the encoded image plus a manifest: the layers drawn, their boxes, and checks for safe margins,
contrast (WCAG), text fit, glyph coverage and output dimensions.
"""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

FONT_DIR = Path(__file__).parent / "fonts"
TEMPLATES = ("product_showcase", "festive", "educational", "launch", "promo_poster", "awareness", "carousel_slide")
SAFE_MARGIN = 0.06  # of the shorter side


@dataclass
class Palette:
    primary: str = "#6B1420"  # deep maroon
    accent: str = "#C99A3D"  # gold
    light: str = "#FBF3E7"  # cream
    ink: str = "#2B1810"
    warm: str = "#D9722A"  # saffron

    @classmethod
    def from_colors(cls, colors: list[str] | None) -> Palette:
        """Assign brand colours to roles by lightness, not list order, so any brand palette stays readable."""
        c = [x for x in (colors or []) if re.match(r"^#[0-9A-Fa-f]{6}$", x)]
        p = cls()
        if not c:
            return p
        lum = {x: luminance(rgb(x)) for x in c}
        light = max(c, key=lum.get)
        p.light = light if lum[light] >= 0.6 else "#FBF3E7"
        dark = [x for x in c if contrast_ratio(rgb(x), rgb(p.light)) >= 4.5]
        p.ink = min(dark, key=lum.get) if dark else "#1F1A17"
        p.primary = dark[0] if dark else p.ink  # the brand's first readable dark colour
        rest = [x for x in c if x not in (p.light, p.primary, p.ink)]
        if rest:
            p.accent = rest[0]
            p.warm = rest[1] if len(rest) > 1 else rest[0]
        else:
            p.accent = p.warm = p.primary
        return p


@dataclass
class CreativeInput:
    template: str
    width: int
    height: int
    headline: str
    subline: str = ""
    body: list[str] = field(default_factory=list)
    price_text: str = ""  # already verified against the catalogue by the caller
    cta: str = ""
    badge: str = ""  # e.g. "Deepavali" — occasion label
    product_name: str = ""
    product_category: str = ""
    brand_name: str = ""
    website: str = ""
    palette: Palette = field(default_factory=Palette)
    photo: bytes | None = None  # real product photo (the caller guarantees provenance)
    background: bytes | None = None  # AI-generated background (labelled on the image)
    background_strength: float = 0.4  # how much of the AI background shows through the brand colour (0–1)
    logo: bytes | None = None
    variation: int = 0
    slide: tuple[int, int] | None = None  # (index, total) for carousel slides
    fmt: str = "JPEG"


@dataclass
class Layer:
    kind: str
    text: str
    box: tuple[int, int, int, int]
    font: str
    size: int
    color: str
    contrast: float | None = None
    min_contrast: float = 4.5


@dataclass
class RenderResult:
    data: bytes
    content_type: str
    width: int
    height: int
    layers: list[Layer]
    checks: list[dict]
    notes: list[str]

    @property
    def ok(self) -> bool:
        return not any(c["status"] == "fail" for c in self.checks)


# ── fonts & colour helpers ───────────────────────────────────────────────

_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    key = (name, size)
    if key not in _FONT_CACHE:
        if name.startswith("display"):
            f = ImageFont.truetype(str(FONT_DIR / "CormorantGaramond-Variable.ttf"), size)
            f.set_variation_by_name("Bold" if name == "display-bold" else "SemiBold")
        else:
            weight = {"ui-bold": "Bold", "ui-semibold": "SemiBold", "ui-medium": "Medium"}.get(name, "Regular")
            f = ImageFont.truetype(str(FONT_DIR / f"Poppins-{weight}.ttf"), size)
        _FONT_CACHE[key] = f
    return _FONT_CACHE[key]


def rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def luminance(c: tuple[int, int, int]) -> float:
    def ch(v: float) -> float:
        v = v / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = c[:3]
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _glyph(f: ImageFont.FreeTypeFont, ch: str) -> bytes:
    img = Image.new("L", (f.size * 2, f.size * 2), 0)
    ImageDraw.Draw(img).text((0, 0), ch, font=f, fill=255)
    return img.tobytes()


def missing_glyphs(f: ImageFont.FreeTypeFont, text: str) -> list[str]:
    """Characters the font can't draw (rendered as the .notdef box). Pillow here has no complex-script shaping."""
    notdef = _glyph(f, "\uffff")
    return sorted({ch for ch in text if not ch.isspace() and ord(ch) >= 128 and _glyph(f, ch) == notdef})


def wrap(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=f) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_text(draw, text: str, name: str, width: int, max_lines: int, start: int, minimum: int):
    """Largest size (start → minimum) at which ``text`` fits in ``max_lines`` lines. Returns (font, lines, fits)."""
    size = start
    while size >= minimum:
        f = font(name, size)
        lines = wrap(draw, text, f, width)
        if len(lines) <= max_lines and all(draw.textlength(ln, font=f) <= width for ln in lines):
            return f, lines, True
        size -= 2
    f = font(name, minimum)
    return f, wrap(draw, text, f, width), False


# ── motifs ───────────────────────────────────────────────────────────────


def draw_diya(d: ImageDraw.ImageDraw, cx: int, cy: int, s: int, bowl: str, flame: str, glow: str) -> None:
    """A simple oil lamp: bowl, wick and flame (vector-drawn, so nothing is borrowed)."""
    d.ellipse([cx - s * 0.7, cy - s * 1.45, cx + s * 0.7, cy + s * 0.05], fill=glow)
    d.chord([cx - s, cy - s * 0.6, cx + s, cy + s * 0.9], 0, 180, fill=bowl)
    d.polygon([(cx - s * 0.18, cy - s * 0.05), (cx, cy - s * 1.1), (cx + s * 0.18, cy - s * 0.05)], fill=flame)
    d.ellipse([cx - s * 0.2, cy - s * 0.55, cx + s * 0.2, cy - s * 0.02], fill=flame)


def draw_border(d: ImageDraw.ImageDraw, w: int, h: int, color: str, inset: int, dots: bool = True) -> None:
    d.rectangle([inset, inset, w - inset, h - inset], outline=color, width=max(2, w // 360))
    d.rectangle([inset + 12, inset + 12, w - inset - 12, h - inset - 12], outline=color, width=1)
    if dots:
        step = max(28, w // 30)
        r = max(2, w // 400)
        for x in range(inset + step, w - inset, step):
            for y in (inset - step // 3, h - inset + step // 3):
                if 0 < y < h:
                    d.ellipse([x - r, y - r, x + r, y + r], fill=color)


def draw_rangoli(d: ImageDraw.ImageDraw, cx: int, cy: int, radius: int, color: str, petals: int = 12) -> None:
    for ring in range(3):
        r = radius * (1 - ring * 0.28)
        for i in range(petals):
            a = 2 * math.pi * i / petals + ring * 0.2
            x, y = cx + r * math.cos(a), cy + r * math.sin(a)
            pr = max(3, radius // (9 + ring * 3))
            d.ellipse([x - pr, y - pr, x + pr, y + pr], outline=color, width=2)
    d.ellipse([cx - radius * 0.12, cy - radius * 0.12, cx + radius * 0.12, cy + radius * 0.12], outline=color, width=3)


# ── main entry ───────────────────────────────────────────────────────────


SCHEMES = {
    # (background, headline, body, accent, panel)
    0: ("light", "primary", "ink", "accent", "primary"),
    1: ("primary", "light", "light", "accent", "light"),
    2: ("light", "ink", "ink", "warm", "primary"),
}


def readable(fill: str, preferred: str, candidates: tuple[str, ...], minimum: float = 4.5) -> str:
    """``preferred`` if it meets ``minimum`` contrast on ``fill``, otherwise the best of ``candidates``."""
    if contrast_ratio(rgb(preferred), rgb(fill)) >= minimum:
        return preferred
    return max((preferred, *candidates), key=lambda c: contrast_ratio(rgb(c), rgb(fill)))


def _scheme(inp: CreativeInput) -> dict[str, str]:
    idx = inp.variation % 3
    if inp.template in ("festive", "promo_poster") and idx == 0:
        idx = 1  # festive work defaults to the dark scheme with gold
    bg, head, body, accent, panel = SCHEMES[idx]
    p = inp.palette
    col = {"bg": getattr(p, bg), "head": getattr(p, head), "body": getattr(p, body), "accent": getattr(p, accent),
           "panel": getattr(p, panel), "light": p.light, "primary": p.primary, "ink": p.ink, "gold": p.accent,
           "warm": p.warm}
    both = (p.ink, p.light, "#000000", "#FFFFFF")
    col["head"] = readable(col["bg"], col["head"], both, 3.0)
    col["body"] = readable(col["bg"], col["body"], both, 4.5)
    if contrast_ratio(rgb(col["panel"]), rgb(col["bg"])) < 1.5:  # the card must stand out from the page
        col["panel"] = p.primary if col["bg"] != p.primary else p.ink
    col["on_panel"] = readable(col["panel"], col["bg"], both, 4.5)
    col["on_accent"] = readable(col["accent"], p.ink, both, 4.5)
    col["on_gold"] = readable(col["gold"], p.ink, both, 4.5)
    return col


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    return ImageOps.fit(img.convert("RGB"), (w, h), Image.Resampling.LANCZOS)


def render(inp: CreativeInput) -> RenderResult:
    """Layout: a text block at the top, a bottom block (product photo or card, price, CTA bar), and a decorative
    motif drawn only in the free band between them, so decoration never sits under text."""
    if inp.template not in TEMPLATES:
        raise ValueError(f"Unknown template {inp.template!r}")
    W, H = inp.width, inp.height
    col = _scheme(inp)
    notes: list[str] = []
    m = int(min(W, H) * SAFE_MARGIN)
    unit = min(W, H) / 1080
    portrait = H / W >= 1.2
    landscape = W / H >= 1.4

    base = Image.new("RGB", (W, H), rgb(col["bg"]))
    if inp.background:
        bg = _cover(Image.open(io.BytesIO(inp.background)), W, H).filter(ImageFilter.GaussianBlur(1))
        base = Image.blend(bg, Image.new("RGB", (W, H), rgb(col["bg"])), 1 - max(0.0, min(1.0, inp.background_strength)))
        notes.append("AI-generated background (labelled on the image).")
    d = ImageDraw.Draw(base)
    draw_border(d, W, H, _mix(col["bg"], col["accent"], 0.7), inset=int(m * 0.45))

    layers: list[Layer] = []
    checks: list[dict] = []
    photo = Image.open(io.BytesIO(inp.photo)) if inp.photo else None
    side_photo = photo is not None and landscape
    text_w = W - 2 * m - (int(W * 0.42) + m if side_photo else 0)

    # ── 1. measure the top text block on a scratch canvas ──
    def top_block(draw: ImageDraw.ImageDraw, record: bool) -> int:
        y = m + int(14 * unit)
        if inp.slide:
            y = max(y, m)
        if inp.badge:
            f = font("ui-semibold", int(30 * unit))
            label = inp.badge.upper()
            tw = draw.textlength(label, font=f)
            box = (m, y, int(m + tw + 40 * unit), int(y + 56 * unit))
            draw.rounded_rectangle(box, radius=int(28 * unit), fill=col["accent"])
            draw.text((m + 20 * unit, y + 10 * unit), label, font=f, fill=col["on_accent"])
            if record:
                layers.append(Layer("badge", label, box, "ui-semibold", f.size, col["on_accent"]))
            y += int(78 * unit)
        if inp.slide:
            tag = f"{inp.slide[0]}/{inp.slide[1]}"
            f = font("ui-semibold", int(30 * unit))
            tw = draw.textlength(tag, font=f)
            box = (int(W - m - tw - 36 * unit), m, W - m, int(m + 54 * unit))
            draw.rounded_rectangle(box, radius=int(27 * unit), fill=col["panel"])
            draw.text((W - m - tw - 18 * unit, m + 9 * unit), tag, font=f, fill=col["on_panel"])
            if record:
                layers.append(Layer("slide_counter", tag, box, "ui-semibold", f.size, col["on_panel"], min_contrast=3.0))
            if not inp.badge:
                y += int(70 * unit)
        y = _text(draw, record, "headline", inp.headline, "display-bold", 96 if inp.template != "carousel_slide" else 84,
                  46, 3 if portrait else 2, col["head"], m, y, text_w, unit, layers, checks, min_contrast=3.0,
                  spacing=1.02)
        if inp.subline:
            y = _text(draw, record, "subline", inp.subline, "ui-medium", 40, 26, 2, col["body"], m, y + int(14 * unit),
                      text_w, unit, layers, checks)
        if inp.body:
            y += int(12 * unit)
            for line in inp.body[:5]:
                y = _text(draw, record, "body", f"• {line}", "ui-regular", 34, 24, 2, col["body"], m, y, text_w, unit,
                          layers, checks) + int(6 * unit)
        return y

    scratch = Image.new("RGB", (W, H))
    text_bottom = top_block(ImageDraw.Draw(scratch), record=False)

    # ── 2. bottom block geometry ──
    bar_h = int(84 * unit) if (inp.cta or inp.brand_name) else 0
    bar_top = H - m - bar_h
    card = None  # (box, kind)
    if photo is not None:
        if side_photo:
            card = ((W - m - int(W * 0.42), m, W - m, bar_top - int(24 * unit)), "photo")
        else:
            ph_h = int(H * (0.40 if portrait else 0.34))
            top = max(text_bottom + int(30 * unit), bar_top - int(28 * unit) - ph_h)
            card = ((m, top, W - m, bar_top - int(28 * unit)), "photo")
    elif inp.template in ("product_showcase", "launch", "promo_poster") and inp.product_name:
        card_h = int(H * (0.27 if portrait else 0.30))
        top = max(text_bottom + int(30 * unit), bar_top - int(28 * unit) - card_h)
        card = ((m, top, W - m, bar_top - int(28 * unit)), "card")
    elif inp.price_text:
        top = bar_top - int(28 * unit) - int(80 * unit)
        card = ((m, top, W - m, bar_top - int(28 * unit)), "price_only")
    band_top = text_bottom + int(24 * unit)
    band_bottom = (card[0][1] if card and not side_photo else bar_top) - int(24 * unit)

    # ── 3. motif in the free band (behind nothing) ──
    if not inp.background:
        band_h = band_bottom - band_top
        cx = (m + (m + text_w)) // 2 if side_photo else W // 2
        if band_h >= int(140 * unit):
            if inp.template in ("festive", "promo_poster", "launch"):
                s = int(min(band_h * 0.22, 60 * unit))
                n = 3 if (text_w < W * 0.6) else 5
                gap = min(text_w // n, int(s * 4.2))
                start = cx - gap * (n - 1) // 2
                for i in range(n):
                    draw_diya(d, start + i * gap, band_top + band_h // 2 + s, s, col["gold"], col["warm"],
                              _mix(col["bg"], col["gold"], 0.16))
            else:
                r = int(min(band_h * 0.42, text_w * 0.3))
                draw_rangoli(d, cx, band_top + band_h // 2, r, _mix(col["bg"], col["accent"], 0.45))
    bg_snapshot = base.copy()

    # ── 4. draw text for real ──
    top_block(d, record=True)

    # ── 5. bottom block ──
    if card:
        box, kind = card
        if kind == "photo":
            frame = _cover(photo, box[2] - box[0], box[3] - box[1])
            mask = Image.new("L", frame.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, frame.size[0], frame.size[1]], radius=int(28 * unit), fill=255)
            base.paste(frame, box[:2], mask)
            d.rounded_rectangle(box, radius=int(28 * unit), outline=col["accent"], width=max(2, int(4 * unit)))
            notes.append("Real product photo from the catalogue.")
            price_xy = (box[0] + int(24 * unit), box[1] + int(24 * unit))
        elif kind == "card":
            d.rounded_rectangle(box, radius=int(30 * unit), fill=col["panel"])
            inner = box[2] - box[0] - int(72 * unit)
            draw_rangoli(d, int(box[2] - 110 * unit), int((box[1] + box[3]) / 2), int(min(80 * unit, (box[3] - box[1]) * 0.35)),
                         _mix(col["panel"], col["accent"], 0.6))
            y = _text(d, True, "product_name", inp.product_name, "display-bold", 70, 40, 2, col["on_panel"],
                      box[0] + int(36 * unit), box[1] + int(30 * unit), inner - int(200 * unit), unit, layers, checks,
                      min_contrast=3.0)
            if inp.product_category:
                _text(d, True, "category", inp.product_category, "ui-medium", 30, 22, 1,
                      readable(col["panel"], _mix(col["on_panel"], col["accent"], 0.2), (col["on_panel"],), 4.5),
                      box[0] + int(36 * unit), y + int(6 * unit),
                      inner - int(200 * unit), unit, layers, checks, min_contrast=3.0)
            price_xy = (box[0] + int(36 * unit), box[3] - int(36 * unit) - int(76 * unit))
            notes.append("No real product photo available: typographic product card used (stock images are never "
                         "shown as the product).")
        else:
            price_xy = (box[0], box[1])
        if inp.price_text:
            f = font("ui-bold", int(44 * unit))
            tw = d.textlength(inp.price_text, font=f)
            px, py = price_xy
            pbox = (px, py, int(px + tw + 48 * unit), int(py + 76 * unit))
            d.rounded_rectangle(pbox, radius=int(38 * unit), fill=col["gold"])
            d.text((px + 24 * unit, py + 10 * unit), inp.price_text, font=f, fill=col["on_gold"])
            layers.append(Layer("price", inp.price_text, pbox, "ui-bold", f.size, col["on_gold"]))

    # CTA bar: CTA on the left, wordmark on the right (or under the CTA when there isn't room).
    mark = inp.brand_name + (f" · {inp.website}" if inp.website else "")
    cta_right = m
    if inp.cta:
        f = font("ui-semibold", int(36 * unit))
        tw = d.textlength(inp.cta, font=f)
        if tw > W - 2 * m - int(64 * unit):
            checks.append({"code": "text_fit", "status": "fail", "message": "The CTA is too long for one line."})
        on_dark = col["bg"] == col["primary"]
        fill = col["gold"] if on_dark else col["primary"]
        color = readable(fill, col["ink"] if on_dark else col["light"], (col["ink"], col["light"], "#000000", "#FFFFFF"))
        box = (m, bar_top, int(m + tw + 64 * unit), bar_top + bar_h)
        d.rounded_rectangle(box, radius=int(bar_h / 2), fill=fill)
        d.text((m + 32 * unit, bar_top + 16 * unit), inp.cta, font=f, fill=color)
        layers.append(Layer("cta", inp.cta, box, "ui-semibold", f.size, color))
        cta_right = box[2]
    if mark.strip():
        f = font("ui-medium", int(26 * unit))
        tw = d.textlength(mark, font=f)
        if W - m - tw > cta_right + int(30 * unit):
            wx, wy = int(W - m - tw), int(bar_top + bar_h / 2 - 17 * unit)
        else:
            f = font("ui-medium", int(22 * unit))
            tw = d.textlength(mark, font=f)
            wx, wy = int(W - m - tw), int(m * 0.45) + int(6 * unit)
        d.text((wx, wy), mark, font=f, fill=col["body"])
        layers.append(Layer("wordmark", mark, (wx, wy, int(wx + tw), int(wy + 34 * unit)), "ui-medium", f.size,
                            col["body"], min_contrast=3.0))
    if inp.logo:
        try:
            lg = Image.open(io.BytesIO(inp.logo)).convert("RGBA")
            size = int(88 * unit)
            lg.thumbnail((size, size))
            base.paste(lg, (W - m - lg.size[0], m + (int(70 * unit) if inp.slide else 0)), lg)
        except Exception:  # noqa: BLE001
            notes.append("Logo couldn't be read; wordmark only.")
    if inp.background:
        f = font("ui-regular", int(20 * unit))
        txt = "AI-generated background"
        d.text((m, int(m * 0.5)), txt, font=f, fill=col["body"])
        layers.append(Layer("disclosure", txt, (m, int(m * 0.5), int(m + d.textlength(txt, font=f)),
                                                int(m * 0.5 + 26 * unit)), "ui-regular", f.size, col["body"],
                            min_contrast=3.0))
    if text_bottom > band_bottom + int(24 * unit) and card:
        checks.append({"code": "layout", "status": "fail", "message": "Text and the product block overlap; shorten "
                       "the headline or body."})

    # ── 6. checks ──
    filled = ("price", "cta", "badge", "slide_counter", "product_name", "category")
    for ly in layers:
        x0, y0, x1, y1 = (max(0, ly.box[0]), max(0, ly.box[1]), min(W, ly.box[2]), min(H, ly.box[3]))
        if x1 <= x0 or y1 <= y0:
            continue
        region = (base if ly.kind in filled else bg_snapshot).crop((x0, y0, x1, y1))
        ly.contrast = round(contrast_ratio(rgb(ly.color), _dominant_background(region, rgb(ly.color))), 2)
        if ly.contrast < ly.min_contrast:
            checks.append({"code": "contrast", "status": "fail", "message": f"{ly.kind} contrast {ly.contrast}:1 is "
                           f"below {ly.min_contrast}:1.", "layer": ly.kind})
        inside = (ly.box[0] >= m - 2 and ly.box[1] >= int(m * 0.45) - 2 and ly.box[2] <= W - m + 2
                  and ly.box[3] <= H - int(m * 0.45) + 2)
        if not inside:
            checks.append({"code": "safe_margin", "status": "fail", "message": f"{ly.kind} extends outside the safe "
                           "area.", "layer": ly.kind})
        if re.search(r"\b(\w+) \1\b", ly.text, re.I):
            checks.append({"code": "text_quality", "status": "warn", "message": f"Repeated word in {ly.kind}: "
                           f"“{ly.text[:60]}”."})
        if re.search(r"\[[^\]]*\]|\{[^}]*\}|TODO|lorem ipsum", ly.text, re.I):
            checks.append({"code": "text_quality", "status": "fail", "message": f"Placeholder text in {ly.kind}."})
    for code, msg in (("contrast", "All text meets WCAG contrast (4.5:1 body, 3:1 large text)."),
                      ("safe_margin", f"All text inside the {int(SAFE_MARGIN * 100)}% safe area."),
                      ("text_fit", "All text fits without truncation.")):
        if not any(c["code"] == code for c in checks):
            checks.append({"code": code, "status": "pass", "message": msg})

    out = io.BytesIO()
    if inp.fmt.upper() == "PNG":
        base.save(out, "PNG", optimize=True)
        ctype = "image/png"
    else:
        base.save(out, "JPEG", quality=92, optimize=True)
        ctype = "image/jpeg"
    checks.append({"code": "dimensions", "status": "pass" if base.size == (W, H) else "fail",
                   "message": f"Output is {base.size[0]}×{base.size[1]} {ctype.split('/')[1].upper()}."})
    return RenderResult(data=out.getvalue(), content_type=ctype, width=W, height=H, layers=layers, checks=checks,
                        notes=notes)


def _text(draw, record: bool, kind: str, text: str, name: str, start: int, minimum: int, max_lines: int, color: str,
          x: int, y: int, width: int, unit: float, layers: list, checks: list, *, min_contrast: float = 4.5,
          spacing: float = 1.12) -> int:
    """Draw wrapped, auto-fitted text; returns the y below it. Records a layer and any checks when ``record``."""
    if not text.strip():
        return y
    f, lines, fits = fit_text(draw, text, name, width, max_lines, int(start * unit), int(minimum * unit))
    if record:
        miss = missing_glyphs(f, text)
        if miss:
            checks.append({"code": "glyphs", "status": "fail", "message": f"The font can't draw {''.join(miss)} in the "
                           f"{kind}; keep on-image text in English (captions can use other scripts)."})
        if not fits:
            checks.append({"code": "text_fit", "status": "fail", "message": f"The {kind} doesn't fit in {max_lines} "
                           "line(s) even at the minimum size; shorten it."})
    lines = lines[:max_lines]
    asc, desc = f.getmetrics()
    line_h = int((asc + desc) * spacing)
    top = y
    for ln in lines:
        draw.text((x, y), ln, font=f, fill=color)
        y += line_h
    if record:
        layers.append(Layer(kind=kind, text=" ".join(lines), box=(x, top, x + int(max(draw.textlength(ln, font=f)
                                                                                         for ln in lines)), y),
                            font=name, size=f.size, color=color, min_contrast=min_contrast))
    return y


def _mix(a: str, b: str, t: float) -> str:
    ca, cb = rgb(a), rgb(b)
    return "#" + "".join(f"{int(ca[i] + (cb[i] - ca[i]) * t):02x}" for i in range(3))


def _dominant_background(region: Image.Image, text_color: tuple[int, int, int]) -> tuple[int, int, int]:
    """Most common colour in the region that isn't the text colour (approximates the background behind text)."""
    small = region.convert("RGB").resize((max(1, region.width // 4), max(1, region.height // 4)))
    colors = small.getcolors(maxcolors=small.width * small.height + 1) or []
    colors.sort(reverse=True)
    for _n, c in colors:
        if contrast_ratio(c, text_color) > 1.15:
            return c
    return colors[0][1] if colors else (255, 255, 255)


def convert(data: bytes, fmt: str) -> tuple[bytes, str]:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    out = io.BytesIO()
    if fmt.lower() == "png":
        img.save(out, "PNG", optimize=True)
        return out.getvalue(), "image/png"
    img.save(out, "JPEG", quality=92)
    return out.getvalue(), "image/jpeg"
