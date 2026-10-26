"""Trakt routes: device login, logout and the account's VIP status and limits."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from pakt.config import Config
from pakt.web import state

router = APIRouter()


@router.post("/api/trakt/auth")
async def start_trakt_auth(request: Request) -> dict[str, Any]:
    """Start a Trakt device login, replacing any pending one, and return the code to enter."""
    from pakt.trakt import TraktClient

    config = Config.load()
    if not config.trakt.client_id:
        return {"error": "Trakt client_id not configured"}

    device = await TraktClient(config.trakt).device_code()
    pending: dict[str, Any] = request.app.state.trakt_device
    pending.clear()
    pending.update(device)
    return {
        "verification_url": device["verification_url"],
        "user_code": device["user_code"],
        "expires_in": device["expires_in"],
        "interval": device.get("interval", 5),
    }


@router.post("/api/trakt/auth/poll")
async def poll_trakt_auth(request: Request) -> dict[str, Any]:
    """Poll the pending Trakt login once and save the tokens when the user has approved it."""
    from pakt.trakt import DeviceAuthStatus, TraktClient

    pending: dict[str, Any] = request.app.state.trakt_device
    if not pending:
        return {"status": "error", "message": "No Trakt login in progress"}

    config = Config.load()
    result = await TraktClient(config.trakt).check_device_token(pending["device_code"])
    if result.status in (DeviceAuthStatus.PENDING, DeviceAuthStatus.RATE_LIMITED):
        return {"status": "pending"}
    pending.clear()
    if result.status == DeviceAuthStatus.SUCCESS and result.token:
        config.save_trakt_token(result.token)
        state.invalidate_config_cache()
        state.drop_cached("trakt_account")
        return {"status": "authenticated"}
    return {"status": "error", "message": result.message}


@router.post("/api/trakt/logout")
async def logout_trakt() -> dict[str, Any]:
    """Log out of Trakt, revoking the token and clearing the stored tokens."""
    from pakt.trakt import TraktClient

    config = Config.load()
    if not config.trakt.access_token:
        return {"status": "ok", "message": "Not logged in"}

    success = await TraktClient(config.trakt).revoke_token()

    config.trakt.access_token = ""
    config.trakt.refresh_token = ""
    config.trakt.expires_at = 0
    config.save()
    state.invalidate_config_cache()

    return {"status": "ok", "revoked": success}


@router.get("/api/trakt/account")
async def get_trakt_account() -> dict[str, Any]:
    """Get Trakt account info including VIP status and limits."""
    from pakt.trakt import TraktClient

    cached = state.get_cached("trakt_account")
    if cached:
        return cached

    config = Config.load()
    if not config.trakt.access_token:
        return {"status": "error", "message": "Not authenticated"}

    try:
        async with TraktClient(config.trakt, on_token_refresh=state.token_saver(config)) as client:
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
            state.set_cached("trakt_account", result)
            return result
    except Exception as e:
        return {"status": "error", "message": str(e)}
