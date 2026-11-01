"""Groq provider against a mocked API: structured output, tool calls, rate limits, config and a rules-led live run."""

from __future__ import annotations

import json
import re

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import select

from growthpilot import config, usage
from growthpilot.agent import generation
from growthpilot.agent import runner as runner_mod
from growthpilot.agent.planner_demo import DemoPlanner
from growthpilot.agent.runner import Runner, create_run, execute_run
from growthpilot.db import session_scope
from growthpilot.demo import fixtures
from growthpilot.llm import LLMError, LLMUnavailable, get_llm, groq_llm, provider_of
from growthpilot.llm.groq_llm import GroqLLM, check_key, strict_schema, to_openai_messages
from growthpilot.models import AgentRun, ContentItem, UsageRecord, Workspace
from growthpilot.research import net

from .conftest import Api, make_client

KEY = "gsk_test-groq-key-not-real"
MODEL = "openai/gpt-oss-120b"


class Item(BaseModel):
    title: str
    note: str | None = None


class Out(BaseModel):
    name: str
    count: int = 1
    items: list[Item] = []


def _live(monkeypatch, tmp_path, **extra):
    api = Api(make_client(monkeypatch, tmp_path, "live", GROQ_API_KEY=KEY, **extra))
    api.register()
    api.put("/api/brand", json={"name": "Live Brand", "location": "Lisbon"})
    net.set_resolver(lambda host, port, proto=0: [(2, 1, 6, "", ("93.184.216.34", 443))])
    return api


def _scope(db):
    ws = db.execute(select(Workspace)).scalars().first()
    return usage.UsageScope(db=db, workspace_id=ws.id)


def _completion(content=None, tool_calls=None, finish="stop", prompt=100, completion=20):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}


# ── schema adapter and message translation ──────────────────────────────

def test_strict_schema_requires_every_property_and_closes_objects():
    s = strict_schema(Out.model_json_schema())
    assert "$defs" not in json.dumps(s) and "default" not in json.dumps(s)
    assert s["required"] == ["name", "count", "items"] and s["additionalProperties"] is False
    item = s["properties"]["items"]["items"]
    assert item["required"] == ["title", "note"] and item["additionalProperties"] is False


def test_messages_translate_tool_calls_and_results():
    history = [
        {"role": "user", "content": "Goal: plan a week"},
        {"role": "assistant", "content": [{"type": "text", "text": "Loading."},
                                          {"type": "tool_use", "id": "c1", "name": "inspect_brand", "input": {}}],
         "raw_steps": [{"type": "gemini-only"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "brand ok"},
                                     {"type": "text", "text": "Continue."}]},
    ]
    out = to_openai_messages("sys", history)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "user"]
    call = out[2]["tool_calls"][0]
    assert call["id"] == "c1" and json.loads(call["function"]["arguments"]) == {}
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "brand ok"}
    assert "raw_steps" not in json.dumps(out)


# ── HTTP behaviour ──────────────────────────────────────────────────────

