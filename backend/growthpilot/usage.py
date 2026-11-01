"""Provider usage tracking, cost estimates and budget enforcement.

Prices are estimates taken from each provider's public price list (checked 2026-10-24). They are labelled
as estimates everywhere they appear; the provider's bill is authoritative.

How it works: the agent runner opens a ``UsageScope`` around each tool call. Provider clients call
``preflight()`` before a paid request (which raises ``BudgetExceeded`` when the run, campaign or monthly budget
would be crossed) and ``record()`` afterwards. Records outside a scope (e.g. a "Test connection" button) are
still stored, without a run.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

PRICES_CHECKED = "2026-10-24"

# (input USD per 1M tokens, output USD per 1M tokens)
TOKEN_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (3.00, 15.00),
    "claude-haiku-5-5": (1.00, 5.00),
    # Gemini 3.x Flash prices rise on 2027-01-01 (to 1.50 / 7.50); see docs/API_INTEGRATIONS.md.
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-4-argon": (2.00, 10.00),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    # Groq on-demand (Developer tier) prices; the free tier records $0. See docs/API_INTEGRATIONS.md.
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
}
# USD per unit
UNIT_PRICES: dict[str, float] = {
    "brave:web_search": 5.00 / 1000,  # Search plan; Brave applies a monthly free credit first
    "gemini-3.1-flash-lite-image": 0.0336,
    "gemini-3.1-flash-image": 0.067,
    "gemini-nano-banana-2.1": 0.0336,  # 1K
    # Per output image by size (Gemini API pricing page, checked 2026-11-01). No free tier for image output.
    "gemini-nano-banana-2.1:1K": 0.0336,
    "gemini-nano-banana-2.1:2K": 0.0504,
    "gemini-nano-banana-2.1:4K": 0.113,
    "gemini-3.1-flash-image:2K": 0.101,
}
FALLBACK_TOKEN_PRICE = (5.00, 25.00)  # conservative when a model isn't in the table


def token_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = TOKEN_PRICES.get(model, FALLBACK_TOKEN_PRICE)
    return round(input_tokens * pin / 1e6 + output_tokens * pout / 1e6, 6)


def unit_cost(key: str, units: int = 1) -> float:
    return round(UNIT_PRICES.get(key, 0.0) * units, 6)


class BudgetExceeded(RuntimeError):
    def __init__(self, message: str, *, scope: str, needed_usd: float, budget_usd: float, spent_usd: float):
        super().__init__(message)
        self.scope = scope
        self.needed_usd = needed_usd
        self.budget_usd = budget_usd
        self.spent_usd = spent_usd


class QuotaExceeded(RuntimeError):
    """A request cap (not money) was reached, e.g. searches per run."""


@dataclass
class UsageScope:
    db: Session
    workspace_id: int | None
    run_id: str | None = None
    campaign_id: int | None = None
    run_budget_usd: float | None = None
    approved_extra_usd: float = 0.0  # budget increases a person approved for this run
    cost_usd: float = 0.0  # spent inside this scope (one tool call)
    tokens: int = 0
    records: list[int] = field(default_factory=list)
    # Copies of this scope's records, so a rollback of a failed attempt can't erase usage that was really spent.
    kept: list[dict] = field(default_factory=list)


_scope: contextvars.ContextVar[UsageScope | None] = contextvars.ContextVar("usage_scope", default=None)


def open_scope(scope: UsageScope):
    return _scope.set(scope)


def close_scope(token) -> None:
    _scope.reset(token)


def current() -> UsageScope | None:
    return _scope.get()


def month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def spent(db: Session, *, workspace_id: int | None = None, run_id: str | None = None,
          campaign_id: int | None = None, since: datetime | None = None) -> float:
    from .models import UsageRecord

    q = select(func.coalesce(func.sum(UsageRecord.cost_usd), 0.0))
    if workspace_id is not None:
        q = q.where(UsageRecord.workspace_id == workspace_id)
    if run_id is not None:
        q = q.where(UsageRecord.run_id == run_id)
    if campaign_id is not None:
        q = q.where(UsageRecord.campaign_id == campaign_id)
    if since is not None:
        q = q.where(UsageRecord.created_at >= since)
    return float(db.execute(q).scalar_one() or 0.0)


def preflight(estimated_usd: float, what: str) -> None:
    """Refuse a paid call that would cross a budget. No-op outside a scope or for free calls."""
    sc = current()
    if sc is None or estimated_usd <= 0 or sc.workspace_id is None:
        return
    from .models import Campaign, Workspace, utcnow

    db = sc.db
    ws = db.get(Workspace, sc.workspace_id)
    if sc.run_id and sc.run_budget_usd is not None:
        run_spent = spent(db, run_id=sc.run_id)
        budget = sc.run_budget_usd + sc.approved_extra_usd
        if run_spent + estimated_usd > budget + 1e-9:
            raise BudgetExceeded(
                f"{what} would cost about ${estimated_usd:.4f}; this run has used ${run_spent:.4f} of its "
                f"${budget:.2f} budget.", scope="run", needed_usd=estimated_usd, budget_usd=budget,
                spent_usd=run_spent)
        if ws and estimated_usd > ws.approval_threshold_usd and sc.approved_extra_usd < estimated_usd:
            raise BudgetExceeded(
                f"{what} is estimated at ${estimated_usd:.4f}, above the ${ws.approval_threshold_usd:.2f} "
                "single-operation approval threshold.", scope="operation", needed_usd=estimated_usd,
                budget_usd=ws.approval_threshold_usd, spent_usd=0.0)
    if sc.campaign_id:
        camp = db.get(Campaign, sc.campaign_id)
        if camp and camp.budget_usd is not None:
            camp_spent = spent(db, campaign_id=camp.id)
            if camp_spent + estimated_usd > camp.budget_usd + sc.approved_extra_usd + 1e-9:
                raise BudgetExceeded(
                    f"{what} would take campaign “{camp.name}” over its ${camp.budget_usd:.2f} budget "
                    f"(${camp_spent:.4f} used).", scope="campaign", needed_usd=estimated_usd,
                    budget_usd=camp.budget_usd, spent_usd=camp_spent)
    if ws:
        m_spent = spent(db, workspace_id=ws.id, since=month_start(utcnow()))
        if m_spent + estimated_usd > ws.monthly_budget_usd + 1e-9:
            raise BudgetExceeded(
                f"{what} would take the workspace over its ${ws.monthly_budget_usd:.2f} monthly budget "
                f"(${m_spent:.4f} used this month).", scope="monthly", needed_usd=estimated_usd,
                budget_usd=ws.monthly_budget_usd, spent_usd=m_spent)


def record(*, provider: str, operation: str, model: str = "", input_tokens: int = 0, output_tokens: int = 0,
           units: int = 0, cost_usd: float | None = None, cached: bool = False, db: Session | None = None,
           workspace_id: int | None = None) -> float:
    """Store a usage record (in the current scope's session) and return its estimated cost."""
    from .models import UsageRecord

    if cost_usd is None:
        cost_usd = token_cost(model, input_tokens, output_tokens) if (input_tokens or output_tokens) else 0.0
    if cached:
        cost_usd = 0.0
    sc = current()
    session = sc.db if sc else db
    if session is None:
        return cost_usd
    fields = dict(workspace_id=sc.workspace_id if sc else workspace_id, run_id=sc.run_id if sc else None,
                  campaign_id=sc.campaign_id if sc else None, provider=provider, operation=operation[:60],
                  model=model[:80], input_tokens=input_tokens, output_tokens=output_tokens, units=units,
                  cost_usd=cost_usd, cached=cached)
    rec = UsageRecord(**fields)
    session.add(rec)
    session.flush()
    if sc:
        sc.cost_usd += cost_usd
        sc.tokens += input_tokens + output_tokens
        sc.records.append(rec.id)
        sc.kept.append(fields | {"id": rec.id} | ({"created_at": rec.created_at} if rec.created_at else {}))
    return cost_usd


def restore(sc: UsageScope) -> int:
    """After a rollback: write back the scope's usage records the rollback removed. Returns how many."""
    from .models import UsageRecord

    restored = 0
    for fields in sc.kept:
        if sc.db.get(UsageRecord, fields["id"]) is None:  # committed records survive a rollback; don't duplicate
            sc.db.add(UsageRecord(**fields))
            restored += 1
    if restored:
        sc.db.flush()
    return restored


def estimate_llm_call(model: str, prompt_chars: int, max_output_tokens: int = 4000) -> float:
    """Rough upper estimate: ~4 characters per token for input, the expected output at most."""
    return token_cost(model, prompt_chars // 4 + 500, max_output_tokens)


def summary(db: Session, workspace_id: int) -> dict:
    from .models import UsageRecord, Workspace, utcnow

    ws = db.get(Workspace, workspace_id)
    since = month_start(utcnow())
    rows = db.execute(select(UsageRecord.provider, UsageRecord.operation, func.count(UsageRecord.id),
                             func.sum(UsageRecord.input_tokens), func.sum(UsageRecord.output_tokens),
                             func.sum(UsageRecord.units), func.sum(UsageRecord.cost_usd),
                             func.sum(case((UsageRecord.cached.is_(True), 1), else_=0)))
                      .where(UsageRecord.workspace_id == workspace_id, UsageRecord.created_at >= since)
                      .group_by(UsageRecord.provider, UsageRecord.operation)).all()
    by_provider = [{"provider": r[0], "operation": r[1], "calls": r[2], "input_tokens": int(r[3] or 0),
                    "output_tokens": int(r[4] or 0), "units": int(r[5] or 0), "cost_usd": round(float(r[6] or 0), 6),
                    "cached": int(r[7] or 0)} for r in rows]
    month = round(sum(r["cost_usd"] for r in by_provider), 6)
    return {"month_start": since.date().isoformat(), "month_cost_usd": month,
            "monthly_budget_usd": ws.monthly_budget_usd if ws else None,
            "run_budget_usd": ws.run_budget_usd if ws else None,
            "approval_threshold_usd": ws.approval_threshold_usd if ws else None,
            "search_requests_per_run": ws.search_requests_per_run if ws else None,
            "search_requests_per_day": ws.search_requests_per_day if ws else None,
            "by_provider": sorted(by_provider, key=lambda r: -r["cost_usd"]),
            "prices": {"checked": PRICES_CHECKED, "token_prices_per_million": TOKEN_PRICES,
                       "unit_prices": UNIT_PRICES},
            "note": "Estimates from published provider prices. Free tiers and credits are not deducted; the "
                    "provider's own billing is authoritative."}
