"""Status routes: what the page loads with, and the global sync and scheduler settings."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from pakt.config import Config, ServerConfig
from pakt.web import state
from pakt.web.api.sync import start_scheduler

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
    rating_priority: str | None = None
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
    from pakt.plex.client import PlexClient
    from pakt.trakt import TraktClient

    config = state.load_config_cached()
    sync_state = state.sync_state

    result: dict[str, Any] = {
        "status": {
            "trakt_configured": bool(config.trakt.client_id),
            "trakt_authenticated": bool(config.trakt.access_token),
            "plex_configured": bool(config.servers),
            "sync_running": sync_state["running"],
            "last_run": sync_state["last_run"],
            "last_result": sync_state["last_result"],
        },
        "config": {
            "sync": {
                "watched_plex_to_trakt": config.sync.watched_plex_to_trakt,
                "watched_trakt_to_plex": config.sync.watched_trakt_to_plex,
                "ratings_plex_to_trakt": config.sync.ratings_plex_to_trakt,
                "ratings_trakt_to_plex": config.sync.ratings_trakt_to_plex,
                "collection_plex_to_trakt": config.sync.collection_plex_to_trakt,
                "watchlist_plex_to_trakt": config.sync.watchlist_plex_to_trakt,
                "watchlist_trakt_to_plex": config.sync.watchlist_trakt_to_plex,
            },
            "scheduler": {
                "enabled": config.scheduler.enabled,
                "interval_hours": config.scheduler.interval_hours,
            },
        },
        "servers": [],
        "trakt_account": None,
    }

    for s in config.servers:
        server_data: dict[str, Any] = {
            "name": s.name,
            "server_name": s.server_name,
            "url": s.url,
            "enabled": s.enabled,
            "movie_libraries": s.movie_libraries,
            "show_libraries": s.show_libraries,
            "sync": None,
            "libraries": None,
        }
        if s.sync:
            server_data["sync"] = {
                "watched_plex_to_trakt": s.sync.watched_plex_to_trakt,
                "watched_trakt_to_plex": s.sync.watched_trakt_to_plex,
                "ratings_plex_to_trakt": s.sync.ratings_plex_to_trakt,
                "ratings_trakt_to_plex": s.sync.ratings_trakt_to_plex,
                "collection_plex_to_trakt": s.sync.collection_plex_to_trakt,
                "watchlist_plex_to_trakt": s.sync.watchlist_plex_to_trakt,
                "watchlist_trakt_to_plex": s.sync.watchlist_trakt_to_plex,
            }

        cache_key = f"plex_libraries_{s.name}"
        cached_libs = state.get_cached(cache_key)
        if cached_libs:
            server_data["libraries"] = cached_libs
        else:
            # plexapi blocks; an offline server would stall the event loop until it times out
            def fetch_libraries(server_config: ServerConfig = s) -> dict[str, list[str]]:
                """Connect and list this server's libraries."""
                plex = PlexClient(server_config, account_token=config.plex_token)
                plex.connect()
                return {
                    "movie": plex.get_movie_libraries(),
                    "show": plex.get_show_libraries(),
                }

            try:
                libs = await asyncio.to_thread(fetch_libraries)
                state.set_cached(cache_key, libs)
                server_data["libraries"] = libs
            except Exception:
                server_data["libraries"] = {"movie": [], "show": []}

        result["servers"].append(server_data)

    if config.trakt.access_token:
        cached_trakt = state.get_cached("trakt_account")
        if cached_trakt:
            result["trakt_account"] = cached_trakt
        else:
            try:
                async with TraktClient(config.trakt, on_token_refresh=state.token_saver(config)) as client:
                    limits = await client.get_account_limits()
                    trakt_data = {
                        "status": "ok",
                        "is_vip": limits.is_vip,
                        "limits": {
                            "collection": limits.collection_limit,
                            "watchlist": limits.watchlist_limit,
                        },
                    }
                    state.set_cached("trakt_account", trakt_data)
                    result["trakt_account"] = trakt_data
            except Exception:
                result["trakt_account"] = {"status": "error"}

    return result


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
        "sync": {
            "watched_plex_to_trakt": config.sync.watched_plex_to_trakt,
            "watched_trakt_to_plex": config.sync.watched_trakt_to_plex,
            "ratings_plex_to_trakt": config.sync.ratings_plex_to_trakt,
            "ratings_trakt_to_plex": config.sync.ratings_trakt_to_plex,
            "collection_plex_to_trakt": config.sync.collection_plex_to_trakt,
            "watchlist_plex_to_trakt": config.sync.watchlist_plex_to_trakt,
            "watchlist_trakt_to_plex": config.sync.watchlist_trakt_to_plex,
            "rating_priority": config.sync.rating_priority,
        },
        "scheduler": {
            "enabled": config.scheduler.enabled,
            "interval_hours": config.scheduler.interval_hours,
        },
    }


@router.post("/api/config")
async def update_config(update: ConfigUpdate) -> dict[str, str]:
    """Update configuration and apply scheduler changes, starting the scheduler if needed."""
    config = Config.load()

    if update.watched_plex_to_trakt is not None:
        config.sync.watched_plex_to_trakt = update.watched_plex_to_trakt
    if update.watched_trakt_to_plex is not None:
        config.sync.watched_trakt_to_plex = update.watched_trakt_to_plex
    if update.ratings_plex_to_trakt is not None:
        config.sync.ratings_plex_to_trakt = update.ratings_plex_to_trakt
    if update.ratings_trakt_to_plex is not None:
        config.sync.ratings_trakt_to_plex = update.ratings_trakt_to_plex
    if update.collection_plex_to_trakt is not None:
        config.sync.collection_plex_to_trakt = update.collection_plex_to_trakt
    if update.watchlist_plex_to_trakt is not None:
        config.sync.watchlist_plex_to_trakt = update.watchlist_plex_to_trakt
    if update.watchlist_trakt_to_plex is not None:
        config.sync.watchlist_trakt_to_plex = update.watchlist_trakt_to_plex
    if update.rating_priority is not None and update.rating_priority in ("none", "plex", "trakt"):
        config.sync.rating_priority = update.rating_priority
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
