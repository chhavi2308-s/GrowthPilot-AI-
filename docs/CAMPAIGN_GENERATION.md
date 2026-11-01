# Research-driven campaign generation

One request, such as "Create a 5-day Diwali marketing campaign for my store.", produces:
- a research brief with sources;
- one N-day strategy (5 days by default);
- for each day: a caption, hashtags, a CTA, an image prompt and one 2K creative;
- a dashboard with each day's status, and an approval step before anything is scheduled.

## How it works

```
Campaigns → "Generate campaign" ──POST /api/campaign-generations──▶ campaign + generation + N day rows (queued)
                                                                          │  worker: one unit of work per step,
                                                                          ▼  committed before the next
  1 research  Brave: 3–5 queries for the whole campaign (dedupe, cache, cap, one bounded retry) → sources (S1…)
  2 strategy  Groq (LLM_PROVIDER), one structured call → exactly N days, catalogue products and real source ids only
  3 text      Groq, one structured call → per day: hook, caption, hashtags, CTA, image prompt → N draft posts
  4 images    per day, one after another: Gemini (google-genai SDK, CAMPAIGN_IMAGE_MODEL at CAMPAIGN_IMAGE_SIZE)
              makes the background scene → poster renderer adds the real product photo and the text →
              post goes to review. Each day has its own status (pending → generating → completed | failed).
```

- **Code:**
  - `services/campaign_generation.py`: the stages, retries, recovery and approve-all.
  - `creative/imagegen.generate_image()`: the Gemini SDK call and image validation.
  - `api/routes/generation.py`: the API.
  - `components/campaign-generation.tsx`: the form and the dashboard.
- **Tables:** `campaign_generations` and `campaign_generation_days` (migration `d4e7a1c9b302`). Posts are ordinary
  `content_items`, so editing, versioning, validation, approval and scheduling all use the existing workflow.
- **Never lost:** each stage's output is stored before the next starts.
  - A failed stage stops the generation at that stage, with the reason. **Retry** re-runs only that stage.
  - A failed day leaves the other days as they are. **Retry image** re-runs that day only.
  - If the AI image was already paid for and only composing the poster failed, a retry reuses that image.
  - **Regenerate image** replaces a finished day's image. It is billable and asks first.
  - **Use local poster** renders that day without AI.
- **Restarts:** a worker that dies mid-request leaves its lease behind. On restart the generation is re-queued. The
  interrupted day is marked failed, not re-sent automatically, because that request may already have been billed.
- **Duplicates:**
  - Only one generation per workspace can be queued or running at a time.
  - Workers claim generations and days with atomic status updates.
  - The SDK's own hidden retry is switched off (see Gemini below), so one counted attempt is exactly one request.

## Rules it keeps

- **Facts:** prices, discounts, offers, stock, testimonials and features are never invented.
  - Strategy products must be catalogue ids; unknown ids are dropped.
  - Research findings must cite real source ids. A "finding" without one is moved to assumptions.
  - Posts go through the existing claim validation before approval.
  - The poster renderer refuses on-image text that fails the product checks.
- **Images:** the AI never draws the product. Every image prompt always ends with the exclusions: no text, logos,
  products or lookalikes, people's faces, or deities. The real product photo is composited by Pillow.
- **Publishing:** nothing is published or scheduled by generation. "Approve N posts" approves each post at its
  current version; scheduling and live publishing keep their own checks (`LIVE_PUBLISHING_ENABLED`, owner
  authorisation).
- **Security:**
  - Routes go through the normal session, CSRF and workspace checks (`ctx.get`).
  - Editors start and retry; only owners approve.
  - Keys stay on the server, and the Gemini key is sent only in the SDK's `x-goog-api-key` header.

## Settings (`.env`)

| Setting | Default | What it does |
|---|---|---|
| `GROQ_API_KEY`, `GROQ_MODEL` | `openai/gpt-oss-120b` | Strategy and captions (any configured `LLM_PROVIDER` works) |
| `BRAVE_SEARCH_API_KEY` | | Research. Without it, live research fails with a clear message instead of inventing findings |
| `GEMINI_API_KEY` + `IMAGE_GENERATION_PROVIDER=gemini` | `none` | Turns on AI images. Without them the local renderer is used and the UI says so |
| `CAMPAIGN_IMAGE_MODEL` | `gemini-nano-banana-2.1` | Verified model id for "Nano Banana 2.1" |
| `CAMPAIGN_IMAGE_SIZE` | `2K` | `1K` / `2K` / `4K`; the returned image is checked against it |
| `CAMPAIGN_MAX_SEARCHES` | `5` | Brave requests per campaign |
| `CAMPAIGN_IMAGE_ATTEMPTS` | `2` | Automatic tries per day (rate limits, timeouts, 5xx); safety blocks aren't retried |
| `CAMPAIGN_IMAGE_CONCURRENCY` | `1` | Image requests in flight |
| `CAMPAIGN_BUDGET_USD` | `1.00` | Checked before starting (expected cost) and before every paid call |
| `IMAGE_TIMEOUT_SECONDS`, `SEARCH_TIMEOUT_SECONDS` | `120`, `15` | Request timeouts |
| `BRAVE_STORAGE_RIGHTS` | `false` | Keep search snippets (only if your Brave plan has storage rights) |