def test_structured_output_free_tier_records_tokens_at_zero_cost(monkeypatch, tmp_path):
    _live(monkeypatch, tmp_path)
    seen = {}

    def handler(req: httpx.Request):
        assert str(req.url) == "https://api.groq.com/openai/v1/chat/completions"
        assert req.headers["authorization"] == f"Bearer {KEY}"
        seen.update(json.loads(req.content))
        return httpx.Response(200, json=_completion('{"name": "tile", "count": 3, "items": []}', prompt=120,
                                                    completion=30))

    net.set_transport(httpx.MockTransport(handler))
    with session_scope() as db:
        tok = usage.open_scope(_scope(db))
        try:
            out, tokens = GroqLLM(KEY, MODEL).structured("sys", "prompt", Out)
        finally:
            usage.close_scope(tok)
        rec = db.execute(select(UsageRecord)).scalars().one()
    assert out.name == "tile" and out.count == 3 and tokens == 150
    fmt = seen["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False
    assert seen["reasoning_effort"] == "low" and seen["messages"][0] == {"role": "system", "content": "sys"}
    assert rec.provider == "groq" and rec.input_tokens == 120 and rec.output_tokens == 30 and rec.cost_usd == 0


def test_developer_tier_records_cost(monkeypatch, tmp_path):
    _live(monkeypatch, tmp_path)
    net.set_transport(httpx.MockTransport(lambda req: httpx.Response(
        200, json=_completion('{"name": "x"}', prompt=1_000_000, completion=0))))
    with session_scope() as db:
        tok = usage.open_scope(_scope(db))
        try:
            GroqLLM(KEY, MODEL, free_tier=False).structured("sys", "prompt", Out)
        finally:
            usage.close_scope(tok)
        rec = db.execute(select(UsageRecord)).scalars().one()
    assert rec.cost_usd == pytest.approx(usage.TOKEN_PRICES[MODEL][0])


def test_tool_turn_returns_anthropic_shaped_blocks(monkeypatch, tmp_path):
    _live(monkeypatch, tmp_path)
    seen = {}

    def handler(req: httpx.Request):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json=_completion("Checking the brand first.", finish="tool_calls", tool_calls=[
            {"id": "call_1", "type": "function",
             "function": {"name": "inspect_brand", "arguments": '{"include_products": true}'}}]))

    net.set_transport(httpx.MockTransport(handler))
    tools = [{"name": "inspect_brand", "description": "Load the brand.",
              "input_schema": {"type": "object", "properties": {"include_products": {"type": "boolean"}}}}]
    resp, tokens = GroqLLM(KEY, MODEL).tool_turn("sys", [{"role": "user", "content": "Goal"}], tools)
    assert seen["tools"][0] == {"type": "function", "function": {
        "name": "inspect_brand", "description": "Load the brand.", "parameters": tools[0]["input_schema"]}}
    assert seen["tool_choice"] == "auto"
    assert resp.stop_reason == "tool_use" and tokens == 120
    assert resp.content == [{"type": "text", "text": "Checking the brand first."},
                            {"type": "tool_use", "id": "call_1", "name": "inspect_brand",
                             "input": {"include_products": True}}]


@pytest.mark.parametrize("arguments", ["{not json", "[]", '"publish everything"', "null"])
def test_malformed_tool_arguments_are_retryable(monkeypatch, tmp_path, arguments):
    _live(monkeypatch, tmp_path)
    net.set_transport(httpx.MockTransport(lambda req: httpx.Response(200, json=_completion(
        None, finish="tool_calls", tool_calls=[{"id": "c", "type": "function",
                                                "function": {"name": "x", "arguments": arguments}}]))))
    with pytest.raises(LLMError) as exc:
        GroqLLM(KEY, MODEL).tool_turn("sys", [{"role": "user", "content": "Goal"}], [])
    assert exc.value.retryable and "malformed" in str(exc.value)


@pytest.mark.parametrize("data,retryable", [
    ({"choices": [], "usage": {}}, True),
    (_completion(None), True),  # no text and no tool calls
    (_completion("", finish="content_filter"), False)])
def test_empty_or_filtered_planning_turns_never_end_the_run(monkeypatch, tmp_path, data, retryable):
    _live(monkeypatch, tmp_path)
    net.set_transport(httpx.MockTransport(lambda req: httpx.Response(200, json=data)))
    with pytest.raises(LLMError) as exc:
        GroqLLM(KEY, MODEL).tool_turn("sys", [{"role": "user", "content": "Goal"}], [])
    assert exc.value.retryable is retryable


@pytest.mark.parametrize("model,sent", [(MODEL, True), ("openai/gpt-oss-20b", True),
                                        ("llama-3.3-70b-versatile", False)])
def test_reasoning_effort_only_for_models_that_accept_it(monkeypatch, tmp_path, model, sent):
    _live(monkeypatch, tmp_path)
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json=_completion('{"name": "x"}'))

    net.set_transport(httpx.MockTransport(handler))
    GroqLLM(KEY, model).structured("sys", "prompt", Out)
    assert ("reasoning_effort" in seen) is sent


def test_429_waits_for_retry_after_then_succeeds(monkeypatch, tmp_path):
    _live(monkeypatch, tmp_path)
    waits, calls = [], []
    monkeypatch.setattr(groq_llm, "_sleep", waits.append)

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "7"}, json={"error": {
                "message": "Rate limit reached for model on tokens per minute (TPM): Limit 8000, Used 7900",
                "code": "rate_limit_exceeded"}})
        return httpx.Response(200, json=_completion('{"name": "ok"}'))

    net.set_transport(httpx.MockTransport(handler))
    out, _ = GroqLLM(KEY, MODEL).structured("sys", "prompt", Out)
    assert out.name == "ok" and waits == [7.0] and len(calls) == 2


