"""Sync logic for Pakt."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
from collections.abc import Awaitable, Generator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from rich.console import Console
from rich.errors import MarkupError
from rich.markup import escape
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.text import Text

from pakt.config import Config, ServerConfig, get_config_dir, set_last_sync
from pakt.models import PlexIds, RatedItem, SyncResult, WatchedItem
from pakt.plex import PlexAuthError, PlexClient, extract_media_metadata, extract_plex_ids, media_score
from pakt.trakt import TraktAccountLimitError, TraktAuthError, TraktClient

console = Console()

T = TypeVar("T")

MOVIE_ID_TYPES = ("imdb", "tmdb")
SHOW_ID_TYPES = ("tvdb", "imdb", "tmdb")
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
LOG_MAX_BYTES = 5 * 1024 * 1024


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
    """Items sent to Trakt this run, which later servers must not post again from the cache."""

    watched_movies: set[tuple] = field(default_factory=set)
    rated_movies: set[tuple] = field(default_factory=set)
    watched_episodes: set[tuple] = field(default_factory=set)
    rated_episodes: set[tuple] = field(default_factory=set)
    watchlist_accounts: set[str] = field(default_factory=set)


@dataclass
class EpisodeProcessingResult:
    """Episode changes worked out in the worker thread, each with a display label."""

    watched_to_trakt: list[tuple[dict, int, dict, str]] = field(default_factory=list)
    watched_to_plex: list[tuple[Any, str]] = field(default_factory=list)
    rated_to_trakt: list[tuple[dict, int, dict, str]] = field(default_factory=list)
    rated_to_plex: list[tuple[Any, int, str]] = field(default_factory=list)
    watched_keys: set[tuple] = field(default_factory=set)
    rated_keys: set[tuple] = field(default_factory=set)
    skipped_no_ids: int = 0
    skipped_no_number: int = 0
    cancelled: bool = False


def _safe(text: Any) -> str:
    """Make outside text safe in a markup log line: markup escaped, control characters gone."""
    return escape(CONTROL_CHARS.sub("", str(text)))


def _id_keys(ids: PlexIds | dict[str, Any], kinds: tuple[str, ...]) -> list[tuple[str, Any]]:
    """Typed id pairs in order of preference; ids of different kinds never collide."""
    if isinstance(ids, dict):
        values = [ids.get(kind) for kind in kinds]
    else:
        values = [getattr(ids, kind, None) for kind in kinds]
    return [(kind, value) for kind, value in zip(kinds, values) if value]


def _trakt_item(item: Any, plex_ids: PlexIds, kinds: tuple[str, ...]) -> dict | None:
    """Trakt movie or show object for a Plex item, or None when it carries none of the ids."""
    ids = dict(_id_keys(plex_ids, kinds))
    if not ids:
        return None
    return {"title": item.title, "year": item.year, "ids": ids}


def _group_by_show(rows: list[tuple[dict, int, dict, str]]) -> list[dict]:
    """Nest episode entries under their show and season, the shape Trakt's sync endpoints take."""
    shows: dict[tuple, dict] = {}
    for show, season, episode, _label in rows:
        entry = shows.setdefault(tuple(sorted(show["ids"].items())), {**show, "seasons": {}})
        entry["seasons"].setdefault(season, []).append(episode)
    return [
        {**show, "seasons": [{"number": n, "episodes": eps} for n, eps in sorted(show["seasons"].items())]}
        for show in shows.values()
    ]


def _rating_direction(
    plex: int | None, trakt: int | None, priority: str, to_trakt: bool, to_plex: bool
) -> tuple[bool, bool]:
    """Which way a rating should flow, as (send Plex's to Trakt, send Trakt's to Plex)."""
    if plex and not trakt:
        return to_trakt, False
    if trakt and not plex:
        return False, to_plex
    if plex and trakt and plex != trakt:
        return priority == "plex" and to_trakt, priority == "trakt" and to_plex
    return False, False


def _episode_index(shows: list[WatchedItem]) -> set[tuple]:
    """Keys of every watched episode in Trakt's watched shows, one per show id kind."""
    index: set[tuple] = set()
    for show_item in shows:
        keys = _id_keys(show_item.show.get("ids", {}), SHOW_ID_TYPES) if show_item.show else []
        for season in show_item.seasons or []:
            for ep in season.get("episodes", []):
                if _is_watched_episode(ep):
                    index.update((key, season.get("number", 0), ep.get("number", 0)) for key in keys)
    return index


def _episode_ratings(ratings: list[RatedItem]) -> dict[tuple, int]:
    """Trakt episode ratings keyed like the watched episode index."""
    index: dict[tuple, int] = {}
    for item in ratings:
        if item.episode and item.show:
            season, number = item.episode.get("season", 0), item.episode.get("number", 0)
            for key in _id_keys(item.show.get("ids", {}), SHOW_ID_TYPES):
                index[(key, season, number)] = item.rating
    return index


