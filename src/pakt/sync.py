"""Sync logic for Pakt."""

from __future__ import annotations

import asyncio
import gc
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from pakt.config import Config, ServerConfig, get_config_dir
from pakt.models import PlexIds, RatedItem, SyncResult, WatchedItem
from pakt.plex import PlexClient, extract_media_metadata, extract_plex_ids
from pakt.trakt import AccountLimits, TraktAccountLimitError, TraktClient

console = Console()


@dataclass
class TraktCache:
    """Cached Trakt data to avoid re-fetching for multiple servers."""

    account_limits: AccountLimits | None = None
    watched_movies: list[WatchedItem] = field(default_factory=list)
    movie_ratings: list[RatedItem] = field(default_factory=list)
    watched_shows: list[WatchedItem] = field(default_factory=list)
    episode_ratings: list[RatedItem] = field(default_factory=list)
    collection_movies: list[dict] = field(default_factory=list)
    collection_shows: list[dict] = field(default_factory=list)
    watchlist_movies: list[dict] = field(default_factory=list)
    watchlist_shows: list[dict] = field(default_factory=list)


@dataclass
class MovieProcessingResult:
    """Results from movie processing (runs in thread)."""

    movies_to_mark_watched_trakt: list[dict] = field(default_factory=list)
    movies_to_mark_watched_plex: list[Any] = field(default_factory=list)
    movies_to_rate_trakt: list[dict] = field(default_factory=list)
    movies_to_rate_plex: list[tuple[Any, int]] = field(default_factory=list)
    cancelled: bool = False


@dataclass
class EpisodeProcessingResult:
    """Results from episode processing (runs in thread)."""

    episodes_to_mark_watched_trakt: list[dict] = field(default_factory=list)
    episodes_to_mark_watched_trakt_display: list[str] = field(default_factory=list)
    episodes_to_mark_watched_plex: list[Any] = field(default_factory=list)
    episodes_to_rate_trakt: list[dict] = field(default_factory=list)
    episodes_to_rate_trakt_display: list[str] = field(default_factory=list)
    episodes_to_rate_plex: list[tuple[Any, int]] = field(default_factory=list)
    skipped_no_ids: int = 0
    cancelled: bool = False


def _process_episodes_in_thread(
    plex_episodes: list[Any],
    plex_show_ids_by_key: dict[str, PlexIds],
    trakt_watched_episodes: dict[tuple, dict],
    trakt_episode_ratings: dict[tuple, dict],
    sync_watched_plex_to_trakt: bool,
    sync_watched_trakt_to_plex: bool,
    sync_ratings_plex_to_trakt: bool,
    sync_ratings_trakt_to_plex: bool,
    cancel_event: threading.Event,
    progress_callback: Callable[[int, int], None] | None = None,
) -> EpisodeProcessingResult:
    """Process episodes in a thread to not block the event loop.

    This is CPU-bound work (dict lookups, comparisons) that would otherwise
    block the async event loop and prevent cancellation/UI updates.
    """
    result = EpisodeProcessingResult()
    processed_episode_ids: set[tuple] = set()
    total = len(plex_episodes)

    # Pre-extract all episode data to avoid slow PlexAPI attribute access in comparison loop
    # CRITICAL: PlexAPI's __getattribute__ triggers network reload if attribute is None!
    # Disable auto-reload to prevent 24ms network call per episode for remote servers.
    episode_data: list[tuple] = []
    for i, ep in enumerate(plex_episodes):
        if i % 100 == 0 and cancel_event.is_set():
            result.cancelled = True
            return result
        if i % 500 == 0 and progress_callback:
            progress_callback(i, total)

        episode_data.append((
            str(ep.grandparentRatingKey),  # show_key
            ep.parentIndex,  # seasonNumber
            ep.index,  # episodeNumber
            ep.viewCount > 0 if ep.viewCount else False,  # isWatched
            ep.userRating,
            ep.grandparentTitle,
            ep,  # Keep reference for Plex operations
        ))

    if progress_callback:
        progress_callback(total, total)

    # Now iterate over extracted data (pure Python, no network calls)
    for show_key, season_num, ep_num, plex_watched, plex_ep_rating, show_title, episode in episode_data:
        show_ids = plex_show_ids_by_key.get(show_key)
        if not show_ids or (not show_ids.tvdb and not show_ids.imdb):
            result.skipped_no_ids += 1
            continue

        # Skip duplicates (same episode in multiple libraries)
        ep_key = (show_ids.tvdb or show_ids.imdb, season_num, ep_num)
        if ep_key in processed_episode_ids:
            continue
        processed_episode_ids.add(ep_key)

        # Check watched status
        trakt_watched = False
        if show_ids.tvdb and (show_ids.tvdb, season_num, ep_num) in trakt_watched_episodes:
            trakt_watched = True
        elif show_ids.imdb and (show_ids.imdb, season_num, ep_num) in trakt_watched_episodes:
            trakt_watched = True

        if plex_watched and not trakt_watched and sync_watched_plex_to_trakt:
            ep_ids = {}
            if show_ids.tvdb:
                ep_ids["tvdb"] = show_ids.tvdb
            if show_ids.imdb:
                ep_ids["imdb"] = show_ids.imdb
            if ep_ids:
                result.episodes_to_mark_watched_trakt.append({
                    "ids": ep_ids,
                    "seasons": [{"number": season_num, "episodes": [{"number": ep_num}]}]
                })
                result.episodes_to_mark_watched_trakt_display.append(
                    f"{show_title} S{season_num:02d}E{ep_num:02d}"
                )
        elif trakt_watched and not plex_watched and sync_watched_trakt_to_plex:
            result.episodes_to_mark_watched_plex.append(episode)

        # Check ratings
        plex_ep_rating_int = int(plex_ep_rating) if plex_ep_rating else None
        trakt_ep_rating = None
        if show_ids.tvdb and (show_ids.tvdb, season_num, ep_num) in trakt_episode_ratings:
            trakt_ep_rating = trakt_episode_ratings[(show_ids.tvdb, season_num, ep_num)]
        elif show_ids.imdb and (show_ids.imdb, season_num, ep_num) in trakt_episode_ratings:
            trakt_ep_rating = trakt_episode_ratings[(show_ids.imdb, season_num, ep_num)]

        trakt_ep_rating_val = trakt_ep_rating["rating"] if trakt_ep_rating else None

        if plex_ep_rating_int and not trakt_ep_rating_val and sync_ratings_plex_to_trakt:
            ep_ids = {}
            if show_ids.tvdb:
                ep_ids["tvdb"] = show_ids.tvdb
            if show_ids.imdb:
                ep_ids["imdb"] = show_ids.imdb
            if ep_ids:
                result.episodes_to_rate_trakt.append({
                    "ids": ep_ids,
                    "seasons": [{
                        "number": season_num,
                        "episodes": [{"number": ep_num, "rating": plex_ep_rating_int}]
                    }]
                })
                result.episodes_to_rate_trakt_display.append(
                    f"{show_title} S{season_num:02d}E{ep_num:02d} = {plex_ep_rating_int}"
                )
        elif trakt_ep_rating_val and not plex_ep_rating_int and sync_ratings_trakt_to_plex:
            result.episodes_to_rate_plex.append((episode, trakt_ep_rating_val))

    return result


# File logger setup
_file_logger: logging.Logger | None = None


def get_file_logger() -> logging.Logger:
    """Get or create the file logger."""
    global _file_logger
    if _file_logger is None:
        log_dir = get_config_dir()
        log_file = log_dir / "sync.log"

        _file_logger = logging.getLogger("pakt.sync")
        _file_logger.setLevel(logging.DEBUG)

        # Rotate log if too big (>5MB)
        if log_file.exists() and log_file.stat().st_size > 5 * 1024 * 1024:
            old_log = log_dir / "sync.log.old"
            if old_log.exists():
                old_log.unlink()
            log_file.rename(old_log)

        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _file_logger.addHandler(handler)

    return _file_logger


