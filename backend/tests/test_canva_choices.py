from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from growthpilot.api.deps import ApiError, Ctx, reset_rate_limits
from growthpilot.api.routes import content as routes
from growthpilot.creative.canva_mcp import CanvaMCPError
from growthpilot.models import Asset, Brand, CanvaPosterJob, ContentItem, ContentStatus, Role, UsageRecord
from growthpilot.services import canva_choices as choices
from growthpilot.services import canva_poster_pipeline as pipeline


@pytest.fixture(autouse=True)
def isolate_limits():
    reset_rate_limits()
    choices._previews.clear()
    yield
    reset_rate_limits()
    choices._previews.clear()


def _item(**extra):
    values = dict(id=6, workspace_id=1, brand_id=2, platform="instagram", content_type="post",
                  image_brief="A blue mountain illustration", creative_brief=None, product_ids=[],
                  status=ContentStatus.DRAFT, canva_poster=None)
    return SimpleNamespace(**(values | extra))


def test_image_prompt_needs_no_canonical_brief_and_is_not_truncated():
    prompt = "paint a dreamy landscape " * 170 + "WITH A SMALL ORANGE SUN"
    item = _item(image_brief=prompt, creative_brief={"obsolete": "invalid"})
    brief, handoff = pipeline.prepare_canva_brief(None, item, SimpleNamespace(name="Brand", workspace_id=1))
    assert brief is None
    assert prompt in handoff["canva_query"]
    assert "WITH A SMALL ORANGE SUN" in handoff["canva_query"]
    assert "unsupported" not in prompt


def test_blank_prompt_without_brief_has_actionable_error():
    with pytest.raises(CanvaMCPError, match="Enter an Image brief"):
        pipeline.prepare_canva_brief(None, _item(image_brief="  "), SimpleNamespace(workspace_id=1))


def test_prompt_snapshot_invalidates_edited_prompt():
    item = _item()
    before = choices.request_key(item)
    item.image_brief = "A red city at night"
    assert choices.request_key(item) != before


def _png():
    output = BytesIO()
    Image.new("RGB", (40, 50), "blue").save(output, "PNG")
    return output.getvalue()


def test_previews_resolve_canva_redirects_without_storing_signed_urls(monkeypatch):
    original = httpx.AsyncClient
    requests, stored = [], []

    def respond(request):
        requests.append(str(request.url))
        if request.url.path == "/thumbnail":
            return httpx.Response(302, headers={"location": "https://media.canva.com/preview?signature=private-value"})
        return httpx.Response(200, content=_png())

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs,
    ))

    def save(_db, **kwargs):
        stored.append(kwargs)
        return SimpleNamespace(id=len(stored), source="", is_synthetic=False)

    monkeypatch.setattr(choices.assets, "save_upload", save)
    rows = [{"candidate_id": f"candidate-{i}", "thumbnails": [{"url": "https://design.canva.ai/thumbnail"}]}
            for i in range(3)]
    result = asyncio.run(choices.save_options(None, SimpleNamespace(id=7, workspace_id=1),
                                             SimpleNamespace(id=2), rows))
    assert len(result) == 3
    assert [option["preview_asset_id"] for option in result] == [1, 2, 3]
    assert len(requests) == 6
    assert "private-value" not in json.dumps(result)
    assert "https://" not in json.dumps(result)
    assert all(record["workspace_id"] == 1 and record["kind"] == "preview" for record in stored)


def test_preview_redirect_cannot_access_local_or_unrelated_hosts(monkeypatch):
    original = httpx.AsyncClient
    seen = []

    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    with pytest.raises(choices.PreviewUnavailable) as caught:
        asyncio.run(choices.preview_png("https://design.canva.ai/thumbnail"))
    assert caught.value.reason == "unsupported image host"
    assert seen == ["https://design.canva.ai/thumbnail"]


