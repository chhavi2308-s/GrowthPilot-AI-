"""Optional AI backgrounds via Gemini image generation (Interactions API, ``response_format`` type ``image``).

Only abstract backgrounds are requested — never products, people, deities or text — and the poster labels the
background as AI-generated. Product imagery always comes from real photos. Implemented from Google's image
generation docs (checked 2026-10-24); tested against a mocked API only.
"""

from __future__ import annotations

import base64
import io
import threading

import httpx

from .. import usage
from ..config import get_settings
from ..research.net import client

API = "https://generativelanguage.googleapis.com/v1beta/interactions"
RATIOS = {(4, 5): "4:5", (1, 1): "1:1", (2, 3): "2:3", (16, 9): "16:9", (9, 16): "9:16", (3, 4): "3:4"}


class ImageGenError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def nearest_ratio(w: int, h: int) -> str:
    r = w / h
    return min(RATIOS.items(), key=lambda kv: abs(kv[0][0] / kv[0][1] - r))[1]


def background_prompt(theme: str, colors: list[str]) -> str:
    return (f"An abstract, softly lit decorative background for a festive Indian home-devotion brand poster. Theme: "
            f"{theme[:120]}. Use a palette close to {', '.join(colors[:4]) or 'maroon, gold and cream'}. Subtle "
            "texture, warm bokeh, traditional geometric patterns at the edges, generous empty space in the centre "
            "for text. No text, no letters, no logos, no products, no people, no deities, no religious idols.")


def generate_background(theme: str, colors: list[str], width: int, height: int) -> tuple[bytes, str, dict]:
    s = get_settings()
    if not s.image_generation_configured:
        raise ImageGenError("No image-generation provider is configured (IMAGE_GENERATION_PROVIDER=gemini and a key).")
    if s.is_demo:
        raise ImageGenError("Demo mode makes no external calls; using the template background.")
    model = s.image_generation_model
    usage.preflight(usage.unit_cost(model), "An AI background image")
    prompt = background_prompt(theme, colors)
    body = {"model": model, "store": False, "input": [{"type": "text", "text": prompt}],
            "response_format": {"type": "image", "mime_type": "image/jpeg",
                                "aspect_ratio": nearest_ratio(width, height)}}
    try:
        with client(timeout=httpx.Timeout(120.0, connect=10.0),
                    headers={"x-goog-api-key": s.image_key, "Content-Type": "application/json"}) as c:
            resp = c.post(API, json=body)
    except httpx.HTTPError as exc:
        raise ImageGenError(f"Image request failed ({type(exc).__name__}).", retryable=True) from exc
    if resp.status_code in (401, 403):
        raise ImageGenError("The image provider rejected the API key.")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise ImageGenError(f"Image provider busy (HTTP {resp.status_code}).", retryable=True)
    if resp.status_code >= 400:
        raise ImageGenError(f"Image provider returned HTTP {resp.status_code}.")
    data = resp.json()
    for step in data.get("steps") or []:
        for block in step.get("content") or []:
            if block.get("type") == "image" and block.get("data"):
                img = base64.b64decode(block["data"])
                usage.record(provider="gemini-image", operation="background", model=model, units=1,
                             cost_usd=usage.unit_cost(model))
                return img, block.get("mime_type", "image/jpeg"), {"model": model, "prompt": prompt}
    raise ImageGenError("The image provider returned no image.")


# ── campaign images: Gemini through the official google-genai SDK ───────────────────────────────────────────
# Verified 2026-11-01 against ai.google.dev/gemini-api/docs/image-generation: model ``gemini-nano-banana-2.1``
# ("Nano Banana 2.1") supports 1K/2K/4K via ``response_format={"type": "image", "image_size": "2K",
# "aspect_ratio": ...}`` on ``client.interactions.create``; the image comes back base64 in ``output_image`` /
# ``steps[].content[]``. Image output has no free tier ($0.0504 per 2K image).

MIN_LONG_SIDE = {"1K": 900, "2K": 1600, "4K": 3200}  # lenient: exact pixel sizes vary by aspect ratio
MAX_IMAGE_BYTES = 20 * 1024 * 1024
_slots: threading.BoundedSemaphore | None = None
_slots_size = 0
_slots_lock = threading.Lock()


def image_price(model: str, size: str) -> float:
    return usage.UNIT_PRICES.get(f"{model}:{size}", usage.UNIT_PRICES.get(model, 0.0))


def _limiter() -> threading.BoundedSemaphore:
    """Caps image requests in flight in this process (CAMPAIGN_IMAGE_CONCURRENCY)."""
    global _slots, _slots_size
    n = get_settings().campaign_image_concurrency
    with _slots_lock:
        if _slots is None or _slots_size != n:
            _slots, _slots_size = threading.BoundedSemaphore(n), n
        return _slots


def _sdk_client(api_key: str):
    from google import genai
    from google.genai import types

    s = get_settings()
    http = client(timeout=httpx.Timeout(float(s.image_timeout_seconds), connect=10.0))
    c = genai.Client(api_key=api_key, http_options=types.HttpOptions(
        httpxClient=http, retryOptions=types.HttpRetryOptions(attempts=1)))
    # Retries are ours (bounded per day and counted), so each call must send exactly one billable request. With
    # google-genai 2.34 the interactions API treats `attempts` as a retry count and the client raises 0 to 1, so
    # a 429 or 5xx was silently re-sent; switch its retry strategy off (tests pin one request per call).
    cfg = c.interactions.sdk_configuration
    cfg.retry_config = type(cfg.retry_config)("none", None, False)
    return c


