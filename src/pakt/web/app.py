"""FastAPI application for Pakt web interface."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from pakt.config import Config, ServerConfig, get_config_dir
from pakt.sync import run_multi_server_sync

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

# Cache for expensive API calls (Trakt account, Plex libraries)
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 300  # 5 minutes
CONFIG_CACHE_TTL = 2  # 2 seconds for config (avoids repeated disk reads during page load)

_config_cache: tuple[float, Config | None] = (0, None)


def get_cached(key: str) -> Any | None:
    """Get cached value if not expired."""
    if key in _cache:
        ts, value = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return value
    return None


def set_cached(key: str, value: Any) -> None:
    """Cache a value."""
    _cache[key] = (time.time(), value)


def load_config_cached() -> Config:
    """Get config with short-term caching to avoid repeated disk reads."""
    global _config_cache
    ts, config = _config_cache
    if config is not None and time.time() - ts < CONFIG_CACHE_TTL:
        return config
    config = Config.load()
    _config_cache = (time.time(), config)
    return config


def invalidate_config_cache() -> None:
    """Invalidate config cache after saves."""
    global _config_cache
    _config_cache = (0, None)


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
    # Scheduler
    scheduler_enabled: bool | None = None
    scheduler_interval_hours: int | None = None


class SyncRequest(BaseModel):
    """Sync request options."""

    dry_run: bool = False
    verbose: bool = False
    servers: list[str] | None = None  # Optional list of server names to sync


class SyncOverrideUpdate(BaseModel):
    """Per-server sync option overrides. None = use global."""

    watched_plex_to_trakt: bool | None = None
    watched_trakt_to_plex: bool | None = None
    ratings_plex_to_trakt: bool | None = None
    ratings_trakt_to_plex: bool | None = None
    collection_plex_to_trakt: bool | None = None
    watchlist_plex_to_trakt: bool | None = None
    watchlist_trakt_to_plex: bool | None = None


class ServerUpdate(BaseModel):
    """Server configuration update."""

    enabled: bool | None = None
    movie_libraries: list[str] | None = None
    show_libraries: list[str] | None = None
    sync: SyncOverrideUpdate | None = None


class ServerCreate(BaseModel):
    """Create server request."""

    name: str
    url: str | None = None
    token: str | None = None
    server_name: str | None = None  # For discovered servers


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

        result = await run_multi_server_sync(
            config,
            dry_run=False,
            on_token_refresh=on_token_refresh,
            log_callback=lambda msg: sync_state["logs"].append(msg),
            cancel_check=lambda: sync_state["cancelled"],
        )

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
        config = load_config_cached()

        return {
            "trakt_configured": bool(config.trakt.client_id),
            "trakt_authenticated": bool(config.trakt.access_token),
            "plex_configured": bool(config.servers),
            "sync_running": sync_state["running"],
            "last_run": sync_state["last_run"],
            "last_result": sync_state["last_result"],
        }

    @app.get("/api/init")
    async def get_init() -> dict[str, Any]:
        """Get all data needed for page load in one request."""
        from pakt.plex import PlexClient
        from pakt.trakt import TraktClient

        config = load_config_cached()

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

        # Add servers with library info
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

            # Get libraries (cached)
            cache_key = f"plex_libraries_{s.name}"
            cached_libs = get_cached(cache_key)
            if cached_libs:
                server_data["libraries"] = cached_libs
            else:
                try:
                    plex = PlexClient(s)
                    plex.connect()
                    libs = {
                        "movie": plex.get_movie_libraries(),
                        "show": plex.get_show_libraries(),
                    }
                    set_cached(cache_key, libs)
                    server_data["libraries"] = libs
                except Exception:
                    server_data["libraries"] = {"movie": [], "show": []}

            result["servers"].append(server_data)

        # Get Trakt account info (cached)
        if config.trakt.access_token:
            cached_trakt = get_cached("trakt_account")
            if cached_trakt:
                result["trakt_account"] = cached_trakt
            else:
                try:
                    async with TraktClient(config.trakt) as client:
                        limits = await client.get_account_limits()
                        trakt_data = {
                            "status": "ok",
                            "is_vip": limits.is_vip,
                            "limits": {
                                "collection": limits.collection_limit,
                                "watchlist": limits.watchlist_limit,
                            },
                        }
                        set_cached("trakt_account", trakt_data)
                        result["trakt_account"] = trakt_data
                except Exception:
                    result["trakt_account"] = {"status": "error"}

        return result

    @app.get("/api/config")
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
                w_p2t = config.sync.watched_plex_to_trakt
                w_t2p = config.sync.watched_trakt_to_plex
                log(f"Sync options: Plex→Trakt watched={w_p2t}, Trakt→Plex watched={w_t2p}")
                r_p2t = config.sync.ratings_plex_to_trakt
                r_t2p = config.sync.ratings_trakt_to_plex
                log(f"Sync options: Plex→Trakt ratings={r_p2t}, Trakt→Plex ratings={r_t2p}")

                result = await run_multi_server_sync(
                    config,
                    server_names=request.servers,
                    dry_run=request.dry_run,
                    verbose=request.verbose,
                    on_token_refresh=on_token_refresh,
                    log_callback=log,
                    cancel_check=is_cancelled,
                )

                if sync_state["cancelled"]:
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
                        wl_trakt = result.watchlist_added_trakt
                        wl_plex = result.watchlist_added_plex
                        log(f"DETAIL:Watchlist - Added to Trakt: {wl_trakt}, Plex: {wl_plex}")
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

        # Check cache first
        cached = get_cached("trakt_account")
        if cached:
            return cached

        config = Config.load()
        if not config.trakt.access_token:
            return {"status": "error", "message": "Not authenticated"}

        try:
            async with TraktClient(config.trakt) as client:
                limits = await client.get_account_limits()
                result = {
                    "status": "ok",
                    "is_vip": limits.is_vip,
                    "limits": {
                        "collection": limits.collection_limit,
                        "watchlist": limits.watchlist_limit,
                        "lists": limits.list_limit,
                        "list_items": limits.list_item_limit,
                    },
                }
                set_cached("trakt_account", result)
                return result
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @app.post("/api/plex/test")
    async def test_plex_connection() -> dict[str, Any]:
        """Test Plex connection (first server)."""
        from pakt.plex import PlexClient

        config = Config.load()

        try:
            if not config.servers:
                return {"status": "error", "message": "No Plex servers configured"}

            server = config.get_enabled_servers()[0] if config.get_enabled_servers() else config.servers[0]
            plex = PlexClient(server)
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
        """Get available Plex libraries and current selection (first server)."""
        from pakt.plex import PlexClient

        try:
            config = Config.load()

            if not config.servers:
                return {"status": "error", "message": "No Plex servers configured"}

            server = config.get_enabled_servers()[0] if config.get_enabled_servers() else config.servers[0]
            plex = PlexClient(server)
            plex.connect()
            return {
                "status": "ok",
                "available": {
                    "movie": plex.get_movie_libraries(),
                    "show": plex.get_show_libraries(),
                },
                "selected": {
                    "movie": server.movie_libraries,
                    "show": server.show_libraries,
                },
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # =========================================================================
    # Plex PIN Authentication
    # =========================================================================

    # Store active PIN logins (pin_id -> login object)
    _plex_pin_logins: dict[int, Any] = {}

    @app.post("/api/plex/pin")
    async def start_plex_pin_login() -> dict[str, Any]:
        """Start Plex PIN login flow."""
        from pakt.plex import start_plex_pin_login as _start_pin

        try:
            login_obj, auth_info = _start_pin()
            _plex_pin_logins[auth_info.pin_id] = login_obj
            return {
                "status": "ok",
                "pin": auth_info.pin,
                "pin_id": auth_info.pin_id,
                "verification_url": auth_info.verification_url,
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @app.get("/api/plex/pin/{pin_id}")
    async def check_plex_pin_login(pin_id: int) -> dict[str, Any]:
        """Check if Plex PIN login has been authorized."""
        from pakt.plex import check_plex_pin_login as _check_pin

        login_obj = _plex_pin_logins.get(pin_id)
        if not login_obj:
            return {"status": "error", "message": "PIN login not found or expired"}

        try:
            token = _check_pin(login_obj)
            if token:
                del _plex_pin_logins[pin_id]
                config = Config.load()
                config.plex_token = token
                config.save()
                return {"status": "authenticated", "token": token[:10] + "..."}
            else:
                return {"status": "pending"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @app.get("/api/plex/discover")
    async def discover_plex_servers() -> dict[str, Any]:
        """Discover Plex servers from account."""
        from pakt.plex import discover_servers

        config = Config.load()
        if not config.plex_token:
            return {"status": "error", "message": "No Plex account token"}

        try:
            discovered = discover_servers(config.plex_token)
            return {
                "status": "ok",
                "servers": [
                    {
                        "name": s.name,
                        "owned": s.owned,
                        "has_local": s.has_local_connection,
                        "url": s.best_connection_url,
                        "configured": config.get_server(s.name) is not None,
                    }
                    for s in discovered
                ],
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # =========================================================================
    # Server Management
    # =========================================================================

    @app.get("/api/servers")
    async def get_servers() -> dict[str, Any]:
        """Get configured Plex servers."""
        config = Config.load()
        servers = []
        for s in config.servers:
            server_data = {
                "name": s.name,
                "server_name": s.server_name,
                "url": s.url,
                "enabled": s.enabled,
                "movie_libraries": s.movie_libraries,
                "show_libraries": s.show_libraries,
                "sync": None,
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
            servers.append(server_data)
        return {
            "status": "ok",
            "has_account_token": bool(config.plex_token),
            "servers": servers,
            "global_sync": {
                "watched_plex_to_trakt": config.sync.watched_plex_to_trakt,
                "watched_trakt_to_plex": config.sync.watched_trakt_to_plex,
                "ratings_plex_to_trakt": config.sync.ratings_plex_to_trakt,
                "ratings_trakt_to_plex": config.sync.ratings_trakt_to_plex,
                "collection_plex_to_trakt": config.sync.collection_plex_to_trakt,
                "watchlist_plex_to_trakt": config.sync.watchlist_plex_to_trakt,
                "watchlist_trakt_to_plex": config.sync.watchlist_trakt_to_plex,
            },
        }

    @app.post("/api/servers")
    async def add_server(request: ServerCreate) -> dict[str, Any]:
        """Add a Plex server."""
        from pakt.plex import discover_servers

        config = Config.load()

        if config.get_server(request.name):
            return {"status": "error", "message": f"Server '{request.name}' already exists"}

        if request.url and request.token:
            new_server = ServerConfig(
                name=request.name,
                url=request.url,
                token=request.token,
                enabled=True,
            )
        elif request.server_name:
            if not config.plex_token:
                return {"status": "error", "message": "No account token for discovery"}

            try:
                discovered = discover_servers(config.plex_token)
            except Exception as e:
                return {"status": "error", "message": f"Discovery failed: {e}"}

            matching = next((s for s in discovered if s.name == request.server_name), None)
            if not matching:
                return {"status": "error", "message": f"Server '{request.server_name}' not found"}

            new_server = ServerConfig(
                name=request.name,
                server_name=matching.name,
                url=matching.best_connection_url or "",
                token=config.plex_token,
                enabled=True,
            )
        else:
            return {"status": "error", "message": "Provide url+token or server_name"}

        config.servers.append(new_server)
        config.save()
        return {"status": "ok", "message": f"Added server: {request.name}"}

    @app.put("/api/servers/{name}")
    async def update_server(name: str, request: ServerUpdate) -> dict[str, Any]:
        """Update server configuration."""
        from pakt.config import ServerSyncOverrides

        config = Config.load()
        server = config.get_server(name)

        if not server:
            return {"status": "error", "message": f"Server '{name}' not found"}

        if request.enabled is not None:
            server.enabled = request.enabled
        if request.movie_libraries is not None:
            server.movie_libraries = request.movie_libraries
        if request.show_libraries is not None:
            server.show_libraries = request.show_libraries
        if request.sync is not None:
            # Update sync overrides
            if server.sync is None:
                server.sync = ServerSyncOverrides()
            for field in [
                "watched_plex_to_trakt", "watched_trakt_to_plex",
                "ratings_plex_to_trakt", "ratings_trakt_to_plex",
                "collection_plex_to_trakt",
                "watchlist_plex_to_trakt", "watchlist_trakt_to_plex",
            ]:
                val = getattr(request.sync, field, None)
                # Allow explicit None to clear override (use global)
                setattr(server.sync, field, val)

        config.save()
        return {"status": "ok"}

    @app.delete("/api/servers/{name}")
    async def delete_server(name: str) -> dict[str, Any]:
        """Remove a configured server."""
        config = Config.load()

        if not config.get_server(name):
            return {"status": "error", "message": f"Server '{name}' not found"}

        config.servers = [s for s in config.servers if s.name != name]
        config.save()
        return {"status": "ok", "message": f"Removed server: {name}"}

    @app.post("/api/servers/{name}/test")
    async def test_server(name: str) -> dict[str, Any]:
        """Test connection to a specific server."""
        from pakt.plex import PlexClient

        config = Config.load()
        server = config.get_server(name)

        if not server:
            return {"status": "error", "message": f"Server '{name}' not found"}

        try:
            plex = PlexClient(server)
            plex.connect()
            return {
                "status": "ok",
                "server_name": plex.server.friendlyName,
                "movie_libraries": plex.get_movie_libraries(),
                "show_libraries": plex.get_show_libraries(),
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @app.get("/api/servers/{name}/libraries")
    async def get_server_libraries(name: str) -> dict[str, Any]:
        """Get libraries for a specific server."""
        from pakt.plex import PlexClient

        config = Config.load()
        server = config.get_server(name)

        if not server:
            return {"status": "error", "message": f"Server '{name}' not found"}

        # Check cache for available libraries (selected comes from config)
        cache_key = f"plex_libraries_{name}"
        cached_available = get_cached(cache_key)

        if cached_available:
            return {
                "status": "ok",
                "available": cached_available,
                "selected": {
                    "movie": server.movie_libraries,
                    "show": server.show_libraries,
                },
            }

        try:
            plex = PlexClient(server)
            plex.connect()
            available = {
                "movie": plex.get_movie_libraries(),
                "show": plex.get_show_libraries(),
            }
            set_cached(cache_key, available)
            return {
                "status": "ok",
                "available": available,
                "selected": {
                    "movie": server.movie_libraries,
                    "show": server.show_libraries,
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