def test_generation_stops_at_options_without_auto_selecting_or_attaching(monkeypatch):
    item = _item()
    key = choices.request_key(item)
    item.canva_poster = {"job_id": 7, "request_key": key}
    brand = SimpleNamespace(id=2, workspace_id=1, name="Brand")
    job = SimpleNamespace(id=7, content_id=6, workspace_id=1, calls_used=0, status="generating", error="")
    calls, reserved = [], []

    class Db:
        def get(self, model, _identity):
            return {ContentItem: item, Brand: brand}[model]

        def commit(self):
            pass

        def flush(self):
            pass

        def refresh(self, _item):
            pass

    class Client:
        tools = {"generate-design": {"properties": {"query": {}, "design_type": {}}},
                 "create-design-from-candidate": {}}

        def schema(self, _name):
            return {"properties": {"query": {}, "design_type": {}}}

        async def call(self, name, arguments, **kwargs):
            calls.append(name)
            assert name == "generate-design"
            assert item.image_brief in arguments["query"]
            assert kwargs["reservation_made"] is True
            return {"job": {"id": "canva-generation", "result": {"generated_designs": [
                {"candidate_id": "first", "score": 0.1}, {"candidate_id": "second", "score": 0.9},
            ]}}}

        async def poll_design_job(self, payload):
            return payload

    @asynccontextmanager
    async def connected(*_args):
        yield Client(), None

    async def previews(_db, _job, _brand, rows):
        return [{"id": row["candidate_id"], "title": "Option", "preview_asset_id": i + 1}
                for i, row in enumerate(rows)]

    monkeypatch.setattr(pipeline, "connected_workspace_client", connected)
    monkeypatch.setattr(pipeline, "save_options", previews)
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(canva_mcp_daily_cap=5))
    monkeypatch.setattr(pipeline, "reserve_daily_generation", lambda *_args, **_kwargs: reserved.append(1))
    monkeypatch.setattr(pipeline, "_persist_poster", lambda *_args, **_kwargs: pytest.fail("User has not chosen"))
    asyncio.run(pipeline.process_job(Db(), job))
    assert calls == ["generate-design"]
    assert reserved == [1]
    assert job.calls_used == 1
    assert job.status == "awaiting_selection"
    assert item.canva_poster["selected_candidate_id"] is None
    assert item.canva_poster["generation_job_id"] == "canva-generation"
    assert len(item.canva_poster["candidates"]) == 2
    assert "design_id" not in item.canva_poster


