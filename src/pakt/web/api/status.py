"""Status routes: what the page loads with, and the global sync and scheduler settings."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from pakt.config import Config, SyncConfig
from pakt.web import state
from pakt.web.api.servers import list_libraries, server_json
from pakt.web.api.sync import start_scheduler
from pakt.web.api.trakt import account_status

router = APIRouter()


class ConfigUpdate(BaseModel):
    """Changes to the global sync and scheduler settings; a None field stays as it is."""

    watched_plex_to_trakt: bool | None = None
    watched_trakt_to_plex: bool | None = None
    ratings_plex_to_trakt: bool | None = None
    ratings_trakt_to_plex: bool | None = None
    collection_plex_to_trakt: bool | None = None
    watchlist_plex_to_trakt: bool | None = None
    watchlist_trakt_to_plex: bool | None = None
    rating_priority: Literal["none", "plex", "trakt"] | None = None
    scheduler_enabled: bool | None = None
    scheduler_interval_hours: int | None = Field(default=None, ge=1, le=8760)


@router.get("/api/status")
async def get_status() -> dict[str, Any]:
    """Get whether Trakt and Plex are set up, and the running or last sync's state."""
    config = state.load_config_cached()
    sync_state = state.sync_state

    return {
        "trakt_configured": bool(config.trakt.client_id),
        "trakt_authenticated": bool(config.trakt.access_token),
        "plex_configured": bool(config.servers),
        "sync_running": sync_state["running"],
        "last_run": sync_state["last_run"],
        "last_result": sync_state["last_result"],
    }


@router.get("/api/init")
async def get_init() -> dict[str, Any]:
    """Get all data needed for page load in one request."""
    config = state.load_config_cached()

    servers = []
    for server in config.servers:
        entry = server_json(server)
        try:
            entry["libraries"] = await asyncio.to_thread(list_libraries, server, config.plex_token)
        except Exception:
            entry["libraries"] = {"movie": [], "show": []}
        servers.append(entry)

    return {
        "status": await get_status(),
        "config": {
            "sync": config.sync.model_dump(exclude={"rating_priority"}),
            "scheduler": config.scheduler.model_dump(),
        },
        "servers": servers,
        "trakt_account": await account_status(config) if config.trakt.access_token else None,
    }


@router.get("/api/config")
async def get_config() -> dict[str, Any]:
    """Get current configuration (sensitive values masked)."""
    config = Config.load()
    first_server = config.servers[0] if config.servers else None
    return {
        "trakt": {
            "client_id": config.trakt.client_id[:10] + "..." if config.trakt.client_id else None,
            "authenticated": bool(config.trakt.access_token),
        },
        "plex": {
            "url": first_server.url if first_server else "",
            "configured": bool(config.servers),
        },
        "sync": config.sync.model_dump(),
        "scheduler": config.scheduler.model_dump(),
    }


@router.post("/api/config")
async def update_config(update: ConfigUpdate) -> dict[str, str]:
    """Update configuration and apply scheduler changes, starting the scheduler if needed."""
    config = Config.load()

    changes = update.model_dump(exclude_none=True)
    for name in SyncConfig.model_fields:
        if name in changes:
            setattr(config.sync, name, changes[name])
    if update.scheduler_enabled is not None:
        config.scheduler.enabled = update.scheduler_enabled
    if update.scheduler_interval_hours is not None:
        config.scheduler.interval_hours = update.scheduler_interval_hours

    config.save()
    state.invalidate_config_cache()

    if update.scheduler_enabled is not None or update.scheduler_interval_hours is not None:
        if state.scheduler:
            state.scheduler.update_config(
                config.scheduler.enabled,
                config.scheduler.interval_hours,
            )
        elif config.scheduler.enabled and config.scheduler.interval_hours > 0:
            start_scheduler(config)

    return {"status": "ok"}
