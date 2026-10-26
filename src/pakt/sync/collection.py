"""Collection phase: the Plex library sent to the Trakt collection, from each item's best copy."""

from __future__ import annotations

import asyncio
import gc
import logging
import time
from typing import TYPE_CHECKING, Any

from pakt.models import SyncResult
from pakt.plex import extract_media_metadata, extract_plex_ids, media_score
from pakt.sync import report
from pakt.sync.match import MOVIE_ID_TYPES, SHOW_ID_TYPES, group_by_show, id_keys, plex_dt_to_utc_iso, trakt_item
from pakt.trakt import TraktAccountLimitError

if TYPE_CHECKING:
    from pakt.sync.engine import SyncEngine

logger = logging.getLogger(__name__)


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

    collected_movies = {key for item in trakt_collection_movies
                        for key in id_keys(item.get("movie", {}).get("ids", {}), MOVIE_ID_TYPES)}
    collected_episodes: set[tuple] = set()
    for item in trakt_collection_shows:
        keys = id_keys(item.get("show", {}).get("ids", {}), SHOW_ID_TYPES)
        for season in item.get("seasons", []):
            for ep in season.get("episodes", []):
                collected_episodes.update((key, season.get("number", 0), ep.get("number", 0)) for key in keys)

    fixing = engine.fix_collection_dates
    best: dict[tuple, tuple[int, dict]] = {}
    total_movies = len(plex_movies)
    update_interval = max(1, total_movies // 100)
    yield_interval = max(1, min(500, total_movies // 200))
    for i, plex_movie in enumerate(plex_movies):
        if i % yield_interval == 0:
            await asyncio.sleep(0)
            if engine.is_cancelled():
                return False
        if i % update_interval == 0:
            engine.progress(3, 4, 25 + (i / total_movies) * 20, f"Movies {i}/{total_movies}")

        plex_ids = extract_plex_ids(plex_movie)
        keys = id_keys(plex_ids, MOVIE_ID_TYPES)
        if not keys or (not fixing and any(key in collected_movies for key in keys)):
            continue
        # the same movie in several libraries is collected once, from its best copy
        score = media_score(plex_movie)
        if keys[0] in best and best[keys[0]][0] >= score:
            continue
        movie = trakt_item(plex_movie, plex_ids, MOVIE_ID_TYPES) or {}
        movie.update(extract_media_metadata(plex_movie))
        collected_at = plex_dt_to_utc_iso(getattr(plex_movie, "addedAt", None))
        if collected_at:
            movie["collected_at"] = collected_at
        best[keys[0]] = (score, movie)
    movies_to_collect = [movie for _, movie in best.values()]
    engine.plex_cache.clear()
    del plex_movies, best

    show_info = {str(show.ratingKey): (extract_plex_ids(show), show.title, show.year) for show in plex_shows}
    rows: list[tuple[dict, int, dict, str]] = []
    shows_seen: set[tuple] = set()
    seen: set[tuple] = set()
    skipped_no_number = 0
    total_episodes = len(plex_episodes)
    update_interval = max(1, total_episodes // 100)
    yield_interval = max(1, min(500, total_episodes // 200))
    for i, episode in enumerate(plex_episodes):
        if i % yield_interval == 0:
            await asyncio.sleep(0)
            if engine.is_cancelled():
                return False
        if i % update_interval == 0:
            engine.progress(3, 4, 45 + (i / total_episodes) * 30, f"Episodes {i}/{total_episodes}")

        info = show_info.get(str(episode.grandparentRatingKey))
        keys = id_keys(info[0], SHOW_ID_TYPES) if info else []
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
        shows_seen.add(keys[0])
        entry: dict[str, Any] = {"number": number}
        collected_at = plex_dt_to_utc_iso(getattr(episode, "addedAt", None))
        if collected_at:
            entry["collected_at"] = collected_at
        _, title, year = info
        rows.append(({"title": title, "year": year, "ids": dict(keys)}, season, entry, ""))
    del plex_episodes, plex_shows, show_info
    gc.collect()

    shows_to_collect = group_by_show(rows)
    if skipped_no_number:
        logger.warning(
            f"Collection: skipped {report.plural(skipped_no_number, 'episode')} without a season or episode number"
        )
    sending = [report.plural(len(movies_to_collect), "movie")] if movies_to_collect else []
    if rows:
        sending.append(f"{report.plural(len(rows), 'episode')} of {report.plural(len(shows_seen), 'show')}")
    if not sending:
        planned = "no changes"
    elif fixing:
        planned = f"re-sending {' and '.join(sending)} with Plex dates"
    else:
        planned = f"Trakt +{', +'.join(sending)}"
    logger.info(f"Collection: {planned} ({report.duration(time.time() - phase_start)})")

    if engine.verbose:
        for m in movies_to_collect:
            added = f", added {m['collected_at'][:10]}" if m.get("collected_at") else ""
            report.item(f"Trakt collected: {report.title(m)}{added}")
        for show in shows_to_collect:
            codes = [f"S{s['number']:02d}E{e['number']:02d}" for s in show["seasons"] for e in s["episodes"]]
            listed = ", ".join(codes[:5]) + (f" and {len(codes) - 5} more" if len(codes) > 5 else "")
            report.item(f"Trakt collected: {show['title']} {listed}")

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
