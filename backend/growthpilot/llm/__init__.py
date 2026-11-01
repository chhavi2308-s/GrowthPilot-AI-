"""LLM provider interface.

Three providers are implemented behind one interface: Groq (OpenAI-compatible Chat Completions), Google Gemini
(Interactions API) and Anthropic Claude (Messages API). Demo mode uses no LLM at all. Every call goes through ``usage.preflight`` (budget check) and
``usage.record`` (cost estimate), so the orchestrator never needs provider-specific code.

The conversation format used by the agent planner is provider-neutral and Anthropic-shaped:
``{"role": "user"|"assistant", "content": str | [blocks]}`` with ``text``, ``tool_use`` and ``tool_result``
blocks. Providers translate it; provider-specific data that must be replayed verbatim (Gemini thought
signatures) is kept on the assistant message under ``raw_steps`` and stripped before other providers see it.
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from ..config import get_settings

M = TypeVar("M", bound=BaseModel)


class LLMError(RuntimeError):
    """Model call failed. Message is safe to show users."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class LLMUnavailable(LLMError):
    pass


class LLM(Protocol):
    name: str  # "<provider>:<model>"
    provider: str
    model: str

    def structured(self, system: str, prompt: str, schema: type[M]) -> tuple[M, int]:
        """Return a schema-validated object and tokens used."""

    def tool_turn(self, system: str, messages: list[dict], tools: list[dict]) -> tuple[Any, int]:
        """One agent turn with tools. Returns a response with ``content`` blocks and ``stop_reason``."""


_override: LLM | None = None


def set_llm_override(llm: LLM | None) -> None:
    """Tests inject a fake LLM here."""
    global _override
    _override = llm


# The provider part of an LLM's ``name`` (and so of a run's ``planner``) → the provider key in settings.
_NAME_PREFIXES = {"groq": "groq", "gemini": "gemini", "claude": "anthropic"}


def provider_of(planner: str | None) -> str | None:
    """The provider a run was started with, from its stored planner name (None for the demo planner)."""
    head = (planner or "").removeprefix("rules:").split(":", 1)[0]
    return _NAME_PREFIXES.get(head)


def get_llm(provider: str | None = None) -> LLM | None:
    """The configured LLM, or None in demo mode. Raises LLMUnavailable in live mode without a key.

    ``provider`` pins a specific provider (a resumed run keeps the provider it started with) instead of the one
    the current settings would pick."""
    if _override is not None:
        return _override
    s = get_settings()
    if s.is_demo:
        return None
    if provider:
        key = {"groq": s.groq_api_key, "gemini": s.gemini_api_key, "anthropic": s.llm_key}.get(provider)
        if not key:
            raise LLMUnavailable(f"This run was started with {provider}, which is no longer configured. Restore its "
                                 "API key, or start a new run.")
    else:
        provider = s.active_llm_provider
    if provider == "groq":
        from .groq_llm import GroqLLM

        return GroqLLM(api_key=s.groq_api_key, model=s.groq_model, reasoning_effort=s.groq_reasoning_effort,
                       free_tier=s.groq_tier == "free")
    if provider == "gemini":
        from .gemini_llm import GeminiLLM

        return GeminiLLM(api_key=s.gemini_api_key, model=s.gemini_model, thinking_level=s.gemini_thinking_level)
    if provider == "anthropic":
        from .anthropic_llm import AnthropicLLM

        return AnthropicLLM(api_key=s.llm_key, model=s.llm_model, effort=s.llm_effort)
    raise LLMUnavailable("No LLM configured. Set GROQ_API_KEY (or GEMINI_API_KEY, or LLM_API_KEY for Anthropic) in "
                         ".env and restart.")


def strip_provider_fields(messages: list[dict]) -> list[dict]:
    """Remove keys that only one provider understands before sending messages to another."""
    return [{k: v for k, v in m.items() if k in ("role", "content")} for m in messages]
