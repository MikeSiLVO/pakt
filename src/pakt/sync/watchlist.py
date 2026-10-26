"""Watchlist phase: the Plex and Trakt watchlists, matched by id and added both ways."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Callable

from pakt.models import SyncResult
from pakt.plex import PlexClient, extract_plex_ids
from pakt.sync import report
from pakt.sync.match import MOVIE_ID_TYPES, SHOW_ID_TYPES, id_keys, trakt_item
from pakt.trakt import TraktAccountLimitError

if TYPE_CHECKING:
    from pakt.sync.engine import SyncEngine

logger = logging.getLogger(__name__)


def plan(
    plex: PlexClient,
    trakt_movies: list[dict],
    trakt_shows: list[dict],
    just_watched: set[tuple],
    options: dict[str, bool],
    is_cancelled: Callable[[], bool],
) -> tuple[list[dict], list[dict], list[Any], int] | None:
    """Plan watchlist additions both ways in a worker thread; None if cancelled."""
    plex_watchlist = plex.get_watchlist()
    plex_movie_keys: set[tuple] = set()
    plex_show_keys: set[tuple] = set()
    plex_entries = []
    for item in plex_watchlist:
        plex_ids = extract_plex_ids(item)
        is_show = (getattr(item, "TYPE", None) or getattr(item, "type", None)) == "show"
        keys = id_keys(plex_ids, SHOW_ID_TYPES if is_show else MOVIE_ID_TYPES)
        (plex_show_keys if is_show else plex_movie_keys).update(keys)
        plex_entries.append((item, plex_ids, is_show, keys))

    trakt_movie_keys = {k for i in trakt_movies for k in id_keys(i.get("movie", {}).get("ids", {}), MOVIE_ID_TYPES)}
    trakt_show_keys = {k for i in trakt_shows for k in id_keys(i.get("show", {}).get("ids", {}), SHOW_ID_TYPES)}

    movies_to_trakt: list[dict] = []
    shows_to_trakt: list[dict] = []
    if options["watchlist_plex_to_trakt"]:
        for item, plex_ids, is_show, keys in plex_entries:
            on_trakt = any(k in (trakt_show_keys if is_show else trakt_movie_keys) for k in keys)
            # Trakt drops a movie from its watchlist once it is watched
            if on_trakt or (not is_show and any(k in just_watched for k in keys)):
                continue
            data = trakt_item(item, plex_ids, SHOW_ID_TYPES if is_show else MOVIE_ID_TYPES)
            if data:
                (shows_to_trakt if is_show else movies_to_trakt).append(data)

    items_to_plex: list[Any] = []
    if options["watchlist_trakt_to_plex"]:
        wanted = [("movie", i.get("movie", {}), MOVIE_ID_TYPES, plex_movie_keys) for i in trakt_movies]
        wanted += [("show", i.get("show", {}), SHOW_ID_TYPES, plex_show_keys) for i in trakt_shows]
        for libtype, trakt_entry, kinds, plex_keys in wanted:
            if is_cancelled():
                return None
            keys = id_keys(trakt_entry.get("ids", {}), kinds)
            title = trakt_entry.get("title", "")
            if not title or any(k in plex_keys for k in keys):
                continue
            try:
                for match in plex.search_discover(title, libtype=libtype)[:5]:
                    if any(k in keys for k in id_keys(extract_plex_ids(match), kinds)):
                        items_to_plex.append(match)
                        break
            except Exception as e:
                logger.warning(f"Watchlist: could not search Plex for '{title}': {e}")

    return movies_to_trakt, shows_to_trakt, items_to_plex, len(plex_watchlist)


async def run(engine: SyncEngine, result: SyncResult, dry_run: bool) -> bool:
    """Sync watchlists between Plex and Trakt. Returns False if cancelled."""
    options = engine.options()
    if not (options["watchlist_plex_to_trakt"] or options["watchlist_trakt_to_plex"]):
        return True
    if engine.plex.account_token in engine.pushed.watchlist_accounts:
        logger.info("Watchlist: already synced for this Plex account")
        return True

    phase_start = time.time()
    engine.progress(4, 4, 0, "Fetching watchlist data")

    limits = await engine.cache.get(engine.trakt.get_account_limits)
    engine.progress(4, 4, 15, "Trakt watchlist")
    trakt_movies = await engine.cache.get(engine.trakt.get_watchlist_movies)
    trakt_shows = await engine.cache.get(engine.trakt.get_watchlist_shows)
    at_limit = (not limits.is_vip and options["watchlist_plex_to_trakt"]
                and len(trakt_movies) + len(trakt_shows) >= limits.watchlist_limit)
    if at_limit:
        options["watchlist_plex_to_trakt"] = False

    engine.progress(4, 4, 30, "Comparing watchlists")
    planned = await asyncio.to_thread(
        plan, engine.plex, trakt_movies, trakt_shows, engine.pushed.watched_movies,
        options, engine.is_cancelled,
    )
    if planned is None:
        return False
    movies_to_trakt, shows_to_trakt, items_to_plex, plex_count = planned
    engine.pushed.watchlist_accounts.add(engine.plex.account_token)

    free = "" if limits.is_vip else f" (free account, limit {limits.watchlist_limit})"
    logger.info(
        f"Watchlist: Trakt {len(trakt_movies)} movies, {len(trakt_shows)} shows{free}; Plex {plex_count} items"
    )
    if at_limit:
        logger.warning(
            f"Watchlist: Plex to Trakt skipped, the Trakt free-account limit of {limits.watchlist_limit} items "
            "is reached. Trakt VIP removes it: https://trakt.tv/vip"
        )
    changes = report.changes(
        {"watchlisted": len(movies_to_trakt) + len(shows_to_trakt)}, {"watchlisted": len(items_to_plex)}
    )
    logger.info(f"Watchlist: {changes} ({report.duration(time.time() - phase_start)})")

    if engine.verbose:
        for item in movies_to_trakt + shows_to_trakt:
            report.item(f"Trakt watchlisted: {report.title(item)}")
        for item in items_to_plex:
            report.item(f"Plex watchlisted: {getattr(item, 'title', '?')} ({getattr(item, 'year', '?')})")

    if not dry_run:
        engine.progress(4, 4, 80, "Applying watchlist changes")
        try:
            response = await engine.push(
                "watchlist", engine.trakt.add_to_watchlist, movies=movies_to_trakt, shows=shows_to_trakt,
            )
            added = response.get("added", {})
            result.watchlist_added_trakt += added.get("movies", 0) + added.get("shows", 0)
        except TraktAccountLimitError as e:
            logger.error(f"Watchlist: {e}")
            result.errors.append(f"Watchlist: {e}")
        result.watchlist_added_plex += await engine.write_plex(
            "added to the watchlist", engine.plex.add_to_watchlist_batch, items_to_plex, result,
        )

    engine.progress(4, 4, 100, "Watchlist complete")
    return True