@pytest.fixture
def choice_context(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in (Brand, ContentItem, CanvaPosterJob, Asset):
        model.__table__.create(engine)
    with Session(engine) as db:
        brand = Brand(id=2, workspace_id=1, name="Brand")
        item = ContentItem(id=6, workspace_id=1, brand_id=2, platform="instagram", content_type="post",
                           image_brief="A blue mountain illustration", status=ContentStatus.DRAFT)
        job = CanvaPosterJob(id=7, workspace_id=1, content_id=6, status="awaiting_selection", calls_used=1)
        db.add_all([brand, item, job])
        db.flush()
        key = choices.request_key(item)
        item.canva_poster = {"job_id": 7, "request_key": key, "prompt": item.image_brief,
            "generation_job_id": "generation", "candidates": [{"id": "first"}, {"id": "second"}]}
        db.commit()
        ctx = Ctx(db=db, workspace=SimpleNamespace(id=1), user=SimpleNamespace(id=8), role=Role.OWNER,
                  session=None, request=Request({"type": "http", "headers": [], "client": ("test", 1)}))
        monkeypatch.setattr(routes.worker, "notify", lambda: None)
        yield ctx, item, job, key
    engine.dispose()


def test_choice_is_explicit_idempotent_and_does_not_generate_again(choice_context):
    ctx, item, job, key = choice_context
    body = routes.CanvaChoiceIn(candidate_id="second", request_key=key)
    result = routes.select_canva_candidate(6, body, ctx)
    assert result["status"] == "queued"
    assert item.canva_poster["selected_candidate_id"] == "second"
    assert item.asset_id is None
    assert job.calls_used == 1
    assert routes.select_canva_candidate(6, body, ctx) == result


@pytest.mark.parametrize("scenario", ["foreign_candidate", "edited_prompt", "stale_batch", "viewer", "other_workspace"])
def test_choice_rejects_forged_stale_and_unauthorized_requests(choice_context, scenario):
    ctx, item, job, key = choice_context
    candidate = "first"
    if scenario == "foreign_candidate":
        candidate = "not-from-this-post"
    elif scenario == "edited_prompt":
        item.image_brief = "A red city at night"
    elif scenario == "stale_batch":
        key = "0" * 64
    elif scenario == "viewer":
        ctx.role = Role.VIEWER
    elif scenario == "other_workspace":
        ctx.workspace = SimpleNamespace(id=99)
    with pytest.raises(ApiError):
        routes.select_canva_candidate(6, routes.CanvaChoiceIn(candidate_id=candidate, request_key=key), ctx)
    assert job.status == "awaiting_selection"
    assert not item.canva_poster.get("selected_candidate_id")


def test_new_prompt_is_saved_and_queues_fresh_options_without_ai_workspace(choice_context, monkeypatch):
    ctx, item, job, _key = choice_context

    def save_prompt(_db, target, changes, **_kwargs):
        target.image_brief = changes["image_brief"]

    monkeypatch.setattr(routes.workflow, "update", save_prompt)
    result = routes.enqueue_canva_poster(6, ctx, routes.CanvaPosterIn(prompt="A red city at night", regenerate=True))
    assert result["status"] == "queued"
    assert item.image_brief == "A red city at night"
    assert item.creative_brief is None
    assert item.canva_poster["request_key"] == choices.request_key(item)
    assert "candidates" not in item.canva_poster
    assert job.calls_used == 0


def test_automatic_retries_obey_the_batch_cap(choice_context):
    ctx, item, job, _key = choice_context
    job.calls_used = pipeline.MAX_JOB_CALLS
    job.status = "failed"
    with pytest.raises(ApiError):
        routes.enqueue_canva_poster(6, ctx, routes.CanvaPosterIn(regenerate=False))
    assert job.status == "failed"
    assert item.canva_poster["candidates"]


def test_explicit_new_options_start_fresh_batch(choice_context):
    ctx, _item, job, _key = choice_context
    job.calls_used = pipeline.MAX_JOB_CALLS
    result = routes.enqueue_canva_poster(6, ctx, routes.CanvaPosterIn(regenerate=True))
    assert result["status"] == "queued"
    assert job.calls_used == 0


@pytest.mark.parametrize("scenario", ["wrong_workspace", "wrong_brand", "not_image"])
def test_reference_images_are_scoped_to_workspace_and_brand(choice_context, scenario):
    ctx, _item, job, _key = choice_context
    asset = Asset(id=19, workspace_id=99 if scenario == "wrong_workspace" else 1,
                  brand_id=99 if scenario == "wrong_brand" else 2, filename="reference.png", storage_key="reference",
                  content_type="text/plain" if scenario == "not_image" else "image/png", size_bytes=10)
    ctx.db.add(asset)
    ctx.db.commit()
    with pytest.raises(ApiError):
        routes.enqueue_canva_poster(6, ctx, routes.CanvaPosterIn(regenerate=True, reference_asset_id=19))
    assert job.status == "awaiting_selection"


def test_duplicate_option_generation_returns_same_active_job(choice_context):
    ctx, item, job, _key = choice_context
    first = routes.enqueue_canva_poster(6, ctx, routes.CanvaPosterIn(regenerate=True))
    second = routes.enqueue_canva_poster(6, ctx, routes.CanvaPosterIn(regenerate=True))
    assert first == second
    assert job.status == "queued"
    assert job.calls_used == 0


def test_editing_prompt_during_active_generation_is_rejected(choice_context):
    ctx, item, job, _key = choice_context
    job.status = "generating"
    with pytest.raises(ApiError):
        routes.enqueue_canva_poster(6, ctx, routes.CanvaPosterIn(prompt="A different prompt"))
    assert item.image_brief == "A blue mountain illustration"
    assert job.status == "generating"


def test_http_generation_accepts_typed_prompt_and_http_choice_requires_batch(choice_context, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from growthpilot.api.deps import get_ctx

    ctx, item, _job, _key = choice_context

    def save_prompt(_db, target, changes, **_kwargs):
        target.image_brief = changes["image_brief"]

    monkeypatch.setattr(routes.workflow, "update", save_prompt)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_ctx] = lambda: ctx
    with TestClient(app) as client:
        result = client.post("/api/content/6/canva-poster", json={"prompt": "A forest in watercolour", "regenerate": True})
        assert result.status_code == 202
        assert item.image_brief == "A forest in watercolour"
        assert client.post("/api/content/6/canva-poster/select", json={"candidate_id": "first"}).status_code == 422


def test_worker_commits_only_the_selected_candidate_and_exports_without_new_generation(monkeypatch):
    item = _item()
    job = SimpleNamespace(id=7, content_id=6, workspace_id=1, calls_used=pipeline.MAX_JOB_CALLS, status="generating")
    item.canva_poster = {"job_id": 7, "request_key": choices.request_key(item), "mode": "prompt-led",
        "generation_job_id": "generation", "candidates": [{"id": "first"}, {"id": "second"}],
        "selected_candidate_id": "second"}
    brand = SimpleNamespace(id=2, workspace_id=1, name="Brand")
    calls, persisted = [], []

    class Db:
        def get(self, model, _identity):
            return {ContentItem: item, Brand: brand}[model]

        def commit(self):
            pass

        def refresh(self, _item):
            pass

    class Client:
        tools = {"create-design-from-candidate": {}, "export-design": {}}

        def schema(self, name):
            props = {"candidate_id": {}, "job_id": {}} if name == "create-design-from-candidate" else {
                "design_id": {}, "format": {"type": "string"},
            }
            return {"properties": props, "required": list(props)}

        async def call(self, name, arguments):
            calls.append((name, arguments))
            if name == "create-design-from-candidate":
                assert arguments == {"candidate_id": "second", "job_id": "generation"}
                return {"design_summary": {"id": "chosen-design"}}
            assert name == "export-design"
            return {"job": {"urls": ["https://media.canva.com/export.png"]}}

    @asynccontextmanager
    async def connected(*_args):
        yield Client(), None

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=_png())), **kwargs,
    ))
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(pipeline, "connected_workspace_client", connected)
    monkeypatch.setattr(pipeline, "reserve_daily_generation", lambda *_args, **_kwargs: pytest.fail("No generation quota write"))
    monkeypatch.setattr(pipeline, "_overlay_brand_mark", lambda image, *_: image)
    monkeypatch.setattr(pipeline, "_brand_logo_bytes", lambda *_: None)
    monkeypatch.setattr(pipeline, "_persist_poster", lambda *args, **kwargs: persisted.append((args, kwargs)))
    asyncio.run(pipeline.process_job(Db(), job))
    assert [name for name, _ in calls] == ["create-design-from-candidate", "export-design"]
    assert job.calls_used == pipeline.MAX_JOB_CALLS
    assert item.canva_poster["selected_candidate_id"] == "second"
    assert persisted[0][0][5] == "chosen-design"
    assert persisted[0][0][6]["passed"] is False


