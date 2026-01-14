"""FastAPI application for Pakt web interface."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from pakt.cache import Cache
from pakt.config import Config, get_config_dir
from pakt.sync import run_sync

# Global state for sync status
sync_state = {
    "running": False,
    "last_run": None,
    "last_result": None,
    "logs": [],
}


class ConfigUpdate(BaseModel):
    """Configuration update request."""

    trakt_client_id: str | None = None
    trakt_client_secret: str | None = None
    plex_url: str | None = None
    plex_token: str | None = None
    watched_plex_to_trakt: bool | None = None
    watched_trakt_to_plex: bool | None = None
    ratings_plex_to_trakt: bool | None = None
    ratings_trakt_to_plex: bool | None = None


class SyncRequest(BaseModel):
    """Sync request options."""

    dry_run: bool = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    yield


def create_app() -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(
        title="Pakt",
        description="Plex-Trakt sync",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Templates
    template_dir = Path(__file__).parent / "templates"
    templates = Jinja2Templates(directory=str(template_dir))

    # =========================================================================
    # Web UI Routes
    # =========================================================================

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        """Main dashboard."""
        config = Config.load()
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "config": config,
                "sync_state": sync_state,
            },
        )

    # =========================================================================
    # API Routes
    # =========================================================================

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        """Get current status."""
        config = Config.load()

        # Get cache stats
        cache_stats = {}
        try:
            async with Cache(config.cache) as cache:
                cache_stats = await cache.get_stats()
        except Exception:
            pass

        return {
            "trakt_configured": bool(config.trakt.client_id),
            "trakt_authenticated": bool(config.trakt.access_token),
            "plex_configured": bool(config.plex.url and config.plex.token),
            "sync_running": sync_state["running"],
            "last_run": sync_state["last_run"],
            "last_result": sync_state["last_result"],
            "cache_stats": cache_stats,
        }

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        """Get current configuration (sensitive values masked)."""
        config = Config.load()
        return {
            "trakt": {
                "client_id": config.trakt.client_id[:10] + "..." if config.trakt.client_id else None,
                "authenticated": bool(config.trakt.access_token),
            },
            "plex": {
                "url": config.plex.url,
                "configured": bool(config.plex.token),
            },
            "sync": {
                "watched_plex_to_trakt": config.sync.watched_plex_to_trakt,
                "watched_trakt_to_plex": config.sync.watched_trakt_to_plex,
                "ratings_plex_to_trakt": config.sync.ratings_plex_to_trakt,
                "ratings_trakt_to_plex": config.sync.ratings_trakt_to_plex,
            },
        }

    @app.post("/api/config")
    async def update_config(update: ConfigUpdate) -> dict[str, str]:
        """Update configuration."""
        config = Config.load()

        if update.trakt_client_id is not None:
            config.trakt.client_id = update.trakt_client_id
        if update.trakt_client_secret is not None:
            config.trakt.client_secret = update.trakt_client_secret
        if update.plex_url is not None:
            config.plex.url = update.plex_url
        if update.plex_token is not None:
            config.plex.token = update.plex_token
        if update.watched_plex_to_trakt is not None:
            config.sync.watched_plex_to_trakt = update.watched_plex_to_trakt
        if update.watched_trakt_to_plex is not None:
            config.sync.watched_trakt_to_plex = update.watched_trakt_to_plex
        if update.ratings_plex_to_trakt is not None:
            config.sync.ratings_plex_to_trakt = update.ratings_plex_to_trakt
        if update.ratings_trakt_to_plex is not None:
            config.sync.ratings_trakt_to_plex = update.ratings_trakt_to_plex

        config.save()
        return {"status": "ok"}

    @app.post("/api/sync")
    async def start_sync(request: SyncRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
        """Start a sync operation."""
        if sync_state["running"]:
            return {"status": "error", "message": "Sync already running"}

        async def do_sync():
            sync_state["running"] = True
            sync_state["logs"] = []
            try:
                config = Config.load()
                result = await run_sync(config, dry_run=request.dry_run)
                sync_state["last_result"] = {
                    "added_to_trakt": result.added_to_trakt,
                    "added_to_plex": result.added_to_plex,
                    "ratings_synced": result.ratings_synced,
                    "duration": result.duration_seconds,
                    "errors": result.errors[:10],
                }
                sync_state["last_run"] = datetime.now().isoformat()
            except Exception as e:
                sync_state["last_result"] = {"error": str(e)}
            finally:
                sync_state["running"] = False

        background_tasks.add_task(do_sync)
        return {"status": "started"}

    @app.get("/api/sync/status")
    async def get_sync_status() -> dict[str, Any]:
        """Get sync status."""
        return {
            "running": sync_state["running"],
            "last_run": sync_state["last_run"],
            "last_result": sync_state["last_result"],
            "logs": sync_state["logs"][-50:],
        }

    @app.post("/api/cache/clear")
    async def clear_cache() -> dict[str, Any]:
        """Clear expired cache entries."""
        config = Config.load()
        async with Cache(config.cache) as cache:
            removed = await cache.clear_expired()
            stats = await cache.get_stats()
        return {"removed": removed, "remaining": stats}

    @app.get("/api/trakt/auth")
    async def get_trakt_auth_url() -> dict[str, Any]:
        """Get Trakt device auth code."""
        from pakt.trakt import TraktClient

        config = Config.load()
        if not config.trakt.client_id:
            return {"error": "Trakt client_id not configured"}

        async with TraktClient(config.trakt) as client:
            device = await client.device_code()
            return {
                "verification_url": device["verification_url"],
                "user_code": device["user_code"],
                "device_code": device["device_code"],
                "expires_in": device["expires_in"],
                "interval": device.get("interval", 5),
            }

    @app.post("/api/trakt/auth/poll")
    async def poll_trakt_auth(device_code: str) -> dict[str, Any]:
        """Poll for Trakt auth completion."""
        from pakt.trakt import TraktClient

        config = Config.load()
        async with TraktClient(config.trakt) as client:
            token = await client.poll_device_token(device_code, interval=5, expires_in=30)
            if token:
                config.trakt.access_token = token["access_token"]
                config.trakt.refresh_token = token["refresh_token"]
                config.trakt.expires_at = token["created_at"] + token["expires_in"]
                config.save()
                return {"status": "authenticated"}
            return {"status": "pending"}

    @app.post("/api/plex/test")
    async def test_plex_connection() -> dict[str, Any]:
        """Test Plex connection."""
        from pakt.plex import PlexClient

        config = Config.load()
        try:
            plex = PlexClient(config.plex)
            plex.connect()
            return {
                "status": "ok",
                "server_name": plex.server.friendlyName,
                "movie_libraries": plex.get_movie_libraries(),
                "show_libraries": plex.get_show_libraries(),
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return app
