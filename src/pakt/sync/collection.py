"""Collection phase: the Plex library sent to the Trakt collection, from each item's best copy."""

from __future__ import annotations

import gc
import logging
import time
from typing import TYPE_CHECKING, Any

from pakt.models import SyncResult
from pakt.plex.media import best_media, extract_media_metadata, extract_plex_ids
from pakt.sync import report
from pakt.sync.match import MOVIE_ID_TYPES, SHOW_ID_TYPES, group_by_show, id_keys, plex_dt_to_utc_iso, trakt_item
from pakt.trakt import TraktAccountLimitError

if TYPE_CHECKING:
    from pakt.sync.engine import SyncEngine

logger = logging.getLogger(__name__)


def _collected_keys(movies: list[dict], shows: list[dict]) -> tuple[set[tuple], set[tuple]]:
    """Keys of the movies and the episodes already in the Trakt collection."""
    movie_keys = {key for item in movies for key in id_keys(item.get("movie", {}).get("ids", {}), MOVIE_ID_TYPES)}
    episode_keys: set[tuple] = set()
    for item in shows:
        keys = id_keys(item.get("show", {}).get("ids", {}), SHOW_ID_TYPES)
        for season in item.get("seasons", []):
            for ep in season.get("episodes", []):
                episode_keys.update((key, season.get("number", 0), ep.get("number", 0)) for key in keys)
    return movie_keys, episode_keys


async def _movies_to_collect(engine: SyncEngine, plex_movies: list, collected: set[tuple]) -> list[dict]:
    """Movies to collect, each title once from its best copy; stops on a cancel."""
    best: dict[tuple, tuple[int, dict]] = {}
    slots: dict[tuple, tuple] = {}
    collected = set(collected)
    async for plex_movie in engine.paced(plex_movies, 3, 25, 20, "Movies"):
        plex_ids = extract_plex_ids(plex_movie)
        keys = id_keys(plex_ids, MOVIE_ID_TYPES)
        if not keys:
            continue
        if any(key in collected for key in keys):
            # its other copies may carry only ids Trakt did not list
            collected.update(keys)
            continue
        slot = next((slots[key] for key in keys if key in slots), keys[0])
        for key in keys:
            slots.setdefault(key, slot)
        score, media = best_media(plex_movie)
        if slot in best and best[slot][0] >= score:
            continue
        movie = trakt_item(plex_movie, plex_ids, MOVIE_ID_TYPES) or {}
        movie.update(extract_media_metadata(media))
        collected_at = plex_dt_to_utc_iso(getattr(plex_movie, "addedAt", None))
        if collected_at:
            movie["collected_at"] = collected_at
        best[slot] = (score, movie)
    return [movie for _, movie in best.values()]


async def _episodes_to_collect(
    engine: SyncEngine, plex_shows: list, plex_episodes: list, collected: set[tuple],
) -> tuple[list[tuple[dict, int, dict, str]], int, int]:
    """Episode rows to collect, plus show and unnumbered counts; stops on a cancel."""
    show_info = {str(show.ratingKey): (extract_plex_ids(show), show.title, show.year) for show in plex_shows}
    rows: list[tuple[dict, int, dict, str]] = []
    shows_seen: set[tuple] = set()
    seen = set(collected)
    unnumbered = 0
    async for episode in engine.paced(plex_episodes, 3, 45, 30, "Episodes"):
        info = show_info.get(str(episode.grandparentRatingKey))
        keys = id_keys(info[0], SHOW_ID_TYPES) if info else []
        if not info or not keys:
            continue
        season, number = episode.parentIndex, episode.index
        if season is None or number is None:
            unnumbered += 1
            continue
        ep_keys = [(key, season, number) for key in keys]
        known = any(k in seen for k in ep_keys)
        # its other copies may carry only ids Trakt did not list
        seen.update(ep_keys)
        if known:
            continue
        shows_seen.add(keys[0])
        entry: dict[str, Any] = {"number": number}
        collected_at = plex_dt_to_utc_iso(getattr(episode, "addedAt", None))
        if collected_at:
            entry["collected_at"] = collected_at
        _, title, year = info
        rows.append(({"title": title, "year": year, "ids": dict(keys)}, season, entry, ""))
    return rows, len(shows_seen), unnumbered


def _describe(movies: list[dict], episodes: int, shows: int, fixing: bool) -> str:
    """Describe what the phase sends, such as 'Trakt +2 movies, +5 episodes of 1 show'."""
    sending = [report.plural(len(movies), "movie")] if movies else []
    if episodes:
        sending.append(f"{report.plural(episodes, 'episode')} of {report.plural(shows, 'show')}")
    if not sending:
        return "no changes"
    if fixing:
        return f"re-sending {' and '.join(sending)} with Plex dates"
    return f"Trakt +{', +'.join(sending)}"