def test_preview_failures_never_log_signed_url_details(monkeypatch, caplog):
    async def fail(_url):
        raise RuntimeError("https://media.canva.com/preview?signature=private-value")

    monkeypatch.setattr(choices, "preview_png", fail)
    result = asyncio.run(choices.save_options(None, SimpleNamespace(id=7, workspace_id=1), SimpleNamespace(id=2), [
        {"candidate_id": "first", "thumbnails": [{"url": "https://design.canva.ai/preview"}]},
    ]))
    assert result[0]["preview_asset_id"] is None
    assert "private-value" not in caplog.text
    assert "https://" not in caplog.text


def test_browser_preview_fallback_is_ephemeral_scoped_and_never_mutates_stored_poster(monkeypatch):
    poster = {"job_id": 7, "candidates": [{"id": "first", "preview_asset_id": None}]}
    moment = [100.0]
    monkeypatch.setattr(choices.time, "monotonic", lambda: moment[0])
    choices._remember_preview(1, 7, "first", "https://design.canva.ai/preview?signature=private-value")
    response = choices.option_previews(1, 7, poster)
    assert response["candidates"][0]["preview_url"].startswith("https://design.canva.ai/")
    assert "preview_url" not in poster["candidates"][0]
    assert "private-value" not in json.dumps(poster)
    assert "preview_url" not in choices.option_previews(99, 7, poster)["candidates"][0]
    assert "preview_url" not in choices.option_previews(1, 99, poster)["candidates"][0]
    moment[0] += 301
    assert "preview_url" not in choices.option_previews(1, 7, poster)["candidates"][0]


def test_reference_upload_uses_single_post_and_reads_media_id_from_upload_response(monkeypatch, tmp_path):
    path = tmp_path / "reference.png"
    path.write_bytes(_png())
    monkeypatch.setattr(pipeline.assets, "store", lambda: SimpleNamespace(path=lambda _key: path))
    original = httpx.AsyncClient
    seen = []

    def respond(request):
        seen.append(request.method)
        assert request.method == "POST"
        assert request.headers["content-type"] == "application/octet-stream"
        assert request.content == _png()
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"mediaId": "uploaded-media"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(respond), **kwargs,
    ))

    class Client:
        def schema(self, _name):
            return {"properties": {}}

        async def call(self, name, arguments):
            assert name == "create-upload-url" and arguments == {}
            return {"uploadUrl": "https://www.canva.com/_blob/upload?signature=private-value", "httpMethod": "POST"}

    asset = SimpleNamespace(storage_key="reference", filename="reference.png", content_type="image/png")
    assert asyncio.run(pipeline._upload_asset(Client(), asset, "Reference upload")) == "uploaded-media"
    assert seen == ["POST"]


