"""Web search: Brave Search API (https://api.search.brave.com/res/v1/web/search, header X-Subscription-Token).

Cost controls: identical queries are served from a database cache for SEARCH_CACHE_HOURS; requests are capped
per agent run and per workspace per day (Settings → Budgets); each real request is recorded as usage with the
published Search-plan price ($5 per 1,000 requests, checked 2026-10-24; Brave applies a monthly credit first).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import timedelta

import httpx
from sqlalchemy import func, select

from .. import usage
from ..config import get_settings
from .net import client

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class SearchUnavailable(RuntimeError):
    """No search provider configured (or not allowed in this mode)."""


class SearchError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    age: str | None
    provider: str
    cached: bool = False


def _key(params: dict) -> str:
    return hashlib.sha256(("brave|" + json.dumps(params, sort_keys=True)).encode()).hexdigest()


def _check_caps(sc: usage.UsageScope, pending: int = 0) -> None:
    from ..models import UsageRecord, Workspace, utcnow

    ws = sc.db.get(Workspace, sc.workspace_id) if sc.workspace_id else None
    if ws is None:
        return
    base = select(func.count(UsageRecord.id)).where(UsageRecord.provider == "brave", UsageRecord.cached.is_(False))
    if sc.run_id:
        n_run = sc.db.execute(base.where(UsageRecord.run_id == sc.run_id)).scalar_one()
        if n_run + pending >= ws.search_requests_per_run:
            raise usage.QuotaExceeded(f"This run reached its cap of {ws.search_requests_per_run} web searches.")
    n_day = sc.db.execute(base.where(UsageRecord.workspace_id == ws.id,
                                     UsageRecord.created_at >= utcnow() - timedelta(days=1))).scalar_one()
    if n_day + pending >= ws.search_requests_per_day:
        raise usage.QuotaExceeded(f"The workspace reached its cap of {ws.search_requests_per_day} web searches per day.")


def search(query: str, *, count: int = 8, country: str | None = None, freshness: str | None = None,
           use_cache: bool = True) -> list[SearchResult]:
    from ..models import CacheEntry, utcnow

    s = get_settings()
    if s.is_demo:
        raise SearchUnavailable("Live web search is disabled in demo mode (no external calls).")
    if not s.search_configured:
        raise SearchUnavailable("No search provider configured. Set BRAVE_SEARCH_API_KEY in .env.")
    params: dict = {"q": query[:400], "count": max(1, min(count, 20))}
    if country:
        params["country"] = country
    if freshness in {"pd", "pw", "pm", "py"}:
        params["freshness"] = freshness
    sc = usage.current()
    key = _key(params)
    if sc is not None and use_cache and s.search_cache_hours > 0:
        hit = sc.db.execute(select(CacheEntry).where(CacheEntry.key_hash == key, CacheEntry.expires_at > utcnow())
                            ).scalar_one_or_none()
        if hit is not None:
            usage.record(provider="brave", operation="web_search", units=1, cached=True)
            return [SearchResult(**{**r, "cached": True}) for r in hit.response]
    if sc is not None:
        _check_caps(sc)
        usage.preflight(usage.unit_cost("brave:web_search"), "A web search")
    try:
        with client(timeout=_timeout(), headers={"Accept": "application/json",
                                                 "X-Subscription-Token": s.brave_key}) as c:
            resp = c.get(ENDPOINT, params=params)
    except Exception as exc:  # noqa: BLE001
        raise SearchError(f"Search request failed: {type(exc).__name__}", retryable=True) from exc
    if resp.status_code in (401, 403):
        raise SearchError(f"Brave rejected the API key (HTTP {resp.status_code}).")
    if resp.status_code == 429:
        raise SearchError("Brave rate limit reached.", retryable=True)
    if resp.status_code >= 400:
        raise SearchError(f"Brave returned HTTP {resp.status_code}.", retryable=resp.status_code >= 500)
    usage.record(provider="brave", operation="web_search", units=1, cost_usd=usage.unit_cost("brave:web_search"))
    data = resp.json()
    out = []
    for r in (data.get("web") or {}).get("results", [])[:count]:
        url = r.get("url") or ""
        if not url.startswith(("http://", "https://")):
            continue
        out.append(SearchResult(title=(r.get("title") or "")[:300], url=url[:1000],
                                snippet=(r.get("description") or "")[:600], age=r.get("age") or r.get("page_age"),
                                provider="brave"))
    if sc is not None and s.search_cache_hours > 0:
        row = sc.db.execute(select(CacheEntry).where(CacheEntry.key_hash == key)).scalar_one_or_none()
        payload = [asdict(r) for r in out]
        if row is None:
            sc.db.add(CacheEntry(key_hash=key, provider="brave", request=params, response=payload,
                                 expires_at=utcnow() + timedelta(hours=s.search_cache_hours)))
        else:
            row.response, row.created_at = payload, utcnow()
            row.expires_at = utcnow() + timedelta(hours=s.search_cache_hours)
        sc.db.flush()
    return out


def search_many(queries: list[str], *, count: int = 5, country: str | None = "IN",
                freshness: str | None = "py") -> dict[str, list[SearchResult] | Exception]:
    """Run several searches. Cache lookups, caps, budget checks and usage records happen in the calling thread;
    only the HTTP requests run in parallel (the database session isn't shared across threads)."""
    from concurrent.futures import ThreadPoolExecutor

    s = get_settings()
    out: dict[str, list[SearchResult] | Exception] = {}
    todo: list[tuple[str, dict]] = []
    for q in dict.fromkeys(queries):  # de-duplicate, keep order
        try:
            cached = _cached(q, count, country, freshness)
        except Exception as exc:  # noqa: BLE001
            out[q] = exc
            continue
        if cached is not None:
            out[q] = cached
        else:
            todo.append((q, _params(q, count, country, freshness)))
    sc = usage.current()
    allowed: list[tuple[str, dict]] = []
    for q, params in todo:
        try:
            if s.is_demo:
                raise SearchUnavailable("Live web search is disabled in demo mode (no external calls).")
            if not s.search_configured:
                raise SearchUnavailable("No search provider configured. Set BRAVE_SEARCH_API_KEY in .env.")
            if sc is not None:
                _check_caps(sc, pending=len(allowed))
                usage.preflight(usage.unit_cost("brave:web_search") * (len(allowed) + 1), "Web searches")
            allowed.append((q, params))
        except Exception as exc:  # noqa: BLE001
            out[q] = exc

    def fetch(params: dict):
        with client(timeout=_timeout(), headers={"Accept": "application/json",
                                                 "X-Subscription-Token": s.brave_key}) as c:
            return c.get(ENDPOINT, params=params)

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(allowed)))) as pool:
        futures = {q: pool.submit(fetch, params) for q, params in allowed}
    for q, params in allowed:
        try:
            resp = futures[q].result()
            out[q] = _parse(resp, q, params, count)
        except Exception as exc:  # noqa: BLE001
            out[q] = exc if isinstance(exc, SearchError) else SearchError(f"Search request failed: {type(exc).__name__}",
                                                                           retryable=True)
    return {q: out[q] for q in dict.fromkeys(queries)}


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(float(get_settings().search_timeout_seconds), connect=5.0)


