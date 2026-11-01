"""Application settings and startup validation.

Two data modes, kept strictly apart:

* ``demo`` — deterministic demo planner/generator, labelled fixtures, mock Instagram adapter,
  synthetic metrics. No external calls are made. Uses its own database (data/demo.db).
* ``live`` — real LLM, real research providers, real platform adapters, real data only.
  Uses its own database. Publishing additionally needs LIVE_PUBLISHING_ENABLED=true plus
  per-item owner approval and explicit publish authorisation.

``APP_ENV`` (development | test | production) controls operational strictness, independent of mode.
"""

from __future__ import annotations

import json
import secrets
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_ROOT.parent
DATA_DIR = PROJECT_ROOT / "data"


class AppMode(StrEnum):
    DEMO = "demo"
    LIVE = "live"


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class ConfigError(RuntimeError):
    """Settings are unsafe or incomplete for the selected mode. Messages never include secret values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Always load the repository-root file, independent of the caller's working directory.
        # Pydantic Settings still gives process environment variables higher precedence.
        env_file=str(PROJECT_ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_mode: AppMode = AppMode.DEMO
    app_env: AppEnv = AppEnv.DEVELOPMENT
    database_url: str = ""
    redis_url: str = ""  # optional; the job queue is database-backed
    secret_key: str = ""
    encryption_key: str = ""
    # Public URL users reach the app at (frontend origin). OAuth redirects and asset links derive from it.
    base_url: str = "http://localhost:3000"
    asset_dir: str = ""

    # LLM. Demo mode always uses the deterministic planner and generator.
    # auto = Groq if GROQ_API_KEY is set, else Gemini if GEMINI_API_KEY is set, else Anthropic if
    # LLM_API_KEY/ANTHROPIC_API_KEY is set.
    llm_provider: str = "auto"  # auto | groq | gemini | anthropic
    llm_api_key: str = ""  # Anthropic key (kept for compatibility)
    anthropic_api_key: str = ""  # accepted as an alias for LLM_API_KEY
    llm_model: str = "claude-opus-5-5"  # Anthropic model
    llm_effort: str = "medium"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    planner_fallback_model: str = ""
    gemini_thinking_level: str = "medium"  # minimal | low | medium | high (per-model support varies)
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    groq_tier: str = "free"  # free | developer (free records $0 cost but still counts tokens)
    groq_reasoning_effort: str = "low"  # low | medium | high
    # Live-mode planning. model = the LLM chooses tools each turn; rules = the fixed step plan, with the LLM only
    # writing text (about ten times fewer tokens); auto = rules on Groq's free tier, model otherwise.
    agent_planner: str = "auto"  # auto | model | rules

    # Research
    search_provider: str = "brave"  # brave | none
    brave_search_api_key: str = ""
    search_api_key: str = ""  # accepted as an alias for BRAVE_SEARCH_API_KEY
    search_cache_hours: int = Field(default=24, ge=0, le=720)

    # Image generation for optional AI backgrounds (product imagery always comes from real photos).
    image_generation_provider: str = "none"  # none | gemini
    image_generation_model: str = "gemini-3.1-flash-lite-image"
    image_generation_api_key: str = ""  # defaults to GEMINI_API_KEY when the provider is gemini

    # Campaign generation (research → strategy → captions → one 2K image per day). Images need
    # IMAGE_GENERATION_PROVIDER=gemini; otherwise the local poster renderer is used and the UI says so.
    campaign_image_model: str = "gemini-nano-banana-2.1"  # "Nano Banana 2.1"; verified 2026-11-01
    campaign_image_size: str = "2K"  # 1K | 2K | 4K (uppercase K, as the Gemini API requires)
    campaign_max_searches: int = Field(default=5, ge=0, le=20)  # Brave requests per campaign
    campaign_image_attempts: int = Field(default=2, ge=1, le=5)  # automatic tries per day before it fails
    campaign_image_concurrency: int = Field(default=1, ge=1, le=4)  # image requests in flight at once
    campaign_budget_usd: float = Field(default=1.00, ge=0)  # per generated campaign; checked before every call
    image_timeout_seconds: int = Field(default=120, ge=10, le=600)
    search_timeout_seconds: int = Field(default=15, ge=3, le=120)
    # Brave's terms allow keeping API results only on plans with storage rights. Without them, search snippets are
    # used for the strategy and then dropped; only titles, URLs and the agent's own findings are kept.
    brave_storage_rights: bool = False

    # Canva MCP poster integration (separate from social publishing).
    canva_mcp_url: str = "https://api.canva.com/connect/v1/mcp"
    canva_client_id: str = ""
    canva_client_secret: str = ""
    canva_redirect_uri: str = ""
    canva_mcp_daily_cap: int = Field(default=10, ge=1, le=1000)

    @field_validator("canva_mcp_url")
    @classmethod
    def _canva_mcp_https(cls, value: str) -> str:
        from urllib.parse import urlparse
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("CANVA_MCP_URL must be an HTTPS URL without embedded credentials.")
        return value.rstrip("/")

    # Catalogue ingestion (Poojaloka storefront API). Demo mode uses the bundled snapshot unless allowed.
    poojaloka_api_base: str = "https://api.poojaloka.com/api"
    demo_allow_catalogue_sync: bool = False
    catalogue_fresh_hours: int = Field(default=24, ge=1, le=24 * 30)

    # Instagram API with Instagram Login (separate Instagram product credentials)
    instagram_app_id: str = ""
    instagram_app_secret: str = ""
    # Meta Unified Login for newer Instagram API apps, and Facebook Pages.
    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_login_config_id: str = ""
    meta_redirect_uri: str = ""
    instagram_api_version: str = "v26.0"

    # Facebook Pages
    facebook_redirect_uri: str = ""
    facebook_api_version: str = "v26.0"

    # Safety switches
    live_publishing_enabled: bool = False
    allow_insecure_webhooks: bool = False

    # Worker
    run_worker_in_process: bool = True
    worker_interval_seconds: int = Field(default=10, ge=1, le=3600)

    session_ttl_hours: int = 12

    # ── derived ──────────────────────────────────────────────────────────
    @property
    def is_demo(self) -> bool:
        return self.app_mode == AppMode.DEMO

    @property
    def is_live(self) -> bool:
        return self.app_mode == AppMode.LIVE

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")

    @property
    def llm_key(self) -> str:
        return self.llm_api_key or self.anthropic_api_key

    @property
    def active_llm_provider(self) -> str | None:
        """Which LLM provider live mode will use, or None if none is configured."""
        if self.llm_provider == "groq":
            return "groq" if self.groq_api_key else None
        if self.llm_provider == "gemini":
            return "gemini" if self.gemini_api_key else None
        if self.llm_provider == "anthropic":
            return "anthropic" if self.llm_key else None
        if self.groq_api_key:
            return "groq"
        if self.gemini_api_key:
            return "gemini"
        return "anthropic" if self.llm_key else None

    @property
    def llm_configured(self) -> bool:
        return self.active_llm_provider is not None

    @property
    def active_llm_model(self) -> str:
        return {"groq": self.groq_model, "gemini": self.gemini_model}.get(self.active_llm_provider or "",
                                                                         self.llm_model)

    @property
    def uses_rules_planner(self) -> bool:
        """Live runs follow the fixed step plan (the LLM writes text only) instead of model-chosen tools."""
        if self.agent_planner == "auto":
            return self.active_llm_provider == "groq" and self.groq_tier == "free"
        return self.agent_planner == "rules"

    @property
    def brave_key(self) -> str:
        return self.brave_search_api_key or self.search_api_key

    @property
    def search_configured(self) -> bool:
        return self.search_provider == "brave" and bool(self.brave_key)

    @property
    def image_key(self) -> str:
        return self.image_generation_api_key or (self.gemini_api_key if self.image_generation_provider == "gemini"
                                                 else "")

    @property
    def image_generation_configured(self) -> bool:
        return self.image_generation_provider == "gemini" and bool(self.image_key)

    @property
    def canva_callback_uri(self) -> str:
        # Canva compares redirect_uri byte-for-byte with the registered value.
        # Require operators to configure that exact URI instead of deriving one.
        return self.canva_redirect_uri

    @property
    def facebook_redirect(self) -> str:
        return self.facebook_redirect_uri or f"{self.base_url.rstrip('/')}/api/integrations/facebook/callback"

    @property
    def instagram_configured(self) -> bool:
        return self.instagram_login_configured or self.instagram_unified_configured

    @property
    def instagram_login_configured(self) -> bool:
        return bool(self.instagram_app_id and self.instagram_app_secret)

    @property
    def instagram_unified_configured(self) -> bool:
        return bool(self.meta_app_id and self.meta_app_secret and self.meta_login_config_id)

    @property
    def instagram_redirect_uri(self) -> str:
        return self.meta_redirect_uri or f"{self.base_url.rstrip('/')}/api/integrations/instagram/callback"

    @property
    def asset_path(self) -> Path:
        return Path(self.asset_dir) if self.asset_dir else DATA_DIR / f"assets-{self.app_mode.value}"

    def problems(self) -> list[dict]:
        """Non-fatal configuration gaps, for the health/integrations page. Never includes values."""
        out = []
        if self.is_live:
            if not self.llm_configured:
                out.append({"key": "GROQ_API_KEY (or GEMINI_API_KEY / LLM_API_KEY)",
                            "impact": "Agent runs and content generation are unavailable."})
            if not self.search_configured:
                out.append({"key": "BRAVE_SEARCH_API_KEY", "impact": "Web research is unavailable; agent will say so."})
            if not self.instagram_configured:
                out.append({"key": "INSTAGRAM_APP_ID / INSTAGRAM_APP_SECRET or META_APP_ID / "
                                  "META_APP_SECRET / META_LOGIN_CONFIG_ID",
                            "impact": "Instagram can't be connected."})
            if not (self.meta_app_id and self.meta_app_secret):
                out.append({"key": "META_APP_ID / META_APP_SECRET",
                            "impact": "Facebook Pages can't be connected."})
            if not self.secure_cookies:
                out.append({"key": "BASE_URL", "impact": "Not HTTPS: cookies aren't marked Secure and Instagram "
                            "can't fetch images for publishing."})
        return out

    @model_validator(mode="after")
    def _fill_and_check(self) -> Settings:
        if self.llm_effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ConfigError("LLM_EFFORT must be one of low, medium, high, xhigh, max")
        if self.llm_provider not in {"auto", "groq", "anthropic", "gemini"}:
            raise ConfigError("LLM_PROVIDER must be auto, groq, gemini or anthropic.")
        if self.groq_tier not in {"free", "developer"}:
            raise ConfigError("GROQ_TIER must be free or developer.")
        if self.groq_reasoning_effort not in {"low", "medium", "high"}:
            raise ConfigError("GROQ_REASONING_EFFORT must be low, medium or high.")
        if self.agent_planner not in {"auto", "model", "rules"}:
            raise ConfigError("AGENT_PLANNER must be auto, model or rules.")
        if self.gemini_thinking_level not in {"minimal", "low", "medium", "high"}:
            raise ConfigError("GEMINI_THINKING_LEVEL must be minimal, low, medium or high.")
        if self.search_provider not in {"brave", "none"}:
            raise ConfigError("SEARCH_PROVIDER must be 'brave' or 'none'.")
        if self.image_generation_provider not in {"none", "gemini"}:
            raise ConfigError("IMAGE_GENERATION_PROVIDER must be none or gemini.")
        if self.campaign_image_size not in {"1K", "2K", "4K"}:
            raise ConfigError("CAMPAIGN_IMAGE_SIZE must be 1K, 2K or 4K (uppercase K).")

        if not self.database_url:
            name = "test.db" if self.app_env == AppEnv.TEST else f"{self.app_mode.value}.db"
            self.database_url = f"sqlite:///{DATA_DIR / name}"

        if self.is_live:
            if len(self.secret_key) < 32:
                raise ConfigError("Live mode requires SECRET_KEY (32+ chars). Run `make env` to generate one.")
            if not self.encryption_key:
                raise ConfigError("Live mode requires ENCRYPTION_KEY. Run `make env` to generate one.")
            if "demo" in self.database_url.rsplit("/", 1)[-1]:
                raise ConfigError("Live mode cannot use a demo database. Point DATABASE_URL at a live database.")
        else:
            self.live_publishing_enabled = False  # demo never publishes anywhere
            if not self.secret_key or not self.encryption_key:
                keys = _load_or_create_demo_keys()
                self.secret_key = self.secret_key or keys["secret_key"]
                self.encryption_key = self.encryption_key or keys["encryption_key"]

        try:
            Fernet(self.encryption_key.encode())
        except Exception as exc:  # noqa: BLE001
            raise ConfigError("ENCRYPTION_KEY is not a valid Fernet key (run `make env`).") from exc
        return self


def _load_or_create_demo_keys() -> dict[str, str]:
    """Demo-only keys under data/ (gitignored) so sessions survive restarts."""
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / "demo-secrets.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    keys = {"secret_key": secrets.token_urlsafe(48), "encryption_key": Fernet.generate_key().decode()}
    path.write_text(json.dumps(keys), encoding="utf-8")
    path.chmod(0o600)
    return keys


@lru_cache
def get_settings() -> Settings:
    return Settings()
