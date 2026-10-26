"""Server routes: list, add, change and remove Plex servers, and test or list each one."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from pakt.config import Config, ServerConfig
from pakt.web import state

router = APIRouter()


class SyncOverrideUpdate(BaseModel):
    """Per-server sync option overrides, where None falls back to the global setting."""

    watched_plex_to_trakt: bool | None = None
    watched_trakt_to_plex: bool | None = None
    ratings_plex_to_trakt: bool | None = None
    ratings_trakt_to_plex: bool | None = None
    collection_plex_to_trakt: bool | None = None
    watchlist_plex_to_trakt: bool | None = None
    watchlist_trakt_to_plex: bool | None = None


class ServerUpdate(BaseModel):
    """Changes to one server's enabled flag, libraries and sync overrides."""

    enabled: bool | None = None
    movie_libraries: list[str] | None = None
    show_libraries: list[str] | None = None
    sync: SyncOverrideUpdate | None = None


class ServerCreate(BaseModel):
    """A server to add, by URL and token or by its name on the Plex account."""

    name: str
    url: str | None = None
    token: str | None = None
    server_name: str | None = None  # for discovered servers


@router.get("/api/servers")
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


@router.post("/api/servers")
async def add_server(request: ServerCreate) -> dict[str, Any]:
    """Add a Plex server, found through the account or entered by hand."""
    from pakt.plex.account import discover_servers

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
            token=matching.access_token,
            enabled=True,
        )
    else:
        return {"status": "error", "message": "Provide url+token or server_name"}

    config.servers.append(new_server)
    config.save()
    state.invalidate_config_cache()
    return {"status": "ok", "message": f"Added server: {request.name}"}


@router.put("/api/servers/{name}")
async def update_server(name: str, request: ServerUpdate) -> dict[str, Any]:
    """Update a server's enabled flag, libraries and sync overrides."""
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
        if server.sync is None:
            server.sync = ServerSyncOverrides()
        for field in [
            "watched_plex_to_trakt", "watched_trakt_to_plex",
            "ratings_plex_to_trakt", "ratings_trakt_to_plex",
            "collection_plex_to_trakt",
            "watchlist_plex_to_trakt", "watchlist_trakt_to_plex",
        ]:
            val = getattr(request.sync, field, None)
            # None clears the override back to the global setting
            setattr(server.sync, field, val)

    config.save()
    state.invalidate_config_cache()
    return {"status": "ok"}


@router.delete("/api/servers/{name}")
async def delete_server(name: str) -> dict[str, Any]:
    """Delete a configured server and save the config."""
    config = Config.load()

    if not config.get_server(name):
        return {"status": "error", "message": f"Server '{name}' not found"}

    config.servers = [s for s in config.servers if s.name != name]
    config.save()
    state.invalidate_config_cache()
    return {"status": "ok", "message": f"Removed server: {name}"}


@router.post("/api/servers/{name}/test")
def test_server(name: str) -> dict[str, Any]:
    """Test connection to a specific server."""
    from pakt.plex.client import PlexClient

    config = Config.load()
    server = config.get_server(name)

    if not server:
        return {"status": "error", "message": f"Server '{name}' not found"}

    try:
        plex = PlexClient(server, account_token=config.plex_token)
        plex.connect()
        return {
            "status": "ok",
            "server_name": plex.server.friendlyName,
            "movie_libraries": plex.get_movie_libraries(),
            "show_libraries": plex.get_show_libraries(),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/servers/{name}/libraries")
def get_server_libraries(name: str) -> dict[str, Any]:
    """Get a server's available libraries and the ones selected to sync."""
    from pakt.plex.client import PlexClient

    config = Config.load()
    server = config.get_server(name)

    if not server:
        return {"status": "error", "message": f"Server '{name}' not found"}

    # only the available list is cached; the selection always comes from config
    cache_key = f"plex_libraries_{name}"
    cached_available = state.get_cached(cache_key)

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
        plex = PlexClient(server, account_token=config.plex_token)
        plex.connect()
        available = {
            "movie": plex.get_movie_libraries(),
            "show": plex.get_show_libraries(),
        }
        state.set_cached(cache_key, available)
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