def _list_items(movies: list[dict], shows: list[dict]) -> None:
    """List each movie and show being sent, with up to five episode codes per show."""
    for movie in movies:
        added = f", added {movie['collected_at'][:10]}" if movie.get("collected_at") else ""
        report.item(f"Trakt collected: {report.title(movie)}{added}")
    for show in shows:
        codes = [f"S{s['number']:02d}E{e['number']:02d}" for s in show["seasons"] for e in s["episodes"]]
        listed = ", ".join(codes[:5]) + (f" and {len(codes) - 5} more" if len(codes) > 5 else "")
        report.item(f"Trakt collected: {show['title']} {listed}")


async def run(engine: SyncEngine, result: SyncResult, dry_run: bool,
              no_movies: bool = False, no_shows: bool = False) -> bool:
    """Sync Plex library to Trakt collection. Returns False if cancelled."""
    if not engine.get_sync_option("collection_plex_to_trakt"):
        return True

    phase_start = time.time()
    engine.progress(3, 4, 0, "Fetching collection data")

    limits = await engine.cache.get(engine.trakt.get_account_limits)
    engine.progress(3, 4, 5, "Trakt collection")
    trakt_collection_movies = await engine.cache.get(engine.trakt.get_collection_movies)
    trakt_collection_shows = await engine.cache.get(engine.trakt.get_collection_shows)
    plex_movies: list = []
    plex_shows: list = []
    plex_episodes: list = []
    if not no_movies:
        engine.progress(3, 4, 15, "Plex movies")
        plex_movies, _ = await engine.fetch_plex(
            engine.plex.get_all_movies_with_counts, engine.movie_libraries_to_sync(), warn=False
        )
    if not no_shows:
        engine.progress(3, 4, 18, "Plex shows")
        plex_shows, _ = await engine.fetch_plex(
            engine.plex.get_all_shows_with_counts, engine.show_libraries_to_sync(), warn=False
        )
        engine.progress(3, 4, 20, "Plex episodes")
        plex_episodes, _ = await engine.fetch_plex(
            engine.plex.get_all_episodes_with_counts, engine.show_libraries_to_sync(), warn=False
        )

    current_count = len(trakt_collection_movies) + len(trakt_collection_shows)
    free = "" if limits.is_vip else f" (free account, limit {limits.collection_limit})"
    logger.info(
        f"Collection: Trakt {len(trakt_collection_movies)} movies, {len(trakt_collection_shows)} shows{free}; "
        f"Plex {len(plex_movies)} movies, {len(plex_episodes)} episodes"
    )

    if not limits.is_vip and current_count >= limits.collection_limit:
        logger.warning(
            f"Collection: skipped, the Trakt free-account limit of {limits.collection_limit} items is reached. "
            "Trakt VIP removes it: https://trakt.tv/vip"
        )
        return True

    fixing = engine.fix_collection_dates
    # fixing dates resends what Trakt already has
    collected_movies, collected_episodes = (
        (set(), set()) if fixing else _collected_keys(trakt_collection_movies, trakt_collection_shows)
    )
    movies_to_collect = await _movies_to_collect(engine, plex_movies, collected_movies)
    engine.plex_cache.clear()
    del plex_movies
    if engine.is_cancelled():
        return False
    rows, show_count, unnumbered = await _episodes_to_collect(engine, plex_shows, plex_episodes, collected_episodes)
    del plex_episodes, plex_shows
    gc.collect()
    if engine.is_cancelled():
        return False

    shows_to_collect = group_by_show(rows)
    if unnumbered:
        logger.warning(f"Collection: skipped {report.plural(unnumbered, 'episode')} without a season or episode number")
    planned = _describe(movies_to_collect, len(rows), show_count, fixing)
    logger.info(f"Collection: {planned} ({report.duration(time.time() - phase_start)})")
    if engine.verbose:
        _list_items(movies_to_collect, shows_to_collect)

    if not dry_run:
        engine.progress(3, 4, 80, "Adding to Trakt collection")
        try:
            response = await engine.push("collection", engine.trakt.add_to_collection, movies=movies_to_collect)
            result.collection_added += response.get("added", {}).get("movies", 0)
            result.collection_updated += response.get("updated", {}).get("movies", 0)
            response = await engine.push("collection", engine.trakt.add_to_collection, shows=shows_to_collect)
            result.collection_added += response.get("added", {}).get("episodes", 0)
            result.collection_updated += response.get("updated", {}).get("episodes", 0)
        except TraktAccountLimitError as e:
            logger.error(f"Collection: {e}")
            result.errors.append(f"Collection: {e}")

    engine.progress(3, 4, 100, "Collection complete")
    return True