def _process_episodes_in_thread(
    plex_episodes: list[Any],
    plex_show_ids_by_key: dict[str, PlexIds],
    trakt_watched: set[tuple],
    trakt_ratings: dict[tuple, int],
    options: dict[str, bool],
    rating_priority: str,
    cancel_event: threading.Event,
    pushed: PushedToTrakt,
    progress_callback: Callable[[int, int], None] | None = None,
) -> EpisodeProcessingResult:
    """Compare Plex episodes against Trakt in a worker thread, off the event loop."""
    result = EpisodeProcessingResult()
    total = len(plex_episodes)

    # read each field once; PlexAPI reloads over the network on a None attribute
    rows: list[tuple] = []
    for i, ep in enumerate(plex_episodes):
        if i % 100 == 0 and cancel_event.is_set():
            result.cancelled = True
            return result
        if i % 500 == 0 and progress_callback:
            progress_callback(i, total)
        rows.append((
            str(ep.grandparentRatingKey), ep.parentIndex, ep.index, bool(ep.viewCount), ep.userRating,
            ep.grandparentTitle, _plex_dt_to_utc_iso(ep.lastViewedAt), _plex_dt_to_utc_iso(ep.lastRatedAt), ep,
        ))
    if progress_callback:
        progress_callback(total, total)

    for i, (show_key, season, number, plex_watched, plex_rating, show_title,
            watched_at, rated_at, episode) in enumerate(rows):
        if i % 500 == 0 and cancel_event.is_set():
            result.cancelled = True
            return result
        show_ids = plex_show_ids_by_key.get(show_key)
        keys = _id_keys(show_ids, SHOW_ID_TYPES) if show_ids else []
        if not keys:
            result.skipped_no_ids += 1
            continue
        if season is None or number is None:
            result.skipped_no_number += 1
            continue

        ep_key = (keys[0], season, number)
        label = f"{show_title} S{season:02d}E{number:02d}"
        show = {"ids": dict(keys)}

        trakt_has_watch = any((key, season, number) in trakt_watched for key in keys)
        if plex_watched and not trakt_has_watch:
            if (options["watched_plex_to_trakt"] and ep_key not in pushed.watched_episodes
                    and ep_key not in result.watched_keys):
                entry: dict[str, Any] = {"number": number}
                if watched_at:
                    entry["watched_at"] = watched_at
                result.watched_to_trakt.append((show, season, entry, label))
                result.watched_keys.add(ep_key)
        elif trakt_has_watch and not plex_watched and options["watched_trakt_to_plex"]:
            result.watched_to_plex.append((episode, label))

        plex_rating_int = int(plex_rating) if plex_rating else None
        trakt_rating = next(
            (trakt_ratings[(key, season, number)] for key in keys if (key, season, number) in trakt_ratings), None
        )
        send, fetch = _rating_direction(
            plex_rating_int, trakt_rating, rating_priority,
            options["ratings_plex_to_trakt"], options["ratings_trakt_to_plex"],
        )
        if send and ep_key not in pushed.rated_episodes and ep_key not in result.rated_keys:
            entry = {"number": number, "rating": plex_rating_int}
            if rated_at:
                entry["rated_at"] = rated_at
            result.rated_to_trakt.append((show, season, entry, f"{label} = {plex_rating_int}"))
            result.rated_keys.add(ep_key)
        elif fetch and trakt_rating is not None:
            result.rated_to_plex.append((episode, trakt_rating, f"{label} = {trakt_rating}"))

    return result


def _plan_watchlist(
    plex: PlexClient,
    trakt_movies: list[dict],
    trakt_shows: list[dict],
    just_watched: set[tuple],
    options: dict[str, bool],
    is_cancelled: Callable[[], bool],
    warn: Callable[[str], None],
) -> tuple[list[dict], list[dict], list[Any], int] | None:
    """Plan watchlist additions both ways in a worker thread; None if cancelled."""
    plex_watchlist = plex.get_watchlist()
    plex_movie_keys: set[tuple] = set()
    plex_show_keys: set[tuple] = set()
    plex_entries = []
    for item in plex_watchlist:
        plex_ids = extract_plex_ids(item)
        is_show = (getattr(item, "TYPE", None) or getattr(item, "type", None)) == "show"
        keys = _id_keys(plex_ids, SHOW_ID_TYPES if is_show else MOVIE_ID_TYPES)
        (plex_show_keys if is_show else plex_movie_keys).update(keys)
        plex_entries.append((item, plex_ids, is_show, keys))

    trakt_movie_keys = {k for i in trakt_movies for k in _id_keys(i.get("movie", {}).get("ids", {}), MOVIE_ID_TYPES)}
    trakt_show_keys = {k for i in trakt_shows for k in _id_keys(i.get("show", {}).get("ids", {}), SHOW_ID_TYPES)}

    movies_to_trakt: list[dict] = []
    shows_to_trakt: list[dict] = []
    if options["watchlist_plex_to_trakt"]:
        for item, plex_ids, is_show, keys in plex_entries:
            on_trakt = any(k in (trakt_show_keys if is_show else trakt_movie_keys) for k in keys)
            # Trakt drops a movie from its watchlist once it is watched
            if on_trakt or (not is_show and any(k in just_watched for k in keys)):
                continue
            data = _trakt_item(item, plex_ids, SHOW_ID_TYPES if is_show else MOVIE_ID_TYPES)
            if data:
                (shows_to_trakt if is_show else movies_to_trakt).append(data)

    items_to_plex: list[Any] = []
    if options["watchlist_trakt_to_plex"]:
        wanted = [("movie", i.get("movie", {}), MOVIE_ID_TYPES, plex_movie_keys) for i in trakt_movies]
        wanted += [("show", i.get("show", {}), SHOW_ID_TYPES, plex_show_keys) for i in trakt_shows]
        for libtype, trakt_item, kinds, plex_keys in wanted:
            if is_cancelled():
                return None
            keys = _id_keys(trakt_item.get("ids", {}), kinds)
            title = trakt_item.get("title", "")
            if not title or any(k in plex_keys for k in keys):
                continue
            try:
                for match in plex.search_discover(title, libtype=libtype)[:5]:
                    if any(k in keys for k in _id_keys(extract_plex_ids(match), kinds)):
                        items_to_plex.append(match)
                        break
            except Exception as e:
                warn(f"  [yellow]Warning: Could not search for '{_safe(title)}': {_safe(e)}[/]")

    return movies_to_trakt, shows_to_trakt, items_to_plex, len(plex_watchlist)


_file_logger: logging.Logger | None = None


def get_file_logger() -> logging.Logger:
    """Get the pakt logger, writing every pakt module's messages to a rotating sync.log."""
    global _file_logger
    if _file_logger is None:
        log_file = get_config_dir() / "sync.log"
        log_file.touch(exist_ok=True)
        if os.name != "nt":
            log_file.chmod(0o600)
        handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        _file_logger = logging.getLogger("pakt")
        _file_logger.setLevel(logging.INFO)
        _file_logger.addHandler(handler)
    return _file_logger


def _emit(markup: str, log_callback: Callable[[str], None] | None) -> None:
    """Emit a markup line to the console, and its plain text to sync.log and the caller."""
    try:
        plain = Text.from_markup(markup).plain
    except MarkupError:
        plain, markup = markup, escape(markup)
    get_file_logger().info(plain)
    console.print(markup)
    if log_callback:
        log_callback(plain)


@contextlib.contextmanager
def _sync_lock() -> Generator[bool, None, None]:
    """Hold the config folder's sync lock, one process at a time; yields False when taken."""
    handle = open(get_config_dir() / "sync.lock", "a+")
    try:
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        handle.close()


