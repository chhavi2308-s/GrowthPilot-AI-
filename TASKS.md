# Tasks

Legend: [x] done and verified locally · [~] implemented, verified only with mocks/tests · [ ] not done · [!] blocked on you

## Phase 0–1 — Environment, foundation (done earlier)
- [x] Environment, repo, config validation, demo/live separation, auth/RBAC/CSRF/audit, migrations
- [x] Git commits; pushed to the private GitHub repo on request (later work is local until you say so)

## Phase A — Inspect (2026-10-24)
- [x] Reviewed the codebase, ran tests and the app
- [x] Inspected poojaloka.com: SPA backed by a public storefront API; 2,877 products; 2,876 use stock images

## Phase B — Agentic architecture
- [x] Orchestrator + six specialist roles; delegation events; shared brand snapshot
- [x] 26 typed tools with role and side-effect tags (incl. all names from the brief)
- [x] Persistent runs: pause/resume/cancel/retry; crash recovery without redoing work
- [x] Budget gate with owner approval; usage records; search cache and caps
- [x] Non-content approval requests (budget, strategy, paid promotion, account change)

## Phase C — Poojaloka experience
- [x] Catalogue connector (storefront API) + dated snapshot with provenance, freshness, data-quality flags
- [~] Live sync/refresh from the app (mocked API in tests; snapshot fetched for real)
- [x] Editable marketing profile (pillars, languages, objectives, platform preferences, posting constraints, budget)
- [x] Pillow poster renderer (brand fonts/colours, real photos only, typographic cards, checks, PNG/JPEG)
- [~] Optional Gemini AI backgrounds (mocked)
- [x] Platform variants, regenerate one variant, re-render with variations
- [x] UI: live timeline, platform previews, gallery, calendar drag & drop, inspiration gallery, side by side,
      asset library, version compare, usage dashboard, seven-platform integrations page

## Phase D — APIs
- [~] Gemini (Interactions API) planner + generator; connection test
- [~] Brave Search (parallel, cached, capped); connection test
- [!] Real keys: GEMINI_API_KEY, BRAVE_SEARCH_API_KEY

## Phase E — Distribution and learning
- [~] Instagram + Facebook Pages live adapters (mocked)
- [!] Meta app, professional IG account, Facebook Page, tester role / App Review, public HTTPS BASE_URL
- [x] Simulated adapters for all seven platforms; specs with limits/auth/eligibility
- [x] Per-platform status, partial failures, approval hash + DB constraint
- [x] Baselines, platform-scoped A/B, confidence, approvable recommendations
- [ ] Live adapters for Pinterest, X, LinkedIn, YouTube, Threads
- [ ] Video / reels; Kannada on-image text (needs libraqm); paid promotion APIs

## Phase F — Verify and document
- [x] 96 backend tests, ruff, ESLint, tsc, next build, browser e2e (Poojaloka flow)
- [x] README, ARCHITECTURE, API_INTEGRATIONS, DEPLOYMENT, SECURITY, DEMO_SCRIPT, KNOWN_LIMITATIONS
- [~] Docker Compose / PostgreSQL (written, not run here)
- [ ] Evaluation harness vs a one-shot LLM baseline (needs an LLM key)

## Handover milestones (docs/HANDOVER.md §10)
- [~] M1 Groq provider (2026-11-01): `llm/groq_llm.py` (tools, strict structured output, 429/retry-after, daily
      limit, 413), `LLM_PROVIDER=groq`, `GROQ_*` settings, Integrations card and free connection test,
      `AGENT_PLANNER` (rules-led on the free tier). 22 tests in `tests/test_groq.py`, including a full live campaign
      run on the rules-led plan against a mocked Groq API (9 requests, ≈14k prompt tokens).
      [!] Still needs: a real run with your `GROQ_API_KEY` to confirm strict schemas are accepted and to record real
      tokens per run in `docs/API_INTEGRATIONS.md`.
- [x] Windows: text files read and written as UTF-8 (demo setup crashed on Windows before).
- [~] Research-driven 5-day campaign generation (2026-11-01): Brave research (≤5 searches per campaign) → Groq
      strategy (one call, exactly N days) → Groq captions and image prompts (one call) → one 2K Gemini image per
      day (`gemini-nano-banana-2.1`, verified in Google's docs) composited with the real product photo; per-day
      status, retries, restart recovery, budget checks, approve-all. 22 tests with mocked APIs. See
      `docs/CAMPAIGN_GENERATION.md`. [!] Needs the live smoke tests listed there (no credentials yet).