def test_429_that_persists_is_retryable_for_the_runner(monkeypatch, tmp_path):
    _live(monkeypatch, tmp_path)
    net.set_transport(httpx.MockTransport(lambda req: httpx.Response(
        429, headers={"retry-after": "2"}, json={"error": {"message": "tokens per minute (TPM)"}})))
    with pytest.raises(LLMError) as exc:
        GroqLLM(KEY, MODEL).structured("sys", "prompt", Out)
    assert exc.value.retryable and "rate limit" in str(exc.value)


def test_daily_limit_stops_immediately(monkeypatch, tmp_path):
    _live(monkeypatch, tmp_path)
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "3"}, json={"error": {
            "message": "Rate limit reached for model on tokens per day (TPD): Limit 200000, Used 199990"}})

    net.set_transport(httpx.MockTransport(handler))
    with pytest.raises(LLMError) as exc:
        GroqLLM(KEY, MODEL).structured("sys", "prompt", Out)
    assert not exc.value.retryable and "daily" in str(exc.value) and len(calls) == 1


@pytest.mark.parametrize("status,retryable,text", [(401, False, "GROQ_API_KEY"), (404, False, "GROQ_MODEL"),
                                                   (413, False, "AGENT_PLANNER"), (408, True, "408"),
                                                   (503, True, "503")])
def test_error_statuses(monkeypatch, tmp_path, status, retryable, text):
    _live(monkeypatch, tmp_path)
    net.set_transport(httpx.MockTransport(lambda req: httpx.Response(status, json={"error": {"message": KEY}})))
    with pytest.raises(LLMError) as exc:
        GroqLLM(KEY, MODEL).structured("sys", "prompt", Out)
    assert exc.value.retryable is retryable and text in str(exc.value) and KEY not in str(exc.value)


def test_invalid_json_from_the_model_is_retryable(monkeypatch, tmp_path):
    _live(monkeypatch, tmp_path)
    net.set_transport(httpx.MockTransport(lambda req: httpx.Response(400, json={"error": {
        "message": "Failed to validate JSON", "code": "json_validate_failed"}})))
    with pytest.raises(LLMError) as exc:
        GroqLLM(KEY, MODEL).structured("sys", "prompt", Out)
    assert exc.value.retryable


def test_check_key_uses_the_free_models_endpoint(monkeypatch, tmp_path):
    _live(monkeypatch, tmp_path)
    paths = []

    def handler(req):
        paths.append((req.method, req.url.path))
        return httpx.Response(200, json={"id": MODEL, "object": "model"})

    net.set_transport(httpx.MockTransport(handler))
    ok, msg = check_key(KEY, MODEL)
    assert ok and paths == [("GET", f"/openai/v1/models/{MODEL}")] and KEY not in msg
    net.set_transport(httpx.MockTransport(lambda req: httpx.Response(401)))
    assert check_key(KEY, MODEL) == (False, "Groq rejected the API key.")


# ── configuration ───────────────────────────────────────────────────────

def test_llm_provider_groq_starts_and_auto_prefers_groq(monkeypatch, tmp_path):
    api = _live(monkeypatch, tmp_path, LLM_PROVIDER="groq", GEMINI_API_KEY="also-set")
    assert api.c.get("/api/health").status_code == 200
    s = config.get_settings()
    assert s.active_llm_provider == "groq" and s.active_llm_model == MODEL and s.uses_rules_planner
    llm = get_llm()
    assert isinstance(llm, GroqLLM) and llm.free_tier and llm.name == f"groq:{MODEL}"
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    config.get_settings.cache_clear()
    assert config.get_settings().active_llm_provider == "groq"
    cards = {c["key"]: c for c in api.get("/api/integrations").json()["ai"]}
    assert cards["groq"]["active"] and "rules-led plan" in cards["groq"]["detail"]
    assert not cards["gemini"]["active"] and KEY not in json.dumps(cards)


