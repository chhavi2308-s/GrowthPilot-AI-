from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from growthpilot.agent.briefs import CreativeBrief
from growthpilot.models import Brand, CanvaPosterJob
from growthpilot.services.canva_poster_pipeline import (
    _design_id,
    _overlay_brand_mark,
    _persist_poster,
    _qa_verdict,
    _schema_args,
    _select_candidate,
    _urls,
    process_queued,
    rest_export,
)


@pytest.mark.parametrize("with_logo", [True, False])
def test_brand_mark_overlay_changes_exported_png(with_logo):
    poster = Image.new("RGB", (600, 750), (224, 205, 180))
    poster_bytes = BytesIO()
    poster.save(poster_bytes, format="PNG")
    logo_bytes = None
    if with_logo:
        logo = Image.new("RGBA", (120, 60), (120, 15, 35, 255))
        logo_stream = BytesIO()
        logo.save(logo_stream, format="PNG")
        logo_bytes = logo_stream.getvalue()

    result = _overlay_brand_mark(poster_bytes.getvalue(), "Example Brand", logo_bytes)

    assert result != poster_bytes.getvalue()
    with Image.open(BytesIO(result)) as branded:
        assert branded.size == (600, 750)
        assert branded.mode == "RGBA"


def test_poster_job_mapping_matches_applied_migration():
    table = CanvaPosterJob.__table__
    indexes = {i.name: i for i in table.indexes}
    assert set(indexes) == {"ix_canva_poster_jobs_content_id", "ix_canva_poster_jobs_workspace_id",
                            "ix_canva_poster_jobs_status"}
    assert indexes["ix_canva_poster_jobs_content_id"].unique is True
    assert len([column for column in table.columns if column.name == "content_id"]) == 1
    assert table.c.created_at.nullable is False
    assert table.c.updated_at.nullable is False
    foreign_keys = {fk.parent.name: (fk.target_fullname, fk.ondelete) for fk in table.foreign_keys}
    assert foreign_keys == {"content_id": ("content_items.id", "CASCADE"),
                            "workspace_id": ("workspaces.id", "CASCADE")}
    assert table.c.status.server_default.arg == "queued"
    assert table.c.calls_used.server_default.arg == "0"
    assert table.c.error.server_default.arg == ""


def test_schema_mapping_fails_closed_on_unknown_required_fields():
    class Client:
        def schema(self, _tool):
            return {"properties": {"candidate_id": {"type": "string"}, "mystery": {"type": "string"}},
                    "required": ["candidate_id", "mystery"]}
    with pytest.raises(Exception, match="Unmapped required"):
        _schema_args(Client(), "create-design-from-candidate", {"candidate_id": "candidate"})
    with pytest.raises(Exception, match="Unmapped required"):
        _schema_args(Client(), "create-design-from-candidate", {"candidate_id": "candidate", "mystery": None})


def test_export_url_discovery_walks_nested_response():
    assert list(_urls({"job": {"artifacts": [{"download": {"url": "https://download.invalid/a?sig=hidden"}}]}})) == [
        "https://download.invalid/a?sig=hidden"]


def test_candidate_choice_uses_returned_score_and_design_id_shape():
    rows = [{"candidate_id": "a", "score": 0.2}, {"candidate_id": "b", "score": 0.9}]
    assert _select_candidate(rows) is rows[1]
    assert _select_candidate([{"candidate_id": "first"}, {"candidate_id": "second"}]) == {"candidate_id": "first"}
    assert _design_id({"job": {"id": "job-id", "design": {"id": "design-id"}}}) == "design-id"
    assert _design_id({"design_summary": {"id": "design-id", "title": "Private draft"}}) == "design-id"


@pytest.mark.parametrize("spec,expected", [
    ({"type": "string", "enum": ["pdf", "png"]}, "png"),
    ({"type": "object", "properties": {"type": {"type": "string", "enum": ["png", "jpg"]}},
      "required": ["type"]}, {"type": "png"}),
])
def test_png_export_uses_discovered_format_shape(spec, expected):
    from growthpilot.services.canva_poster_pipeline import _export_args

    class Client:
        def schema(self, _name):
            return {"properties": {"design_id": {"type": "string"}, "format": spec},
                    "required": ["design_id", "format"]}

    assert _export_args(Client(), "design") == {"design_id": "design", "format": expected}


