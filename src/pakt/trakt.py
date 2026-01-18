"""Trakt API client with batch operations."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

import httpx
from rich.console import Console

from pakt.config import TraktConfig
from pakt.models import RatedItem, TraktIds, WatchedItem

console = Console()

TRAKT_API_URL = "https://api.trakt.tv"
TRAKT_AUTH_URL = "https://trakt.tv"

# Refresh token 1 day before expiry (tokens valid for 7 days)
TOKEN_REFRESH_THRESHOLD = 24 * 60 * 60


class TraktRateLimitError(Exception):
    """Raised when rate limited."""

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Rate limited, retry after {retry_after}s")


class TraktAccountLimitError(Exception):
    """Raised when account limit exceeded (HTTP 420)."""

    def __init__(self, limit: int, is_vip: bool, upgrade_url: str = "https://trakt.tv/vip"):
        self.limit = limit
        self.is_vip = is_vip
        self.upgrade_url = upgrade_url
        msg = f"Account limit exceeded ({limit} items)"
        if not is_vip:
            msg += f". Upgrade to VIP: {upgrade_url}"
        super().__init__(msg)


@dataclass
class AccountLimits:
    """User account limits from Trakt."""

    is_vip: bool
    collection_limit: int
    watchlist_limit: int
    list_limit: int
    list_item_limit: int


class DeviceAuthStatus(Enum):
    """Device authentication polling status."""
    SUCCESS = "success"
    PENDING = "pending"
    INVALID_CODE = "invalid_code"
    EXPIRED = "expired"
    DENIED = "denied"
    RATE_LIMITED = "rate_limited"


@dataclass
class DeviceAuthResult:
    """Result of device authentication polling."""
    status: DeviceAuthStatus
    token: dict[str, Any] | None = None
    message: str = ""


class TraktClient:
    """Async Trakt API client optimized for batch operations."""

    def __init__(
        self,
        config: TraktConfig,
        on_token_refresh: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._on_token_refresh = on_token_refresh

    async def __aenter__(self) -> TraktClient:
        self._client = httpx.AsyncClient(
            base_url=TRAKT_API_URL,
            timeout=30.0,
            headers=self._headers,
        )
        # Check if token needs refresh on entry
        await self._ensure_valid_token()
        return self

    async def __aexit__(self, *args) -> None:
        if self._client:
            await self._client.aclose()

    def _token_needs_refresh(self) -> bool:
        """Check if access token needs refresh (within threshold of expiry)."""
        if not self.config.access_token or not self.config.refresh_token:
            return False
        if not self.config.expires_at:
            return False
        return time.time() >= (self.config.expires_at - TOKEN_REFRESH_THRESHOLD)

    async def _ensure_valid_token(self) -> None:
        """Refresh access token if it's about to expire."""
        if not self._token_needs_refresh():
            return

        console.print("[cyan]Refreshing Trakt access token...[/]")
        try:
            token = await self.refresh_access_token()
            self.config.access_token = token["access_token"]
            self.config.refresh_token = token["refresh_token"]
            self.config.expires_at = token["created_at"] + token["expires_in"]

            # Update client headers with new token
            if self._client:
                self._client.headers["Authorization"] = f"Bearer {token['access_token']}"

            # Notify caller to persist the new tokens
            if self._on_token_refresh:
                self._on_token_refresh(token)

            console.print("[green]Token refreshed successfully[/]")
        except Exception as e:
            console.print(f"[yellow]Token refresh failed: {e}[/]")

    @property
    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self.config.client_id,
        }
        if self.config.access_token:
            headers["Authorization"] = f"Bearer {self.config.access_token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        retries: int = 3,
        **kwargs,
    ) -> httpx.Response:
        """Make a request with rate limit handling."""
        if not self._client:
            raise RuntimeError("Client not initialized. Use async with.")

        for attempt in range(retries):
            try:
                response = await self._client.request(method, path, **kwargs)

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    console.print(
                        f"[yellow]Rate limited, waiting {retry_after}s "
                        f"(attempt {attempt + 1}/{retries})[/]"
                    )
                    await asyncio.sleep(retry_after + 1)
                    continue

                # Handle account limit exceeded (non-VIP limit)
                if response.status_code == 420:
                    is_vip = response.headers.get("X-VIP-User", "false").lower() == "true"
                    limit = int(response.headers.get("X-Account-Limit", 100))
                    upgrade_url = response.headers.get("X-Upgrade-URL", "https://trakt.tv/vip")
                    raise TraktAccountLimitError(limit, is_vip, upgrade_url)

                response.raise_for_status()
                return response

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < retries - 1:
                    continue
                raise

        raise TraktRateLimitError(60)

    # =========================================================================
    # BATCH READ OPERATIONS - Single call gets everything
    # =========================================================================

    async def get_watched_movies(self) -> list[WatchedItem]:
        """Get ALL watched movies in a single API call."""
        response = await self._request("GET", "/sync/watched/movies")
        return [WatchedItem(**item) for item in response.json()]

    async def get_watched_shows(self) -> list[WatchedItem]:
        """Get ALL watched shows in a single API call."""
        response = await self._request("GET", "/sync/watched/shows")
        return [WatchedItem(**item) for item in response.json()]

    async def get_movie_ratings(self) -> list[RatedItem]:
        """Get ALL movie ratings in a single API call."""
        response = await self._request("GET", "/sync/ratings/movies")
        return [RatedItem(**item) for item in response.json()]

    async def get_show_ratings(self) -> list[RatedItem]:
        """Get ALL show ratings in a single API call."""
        response = await self._request("GET", "/sync/ratings/shows")
        return [RatedItem(**item) for item in response.json()]

    async def get_episode_ratings(self) -> list[RatedItem]:
        """Get ALL episode ratings in a single API call."""
        response = await self._request("GET", "/sync/ratings/episodes")
        return [RatedItem(**item) for item in response.json()]

    async def get_collection_movies(self) -> list[dict[str, Any]]:
        """Get ALL collected movies in a single API call."""
        response = await self._request("GET", "/sync/collection/movies")
        return response.json()

    async def get_collection_shows(self) -> list[dict[str, Any]]:
        """Get ALL collected shows in a single API call."""
        response = await self._request("GET", "/sync/collection/shows")
        return response.json()

    async def get_watchlist_movies(self) -> list[dict[str, Any]]:
        """Get ALL watchlist movies in a single API call."""
        response = await self._request("GET", "/sync/watchlist/movies")
        return response.json()

    async def get_watchlist_shows(self) -> list[dict[str, Any]]:
        """Get ALL watchlist shows in a single API call."""
        response = await self._request("GET", "/sync/watchlist/shows")
        return response.json()

    async def get_user_settings(self) -> dict[str, Any]:
        """Get user settings including VIP status and account limits."""
        response = await self._request("GET", "/users/settings")
        return response.json()

    async def get_account_limits(self) -> AccountLimits:
        """Get account limits for the authenticated user.

        Returns:
            AccountLimits with VIP status and various limits.
            Non-VIP users typically have 100 item limits.
        """
        settings = await self.get_user_settings()
        user = settings.get("user", {})
        limits = settings.get("limits", {})

        return AccountLimits(
            is_vip=user.get("vip", False),
            collection_limit=limits.get("collection", {}).get("item_count", 100),
            watchlist_limit=limits.get("watchlist", {}).get("item_count", 100),
            list_limit=limits.get("list", {}).get("count", 2),
            list_item_limit=limits.get("list", {}).get("item_count", 100),
        )

    # =========================================================================
    # BATCH WRITE OPERATIONS - Single call updates everything
    # =========================================================================

    async def add_to_history(
        self,
        movies: list[dict] | None = None,
        shows: list[dict] | None = None,
        episodes: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Add multiple items to watch history in a single call."""
        payload = {}
        if movies:
            payload["movies"] = movies
        if shows:
            payload["shows"] = shows
        if episodes:
            payload["episodes"] = episodes

        if not payload:
            return {"added": {"movies": 0, "episodes": 0}}

        response = await self._request("POST", "/sync/history", json=payload)
        return response.json()

    async def remove_from_history(
        self,
        movies: list[dict] | None = None,
        shows: list[dict] | None = None,
        episodes: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Remove multiple items from watch history in a single call."""
        payload = {}
        if movies:
            payload["movies"] = movies
        if shows:
            payload["shows"] = shows
        if episodes:
            payload["episodes"] = episodes

        if not payload:
            return {"deleted": {"movies": 0, "episodes": 0}}

        response = await self._request("POST", "/sync/history/remove", json=payload)
        return response.json()

    async def add_ratings(
        self,
        movies: list[dict] | None = None,
        shows: list[dict] | None = None,
        episodes: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Add/update multiple ratings in a single call."""
        payload = {}
        if movies:
            payload["movies"] = movies
        if shows:
            payload["shows"] = shows
        if episodes:
            payload["episodes"] = episodes

        if not payload:
            return {"added": {"movies": 0, "shows": 0, "episodes": 0}}

        response = await self._request("POST", "/sync/ratings", json=payload)
        return response.json()

    async def remove_ratings(
        self,
        movies: list[dict] | None = None,
        shows: list[dict] | None = None,
        episodes: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Remove multiple ratings in a single call."""
        payload = {}
        if movies:
            payload["movies"] = movies
        if shows:
            payload["shows"] = shows
        if episodes:
            payload["episodes"] = episodes

        if not payload:
            return {"deleted": {"movies": 0, "shows": 0, "episodes": 0}}

        response = await self._request("POST", "/sync/ratings/remove", json=payload)
        return response.json()

    async def add_to_collection(
        self,
        movies: list[dict] | None = None,
        shows: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Add items to collection with optional metadata."""
        payload = {}
        if movies:
            payload["movies"] = movies
        if shows:
            payload["shows"] = shows

        if not payload:
            return {"added": {"movies": 0, "shows": 0}}

        response = await self._request("POST", "/sync/collection", json=payload)
        return response.json()

    async def remove_from_collection(
        self,
        movies: list[dict] | None = None,
        shows: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Remove items from collection."""
        payload = {}
        if movies:
            payload["movies"] = movies
        if shows:
            payload["shows"] = shows

        if not payload:
            return {"deleted": {"movies": 0, "shows": 0}}

        response = await self._request("POST", "/sync/collection/remove", json=payload)
        return response.json()

    async def add_to_watchlist(
        self,
        movies: list[dict] | None = None,
        shows: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Add items to watchlist."""
        payload = {}
        if movies:
            payload["movies"] = movies
        if shows:
            payload["shows"] = shows

        if not payload:
            return {"added": {"movies": 0, "shows": 0}}

        response = await self._request("POST", "/sync/watchlist", json=payload)
        return response.json()

    async def remove_from_watchlist(
        self,
        movies: list[dict] | None = None,
        shows: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Remove items from watchlist."""
        payload = {}
        if movies:
            payload["movies"] = movies
        if shows:
            payload["shows"] = shows

        if not payload:
            return {"deleted": {"movies": 0, "shows": 0}}

        response = await self._request("POST", "/sync/watchlist/remove", json=payload)
        return response.json()

    # =========================================================================
    # SEARCH - For ID lookups (cached heavily)
    # =========================================================================

    async def search_by_id(
        self,
        id_type: str,
        media_id: str,
        media_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search for an item by external ID."""
        params = {"id_type": id_type}
        if media_type:
            params["type"] = media_type

        response = await self._request("GET", f"/search/{id_type}/{media_id}", params=params)
        return response.json()

    # =========================================================================
    # AUTHENTICATION
    # =========================================================================

    async def device_code(self) -> dict[str, Any]:
        """Start device authentication flow."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{TRAKT_API_URL}/oauth/device/code",
                json={"client_id": self.config.client_id},
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            return response.json()

    async def poll_device_token(
        self,
        device_code: str,
        interval: int = 5,
        expires_in: int = 600,
    ) -> DeviceAuthResult:
        """Poll for device token after user authorizes.

        Returns DeviceAuthResult with specific status:
        - SUCCESS: Token obtained, ready to use
        - PENDING: Still waiting for user authorization
        - INVALID_CODE: Device code is invalid (404)
        - EXPIRED: Device code has expired (410)
        - DENIED: User explicitly denied authorization (418)
        - RATE_LIMITED: Polling too fast (429)
        """
        start = time.time()
        current_interval = interval

        async with httpx.AsyncClient() as client:
            while time.time() - start < expires_in:
                response = await client.post(
                    f"{TRAKT_API_URL}/oauth/device/token",
                    json={
                        "code": device_code,
                        "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret,
                    },
                    headers={"Content-Type": "application/json"},
                )

                if response.status_code == 200:
                    return DeviceAuthResult(
                        status=DeviceAuthStatus.SUCCESS,
                        token=response.json(),
                        message="Authentication successful",
                    )
                elif response.status_code == 400:
                    # Pending authorization - keep polling
                    await asyncio.sleep(current_interval)
                elif response.status_code == 404:
                    return DeviceAuthResult(
                        status=DeviceAuthStatus.INVALID_CODE,
                        message="Invalid device code. Please restart authentication.",
                    )
                elif response.status_code == 409:
                    # Code already used - treat as success check
                    return DeviceAuthResult(
                        status=DeviceAuthStatus.INVALID_CODE,
                        message="Device code was already used.",
                    )
                elif response.status_code == 410:
                    return DeviceAuthResult(
                        status=DeviceAuthStatus.EXPIRED,
                        message="Device code has expired. Please restart authentication.",
                    )
                elif response.status_code == 418:
                    return DeviceAuthResult(
                        status=DeviceAuthStatus.DENIED,
                        message="User denied authorization.",
                    )
                elif response.status_code == 429:
                    # Slow down polling
                    current_interval = min(current_interval * 2, 30)
                    await asyncio.sleep(current_interval)
                else:
                    return DeviceAuthResult(
                        status=DeviceAuthStatus.INVALID_CODE,
                        message=f"Unexpected error: {response.status_code}",
                    )

        return DeviceAuthResult(
            status=DeviceAuthStatus.EXPIRED,
            message="Polling timed out. Please restart authentication.",
        )

    async def refresh_access_token(self) -> dict[str, Any]:
        """Refresh the access token."""
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{TRAKT_API_URL}/oauth/token",
                json={
                    "refresh_token": self.config.refresh_token,
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "grant_type": "refresh_token",
                },
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            return response.json()

    async def revoke_token(self) -> bool:
        """Revoke the current access token (logout).

        Per Trakt API docs, should be called when user logs out to
        invalidate the token on Trakt's side.
        """
        if not self.config.access_token:
            return True

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{TRAKT_API_URL}/oauth/revoke",
                json={
                    "token": self.config.access_token,
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                },
                headers={"Content-Type": "application/json"},
            )
            return response.status_code == 200


def extract_trakt_ids(data: dict[str, Any]) -> TraktIds:
    """Extract Trakt IDs from API response."""
    ids = data.get("ids", {})
    return TraktIds(
        trakt=ids.get("trakt"),
        slug=ids.get("slug"),
        imdb=ids.get("imdb"),
        tmdb=ids.get("tmdb"),
        tvdb=ids.get("tvdb"),
    )