@pytest.mark.parametrize("env,rules", [({}, True), ({"GROQ_TIER": "developer"}, False),
                                       ({"AGENT_PLANNER": "model"}, False),
                                       ({"GROQ_TIER": "developer", "AGENT_PLANNER": "rules"}, True)])
def test_planner_choice(monkeypatch, tmp_path, env, rules):
    _live(monkeypatch, tmp_path, **env)
    assert config.get_settings().uses_rules_planner is rules
    with session_scope() as db:
        ws = db.execute(select(Workspace)).scalars().first()
        run = create_run(db, workspace_id=ws.id, user_id=1, goal="Plan a week of Instagram posts")
        assert run.planner == (f"rules:groq:{MODEL}" if rules else f"groq:{MODEL}")
        assert isinstance(Runner(db, run).planner, DemoPlanner) is rules


def test_runs_resume_on_the_provider_they_started_with(monkeypatch, tmp_path):
    _live(monkeypatch, tmp_path, GEMINI_API_KEY="gemini-key", LLM_PROVIDER="gemini", AGENT_PLANNER="model")
    with session_scope() as db:
        ws = db.execute(select(Workspace)).scalars().first()
        rid = create_run(db, workspace_id=ws.id, user_id=1, goal="Plan a week of Instagram posts").id
    # The owner switches to Groq while the Gemini run is still queued.
    monkeypatch.setenv("LLM_PROVIDER", "auto")
    config.get_settings.cache_clear()
    assert config.get_settings().active_llm_provider == "groq"
    with session_scope() as db:
        run = db.get(AgentRun, rid)
        assert run.planner.startswith("gemini:") and provider_of(run.planner) == "gemini"
        assert Runner(db, run).planner.llm.provider == "gemini"
    # If the run's provider is no longer configured, it says so instead of silently switching.
    monkeypatch.delenv("GEMINI_API_KEY")
    config.get_settings.cache_clear()
    with session_scope() as db, pytest.raises(LLMUnavailable, match="gemini"):
        Runner(db, db.get(AgentRun, rid))
    assert provider_of(f"rules:groq:{MODEL}") == "groq" and provider_of("demo-planner") is None


def test_bad_groq_settings_are_rejected(monkeypatch, tmp_path):
    for k, v in (("GROQ_TIER", "gold"), ("AGENT_PLANNER", "magic"), ("GROQ_REASONING_EFFORT", "max")):
        with pytest.raises(config.ConfigError):
            make_client(monkeypatch, tmp_path, "live", GROQ_API_KEY=KEY, **{k: v})
        monkeypatch.delenv(k)


# ── a full live campaign run on the rules-led plan ──────────────────────

def _demo_answer(body: dict) -> str:
    """Answer a structured() request the way the demo generator would, from the prompt's own data."""
    prompt = body["messages"][1]["content"]
    brief = json.loads(re.search(r"<brand_brief>\n(.*?)\n</brand_brief>", prompt, re.S).group(1))
    data = json.loads(re.search(r"<data>\n(.*?)\n</data>", prompt, re.S).group(1))
    gen = generation.DemoGenerator()
    name = body["response_format"]["json_schema"]["name"]
    if name == "HypothesesOut":
        out, _ = gen.hypotheses(brief, [], data.get("gaps", []), int(re.search(r"Propose (\d+)", prompt).group(1)))
    elif name == "CampaignPlanOut":
        out, _ = gen.campaign_plan(brief, data["selected_hypothesis"], data["params"])
    elif name == "SlotContentOut" and "failures" in data:
        out, _ = gen.revise(brief, data["post"], data["failures"], data["products"])
    elif name == "SlotContentOut":
        out, _ = gen.slot_content(brief, data["slot"], data["products"])
    elif name == "VariantOut":
        out, _ = gen.adapt(brief, data["post"], data["platform"], data["products"])
    elif name == "StrategyRevisionOut":
        out, _ = gen.strategy_revision(brief, data["findings"])
    else:
        raise AssertionError(f"unexpected schema {name}")
    return out.model_dump_json()


