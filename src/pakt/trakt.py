"""Trakt API client with batch operations."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from rich.console import Console

from pakt.config import Config, TraktConfig
from pakt.models import MediaType, RatedItem, TraktIds, WatchedItem

console = Console()

TRAKT_API_URL = "https://api.trakt.tv"
TRAKT_AUTH_URL = "https://trakt.tv"


class TraktRateLimitError(Exception):
    """Raised when rate limited."""

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Rate limited, retry after {retry_after}s")


class TraktClient:
    """Async Trakt API client optimized for batch operations."""

    def __init__(self, config: TraktConfig):
        self.config = config
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> TraktClient:
        self._client = httpx.AsyncClient(
            base_url=TRAKT_API_URL,
            timeout=30.0,
            headers=self._headers,
        )
        return self

    async def __aexit__(self, *args) -> None:
        if self._client:
            await self._client.aclose()

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
    ) -> dict[str, Any] | None:
        """Poll for device token after user authorizes."""
        start = time.time()
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
                    return response.json()
                elif response.status_code == 400:
                    # Pending authorization
                    await asyncio.sleep(interval)
                elif response.status_code == 429:
                    # Polling too fast
                    await asyncio.sleep(interval * 2)
                else:
                    # 404 = invalid code, 410 = expired, 418 = denied
                    return None

        return None

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
