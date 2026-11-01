"""Agent run execution: Observe → Plan → Select tool → Execute → Verify → Replan or finish.

Every step is persisted as an ``AgentStep`` (with the acting specialist role, the tool's side-effect class, its
estimated cost and the evidence it used) so the Command Centre shows real progress by polling. Limits: max steps,
token budget, cost budget (with a human budget-approval pause), wall-clock deadline, cancellation, pause/resume,
bounded retries, and automatic resume after a worker crash (completed work is never redone).
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import audit, usage
from ..config import get_settings
from ..llm import LLMError, LLMUnavailable, get_llm, provider_of
from ..models import (
    AgentRun,
    AgentStep,
    ApprovalRequest,
    Brand,
    Membership,
    RunStatus,
    Workspace,
    utcnow,
)
from ..security import redact
from . import tools as _tools  # noqa: F401 - registers tools
from .intent import parse_goal
from .planner_demo import Action, DemoPlanner
from .planner_llm import LLMPlanner
from .registry import ROLE_LABELS, Role, ToolBlocked, ToolContext, ToolError, ToolResult, get_tool

log = logging.getLogger(__name__)

RULES_PREFIX = "rules:"  # live run on the fixed step plan; the rest of the name is the LLM writing the text

MAX_RETRIES = 2
PLANNER_RETRY_WAITS = (10, 20, 40, 60)  # seconds; long enough for per-minute rate limits to reset
PLANNER_RETRIES = len(PLANNER_RETRY_WAITS)
MAX_RECOVERIES = 2
_sleep = time.sleep
FINISHED = (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.BLOCKED)


def _gemini_fallback(settings, model: str):
    from ..llm.gemini_llm import GeminiLLM

    return GeminiLLM(api_key=settings.gemini_api_key, model=model, thinking_level=settings.gemini_thinking_level)


def set_sleep(fn) -> None:
    global _sleep
    _sleep = fn


def _json(v: Any) -> Any:
    """Truncate large values for storage in the trace."""
    import json

    text = json.dumps(v, default=str, ensure_ascii=False)
    if len(text) <= 20000:
        return json.loads(text)
    return {"truncated": True, "preview": text[:20000]}


def _evidence(out: Any) -> list[int]:
    """Collect research-finding ids a tool reports using, for the trace."""
    ids: set[int] = set()

    def walk(v: Any, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(v, dict):
            for k, x in v.items():
                if k in ("finding_id",) and isinstance(x, int):
                    ids.add(x)
                elif k in ("evidence_ids", "finding_ids") and isinstance(x, list):
                    ids.update(i for i in x if isinstance(i, int))
                else:
                    walk(x, depth + 1)
        elif isinstance(v, list):
            for x in v[:200]:
                walk(x, depth + 1)

    walk(out)
    return sorted(ids)[:100]


def create_run(db: Session, *, workspace_id: int, user_id: int | None, goal: str, brand_id: int | None = None) -> AgentRun:
    s = get_settings()
    ws = db.get(Workspace, workspace_id)
    if brand_id is None:
        brand_id = db.execute(select(Brand.id).where(Brand.workspace_id == workspace_id).order_by(Brand.id)
                              .limit(1)).scalar_one_or_none()
    if s.is_live:
        llm = get_llm()  # raises LLMUnavailable with a clear message
        # Rules-led: the fixed step plan, with the LLM writing the text (fits Groq's free-tier token limits).
        planner = (RULES_PREFIX + llm.name if s.uses_rules_planner else llm.name)[:40]
    else:
        planner = DemoPlanner.name
    intent, params = parse_goal(goal)
    run = AgentRun(workspace_id=workspace_id, brand_id=brand_id, goal=goal.strip()[:4000], intent=intent,
                   planner=planner, status=RunStatus.QUEUED, max_steps=ws.agent_max_steps,
                   token_budget=ws.agent_token_budget, cost_budget_usd=ws.run_budget_usd, created_by=user_id,
                   state={"params": params})
    db.add(run)
    db.flush()
    audit.record(db, "agent.run_created", workspace_id=workspace_id, user_id=user_id, target_type="run",
                 target_id=run.id, intent=intent, planner=planner)
    return run


class Runner:
    def __init__(self, db: Session, run: AgentRun):
        self.db = db
        self.run = run
        role = "editor"
        if run.created_by:
            m = db.execute(select(Membership).where(Membership.user_id == run.created_by,
                                                    Membership.workspace_id == run.workspace_id)).scalar_one_or_none()
            role = m.role if m else "viewer"
        self.ctx = ToolContext(db=db, run=run, workspace_id=run.workspace_id, brand_id=run.brand_id,
                               settings=get_settings(), user_role=role)
        if run.planner == DemoPlanner.name or run.planner.startswith(RULES_PREFIX):
            self.planner = DemoPlanner()
        else:
            llm = get_llm(provider_of(run.planner))  # resume on the provider the run started with
            if llm is None:
                raise LLMUnavailable("This run needs an LLM but the app is in demo mode.")
            self.planner = LLMPlanner(llm)

    # ── trace ────────────────────────────────────────────────────────────
    def step(self, phase: str, message: str, *, tool: str | None = None, inp: Any = None, out: Any = None,
             status: str = "ok", is_mock: bool = False, attempt: int = 1, duration_ms: int = 0, tokens: int = 0,
             role: str = Role.ORCHESTRATOR.value, effect: str = "", cost_usd: float = 0.0, commit: bool = True) -> None:
        self.run.step_count += 1 if phase == "tool" else 0
        seq = (self.db.execute(select(AgentStep.seq).where(AgentStep.run_id == self.run.id)
                               .order_by(AgentStep.seq.desc()).limit(1)).scalar_one_or_none() or 0) + 1
        self.db.add(AgentStep(run_id=self.run.id, seq=seq, phase=phase, tool=tool, message=redact(message)[:8000]
                              if message else "", input=_json(inp) if inp is not None else None,
                              output=_json(out) if out is not None else None, status=status, is_mock=is_mock,
                              attempt=attempt, duration_ms=duration_ms, tokens=tokens, role=role, effect=effect,
                              cost_usd=round(cost_usd, 6), evidence_ids=_evidence(out) if out is not None else []))
        self.run.heartbeat_at = utcnow()
        if commit:
            self.db.commit()
        else:
            self.db.flush()

    def _finish(self, status: str, reason: str, summary: str) -> None:
        self.run.status = status
        self.run.completion_reason = reason[:60]
        self.run.outcome_summary = summary[:8000]
        self.run.finished_at = utcnow()
        self.step("finish", summary, status="ok" if status == RunStatus.COMPLETED else "blocked")
        audit.record(self.db, "agent.run_finished", workspace_id=self.run.workspace_id, target_type="run",
                     target_id=self.run.id, status=status, reason=reason, cost_usd=round(self.run.cost_usd, 6))
        self.db.commit()

    def _limits(self) -> str | None:
        self.db.refresh(self.run, ["cancel_requested", "pause_requested"])
        if self.run.cancel_requested:
            return "cancelled"
        if self.run.pause_requested:
            return "paused"
        if self.run.step_count >= self.run.max_steps:
            return "step_limit"
        if self.run.tokens_used >= self.run.token_budget:
            return "token_budget"
        if self.run.deadline_at and utcnow() > self.run.deadline_at:
            return "timeout"
        return None

    def _scope(self) -> usage.UsageScope:
        st = self.run.state or {}
        return usage.UsageScope(db=self.db, workspace_id=self.run.workspace_id, run_id=self.run.id,
                                campaign_id=st.get("campaign_id") or self.run.campaign_id,
                                run_budget_usd=self.run.cost_budget_usd,
                                approved_extra_usd=float(st.get("approved_extra_usd", 0.0)))

    def _rollback(self, scope: usage.UsageScope) -> None:
        """Undo a failed attempt's work but keep what it really spent: usage records and the run's totals."""
        self.db.rollback()
        usage.restore(scope)
        self._count(scope)

    def _count(self, scope: usage.UsageScope) -> None:
        self.run.tokens_used += scope.tokens
        self.run.cost_usd = round((self.run.cost_usd or 0.0) + scope.cost_usd, 6)

    # ── tool execution ───────────────────────────────────────────────────
    def execute(self, action: Action) -> tuple[str, ToolResult | None, str]:
        try:
            tool = get_tool(action.tool or "")
        except ToolError as exc:
            self.step("tool", str(exc), tool=action.tool, inp=action.input, status="error")
            return "error", None, str(exc)
        meta = {"role": tool.role.value, "effect": ",".join(e.value for e in tool.effects)}
        try:
            inp = tool.input_model.model_validate(action.input or {})
        except ValidationError as exc:
            msg = "Invalid tool input: " + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
            self.step("tool", msg, tool=tool.name, inp=action.input, status="error", **meta)
            return "error", None, msg
        attempt = 0
        while True:
            attempt += 1
            t0 = time.monotonic()
            scope = self._scope()
            token = usage.open_scope(scope)
            try:
                result = tool.fn(self.ctx, inp)
                self.db.flush()
            except usage.BudgetExceeded as exc:
                self._rollback(scope)
                return self._blocked(tool, inp, attempt, t0, meta, str(exc), "budget",
                                     budget={"scope": exc.scope, "needed_usd": exc.needed_usd,
                                             "budget_usd": exc.budget_usd, "spent_usd": exc.spent_usd})
            except usage.QuotaExceeded as exc:
                self._rollback(scope)
                return self._blocked(tool, inp, attempt, t0, meta, str(exc), "search_quota")
            except ToolBlocked as exc:
                self._rollback(scope)
                return self._blocked(tool, inp, attempt, t0, meta, str(exc), exc.blocker)
            except (ToolError, LLMError) as exc:
                self._rollback(scope)
                ms = int((time.monotonic() - t0) * 1000)
                retry = getattr(exc, "retryable", False) and attempt <= MAX_RETRIES
                self.step("tool", f"{'Retrying after error' if retry else 'Error'}: {exc}", tool=tool.name,
                          inp=inp.model_dump(mode="json"), status="error", attempt=attempt, duration_ms=ms, **meta)
                if retry:
                    _sleep(min(2 ** attempt, 8))
                    continue
                return "error", None, str(exc)
            except Exception as exc:  # noqa: BLE001
                self._rollback(scope)
                log.exception("tool %s crashed", tool.name)
                self.step("tool", f"Unexpected error in {tool.name}: {type(exc).__name__}", tool=tool.name,
                          inp=inp.model_dump(mode="json"), status="error", attempt=attempt, **meta)
                return "error", None, f"internal error ({type(exc).__name__})"
            finally:
                usage.close_scope(token)
            ms = int((time.monotonic() - t0) * 1000)
            tokens = max(result.tokens, scope.tokens)
            self.run.tokens_used += tokens
            self.run.cost_usd = round((self.run.cost_usd or 0.0) + scope.cost_usd, 6)
            # Not committed yet: the planner records the outcome first, so a crash can't split the two.
            self.step("tool", result.summary, tool=tool.name, inp=inp.model_dump(mode="json"), out=result.output,
                      is_mock=result.is_mock, attempt=attempt, duration_ms=ms, tokens=tokens, cost_usd=scope.cost_usd,
                      commit=False, **meta)
            return "ok", result, ""

    def _blocked(self, tool, inp, attempt, t0, meta, message: str, blocker: str, budget: dict | None = None):
        ms = int((time.monotonic() - t0) * 1000)
        blockers = list(self.run.blockers or [])
        if blocker not in blockers:
            blockers.append(blocker)
        self.run.blockers = blockers
        if budget:
            self.ctx.remember("budget_block", budget | {"tool": tool.name, "message": message})
        self.step("tool", f"Blocked: {message}", tool=tool.name, inp=inp.model_dump(mode="json"), status="blocked",
                  attempt=attempt, duration_ms=ms, out={"blocker": blocker, **({"budget": budget} if budget else {})},
                  **meta)
        return "blocked", None, message

    # ── main loop ────────────────────────────────────────────────────────
    def run_until_pause(self) -> None:
        r = self.run
        if r.status == RunStatus.QUEUED and not r.started_at:
            r.started_at = utcnow()
            ws = self.db.get(Workspace, r.workspace_id)
            r.deadline_at = utcnow() + timedelta(minutes=ws.agent_timeout_minutes)
            r.status = RunStatus.RUNNING
            self.step("observe", f"Goal received: “{r.goal}”. Mode: {self.ctx.settings.app_mode.value}; planner: "
                      f"{r.planner}. Budget: ${r.cost_budget_usd:.2f} for this run, {r.max_steps} tool steps."
                      + (" Demo mode uses the bundled catalogue snapshot, labelled fixtures and simulated platform "
                         "adapters; nothing leaves this machine." if self.ctx.settings.is_demo else ""))
            plan, note = self.planner.start(self.ctx, r.intent, (r.state or {}).get("params", {}))
            r.plan = plan
            self.step("plan", note + "\n" + "\n".join(
                f"{i + 1}. [{ROLE_LABELS[Role(s.get('role', 'orchestrator'))]}] {s['tool']} — {s['purpose']}"
                for i, s in enumerate(plan)))
        else:
            r.status = RunStatus.RUNNING
            self.db.commit()
        planner_retries = 0
        planner_fallback_attempted = False
        while True:
            stop = self._limits()
            if stop == "paused":
                r.status, r.pause_requested = RunStatus.PAUSED, False
                self.step("decision", "Paused by a person. Completed work is kept; resume to continue from here.")
                return
            if stop:
                status = RunStatus.CANCELLED if stop == "cancelled" else RunStatus.BLOCKED
                msg = {"cancelled": "Cancelled by a user.", "step_limit": f"Stopped: reached the {r.max_steps}-step "
                       "limit.", "token_budget": f"Stopped: token budget ({r.token_budget:,}) used.",
                       "timeout": "Stopped: run exceeded its time limit."}[stop]
                self._finish(status, stop, msg)
                return
            scope = self._scope()
            token = usage.open_scope(scope)
            try:
                res = self.planner.next(self.ctx)
            except usage.BudgetExceeded as exc:
                self._rollback(scope)
                self.ctx.remember("budget_block", {"scope": exc.scope, "needed_usd": exc.needed_usd,
                                                   "budget_usd": exc.budget_usd, "spent_usd": exc.spent_usd,
                                                   "tool": "planner", "message": str(exc)})
                self._await_budget(str(exc))
                return
            except LLMError as exc:
                if exc.retryable and planner_retries < PLANNER_RETRIES:
                    # Rate limits, overloaded models and network blips usually clear within a minute.
                    self._rollback(scope)
                    planner_retries += 1
                    wait = PLANNER_RETRY_WAITS[planner_retries - 1]
                    self.step("decision", f"The planner model is unavailable right now ({exc}). Waiting {wait}s, then "
                              f"retrying (retry {planner_retries} of {PLANNER_RETRIES}). Completed work is kept.",
                              status="error")
                    _sleep(wait)
                    continue
                fallback_model = self.ctx.settings.planner_fallback_model.strip()
                current_llm = getattr(self.planner, "llm", None)
                if (exc.retryable and not planner_fallback_attempted and fallback_model and
                        self.ctx.settings.active_llm_provider == "gemini" and current_llm is not None and
                        fallback_model != getattr(current_llm, "model", None)):
                    self._rollback(scope)
                    fallback = _gemini_fallback(self.ctx.settings, fallback_model)
                    self.planner.llm = fallback
                    self.planner.name = fallback.name
                    planner_fallback_attempted = True
                    self.step("decision", f"Primary planner model retries were exhausted; trying fallback model "
                              f"{fallback.name} once.", status="error")
                    continue
                self._count(scope)
                self._finish(RunStatus.FAILED, "llm_error", f"The planner model failed: {exc}")
                return
            finally:
                usage.close_scope(token)
            planner_retries = 0
            if isinstance(res, tuple):  # LLM planner made a model call
                _action, events, tokens = res
                cost = scope.cost_usd
                r.tokens_used += tokens
                r.cost_usd = round((r.cost_usd or 0.0) + cost, 6)
                for phase, msg in events:
                    self.step(phase, msg, tokens=tokens, cost_usd=cost)
                    tokens, cost = 0, 0.0
                if not events:
                    self.db.commit()
                continue
            action = res
            if action.kind == "await_approval":
                r.status = RunStatus.AWAITING_APPROVAL
                self.step("approval", action.message, role=Role.DISTRIBUTION.value)
                return
            if action.kind == "await_budget":
                self._await_budget(action.message)
                return
            if action.kind == "finish":
                reason = action.reason or "goal_achieved"
                status = RunStatus.COMPLETED if not reason.startswith(("blocked", "failed")) else RunStatus.BLOCKED
                self._finish(status, reason, action.message)
                return
            for phase, msg in action.events:
                self.step(phase, msg, role=action.role or Role.ORCHESTRATOR.value)
            self.db.commit()  # persist planner bookkeeping so a tool-error rollback can't undo it
            status, result, error = self.execute(action)
            for phase, msg in self.planner.observe(self.ctx, action, status, result, error):
                self.step(phase, msg, commit=False)
            self.db.commit()

    def _await_budget(self, message: str) -> None:
        """Pause the run and ask an owner to approve more budget (or stop)."""
        r = self.run
        info = dict((r.state or {}).get("budget_block") or {})
        need = round(max(float(info.get("needed_usd", 0.0)), 0.05), 4)
        req = ApprovalRequest(workspace_id=r.workspace_id, run_id=r.id, kind="budget",
                              title=f"Approve ${need:.2f} more for this agent run",
                              detail=message[:2000], payload={"needed_usd": need, **info})
        self.db.add(req)
        self.db.flush()
        self.ctx.remember("awaiting_budget_request", req.id)
        r.status = RunStatus.AWAITING_APPROVAL
        self.step("approval", f"Budget check: {message} Waiting for an owner to approve ${need:.2f} more (request "
                  f"#{req.id}) or stop the run.", status="blocked", role=Role.ORCHESTRATOR.value)
        audit.record(self.db, "approval.requested", workspace_id=r.workspace_id, target_type="approval_request",
                     target_id=req.id, kind="budget", run_id=r.id, needed_usd=need)
        self.db.commit()


def claim_next_run(db: Session) -> AgentRun | None:
    rid = db.execute(select(AgentRun.id).where(AgentRun.status == RunStatus.QUEUED).order_by(AgentRun.created_at)
                     .limit(1)).scalar_one_or_none()
    if rid is None:
        return None
    res = db.execute(update(AgentRun).where(AgentRun.id == rid, AgentRun.status == RunStatus.QUEUED)
                     .values(status=RunStatus.RUNNING, heartbeat_at=utcnow()))
    db.commit()
    if res.rowcount != 1:
        return None
    run = db.get(AgentRun, rid)
    db.refresh(run)
    return run


def execute_run(db: Session, run: AgentRun) -> AgentRun:
    """Run (or resume) until it finishes or pauses. Never raises."""
    try:
        if run.status == RunStatus.RUNNING and not run.started_at:
            run.status = RunStatus.QUEUED  # let run_until_pause initialise it
        Runner(db, run).run_until_pause()
    except LLMUnavailable as exc:
        db.rollback()
        run = db.get(AgentRun, run.id)
        run.status, run.error, run.completion_reason, run.finished_at = RunStatus.BLOCKED, str(exc), "llm_unavailable", utcnow()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        log.exception("agent run %s crashed", run.id)
        db.rollback()
        run = db.get(AgentRun, run.id)
        run.status, run.error, run.completion_reason = RunStatus.FAILED, f"Internal error ({type(exc).__name__}).", "crash"
        run.finished_at = utcnow()
        db.commit()
    return run


def resume_run(db: Session, run: AgentRun, note: str) -> None:
    if run.status != RunStatus.AWAITING_APPROVAL:
        return
    runner = Runner(db, run)
    runner.planner.resume(runner.ctx, note)
    run.status = RunStatus.QUEUED
    runner.step("approval", f"Resumed: {note}")


def pause_run(db: Session, run: AgentRun) -> None:
    if run.status == RunStatus.QUEUED:
        run.status = RunStatus.PAUSED
    elif run.status == RunStatus.RUNNING:
        run.pause_requested = True


def unpause_run(db: Session, run: AgentRun) -> None:
    if run.status != RunStatus.PAUSED:
        return
    run.pause_requested = False
    run.status = RunStatus.QUEUED
    Runner(db, run).step("decision", "Resumed by a person; continuing from the last completed step.")


def retry_run(db: Session, run: AgentRun) -> None:
    """Continue a blocked or failed run from where it stopped, keeping completed work."""
    if run.status not in (RunStatus.BLOCKED, RunStatus.FAILED):
        return
    if run.brand_id is None:  # the brand may have been created after the run was blocked on it
        run.brand_id = db.execute(select(Brand.id).where(Brand.workspace_id == run.workspace_id).order_by(Brand.id)
                                  .limit(1)).scalar_one_or_none()
    runner = Runner(db, run)
    runner.planner.retry(runner.ctx)
    ws = db.get(Workspace, run.workspace_id)
    run.status, run.finished_at, run.error, run.completion_reason = RunStatus.QUEUED, None, None, ""
    run.blockers = []
    run.max_steps = max(run.max_steps, run.step_count + ws.agent_max_steps // 2)
    run.deadline_at = utcnow() + timedelta(minutes=ws.agent_timeout_minutes)
    runner.step("replan", "Retry requested: failed or blocked steps are queued again; completed steps are kept.")


def decide_budget(db: Session, req: ApprovalRequest, approved: bool, user_id: int, note: str = "") -> None:
    run = db.get(AgentRun, req.run_id) if req.run_id else None
    req.status = "approved" if approved else "rejected"
    req.decided_by, req.decided_at, req.decision_note = user_id, utcnow(), note[:2000]
    audit.record(db, "approval.decided", workspace_id=req.workspace_id, user_id=user_id,
                 target_type="approval_request", target_id=req.id, kind=req.kind, approved=approved)
    if run is None or run.status != RunStatus.AWAITING_APPROVAL:
        return
    runner = Runner(db, run)
    if approved:
        extra = float((run.state or {}).get("approved_extra_usd", 0.0)) + float(req.payload.get("needed_usd", 0.0))
        # Approving one blocked operation covers it and a little headroom for the rest of the run.
        runner.ctx.remember("approved_extra_usd", round(extra + 0.05, 4))
        runner.ctx.remember("awaiting_budget_request", None)
        runner.planner.on_budget(runner.ctx, True)
        run.status = RunStatus.QUEUED
        runner.step("approval", f"Budget increase approved (${req.payload.get('needed_usd', 0):.2f}); continuing.")
    else:
        runner.ctx.remember("awaiting_budget_request", None)
        runner.planner.on_budget(runner.ctx, False)
        run.status = RunStatus.QUEUED
        runner.step("approval", "Budget increase declined; the agent continues without the paid step where it can.")


def recover_stale(db: Session) -> None:
    """Runs left 'running' by a stopped worker resume from their last committed step (at most twice)."""
    cutoff = utcnow() - timedelta(minutes=10)
    for run in db.execute(select(AgentRun).where(AgentRun.status == RunStatus.RUNNING,
                                                 AgentRun.heartbeat_at < cutoff)).scalars().all():
        if run.recoveries >= MAX_RECOVERIES:
            run.status, run.completion_reason, run.finished_at = RunStatus.FAILED, "worker_stopped", utcnow()
            run.error = "The worker stopped repeatedly while this run was in progress. Use Retry to continue."
            continue
        run.recoveries += 1
        try:
            runner = Runner(db, run)
            runner.planner.recover(runner.ctx)
            run.status = RunStatus.QUEUED
            runner.step("replan", "Recovered after the worker stopped mid-run; continuing from the last completed "
                        "step (nothing completed is redone).", commit=False)
        except Exception:  # noqa: BLE001
            log.exception("could not recover run %s", run.id)
            run.status, run.completion_reason, run.finished_at = RunStatus.FAILED, "worker_stopped", utcnow()
    db.commit()
