"""Server routes: list, add, change and remove Plex servers, and test or list each one."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from pakt.config import Config, ServerConfig, ServerSyncOverrides
from pakt.web import state

router = APIRouter()

# the fields the web UI sees; the token stays out
SERVER_FIELDS = {"name", "server_name", "url", "enabled", "movie_libraries", "show_libraries", "sync"}


class ServerUpdate(BaseModel):
    """Changes to one server's enabled flag, libraries and sync overrides."""

    enabled: bool | None = None
    movie_libraries: list[str] | None = None
    show_libraries: list[str] | None = None
    sync: ServerSyncOverrides | None = None


class ServerCreate(BaseModel):
    """A server to add, by URL and token or by its name on the Plex account."""

    name: str
    url: str | None = None
    token: str | None = None
    server_name: str | None = None  # for discovered servers


def server_json(server: ServerConfig) -> dict[str, Any]:
    """Server as the web UI shows it, without its token."""
    return server.model_dump(include=SERVER_FIELDS)


def list_libraries(server: ServerConfig, account_token: str) -> dict[str, list[str]]:
    """List the server's movie and show libraries, connecting only when none are cached."""
    from pakt.plex.client import PlexClient

    cache_key = f"plex_libraries_{server.name}"
    cached = state.get_cached(cache_key)
    if cached:
        return cached
    plex = PlexClient(server, account_token=account_token)
    plex.connect()
    libraries = plex.get_library_names()
    state.set_cached(cache_key, libraries)
    return libraries


def check_connection(server: ServerConfig, account_token: str) -> dict[str, Any]:
    """Check a fresh connection to the server, reporting its name and libraries or the error."""
    from pakt.plex.client import PlexClient

    try:
        plex = PlexClient(server, account_token=account_token)
        plex.connect()
        libraries = plex.get_library_names()
        return {
            "status": "ok",
            "server_name": plex.server.friendlyName,
            "movie_libraries": libraries["movie"],
            "show_libraries": libraries["show"],
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/servers")
async def get_servers() -> dict[str, Any]:
    """Get configured Plex servers without their tokens, and the global sync settings."""
    config = Config.load()
    return {
        "status": "ok",
        "has_account_token": bool(config.plex_token),
        "servers": [server_json(s) for s in config.servers],
        "global_sync": config.sync.model_dump(exclude={"rating_priority"}),
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
    """Update a server's enabled flag, libraries and sync overrides; a None override clears it."""
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
        server.sync = request.sync

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
    config = Config.load()
    server = config.get_server(name)
    if not server:
        return {"status": "error", "message": f"Server '{name}' not found"}
    return check_connection(server, config.plex_token)


@router.get("/api/servers/{name}/libraries")
def get_server_libraries(name: str) -> dict[str, Any]:
    """Get a server's available libraries and the ones selected to sync."""
    config = Config.load()
    server = config.get_server(name)
    if not server:
        return {"status": "error", "message": f"Server '{name}' not found"}

    try:
        available = list_libraries(server, config.plex_token)
    except Exception as e:
        return {"status": "error", "message": str(e)}
    return {
        "status": "ok",
        "available": available,
        "selected": {"movie": server.movie_libraries, "show": server.show_libraries},
    }