def test_full_live_campaign_run_on_groq_rules_plan(monkeypatch, tmp_path):
    _live(monkeypatch, tmp_path)
    with session_scope() as db:
        ws = db.execute(select(Workspace)).scalars().first()
        fixtures.seed(db, ws.id, 1, "aranya")
    requests: list[dict] = []

    def handler(req: httpx.Request):
        if req.url.host != "api.groq.com":
            return httpx.Response(404)  # no web access in this test
        body = json.loads(req.content)
        assert "tools" not in body  # the rules-led plan never sends tool definitions
        requests.append(body)
        chars = len(req.content)
        return httpx.Response(200, json=_completion(_demo_answer(body), prompt=chars // 4, completion=400))

    net.set_transport(httpx.MockTransport(handler))
    with session_scope() as db:
        run = create_run(db, workspace_id=ws.id, user_id=1, goal="Plan a week of Instagram posts")
        rid = run.id
    with session_scope() as db:
        run = execute_run(db, db.get(AgentRun, rid))
        assert run.status in ("completed", "awaiting_approval"), (
            run.completion_reason, run.outcome_summary,
            [(s.phase, s.tool, s.status, s.message[:100]) for s in run.steps if s.status == "error"])
        assert run.planner == f"rules:groq:{MODEL}" and run.cost_usd == 0
        items = db.execute(select(ContentItem).where(ContentItem.campaign_id == run.campaign_id)).scalars().all()
        assert items and {i.generated_by for i in items} == {f"groq:{MODEL}"}
        recs = db.execute(select(UsageRecord).where(UsageRecord.provider == "groq")).scalars().all()
        assert len(recs) == len(requests) and run.tokens_used >= sum(r.input_tokens + r.output_tokens for r in recs)
    # Every request fits well inside the free tier's 8,000 tokens a minute (prompt + output cap).
    biggest = max(len(json.dumps(b)) // 4 for b in requests) + groq_llm.MAX_OUTPUT_TOKENS
    print(f"groq rules-led run: {len(requests)} requests, ~{sum(len(json.dumps(b)) // 4 for b in requests)} prompt "
          f"tokens, largest request ~{biggest} tokens incl. output cap")
    assert biggest < 8000


# ── usage spent on failed attempts survives the rollback ────────────────

def _runs_requests(monkeypatch, tmp_path, answer, **env):
    _live(monkeypatch, tmp_path, **env)
    with session_scope() as db:
        ws = db.execute(select(Workspace)).scalars().first()
        fixtures.seed(db, ws.id, 1, "aranya")
    requests: list[dict] = []

    def handler(req: httpx.Request):
        if req.url.host != "api.groq.com":
            return httpx.Response(404)
        requests.append(json.loads(req.content))
        return httpx.Response(200, json=answer())

    net.set_transport(httpx.MockTransport(handler))
    with session_scope() as db:
        rid = create_run(db, workspace_id=ws.id, user_id=1, goal="Plan a week of Instagram posts").id
    with session_scope() as db:
        execute_run(db, db.get(AgentRun, rid))
    with session_scope() as db:
        run = db.get(AgentRun, rid)
        recs = db.execute(select(UsageRecord).where(UsageRecord.run_id == rid)).scalars().all()
        return run.status, run.tokens_used, recs, requests


def test_tool_attempts_that_fail_validation_keep_their_usage(monkeypatch, tmp_path):
    # Every structured answer is valid JSON but the wrong shape, so each generation attempt is rolled back.
    status, tokens, recs, requests = _runs_requests(
        monkeypatch, tmp_path, lambda: _completion('{"unexpected": true}', prompt=300, completion=50))
    assert requests and len(recs) == len(requests)
    assert all(r.provider == "groq" and r.input_tokens == 300 for r in recs)
    assert tokens >= sum(r.input_tokens + r.output_tokens for r in recs)


def test_planner_turns_that_fail_keep_their_usage(monkeypatch, tmp_path):
    bad_turn = lambda: _completion(None, finish="tool_calls", prompt=200, completion=10,  # noqa: E731
                                   tool_calls=[{"id": "c", "type": "function",
                                                "function": {"name": "inspect_brand", "arguments": "[]"}}])
    status, tokens, recs, requests = _runs_requests(monkeypatch, tmp_path, bad_turn, AGENT_PLANNER="model")
    assert status == "failed"
    assert len(requests) == len(recs) == 1 + len(runner_mod.PLANNER_RETRY_WAITS)
    assert tokens == sum(r.input_tokens + r.output_tokens for r in recs) == 210 * len(recs)