## Gemini image model (verified 2026-11-01)

- **Source:** [ai.google.dev/gemini-api/docs/image-generation](https://ai.google.dev/gemini-api/docs/image-generation)
  and the [pricing page](https://ai.google.dev/gemini-api/docs/pricing).
- **Model:** "Nano Banana 2.1" is **`gemini-nano-banana-2.1`**. It supports 1K, 2K and 4K output.
- **Call:** `client.interactions.create(model=..., input=[...], response_format={"type": "image", "image_size": "2K",
  "aspect_ratio": "4:5"}, store=False)`. The image comes back base64-encoded in `output_image` or the steps.
- **Price:** **$0.0504 per 2K image** (1K $0.0336, 4K $0.113). Image output has **no free tier**. The docs don't say
  whether the model is preview or stable.
- **SDK quirk (google-genai 2.34):** the interactions client treats `HttpRetryOptions.attempts` as a *retry* count,
  and the client raises `0` to `1`. So a 429 or 5xx was silently sent a second time. `imagegen._sdk_client` switches
  the interactions retry strategy off. `test_transient_image_errors_are_retried_within_the_day_limit` pins exactly
  one HTTP request per counted attempt, so an SDK update that changes this fails the test.

## Requests for one 5-day campaign

| Provider | Requests | Cost |
|---|---|---|
| Brave | up to 5, or fewer if cached (one retry pass only for failed queries, within the cap) | up to $0.025 at $5/1,000 (after Brave's monthly credit) |
| Groq | 2: strategy, then captions and prompts. Up to 4 if both outputs are invalid once; each stage stops after 2 tries | $0 on the free tier (tokens are counted) |
| Gemini | 5 (up to 10 with `CAMPAIGN_IMAGE_ATTEMPTS=2`) | $0.25 expected, $0.50 at most |
| **Total** | **≤ 12 expected** | **≈ $0.28 expected** |

Regenerating one day's image is one more Gemini request and never repeats research or text. The Groq token count per
campaign isn't measured yet; record it from the dashboard after the first live run.

## Run it

```bash
make start   # or on Windows: see README; the API and web app must both run (the worker runs inside the API)
```

1. **Demo, no keys:** sign in with `data/demo-login.txt`, then go to **Campaigns → Generate campaign**. Enter "Create a
   5-day Diwali marketing campaign for my store." and click **Start generating**. You get local posters, no external
   calls and a $0 cost.
2. **Live:** in `.env`, set the following and restart:
   - `APP_MODE=live`
   - `GROQ_API_KEY`
   - `BRAVE_SEARCH_API_KEY`
   - `GEMINI_API_KEY`
   - `IMAGE_GENERATION_PROVIDER=gemini`

   Then register the first account at `/register`, set up the brand (Brand Brain: website scan or CSV), and do the
   same steps. Check the cost estimate in the form before starting.

## Live smoke tests (not done yet: no credentials were available)

Everything above is tested only against mocked APIs (`tests/test_campaign_generation.py`). Before a demo, run these
once with real keys:

1. **Integrations:** Groq → **Test**, and Brave search → **Test**. Both must pass.
2. **Small run:** generate a **1-day** campaign. On the dashboard check that:
   - research shows 3–5 queries and real source links;
   - the strategy has exactly 1 day;
   - day 1 completes with a 2K creative (about 1638×2048 for 4:5). The card shows "AI background
     (gemini-nano-banana-2.1) + real product photo".
3. **Gemini usage:** in Google AI Studio's usage page, confirm exactly **one** image request for that run, and the
   2K price.
4. **Full run:** generate the **5-day** Diwali campaign and check the usage line. Expect ≤ 5 Brave requests, 2 model
   calls and 5 images, at about $0.28. Record the Groq tokens in `docs/API_INTEGRATIONS.md`.
5. **Retry:** regenerate one day and confirm only one new Gemini request, and no new Brave or Groq requests.
6. **Approval:** reject one post, then approve the rest with **Approve N posts**. Confirm nothing was scheduled or
   published.

## Known limitations

- **Live APIs:** none of Brave, Groq or Gemini has been run live for this feature.
- **Prompts and research:** the image prompt is in English, and research queries come from templates (occasion,
  category, market), not from a model call. That saves a Groq request; tune the templates per industry if needed.
- **Brave storage:** the existing 24-hour Brave cache (`SEARCH_CACHE_HOURS`) also stores results. Set it to 0 if
  your Brave plan has no storage rights.
- **Concurrency:** `CAMPAIGN_IMAGE_CONCURRENCY` is enforced across worker processes through the database count.
  The in-process limiter applies per process.
- **Store selection:** a workspace normally has one brand (store). The store picker lists the workspace's brands.
