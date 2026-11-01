# API integrations

Status key: **Verified** = exercised against the real service. **Implemented** = built from current official
docs and tested against a mocked API, not yet run with real credentials. **Simulated** = demo adapter, labelled.

| Integration | Mode | Status | Needs from you | Cost |
|---|---|---|---|---|
| Google Gemini (LLM) | live | Implemented | `GEMINI_API_KEY` | per token (free tier on some models) |
| Anthropic Claude (LLM, optional) | live | Implemented | `LLM_API_KEY` | per token |
| Groq (LLM, main) | live | Implemented | `GROQ_API_KEY` | free tier (rate-limited), or per token on the Developer tier |
| Brave Search | live | Implemented | `BRAVE_SEARCH_API_KEY` | $5 / 1,000 requests after a monthly credit |
| Poojaloka storefront API (catalogue) | live (demo: snapshot) | Implemented; snapshot fetched for real on 2026-10-24 | nothing | free |
| Public page fetch (`inspect_website`) | live | Implemented, mocked pages | permitted domains in Brand Brain | free |
| Gemini image (AI backgrounds) | live, optional | Implemented | `IMAGE_GENERATION_PROVIDER=gemini` | ≈ $0.03 per image |
| Gemini image, campaign creatives (`gemini-nano-banana-2.1`, 2K, google-genai SDK) | live, optional | Implemented; mocked tests only | `IMAGE_GENERATION_PROVIDER=gemini`, `GEMINI_API_KEY` | $0.0504 per 2K image, no free tier (see `docs/CAMPAIGN_GENERATION.md`) |
| Instagram (Instagram Login) | live | Implemented, mocked Meta API | Meta app, professional account, public HTTPS | free API |
| Facebook Pages | live | Implemented, mocked Graph API | same Meta app + Facebook Login for Business, a Page | free API |
| Canva | — | Not integrated; no Canva API calls are made | — | — |
| Pinterest, X, LinkedIn, YouTube, Threads | demo | Specs + simulated adapters only | see table below | — |
| Signed webhook (Zapier / Make / n8n) | live | Implemented, mock receiver | a webhook URL | — |

Owners can check keys from **Integrations → Test connection**; each test is one small request and results never
show the key. Prices are from the providers' pages on 2026-10-24 and are used only for estimates
(**Usage & Budgets** shows the table); your provider's bill is authoritative.

### Checks completed for this repository review (2026-10-31)

The checked-in implementation and environment template were inspected without reading or displaying local secret
values. No live provider connection checks were run in this review, so no account or credential is marked verified.
Canva is unavailable in the current application. Canva mentions in the creative brief schema describe a
design brief/export target only; they do not invoke Canva or establish a Canva integration. The available LLM
adapters are Groq, Gemini and Anthropic. Their connection checks are owner-only, live-mode requests and may consume
provider quota; run them explicitly from Integrations when that cost is acceptable.

Current official docs were checked on 2026-10-31: Meta's [Instagram API collection](https://www.postman.com/meta/instagram/documentation/6yqw8pt/instagram-api)
describes professional (Business or Creator) accounts and publishing endpoints; Meta's [Facebook API collection](https://www.postman.com/meta/facebook/documentation/r56bjfd/facebook-api)
documents Page access tokens. The code uses separate Instagram Login and Facebook Login flows, and the Facebook
adapter only accepts Pages where the returned tasks include `CREATE_CONTENT`. Meta still controls the app roles,
access level, permissions and review needed for a particular account; this repository has not verified those for
your app. The `.env` App ID/Secret presence alone does not connect an account.

Groq's [OpenAI-compatible API](https://console.groq.com/docs/openai) supports chat completions and tool calling, but
the current code has no Groq adapter, structured-output compatibility layer, quota handling, or tested model mapping.
It is therefore not selected even if a Groq key is present. Canva's [Connect API](https://www.canva.dev/docs/connect/)
can create/edit/export designs with OAuth and scopes, but this project has no Canva OAuth or API client. The local
poster renderer remains the implemented poster path; the agent's Canva query is a design brief, not a Canva action.

---

## Groq (main LLM)

`backend/growthpilot/llm/groq_llm.py`, built from Groq's docs (checked 2026-11-01) and tested against a mocked API
(`tests/test_groq.py`). **Not yet run with a real key.**

- **API:** OpenAI-compatible Chat Completions at `https://api.groq.com/openai/v1`, Bearer key. Default model
  `openai/gpt-oss-120b` (`GROQ_MODEL`), `reasoning_effort` from `GROQ_REASONING_EFFORT` (default `low`).
- **Tools:** the planner's Anthropic-shaped history is translated to `tool_calls` / `role: "tool"` messages; tool
  calls come back as `tool_use` blocks. Malformed tool arguments are a retryable error.
