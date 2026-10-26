"""Plex routes: PIN login, server discovery, and a connection test on the first server."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request

from pakt.config import Config, ServerConfig
from pakt.web import state
from pakt.web.api.servers import check_connection, list_libraries

router = APIRouter()

PIN_MAX_AGE = 600  # Plex PINs expire after 10 minutes
NO_SERVERS = {"status": "error", "message": "No Plex servers configured"}


def _cleanup_expired_pins(pins: dict[int, tuple[float, Any]]) -> None:
    """Clean up PIN logins older than PIN_MAX_AGE."""
    now = time.time()
    expired = [pid for pid, (ts, _) in pins.items() if now - ts > PIN_MAX_AGE]
    for pid in expired:
        del pins[pid]


def _first_server(config: Config) -> ServerConfig | None:
    """First enabled server, else the first configured one, or None when there are none."""
    return next(iter(config.get_enabled_servers() or config.servers), None)


@router.post("/api/plex/test")
def test_plex_connection() -> dict[str, Any]:
    """Test the Plex connection to the first enabled server, else the first server."""
    config = Config.load()
    server = _first_server(config)
    return check_connection(server, config.plex_token) if server else NO_SERVERS


@router.get("/api/plex/libraries")
def get_plex_libraries() -> dict[str, Any]:
    """Get the first enabled server's available libraries and selection, else the first server's."""
    config = Config.load()
    server = _first_server(config)
    if not server:
        return NO_SERVERS
    try:
        available = list_libraries(server, config.plex_token)
    except Exception as e:
        return {"status": "error", "message": str(e)}
    return {
        "status": "ok",
        "available": available,
        "selected": {"movie": server.movie_libraries, "show": server.show_libraries},
    }


@router.post("/api/plex/pin")
def start_plex_pin_login(request: Request) -> dict[str, Any]:
    """Start Plex PIN login, keeping the login object for polling under its PIN ID."""
    from pakt.plex.account import start_plex_pin_login as _start_pin

    pins: dict[int, tuple[float, Any]] = request.app.state.plex_pins
    _cleanup_expired_pins(pins)

    try:
        login_obj, auth_info = _start_pin()
        pins[auth_info.pin_id] = (time.time(), login_obj)
        return {
            "status": "ok",
            "pin": auth_info.pin,
            "pin_id": auth_info.pin_id,
            "verification_url": auth_info.verification_url,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.post("/api/plex/pin/{pin_id}")
def check_plex_pin_login(pin_id: int, request: Request) -> dict[str, Any]:
    """Check if Plex PIN login has been authorized, and save the Plex token when it has."""
    from pakt.plex.account import check_plex_pin_login as _check_pin

    pins: dict[int, tuple[float, Any]] = request.app.state.plex_pins
    entry = pins.get(pin_id)
    if not entry:
        return {"status": "error", "message": "PIN login not found or expired"}

    try:
        _, login_obj = entry
        token = _check_pin(login_obj)
        if token:
            del pins[pin_id]
            config = Config.load()
            config.plex_token = token
            config.save()
            state.invalidate_config_cache()
            return {"status": "authenticated"}
        else:
            return {"status": "pending"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/plex/discover")
def discover_plex_servers() -> dict[str, Any]:
    """Discover Plex servers from the account, marking the ones already configured."""
    from pakt.plex.account import discover_servers

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
