"""Engine syncing one Plex server: its options, shared Trakt data and the calls phases make."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from pakt.config import Config, ServerConfig
from pakt.models import SyncResult
from pakt.plex.account import PlexAuthError
from pakt.plex.client import PlexClient
from pakt.sync import collection, episodes, movies, report, watchlist
from pakt.trakt import TraktAuthError, TraktClient

logger = logging.getLogger(__name__)

T = TypeVar("T")


class TraktCache:
    """Trakt data fetched once per run and shared by every server and phase."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, fetch: Callable[[], Awaitable[T]]) -> T:
        """Get what a TraktClient getter returns, calling it only the first time."""
        key = fetch.__name__
        if key not in self._data:
            self._data[key] = await fetch()
        return self._data[key]


@dataclass
class PushedToTrakt:
    """Items queued for Trakt this run and watchlists already synced; later servers skip both."""

    watched_movies: set[tuple] = field(default_factory=set)
    rated_movies: set[tuple] = field(default_factory=set)
    watched_episodes: set[tuple] = field(default_factory=set)
    rated_episodes: set[tuple] = field(default_factory=set)
    watchlist_accounts: set[str] = field(default_factory=set)


class SyncEngine:
    """Sync engine coordinating one Plex server with Trakt."""

    def __init__(
        self,
        config: Config,
        trakt: TraktClient,
        plex: PlexClient,
        log_callback: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        verbose: bool = False,
        server_config: ServerConfig | None = None,
        trakt_cache: TraktCache | None = None,
        pushed: PushedToTrakt | None = None,
    ):
        self.config = config
        self.trakt = trakt
        self.plex = plex
        self.verbose = verbose
        self.cache = trakt_cache or TraktCache()
        self.pushed = pushed or PushedToTrakt()
        self.fix_collection_dates = False
        self.plex_cache: dict[str, tuple[list, dict[str, int]]] = {}
        self._log_callback = log_callback
        self._cancel_check = cancel_check
        self._server_config = server_config
        self._keep_plex = False

    def get_sync_option(self, option: str) -> bool:
        """Get effective sync option, checking server override first."""
        if self._server_config:
            return self._server_config.get_sync_option(option, self.config.sync)
        return getattr(self.config.sync, option)

    def options(self) -> dict[str, bool]:
        """Every on/off sync option for this server, by name."""
        return {name: self.get_sync_option(name) for name in type(self.config.sync).model_fields
                if name != "rating_priority"}

    def movie_libraries_to_sync(self) -> list[str] | None:
        """Movie libraries chosen for this server, or None for all of them."""
        if self._server_config and self._server_config.movie_libraries:
            return self._server_config.movie_libraries
        return None

    def show_libraries_to_sync(self) -> list[str] | None:
        """Show libraries chosen for this server, or None for all of them."""
        if self._server_config and self._server_config.show_libraries:
            return self._server_config.show_libraries
        return None

    def is_cancelled(self) -> bool:
        """Whether the caller has asked to cancel."""
        return self._cancel_check() if self._cancel_check else False

    def progress(self, phase: int, total_phases: int, percent: float, label: str = "") -> None:
        """Send a progress line to the callback, as a share of the whole run."""
        phase_weight = 100 / total_phases
        overall = ((phase - 1) * phase_weight) + (percent / 100 * phase_weight)
        if self._log_callback:
            self._log_callback(f"PROGRESS:{phase}:{overall:.1f}:{label}")

    async def fetch_plex(
        self, fetch: Callable[[list[str] | None], tuple[list, dict[str, int]]],
        libraries: list[str] | None, warn: bool = True,
    ) -> tuple[list, dict[str, int]]:
        """Fetch Plex items off the event loop, reusing a kept copy; warns of missing libraries."""
        if fetch.__name__ in self.plex_cache:
            return self.plex_cache[fetch.__name__]
        items, counts = await asyncio.to_thread(fetch, libraries)
        if self._keep_plex:
            self.plex_cache[fetch.__name__] = (items, counts)
        for name in (libraries or []) if warn else []:
            if name not in counts:
                logger.warning(f"Library '{name}' not found on this server")
        return items, counts

    async def push(self, what: str, send: Callable[..., Awaitable[dict]], **lists: list[dict]) -> dict:
        """Push items to Trakt, logging what it added or updated and anything it could not match."""
        if not any(lists.values()):
            return {}
        response = await send(**lists)
        done = [f"added {report.counted(response.get('added'))}"]
        if any((response.get("updated") or {}).values()):
            done.append(f"updated {report.counted(response.get('updated'))}")
        logger.info(f"Trakt {what}: {', '.join(done)}")
        not_found = [i for items in (response.get("not_found") or {}).values() if isinstance(items, list)
                     for i in items]
        if not_found:
            logger.warning(f"Trakt {what}: could not match {report.plural(len(not_found), 'item')}")
            if self.verbose:
                report.items([report.title(i) for i in not_found])
        return response

    async def write_plex(self, what: str, write: Callable[[list], list], items: list, result: SyncResult) -> int:
        """Write a Plex batch off the event loop; returns successes, failures recorded as errors."""
        if not items:
            return 0
        failed = await asyncio.to_thread(write, items)
        done = len(items) - len(failed)
        if failed:
            logger.error(f"Plex: {done} {what}, {len(failed)} failed")
            result.errors.append(f"Plex: {len(failed)} failed ({what})")
        else:
            logger.info(f"Plex: {done} {what}")
        return done

    async def run_phase(self, name: str, phase: Awaitable[bool], result: SyncResult) -> bool:
        """Run one phase, recording any non-auth error and carrying on; False only on cancel."""
        try:
            return await phase
        except (TraktAuthError, PlexAuthError):
            raise
        except Exception as e:
            logger.exception(f"{name} failed: {type(e).__name__}: {e}")
            result.errors.append(f"{name}: {type(e).__name__}: {e}")
            return True

    def describe_options(self, collection_only: bool) -> str:
        """Describe what this server syncs and which way, for the line that opens its sync."""
        if collection_only:
            return "Syncing collection only"
        options = self.options()
        ratings = report.direction(options["ratings_plex_to_trakt"], options["ratings_trakt_to_plex"])
        if ratings != "off":
            wins = {"plex": "Plex wins", "trakt": "Trakt wins"}.get(self.config.sync.rating_priority)
            ratings += f", {wins} conflicts" if wins else ", conflicts left alone"
        watched = report.direction(options["watched_plex_to_trakt"], options["watched_trakt_to_plex"])
        wanted = report.direction(options["watchlist_plex_to_trakt"], options["watchlist_trakt_to_plex"])
        return (
            f"Syncing watched {watched}; ratings {ratings}; "
            f"collection {report.direction(options['collection_plex_to_trakt'], False)}; watchlist {wanted}"
        )

    async def sync(
        self,
        dry_run: bool = False,
        fix_collection_dates: bool = False,
        collection_only: bool = False,
        no_movies: bool = False,
        no_shows: bool = False,
    ) -> SyncResult | None:
        """Sync this server through every enabled phase; None if cancelled."""
        self.fix_collection_dates = fix_collection_dates
        # the collection phase reads the same Plex lists as the movie and episode phases
        self._keep_plex = not collection_only and self.get_sync_option("collection_plex_to_trakt")
        start_time = time.time()
        result = SyncResult()
        logger.info(self.describe_options(collection_only))

        if not collection_only:
            if not no_movies and not await self.run_phase("Movies", movies.run(self, result, dry_run), result):
                return None
            if not no_shows and not await self.run_phase("Episodes", episodes.run(self, result, dry_run), result):
                return None

        phase = collection.run(self, result, dry_run, no_movies=no_movies, no_shows=no_shows)
        if not await self.run_phase("Collection", phase, result):
            return None

        if not collection_only:
            if not await self.run_phase("Watchlist", watchlist.run(self, result, dry_run), result):
                return None

        result.duration_seconds = time.time() - start_time
        return result