- **Structured output:** `json_schema` with `strict: true`. `strict_schema()` inlines refs, makes every property
  required, sets `additionalProperties: false` and drops keywords strict mode may reject; Pydantic still validates.
- **Errors:** 401/403 bad key and 404 unknown model are final. 429 waits `retry-after` once in-process (up to 60 s),
  then hands a retryable error to the runner (waits 10/20/40/60 s). A daily limit (TPD/RPD, or a wait over 60 s)
  stops immediately. 413 means a single request exceeds the tokens-per-minute limit.
- **Usage:** every call is recorded with its tokens. `GROQ_TIER=free` records $0; `developer` uses the prices in
  `usage.TOKEN_PRICES` (gpt-oss-120b $0.15 / $0.60 per million input / output tokens; confirm on groq.com/pricing).
- **Connection test:** Integrations → Groq → Test calls `GET /models/{model}`, which uses no tokens.
- **Selection:** `LLM_PROVIDER=groq`, or `auto` picks Groq whenever `GROQ_API_KEY` is set.

**Fitting the free tier (reported: 30 requests/min, 8,000 tokens/min, 200,000 tokens/day, per organisation).**
`AGENT_PLANNER=auto` runs Groq's free tier on the **rules-led plan**: the fixed step plan from `planner_demo.py`,
with Groq only writing text through `structured()`. No tool definitions are sent. The run's planner shows as
`rules:groq:<model>`. `AGENT_PLANNER=model` (or `GROQ_TIER=developer`) uses model-chosen tools instead, which sends
all 26 tool definitions (about 5,000 tokens) every turn and does not fit 8,000 tokens a minute.

Tokens per run, measured from request sizes in the mocked "plan a week of Instagram posts" run
(`test_full_live_campaign_run_on_groq_rules_plan`, 2026-11-01):

| Plan | Requests | Prompt tokens (≈ chars / 4) | Largest request (prompt + 4,096 output cap) |
|---|---|---|---|
| Rules-led | 9 | ≈ 14,200 | ≈ 6,600 |

Output and reasoning tokens depend on the real model and aren't in this estimate. **Replace this table with real
numbers from the first live run** (the run page shows `tokens_used`; usage records hold the per-call split).

## Google Gemini (LLM)

Used by the orchestrator (tool use) and by the Strategy, Creative, Distribution and Analyst roles (structured
outputs). Implemented with the **Interactions API** over REST, server-side (`backend/growthpilot/llm/gemini_llm.py`),
from Google's docs checked 2026-10-24:

* `POST https://generativelanguage.googleapis.com/v1beta/interactions`, header `x-goog-api-key`.
* Stateless: `store: false`, the full history in `input` as steps — `user_input`, the model's own steps **exactly as
  received** (including `thought` steps that carry signatures, which Gemini 3+ models require on later turns) and
  `function_result` steps with `call_id`. GrowthPilot stores the raw steps on each assistant turn and replays them.
* Tools: `{"type": "function", "name", "description", "parameters"}`; Pydantic `$ref`s are inlined first.
* Structured output: `response_format: {"type": "text", "mime_type": "application/json", "schema": …}`; the text is
  validated with the Pydantic model (schema mismatch → retryable error).
* `generation_config.thinking_level` (`GEMINI_THINKING_LEVEL`, default medium); usage from
  `usage.total_input_tokens / total_output_tokens / total_thought_tokens` is recorded per call.
* Errors: 401/403 → key rejected; 404 → model not found; 429 and 5xx → retryable; `status: incomplete` → cut off;
  messages are redacted and never include the key.
* Connection test: `GET /v1beta/models/{model}` (free).

Setup: create a key at https://aistudio.google.com/apikey, set `GEMINI_API_KEY`, optionally `GEMINI_MODEL` (default
`gemini-3.8-flash`, listed as stable on 2026-10-24; Flash prices rise on 2027-01-01). `LLM_PROVIDER=auto` picks
Gemini when its key is present. A Gemini app subscription doesn't include API credits.

## Anthropic Claude (optional)

`LLM_PROVIDER=anthropic`, `LLM_API_KEY`, `LLM_MODEL` (default `claude-opus-5-5`), `LLM_EFFORT`. Official
`anthropic` SDK: `beta.messages.parse` with Pydantic `output_format`, `beta.messages.create` with typed tools,
prompt caching, and the server-side refusal fallback. Provider-specific fields (Gemini raw steps) are stripped
before Claude sees a history, so a run is bound to one provider.

## Brave Search

