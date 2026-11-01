from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from growthpilot import config
from growthpilot.adapters import instagram as ig_mod
from growthpilot.agent import runner as runner_mod
from growthpilot.api.deps import reset_rate_limits
from growthpilot.llm import groq_llm, set_llm_override
from growthpilot.research import net

PASSWORD = "Sup3rSecret-pass"


def _configure(monkeypatch, tmp_path, mode: str, **extra):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_MODE", mode)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / (mode + '.db')}")
    monkeypatch.setenv("ASSET_DIR", str(tmp_path / f"assets-{mode}"))
    monkeypatch.setenv("SECRET_KEY", "x" * 48)
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("RUN_WORKER_IN_PROCESS", "false")
    monkeypatch.setenv("BASE_URL", extra.pop("BASE_URL", "http://testserver"))
    for k in ("LLM_API_KEY", "ANTHROPIC_API_KEY", "SEARCH_API_KEY", "BRAVE_SEARCH_API_KEY", "GEMINI_API_KEY",
              "GROQ_API_KEY", "GROQ_MODEL", "GROQ_TIER", "AGENT_PLANNER", "LLM_PROVIDER", "IMAGE_GENERATION_PROVIDER", "IMAGE_GENERATION_API_KEY", "META_APP_ID",
              "META_APP_SECRET", "LIVE_PUBLISHING_ENABLED", "ALLOW_INSECURE_WEBHOOKS", "DEMO_ALLOW_CATALOGUE_SYNC"):
        monkeypatch.delenv(k, raising=False)
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    config.get_settings.cache_clear()
    reset_rate_limits()


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    # Never read the developer's .env: tests must not see real API keys or settings.
    monkeypatch.setitem(config.Settings.model_config, "env_file", None)
    config.get_settings.cache_clear()
    monkeypatch.setattr(runner_mod, "_sleep", lambda _s: None)
    monkeypatch.setattr(ig_mod, "_sleep", lambda _s: None)
    monkeypatch.setattr(groq_llm, "_sleep", lambda _s: None)
    yield
    set_llm_override(None)
    net.set_transport(None)
    net.set_resolver(__import__("socket").getaddrinfo)
    config.get_settings.cache_clear()


def make_client(monkeypatch, tmp_path, mode: str = "demo", **extra) -> TestClient:
    https = extra.get("BASE_URL", "").startswith("https://")
    _configure(monkeypatch, tmp_path, mode, **extra)
    from growthpilot.api.app import create_app

    # Secure cookies are only sent over https, so match the configured scheme.
    return TestClient(create_app(start_worker=False), base_url="https://testserver" if https else "http://testserver")


class Api:
    """Thin helper that adds the CSRF header automatically."""

    def __init__(self, client: TestClient):
        self.c = client
        self.csrf = ""

    def state(self) -> dict:
        s = self.c.get("/api/auth/state").json()
        self.csrf = s.get("csrf", "")
        return s

    def register(self, email="owner@example.com", name="Owner", invite="") -> dict:
        r = self.c.post("/api/auth/register", json={"name": name, "email": email, "password": PASSWORD,
                                                     "workspace_name": "Test WS", "invite": invite},
                        headers={"X-Requested-With": "GrowthPilot"})
        assert r.status_code == 200, r.text
        return self.state()

    def login(self, email, password=PASSWORD):
        r = self.c.post("/api/auth/login", json={"email": email, "password": password},
                        headers={"X-Requested-With": "GrowthPilot"})
        if r.status_code == 200:
            self.state()
        return r

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, json=None, **kw):
        return self.c.post(url, json=json, headers={"X-CSRF-Token": self.csrf}, **kw)

    def put(self, url, json=None):
        return self.c.put(url, json=json, headers={"X-CSRF-Token": self.csrf})

    def delete(self, url):
        return self.c.delete(url, headers={"X-CSRF-Token": self.csrf})


@pytest.fixture
def demo(monkeypatch, tmp_path):
    client = make_client(monkeypatch, tmp_path, "demo")
    api = Api(client)
    api.register()
    return api


@pytest.fixture
def demo_seeded(demo):
    """The fictional Aranya furniture brand (regression fixtures with fixed products)."""
    r = demo.post("/api/demo/reset", json={"confirm": "RESET", "brand": "aranya"})
    assert r.status_code == 200, r.text
    return demo


@pytest.fixture
def poojaloka_seeded(demo):
    """The primary demo: Poojaloka's real catalogue snapshot plus labelled fixtures."""
    r = demo.post("/api/demo/reset", json={"confirm": "RESET", "brand": "poojaloka"})
    assert r.status_code == 200, r.text
    return demo


def run_worker():
    from growthpilot import worker

    return worker.tick()