def test_reference_edit_polls_same_job_and_does_not_restart_generation(monkeypatch):
    from growthpilot.services.canva_reference import edit_reference

    calls = []

    class Client:
        tools = {"generate-image": {}, "get-generate-image-job": {}}

        def schema(self, name):
            props = {"prompt": {}, "imageReferences": {"type": "array", "items": {"type": "string"}}} \
                if name == "generate-image" else {"jobId": {}}
            return {"properties": props, "required": list(props)}

        async def call(self, name, arguments):
            calls.append((name, arguments))
            if name == "generate-image":
                assert arguments["imageReferences"] == ["uploaded-media"]
                return {"jobId": "edit-job"}
            assert arguments == {"jobId": "edit-job"}
            if len(calls) == 2:
                return {"status": "PENDING"}
            return {"status": "SUCCESS", "result": {"results": [{"type": "MEDIA", "id": "edited-media"}]}}

    async def no_delay(_seconds):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    assert asyncio.run(edit_reference(Client(), "uploaded-media", "Turn the background blue")) == "edited-media"
    assert [name for name, _ in calls] == ["generate-image", "get-generate-image-job", "get-generate-image-job"]


def test_image_edit_and_design_generation_share_the_daily_quota():
    from growthpilot.creative.canva_mcp import reserve_daily_generation

    engine = create_engine("sqlite://")
    UsageRecord.__table__.create(engine)
    with Session(engine) as db:
        reserve_daily_generation(db, limit=1, operation="generate-image")
        db.commit()
        with pytest.raises(CanvaMCPError, match="daily generation cap"):
            reserve_daily_generation(db, limit=1)
    engine.dispose()


def test_reference_flow_edits_image_then_passes_edited_media_to_design_options(monkeypatch):
    item = _item()
    item.canva_poster = {"job_id": 7, "reference_asset_id": 19}
    item.canva_poster["request_key"] = choices.request_key(item)
    job = SimpleNamespace(id=7, content_id=6, workspace_id=1, calls_used=0, status="generating", error="")
    brand = SimpleNamespace(id=2, workspace_id=1, name="Brand")
    reference = SimpleNamespace(id=19, workspace_id=1, brand_id=2, content_type="image/png")
    calls, reserved = [], []

    class Db:
        def get(self, model, _identity):
            return {ContentItem: item, Brand: brand, Asset: reference}[model]

        def commit(self):
            pass

        def flush(self):
            pass

        def refresh(self, _item):
            pass

    design_schema = {"properties": {"query": {}, "design_type": {}, "asset_ids": {"type": "array"}}}

    class Client:
        tools = {"generate-design": design_schema, "create-design-from-candidate": {}, "create-upload-url": {},
                 "generate-image": {}, "get-generate-image-job": {}}

        def schema(self, name):
            return design_schema

        async def call(self, name, arguments, **_kwargs):
            calls.append(name)
            assert name == "generate-design"
            assert arguments["asset_ids"] == ["edited-media"]
            assert "reference image" in arguments["query"]
            return {"job_id": "generation", "generated_designs": [{"candidate_id": "first"}]}

        async def poll_design_job(self, payload):
            return payload

    @asynccontextmanager
    async def connected(*_args):
        yield Client(), None

    async def upload(_client, asset, _intent):
        assert asset is reference
        calls.append("upload")
        return "uploaded-media"

    async def edit(_client, media, prompt):
        assert media == "uploaded-media" and prompt == item.image_brief
        calls.append("generate-image")
        return "edited-media"

    async def previews(*_args):
        return [{"id": "first", "preview_asset_id": 20}]

    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace(canva_mcp_daily_cap=10))
    monkeypatch.setattr(pipeline, "connected_workspace_client", connected)
    monkeypatch.setattr(pipeline, "_upload_asset", upload)
    monkeypatch.setattr(pipeline, "edit_reference", edit)
    monkeypatch.setattr(pipeline, "save_options", previews)
    monkeypatch.setattr(pipeline, "reserve_daily_generation", lambda *_args, **kwargs: reserved.append(kwargs.get("operation", "generate-design")))
    asyncio.run(pipeline.process_job(Db(), job))
    assert calls == ["upload", "generate-image", "generate-design"]
    assert reserved == ["generate-image", "generate-design"]
    assert job.calls_used == 2
    assert job.status == "awaiting_selection"
    assert item.canva_poster["reference_asset_id"] == 19
    assert item.canva_poster["mode"] == "image-to-image"
