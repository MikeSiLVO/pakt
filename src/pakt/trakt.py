"""Trakt API client with batch operations."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

import httpx
from rich.console import Console

from pakt import __version__
from pakt.config import TraktConfig
from pakt.models import RatedItem, WatchedItem

console = Console()

TRAKT_API_URL = "https://api.trakt.tv"
TRAKT_AUTH_URL = "https://auth.trakt.tv"
USER_AGENT = f"pakt/{__version__}"

# Refresh token 1 day before expiry (tokens valid for 7 days)
TOKEN_REFRESH_THRESHOLD = 24 * 60 * 60

# Trakt allows one authenticated write per second
WRITE_INTERVAL = 1.0

RETRY_STATUSES = {429, 502, 503, 504, 520, 521, 522}

# items per sync POST; a show entry can carry hundreds of episodes
BATCH_SIZES = {"movies": 500, "shows": 100}


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Seconds to wait before a retry: Retry-After when sent, else backoff with jitter."""
    if response is not None:
        try:
            return float(response.headers["Retry-After"]) + 1
        except (KeyError, ValueError):
            pass
    return 5 * 2 ** attempt + random.uniform(0, 1)


def _oauth_error(response: httpx.Response) -> str:
    """Describe a failed OAuth exchange from its error body, falling back to the status code."""
    try:
        body = response.json()
        return body.get("error_description") or body.get("error") or f"HTTP {response.status_code}"
    except ValueError:
        return f"HTTP {response.status_code}"


def _merge_counts(total: dict[str, Any], part: dict[str, Any]) -> None:
    """Merge one batch's sync response into the running total: counts add up, lists join."""
    for key, value in part.items():
        if isinstance(value, dict):
            _merge_counts(total.setdefault(key, {}), value)
        elif isinstance(value, list):
            total.setdefault(key, []).extend(value)
        elif isinstance(value, int) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value
        else:
            total[key] = value


class TraktRateLimitError(Exception):
    """Raised when rate limited."""

    def __init__(self, retry_after: int):
        """Carry the server's Retry-After so callers can wait the right amount."""
        self.retry_after = retry_after
        super().__init__(f"Rate limited, retry after {retry_after}s")


class TraktAccountLimitError(Exception):
    """Raised when account limit exceeded (HTTP 420)."""

    def __init__(self, limit: int | None, is_vip: bool, upgrade_url: str = "https://trakt.tv/vip"):
        """Carry the limit and upgrade URL Trakt returned so the message can point at a fix."""
        self.limit = limit
        self.is_vip = is_vip
        self.upgrade_url = upgrade_url
        msg = f"Account limit exceeded ({limit} items)" if limit else "Account limit exceeded"
        if not is_vip:
            msg += f". Upgrade to VIP: {upgrade_url}"
        super().__init__(msg)


