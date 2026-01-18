"""FastAPI application for Pakt web interface."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from pakt.config import Config, get_config_dir
from pakt.sync import run_sync

# Global state for sync status
sync_state = {
    "running": False,
    "cancelled": False,
    "last_run": None,
    "last_result": None,
    "logs": [],
}

# Global scheduler instance
_scheduler = None


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
    collection_plex_to_trakt: bool | None = None
    watchlist_plex_to_trakt: bool | None = None
    watchlist_trakt_to_plex: bool | None = None
    movie_libraries: list[str] | None = None
    show_libraries: list[str] | None = None
    # Scheduler
    scheduler_enabled: bool | None = None
    scheduler_interval_hours: int | None = None


class SyncRequest(BaseModel):
    """Sync request options."""

    dry_run: bool = False
    verbose: bool = False


async def _scheduled_sync() -> None:
    """Run sync for scheduler (no dry run, no UI feedback)."""
    global sync_state
    if sync_state["running"]:
        return

    sync_state["running"] = True
    sync_state["cancelled"] = False
    sync_state["logs"] = ["Scheduled sync started"]

    try:
        config = Config.load()

        def on_token_refresh(token: dict):
            config.trakt.access_token = token["access_token"]
            config.trakt.refresh_token = token["refresh_token"]
            config.trakt.expires_at = token["created_at"] + token["expires_in"]
            config.save()

        result = await run_sync(
            config,
            dry_run=False,
            on_token_refresh=on_token_refresh,
            log_callback=lambda msg: sync_state["logs"].append(msg),
            cancel_check=lambda: sync_state["cancelled"],
        )

        if result:
            sync_state["last_result"] = {
                "added_to_trakt": result.added_to_trakt,
                "added_to_plex": result.added_to_plex,
                "ratings_synced": result.ratings_synced,
                "collection_added": result.collection_added,
                "watchlist_added_trakt": result.watchlist_added_trakt,
                "watchlist_added_plex": result.watchlist_added_plex,
                "duration": result.duration_seconds,
                "errors": result.errors[:10],
            }
            sync_state["last_run"] = datetime.now().isoformat()
    except Exception as e:
        sync_state["logs"].append(f"ERROR:{str(e)}")
        sync_state["last_result"] = {"error": str(e)}
    finally:
        sync_state["running"] = False
        sync_state["cancelled"] = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global _scheduler

    config = Config.load()
    if config.scheduler.enabled and config.scheduler.interval_hours > 0:
        try:
            from pakt.scheduler import SyncScheduler

            _scheduler = SyncScheduler(
                config,
                sync_func=_scheduled_sync,
                is_running_func=lambda: sync_state["running"],
            )
            _scheduler.start()
        except ImportError:
            pass  # APScheduler not installed

    yield

    if _scheduler:
        _scheduler.stop()


def create_app() -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(
        title="Pakt",
        description="Plex-Trakt sync",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Templates and assets
    template_dir = Path(__file__).parent / "templates"
    templates = Jinja2Templates(directory=str(template_dir))
    assets_dir = Path(__file__).parent.parent / "assets"

    # =========================================================================
    # Web UI Routes
    # =========================================================================

    @app.get("/favicon.ico")
    async def favicon():
        """Serve favicon."""
        icon_path = assets_dir / "icon.png"
        if icon_path.exists():
            return FileResponse(icon_path, media_type="image/png")
        return FileResponse(assets_dir / "icon.svg", media_type="image/svg+xml")

    @app.get("/assets/{filename}")
    async def serve_asset(filename: str):
        """Serve static assets."""
        file_path = assets_dir / filename
        if file_path.exists() and file_path.is_file():
            media_type = "image/png" if filename.endswith(".png") else "image/svg+xml"
            return FileResponse(file_path, media_type=media_type)
        return HTMLResponse(status_code=404, content="Not found")

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
                "config_dir": str(get_config_dir()),
            },
        )

    # =========================================================================
    # API Routes
    # =========================================================================

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        """Get current status."""
        config = Config.load()

        return {
            "trakt_configured": bool(config.trakt.client_id),
            "trakt_authenticated": bool(config.trakt.access_token),
            "plex_configured": bool(config.plex.url and config.plex.token),
            "sync_running": sync_state["running"],
            "last_run": sync_state["last_run"],
            "last_result": sync_state["last_result"],
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
                "collection_plex_to_trakt": config.sync.collection_plex_to_trakt,
                "watchlist_plex_to_trakt": config.sync.watchlist_plex_to_trakt,
                "watchlist_trakt_to_plex": config.sync.watchlist_trakt_to_plex,
                "movie_libraries": config.sync.movie_libraries,
                "show_libraries": config.sync.show_libraries,
            },
            "scheduler": {
                "enabled": config.scheduler.enabled,
                "interval_hours": config.scheduler.interval_hours,
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
        if update.collection_plex_to_trakt is not None:
            config.sync.collection_plex_to_trakt = update.collection_plex_to_trakt
        if update.watchlist_plex_to_trakt is not None:
            config.sync.watchlist_plex_to_trakt = update.watchlist_plex_to_trakt
        if update.watchlist_trakt_to_plex is not None:
            config.sync.watchlist_trakt_to_plex = update.watchlist_trakt_to_plex
        if update.movie_libraries is not None:
            config.sync.movie_libraries = update.movie_libraries
        if update.show_libraries is not None:
            config.sync.show_libraries = update.show_libraries
        if update.scheduler_enabled is not None:
            config.scheduler.enabled = update.scheduler_enabled
        if update.scheduler_interval_hours is not None:
            config.scheduler.interval_hours = update.scheduler_interval_hours

        config.save()

        # Update scheduler if settings changed
        global _scheduler
        if update.scheduler_enabled is not None or update.scheduler_interval_hours is not None:
            if _scheduler:
                _scheduler.update_config(
                    config.scheduler.enabled,
                    config.scheduler.interval_hours,
                )
            elif config.scheduler.enabled and config.scheduler.interval_hours > 0:
                try:
                    from pakt.scheduler import SyncScheduler

                    _scheduler = SyncScheduler(
                        config,
                        sync_func=_scheduled_sync,
                        is_running_func=lambda: sync_state["running"],
                    )
                    _scheduler.start()
                except ImportError:
                    pass

        return {"status": "ok"}

    @app.post("/api/sync")
    async def start_sync(request: SyncRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
        """Start a sync operation."""
        if sync_state["running"]:
            return {"status": "error", "message": "Sync already running"}

        def log(msg: str):
            sync_state["logs"].append(msg)

        def is_cancelled() -> bool:
            return sync_state["cancelled"]

        async def do_sync():
            sync_state["running"] = True
            sync_state["cancelled"] = False
            sync_state["logs"] = []
            try:
                config = Config.load()

                def on_token_refresh(token: dict):
                    config.trakt.access_token = token["access_token"]
                    config.trakt.refresh_token = token["refresh_token"]
                    config.trakt.expires_at = token["created_at"] + token["expires_in"]
                    config.save()
                    log("Token refreshed")

                log("Loading configuration...")
                log(f"Sync options: Plex→Trakt watched={config.sync.watched_plex_to_trakt}, Trakt→Plex watched={config.sync.watched_trakt_to_plex}")
                log(f"Sync options: Plex→Trakt ratings={config.sync.ratings_plex_to_trakt}, Trakt→Plex ratings={config.sync.ratings_trakt_to_plex}")

                result = await run_sync(config, dry_run=request.dry_run, verbose=request.verbose, on_token_refresh=on_token_refresh, log_callback=log, cancel_check=is_cancelled)

                if result is None:
                    log("WARNING:Sync cancelled")
                    sync_state["last_result"] = {"cancelled": True}
                else:
                    log("SUCCESS:Sync complete!")
                    log(f"DETAIL:Watched - Added to Trakt: {result.added_to_trakt}")
                    log(f"DETAIL:Watched - Added to Plex: {result.added_to_plex}")
                    log(f"DETAIL:Ratings synced: {result.ratings_synced}")
                    if result.collection_added:
                        log(f"DETAIL:Collection added: {result.collection_added}")
                    if result.watchlist_added_trakt or result.watchlist_added_plex:
                        log(f"DETAIL:Watchlist - Added to Trakt: {result.watchlist_added_trakt}, Plex: {result.watchlist_added_plex}")
                    log(f"DETAIL:Duration: {result.duration_seconds:.1f}s")

                    sync_state["last_result"] = {
                        "added_to_trakt": result.added_to_trakt,
                        "added_to_plex": result.added_to_plex,
                        "ratings_synced": result.ratings_synced,
                        "collection_added": result.collection_added,
                        "watchlist_added_trakt": result.watchlist_added_trakt,
                        "watchlist_added_plex": result.watchlist_added_plex,
                        "duration": result.duration_seconds,
                        "errors": result.errors[:10],
                    }
                    sync_state["last_run"] = datetime.now().isoformat()
            except Exception as e:
                log(f"ERROR:{str(e)}")
                sync_state["last_result"] = {"error": str(e)}
            finally:
                sync_state["running"] = False
                sync_state["cancelled"] = False

        background_tasks.add_task(do_sync)
        return {"status": "started"}

    @app.get("/api/sync/status")
    async def get_sync_status() -> dict[str, Any]:
        """Get sync status."""
        return {
            "running": sync_state["running"],
            "last_run": sync_state["last_run"],
            "last_result": sync_state["last_result"],
            "logs": sync_state["logs"],
        }

    @app.post("/api/sync/cancel")
    async def cancel_sync() -> dict[str, Any]:
        """Cancel a running sync."""
        if not sync_state["running"]:
            return {"status": "error", "message": "No sync running"}
        sync_state["cancelled"] = True
        return {"status": "ok"}

    @app.get("/api/scheduler/status")
    async def get_scheduler_status() -> dict[str, Any]:
        """Get scheduler status."""
        global _scheduler
        config = Config.load()
        if _scheduler:
            return _scheduler.get_status()
        return {
            "enabled": config.scheduler.enabled,
            "interval_hours": config.scheduler.interval_hours,
            "next_run": None,
            "last_run": None,
        }

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
        from pakt.trakt import DeviceAuthStatus, TraktClient

        config = Config.load()
        async with TraktClient(config.trakt) as client:
            result = await client.poll_device_token(device_code, interval=5, expires_in=30)
            if result.status == DeviceAuthStatus.SUCCESS and result.token:
                config.trakt.access_token = result.token["access_token"]
                config.trakt.refresh_token = result.token["refresh_token"]
                config.trakt.expires_at = result.token["created_at"] + result.token["expires_in"]
                config.save()
                return {"status": "authenticated"}
            elif result.status == DeviceAuthStatus.PENDING:
                return {"status": "pending"}
            else:
                return {"status": "error", "message": result.message}

    @app.post("/api/trakt/logout")
    async def logout_trakt() -> dict[str, Any]:
        """Revoke Trakt token and clear authentication."""
        from pakt.trakt import TraktClient

        config = Config.load()
        if not config.trakt.access_token:
            return {"status": "ok", "message": "Not logged in"}

        async with TraktClient(config.trakt) as client:
            success = await client.revoke_token()

        # Clear local tokens regardless of revocation success
        config.trakt.access_token = ""
        config.trakt.refresh_token = ""
        config.trakt.expires_at = 0
        config.save()

        return {"status": "ok", "revoked": success}

    @app.get("/api/trakt/account")
    async def get_trakt_account() -> dict[str, Any]:
        """Get Trakt account info including VIP status and limits."""
        from pakt.trakt import TraktClient

        config = Config.load()
        if not config.trakt.access_token:
            return {"status": "error", "message": "Not authenticated"}

        try:
            async with TraktClient(config.trakt) as client:
                limits = await client.get_account_limits()
                return {
                    "status": "ok",
                    "is_vip": limits.is_vip,
                    "limits": {
                        "collection": limits.collection_limit,
                        "watchlist": limits.watchlist_limit,
                        "lists": limits.list_limit,
                        "list_items": limits.list_item_limit,
                    },
                }
        except Exception as e:
            return {"status": "error", "message": str(e)}

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

    @app.get("/api/plex/libraries")
    async def get_plex_libraries() -> dict[str, Any]:
        """Get available Plex libraries and current selection."""
        from pakt.plex import PlexClient

        try:
            config = Config.load()
            plex = PlexClient(config.plex)
            plex.connect()
            return {
                "status": "ok",
                "available": {
                    "movie": plex.get_movie_libraries(),
                    "show": plex.get_show_libraries(),
                },
                "selected": {
                    "movie": config.sync.movie_libraries,
                    "show": config.sync.show_libraries,
                },
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @app.post("/api/shutdown")
    async def shutdown_server() -> dict[str, str]:
        """Shutdown the web server."""
        async def do_shutdown():
            await asyncio.sleep(0.5)
            os._exit(0)

        asyncio.create_task(do_shutdown())
        return {"status": "ok", "message": "Server shutting down..."}

    return app