def _is_watched_episode(ep_data: dict) -> bool:
    """Whether a Trakt episode is watched; extended=progress also lists unwatched ones."""
    completed = ep_data.get("completed")
    if completed is False:
        return False
    if completed is None:
        return bool(ep_data.get("last_watched_at") or ep_data.get("plays"))
    return True


def _plex_dt_to_utc_iso(dt: datetime | None) -> str | None:
    """Convert PlexAPI's naive local datetime to the UTC ISO 8601 form Trakt takes."""
    if dt is None:
        return None
    # PlexAPI gives naive local time; astimezone() assumes local if no tzinfo
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


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
        pushed: PushedToTrakt | None = None,
    ):
        self.config = config
        self.trakt = trakt
        self.plex = plex
        self._log_callback = log_callback
        self._cancel_check = cancel_check
        self._verbose = verbose
        self._server_name = server_name
        self._server_config = server_config
        self._cache = trakt_cache or TraktCache()
        self._pushed = pushed or PushedToTrakt()
        self._fix_collection_dates = False
        self._plex_cache: dict[str, tuple[list, dict[str, int]]] = {}
        self._keep_plex = False

    def _get_sync_option(self, option: str) -> bool:
        """Get effective sync option, checking server override first."""
        if self._server_config:
            return self._server_config.get_sync_option(option, self.config.sync)
        return getattr(self.config.sync, option)

    def _options(self) -> dict[str, bool]:
        """Every on/off sync option for this server, by name."""
        return {name: self._get_sync_option(name) for name in type(self.config.sync).model_fields
                if name != "rating_priority"}

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

    def _is_cancelled(self) -> bool:
        """Check if sync has been cancelled."""
        return self._cancel_check() if self._cancel_check else False

    def _log(self, msg: str) -> None:
        """Log a markup message to console, callback and sync.log, prefixed with the server."""
        if self._server_name:
            msg = f"[dim]{_safe(f'[{self._server_name}]')}[/] {msg}"
        _emit(msg, self._log_callback)

    def _progress(self, phase: int, total_phases: int, percent: float, label: str = "") -> None:
        """Send progress update to callback and log."""
        phase_weight = 100 / total_phases
        overall = ((phase - 1) * phase_weight) + (percent / 100 * phase_weight)
        if self._log_callback:
            self._log_callback(f"PROGRESS:{phase}:{overall:.1f}:{label}")

    async def _fetch_plex(
        self, fetch: Callable[[list[str] | None], tuple[list, dict[str, int]]],
        libraries: list[str] | None, warn: bool = True,
    ) -> tuple[list, dict[str, int]]:
        """Fetch Plex items off the event loop, reusing a kept copy; warns of missing libraries."""
        if fetch.__name__ in self._plex_cache:
            return self._plex_cache[fetch.__name__]
        items, counts = await asyncio.to_thread(fetch, libraries)
        if self._keep_plex:
            self._plex_cache[fetch.__name__] = (items, counts)
        for name in (libraries or []) if warn else []:
            if name not in counts:
                self._log(f"  [yellow]Library '{_safe(name)}' not found on this server[/]")
        return items, counts

    async def _push(self, message: str, send: Callable[..., Awaitable[dict]], **lists: list[dict]) -> dict:
        """Push items to Trakt, logging the request and anything Trakt could not match."""
        if not any(lists.values()):
            return {}
        self._log(f"  {message}...")
        response = await send(**lists)
        not_found = [i for items in (response.get("not_found") or {}).values() if isinstance(items, list)
                     for i in items]
        if not_found:
            self._log(f"  [yellow]Trakt could not match {len(not_found)} item(s)[/]")
            if self._verbose:
                for item in not_found:
                    self._log(f"    [dim]→ Not found: {_safe(item.get('title') or item.get('ids'))}[/]")
        return response

    async def _write_plex(self, message: str, write: Callable[[list], list], items: list, result: SyncResult) -> int:
        """Write a Plex batch off the event loop; returns successes, recording failures."""
        if not items:
            return 0
        self._log(f"  {message}...")
        failed = await asyncio.to_thread(write, items)
        if failed:
            names = ", ".join(_safe(getattr(entry[0], "title", "?")) for entry in failed[:5])
            self._log(f"  [yellow]{len(failed)} Plex update(s) failed: {names}[/]")
            result.errors.append(f"{len(failed)} Plex updates failed ({message.lower()})")
        return len(items) - len(failed)

    async def _sync_movies(self, result: SyncResult, dry_run: bool) -> bool:
        """Sync movies. Returns False if cancelled."""
        phase_start = time.time()
        self._log("\n[cyan]Phase 1:[/] Syncing movies...")
        self._progress(1, 4, 5, "Trakt watched movies")
        trakt_watched_movies = await self._cache.get(self.trakt.get_watched_movies)
        self._progress(1, 4, 15, "Trakt movie ratings")
        trakt_movie_ratings = await self._cache.get(self.trakt.get_movie_ratings)
        self._progress(1, 4, 25, "Plex movies")
        plex_movies, movie_libs = await self._fetch_plex(
            self.plex.get_all_movies_with_counts, self._get_movie_libraries()
        )

        self._log(f"  Trakt: {len(trakt_watched_movies)} watched, {len(trakt_movie_ratings)} ratings")
        for lib_name, count in movie_libs.items():
            self._log(f"  Plex {_safe(f'[{lib_name}]')}: {count} movies")

        watched_keys = {key for item in trakt_watched_movies if item.movie
                        for key in _id_keys(item.movie.get("ids", {}), MOVIE_ID_TYPES)}
        rating_by_key = {key: item.rating for item in trakt_movie_ratings if item.movie
                         for key in _id_keys(item.movie.get("ids", {}), MOVIE_ID_TYPES)}

        options = self._options()
        priority = self.config.sync.rating_priority
        pushed = self._pushed
        to_trakt_watched: list[dict] = []
        to_trakt_rated: list[dict] = []
        to_plex_watched: list[Any] = []
        to_plex_rated: list[tuple[Any, int]] = []
        queued_watched: set[tuple] = set()
        queued_rated: set[tuple] = set()
        no_ids = 0

        total_movies = len(plex_movies)
        self._log(f"  Processing {total_movies} movies...")
        update_interval = max(1, total_movies // 100)
        yield_interval = max(1, min(500, total_movies // 200))

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TaskProgressColumn(), console=console, transient=False
        ) as progress:
            task = progress.add_task(f"Movies 0/{total_movies}", total=total_movies)
            for i, plex_movie in enumerate(plex_movies):
                if i % yield_interval == 0:
                    await asyncio.sleep(0)
                    if self._is_cancelled():
                        self._log("  [yellow]Cancelled[/]")
                        return False
                if i % update_interval == 0:
                    self._progress(1, 4, 30 + (i / total_movies) * 40, f"Movies {i}/{total_movies}")
                    progress.update(task, completed=i, description=f"Movies {i}/{total_movies}")

                plex_ids = extract_plex_ids(plex_movie)
                keys = _id_keys(plex_ids, MOVIE_ID_TYPES)
                if not keys:
                    no_ids += 1
                    continue
                movie_key = keys[0]
                trakt_watched = any(key in watched_keys for key in keys)
                trakt_rating = next((rating_by_key[key] for key in keys if key in rating_by_key), None)

                if plex_movie.isWatched and not trakt_watched:
                    if (options["watched_plex_to_trakt"] and movie_key not in pushed.watched_movies
                            and movie_key not in queued_watched):
                        movie = _trakt_item(plex_movie, plex_ids, MOVIE_ID_TYPES) or {}
                        watched_at = _plex_dt_to_utc_iso(plex_movie.lastViewedAt)
                        if watched_at:
                            movie["watched_at"] = watched_at
                        to_trakt_watched.append(movie)
                        queued_watched.add(movie_key)
                elif trakt_watched and not plex_movie.isWatched and options["watched_trakt_to_plex"]:
                    to_plex_watched.append(plex_movie)

                plex_rating = int(plex_movie.userRating) if plex_movie.userRating else None
                send, fetch = _rating_direction(
                    plex_rating, trakt_rating, priority,
                    options["ratings_plex_to_trakt"], options["ratings_trakt_to_plex"],
                )
                if send and movie_key not in pushed.rated_movies and movie_key not in queued_rated:
                    movie = _trakt_item(plex_movie, plex_ids, MOVIE_ID_TYPES) or {}
                    movie["rating"] = plex_rating
                    rated_at = _plex_dt_to_utc_iso(plex_movie.lastRatedAt)
                    if rated_at:
                        movie["rated_at"] = rated_at
                    to_trakt_rated.append(movie)
                    queued_rated.add(movie_key)
                elif fetch and trakt_rating is not None:
                    to_plex_rated.append((plex_movie, trakt_rating))
            progress.update(task, completed=total_movies, description=f"Movies {total_movies}/{total_movies}")

        del plex_movies
        gc.collect()

        if no_ids:
            self._log(f"  [yellow]Skipped {no_ids} movies without IMDb or TMDB IDs[/]")
        self._log(f"  Movies - To mark watched on Trakt: {len(to_trakt_watched)}")
        self._log(f"  Movies - To mark watched on Plex: {len(to_plex_watched)}")
        self._log(f"  Movies - To rate on Trakt: {len(to_trakt_rated)}")
        self._log(f"  Movies - To rate on Plex: {len(to_plex_rated)}")

        if self._verbose:
            for m in to_trakt_watched:
                self._log(f"    [dim]→ Trakt watched: {_safe(m.get('title'))} ({m.get('year')})[/]")
            for m in to_plex_watched:
                self._log(f"    [dim]→ Plex watched: {_safe(m.title)} ({m.year})[/]")
            for m in to_trakt_rated:
                self._log(f"    [dim]→ Trakt rating: {_safe(m.get('title'))} ({m.get('year')}) = {m.get('rating')}[/]")
            for m, rating in to_plex_rated:
                self._log(f"    [dim]→ Plex rating: {_safe(m.title)} ({m.year}) = {rating}[/]")

        if not dry_run:
            self._progress(1, 4, 75, "Applying movie changes")
            response = await self._push(
                f"Adding {len(to_trakt_watched)} movies to Trakt history",
                self.trakt.add_to_history, movies=to_trakt_watched,
            )
            result.added_to_trakt += response.get("added", {}).get("movies", 0)
        pushed.watched_movies |= queued_watched
        if not dry_run:
            response = await self._push(
                f"Adding {len(to_trakt_rated)} movie ratings to Trakt", self.trakt.add_ratings, movies=to_trakt_rated,
            )
            result.ratings_synced += response.get("added", {}).get("movies", 0)
        pushed.rated_movies |= queued_rated
        if not dry_run:
            result.added_to_plex += await self._write_plex(
                f"Marking {len(to_plex_watched)} movies watched on Plex",
                self.plex.mark_watched_batch, to_plex_watched, result,
            )
            result.ratings_synced += await self._write_plex(
                f"Rating {len(to_plex_rated)} movies on Plex", self.plex.rate_batch, to_plex_rated, result,
            )

        self._progress(1, 4, 100, "Movies complete")
        self._log(f"  [dim]Phase 1 completed in {time.time() - phase_start:.1f}s[/]")
        return True

    async def _sync_episodes(self, result: SyncResult, dry_run: bool) -> bool:
        """Sync episodes. Returns False if cancelled."""
        phase_start = time.time()
        self._log("\n[cyan]Phase 2:[/] Syncing episodes...")
        self._progress(2, 4, 5, "Trakt watched shows")
        trakt_watched_shows = await self._cache.get(self.trakt.get_watched_shows)
        self._progress(2, 4, 15, "Trakt episode ratings")
        trakt_episode_ratings = await self._cache.get(self.trakt.get_episode_ratings)
        self._progress(2, 4, 25, "Plex shows")
        plex_shows, show_libs = await self._fetch_plex(
            self.plex.get_all_shows_with_counts, self._get_show_libraries()
        )
        self._progress(2, 4, 30, "Plex episodes")
        plex_episodes, episode_libs = await self._fetch_plex(
            self.plex.get_all_episodes_with_counts, self._get_show_libraries(), warn=False
        )

        self._log(
            f"  Trakt: {len(trakt_watched_shows)} watched shows, {len(trakt_episode_ratings)} episode ratings"
        )
        for lib_name, count in show_libs.items():
            self._log(f"  Plex {_safe(f'[{lib_name}]')}: {count} shows, {episode_libs.get(lib_name, 0)} episodes")

        plex_show_ids_by_key = {str(show.ratingKey): extract_plex_ids(show) for show in plex_shows}
        del plex_shows

        watched_index = _episode_index(trakt_watched_shows)
        # shows without episodes mean a changed response shape; syncing on it would unwatch all
        if trakt_watched_shows and not watched_index:
            self._log(
                f"ERROR:Trakt returned {len(trakt_watched_shows)} watched shows but no watched episodes. "
                "Skipping episode sync rather than acting on empty data."
            )
            return True
        ratings_index = _episode_ratings(trakt_episode_ratings)

        total_episodes = len(plex_episodes)
        self._log(f"  Processing {total_episodes} episodes...")
        cancel_event = threading.Event()
        progress_state = {"processed": 0}

        def on_progress(processed: int, total: int) -> None:
            """Called from the worker thread; only touches plain dict entries."""
            progress_state["processed"] = processed

        process_task = asyncio.create_task(asyncio.to_thread(
            _process_episodes_in_thread,
            plex_episodes, plex_show_ids_by_key, watched_index, ratings_index, self._options(),
            self.config.sync.rating_priority, cancel_event, self._pushed, on_progress,
        ))

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TaskProgressColumn(), console=console, transient=False
        ) as progress:
            task = progress.add_task(f"Episodes 0/{total_episodes}", total=total_episodes)
            while not process_task.done():
                if self._is_cancelled() and not cancel_event.is_set():
                    cancel_event.set()
                    self._log("  [yellow]Cancelling...[/]")
                processed = progress_state["processed"]
                self._progress(2, 4, 35 + (processed / max(1, total_episodes)) * 50,
                               f"Episodes {processed}/{total_episodes}")
                progress.update(task, completed=processed, description=f"Episodes {processed}/{total_episodes}")
                await asyncio.sleep(0.1)
            progress.update(task, completed=total_episodes, description=f"Episodes {total_episodes}/{total_episodes}")

        ep_result = await process_task
        del plex_episodes, plex_show_ids_by_key, watched_index, ratings_index
        gc.collect()
        if ep_result.cancelled or self._is_cancelled():
            self._log("  [yellow]Cancelled[/]")
            return False

        if ep_result.skipped_no_ids:
            self._log(f"  [yellow]Skipped {ep_result.skipped_no_ids} episodes without show IDs[/]")
        if ep_result.skipped_no_number:
            self._log(f"  [yellow]Skipped {ep_result.skipped_no_number} episodes without season or episode number[/]")
        self._log(f"  Episodes - To mark watched on Trakt: {len(ep_result.watched_to_trakt)}")
        self._log(f"  Episodes - To mark watched on Plex: {len(ep_result.watched_to_plex)}")
        self._log(f"  Episodes - To rate on Trakt: {len(ep_result.rated_to_trakt)}")
        self._log(f"  Episodes - To rate on Plex: {len(ep_result.rated_to_plex)}")

        if self._verbose:
            for *_, label in ep_result.watched_to_trakt:
                self._log(f"    [dim]→ Trakt watched: {_safe(label)}[/]")
            for _, label in ep_result.watched_to_plex:
                self._log(f"    [dim]→ Plex watched: {_safe(label)}[/]")
            for *_, label in ep_result.rated_to_trakt:
                self._log(f"    [dim]→ Trakt rating: {_safe(label)}[/]")
            for *_, label in ep_result.rated_to_plex:
                self._log(f"    [dim]→ Plex rating: {_safe(label)}[/]")

        pushed = self._pushed
        if not dry_run:
            self._progress(2, 4, 90, "Applying episode changes")
            response = await self._push(
                f"Adding {len(ep_result.watched_to_trakt)} episodes to Trakt history",
                self.trakt.add_to_history, shows=_group_by_show(ep_result.watched_to_trakt),
            )
            result.added_to_trakt += response.get("added", {}).get("episodes", 0)
        pushed.watched_episodes |= ep_result.watched_keys
        if not dry_run:
            response = await self._push(
                f"Adding {len(ep_result.rated_to_trakt)} episode ratings to Trakt",
                self.trakt.add_ratings, shows=_group_by_show(ep_result.rated_to_trakt),
            )
            result.ratings_synced += response.get("added", {}).get("episodes", 0)
        pushed.rated_episodes |= ep_result.rated_keys
        if not dry_run:
            result.added_to_plex += await self._write_plex(
                f"Marking {len(ep_result.watched_to_plex)} episodes watched on Plex",
                self.plex.mark_watched_batch, [ep for ep, _ in ep_result.watched_to_plex], result,
            )
            result.ratings_synced += await self._write_plex(
                f"Rating {len(ep_result.rated_to_plex)} episodes on Plex",
                self.plex.rate_batch, [(ep, rating) for ep, rating, _ in ep_result.rated_to_plex], result,
            )

        self._progress(2, 4, 100, "Episodes complete")
        self._log(f"  [dim]Phase 2 completed in {time.time() - phase_start:.1f}s[/]")
        return True

    async def _sync_collection(self, result: SyncResult, dry_run: bool,
                               no_movies: bool = False, no_shows: bool = False) -> bool:
        """Sync Plex library to Trakt collection. Returns False if cancelled."""
        if not self._get_sync_option("collection_plex_to_trakt"):
            self._log("\n[dim]Phase 3: Skipped (collection sync disabled)[/]")
            return True

        phase_start = time.time()
        self._log("\n[cyan]Phase 3:[/] Syncing collection...")
        self._progress(3, 4, 0, "Fetching collection data")

        limits = await self._cache.get(self.trakt.get_account_limits)
        if not limits.is_vip:
            self._log(f"  [yellow]Note: Free Trakt account (limit: {limits.collection_limit} items)[/]")

        self._progress(3, 4, 5, "Trakt collection")
        trakt_collection_movies = await self._cache.get(self.trakt.get_collection_movies)
        trakt_collection_shows = await self._cache.get(self.trakt.get_collection_shows)
        plex_movies: list = []
        plex_shows: list = []
        plex_episodes: list = []
        if not no_movies:
            self._progress(3, 4, 15, "Plex movies")
            plex_movies, _ = await self._fetch_plex(
                self.plex.get_all_movies_with_counts, self._get_movie_libraries(), warn=False
            )
        if not no_shows:
            self._progress(3, 4, 18, "Plex shows")
            plex_shows, _ = await self._fetch_plex(
                self.plex.get_all_shows_with_counts, self._get_show_libraries(), warn=False
            )
            self._progress(3, 4, 20, "Plex episodes")
            plex_episodes, _ = await self._fetch_plex(
                self.plex.get_all_episodes_with_counts, self._get_show_libraries(), warn=False
            )

        current_count = len(trakt_collection_movies) + len(trakt_collection_shows)
        self._log(f"  Trakt collection: {len(trakt_collection_movies)} movies, {len(trakt_collection_shows)} shows")
        self._log(f"  Plex library: {len(plex_movies)} movies, {len(plex_shows)} shows, {len(plex_episodes)} episodes")

        if not limits.is_vip and current_count >= limits.collection_limit:
            self._log(f"  [yellow]WARNING: Collection at limit ({current_count}/{limits.collection_limit})[/]")
            self._log("  [yellow]Upgrade to Trakt VIP for unlimited collection: https://trakt.tv/vip[/]")
            self._log("  [yellow]Skipping collection sync[/]")
            return True

        collected_movies = {key for item in trakt_collection_movies
                            for key in _id_keys(item.get("movie", {}).get("ids", {}), MOVIE_ID_TYPES)}
        collected_shows: set[tuple] = set()
        collected_episodes: set[tuple] = set()
        for item in trakt_collection_shows:
            keys = _id_keys(item.get("show", {}).get("ids", {}), SHOW_ID_TYPES)
            collected_shows.update(keys)
            for season in item.get("seasons", []):
                for ep in season.get("episodes", []):
                    collected_episodes.update((key, season.get("number", 0), ep.get("number", 0)) for key in keys)

        fixing = self._fix_collection_dates
        best: dict[tuple, tuple[int, dict]] = {}
        total_movies = len(plex_movies)
        self._log(f"  Processing {total_movies} movies...")
        update_interval = max(1, total_movies // 100)
        yield_interval = max(1, min(500, total_movies // 200))
        for i, plex_movie in enumerate(plex_movies):
            if i % yield_interval == 0:
                await asyncio.sleep(0)
                if self._is_cancelled():
                    self._log("  [yellow]Cancelled[/]")
                    return False
            if i % update_interval == 0:
                self._progress(3, 4, 25 + (i / total_movies) * 20, f"Movies {i}/{total_movies}")

            plex_ids = extract_plex_ids(plex_movie)
            keys = _id_keys(plex_ids, MOVIE_ID_TYPES)
            if not keys or (not fixing and any(key in collected_movies for key in keys)):
                continue
            # the same movie in several libraries is collected once, from its best copy
            score = media_score(plex_movie)
            if keys[0] in best and best[keys[0]][0] >= score:
                continue
            movie = _trakt_item(plex_movie, plex_ids, MOVIE_ID_TYPES) or {}
            movie.update(extract_media_metadata(plex_movie))
            collected_at = _plex_dt_to_utc_iso(getattr(plex_movie, "addedAt", None))
            if collected_at:
                movie["collected_at"] = collected_at
            best[keys[0]] = (score, movie)
        movies_to_collect = [movie for _, movie in best.values()]
        self._plex_cache.clear()
        del plex_movies, best

        show_info = {str(show.ratingKey): (extract_plex_ids(show), show.title, show.year) for show in plex_shows}
        rows: list[tuple[dict, int, dict, str]] = []
        new_shows: set[tuple] = set()
        existing_shows: set[tuple] = set()
        seen: set[tuple] = set()
        skipped_no_number = 0
        total_episodes = len(plex_episodes)
        self._log(f"  Processing {total_episodes} episodes...")
        update_interval = max(1, total_episodes // 100)
        yield_interval = max(1, min(500, total_episodes // 200))
        for i, episode in enumerate(plex_episodes):
            if i % yield_interval == 0:
                await asyncio.sleep(0)
                if self._is_cancelled():
                    self._log("  [yellow]Cancelled[/]")
                    return False
            if i % update_interval == 0:
                self._progress(3, 4, 45 + (i / total_episodes) * 30, f"Episodes {i}/{total_episodes}")

            info = show_info.get(str(episode.grandparentRatingKey))
            keys = _id_keys(info[0], SHOW_ID_TYPES) if info else []
            if not info or not keys:
                continue
            season, number = episode.parentIndex, episode.index
            if season is None or number is None:
                skipped_no_number += 1
                continue
            ep_key = (keys[0], season, number)
            if ep_key in seen or (not fixing and any((k, season, number) in collected_episodes for k in keys)):
                continue
            seen.add(ep_key)
            (existing_shows if any(k in collected_shows for k in keys) else new_shows).add(keys[0])
            entry: dict[str, Any] = {"number": number}
            collected_at = _plex_dt_to_utc_iso(getattr(episode, "addedAt", None))
            if collected_at:
                entry["collected_at"] = collected_at
            _, title, year = info
            rows.append(({"title": title, "year": year, "ids": dict(keys)}, season, entry, ""))
        del plex_episodes, plex_shows, show_info
        gc.collect()

        shows_to_collect = _group_by_show(rows)
        if skipped_no_number:
            self._log(f"  [yellow]Skipped {skipped_no_number} episodes without season or episode number[/]")
        verb = "to sync" if fixing else "to add"
        self._log(f"  Collection - Movies {verb}: {len(movies_to_collect)}")
        self._log(f"  Collection - New shows {verb}: {len(new_shows)}")
        self._log(f"  Collection - Existing shows with episodes {verb}: {len(existing_shows)}")
        self._log(f"  Collection - Total episodes {verb}: {len(rows)}")

        if self._verbose:
            for m in movies_to_collect:
                date = f" [{m['collected_at'][:10]}]" if m.get("collected_at") else ""
                self._log(f"    [dim]→ Collection: {_safe(m.get('title'))} ({m.get('year')}){_safe(date)}[/]")
            for show in shows_to_collect:
                codes = [f"S{s['number']:02d}E{e['number']:02d}" for s in show["seasons"] for e in s["episodes"]]
                listed = ", ".join(codes[:5]) + (f" (+{len(codes) - 5} more)" if len(codes) > 5 else "")
                self._log(f"    [dim]→ Collection: {_safe(show['title'])} - {listed}[/]")

        if not dry_run:
            self._progress(3, 4, 80, "Adding to Trakt collection")
            try:
                response = await self._push(
                    f"Sending {len(movies_to_collect)} movies to Trakt collection",
                    self.trakt.add_to_collection, movies=movies_to_collect,
                )
                result.collection_added += response.get("added", {}).get("movies", 0)
                result.collection_updated += response.get("updated", {}).get("movies", 0)
                response = await self._push(
                    f"Sending {len(shows_to_collect)} shows ({len(rows)} episodes) to Trakt collection",
                    self.trakt.add_to_collection, shows=shows_to_collect,
                )
                result.collection_added += response.get("added", {}).get("episodes", 0)
                result.collection_updated += response.get("updated", {}).get("episodes", 0)
            except TraktAccountLimitError as e:
                self._log(f"  [red]ERROR: {_safe(e)}[/]")
                if not e.is_vip:
                    self._log(f"  [yellow]Upgrade to Trakt VIP for unlimited collection: {_safe(e.upgrade_url)}[/]")
                result.errors.append(f"Collection limit exceeded: {e}")

        self._progress(3, 4, 100, "Collection complete")
        self._log(f"  [dim]Phase 3 completed in {time.time() - phase_start:.1f}s[/]")
        return True

    async def _sync_watchlist(self, result: SyncResult, dry_run: bool) -> bool:
        """Sync watchlists between Plex and Trakt. Returns False if cancelled."""
        options = self._options()
        if not (options["watchlist_plex_to_trakt"] or options["watchlist_trakt_to_plex"]):
            self._log("\n[dim]Phase 4: Skipped (watchlist sync disabled)[/]")
            return True
        if self.plex.account_token in self._pushed.watchlist_accounts:
            self._log("\n[dim]Phase 4: Skipped (watchlist already synced for this Plex account)[/]")
            return True

        phase_start = time.time()
        self._log("\n[cyan]Phase 4:[/] Syncing watchlist...")
        self._progress(4, 4, 0, "Fetching watchlist data")

        limits = await self._cache.get(self.trakt.get_account_limits)
        if not limits.is_vip and options["watchlist_plex_to_trakt"]:
            self._log(f"  [yellow]Note: Free Trakt account (limit: {limits.watchlist_limit} items)[/]")

        self._progress(4, 4, 15, "Trakt watchlist")
        trakt_movies = await self._cache.get(self.trakt.get_watchlist_movies)
        trakt_shows = await self._cache.get(self.trakt.get_watchlist_shows)
        trakt_count = len(trakt_movies) + len(trakt_shows)

        if not limits.is_vip and trakt_count >= limits.watchlist_limit and options["watchlist_plex_to_trakt"]:
            self._log(f"  [yellow]WARNING: Trakt watchlist at limit ({trakt_count}/{limits.watchlist_limit})[/]")
            self._log("  [yellow]Upgrade to Trakt VIP for unlimited watchlist: https://trakt.tv/vip[/]")
            self._log("  [yellow]Skipping Plex → Trakt watchlist sync[/]")
            options["watchlist_plex_to_trakt"] = False

        self._progress(4, 4, 30, "Comparing watchlists")
        plan = await asyncio.to_thread(
            _plan_watchlist, self.plex, trakt_movies, trakt_shows, self._pushed.watched_movies,
            options, self._is_cancelled, self._log,
        )
        if plan is None:
            self._log("  [yellow]Cancelled[/]")
            return False
        movies_to_trakt, shows_to_trakt, items_to_plex, plex_count = plan
        self._pushed.watchlist_accounts.add(self.plex.account_token)

        self._log(f"  Plex watchlist: {plex_count} items")
        self._log(f"  Trakt watchlist: {len(trakt_movies)} movies, {len(trakt_shows)} shows")
        self._log(f"  Watchlist - To add to Trakt: {len(movies_to_trakt)} movies, {len(shows_to_trakt)} shows")
        self._log(f"  Watchlist - To add to Plex: {len(items_to_plex)} items")

        if self._verbose:
            for item in movies_to_trakt + shows_to_trakt:
                self._log(f"    [dim]→ Trakt watchlist: {_safe(item.get('title'))} ({item.get('year')})[/]")
            for item in items_to_plex:
                title, year = getattr(item, "title", "?"), getattr(item, "year", "?")
                self._log(f"    [dim]→ Plex watchlist: {_safe(title)} ({year})[/]")

        if not dry_run:
            self._progress(4, 4, 80, "Applying watchlist changes")
            try:
                response = await self._push(
                    f"Adding {len(movies_to_trakt)} movies and {len(shows_to_trakt)} shows to Trakt watchlist",
                    self.trakt.add_to_watchlist, movies=movies_to_trakt, shows=shows_to_trakt,
                )
                added = response.get("added", {})
                result.watchlist_added_trakt += added.get("movies", 0) + added.get("shows", 0)
            except TraktAccountLimitError as e:
                self._log(f"  [red]ERROR: {_safe(e)}[/]")
                if not e.is_vip:
                    self._log(f"  [yellow]Upgrade to Trakt VIP for unlimited watchlist: {_safe(e.upgrade_url)}[/]")
                result.errors.append(f"Watchlist limit exceeded: {e}")
            result.watchlist_added_plex += await self._write_plex(
                f"Adding {len(items_to_plex)} items to Plex watchlist",
                self.plex.add_to_watchlist_batch, items_to_plex, result,
            )

        self._progress(4, 4, 100, "Watchlist complete")
        self._log(f"  [dim]Phase 4 completed in {time.time() - phase_start:.1f}s[/]")
        return True

    async def _run_phase(self, phase: Awaitable[bool], result: SyncResult) -> bool:
        """Run one phase, recording an error and carrying on to the next; False only on cancel."""
        try:
            return await phase
        except (TraktAuthError, PlexAuthError):
            raise
        except Exception as e:
            get_file_logger().exception("Sync phase failed")
            self._log(f"  [red]Phase failed: {_safe(type(e).__name__)}: {_safe(e)}[/]")
            result.errors.append(f"{type(e).__name__}: {e}")
            return True

    async def sync(
        self,
        dry_run: bool = False,
        fix_collection_dates: bool = False,
        collection_only: bool = False,
        no_movies: bool = False,
        no_shows: bool = False,
    ) -> SyncResult | None:
        """Sync this server through every enabled phase; None if cancelled."""
        self._fix_collection_dates = fix_collection_dates
        # phase 3 reads the same Plex lists as phases 1 and 2
        self._keep_plex = not collection_only and self._get_sync_option("collection_plex_to_trakt")
        start_time = time.time()
        result = SyncResult()

        logger = get_file_logger()
        logger.info("=" * 60)
        logger.info(f"SYNC STARTED - dry_run={dry_run}")
        logger.info("=" * 60)

        self._log("[bold]Starting Pakt sync...[/]")
        self._log(f"  Mode: {'Dry run' if dry_run else 'Live sync'}")
        if fix_collection_dates:
            self._log("  [yellow]Fix collection dates: re-sending all items with Plex addedAt dates[/]")
        if collection_only:
            self._log("  [cyan]Collection only: skipping watched, ratings, watchlist[/]")
        if no_movies:
            self._log("  [cyan]Skipping movies[/]")
        if no_shows:
            self._log("  [cyan]Skipping shows/episodes[/]")

        if collection_only:
            self._log("\n[dim]Phase 1: Skipped (--collection-only)[/]")
            self._log("\n[dim]Phase 2: Skipped (--collection-only)[/]")
        else:
            if no_movies:
                self._log("\n[dim]Phase 1: Skipped (--no-movies)[/]")
            elif not await self._run_phase(self._sync_movies(result, dry_run), result):
                return None
            if no_shows:
                self._log("\n[dim]Phase 2: Skipped (--no-shows)[/]")
            elif not await self._run_phase(self._sync_episodes(result, dry_run), result):
                return None

        collection = self._sync_collection(result, dry_run, no_movies=no_movies, no_shows=no_shows)
        if not await self._run_phase(collection, result):
            return None

        if collection_only:
            self._log("\n[dim]Phase 4: Skipped (--collection-only)[/]")
        elif not await self._run_phase(self._sync_watchlist(result, dry_run), result):
            return None

        if dry_run:
            self._log("\n[yellow]Dry run complete - no changes applied[/]")
        elif result.errors:
            self._log(f"\n[yellow]Sync finished with {len(result.errors)} error(s)[/]")
        else:
            self._log("\n[green]Sync complete![/]")

        result.duration_seconds = time.time() - start_time
        return result


async def run_multi_server_sync(
    config: Config,
    server_names: list[str] | None = None,
    dry_run: bool = False,
    verbose: bool = False,
    fix_collection_dates: bool = False,
    collection_only: bool = False,
    no_movies: bool = False,
    no_shows: bool = False,
    on_token_refresh: Callable[[dict], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> SyncResult:
    """Run sync across multiple Plex servers, refusing to start while another process is syncing."""
    def log(msg: str) -> None:
        """Log a markup line to the console, sync.log and the caller's sink."""
        _emit(msg, log_callback)

    if server_names:
        servers_to_sync = []
        for name in server_names:
            server = config.get_server(name)
            if server:
                servers_to_sync.append(server)
            else:
                log(f"WARNING:Server '{_safe(name)}' not found in configuration")
    else:
        servers_to_sync = config.get_enabled_servers()

    total_result = SyncResult()
    if not servers_to_sync:
        log("ERROR:No servers configured or enabled for sync")
        total_result.errors.append("No servers configured or enabled for sync")
        return total_result

    with _sync_lock() as acquired:
        if not acquired:
            log("ERROR:Another Pakt sync is already running")
            total_result.errors.append("Another sync is already running")
            return total_result

        start_time = time.time()
        server_count = len(servers_to_sync)
        pushed = PushedToTrakt()
        cache = TraktCache()
        synced = 0
        log(f"[bold]Starting sync across {server_count} server(s)...[/]")

        async with TraktClient(config.trakt, on_token_refresh=on_token_refresh) as trakt:
            for idx, server_config in enumerate(servers_to_sync, 1):
                if cancel_check and cancel_check():
                    log("WARNING:Sync cancelled")
                    break
                log(f"\n[bold cyan]═══ Server {idx}/{server_count}: {_safe(server_config.name)} ═══[/]")
                try:
                    plex = PlexClient(server_config, account_token=config.plex_token)
                    await asyncio.to_thread(plex.connect)
                    log(f"Connected to: {_safe(plex.server.friendlyName)}")
                    engine = SyncEngine(
                        config, trakt, plex,
                        log_callback=log_callback,
                        cancel_check=cancel_check,
                        verbose=verbose,
                        server_name=server_config.name,
                        server_config=server_config,
                        trakt_cache=cache,
                        pushed=pushed,
                    )
                    result = await engine.sync(
                        dry_run=dry_run,
                        fix_collection_dates=fix_collection_dates,
                        collection_only=collection_only,
                        no_movies=no_movies,
                        no_shows=no_shows,
                    )
                except TraktAuthError as e:
                    log(f"ERROR:{_safe(e)}")
                    total_result.errors.append(str(e))
                    break
                except Exception as e:
                    get_file_logger().exception("Server %s failed", server_config.name)
                    error_msg = f"[{server_config.name}] Error: {type(e).__name__}: {e}"
                    log(f"ERROR:{_safe(error_msg)}")
                    total_result.errors.append(error_msg)
                    continue

                if result:
                    synced += 1
                    total_result.added_to_trakt += result.added_to_trakt
                    total_result.added_to_plex += result.added_to_plex
                    total_result.ratings_synced += result.ratings_synced
                    total_result.collection_added += result.collection_added
                    total_result.collection_updated += result.collection_updated
                    total_result.watchlist_added_trakt += result.watchlist_added_trakt
                    total_result.watchlist_added_plex += result.watchlist_added_plex
                    total_result.errors.extend(result.errors)

        total_result.duration_seconds = time.time() - start_time
        if synced and not dry_run:
            set_last_sync(datetime.now(timezone.utc))

    if server_count > 1:
        log("\n[bold green]═══ Multi-Server Sync Complete ═══[/]")
        log(f"  Servers synced: {synced}/{server_count}")
        log(f"  Total added to Trakt: {total_result.added_to_trakt}")
        log(f"  Total added to Plex: {total_result.added_to_plex}")
        log(f"  Total ratings synced: {total_result.ratings_synced}")
        log(f"  Duration: {total_result.duration_seconds:.1f}s")

    return total_result
