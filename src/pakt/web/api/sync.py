"""Sync routes: start, follow and cancel a sync, and the built-in scheduler that starts one."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from pakt.config import Config
from pakt.models import SyncResult
from pakt.sync.run import run_multi_server_sync
from pakt.web import state

logger = logging.getLogger(__name__)

router = APIRouter()


class SyncRequest(BaseModel):
    """Options for a sync started over the API."""

    dry_run: bool = False
    verbose: bool = False
    fix_collection_dates: bool = False
    collection_only: bool = False
    no_movies: bool = False
    no_shows: bool = False
    servers: list[str] | None = None  # None syncs every enabled server


def _last_result(result: SyncResult) -> dict[str, Any]:
    """Build the status card's counts from a finished sync, ratings both ways counted together."""
    return {
        "added_to_trakt": result.added_to_trakt,
        "added_to_plex": result.added_to_plex,
        "ratings_synced": result.ratings_to_trakt + result.ratings_to_plex,
        "collection_added": result.collection_added,
        "collection_updated": result.collection_updated,
        "watchlist_added_trakt": result.watchlist_added_trakt,
        "watchlist_added_plex": result.watchlist_added_plex,
        "duration": result.duration_seconds,
        "errors": result.errors[:10],
    }


async def scheduled_sync() -> None:
    """Run a live sync for the scheduler unless one is already running."""
    sync_state = state.sync_state
    if sync_state["running"]:
        return

    sync_state["running"] = True
    sync_state["cancelled"] = False
    sync_state["logs"] = []

    try:
        config = Config.load()
        result = await run_multi_server_sync(
            config,
            dry_run=False,
            on_token_refresh=state.token_saver(config),
            log_callback=lambda msg: sync_state["logs"].append(msg),
            cancel_check=lambda: sync_state["cancelled"],
            started_by="scheduler",
        )
        sync_state["last_result"] = _last_result(result)
        sync_state["last_run"] = datetime.now().isoformat()
    except Exception as e:
        logger.exception(f"Scheduled sync failed: {type(e).__name__}: {e}")
        sync_state["logs"].append(f"ERROR:{e}")
        sync_state["last_result"] = {"error": str(e)}
    finally:
        sync_state["running"] = False
        sync_state["cancelled"] = False


def start_scheduler(config: Config) -> None:
    """Start the sync scheduler from config, keeping it in state.scheduler."""
    from pakt.scheduler import SyncScheduler

    state.scheduler = SyncScheduler(
        config, sync_func=scheduled_sync, is_running_func=lambda: state.sync_state["running"]
    )
    state.scheduler.start()


@router.post("/api/sync")
async def start_sync(request: SyncRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Start a sync in the background, refusing while one is already running."""
    sync_state = state.sync_state
    if sync_state["running"]:
        return {"status": "error", "message": "Sync already running"}

    # claim it now; a background task starts after the response, past a second request's check
    sync_state["running"] = True
    sync_state["cancelled"] = False
    sync_state["logs"] = []

    def log(msg: str):
        """Append a line for the web console to poll."""
        sync_state["logs"].append(msg)

    def is_cancelled() -> bool:
        """Whether a cancel was requested, polled by the sync engine."""
        return sync_state["cancelled"]

    async def do_sync():
        """Run the sync in the background and mirror its outcome into sync_state."""
        try:
            config = Config.load()
            result = await run_multi_server_sync(
                config,
                server_names=request.servers,
                dry_run=request.dry_run,
                verbose=request.verbose,
                fix_collection_dates=request.fix_collection_dates,
                collection_only=request.collection_only,
                no_movies=request.no_movies,
                no_shows=request.no_shows,
                on_token_refresh=state.token_saver(config),
                log_callback=log,
                cancel_check=is_cancelled,
                started_by="web UI",
            )

            if sync_state["cancelled"]:
                sync_state["last_result"] = {"cancelled": True}
            else:
                sync_state["last_result"] = _last_result(result)
                sync_state["last_run"] = datetime.now().isoformat()
        except Exception as e:
            logger.exception(f"Sync failed: {type(e).__name__}: {e}")
            log(f"ERROR:{e}")
            sync_state["last_result"] = {"error": str(e)}
        finally:
            sync_state["running"] = False
            sync_state["cancelled"] = False

    background_tasks.add_task(do_sync)
    return {"status": "started"}


@router.get("/api/sync/status")
async def get_sync_status(since: int = 0) -> dict[str, Any]:
    """Get sync status, returning only log lines the caller hasn't seen yet."""
    sync_state = state.sync_state
    logs = sync_state["logs"]
    return {
        "running": sync_state["running"],
        "last_run": sync_state["last_run"],
        "last_result": sync_state["last_result"],
        "logs": logs[since:] if since else logs,
        "log_count": len(logs),
    }


@router.post("/api/sync/cancel")
async def cancel_sync() -> dict[str, Any]:
    """Cancel a running sync."""
    if not state.sync_state["running"]:
        return {"status": "error", "message": "No sync running"}
    state.sync_state["cancelled"] = True
    return {"status": "ok"}


@router.get("/api/scheduler/status")
async def get_scheduler_status() -> dict[str, Any]:
    """Get scheduler status, from config alone when no scheduler has started."""
    config = Config.load()
    if state.scheduler:
        return state.scheduler.get_status()
    return {
        "enabled": config.scheduler.enabled,
        "interval_hours": config.scheduler.interval_hours,
        "next_run": None,
        "last_run": None,
    }
