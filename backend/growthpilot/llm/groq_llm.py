"""Groq via its OpenAI-compatible Chat Completions API (REST, server-side).

Implemented from Groq's documentation (checked 2026-11-01):
  * Endpoint:  POST https://api.groq.com/openai/v1/chat/completions, ``Authorization: Bearer <key>``
  * Tools:     ``[{"type": "function", "function": {"name", "description", "parameters"}}]``. Assistant tool calls
               carry ``arguments`` as a JSON string; results go back as ``role: "tool"`` messages with
               ``tool_call_id``.
  * JSON:      ``response_format: {"type": "json_schema", "json_schema": {"name", "schema", "strict": true}}``.
               Strict mode needs every property in ``required`` and ``additionalProperties: false``
               (``strict_schema``); the result is still validated with Pydantic.
  * Reasoning: ``reasoning_effort`` (low/medium/high) on the gpt-oss models.
  * Limits:    per organisation. A 429 carries ``retry-after`` (seconds); a request larger than the tokens-per-minute
               limit gets 413. Free tier (reported): 30 requests/min, 8,000 tokens/min, 200,000 tokens/day.
  * Result:    ``choices[0].message`` (``content``, ``tool_calls``), ``finish_reason`` (stop | tool_calls | length),
               ``usage.prompt_tokens`` / ``completion_tokens``.

Status: implemented and tested against a mocked API; verify with a real key using the Integrations page.
"""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .. import usage
from ..research.net import client
from ..security import redact
from . import LLMError
from .gemini_llm import inline_schema

log = logging.getLogger(__name__)
M = TypeVar("M", bound=BaseModel)

API = "https://api.groq.com/openai/v1"
MAX_OUTPUT_TOKENS = 4096  # counts toward the tokens-per-minute limit, so kept modest for the free tier
MAX_WAIT_SECONDS = 60  # wait out a 429 in-process up to this long; longer means a daily or hourly limit
RATE_LIMIT_ATTEMPTS = 2
# Models that accept ``reasoning_effort``; other models reject requests that include it.
REASONING_MODEL_PREFIXES = ("openai/gpt-oss", "qwen/qwen3")

# Keywords strict mode may reject; Pydantic still enforces them on the parsed result.
_UNSUPPORTED = {"default", "format", "pattern", "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum",
                "exclusiveMaximum", "multipleOf", "minItems", "maxItems", "uniqueItems", "examples"}

_sleep = time.sleep  # tests replace this


def strict_schema(schema: dict) -> dict:
    """A Pydantic JSON schema made acceptable to strict structured outputs: refs inlined, every property
    required, no additional properties, unsupported keywords removed."""

    def walk(node: Any, field_names: bool = False) -> Any:
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        if field_names:
            return {k: walk(v) for k, v in node.items()}
        out = {k: walk(v, field_names=k == "properties") for k, v in node.items() if k not in _UNSUPPORTED}
        if out.get("type") == "object" or "properties" in out:
            props = out.get("properties", {})
            out["required"] = list(props)
            out["additionalProperties"] = False
        return out

    return walk(inline_schema(schema))