class SyncEngine:
    """Main sync engine coordinating Plex and Trakt."""

    def __init__(
        self,
        config: Config,
        trakt: TraktClient,
        plex: PlexClient,
        log_callback: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        verbose: bool = False,
        server_name: str | None = None,
        server_config: ServerConfig | None = None,
        trakt_cache: TraktCache | None = None,
    ):
        self.config = config
        self.trakt = trakt
        self.plex = plex
        self._log_callback = log_callback
        self._cancel_check = cancel_check
        self._verbose = verbose
        self._server_name = server_name
        self._server_config = server_config
        self._trakt_cache = trakt_cache
        self._account_limits: AccountLimits | None = None

    def _get_sync_option(self, option: str) -> bool:
        """Get effective sync option, checking server override first."""
        if self._server_config:
            return self._server_config.get_sync_option(option, self.config.sync)
        return getattr(self.config.sync, option)

    def _get_movie_libraries(self) -> list[str] | None:
        """Get movie libraries to sync (server-specific or global)."""
        if self._server_config and self._server_config.movie_libraries:
            return self._server_config.movie_libraries
        return None

    def _get_show_libraries(self) -> list[str] | None:
        """Get show libraries to sync (server-specific or global)."""
        if self._server_config and self._server_config.show_libraries:
            return self._server_config.show_libraries
        return None

    async def _get_account_limits(self) -> AccountLimits:
        """Fetch and cache account limits."""
        if self._trakt_cache and self._trakt_cache.account_limits:
            return self._trakt_cache.account_limits
        if self._account_limits is None:
            self._account_limits = await self.trakt.get_account_limits()
        return self._account_limits

    def _is_cancelled(self) -> bool:
        """Check if sync has been cancelled."""
        return self._cancel_check() if self._cancel_check else False

    def _log(self, msg: str) -> None:
        """Log a message to console, callback, and file."""
        # Add server name prefix if set
        if self._server_name:
            display_msg = f"[dim][{self._server_name}][/] {msg}"
        else:
            display_msg = msg

        # Strip rich markup for clean message
        clean_msg = re.sub(r'\[/?[^\]]+\]', '', display_msg)

        # Always log to file
        get_file_logger().info(clean_msg)

        # Console and callback
        console.print(display_msg)
        if self._log_callback:
            self._log_callback(clean_msg)

    def _progress(self, phase: int, total_phases: int, percent: float, label: str = "") -> None:
        """Send progress update to callback and log."""
        # Calculate overall progress across all phases
        phase_weight = 100 / total_phases
        overall = ((phase - 1) * phase_weight) + (percent / 100 * phase_weight)

        if self._log_callback:
            self._log_callback(f"PROGRESS:{phase}:{overall:.1f}:{label}")

    async def _sync_movies(self, result: SyncResult, dry_run: bool) -> bool:
        """Sync movies. Returns False if cancelled."""
        phase_start = time.time()
        self._log("\n[cyan]Phase 1:[/] Syncing movies...")
        self._progress(1, 4, 0, "Fetching movie data")

        # Fetch Trakt movie data (use cache if available)
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            transient=True, console=console
        ) as progress:
            if self._trakt_cache:
                trakt_watched_movies = self._trakt_cache.watched_movies
                trakt_movie_ratings = self._trakt_cache.movie_ratings
                self._progress(1, 4, 15, "Using cached Trakt data")
            else:
                task = progress.add_task("Fetching Trakt watched movies...", total=None)
                self._progress(1, 4, 5, "Trakt watched movies")
                trakt_watched_movies = await self.trakt.get_watched_movies()
                progress.update(task, description=f"Got {len(trakt_watched_movies)} watched movies")

                task = progress.add_task("Fetching Trakt movie ratings...", total=None)
                self._progress(1, 4, 15, "Trakt movie ratings")
                trakt_movie_ratings = await self.trakt.get_movie_ratings()
                progress.update(task, description=f"Got {len(trakt_movie_ratings)} movie ratings")

            task = progress.add_task("Fetching Plex movies...", total=None)
            self._progress(1, 4, 25, "Plex movies")
            # Run in thread to not block event loop (allows web UI updates)
            plex_movies, movie_libs = await asyncio.to_thread(
                self.plex.get_all_movies_with_counts, self._get_movie_libraries()
            )
            progress.update(task, description=f"Got {len(plex_movies)} movies from Plex")

        self._log(f"  Trakt: {len(trakt_watched_movies)} watched, {len(trakt_movie_ratings)} ratings")
        for lib_name, count in movie_libs.items():
            self._log(f"  Plex [{lib_name}]: {count} movies")

        # Build indices
        trakt_watched_by_imdb: dict[str, dict] = {}
        trakt_watched_by_tmdb: dict[int, dict] = {}
        for item in trakt_watched_movies:
            if item.movie:
                ids = item.movie.get("ids", {})
                data = {"item": item, "movie": item.movie}
                if ids.get("imdb"):
                    trakt_watched_by_imdb[ids["imdb"]] = data
                if ids.get("tmdb"):
                    trakt_watched_by_tmdb[ids["tmdb"]] = data

        trakt_ratings_by_imdb: dict[str, dict] = {}
        trakt_ratings_by_tmdb: dict[int, dict] = {}
        for item in trakt_movie_ratings:
            if item.movie:
                ids = item.movie.get("ids", {})
                data = {"rating": item.rating, "rated_at": item.rated_at}
                if ids.get("imdb"):
                    trakt_ratings_by_imdb[ids["imdb"]] = data
                if ids.get("tmdb"):
                    trakt_ratings_by_tmdb[ids["tmdb"]] = data

        # Free raw Trakt data
        del trakt_watched_movies, trakt_movie_ratings
        gc.collect()

        # Process movies (deduplicate by external ID for multi-library setups)
        movies_to_mark_watched_trakt: list[dict] = []
        movies_to_mark_watched_plex: list[Any] = []
        movies_to_rate_trakt: list[dict] = []
        movies_to_rate_plex: list[tuple[Any, int]] = []
        processed_movie_ids: set[str] = set()

        total_movies = len(plex_movies)
        self._log(f"  Processing {total_movies} movies...")
        processed = 0

        # Update progress display every 1%, but yield to event loop more often for cancellation
        update_interval = max(1, total_movies // 100)
        yield_interval = max(1, min(500, total_movies // 200))

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TaskProgressColumn(), console=console, transient=False
        ) as progress:
            task = progress.add_task(f"Movies 0/{total_movies}", total=total_movies)

            while plex_movies:
                chunk = plex_movies[:2000]
                plex_movies = plex_movies[2000:]

                for plex_movie in chunk:
                    # Yield to event loop frequently for responsive cancellation
                    if processed % yield_interval == 0:
                        await asyncio.sleep(0)
                        if self._is_cancelled():
                            self._log("  [yellow]Cancelled[/]")
                            return False

                    # Update display less frequently (1%)
                    if processed % update_interval == 0:
                        self._progress(1, 4, 30 + (processed / total_movies) * 40, f"Movies {processed}/{total_movies}")
                        progress.update(task, completed=processed, description=f"Movies {processed}/{total_movies}")

                    plex_ids = extract_plex_ids(plex_movie)

                    # Skip duplicates (same movie in multiple libraries)
                    movie_key = plex_ids.imdb or (f"tmdb:{plex_ids.tmdb}" if plex_ids.tmdb else None)
                    if movie_key:
                        if movie_key in processed_movie_ids:
                            processed += 1
                            continue
                        processed_movie_ids.add(movie_key)

                    trakt_data = None
                    if plex_ids.imdb and plex_ids.imdb in trakt_watched_by_imdb:
                        trakt_data = trakt_watched_by_imdb[plex_ids.imdb]
                    elif plex_ids.tmdb and plex_ids.tmdb in trakt_watched_by_tmdb:
                        trakt_data = trakt_watched_by_tmdb[plex_ids.tmdb]

                    trakt_rating = None
                    if plex_ids.imdb and plex_ids.imdb in trakt_ratings_by_imdb:
                        trakt_rating = trakt_ratings_by_imdb[plex_ids.imdb]
                    elif plex_ids.tmdb and plex_ids.tmdb in trakt_ratings_by_tmdb:
                        trakt_rating = trakt_ratings_by_tmdb[plex_ids.tmdb]

                    plex_watched = plex_movie.isWatched
                    trakt_watched = trakt_data is not None

                    if plex_watched and not trakt_watched and self._get_sync_option("watched_plex_to_trakt"):
                        movie_data = self._build_trakt_movie(plex_movie, plex_ids)
                        if movie_data:
                            movies_to_mark_watched_trakt.append(movie_data)
                    elif trakt_watched and not plex_watched and self._get_sync_option("watched_trakt_to_plex"):
                        movies_to_mark_watched_plex.append(plex_movie)

                    plex_rating = int(plex_movie.userRating) if plex_movie.userRating else None
                    trakt_rating_val = trakt_rating["rating"] if trakt_rating else None

                    if plex_rating and not trakt_rating_val and self._get_sync_option("ratings_plex_to_trakt"):
                        movie_data = self._build_trakt_movie(plex_movie, plex_ids)
                        if movie_data:
                            movie_data["rating"] = plex_rating
                            movies_to_rate_trakt.append(movie_data)
                    elif trakt_rating_val and not plex_rating and self._get_sync_option("ratings_trakt_to_plex"):
                        movies_to_rate_plex.append((plex_movie, trakt_rating_val))

                    processed += 1

                del chunk
                gc.collect()

        self._log(f"  Movies - To mark watched on Trakt: {len(movies_to_mark_watched_trakt)}")
        self._log(f"  Movies - To mark watched on Plex: {len(movies_to_mark_watched_plex)}")
        self._log(f"  Movies - To rate on Trakt: {len(movies_to_rate_trakt)}")
        self._log(f"  Movies - To rate on Plex: {len(movies_to_rate_plex)}")

        if self._verbose:
            for m in movies_to_mark_watched_trakt:
                self._log(f"    [dim]→ Trakt watched: {m.get('title')} ({m.get('year')})[/]")
            for m in movies_to_mark_watched_plex:
                self._log(f"    [dim]→ Plex watched: {m.title} ({m.year})[/]")
            for m in movies_to_rate_trakt:
                self._log(f"    [dim]→ Trakt rating: {m.get('title')} ({m.get('year')}) = {m.get('rating')}[/]")
            for m, rating in movies_to_rate_plex:
                self._log(f"    [dim]→ Plex rating: {m.title} ({m.year}) = {rating}[/]")

        # Apply changes
        if not dry_run:
            self._progress(1, 4, 75, "Applying movie changes")
            if movies_to_mark_watched_trakt:
                self._log(f"  Adding {len(movies_to_mark_watched_trakt)} movies to Trakt history...")
                response = await self.trakt.add_to_history(movies=movies_to_mark_watched_trakt)
                result.added_to_trakt += response.get("added", {}).get("movies", 0)

            if movies_to_rate_trakt:
                self._log(f"  Adding {len(movies_to_rate_trakt)} movie ratings to Trakt...")
                response = await self.trakt.add_ratings(movies=movies_to_rate_trakt)
                result.ratings_synced += response.get("added", {}).get("movies", 0)

            if movies_to_mark_watched_plex:
                self._log(f"  Marking {len(movies_to_mark_watched_plex)} movies watched on Plex...")
                failed = self.plex.mark_watched_batch(movies_to_mark_watched_plex)
                result.added_to_plex += len(movies_to_mark_watched_plex) - len(failed)

            if movies_to_rate_plex:
                self._log(f"  Rating {len(movies_to_rate_plex)} movies on Plex...")
                failed = self.plex.rate_batch(movies_to_rate_plex)
                result.ratings_synced += len(movies_to_rate_plex) - len(failed)

        self._progress(1, 4, 100, "Movies complete")
        self._log(f"  [dim]Phase 1 completed in {time.time() - phase_start:.1f}s[/]")

        # Free all movie data
        del movies_to_mark_watched_trakt, movies_to_mark_watched_plex
        del movies_to_rate_trakt, movies_to_rate_plex
        del trakt_watched_by_imdb, trakt_watched_by_tmdb
        del trakt_ratings_by_imdb, trakt_ratings_by_tmdb
        gc.collect()

        return True

    async def _sync_episodes(self, result: SyncResult, dry_run: bool) -> bool:
        """Sync episodes. Returns False if cancelled."""
        phase_start = time.time()
        self._log("\n[cyan]Phase 2:[/] Syncing episodes...")
        self._progress(2, 4, 0, "Fetching episode data")

        # Fetch Trakt episode data (use cache if available)
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            transient=True, console=console
        ) as progress:
            if self._trakt_cache:
                trakt_watched_shows = self._trakt_cache.watched_shows
                trakt_episode_ratings_list = self._trakt_cache.episode_ratings
                self._progress(2, 4, 15, "Using cached Trakt data")
            else:
                task = progress.add_task("Fetching Trakt watched shows...", total=None)
                self._progress(2, 4, 5, "Trakt watched shows")
                trakt_watched_shows = await self.trakt.get_watched_shows()
                progress.update(task, description=f"Got {len(trakt_watched_shows)} watched shows")

                task = progress.add_task("Fetching Trakt episode ratings...", total=None)
                self._progress(2, 4, 15, "Trakt episode ratings")
                trakt_episode_ratings_list = await self.trakt.get_episode_ratings()
                progress.update(task, description=f"Got {len(trakt_episode_ratings_list)} episode ratings")

            task = progress.add_task("Fetching Plex shows...", total=None)
            self._progress(2, 4, 25, "Plex shows")
            # Run in thread to not block event loop (allows web UI updates)
            plex_shows, show_libs = await asyncio.to_thread(
                self.plex.get_all_shows_with_counts, self._get_show_libraries()
            )
            progress.update(task, description=f"Got {len(plex_shows)} shows from Plex")

            task = progress.add_task("Fetching Plex episodes...", total=None)
            self._progress(2, 4, 30, "Plex episodes")
            # Run in thread to not block event loop (allows web UI updates)
            plex_episodes, episode_libs = await asyncio.to_thread(
                self.plex.get_all_episodes_with_counts, self._get_show_libraries()
            )
            progress.update(task, description=f"Got {len(plex_episodes)} episodes from Plex")

        self._log(
            f"  Trakt: {len(trakt_watched_shows)} watched shows, "
            f"{len(trakt_episode_ratings_list)} episode ratings"
        )
        for lib_name, count in show_libs.items():
            ep_count = episode_libs.get(lib_name, 0)
            self._log(f"  Plex [{lib_name}]: {count} shows, {ep_count} episodes")

        # Build indices
        plex_show_ids_by_key: dict[str, PlexIds] = {}
        for show in plex_shows:
            plex_show_ids_by_key[str(show.ratingKey)] = extract_plex_ids(show)
        del plex_shows
        gc.collect()

        trakt_watched_episodes: dict[tuple, dict] = {}
        for show_item in trakt_watched_shows:
            if not show_item.show:
                continue
            show_ids = show_item.show.get("ids", {})
            tvdb_id = show_ids.get("tvdb")
            imdb_id = show_ids.get("imdb")
            for season_data in show_item.seasons or []:
                season_num = season_data.get("number", 0)
                for ep_data in season_data.get("episodes", []):
                    ep_num = ep_data.get("number", 0)
                    data = {"show": show_item.show, "last_watched_at": ep_data.get("last_watched_at")}
                    if tvdb_id:
                        trakt_watched_episodes[(tvdb_id, season_num, ep_num)] = data
                    if imdb_id:
                        trakt_watched_episodes[(imdb_id, season_num, ep_num)] = data
        del trakt_watched_shows
        gc.collect()

        trakt_episode_ratings: dict[tuple, dict] = {}
        for item in trakt_episode_ratings_list:
            if not item.episode or not item.show:
                continue
            show_ids = item.show.get("ids", {})
            ep_data = item.episode
            season_num = ep_data.get("season", 0)
            ep_num = ep_data.get("number", 0)
            rating_data = {"rating": item.rating, "rated_at": item.rated_at}
            if show_ids.get("tvdb"):
                trakt_episode_ratings[(show_ids["tvdb"], season_num, ep_num)] = rating_data
            if show_ids.get("imdb"):
                trakt_episode_ratings[(show_ids["imdb"], season_num, ep_num)] = rating_data
        del trakt_episode_ratings_list
        gc.collect()

        # Process episodes in a thread to not block the event loop
        total_episodes = len(plex_episodes)
        self._log(f"  Processing {total_episodes} episodes...")

        # Create cancellation event for thread
        cancel_event = threading.Event()

        # Progress state for thread callback
        progress_state = {"processed": 0, "total": total_episodes}

        def on_progress(processed: int, total: int) -> None:
            progress_state["processed"] = processed
            progress_state["total"] = total

        # Start processing in thread
        process_task = asyncio.create_task(
            asyncio.to_thread(
                _process_episodes_in_thread,
                plex_episodes,
                plex_show_ids_by_key,
                trakt_watched_episodes,
                trakt_episode_ratings,
                self._get_sync_option("watched_plex_to_trakt"),
                self._get_sync_option("watched_trakt_to_plex"),
                self._get_sync_option("ratings_plex_to_trakt"),
                self._get_sync_option("ratings_trakt_to_plex"),
                cancel_event,
                on_progress,
            )
        )

        # Poll for progress and cancellation while thread runs
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TaskProgressColumn(), console=console, transient=False
        ) as progress:
            task = progress.add_task(f"Episodes 0/{total_episodes}", total=total_episodes)

            while not process_task.done():
                # Check for cancellation
                if self._is_cancelled():
                    cancel_event.set()
                    self._log("  [yellow]Cancelling...[/]")

                # Update progress display
                processed = progress_state["processed"]
                pct = 35 + (processed / total_episodes) * 50
                self._progress(2, 4, pct, f"Episodes {processed}/{total_episodes}")
                progress.update(task, completed=processed, description=f"Episodes {processed}/{total_episodes}")

                # Small sleep to not busy-wait
                await asyncio.sleep(0.1)

            # Final progress update
            progress.update(task, completed=total_episodes, description=f"Episodes {total_episodes}/{total_episodes}")

        # Get result from thread
        ep_result = await process_task

        if ep_result.cancelled:
            self._log("  [yellow]Cancelled[/]")
            return False

        # Extract results
        episodes_to_mark_watched_trakt = ep_result.episodes_to_mark_watched_trakt
        episodes_to_mark_watched_trakt_display = ep_result.episodes_to_mark_watched_trakt_display
        episodes_to_mark_watched_plex = ep_result.episodes_to_mark_watched_plex
        episodes_to_rate_trakt = ep_result.episodes_to_rate_trakt
        episodes_to_rate_trakt_display = ep_result.episodes_to_rate_trakt_display
        episodes_to_rate_plex = ep_result.episodes_to_rate_plex

        if ep_result.skipped_no_ids > 0:
            self._log(f"  [yellow]Skipped {ep_result.skipped_no_ids} episodes without show IDs[/]")

        self._log(f"  Episodes - To mark watched on Trakt: {len(episodes_to_mark_watched_trakt)}")
        self._log(f"  Episodes - To mark watched on Plex: {len(episodes_to_mark_watched_plex)}")
        self._log(f"  Episodes - To rate on Trakt: {len(episodes_to_rate_trakt)}")
        self._log(f"  Episodes - To rate on Plex: {len(episodes_to_rate_plex)}")

        if self._verbose:
            for display in episodes_to_mark_watched_trakt_display:
                self._log(f"    [dim]→ Trakt watched: {display}[/]")
            for ep in episodes_to_mark_watched_plex:
                ep_code = f"S{ep.seasonNumber:02d}E{ep.episodeNumber:02d}"
                self._log(f"    [dim]→ Plex watched: {ep.grandparentTitle} {ep_code}[/]")
            for display in episodes_to_rate_trakt_display:
                self._log(f"    [dim]→ Trakt rating: {display}[/]")
            for ep, rating in episodes_to_rate_plex:
                ep_code = f"S{ep.seasonNumber:02d}E{ep.episodeNumber:02d}"
                self._log(f"    [dim]→ Plex rating: {ep.grandparentTitle} {ep_code} = {rating}[/]")

        # Free indices before applying
        del trakt_watched_episodes, trakt_episode_ratings, plex_show_ids_by_key
        gc.collect()

        # Apply changes
        if not dry_run:
            self._progress(2, 4, 90, "Applying episode changes")
            if episodes_to_mark_watched_trakt:
                self._log(f"  Adding {len(episodes_to_mark_watched_trakt)} shows to Trakt history...")
                response = await self.trakt.add_to_history(shows=episodes_to_mark_watched_trakt)
                result.added_to_trakt += response.get("added", {}).get("episodes", 0)

            if episodes_to_rate_trakt:
                self._log(f"  Adding {len(episodes_to_rate_trakt)} episode ratings to Trakt...")
                response = await self.trakt.add_ratings(shows=episodes_to_rate_trakt)
                result.ratings_synced += response.get("added", {}).get("episodes", 0)

            if episodes_to_mark_watched_plex:
                self._log(f"  Marking {len(episodes_to_mark_watched_plex)} episodes watched on Plex...")
                failed = self.plex.mark_watched_batch(episodes_to_mark_watched_plex)
                result.added_to_plex += len(episodes_to_mark_watched_plex) - len(failed)

            if episodes_to_rate_plex:
                self._log(f"  Rating {len(episodes_to_rate_plex)} episodes on Plex...")
                failed = self.plex.rate_batch(episodes_to_rate_plex)
                result.ratings_synced += len(episodes_to_rate_plex) - len(failed)

        self._progress(2, 4, 100, "Episodes complete")
        self._log(f"  [dim]Phase 2 completed in {time.time() - phase_start:.1f}s[/]")

        # Free all episode data
        del episodes_to_mark_watched_trakt, episodes_to_mark_watched_plex
        del episodes_to_rate_trakt, episodes_to_rate_plex
        gc.collect()

        return True

    async def _sync_collection(self, result: SyncResult, dry_run: bool) -> bool:
        """Sync Plex library to Trakt collection. Returns False if cancelled."""
        if not self._get_sync_option("collection_plex_to_trakt"):
            return True

        phase_start = time.time()
        self._log("\n[cyan]Phase 3:[/] Syncing collection...")
        self._progress(3, 4, 0, "Fetching collection data")

        # Check account limits first
        limits = await self._get_account_limits()
        if not limits.is_vip:
            self._log(f"  [yellow]Note: Free Trakt account (limit: {limits.collection_limit} items)[/]")

        # Fetch Trakt collection (use cache if available)
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            transient=True, console=console
        ) as progress:
            if self._trakt_cache:
                trakt_collection_movies = self._trakt_cache.collection_movies
                trakt_collection_shows = self._trakt_cache.collection_shows
                self._progress(3, 4, 10, "Using cached Trakt data")
            else:
                task = progress.add_task("Fetching Trakt collection...", total=None)
                self._progress(3, 4, 5, "Trakt movie collection")
                trakt_collection_movies = await self.trakt.get_collection_movies()
                progress.update(task, description=f"Got {len(trakt_collection_movies)} collected movies")

                task = progress.add_task("Fetching Trakt show collection...", total=None)
                self._progress(3, 4, 10, "Trakt show collection")
                trakt_collection_shows = await self.trakt.get_collection_shows()
                progress.update(task, description=f"Got {len(trakt_collection_shows)} collected shows")

            task = progress.add_task("Fetching Plex movies...", total=None)
            self._progress(3, 4, 15, "Plex movies")
            # Run in thread to not block event loop (allows web UI updates)
            plex_movies = await asyncio.to_thread(
                self.plex.get_all_movies, self._get_movie_libraries()
            )
            progress.update(task, description=f"Got {len(plex_movies)} movies")

            task = progress.add_task("Fetching Plex shows...", total=None)
            self._progress(3, 4, 18, "Plex shows")
            # Run in thread to not block event loop (allows web UI updates)
            plex_shows = await asyncio.to_thread(
                self.plex.get_all_shows, self._get_show_libraries()
            )
            progress.update(task, description=f"Got {len(plex_shows)} shows")

            task = progress.add_task("Fetching Plex episodes...", total=None)
            self._progress(3, 4, 20, "Plex episodes")
            # Run in thread to not block event loop (allows web UI updates)
            plex_episodes = await asyncio.to_thread(
                self.plex.get_all_episodes, self._get_show_libraries()
            )
            progress.update(task, description=f"Got {len(plex_episodes)} episodes")

        current_collection_count = len(trakt_collection_movies) + len(trakt_collection_shows)
        self._log(f"  Trakt collection: {len(trakt_collection_movies)} movies, {len(trakt_collection_shows)} shows")
        self._log(f"  Plex library: {len(plex_movies)} movies, {len(plex_shows)} shows, {len(plex_episodes)} episodes")

        # Warn if non-VIP and near/at limit
        if not limits.is_vip and current_collection_count >= limits.collection_limit:
            limit = limits.collection_limit
            self._log(f"  [yellow]WARNING: Collection at limit ({current_collection_count}/{limit})[/]")
            self._log("  [yellow]Upgrade to Trakt VIP for unlimited collection: https://trakt.tv/vip[/]")
            self._log("  [yellow]Skipping collection sync[/]")
            return True

        # Build indices for Trakt movie collection
        collected_movies_by_imdb: set[str] = set()
        collected_movies_by_tmdb: set[int] = set()

        for item in trakt_collection_movies:
            ids = item.get("movie", {}).get("ids", {})
            if ids.get("imdb"):
                collected_movies_by_imdb.add(ids["imdb"])
            if ids.get("tmdb"):
                collected_movies_by_tmdb.add(ids["tmdb"])

        del trakt_collection_movies
        gc.collect()

        # Build indices for Trakt show collection (episode-level)
        # Track which shows exist and which episodes are collected
        collected_shows_by_imdb: set[str] = set()
        collected_shows_by_tvdb: set[int] = set()
        collected_episodes: set[tuple] = set()  # (show_id, season, episode)

        for item in trakt_collection_shows:
            show = item.get("show", {})
            ids = show.get("ids", {})
            imdb_id = ids.get("imdb")
            tvdb_id = ids.get("tvdb")

            if imdb_id:
                collected_shows_by_imdb.add(imdb_id)
            if tvdb_id:
                collected_shows_by_tvdb.add(tvdb_id)

            # Track collected episodes
            for season in item.get("seasons", []):
                season_num = season.get("number", 0)
                for ep in season.get("episodes", []):
                    ep_num = ep.get("number", 0)
                    if imdb_id:
                        collected_episodes.add((imdb_id, season_num, ep_num))
                    if tvdb_id:
                        collected_episodes.add((tvdb_id, season_num, ep_num))

        del trakt_collection_shows
        gc.collect()

        # Build Plex show ID index
        plex_show_ids_by_key: dict[str, PlexIds] = {}
        for show in plex_shows:
            plex_show_ids_by_key[str(show.ratingKey)] = extract_plex_ids(show)

        # Find movies to add to collection (deduplicate by external ID)
        movies_to_collect: list[dict] = []
        processed_movie_ids: set[str] = set()
        total_movies = len(plex_movies)
        self._log(f"  Processing {total_movies} movies...")

        # Update progress display every 1%, but yield to event loop more often for cancellation
        movie_update_interval = max(1, total_movies // 100)
        movie_yield_interval = max(1, min(500, total_movies // 200))

        for i, plex_movie in enumerate(plex_movies):
            # Yield to event loop frequently for responsive cancellation
            if i % movie_yield_interval == 0:
                await asyncio.sleep(0)
                if self._is_cancelled():
                    self._log("  [yellow]Cancelled[/]")
                    return False

            # Update display less frequently (1%)
            if i % movie_update_interval == 0:
                self._progress(3, 4, 25 + (i / total_movies) * 20, f"Movies {i}/{total_movies}")

            plex_ids = extract_plex_ids(plex_movie)

            # Skip duplicates (same movie in multiple libraries)
            movie_key = plex_ids.imdb or (f"tmdb:{plex_ids.tmdb}" if plex_ids.tmdb else None)
            if movie_key:
                if movie_key in processed_movie_ids:
                    continue
                processed_movie_ids.add(movie_key)

            # Check if already in collection
            in_collection = False
            if plex_ids.imdb and plex_ids.imdb in collected_movies_by_imdb:
                in_collection = True
            elif plex_ids.tmdb and plex_ids.tmdb in collected_movies_by_tmdb:
                in_collection = True

            if not in_collection:
                movie_data = self._build_trakt_movie(plex_movie, plex_ids)
                if movie_data:
                    # Add media metadata
                    metadata = extract_media_metadata(plex_movie)
                    movie_data.update(metadata)
                    movies_to_collect.append(movie_data)

        del plex_movies
        gc.collect()

        # Process episodes - group by show and find what's missing
        # Structure: {show_key: {"ids", "title", "year", "new_show", "episodes": [(s,e,title)]}}
        shows_to_update: dict[str, dict] = {}
        processed_episode_ids: set[tuple] = set()
        total_episodes = len(plex_episodes)
        self._log(f"  Processing {total_episodes} episodes...")

        # Update progress display every 1%, but yield to event loop more often for cancellation
        episode_update_interval = max(1, total_episodes // 100)
        episode_yield_interval = max(1, min(500, total_episodes // 200))

        for i, episode in enumerate(plex_episodes):
            # Yield to event loop frequently for responsive cancellation
            if i % episode_yield_interval == 0:
                await asyncio.sleep(0)
                if self._is_cancelled():
                    self._log("  [yellow]Cancelled[/]")
                    return False

            # Update display less frequently (1%)
            if i % episode_update_interval == 0:
                self._progress(3, 4, 45 + (i / total_episodes) * 30, f"Episodes {i}/{total_episodes}")

            show_key = str(episode.grandparentRatingKey)
            show_ids = plex_show_ids_by_key.get(show_key)
            if not show_ids or (not show_ids.tvdb and not show_ids.imdb):
                continue

            season_num = episode.seasonNumber
            ep_num = episode.episodeNumber

            # Skip duplicates
            ep_key = (show_ids.tvdb or show_ids.imdb, season_num, ep_num)
            if ep_key in processed_episode_ids:
                continue
            processed_episode_ids.add(ep_key)

            # Check if episode already in collection
            in_collection = False
            if show_ids.imdb and (show_ids.imdb, season_num, ep_num) in collected_episodes:
                in_collection = True
            elif show_ids.tvdb and (show_ids.tvdb, season_num, ep_num) in collected_episodes:
                in_collection = True

            if not in_collection:
                # Determine if this is a new show or existing show with new episodes
                show_in_collection = False
                if show_ids.imdb and show_ids.imdb in collected_shows_by_imdb:
                    show_in_collection = True
                elif show_ids.tvdb and show_ids.tvdb in collected_shows_by_tvdb:
                    show_in_collection = True

                # Group episodes by show
                show_data_key = show_ids.imdb or f"tvdb:{show_ids.tvdb}"
                if show_data_key not in shows_to_update:
                    shows_to_update[show_data_key] = {
                        "ids": show_ids,
                        "title": episode.grandparentTitle,
                        "year": getattr(episode, "grandparentYear", None),
                        "new_show": not show_in_collection,
                        "episodes": [],
                    }
                shows_to_update[show_data_key]["episodes"].append(
                    (season_num, ep_num, episode.title)
                )

        del plex_episodes, plex_shows, plex_show_ids_by_key
        gc.collect()

        # Count new shows vs shows with new episodes
        new_shows = [s for s in shows_to_update.values() if s["new_show"]]
        existing_shows_with_new_eps = [s for s in shows_to_update.values() if not s["new_show"]]
        total_new_episodes = sum(len(s["episodes"]) for s in shows_to_update.values())

        self._log(f"  Collection - Movies to add: {len(movies_to_collect)}")
        self._log(f"  Collection - New shows to add: {len(new_shows)}")
        self._log(f"  Collection - Existing shows with new episodes: {len(existing_shows_with_new_eps)}")
        self._log(f"  Collection - Total new episodes: {total_new_episodes}")

        if self._verbose:
            for m in movies_to_collect:
                self._log(f"    [dim]→ Collection: {m.get('title')} ({m.get('year')})[/]")
            # New shows - just show name
            for s in new_shows:
                year_str = f" ({s['year']})" if s['year'] else ""
                self._log(f"    [dim]→ Collection: {s['title']}{year_str}[/]")
            # Existing shows - show episode details
            for s in existing_shows_with_new_eps:
                episodes = s["episodes"]
                if len(episodes) <= 5:
                    ep_list = ", ".join(f"S{se:02d}E{ep:02d}" for se, ep, _ in episodes)
                else:
                    first_5 = ", ".join(f"S{se:02d}E{ep:02d}" for se, ep, _ in episodes[:5])
                    ep_list = f"{first_5} (+{len(episodes)-5} more)"
                self._log(f"    [dim]→ Collection: {s['title']} - {ep_list}[/]")

        # Build Trakt show objects with episode data
        shows_to_collect: list[dict] = []
        for show_data in shows_to_update.values():
            ids = {}
            if show_data["ids"].imdb:
                ids["imdb"] = show_data["ids"].imdb
            if show_data["ids"].tvdb:
                ids["tvdb"] = show_data["ids"].tvdb
            if not ids:
                continue

            # Group episodes by season
            seasons_dict: dict[int, list[dict]] = {}
            for season_num, ep_num, _ in show_data["episodes"]:
                if season_num not in seasons_dict:
                    seasons_dict[season_num] = []
                seasons_dict[season_num].append({"number": ep_num})

            seasons = [{"number": s, "episodes": eps} for s, eps in sorted(seasons_dict.items())]

            shows_to_collect.append({
                "title": show_data["title"],
                "year": show_data["year"],
                "ids": ids,
                "seasons": seasons,
            })

        # Apply changes
        if not dry_run:
            self._progress(3, 4, 80, "Adding to Trakt collection")
            try:
                if movies_to_collect:
                    self._log(f"  Adding {len(movies_to_collect)} movies to Trakt collection...")
                    response = await self.trakt.add_to_collection(movies=movies_to_collect)
                    result.collection_added += response.get("added", {}).get("movies", 0)

                if shows_to_collect:
                    n_shows = len(shows_to_collect)
                    self._log(f"  Adding {n_shows} shows ({total_new_episodes} episodes) to Trakt collection...")
                    response = await self.trakt.add_to_collection(shows=shows_to_collect)
                    result.collection_added += response.get("added", {}).get("episodes", 0)
            except TraktAccountLimitError as e:
                self._log(f"  [red]ERROR: {e}[/]")
                if not e.is_vip:
                    self._log(f"  [yellow]Upgrade to Trakt VIP for unlimited collection: {e.upgrade_url}[/]")
                result.errors.append(f"Collection limit exceeded: {e}")

        self._progress(3, 4, 100, "Collection complete")
        self._log(f"  [dim]Phase 3 completed in {time.time() - phase_start:.1f}s[/]")

        del movies_to_collect, shows_to_collect, shows_to_update
        del collected_movies_by_imdb, collected_movies_by_tmdb
        del collected_shows_by_imdb, collected_shows_by_tvdb, collected_episodes
        gc.collect()

        return True

    def _build_trakt_show(self, plex_show: Any, plex_ids: Any) -> dict | None:
        """Build Trakt show object from Plex data."""
        ids = {}
        if plex_ids.imdb:
            ids["imdb"] = plex_ids.imdb
        if plex_ids.tvdb:
            ids["tvdb"] = plex_ids.tvdb

        if not ids:
            return None

        return {
            "title": plex_show.title,
            "year": plex_show.year,
            "ids": ids,
        }

    async def _sync_watchlist(self, result: SyncResult, dry_run: bool) -> bool:
        """Sync watchlists between Plex and Trakt. Returns False if cancelled."""
        if not (self._get_sync_option("watchlist_plex_to_trakt") or self._get_sync_option("watchlist_trakt_to_plex")):
            return True

        phase_start = time.time()
        self._log("\n[cyan]Phase 4:[/] Syncing watchlist...")
        self._progress(4, 4, 0, "Fetching watchlist data")

        # Check account limits
        limits = await self._get_account_limits()
        if not limits.is_vip and self._get_sync_option("watchlist_plex_to_trakt"):
            self._log(f"  [yellow]Note: Free Trakt account (limit: {limits.watchlist_limit} items)[/]")

        # Fetch watchlists (use cache if available for Trakt)
        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            transient=True, console=console
        ) as progress:
            task = progress.add_task("Fetching Plex watchlist...", total=None)
            self._progress(4, 4, 5, "Plex watchlist")
            plex_watchlist = self.plex.get_watchlist()
            progress.update(task, description=f"Got {len(plex_watchlist)} Plex watchlist items")

            if self._trakt_cache:
                trakt_watchlist_movies = self._trakt_cache.watchlist_movies
                trakt_watchlist_shows = self._trakt_cache.watchlist_shows
                self._progress(4, 4, 15, "Using cached Trakt data")
            else:
                task = progress.add_task("Fetching Trakt watchlist...", total=None)
                self._progress(4, 4, 15, "Trakt watchlist")
                trakt_watchlist_movies = await self.trakt.get_watchlist_movies()
                trakt_watchlist_shows = await self.trakt.get_watchlist_shows()
            n_movies, n_shows = len(trakt_watchlist_movies), len(trakt_watchlist_shows)
            progress.update(task, description=f"Got {n_movies} movies, {n_shows} shows")

        trakt_watchlist_count = len(trakt_watchlist_movies) + len(trakt_watchlist_shows)
        self._log(f"  Plex watchlist: {len(plex_watchlist)} items")
        self._log(f"  Trakt watchlist: {len(trakt_watchlist_movies)} movies, {len(trakt_watchlist_shows)} shows")

        # Check if Trakt watchlist is at limit for Plex -> Trakt sync
        skip_plex_to_trakt = False
        if not limits.is_vip and trakt_watchlist_count >= limits.watchlist_limit:
            limit = limits.watchlist_limit
            self._log(f"  [yellow]WARNING: Trakt watchlist at limit ({trakt_watchlist_count}/{limit})[/]")
            self._log("  [yellow]Upgrade to Trakt VIP for unlimited watchlist: https://trakt.tv/vip[/]")
            if self._get_sync_option("watchlist_plex_to_trakt"):
                self._log("  [yellow]Skipping Plex → Trakt watchlist sync[/]")
                skip_plex_to_trakt = True

        # Build indices for Trakt watchlist
        trakt_watchlist_by_imdb: set[str] = set()
        trakt_watchlist_by_tmdb: set[int] = set()
        trakt_watchlist_by_tvdb: set[int] = set()

        for item in trakt_watchlist_movies:
            ids = item.get("movie", {}).get("ids", {})
            if ids.get("imdb"):
                trakt_watchlist_by_imdb.add(ids["imdb"])
            if ids.get("tmdb"):
                trakt_watchlist_by_tmdb.add(ids["tmdb"])

        for item in trakt_watchlist_shows:
            ids = item.get("show", {}).get("ids", {})
            if ids.get("imdb"):
                trakt_watchlist_by_imdb.add(ids["imdb"])
            if ids.get("tvdb"):
                trakt_watchlist_by_tvdb.add(ids["tvdb"])

        # Build index for Plex watchlist
        plex_watchlist_by_imdb: set[str] = set()
        plex_watchlist_by_tmdb: set[int] = set()
        plex_watchlist_by_tvdb: set[int] = set()

        for item in plex_watchlist:
            plex_ids = extract_plex_ids(item)
            if plex_ids.imdb:
                plex_watchlist_by_imdb.add(plex_ids.imdb)
            if plex_ids.tmdb:
                plex_watchlist_by_tmdb.add(plex_ids.tmdb)
            if plex_ids.tvdb:
                plex_watchlist_by_tvdb.add(plex_ids.tvdb)

        # Plex -> Trakt: Find items in Plex watchlist but not Trakt
        movies_to_add_trakt: list[dict] = []
        shows_to_add_trakt: list[dict] = []

        if self._get_sync_option("watchlist_plex_to_trakt") and not skip_plex_to_trakt:
            self._progress(4, 4, 30, "Comparing Plex → Trakt")
            for item in plex_watchlist:
                if self._is_cancelled():
                    self._log("  [yellow]Cancelled[/]")
                    return False

                plex_ids = extract_plex_ids(item)

                # Check if already on Trakt watchlist
                on_trakt = False
                if plex_ids.imdb and plex_ids.imdb in trakt_watchlist_by_imdb:
                    on_trakt = True
                elif plex_ids.tmdb and plex_ids.tmdb in trakt_watchlist_by_tmdb:
                    on_trakt = True
                elif plex_ids.tvdb and plex_ids.tvdb in trakt_watchlist_by_tvdb:
                    on_trakt = True

                if not on_trakt:
                    # Determine if movie or show
                    item_type = getattr(item, "TYPE", None) or getattr(item, "type", None)
                    if item_type == "movie":
                        movie_data = self._build_trakt_movie(item, plex_ids)
                        if movie_data:
                            movies_to_add_trakt.append(movie_data)
                    elif item_type == "show":
                        show_data = self._build_trakt_show(item, plex_ids)
                        if show_data:
                            shows_to_add_trakt.append(show_data)

        # Trakt -> Plex: Find items in Trakt watchlist but not Plex
        items_to_add_plex: list[Any] = []

        if self._get_sync_option("watchlist_trakt_to_plex"):
            self._progress(4, 4, 50, "Comparing Trakt → Plex")

            # Process Trakt movies
            for item in trakt_watchlist_movies:
                if self._is_cancelled():
                    self._log("  [yellow]Cancelled[/]")
                    return False

                ids = item.get("movie", {}).get("ids", {})
                title = item.get("movie", {}).get("title", "")

                # Check if already on Plex watchlist
                on_plex = False
                if ids.get("imdb") and ids["imdb"] in plex_watchlist_by_imdb:
                    on_plex = True
                elif ids.get("tmdb") and ids["tmdb"] in plex_watchlist_by_tmdb:
                    on_plex = True

                if not on_plex and title:
                    # Search Plex Discover for this movie
                    try:
                        results = self.plex.search_discover(title, libtype="movie")
                        for result in results[:5]:  # Check top 5 results
                            result_ids = extract_plex_ids(result)
                            if (ids.get("imdb") and result_ids.imdb == ids["imdb"]) or \
                               (ids.get("tmdb") and result_ids.tmdb == ids["tmdb"]):
                                items_to_add_plex.append(result)
                                break
                    except Exception as e:
                        self._log(f"  [yellow]Warning: Could not search for '{title}': {e}[/]")

            # Process Trakt shows
            for item in trakt_watchlist_shows:
                if self._is_cancelled():
                    self._log("  [yellow]Cancelled[/]")
                    return False

                ids = item.get("show", {}).get("ids", {})
                title = item.get("show", {}).get("title", "")

                # Check if already on Plex watchlist
                on_plex = False
                if ids.get("imdb") and ids["imdb"] in plex_watchlist_by_imdb:
                    on_plex = True
                elif ids.get("tvdb") and ids["tvdb"] in plex_watchlist_by_tvdb:
                    on_plex = True

                if not on_plex and title:
                    # Search Plex Discover for this show
                    try:
                        results = self.plex.search_discover(title, libtype="show")
                        for result in results[:5]:  # Check top 5 results
                            result_ids = extract_plex_ids(result)
                            if (ids.get("imdb") and result_ids.imdb == ids["imdb"]) or \
                               (ids.get("tvdb") and result_ids.tvdb == ids["tvdb"]):
                                items_to_add_plex.append(result)
                                break
                    except Exception as e:
                        self._log(f"  [yellow]Warning: Could not search for '{title}': {e}[/]")

        self._log(f"  Watchlist - To add to Trakt: {len(movies_to_add_trakt)} movies, {len(shows_to_add_trakt)} shows")
        self._log(f"  Watchlist - To add to Plex: {len(items_to_add_plex)} items")

        if self._verbose:
            for m in movies_to_add_trakt:
                self._log(f"    [dim]→ Trakt watchlist: {m.get('title')} ({m.get('year')})[/]")
            for s in shows_to_add_trakt:
                self._log(f"    [dim]→ Trakt watchlist: {s.get('title')} ({s.get('year')})[/]")
            for item in items_to_add_plex:
                self._log(f"    [dim]→ Plex watchlist: {item.get('title')} ({item.get('year')})[/]")

        # Apply changes
        if not dry_run:
            self._progress(4, 4, 80, "Applying watchlist changes")

            if movies_to_add_trakt or shows_to_add_trakt:
                self._log("  Adding to Trakt watchlist...")
                try:
                    if movies_to_add_trakt:
                        response = await self.trakt.add_to_watchlist(movies=movies_to_add_trakt)
                        result.watchlist_added_trakt += response.get("added", {}).get("movies", 0)
                    if shows_to_add_trakt:
                        response = await self.trakt.add_to_watchlist(shows=shows_to_add_trakt)
                        result.watchlist_added_trakt += response.get("added", {}).get("shows", 0)
                except TraktAccountLimitError as e:
                    self._log(f"  [red]ERROR: {e}[/]")
                    if not e.is_vip:
                        self._log(f"  [yellow]Upgrade to Trakt VIP for unlimited watchlist: {e.upgrade_url}[/]")
                    result.errors.append(f"Watchlist limit exceeded: {e}")

            for item in items_to_add_plex:
                try:
                    self.plex.add_to_watchlist(item)
                    result.watchlist_added_plex += 1
                except Exception as e:
                    self._log(f"  [yellow]Warning: Could not add to Plex watchlist: {e}[/]")

        self._progress(4, 4, 100, "Watchlist complete")
        self._log(f"  [dim]Phase 4 completed in {time.time() - phase_start:.1f}s[/]")

        # Cleanup
        del plex_watchlist, trakt_watchlist_movies, trakt_watchlist_shows
        del trakt_watchlist_by_imdb, trakt_watchlist_by_tmdb, trakt_watchlist_by_tvdb
        del plex_watchlist_by_imdb, plex_watchlist_by_tmdb, plex_watchlist_by_tvdb
        gc.collect()

        return True

    async def sync(self, dry_run: bool = False) -> SyncResult | None:
        """Run full sync."""
        start_time = time.time()
        result = SyncResult()

        logger = get_file_logger()
        logger.info("=" * 60)
        logger.info(f"SYNC STARTED - dry_run={dry_run}")
        logger.info("=" * 60)

        self._log("[bold]Starting Pakt sync...[/]")
        self._log(f"  Mode: {'Dry run' if dry_run else 'Live sync'}")

        # Sync movies (fetch, compare, apply, free)
        if not await self._sync_movies(result, dry_run):
            return None  # Cancelled

        # Sync episodes (fetch, compare, apply, free)
        if not await self._sync_episodes(result, dry_run):
            return None  # Cancelled

        # Sync collection (Plex library -> Trakt collection)
        if not await self._sync_collection(result, dry_run):
            return None  # Cancelled

        # Sync watchlist (bidirectional)
        if not await self._sync_watchlist(result, dry_run):
            return None  # Cancelled

        if dry_run:
            self._log("\n[yellow]Dry run complete - no changes applied[/]")
        else:
            self._log("\n[green]Sync complete![/]")

        result.duration_seconds = time.time() - start_time
        return result

    def _build_trakt_movie(self, plex_movie: Any, plex_ids: Any) -> dict | None:
        """Build Trakt movie object from Plex data."""
        ids = {}
        if plex_ids.imdb:
            ids["imdb"] = plex_ids.imdb
        if plex_ids.tmdb:
            ids["tmdb"] = plex_ids.tmdb

        if not ids:
            return None

        return {
            "title": plex_movie.title,
            "year": plex_movie.year,
            "ids": ids,
        }


async def run_multi_server_sync(
    config: Config,
    server_names: list[str] | None = None,
    dry_run: bool = False,
    verbose: bool = False,
    on_token_refresh: Callable[[dict], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> SyncResult:
    """Run sync across multiple Plex servers.

    Args:
        config: Main configuration
        server_names: Optional list of server names to sync. If None, syncs all enabled servers.
        dry_run: If True, don't make changes
        verbose: Show detailed output
        on_token_refresh: Callback when Trakt token is refreshed
        log_callback: Callback for log messages
        cancel_check: Callback to check if sync was cancelled

    Returns:
        Aggregated SyncResult from all servers
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)

    # Determine which servers to sync
    if server_names:
        servers_to_sync = []
        for name in server_names:
            server = config.get_server(name)
            if server:
                servers_to_sync.append(server)
            else:
                log(f"WARNING:Server '{name}' not found in configuration")
    else:
        servers_to_sync = config.get_enabled_servers()

    if not servers_to_sync:
        log("ERROR:No servers configured or enabled for sync")
        return SyncResult()

    # Aggregate results across all servers
    total_result = SyncResult()
    start_time = time.time()
    server_count = len(servers_to_sync)

    log(f"[bold]Starting sync across {server_count} server(s)...[/]")

    async with TraktClient(config.trakt, on_token_refresh=on_token_refresh) as trakt:
        # Pre-fetch all Trakt data once for multi-server efficiency
        trakt_cache: TraktCache | None = None
        if server_count > 1:
            log("[bold]Pre-fetching Trakt data for all servers...[/]")
            with Progress(
                SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                transient=True, console=console
            ) as progress:
                task = progress.add_task("Fetching account limits...", total=None)
                account_limits = await trakt.get_account_limits()

                task = progress.add_task("Fetching watched movies...", total=None)
                watched_movies = await trakt.get_watched_movies()
                progress.update(task, description=f"Got {len(watched_movies)} watched movies")

                task = progress.add_task("Fetching movie ratings...", total=None)
                movie_ratings = await trakt.get_movie_ratings()
                progress.update(task, description=f"Got {len(movie_ratings)} movie ratings")

                task = progress.add_task("Fetching watched shows...", total=None)
                watched_shows = await trakt.get_watched_shows()
                progress.update(task, description=f"Got {len(watched_shows)} watched shows")

                task = progress.add_task("Fetching episode ratings...", total=None)
                episode_ratings = await trakt.get_episode_ratings()
                progress.update(task, description=f"Got {len(episode_ratings)} episode ratings")

                task = progress.add_task("Fetching collection...", total=None)
                collection_movies = await trakt.get_collection_movies()
                collection_shows = await trakt.get_collection_shows()
                progress.update(task, description=f"Got {len(collection_movies)} movies, {len(collection_shows)} shows")

                task = progress.add_task("Fetching watchlist...", total=None)
                watchlist_movies = await trakt.get_watchlist_movies()
                watchlist_shows = await trakt.get_watchlist_shows()
                progress.update(task, description=f"Got {len(watchlist_movies)} movies, {len(watchlist_shows)} shows")

            trakt_cache = TraktCache(
                account_limits=account_limits,
                watched_movies=watched_movies,
                movie_ratings=movie_ratings,
                watched_shows=watched_shows,
                episode_ratings=episode_ratings,
                collection_movies=collection_movies,
                collection_shows=collection_shows,
                watchlist_movies=watchlist_movies,
                watchlist_shows=watchlist_shows,
            )
            log(f"  Cached: {len(watched_movies)} movies, {len(watched_shows)} shows, "
                f"{len(collection_movies)} collection")

        for idx, server_config in enumerate(servers_to_sync, 1):
            if cancel_check and cancel_check():
                log("WARNING:Sync cancelled")
                break

            log(f"\n[bold cyan]═══ Server {idx}/{server_count}: {server_config.name} ═══[/]")

            try:
                # Create PlexClient from server config
                plex = PlexClient(server_config)
                plex.connect()
                log(f"Connected to: {plex.server.friendlyName}")

                # Create SyncEngine with server context and shared cache
                engine = SyncEngine(
                    config, trakt, plex,
                    log_callback=log_callback,
                    cancel_check=cancel_check,
                    verbose=verbose,
                    server_name=server_config.name,
                    server_config=server_config,
                    trakt_cache=trakt_cache,
                )

                # Run sync for this server
                result = await engine.sync(dry_run=dry_run)

                if result:
                    # Aggregate results
                    total_result.added_to_trakt += result.added_to_trakt
                    total_result.added_to_plex += result.added_to_plex
                    total_result.ratings_synced += result.ratings_synced
                    total_result.collection_added += result.collection_added
                    total_result.watchlist_added_trakt += result.watchlist_added_trakt
                    total_result.watchlist_added_plex += result.watchlist_added_plex
                    total_result.errors.extend(result.errors)

            except Exception as e:
                error_msg = f"[{server_config.name}] Error: {e}"
                log(f"ERROR:{error_msg}")
                total_result.errors.append(error_msg)
                # Continue with next server instead of failing entirely

    total_result.duration_seconds = time.time() - start_time

    if server_count > 1:
        log("\n[bold green]═══ Multi-Server Sync Complete ═══[/]")
        log(f"  Servers synced: {server_count}")
        log(f"  Total added to Trakt: {total_result.added_to_trakt}")
        log(f"  Total added to Plex: {total_result.added_to_plex}")
        log(f"  Total ratings synced: {total_result.ratings_synced}")
        log(f"  Duration: {total_result.duration_seconds:.1f}s")

    return total_result
