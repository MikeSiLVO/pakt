"""PlexClient: one server's connection, its libraries, batch writes and the account watchlist."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed

from plexapi.exceptions import Unauthorized
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer
from plexapi.video import Episode, Movie, Show

from pakt.config import Config, ServerConfig
from pakt.plex import account
from pakt.plex.account import PlexAuthError

logger = logging.getLogger(__name__)


def _disable_auto_reload(items: list) -> list:
    """Disable PlexAPI auto-reload on items to prevent network calls for None attributes."""
    for item in items:
        item._autoReload = False
    return items


class PlexClient:
    """Plex API client optimized for batch operations."""

    def __init__(self, server_config: ServerConfig, account_token: str = ""):
        account.identify()
        self._url = server_config.url
        self._token = server_config.token
        self._account_token = account_token or server_config.token
        self._server_name = server_config.server_name
        self.server_config = server_config
        self._server: PlexServer | None = None
        self._account: MyPlexAccount | None = None

    def connect(self) -> None:
        """Connect via plex.tv or saved URL, saving what worked; PlexAuthError on refused token."""
        owned = False
        if self._account_token and self._server_name:
            try:
                logger.debug("Connecting to '%s' via plex.tv discovery", self._server_name)
                resource = MyPlexAccount(token=self._account_token).resource(self._server_name)
                owned = bool(resource.owned)
                self._token = str(resource.accessToken or "") or self._token
                self._server = account.connect_resource(resource)
                self._persist_connection()
                return
            except Unauthorized as e:
                raise PlexAuthError() from e
            except Exception as e:
                if not self._url:
                    raise
                logger.warning("Could not connect through plex.tv (%s), trying the saved address", e)

        if not (self._url and self._token):
            raise ValueError("Need either URL+token or token+server_name")
        # a shared server's owner publishes its addresses; only a LAN address gets the account token
        if (self._server_name and not owned and self._token == self._account_token
                and not account.is_lan_url(self._url)):
            raise ConnectionError(f"Could not reach '{self._server_name}' through plex.tv")
        logger.debug("Connecting directly to %s", self._url)
        try:
            self._server = PlexServer(self._url, self._token)
        except Unauthorized as e:
            raise PlexAuthError() from e
        self._persist_connection()

    def _persist_connection(self) -> None:
        """Save the URL and server token this connection used back to config.json."""
        if not self._server:
            return

        # plexapi types these loosely, but both are strings on a live connection
        new_url = str(self._server._baseurl or "")
        new_token = str(self._server._token or "")

        if not new_url or not new_token:
            return

        if new_url == self._url and new_token == self._token:
            return

        if new_url != self._url:
            logger.info("Server address changed from %s to %s", self._url or "none", new_url)
            self._url = new_url
        if new_token != self._token:
            logger.info("Saved this server's own access token")
            self._token = new_token

        try:
            config = Config.load()
            server = config.get_server(self.server_config.name)
            if server:
                server.url = self._url
                server.token = self._token
                config.save()
        except Exception as e:
            logger.warning("Could not save the new connection details to config.json: %s", e)

    @property
    def route(self) -> str:
        """How this connection reaches the server: over the LAN, or remotely over HTTPS or HTTP."""
        url = str(self.server._baseurl or "")
        if account.is_lan_url(url):
            return "over LAN"
        return "remotely over HTTPS" if url.startswith("https") else "remotely over plain HTTP"

    @property
    def account_token(self) -> str:
        """Token for plex.tv account calls, shared by every server on the account."""
        return self._account_token

    @property
    def account(self) -> MyPlexAccount:
        """Get the plex.tv account for watchlist calls, signing in once."""
        if self._account is None:
            self._account = MyPlexAccount(token=self._account_token)
        return self._account

    @property
    def server(self) -> PlexServer:
        """Connected server, dialing out on first use."""
        if not self._server:
            self.connect()
        if not self._server:
            raise RuntimeError(f"Not connected to Plex server '{self.server_config.name}'")
        return self._server

    def get_movie_libraries(self) -> list[str]:
        """Get all movie library names."""
        return [lib.title for lib in self.server.library.sections() if lib.type == "movie"]

    def get_show_libraries(self) -> list[str]:
        """Get all TV show library names."""
        return [lib.title for lib in self.server.library.sections() if lib.type == "show"]

    def _fetch_sections(
        self, libtype: str, library_names: list[str] | None, episodes: bool = False
    ) -> tuple[list, dict[str, int]]:
        """Fetch all items of one library type, counted per library with clashing names numbered."""
        items: list = []
        lib_counts: dict[str, int] = {}
        for section in self.server.library.sections():
            if section.type != libtype or (library_names and section.title not in library_names):
                continue
            # large pages keep the request count down
            if episodes:
                found = section.searchEpisodes(container_size=1000)
            else:
                found = section.all(container_size=1000)
            _disable_auto_reload(found)
            key, i = section.title, 2
            while key in lib_counts:
                key, i = f"{section.title} ({i})", i + 1
            lib_counts[key] = len(found)
            items.extend(found)
        return items, lib_counts

    def get_all_movies_with_counts(self, library_names: list[str] | None = None) -> tuple[list[Movie], dict[str, int]]:
        """Get all movies from specified libraries with per-library counts."""
        return self._fetch_sections("movie", library_names)

    def get_all_shows_with_counts(self, library_names: list[str] | None = None) -> tuple[list[Show], dict[str, int]]:
        """Get all shows from specified libraries with per-library counts."""
        return self._fetch_sections("show", library_names)

    def get_all_episodes_with_counts(
        self, library_names: list[str] | None = None
    ) -> tuple[list[Episode], dict[str, int]]:
        """Get all episodes from specified libraries with per-library counts."""
        return self._fetch_sections("show", library_names, episodes=True)

    def mark_watched_batch(
        self, items: Sequence[Movie | Episode], max_workers: int = 10
    ) -> list[tuple[Movie | Episode, Exception]]:
        """Mark items watched concurrently; returns (item, error) for any failures."""
        if not items:
            return []

        failed: list[tuple[Movie | Episode, Exception]] = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_item = {
                executor.submit(item.markWatched): item
                for item in items
            }

            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    future.result()
                except Exception as e:
                    failed.append((item, e))
                    logger.warning(f"Plex could not mark '{item.title}' watched: {e}")

        return failed

    def rate_batch(
        self, items: Sequence[tuple[Movie | Show | Episode, int | float]], max_workers: int = 10
    ) -> list[tuple[Movie | Show | Episode, int | float, Exception]]:
        """Rate (item, rating) pairs concurrently; returns (item, rating, error) for failures."""
        if not items:
            return []

        failed: list[tuple[Movie | Show | Episode, int | float, Exception]] = []

        def rate_item(pair: tuple[Movie | Show | Episode, int | float]) -> None:
            """Runs on the thread pool, one item per worker."""
            item, rating = pair
            item.rate(rating)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_item = {
                executor.submit(rate_item, pair): pair
                for pair in items
            }

            for future in as_completed(future_to_item):
                item, rating = future_to_item[future]
                try:
                    future.result()
                except Exception as e:
                    failed.append((item, rating, e))
                    logger.warning(f"Plex could not rate '{item.title}' {rating}: {e}")

        return failed

    def get_watchlist(self) -> list[Movie | Show]:
        """Get account watchlist (includes items not in library), with each item's external IDs."""
        return _disable_auto_reload(self.account.watchlist(includeGuids=1))

    def add_to_watchlist_batch(self, items: Sequence[Movie | Show]) -> list[tuple[Movie | Show, Exception]]:
        """Add items to the account watchlist one by one; returns (item, error) for any failures."""
        failed: list[tuple[Movie | Show, Exception]] = []
        for item in items:
            try:
                self.account.addToWatchlist(item)
            except Exception as e:
                failed.append((item, e))
                logger.warning(f"Plex could not add '{item.title}' to the watchlist: {e}")
        return failed

    def search_discover(self, query: str, libtype: str | None = None) -> list[Movie | Show]:
        """Search Plex Discover for items not in library."""
        return self.account.searchDiscover(query, libtype=libtype)