def _text(blocks: Any) -> str:
    if isinstance(blocks, str):
        return blocks
    return "".join(b.get("text", "") for b in blocks or [] if isinstance(b, dict) and b.get("type") == "text")


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """Translate the planner's Anthropic-shaped history into Chat Completions messages."""
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        content = m.get("content")
        if m.get("role") == "assistant":
            msg: dict = {"role": "assistant", "content": _text(content) or None}
            calls = [{"id": b["id"], "type": "function",
                      "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                     for b in content or [] if isinstance(b, dict) and b.get("type") == "tool_use"]
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        if isinstance(content, str):
            out.append({"role": "user", "content": content})
            continue
        texts = []
        for b in content or []:
            if b.get("type") == "tool_result":
                body = b.get("content", "")
                body = _text(body) if isinstance(body, list) else str(body)
                out.append({"role": "tool", "tool_call_id": b.get("tool_use_id", ""),
                            "content": ("ERROR: " + body) if b.get("is_error") else body})
            elif b.get("type") == "text":
                texts.append(b.get("text", ""))
        if texts:  # after the tool results: every tool call must be answered before the next user turn
            out.append({"role": "user", "content": "\n\n".join(texts)})
    return out


class GroqLLM:
    provider = "groq"

    def __init__(self, api_key: str, model: str, reasoning_effort: str = "low", free_tier: bool = True):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.free_tier = free_tier
        self.name = f"groq:{model}"
        self._key = api_key

    # ── HTTP ─────────────────────────────────────────────────────────────
    def _post(self, body: dict, what: str) -> dict:
        for attempt in range(1, RATE_LIMIT_ATTEMPTS + 1):
            try:
                with client(timeout=httpx.Timeout(120.0, connect=10.0),
                            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}) as c:
                    resp = c.post(f"{API}/chat/completions", json=body)
            except httpx.TimeoutException as exc:
                raise LLMError("The Groq call timed out.", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise LLMError(f"Could not reach the Groq API ({type(exc).__name__}).", retryable=True) from exc
            if resp.status_code != 429:
                break
            msg, wait = self._error_message(resp), _retry_after(resp)
            if "per day" in msg.lower() or "(rpd)" in msg.lower() or "(tpd)" in msg.lower() or wait > MAX_WAIT_SECONDS:
                raise LLMError(f"Groq's daily or hourly limit is used up. {msg} Try again later, or move the "
                               "organisation to the Developer tier.".replace("  ", " "))
            if attempt == RATE_LIMIT_ATTEMPTS:
                raise LLMError("Groq rate limit reached." + (f" {msg}" if msg else ""), retryable=True)
            log.info("groq 429; waiting %.0fs (attempt %d)", wait, attempt)
            _sleep(wait or 5)
        if resp.status_code in (401, 403):
            raise LLMError("Groq rejected the API key (check GROQ_API_KEY).")
        if resp.status_code == 404:
            raise LLMError(f"Groq model {self.model!r} was not found. Check GROQ_MODEL.")
        if resp.status_code == 413:
            raise LLMError("The request is larger than Groq's tokens-per-minute limit for this organisation. Use "
                           "AGENT_PLANNER=rules on the free tier, or the Developer tier.")
        if resp.status_code >= 500 or resp.status_code == 408:
            raise LLMError(f"Groq API error (HTTP {resp.status_code}).", retryable=True)
        if resp.status_code >= 400:
            msg = self._error_message(resp)
            # The model produced JSON that didn't match the schema or a malformed tool call: worth another try.
            retry = any(code in msg for code in ("json_validate_failed", "tool_use_failed"))
            raise LLMError(f"Groq rejected the {what} request: {msg}", retryable=retry)
        return resp.json()

    def _error_message(self, resp: httpx.Response) -> str:
        try:
            err = resp.json().get("error", {}) or {}
            msg = f"{err.get('message', '')} {err.get('code') or ''}".strip()
        except ValueError:
            msg = resp.text[:200]
        msg = redact(str(msg))
        return (msg.replace(self._key, "[redacted]") if self._key else msg)[:300]

    def _account(self, data: dict, operation: str) -> int:
        u = data.get("usage") or {}
        i, o = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
        usage.record(provider="groq", operation=operation, model=self.model, input_tokens=i, output_tokens=o,
                     cost_usd=0.0 if self.free_tier else None)
        return i + o

    def _preflight(self, chars: int, what: str) -> None:
        if not self.free_tier:
            usage.preflight(usage.estimate_llm_call(self.model, chars, MAX_OUTPUT_TOKENS), what)

    def _body(self, messages: list[dict]) -> dict:
        body = {"model": self.model, "messages": messages, "max_completion_tokens": MAX_OUTPUT_TOKENS}
        if self.model.startswith(REASONING_MODEL_PREFIXES):
            body["reasoning_effort"] = self.reasoning_effort
        return body

    @staticmethod
    def _choice(data: dict) -> dict:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("Groq returned no answer.", retryable=True)
        finish = choices[0].get("finish_reason")
        if finish == "length":
            raise LLMError("The Groq response was cut off (output limit). Try a smaller request.")
        if finish == "content_filter":
            raise LLMError("Groq's content filter stopped the response.")
        return choices[0].get("message") or {}

    # ── structured output ────────────────────────────────────────────────
    def structured(self, system: str, prompt: str, schema: type[M]) -> tuple[M, int]:
        self._preflight(len(system) + len(prompt), f"A Groq call ({schema.__name__})")
        body = self._body([{"role": "system", "content": system}, {"role": "user", "content": prompt}]) | {
            "response_format": {"type": "json_schema", "json_schema": {
                "name": schema.__name__[:64], "schema": strict_schema(schema.model_json_schema()), "strict": True}}}
        data = self._post(body, "structured output")
        tokens = self._account(data, f"structured:{schema.__name__}")
        text = self._choice(data).get("content") or ""
        try:
            return schema.model_validate_json(text), tokens
        except ValidationError as exc:
            log.info("groq structured output failed validation: %s", exc.errors()[:3])
            raise LLMError("The model returned output that didn't match the expected schema.", retryable=True) from exc

    # ── tool use ─────────────────────────────────────────────────────────
    def tool_turn(self, system: str, messages: list[dict], tools: list[dict]) -> tuple[Any, int]:
        decls = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                   "parameters": inline_schema(t["input_schema"])}} for t in tools]
        msgs = to_openai_messages(system, messages)
        self._preflight(len(json.dumps(msgs)) + len(json.dumps(decls)), "A Groq planning turn")
        data = self._post(self._body(msgs) | {"tools": decls, "tool_choice": "auto"}, "planning")
        tokens = self._account(data, "tool_turn")
        msg = self._choice(data)
        content: list[dict] = []
        if (msg.get("content") or "").strip():
            content.append({"type": "text", "text": msg["content"]})
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                raise LLMError("The model sent malformed tool arguments.", retryable=True) from exc
            if not isinstance(args, dict):  # never run a tool on defaults the model didn't choose
                raise LLMError("The model sent malformed tool arguments.", retryable=True)
            content.append({"type": "tool_use", "id": str(call.get("id") or ""), "name": fn.get("name", ""),
                            "input": args})
        if not content:  # a refusal or empty answer must not end the run as if the work were done
            raise LLMError("Groq returned an empty planning turn.", retryable=True)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        log.info("groq tool_turn model=%s calls=%d tokens=%d", self.model,
                 sum(1 for b in content if b["type"] == "tool_use"), tokens)
        return SimpleNamespace(content=content, stop_reason=stop), tokens


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(0.0, float(resp.headers.get("retry-after", "0")))
    except ValueError:
        return 0.0


def check_key(api_key: str, model: str) -> tuple[bool, str]:
    """Free connection test: look the model up (models endpoint; no tokens used)."""
    try:
        with client(headers={"Authorization": f"Bearer {api_key}"}) as c:
            resp = c.get(f"{API}/models/{model}")
    except httpx.HTTPError as exc:
        return False, f"Could not reach the Groq API ({type(exc).__name__})."
    if resp.status_code in (401, 403):
        return False, "Groq rejected the API key."
    if resp.status_code == 404:
        return False, f"Model {model} is not available to this key."
    if resp.status_code >= 400:
        return False, f"Groq returned HTTP {resp.status_code}."
    return True, f"Key works and {model} is available."