Used by `search_web` and `analyse_trends` (several queries run in parallel; only the HTTP requests are concurrent).
`GET https://api.search.brave.com/res/v1/web/search`, header `X-Subscription-Token`, params `q`, `count`,
`country`, `freshness`. Each result becomes a `source_reported` finding with URL, title, snippet and retrieval time;
injection-like phrasing is flagged. Cost controls: database cache (`SEARCH_CACHE_HOURS`, default 24 h), caps per run
and per day (Usage & Budgets), and a budget pre-check before each request. Set `BRAVE_SEARCH_API_KEY`
(`SEARCH_API_KEY` is accepted as an alias).

## Poojaloka catalogue (storefront API)

poojaloka.com is a single-page app that reads its catalogue from a public JSON API — the same requests any
visitor's browser makes, with no login. GrowthPilot uses only the public read endpoints (checked 2026-10-24):
`GET /api/products?page=&pageSize=100`, `GET /api/products/{slug}`, `/api/categories`, `/api/festivals`,
`/api/hero`, `/api/articles`. It never calls admin, cart, order or account endpoints. `api.poojaloka.com` has no
robots.txt (404); requests use an identified User-Agent, HTTPS, SSRF checks and a pause between pages.

What's stored per product: name (and Kannada name), category, price, MRP, stock, back-order flag, variants, image
URLs, the storefront URL (`https://poojaloka.com/product/<slug>`), the source id and the retrieval time. Materials
are recorded only when the product's own name (or category) states them. Data-quality flags are computed, never
"fixed": suspicious price (≤ ₹1), negative stock, out of stock, MRP above price (discount wording still needs an
approved claim), generic template description, delivery conflict, duplicate name, possible test listing, and
**no product photo**.