def _params(query: str, count: int, country: str | None, freshness: str | None) -> dict:
    params: dict = {"q": query[:400], "count": max(1, min(count, 20))}
    if country:
        params["country"] = country
    if freshness in {"pd", "pw", "pm", "py"}:
        params["freshness"] = freshness
    return params


def _cached(query: str, count: int, country: str | None, freshness: str | None) -> list[SearchResult] | None:
    from ..models import CacheEntry, utcnow

    sc = usage.current()
    if sc is None or get_settings().search_cache_hours <= 0:
        return None
    hit = sc.db.execute(select(CacheEntry).where(CacheEntry.key_hash == _key(_params(query, count, country, freshness)),
                                                 CacheEntry.expires_at > utcnow())).scalar_one_or_none()
    if hit is None:
        return None
    usage.record(provider="brave", operation="web_search", units=1, cached=True)
    return [SearchResult(**{**r, "cached": True}) for r in hit.response]


def _parse(resp, query: str, params: dict, count: int) -> list[SearchResult]:
    from ..models import CacheEntry, utcnow

    if resp.status_code in (401, 403):
        raise SearchError(f"Brave rejected the API key (HTTP {resp.status_code}).")
    if resp.status_code == 429:
        raise SearchError("Brave rate limit reached.", retryable=True)
    if resp.status_code >= 400:
        raise SearchError(f"Brave returned HTTP {resp.status_code}.", retryable=resp.status_code >= 500)
    usage.record(provider="brave", operation="web_search", units=1, cost_usd=usage.unit_cost("brave:web_search"))
    data = resp.json()
    out = []
    for r in (data.get("web") or {}).get("results", [])[:count]:
        url = r.get("url") or ""
        if url.startswith(("http://", "https://")):
            out.append(SearchResult(title=(r.get("title") or "")[:300], url=url[:1000],
                                    snippet=(r.get("description") or "")[:600], age=r.get("age") or r.get("page_age"),
                                    provider="brave"))
    sc = usage.current()
    s = get_settings()
    if sc is not None and s.search_cache_hours > 0:
        key = _key(params)
        row = sc.db.execute(select(CacheEntry).where(CacheEntry.key_hash == key)).scalar_one_or_none()
        payload = [asdict(r) for r in out]
        if row is None:
            sc.db.add(CacheEntry(key_hash=key, provider="brave", request=params, response=payload,
                                 expires_at=utcnow() + timedelta(hours=s.search_cache_hours)))
        else:
            row.response, row.expires_at = payload, utcnow() + timedelta(hours=s.search_cache_hours)
        sc.db.flush()
    return out
