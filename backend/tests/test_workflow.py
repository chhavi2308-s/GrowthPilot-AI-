from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from growthpilot.db import session_scope
from growthpilot.models import Asset, ChannelConnection, ContentItem, PublishAttempt, utcnow
from growthpilot.services import assets, publishing

from .conftest import Api, make_client


def _new_item(api: Api, caption="A calm corner with the Kabini 3-Seater Sofa.", **extra):
    prods = api.get("/api/products").json()
    body = {"caption": caption, "hook": "Meet Kabini", "cta": "Visit the experience centre",
            "hashtags": ["#AranyaLiving"], "product_ids": [prods[0]["id"]], "content_type": "post"} | extra
    r = api.post("/api/content", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _attach_photo(api: Api, cid: int):
    from .test_security import tiny_jpeg

    r = api.c.post("/api/assets", files={"file": ("p.jpg", tiny_jpeg(1080, 1350), "image/jpeg")},
                   headers={"X-CSRF-Token": api.csrf})
    assert r.status_code == 200, r.text
    return api.put(f"/api/content/{cid}", json={"asset_id": r.json()["id"]}).json()


def _demo_conn(api: Api) -> int:
    return next(c["id"] for c in api.get("/api/integrations").json()["connections"] if c["adapter"] == "demo_instagram")


def test_approval_bound_to_version_and_reapproval_after_edit(demo_seeded):
    api = demo_seeded
    item = _attach_photo(api, _new_item(api)["id"])
    assert api.post(f"/api/content/{item['id']}/submit").status_code == 200
    # Approving a stale version is refused.
    r = api.post(f"/api/content/{item['id']}/approve", json={"version": item["version"] - 1})
    assert r.status_code == 400 and "current version" in r.json()["message"]
    item = api.get(f"/api/content/{item['id']}").json()
    r = api.post(f"/api/content/{item['id']}/approve", json={"version": item["version"], "acknowledge_warnings": True})
    assert r.status_code == 200 and r.json()["status"] == "approved"
    approved_v = r.json()["approved_version"]
    # Editing after approval creates a new version and requires re-approval.
    r = api.put(f"/api/content/{item['id']}", json={"caption": "A changed caption for the Kabini 3-Seater Sofa."})
    after = r.json()
    assert after["status"] == "draft" and after["version"] == approved_v + 1 and after["approved_version"] is None
    r = api.post(f"/api/content/{item['id']}/schedule", json={"connection_id": _demo_conn(api)})
    assert r.status_code == 400 and "approved" in r.json()["message"]
    # History keeps every version.
    assert len(after["revisions"]) >= 3


def test_failed_validation_blocks_approval(demo_seeded):
    api = demo_seeded
    item = _attach_photo(api, _new_item(api, caption="Mega sale: 40% off the Kabini 3-Seater Sofa!")["id"])
    api.post(f"/api/content/{item['id']}/submit")
    item = api.get(f"/api/content/{item['id']}").json()
    r = api.post(f"/api/content/{item['id']}/approve", json={"version": item["version"], "acknowledge_warnings": True})
    assert r.status_code == 400 and "Validation failed" in r.json()["message"]


def test_warnings_must_be_acknowledged(demo_seeded):
    api = demo_seeded
    item = _attach_photo(api, _new_item(api)["id"])
    api.put(f"/api/content/{item['id']}", json={"caption": "The best sofa for Bengaluru flats: Kabini 3-Seater Sofa."})
    api.post(f"/api/content/{item['id']}/submit")
    item = api.get(f"/api/content/{item['id']}").json()
    r = api.post(f"/api/content/{item['id']}/approve", json={"version": item["version"]})
    assert r.status_code == 400 and "warning" in r.json()["message"]


def _approved(api, caption="A calm corner with the Kabini 3-Seater Sofa."):
    item = _attach_photo(api, _new_item(api, caption=caption)["id"])
    api.post(f"/api/content/{item['id']}/submit")
    item = api.get(f"/api/content/{item['id']}").json()
    r = api.post(f"/api/content/{item['id']}/approve", json={"version": item["version"], "acknowledge_warnings": True})
    assert r.status_code == 200, r.text
    return r.json()


def test_schedule_timezone_and_duplicates(demo_seeded):
    api = demo_seeded
    conn = _demo_conn(api)
    a = _approved(api)
    future = (utcnow() + timedelta(days=3)).date().isoformat()
    r = api.post(f"/api/content/{a['id']}/schedule", json={"connection_id": conn, "when": f"{future}T13:00"})
    assert r.status_code == 200, r.text
    # Workspace is Asia/Kolkata (UTC+5:30): 13:00 local → 07:30 UTC.
    assert r.json()["scheduled_at"].startswith(f"{future}T07:30")
    b = _approved(api)  # identical caption
    r = api.post(f"/api/content/{b['id']}/schedule", json={"connection_id": conn, "when": f"{future}T18:00"})
    assert r.status_code == 400 and "Duplicate" in r.json()["message"]
    r = api.post(f"/api/content/{b['id']}/schedule", json={"connection_id": conn, "when": "2020-01-01T10:00"})
    assert r.status_code == 400


def test_publish_is_idempotent_and_rechecks_version(demo_seeded):
    api = demo_seeded
    a = _approved(api)
    api.post(f"/api/content/{a['id']}/schedule", json={"connection_id": _demo_conn(api)})
    r = api.post(f"/api/content/{a['id']}/publish-now", json={})
    assert r.json()["status"] == "published"
    with session_scope() as db:
        item = db.get(ContentItem, a["id"])
        item.status = "scheduled"  # simulate a buggy retry of an already-published version
        item.scheduled_at = utcnow()
    with session_scope() as db:
        publishing.publish_item(db, a["id"])
        n = db.execute(select(PublishAttempt).where(PublishAttempt.content_id == a["id"],
                                                    PublishAttempt.success.is_(True))).scalars().all()
        assert len(n) == 1, "a version is only ever published once"
        assert db.get(ContentItem, a["id"]).status == "published"

    b = _approved(api, caption="Evening light on the Kabini 3-Seater Sofa.")
    r = api.post(f"/api/content/{b['id']}/schedule", json={"connection_id": _demo_conn(api)})
    assert r.status_code == 200, r.text
    # The database itself refuses a scheduled item whose version no longer matches its approval.
    with pytest.raises(IntegrityError):
        with session_scope() as db:
            db.get(ContentItem, b["id"]).version += 1
    # The image file is swapped behind the workflow's back: the approval hash no longer matches.
    with session_scope() as db:
        item = db.get(ContentItem, b["id"])
        path = assets.store().path(db.get(Asset, item.asset_id).storage_key)
        path.write_bytes(path.read_bytes() + b"tampered")
        item.scheduled_at = utcnow()
    with session_scope() as db:
        publishing.publish_item(db, b["id"])
        item = db.get(ContentItem, b["id"])
        assert item.status == "failed" and "approval" in item.last_error


def test_stuck_publishing_is_not_retried_automatically(demo_seeded):
    api = demo_seeded
    a = _approved(api)
    with session_scope() as db:
        item = db.get(ContentItem, a["id"])
        item.status = "publishing"
        item.updated_at = utcnow() - timedelta(hours=1)
    with session_scope() as db:
        db.execute(ContentItem.__table__.update().where(ContentItem.id == a["id"])
                   .values(updated_at=utcnow() - timedelta(hours=1)))
        publishing.recover_stuck(db)
        assert db.get(ContentItem, a["id"]).status == "failed"


def test_cancel_and_viewer_permissions(monkeypatch, tmp_path):
    owner = Api(make_client(monkeypatch, tmp_path))
    owner.register()
    owner.post("/api/demo/reset", json={"confirm": "RESET"})
    link = owner.post("/api/settings/invites", json={"email": "viewer@example.com", "role": "viewer"}).json()["link"]
    token = link.split("invite=")[1]
    owner.post("/api/auth/logout")
    viewer = Api(owner.c)
    viewer.register(email="viewer@example.com", name="Viewer", invite=token)
    assert viewer.state()["role"] == "viewer"
    r = viewer.post("/api/content", json={"caption": "x"})
    assert r.status_code == 403
    assert viewer.post("/api/agent/runs", json={"goal": "Plan a campaign for sofas"}).status_code == 403
    assert viewer.get("/api/content").status_code == 200


def test_live_kill_switch_blocks_scheduling(monkeypatch, tmp_path):
    api = Api(make_client(monkeypatch, tmp_path, "live", WEBHOOK_TEST="1", ALLOW_INSECURE_WEBHOOKS="true"))
    api.register()
    api.put("/api/brand", json={"name": "Live Brand", "location": "Bengaluru"})
    api.post("/api/products", json={"sku": "S1", "name": "Kabini 3-Seater Sofa", "category": "sofa", "price": "78500"})
    r = api.post("/api/connections/webhook", json={"name": "Hook", "url": "https://hooks.example.com/x"})
    assert r.status_code == 200
    conn_id = r.json()["connection"]["id"]
    with session_scope() as db:
        db.get(ChannelConnection, conn_id).status = "connected"
    item = _attach_photo(api, _new_item(api)["id"])
    api.post(f"/api/content/{item['id']}/submit")
    item = api.get(f"/api/content/{item['id']}").json()
    r = api.post(f"/api/content/{item['id']}/approve", json={"version": item["version"], "acknowledge_warnings": True,
                                                              "authorize_publish": True})
    assert r.status_code == 200, r.text
    r = api.post(f"/api/content/{item['id']}/schedule", json={"connection_id": conn_id, "confirm": True})
    assert r.status_code == 400 and "LIVE_PUBLISHING_ENABLED" in r.json()["message"]


def test_owner_can_authorise_publishing_after_approval(monkeypatch, tmp_path):
    """Approved before a live account was connected (so no authorisation was given): the owner can authorise that
    exact version later instead of sending it back, and any edit removes the authorisation again."""
    api = Api(make_client(monkeypatch, tmp_path, "live", WEBHOOK_TEST="1", ALLOW_INSECURE_WEBHOOKS="true",
                          LIVE_PUBLISHING_ENABLED="true"))
    api.register()
    api.put("/api/brand", json={"name": "Live Brand", "location": "Bengaluru"})
    api.post("/api/products", json={"sku": "S1", "name": "Kabini 3-Seater Sofa", "category": "sofa", "price": "78500"})
    item = _attach_photo(api, _new_item(api)["id"])
    cid = item["id"]
    api.post(f"/api/content/{cid}/submit")
    item = api.get(f"/api/content/{cid}").json()
    # Nothing to schedule yet: approved without the authorisation box.
    r = api.post(f"/api/content/{cid}/authorize-publish", json={"version": item["version"]})
    assert r.status_code == 400 and "approved" in r.json()["message"]
    r = api.post(f"/api/content/{cid}/approve", json={"version": item["version"], "acknowledge_warnings": True})
    assert r.status_code == 200 and r.json()["publish_authorized"] is False
    conn_id = api.post("/api/connections/webhook", json={"name": "Hook", "url": "https://hooks.example.com/x"}
                       ).json()["connection"]["id"]
    with session_scope() as db:
        db.get(ChannelConnection, conn_id).status = "connected"
    r = api.post(f"/api/content/{cid}/schedule", json={"connection_id": conn_id, "confirm": True})
    assert r.status_code == 400 and "Authorise publishing" in r.json()["message"]
    # Only the current approved version can be authorised.
    r = api.post(f"/api/content/{cid}/authorize-publish", json={"version": item["version"] + 1})
    assert r.status_code == 400
    r = api.post(f"/api/content/{cid}/authorize-publish", json={"version": item["version"]})
    assert r.status_code == 200 and r.json()["publish_authorized"] is True
    r = api.post(f"/api/content/{cid}/schedule", json={"connection_id": conn_id, "confirm": True})
    assert r.status_code == 200, r.text
    # Editing (after unscheduling) clears both the approval and the authorisation.
    api.post(f"/api/content/{cid}/unschedule")
    edited = api.put(f"/api/content/{cid}", json={"caption": "A brighter corner with the Kabini 3-Seater Sofa."}).json()
    assert edited["publish_authorized"] is False and edited["approved_version"] is None
    r = api.post(f"/api/content/{cid}/authorize-publish", json={"version": edited["version"]})
    assert r.status_code == 400


def test_only_owners_can_authorise_publishing(monkeypatch, tmp_path):
    owner = Api(make_client(monkeypatch, tmp_path))
    owner.register()
    owner.post("/api/demo/reset", json={"confirm": "RESET"})
    item = _attach_photo(owner, _new_item(owner)["id"])
    owner.post(f"/api/content/{item['id']}/submit")
    item = owner.get(f"/api/content/{item['id']}").json()
    assert owner.post(f"/api/content/{item['id']}/approve", json={"version": item["version"],
                                                                  "acknowledge_warnings": True}).status_code == 200
    link = owner.post("/api/settings/invites", json={"email": "ed@example.com", "role": "editor"}).json()["link"]
    owner.post("/api/auth/logout")
    editor = Api(owner.c)
    editor.register(email="ed@example.com", name="Editor", invite=link.split("invite=")[1])
    r = editor.post(f"/api/content/{item['id']}/authorize-publish", json={"version": item["version"]})
    assert r.status_code == 403