def test_export_retry_reuses_committed_design_without_generation_or_quota_write(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import httpx

    from growthpilot.models import Brand, ContentItem, ContentStatus
    from growthpilot.services import canva_poster_pipeline as pipeline

    item = SimpleNamespace(id=3, workspace_id=1, brand_id=2, product_ids=[], status=ContentStatus.DRAFT,
                           canva_poster={"job_id": 7, "design_id": "existing-design", "image_trust": "none",
                                         "edit_link_available": True})
    brand = SimpleNamespace(id=2, workspace_id=1, name="Test brand")
    job = SimpleNamespace(id=7, content_id=3, workspace_id=1, calls_used=3, status="queued")
    calls, saved = [], []

    class Db:
        def get(self, model, _identity):
            return {ContentItem: item, Brand: brand}[model]

        def commit(self):
            pass

    class Client:
        tools = {"export-design": {}}

        def schema(self, _name):
            return {"properties": {"design_id": {"type": "string"}, "format": {"type": "string"}},
                    "required": ["design_id", "format"]}

        async def call(self, name, arguments):
            calls.append((name, arguments))
            return {"job": {"urls": ["https://download.invalid/preview.png"]}}

    @asynccontextmanager
    async def connected(*_args):
        yield Client(), None

    original = httpx.AsyncClient
    png = b"\x89PNG\r\n\x1a\n" + b"test-image"
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=png)), **kwargs,
    ))
    monkeypatch.setattr(pipeline, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(pipeline, "prepare_canva_brief", lambda *_: (SimpleNamespace(), {}))
    monkeypatch.setattr(pipeline, "connected_workspace_client", connected)
    monkeypatch.setattr(pipeline, "reserve_daily_generation", lambda *_args, **_kwargs: pytest.fail("No new generation"))
    monkeypatch.setattr(pipeline, "_overlay_brand_mark", lambda image, *_: image)
    monkeypatch.setattr(pipeline, "_brand_logo_bytes", lambda *_: None)
    monkeypatch.setattr(pipeline, "_qa_verdict", lambda *_args, **_kwargs: {"passed": False})
    monkeypatch.setattr(pipeline, "_persist_poster", lambda *args, **kwargs: saved.append((args, kwargs)))

    asyncio.run(pipeline.process_job(Db(), job))
    assert calls == [("export-design", {"design_id": "existing-design", "format": "png"})]
    assert job.calls_used == 3
    assert saved[0][0][4:6] == (png, "existing-design")
    assert saved[0][1]["edit_link_available"] is True


def test_poster_png_is_persisted_as_workspace_asset_and_invalidates_approval(monkeypatch):
    from types import SimpleNamespace

    import growthpilot.services.canva_poster_pipeline as pipeline
    import growthpilot.services.content as content

    saved = {}
    asset = SimpleNamespace(id=29, source="upload")

    def save_upload(_db, **kwargs):
        saved.update(kwargs)
        return asset

    def update(_db, item, changes, **_kwargs):
        item.asset_id = changes["asset_id"]

    cleared = []
    monkeypatch.setattr(pipeline.assets, "save_upload", save_upload)
    monkeypatch.setattr(content, "update", update)
    monkeypatch.setattr(content, "_clear_approval", lambda item: cleared.append(item.id))
    item = SimpleNamespace(id=4, status="draft", canva_poster=None, asset_id=None)
    job = CanvaPosterJob(id=8, content_id=4, workspace_id=2, status="exporting", calls_used=1)
    brand = Brand(id=3, name="Poojaloka", workspace_id=2)
    _persist_poster(object(), item, job, brand, b"png bytes", "design-2", {"passed": False},
                    image_trust="none", image_skip_reason=None, edit_link_available=False)
    assert saved["data"] == b"png bytes"
    assert saved["workspace_id"] == 2 and saved["brand_id"] == 3
    assert item.asset_id == 29 and item.status == "in_review"
    assert item.canva_poster["design_id"] == "design-2"
    assert item.canva_poster["qa"]["passed"] is False
    assert cleared == [4] and job.status == "completed"


def test_rest_export_fallback_delegates_to_development_interface(monkeypatch):
    async def mocked_export(design_id, **_kwargs):
        assert design_id == "design-1"
        return b"\x89PNG\r\n\x1a\nmock"
    monkeypatch.setattr("growthpilot.services.canva_rest_dev.export_png", mocked_export)
    result = _run_mocked(rest_export(None, 2, "design-1"))
    assert result.startswith(b"\x89PNG\r\n\x1a\n")


async def _no_sleep(_seconds):
    return None


def _run_mocked(awaitable):
    """Run immediate-only async mocks without opening sandbox-blocked Windows sockets."""
    coroutine = awaitable.__await__()
    try:
        while True:
            yielded = coroutine.send(None)
            if yielded is not None:
                raise AssertionError("mock coroutine unexpectedly suspended")
    except StopIteration as done:
        return done.value


def test_qa_never_passes_unverified_rendered_text_and_flags_religious_content():
    brief = CreativeBrief.model_validate({
        "schema_version": "1.0", "content_id": 1, "platform": "instagram", "format": "single_image",
        "objective": "awareness", "audience": "customers", "festival": None, "angle": "brand_promise",
        "image_policy": "text_only", "image_refs": [], "headline": "A calm prayer corner",
        "tagline": "Make space for daily rituals", "cta": "Explore the collection",
        "required_text": ["Poojaloka", "A calm prayer corner", "Make space for daily rituals",
                          "Explore the collection"],
        "claims_allowed": [], "banned": ["unsupported claims"],
        "style": {"composition": "typographic", "layout": "centered_stack", "palette": "warm",
                  "colors": ["#FFFFFF", "#000000"], "mood": "calm"},
        "canva_query": 'Include exactly "Poojaloka", "A calm prayer corner", "Make space for daily rituals", '
                       '"Explore the collection". Use exactly the quoted text and add no other words, numbers, '
                       "prices, offers or claims. Typographic only: no product photos, stock photos, deities, "
                       "religious figures, mantras or Sanskrit text.",
        "signals": {"reason": "review required"}, "needs_human_review": True,
    })
    brand = Brand(id=1, name="Poojaloka", workspace_id=1)
    verdict = _qa_verdict(brief, brand, None, image_used=False, image_skip_reason=None)
    assert verdict["passed"] is False
    assert verdict["checks"]["required_text"] == "not_verified"
    assert verdict["religious_content"] is True


def test_queued_worker_marks_timeout_failed_without_leaking_details(monkeypatch):
    import growthpilot.services.canva_poster_pipeline as pipeline

    job = CanvaPosterJob(id=4, content_id=8, workspace_id=2, status="queued", calls_used=0, error="")
    class Rows:
        def scalars(self):
            return self
        def all(self):
            return [job.id]
    class Claimed:
        rowcount = 1
    class DB:
        calls = 0
        def execute(self, _q):
            self.calls += 1
            return Rows() if self.calls == 1 else Claimed()
        def commit(self): pass
        def rollback(self): pass
        def get(self, _model, _id): return job
    async def fail(*_args, **_kwargs):
        raise TimeoutError("download failed: https://signed.invalid/file?secret=hidden")
    monkeypatch.setattr(pipeline, "process_job", fail)
    monkeypatch.setattr(pipeline.asyncio, "run", _run_mocked)
    assert process_queued(DB()) == 1
    assert job.status == "failed"
    assert "secret=hidden" not in job.error


def test_enqueue_endpoint_returns_existing_active_job_without_duplicate(monkeypatch):
    from types import SimpleNamespace

    from growthpilot.api.routes.content import enqueue_canva_poster

    job = CanvaPosterJob(id=7, content_id=1, workspace_id=2, status="generating", calls_used=1, error="")
    brief = {
        "schema_version": "1.0", "content_id": 1, "platform": "instagram", "format": "single_image",
        "objective": "awareness", "audience": "customers", "festival": None, "angle": "brand_promise",
        "image_policy": "text_only", "image_refs": [], "headline": "A calm prayer corner",
        "tagline": "Make space for daily rituals", "cta": "Explore the collection",
        "required_text": ["Poojaloka", "A calm prayer corner", "Make space for daily rituals", "Explore the collection"],
        "claims_allowed": [], "banned": ["unsupported claims"],
        "style": {"composition": "typographic", "layout": "centered_stack", "palette": "warm",
                  "colors": ["#FFFFFF", "#000000"], "mood": "calm"},
        "canva_query": 'Include exactly "Poojaloka", "A calm prayer corner", "Make space for daily rituals", '
                       '"Explore the collection". Use exactly the quoted text and add no other words, numbers, '
                       "prices, offers or claims. Typographic only: no product photos, stock photos, deities, "
                       "religious figures, mantras or Sanskrit text.",
        "signals": {"reason": "evergreen"}, "needs_human_review": False,
    }
    item = SimpleNamespace(id=1, brand_id=3, workspace_id=2, platform="instagram", creative_brief=brief,
                           product_ids=[])

    class Result:
        def scalars(self):
            return self
        def all(self):
            return []
        def scalar_one_or_none(self):
            return job

    class DB:
        calls = 0
        def execute(self, _query):
            self.calls += 1
            return Result()
        def commit(self):
            raise AssertionError("an active job must not be rewritten")

    class Context:
        db = DB()
        workspace = SimpleNamespace(id=2)
        user = SimpleNamespace(id=3)
        def require_editor(self):
            return None
        def get(self, model, _id):
            return Brand(id=3, name="Poojaloka", workspace_id=2) if model is Brand else item

    monkeypatch.setattr("growthpilot.api.routes.content.worker.notify", lambda: None)
    response = enqueue_canva_poster(1, Context())
    assert response == {"job_id": 7, "status": "generating", "calls_used": 1}