class TraktAuthError(Exception):
    """Raised when authentication fails (invalid/expired tokens)."""

    def __init__(self, message: str = "Authentication failed. Run 'pakt login' to re-authenticate."):
        """Default to the message that tells the user how to recover."""
        super().__init__(message)


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
    """Async Trakt API client for batch sync that hands rotated tokens to its refresh callback."""

    def __init__(
        self,
        config: TraktConfig,
        on_token_refresh: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._on_token_refresh = on_token_refresh
        self._last_write = 0.0

    async def __aenter__(self) -> TraktClient:
        """Open the HTTP client and refresh the token if it is close to expiring."""
        self._client = httpx.AsyncClient(
            base_url=TRAKT_API_URL,
            timeout=60.0,
            headers=self._headers,
        )
        await self._ensure_valid_token()
        return self

    async def __aexit__(self, *args) -> None:
        """Close the HTTP client."""
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
        """Refresh the token near expiry; raise only if Trakt refuses and the old one has lapsed."""
        if not self._token_needs_refresh():
            return

        console.print("[cyan]Refreshing Trakt access token...[/]")
        try:
            token = await self.refresh_access_token()
            self.config.access_token = token["access_token"]
            self.config.refresh_token = token["refresh_token"]
            self.config.expires_at = token["created_at"] + token["expires_in"]

            if self._client:
                self._client.headers["Authorization"] = f"Bearer {token['access_token']}"

            if self._on_token_refresh:
                self._on_token_refresh(token)

            console.print("[green]Token refreshed successfully[/]")
        except TraktAuthError as e:
            hours_left = (self.config.expires_at - time.time()) / 3600
            if hours_left <= 0:
                console.print(f"[red]{e}[/]")
                raise
            console.print(f"[yellow]{e} The current token still works for {hours_left:.0f}h.[/]")
        except Exception as e:
            console.print(f"[yellow]Token refresh failed: {e}[/]")
            console.print("[yellow]Continuing with existing token...[/]")

    @property
    def _headers(self) -> dict[str, str]:
        """Base headers for every API call, with the bearer token when logged in."""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "trakt-api-version": "2",
            "trakt-api-key": self.config.client_id,
        }
        if self.config.access_token:
            headers["Authorization"] = f"Bearer {self.config.access_token}"
        return headers

    def _auth_client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        """HTTP client for Trakt's OAuth host, with the API headers but no bearer token."""
        headers = {k: v for k, v in self._headers.items() if k != "Authorization"}
        return httpx.AsyncClient(base_url=TRAKT_AUTH_URL, timeout=timeout, headers=headers)

    async def _pace_write(self) -> None:
        """Pace a write to go out at least a second after the previous one."""
        wait = self._last_write + WRITE_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_write = time.monotonic()

    async def _request(
        self,
        method: str,
        path: str,
        retries: int = 3,
        **kwargs,
    ) -> httpx.Response:
        """Make a request, pacing writes and retrying rate limits, 5xx and dropped connections."""
        if not self._client:
            raise RuntimeError("Client not initialized. Use async with.")

        for attempt in range(retries):
            if method != "GET":
                await self._pace_write()
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.TransportError as e:
                # Timeouts and dropped connections; pagination makes these routine
                if attempt == retries - 1:
                    raise
                wait = _retry_delay(None, attempt)
                console.print(
                    f"[yellow]Trakt connection error ({type(e).__name__}), retrying in {wait:.0f}s "
                    f"(attempt {attempt + 1}/{retries})[/]"
                )
                await asyncio.sleep(wait)
                continue

            status = response.status_code
            if status in RETRY_STATUSES:
                wait = _retry_delay(response, attempt)
                if status == 429:
                    bucket = response.headers.get("X-Ratelimit", "no X-Ratelimit header")
                    console.print(f"[yellow]Trakt rate limit on {method} {path}: {bucket}[/]")
                if attempt == retries - 1:
                    if status == 429:
                        raise TraktRateLimitError(int(wait))
                    response.raise_for_status()
                console.print(
                    f"[yellow]Trakt {status}, retrying in {wait:.0f}s (attempt {attempt + 1}/{retries})[/]"
                )
                await asyncio.sleep(wait)
                continue

            if status == 401:
                raise TraktAuthError()
            if status == 403:
                raise TraktAuthError("Trakt refused pakt's API key (HTTP 403). Check for a pakt update.")
            if status == 420:
                is_vip = response.headers.get("X-VIP-User", "false").lower() == "true"
                limit_header = response.headers.get("X-Account-Limit", "")
                limit = int(limit_header) if limit_header.isdigit() else None
                upgrade_url = response.headers.get("X-Upgrade-URL", "https://trakt.tv/vip")
                raise TraktAccountLimitError(limit, is_vip, upgrade_url)
            if status == 423:
                raise TraktAuthError("Trakt account is locked or deactivated. Contact support@trakt.tv.")

            response.raise_for_status()
            return response

        raise TraktRateLimitError(60)

    async def _paginated_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get every page of a Trakt endpoint, up to its reported page count or an empty page."""
        params = dict(params or {})
        params["limit"] = limit
        params["page"] = 1

        response = await self._request("GET", path, params=params)
        items: list[dict[str, Any]] = response.json()

        page_count = int(response.headers.get("X-Pagination-Page-Count") or 1)
        for page in range(2, page_count + 1):
            params["page"] = page
            page_items = (await self._request("GET", path, params=params)).json()
            if not page_items:
                break
            items.extend(page_items)

        return items

    async def get_watched_movies(self) -> list[WatchedItem]:
        """Get all watched movies. Trakt paginates this endpoint at 250 items max."""
        items = await self._paginated_get("/sync/watched/movies", limit=250)
        return [WatchedItem(**item) for item in items]

    async def get_watched_shows(self) -> list[WatchedItem]:
        """Get all watched shows with per-season episode progress, 100 per page."""
        items = await self._paginated_get(
            "/sync/watched/shows", params={"extended": "progress"}, limit=100
        )
        return [WatchedItem(**item) for item in items]

    async def get_movie_ratings(self) -> list[RatedItem]:
        """Get ALL movie ratings in a single API call."""
        response = await self._request("GET", "/sync/ratings/movies")
        return [RatedItem(**item) for item in response.json()]

    async def get_episode_ratings(self) -> list[RatedItem]:
        """Get ALL episode ratings in a single API call."""
        response = await self._request("GET", "/sync/ratings/episodes")
        return [RatedItem(**item) for item in response.json()]

    async def get_collection_movies(self) -> list[dict[str, Any]]:
        """Get all collected movies, paging through Trakt's 250-item maximum."""
        return await self._paginated_get("/sync/collection/movies", limit=250)

    async def get_collection_shows(self) -> list[dict[str, Any]]:
        """Get all collected shows, paging through Trakt's 250-item maximum."""
        return await self._paginated_get("/sync/collection/shows", limit=250)

    async def get_watchlist_movies(self) -> list[dict[str, Any]]:
        """Get all watchlist movies, paging through Trakt's 250-item maximum."""
        return await self._paginated_get("/sync/watchlist/movies", limit=250)

    async def get_watchlist_shows(self) -> list[dict[str, Any]]:
        """Get all watchlist shows, paging through Trakt's 250-item maximum."""
        return await self._paginated_get("/sync/watchlist/shows", limit=250)

    async def get_user_settings(self) -> dict[str, Any]:
        """Get user settings including VIP status and account limits."""
        response = await self._request("GET", "/users/settings")
        return response.json()

    async def get_account_limits(self) -> AccountLimits:
        """Get the authenticated user's VIP status and account limits."""
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

    async def _post_sync(self, path: str, **lists: list[dict] | None) -> dict[str, Any]:
        """POST sync items in batches Trakt handles comfortably, summing the responses."""
        total: dict[str, Any] = {}
        for key, items in lists.items():
            items = items or []
            size = BATCH_SIZES[key]
            for start in range(0, len(items), size):
                response = await self._request("POST", path, json={key: items[start:start + size]})
                _merge_counts(total, response.json())
        return total

    async def add_to_history(
        self, movies: list[dict] | None = None, shows: list[dict] | None = None
    ) -> dict[str, Any]:
        """Add items to watch history."""
        return await self._post_sync("/sync/history", movies=movies, shows=shows)

    async def add_ratings(
        self, movies: list[dict] | None = None, shows: list[dict] | None = None
    ) -> dict[str, Any]:
        """Add or update ratings."""
        return await self._post_sync("/sync/ratings", movies=movies, shows=shows)

    async def add_to_collection(
        self, movies: list[dict] | None = None, shows: list[dict] | None = None
    ) -> dict[str, Any]:
        """Add items to collection with optional media metadata."""
        return await self._post_sync("/sync/collection", movies=movies, shows=shows)

    async def add_to_watchlist(
        self, movies: list[dict] | None = None, shows: list[dict] | None = None
    ) -> dict[str, Any]:
        """Add items to watchlist."""
        return await self._post_sync("/sync/watchlist", movies=movies, shows=shows)

    async def device_code(self) -> dict[str, Any]:
        """Start device authentication flow."""
        async with self._auth_client() as client:
            response = await client.post("/oauth/device/code", json={"client_id": self.config.client_id})
            response.raise_for_status()
            return response.json()

    async def check_device_token(self, device_code: str) -> DeviceAuthResult:
        """Check once whether the user has approved the device code."""
        try:
            async with self._auth_client() as client:
                response = await client.post(
                    "/oauth/device/token",
                    json={"code": device_code, "client_id": self.config.client_id},
                )
        except httpx.TransportError:
            return DeviceAuthResult(status=DeviceAuthStatus.PENDING)

        status = response.status_code
        if status == 200:
            return DeviceAuthResult(
                status=DeviceAuthStatus.SUCCESS,
                token=response.json(),
                message="Authentication successful",
            )
        if status == 400:
            return DeviceAuthResult(status=DeviceAuthStatus.PENDING)
        if status == 429:
            return DeviceAuthResult(status=DeviceAuthStatus.RATE_LIMITED)
        if status == 404:
            return DeviceAuthResult(
                status=DeviceAuthStatus.INVALID_CODE,
                message="Invalid device code. Please restart authentication.",
            )
        if status == 409:
            return DeviceAuthResult(
                status=DeviceAuthStatus.INVALID_CODE,
                message="Device code was already used.",
            )
        if status == 410:
            return DeviceAuthResult(
                status=DeviceAuthStatus.EXPIRED,
                message="Device code has expired. Please restart authentication.",
            )
        if status == 418:
            return DeviceAuthResult(
                status=DeviceAuthStatus.DENIED,
                message="User denied authorization.",
            )
        return DeviceAuthResult(
            status=DeviceAuthStatus.INVALID_CODE,
            message=f"Unexpected error: {status}",
        )

    async def poll_device_token(
        self,
        device_code: str,
        interval: int = 5,
        expires_in: int = 600,
    ) -> DeviceAuthResult:
        """Poll until the user approves or denies the device code, or it expires."""
        start = time.time()
        current_interval = interval

        while time.time() - start < expires_in:
            result = await self.check_device_token(device_code)
            if result.status == DeviceAuthStatus.RATE_LIMITED:
                current_interval = min(current_interval * 2, 30)
            elif result.status != DeviceAuthStatus.PENDING:
                return result
            await asyncio.sleep(current_interval)

        return DeviceAuthResult(
            status=DeviceAuthStatus.EXPIRED,
            message="Polling timed out. Please restart authentication.",
        )

    async def refresh_access_token(self) -> dict[str, Any]:
        """Refresh the access token, retrying network errors; TraktAuthError if Trakt refuses it."""
        last_error: Exception | None = None
        retries = 3
        backoff = [1, 2]

        for attempt in range(retries):
            try:
                async with self._auth_client() as client:
                    response = await client.post(
                        "/oauth/token",
                        json={
                            "refresh_token": self.config.refresh_token,
                            "client_id": self.config.client_id,
                            "grant_type": "refresh_token",
                        },
                    )

                # 400 invalid_grant or 401: the refresh token is dead
                if response.status_code in (400, 401):
                    raise TraktAuthError(
                        f"Trakt refused the refresh token ({_oauth_error(response)}). "
                        "Run 'pakt login' to re-authenticate."
                    )

                response.raise_for_status()
                token = response.json()
                if not token.get("access_token") or not token.get("refresh_token"):
                    raise ValueError("Trakt returned an unexpected token response")
                return token

            except TraktAuthError:
                # don't retry auth errors; they won't succeed
                raise
            except (httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.TimeoutException) as e:
                last_error = e
                if attempt < retries - 1:
                    wait = backoff[attempt]
                    console.print(f"[yellow]Token refresh failed (attempt {attempt + 1}/{retries}), "
                                  f"retrying in {wait}s...[/]")
                    await asyncio.sleep(wait)

        raise last_error or Exception("Token refresh failed after retries")

    async def revoke_token(self) -> bool:
        """Revoke the access token on Trakt; True when Trakt accepted it or there was none."""
        if not self.config.access_token:
            return True

        async with self._auth_client() as client:
            response = await client.post(
                "/oauth/revoke",
                json={"token": self.config.access_token, "client_id": self.config.client_id},
            )
            return response.status_code == 200
