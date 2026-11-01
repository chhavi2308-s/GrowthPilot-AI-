"""Background worker: agent runs, due publications, metrics refresh, cleanup.

Runs inside the API process (RUN_WORKER_IN_PROCESS=true) or standalone (``python -m growthpilot worker``).
Running several is safe: runs and publications are claimed with atomic status updates.
"""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import delete, select

from .agent.runner import claim_next_run, execute_run, recover_stale
from .config import get_settings
from .db import session_factory
from .models import OAuthState, UserSession, Workspace, utcnow
from .services import analytics, campaign_generation, publishing
from .services.canva_poster_pipeline import process_queued as process_canva_poster_jobs

log = logging.getLogger("growthpilot.worker")
_wake = threading.Event()


def notify() -> None:
    _wake.set()


def tick(*, metrics: bool = False) -> dict:
    stats = {"runs": 0, "published": 0, "metrics": 0, "canva_posters": 0, "campaign_units": 0}
    db = session_factory()()
    try:
        while (run := claim_next_run(db)) is not None:
            execute_run(db, run)
            stats["runs"] += 1
        stats["published"] = publishing.run_due(db)
        stats["canva_posters"] = process_canva_poster_jobs(db, limit=1)
        stats["campaign_units"] = campaign_generation.process_queued(db)
        if metrics:
            for ws_id in db.execute(select(Workspace.id)).scalars().all():
                stats["metrics"] += analytics.collect(db, ws_id)["collected"]
        now = utcnow()
        db.execute(delete(UserSession).where(UserSession.expires_at < now))
        db.execute(delete(OAuthState).where(OAuthState.expires_at < now))
        db.commit()
    finally:
        db.close()
    return stats


def recover() -> None:
    db = session_factory()()
    try:
        recover_stale(db)
        publishing.recover_stuck(db)
        campaign_generation.recover_stale(db)
    finally:
        db.close()


def run_forever(stop: threading.Event | None = None) -> None:
    interval = get_settings().worker_interval_seconds
    stop = stop or threading.Event()
    recover()
    log.info("worker started (interval %ss)", interval)
    last_metrics = 0.0
    while not stop.is_set():
        try:
            due = time.monotonic() - last_metrics > 1800
            stats = tick(metrics=due)
            if due:
                last_metrics = time.monotonic()
            if any(stats.values()):
                log.info("worker tick %s", stats)
        except Exception:  # noqa: BLE001
            log.exception("worker tick failed")
        _wake.wait(interval)
        _wake.clear()


class InProcessWorker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=run_forever, args=(self._stop,), name="growthpilot-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        _wake.set()
        self._thread.join(timeout=10)
