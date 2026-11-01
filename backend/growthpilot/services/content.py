"""Content lifecycle and the human-approval rules that guard publishing.

  draft ─submit─▶ in_review ─approve(owner)─▶ approved ─schedule(owner)─▶ scheduled ─▶ publishing ─▶ published
                     │  └─request changes─▶ changes_requested ─edit─▶ draft            └─▶ failed ─reschedule─┘
  any non-final state ─cancel─▶ cancelled

* Approval is bound to a version. Editing approved/scheduled content creates a new version and sends it
  back to draft, so only the exact reviewed version can ever be published.
* Live publishing additionally needs the owner's explicit publish authorisation on that version,
  LIVE_PUBLISHING_ENABLED=true, and a confirmation when scheduling.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..adapters import AdapterError, get_adapter
from ..config import get_settings
from ..models import (
    Asset,
    ChannelConnection,
    ConnectionStatus,
    ContentItem,
    ContentRevision,
    ContentStatus,
    Role,
    utcnow,
)
from .errors import PermissionDenied, WorkflowError
from .validation import final_caption, validate_item

EDITABLE_FIELDS = ("title", "hook", "caption", "hashtags", "cta", "image_brief", "carousel_outline", "video_script",
                   "content_type", "pillar", "product_ids", "planned_at", "asset_id", "slide_asset_ids",
                   "creative_brief")
SNAPSHOT_FIELDS = ("title", "hook", "caption", "hashtags", "cta", "image_brief", "carousel_outline", "video_script",
                   "content_type", "pillar", "product_ids", "asset_id", "slide_asset_ids")
BACK_TO_DRAFT = {ContentStatus.APPROVED, ContentStatus.SCHEDULED, ContentStatus.FAILED,
                 ContentStatus.CHANGES_REQUESTED}
FINAL = {ContentStatus.PUBLISHED, ContentStatus.PUBLISHING, ContentStatus.CANCELLED}
MAX_HASHTAGS = 30


def require(role: str, *allowed: Role) -> None:
    if role not in {r.value for r in allowed}:
        raise PermissionDenied("Your role doesn't allow this action.")


def normalise_hashtags(raw: list[str] | str) -> list[str]:
    parts = re.split(r"[\s,]+", raw) if isinstance(raw, str) else list(raw or [])
    out: list[str] = []
    for p in parts:
        tag = re.sub(r"[^\w]", "", str(p).strip().lstrip("#"))
        if tag and ("#" + tag).lower() not in {t.lower() for t in out}:
            out.append("#" + tag)
    return out[:MAX_HASHTAGS]


def snapshot(item: ContentItem) -> dict:
    snap = {f: getattr(item, f) for f in SNAPSHOT_FIELDS}
    if item.creative_brief is not None:  # left out when absent, so approvals made before briefs keep their hash
        snap["creative_brief"] = item.creative_brief
    return snap


def add_revision(db: Session, item: ContentItem, source: str, user_id: int | None, note: str = "") -> None:
    db.add(ContentRevision(content_id=item.id, version=item.version, snapshot=snapshot(item), source=source,
                           note=note[:2000], edited_by=user_id))


def _clear_approval(item: ContentItem) -> None:
    item.approved_by = None
    item.approved_at = None
    item.approved_version = None
    item.approved_hash = None
    item.publish_authorized = False


def approval_hash(db: Session, item: ContentItem) -> str:
    """sha256 over the exact text, platform, type and the bytes of every attached image."""
    from .assets import store

    parts = [json.dumps(snapshot(item), sort_keys=True, default=str), item.platform, item.content_type]
    ids = ([item.asset_id] if item.asset_id else []) + list(item.slide_asset_ids or [])
    for aid in ids:
        a = db.get(Asset, aid)
        if a is None:
            parts.append(f"missing:{aid}")
            continue
        try:
            parts.append(hashlib.sha256(store().path(a.storage_key).read_bytes()).hexdigest())
        except (OSError, WorkflowError):
            parts.append(f"unreadable:{aid}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def approval_intact(db: Session, item: ContentItem) -> bool:
    return bool(item.approved_hash) and item.approved_version == item.version and \
        approval_hash(db, item) == item.approved_hash


def update(db: Session, item: ContentItem, changes: dict, *, user_id: int | None, role: str, source: str = "human",
           note: str = "", ip: str | None = None) -> bool:
    """Apply edits as a new version. Returns True if anything changed."""
    require(role, Role.OWNER, Role.EDITOR)
    if item.status in FINAL:
        raise WorkflowError(f"Content that is {item.status} can't be edited.")
    clean = {}
    for k, v in changes.items():
        if k not in EDITABLE_FIELDS:
            continue
        if k == "hashtags":
            v = normalise_hashtags(v)
        if k == "content_type" and v not in ("post", "carousel", "reel", "story"):
            raise WorkflowError("Unknown content type.")
        if isinstance(v, str):
            v = v.strip() if k not in ("caption", "video_script", "image_brief") else v.strip("\n ")
        clean[k] = v
    changed = {k: v for k, v in clean.items() if getattr(item, k) != v}
    if not changed:
        return False
    previous = item.status
    for k, v in changed.items():
        setattr(item, k, v)
    content_fields = set(changed) - {"planned_at"}
    if content_fields:
        item.version += 1
        if previous in BACK_TO_DRAFT:
            item.status = ContentStatus.DRAFT
            item.scheduled_at = None
            _clear_approval(item)
        item.validation = None
        db.flush()
        add_revision(db, item, source, user_id, note)
    audit.record(db, "content.edited", workspace_id=item.workspace_id, user_id=user_id, target_type="content",
                 target_id=item.id, ip=ip, version=item.version, fields=sorted(changed), previous_status=previous,
                 source=source)
    return True


def restore_revision(db: Session, item: ContentItem, version: int, *, user_id: int, role: str) -> None:
    rev = next((r for r in item.revisions if r.version == version), None)
    if rev is None:
        raise WorkflowError("Version not found.")
    update(db, item, dict(rev.snapshot), user_id=user_id, role=role, source="restore", note=f"Restored v{version}")


def submit(db: Session, item: ContentItem, *, user_id: int | None, role: str, ip: str | None = None) -> None:
    require(role, Role.OWNER, Role.EDITOR)
    if item.status not in (ContentStatus.DRAFT, ContentStatus.CHANGES_REQUESTED):
        raise WorkflowError("Only drafts can be submitted for review.")
    validate_item(db, item)
    item.status = ContentStatus.IN_REVIEW
    audit.record(db, "content.submitted", workspace_id=item.workspace_id, user_id=user_id, target_type="content",
                 target_id=item.id, ip=ip, version=item.version)


def approve(db: Session, item: ContentItem, *, user_id: int, role: str, version: int,
            acknowledge_warnings: bool = False, authorize_publish: bool = False, ip: str | None = None) -> None:
    require(role, Role.OWNER)
    if item.status != ContentStatus.IN_REVIEW:
        raise WorkflowError("Only items in review can be approved.")
    if version != item.version:
        raise WorkflowError(f"You reviewed v{version} but the current version is v{item.version}. Review it again.")
    report = validate_item(db, item)
    if report.failures:
        raise WorkflowError("Validation failed: " + "; ".join(c.message for c in report.failures[:5]))
    if report.warnings and not acknowledge_warnings:
        raise WorkflowError(f"{len(report.warnings)} warning(s) need acknowledging before approval.")
    item.approved_by = user_id
    item.approved_at = utcnow()
    item.approved_version = item.version
    item.approved_hash = approval_hash(db, item)
    item.status = ContentStatus.APPROVED
    item.publish_authorized = bool(authorize_publish)
    item.review_note = ""
    audit.record(db, "content.approved", workspace_id=item.workspace_id, user_id=user_id, target_type="content",
                 target_id=item.id, ip=ip, version=item.version, warnings=len(report.warnings),
                 publish_authorized=item.publish_authorized, platform=item.platform, approved_hash=item.approved_hash)


def authorize_publish(db: Session, item: ContentItem, *, user_id: int, role: str, version: int,
                      ip: str | None = None) -> None:
    """Authorise live publishing for an already-approved version (for when the owner didn't tick it at approval,
    e.g. because no live account was connected yet). Bound to that exact, unchanged version."""
    require(role, Role.OWNER)
    if item.status not in (ContentStatus.APPROVED, ContentStatus.FAILED):
        raise WorkflowError("Only an approved post can be authorised for publishing.")
    if version != item.version or item.approved_version != item.version:
        raise WorkflowError(f"You're authorising v{version}, but the approved version is "
                            f"v{item.approved_version or '-'} and the current one is v{item.version}.")
    if not approval_intact(db, item):
        raise WorkflowError("The text or media changed after approval; it needs approval again.")
    item.publish_authorized = True
    audit.record(db, "content.publish_authorized", workspace_id=item.workspace_id, user_id=user_id,
                 target_type="content", target_id=item.id, ip=ip, version=item.version,
                 approved_hash=item.approved_hash)


def request_changes(db: Session, item: ContentItem, *, user_id: int, role: str, note: str,
                    ip: str | None = None) -> None:
    require(role, Role.OWNER)
    if item.status not in (ContentStatus.IN_REVIEW, ContentStatus.APPROVED):
        raise WorkflowError("Only items in review or approved can be sent back.")
    if not note.strip():
        raise WorkflowError("Say what needs to change.")
    item.status = ContentStatus.CHANGES_REQUESTED
    item.review_note = note.strip()[:2000]
    _clear_approval(item)
    audit.record(db, "content.changes_requested", workspace_id=item.workspace_id, user_id=user_id,
                 target_type="content", target_id=item.id, ip=ip, note=item.review_note)


def cancel(db: Session, item: ContentItem, *, user_id: int | None, role: str, ip: str | None = None) -> None:
    require(role, Role.OWNER, Role.EDITOR)
    if item.status in FINAL:
        raise WorkflowError(f"Content that is {item.status} can't be cancelled.")
    if item.status == ContentStatus.SCHEDULED and role != Role.OWNER:
        raise PermissionDenied("Only an owner can cancel scheduled content.")
    item.status = ContentStatus.CANCELLED
    item.scheduled_at = None
    audit.record(db, "content.cancelled", workspace_id=item.workspace_id, user_id=user_id, target_type="content",
                 target_id=item.id, ip=ip)


def caption_hash(item: ContentItem) -> str:
    return hashlib.sha256(" ".join(final_caption(item).lower().split()).encode()).hexdigest()


def find_duplicate(db: Session, item: ContentItem, connection_id: int) -> ContentItem | None:
    """Same caption to the same account within 7 days counts as a duplicate post."""
    h = caption_hash(item)
    since = utcnow() - timedelta(days=7)
    others = db.execute(
        select(ContentItem).where(
            ContentItem.workspace_id == item.workspace_id, ContentItem.id != item.id,
            ContentItem.connection_id == connection_id,
            ContentItem.status.in_([ContentStatus.SCHEDULED, ContentStatus.PUBLISHING, ContentStatus.PUBLISHED]),
        )
    ).scalars()
    for o in others:
        when = o.published_at or o.scheduled_at or utcnow()
        if when >= since and caption_hash(o) == h:
            return o
    return None


def schedule(db: Session, item: ContentItem, connection: ChannelConnection, when: datetime, *, user_id: int | None,
             role: str, confirmed: bool, ip: str | None = None, actor: str = "human") -> None:
    """Queue an approved item for publishing at ``when`` (naive UTC)."""
    s = get_settings()
    if actor == "human":
        require(role, Role.OWNER)
    if item.status not in (ContentStatus.APPROVED, ContentStatus.FAILED):
        raise WorkflowError("Only approved content can be scheduled.")
    if item.approved_version != item.version or item.approved_by is None:
        raise WorkflowError("This version hasn't been approved by a person.")
    if not approval_intact(db, item):
        raise WorkflowError("The text or media changed after approval; it needs approval again.")
    if connection.workspace_id != item.workspace_id:
        raise WorkflowError("Connection not found.")
    try:
        adapter = get_adapter(connection.adapter)
    except AdapterError as exc:
        raise WorkflowError(str(exc)) from exc
    if adapter.platform not in ("any", item.platform):
        raise WorkflowError(f"{connection.name} publishes to {adapter.platform}, not {item.platform}.")
    if item.content_type not in adapter.capabilities.content_types:
        raise WorkflowError(f"{adapter.label} can't publish {item.content_type}s.")
    if connection.status not in (ConnectionStatus.CONNECTED, ConnectionStatus.DEMO):
        raise WorkflowError(f"{connection.name} isn't a verified connection ({connection.status}).")
    if when < utcnow() - timedelta(minutes=2):
        raise WorkflowError("That time is in the past.")
    report = validate_item(db, item)
    if report.failures:
        raise WorkflowError("Validation now fails: " + "; ".join(c.message for c in report.failures[:3]))
    if dup := find_duplicate(db, item, connection.id):
        raise WorkflowError(f"Duplicate of content #{dup.id} on the same account within 7 days.")
    if adapter.external:
        if not s.live_publishing_enabled:
            raise WorkflowError("Live publishing is turned off (LIVE_PUBLISHING_ENABLED=false).")
        if not item.publish_authorized:
            raise WorkflowError("An owner must authorise publishing for this exact version first (tick it when "
                                "approving, or use “Authorise publishing” on the post).")
        if actor == "human" and not confirmed:
            raise WorkflowError("Confirm that this will publish to a real account.")
    item.status = ContentStatus.SCHEDULED
    item.connection_id = connection.id
    item.scheduled_at = when
    item.last_error = None
    audit.record(db, "content.scheduled", workspace_id=item.workspace_id, user_id=user_id, target_type="content",
                 target_id=item.id, ip=ip, connection_id=connection.id, adapter=connection.adapter,
                 scheduled_at=when.isoformat(), live=adapter.external, version=item.version, actor=actor)


def unschedule(db: Session, item: ContentItem, *, user_id: int, role: str, ip: str | None = None) -> None:
    require(role, Role.OWNER)
    if item.status != ContentStatus.SCHEDULED:
        raise WorkflowError("Item isn't scheduled.")
    item.status = ContentStatus.APPROVED
    item.scheduled_at = None
    audit.record(db, "content.unscheduled", workspace_id=item.workspace_id, user_id=user_id, target_type="content",
                 target_id=item.id, ip=ip)