def _image_from(result) -> tuple[str, str] | None:
    data = result.model_dump() if hasattr(result, "model_dump") else dict(result or {})
    out = data.get("output_image") or {}
    if out.get("data"):
        return out["data"], out.get("mime_type") or ""
    for step in data.get("steps") or []:
        for block in step.get("content") or []:
            if block.get("type") == "image" and block.get("data"):
                return block["data"], block.get("mime_type") or ""
    return None


def validate_image(raw: bytes, *, size: str, aspect_ratio: str) -> tuple[str, int, int]:
    """Check that returned bytes are a real JPEG/PNG of the requested resolution and shape."""
    from PIL import Image, UnidentifiedImageError

    from ..services.assets import sniff

    if not raw:
        raise ImageGenError("The image provider returned an empty image.")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageGenError(f"The returned image is too large ({len(raw) // 1024 // 1024} MB).")
    ctype = sniff(raw)
    if ctype is None:
        raise ImageGenError("The returned file isn't a JPEG or PNG image.")
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.verify()
        with Image.open(io.BytesIO(raw)) as im:
            w, h = im.size
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageGenError("The returned image is corrupt.") from exc
    if max(w, h) < MIN_LONG_SIDE.get(size, 0):
        raise ImageGenError(f"The image came back at {w}×{h}, smaller than the requested {size}.")
    rw, rh = (int(x) for x in aspect_ratio.split(":"))
    if abs((w / h) - (rw / rh)) > 0.03 * (rw / rh):
        raise ImageGenError(f"The image came back at {w}×{h}, not the requested {aspect_ratio} shape.")
    return ctype, w, h


def generate_image(prompt: str, *, aspect_ratio: str, size: str, model: str,
                   operation: str = "campaign_image") -> tuple[bytes, str, dict]:
    """One billable image request. Raises ImageGenError (``retryable`` for rate limits, timeouts and server errors;
    not for bad keys, safety blocks or invalid images). Never retries by itself."""
    s = get_settings()
    if s.is_demo:
        raise ImageGenError("Demo mode makes no external calls.")
    if not s.image_generation_configured:
        raise ImageGenError("Image generation isn't configured (IMAGE_GENERATION_PROVIDER=gemini and a key).")
    price = image_price(model, size)
    usage.preflight(price, f"A {size} campaign image")
    body = {"model": model, "store": False, "input": [{"type": "text", "text": prompt}],
            "response_format": {"type": "image", "mime_type": "image/png", "aspect_ratio": aspect_ratio,
                                "image_size": size}}
    with _limiter():
        try:
            result = _sdk_client(s.image_key).interactions.create(**body)
        except Exception as exc:  # noqa: BLE001 - SDK errors are classified by status code and type name
            raise _classify(exc, s.image_key) from exc
    status = (result.model_dump() if hasattr(result, "model_dump") else dict(result or {})).get("status")
    found = _image_from(result)
    if found is None:
        # Usually a safety block or a refusal; the request itself was accepted.
        usage.record(provider="gemini-image", operation=f"{operation}:no_image", model=model, units=0, cost_usd=0.0)
        raise ImageGenError("The image provider returned no image (status "
                            f"{status or 'unknown'}). This is usually a safety block; change the prompt and retry.")
    raw = base64.b64decode(found[0])
    ctype, w, h = validate_image(raw, size=size, aspect_ratio=aspect_ratio)
    usage.record(provider="gemini-image", operation=operation, model=model, units=1, cost_usd=price)
    return raw, ctype, {"model": model, "image_size": size, "aspect_ratio": aspect_ratio, "width": w, "height": h,
                        "prompt": prompt, "cost_usd": price, "status": status or "completed"}


def _classify(exc: Exception, key: str) -> ImageGenError:
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    name = type(exc).__name__
    text = str(exc).replace(key, "[redacted]") if key else str(exc)
    if isinstance(exc, httpx.TimeoutException) or "Timeout" in name:
        return ImageGenError("The image request timed out.", retryable=True)
    if isinstance(exc, httpx.TransportError) or "Connection" in name:
        return ImageGenError("Could not reach the image provider.", retryable=True)
    if code in (401, 403):
        return ImageGenError("The image provider rejected the API key (check GEMINI_API_KEY and billing).")
    if code == 404:
        return ImageGenError("The image model wasn't found. Check CAMPAIGN_IMAGE_MODEL.")
    if code == 429:
        return ImageGenError("The image provider's rate limit or quota was reached.", retryable=True)
    if isinstance(code, int) and code >= 500:
        return ImageGenError(f"Image provider error (HTTP {code}).", retryable=True)
    if isinstance(code, int) and code >= 400:
        detail = "safety" if "safety" in text.lower() or "block" in text.lower() else f"HTTP {code}"
        return ImageGenError(f"The image provider refused the request ({detail}).")
    return ImageGenError(f"Image request failed ({name}).", retryable=True)