**Image audit (2026-10-24):** 2,876 of 2,877 products show generic stock images from cdn.pixabay.com; one shows an
upload on billing.poojaloka.com (a test listing). Stock images are labelled `stock_photo`, never downloaded as
product photos and never shown as the product — posters use typographic product cards until the store uploads real
photos (Asset Library → Upload, or the store's own uploads are downloaded automatically on sync).

Live mode: **Brand Brain → Catalogue → Sync now** or `python -m growthpilot sync-catalogue [--full]`.
`verify_product_facts` re-checks the selected products at the source before a campaign and flags changes; live mode
refuses to publish a price older than `CATALOGUE_FRESH_HOURS` (default 24). Demo mode uses the bundled dated
snapshot (`backend/growthpilot/demo/poojaloka_snapshot.json`, 40 products) and makes no requests unless
`DEMO_ALLOW_CATALOGUE_SYNC=true`.

## Public page fetch (`inspect_website`)

No key. Only https, only the brand's own site, competitor sites and the domains in **Brand Brain → Claims &
sources → Permitted research sources**. robots.txt is honoured. Private, loopback, link-local and metadata
addresses are blocked, and redirects are re-validated hop by hop. Responses are limited to 1 MB, and login walls or
paywalls (401/402/403) are reported, not bypassed. Page text is stored as data and wrapped as
`<untrusted_content>` before a model sees it.

## Instagram (Meta) — Instagram API with Instagram Login

Verified against Meta's docs on 2026-10-24
(https://developers.facebook.com/docs/instagram-platform/content-publishing,
…/instagram-api-with-instagram-login/business-login, …/reference/instagram-media/insights).

### What you need to do

1. **Instagram account:** switch the brand account to a **professional account (Business or Creator)** in the
   Instagram app (Settings → Account type and tools). Personal accounts can't publish via the API; GrowthPilot
   checks `account_type` and refuses them.
2. **Meta app:** at https://developers.facebook.com/apps create an app and add the **Instagram** product, choosing
   *API setup with Instagram login*.
3. **Redirect URI:** register exactly `{BASE_URL}/api/integrations/instagram/callback` (shown on the Integrations
   page). `BASE_URL` is the URL people open the app at, e.g. `https://growthpilot.example.com`.
4. **Credentials:** copy the Instagram App ID and App Secret into `.env` as `META_APP_ID` and `META_APP_SECRET`.
5. **Testing before App Review:** while the app is in development mode, only accounts with a role on the app can
   authorise it. Add the brand's Instagram account as a tester under App roles and accept the invite from that
   Instagram account. Using it with accounts that have no role on the app requires Meta App Review for the
   permissions below, and may require business verification. Check Meta's current requirements.
6. **Public HTTPS for images:** Meta downloads post images from a public URL when you publish. Set `BASE_URL` to
   a public HTTPS address. For a demo, a tunnel works, for example `cloudflared tunnel --url http://localhost:3000`
   or `ngrok http 3000`; use that URL as `BASE_URL` and as the redirect URI. Images are served from unguessable
   `/api/public/assets/<token>.jpg` URLs (JPEG/PNG uploads only).
7. **Photos:** upload real product photos as **JPEG** (Instagram publishing accepts JPEG only; feed aspect ratio
   4:5 to 1.91:1). Placeholders are blocked from live publishing.
8. **Turn on publishing deliberately:** `LIVE_PUBLISHING_ENABLED=true`. Even then, each post needs owner
   approval, a "publish this exact version" authorisation, and a confirmation when scheduling.

### Scopes requested
`instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_insights` (for
metrics). Without insights permission, analytics show "not available" rather than zeros.

### Flow implemented (`backend/growthpilot/adapters/instagram.py`)
1. `https://www.instagram.com/oauth/authorize` with `state` (random, stored hashed, single use, 10-minute expiry,
   bound to the user and workspace).
2. `POST https://api.instagram.com/oauth/access_token` → short-lived token →
   `GET https://graph.instagram.com/access_token?grant_type=ig_exchange_token` → 60-day token, Fernet-encrypted.
3. Verify the account (`/me`: username, account_type) before marking it **Connected & verified**.
4. Publish: check `content_publishing_limit` → `POST /{ig-id}/media` (image_url, caption, alt_text) →
   poll `status_code` until `FINISHED` (retry later if still `IN_PROGRESS`; fail on `ERROR`/`EXPIRED`) →
   `POST /{ig-id}/media_publish` → media id → verify via `GET /{media-id}?fields=permalink,timestamp`.
   Carousels: child containers (`is_carousel_item`) then a `CAROUSEL` container (2–10 images).
5. Token refresh in the last 7 days of validity (token at least 24h old). An expired or revoked token marks the
   connection **Expired — reconnect**.
6. Insights: `GET /{media-id}/insights?metric=reach,likes,comments,shares,saved,views`.

### Not supported yet
Reels and stories (they need video upload), Instagram via Facebook Login (needed for Business Discovery of
competitor accounts), and comment/DM management.

## Facebook Pages (Meta)

Implemented in `backend/growthpilot/adapters/facebook.py` from Meta's Pages API docs (checked 2026-10-24); tested
against a mocked Graph API only.

1. Use the same Meta app; add **Facebook Login for Business** and register
   `{BASE_URL}/api/integrations/facebook/callback` (shown on the Integrations page).
2. Permissions requested: `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`. Outside development
   mode these need App Review. The connecting person must be able to create content on the Page.
3. **Integrations → Facebook Page → Connect**. The code is exchanged for a long-lived user token, then
   `/me/accounts` returns Page tokens; only Pages with the `CREATE_CONTENT` task are kept (encrypted).
4. Publishing: single photo `POST /{page-id}/photos (url, message)`; several photos uploaded unpublished then one
   `POST /{page-id}/feed` with `attached_media`; text-only `POST /{page-id}/feed`. Verification:
   `GET /{post-id}?fields=permalink_url,created_time`. Metrics: public counts (reactions, comments, shares) only —
   Page insights (reach) are not collected, so reach shows "not available".

## Other platforms

Specs (limits, auth, eligibility, rate limits) live in `backend/growthpilot/platforms.py` and on the Integrations
page. In demo mode each has a simulated adapter; there is no live adapter yet, and GrowthPilot never claims one.

| Platform | Live publishing needs | Notes |
|---|---|---|
| Pinterest | Pinterest developer app, business account and board, Standard access | API v5 `POST /v5/pins`; title/description limits need re-checking |
| X | Developer account on a tier that allows posting (paid plans) | 280 characters for standard accounts |
| LinkedIn | Developer app, Community Management API approval, Page admin | Brand-story content, not product pushes |
| YouTube | Google Cloud project with YouTube Data API, a video file | Video only: GrowthPilot produces a Shorts brief, never publishes images there; unaudited projects upload as private |
| Threads | Meta app with the Threads use case | 500 characters (emoji count as UTF-8 bytes), carousels up to 20, 250 posts / 24 h |

## AI backgrounds (optional)

`IMAGE_GENERATION_PROVIDER=gemini` (default model `gemini-3.1-flash-lite-image`, ≈ $0.0336 per 1K image on
2026-10-24) uses the Interactions API with `response_format: {"type": "image", "aspect_ratio": …}`. Only abstract
backgrounds are requested — the prompt forbids text, products, people and deities — and the poster prints
"AI-generated background" on the image. Each request is budget-checked and recorded. Without it, posters use the
free template background.

## Signed webhook

Hand approved posts to Zapier, Make, n8n or your own service, which then posts anywhere. **Integrations → Add
webhook** shows the signing secret once. Each request is a JSON `POST` with `X-GrowthPilot-Timestamp` and
`X-GrowthPilot-Signature: sha256=HMAC_SHA256(secret, "<timestamp>.<raw body>")`. https only, private targets
blocked (unless `ALLOW_INSECURE_WEBHOOKS=true` for local testing), and redirects aren't followed. Delivery is
recorded as **Delivered**; whether it was actually posted is up to the receiver, and the UI says so.

## Environment variables
See [`.env.example`](../.env.example) for the full list with comments.
