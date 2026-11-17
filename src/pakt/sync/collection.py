"""Collection phase: the Plex library sent to the Trakt collection, from each item's best copy."""

from __future__ import annotations

import gc
import logging
import time
from typing import TYPE_CHECKING, Any

from pakt.models import SyncResult
from pakt.plex.media import best_media, extract_plex_ids, metadata_score
from pakt.sync import report
from pakt.sync.match import MOVIE_ID_TYPES, SHOW_ID_TYPES, group_by_show, id_keys, plex_dt_to_utc_iso, trakt_item
from pakt.trakt import TraktAccountLimitError

if TYPE_CHECKING:
    from pakt.sync.engine import SyncEngine

logger = logging.getLogger(__name__)


def _trakt_collection(movies: list[dict], shows: list[dict]) -> tuple[dict[tuple, tuple[int, str]], set[tuple]]:
    """Trakt's collection as each movie's score and date by key, and the keys of its episodes."""
    held: dict[tuple, tuple[int, str]] = {}
    for item in movies:
        entry = (metadata_score(item.get("metadata") or {}), item.get("collected_at") or "")
        held.update(dict.fromkeys(id_keys(item.get("movie", {}).get("ids", {}), MOVIE_ID_TYPES), entry))
    episode_keys: set[tuple] = set()
    for item in shows:
        keys = id_keys(item.get("show", {}).get("ids", {}), SHOW_ID_TYPES)
        for season in item.get("seasons", []):
            for ep in season.get("episodes", []):
                episode_keys.update((key, season.get("number", 0), ep.get("number", 0)) for key in keys)
    return held, episode_keys


def _earliest(*dates: str | None) -> str | None:
    """Earliest of the given dates that are set, or None."""
    return min(filter(None, dates), default=None)


async def _movies_to_collect(
    engine: SyncEngine, plex_movies: list, held: dict[tuple, tuple[int, str]],
) -> tuple[list[dict], list[dict]]:
    """Movies to add and upgrade from each title's best copy, marked pushed; stops on a cancel."""
    best: dict[tuple, tuple[int, dict]] = {}
    earliest: dict[tuple, str | None] = {}
    slots: dict[tuple, tuple] = {}
    slot_keys: dict[tuple, set[tuple]] = {}
    async for plex_movie in engine.paced(plex_movies, 3, 25, 20, "Movies"):
        plex_ids = extract_plex_ids(plex_movie)
        keys = id_keys(plex_ids, MOVIE_ID_TYPES)
        if not keys:
            continue
        slot = next((slots[key] for key in keys if key in slots), keys[0])
        for key in keys:
            slots.setdefault(key, slot)
        slot_keys.setdefault(slot, set()).update(keys)
        earliest[slot] = _earliest(earliest.get(slot), plex_dt_to_utc_iso(getattr(plex_movie, "addedAt", None)))
        score, metadata = best_media(plex_movie)
        if slot not in best or score > best[slot][0]:
            best[slot] = (score, (trakt_item(plex_movie, plex_ids, MOVIE_ID_TYPES) or {}) | metadata)
    if engine.is_cancelled():
        return [], []
    pushed = engine.pushed.collected_movies
    to_add: list[dict] = []
    to_upgrade: list[dict] = []
    for slot, (score, movie) in best.items():
        keys = slot_keys[slot]
        known = [table[key] for table in (held, pushed) for key in keys if key in table]
        if known and score <= max(s for s, _ in known):
            continue
        collected_at = _earliest(earliest[slot], *(date for _, date in known))
        if collected_at:
            movie["collected_at"] = collected_at
        (to_upgrade if known else to_add).append(movie)
        pushed.update(dict.fromkeys(keys, (score, collected_at or "")))
    return to_add, to_upgrade


async def _episodes_to_collect(
    engine: SyncEngine, plex_shows: list, plex_episodes: list, collected: set[tuple],
) -> tuple[list[tuple[dict, int, dict, str]], int, int]:
    """Episode rows to collect with show and unnumbered counts, marked pushed; stops on a cancel."""
    show_info = {str(show.ratingKey): (extract_plex_ids(show), show.title, show.year) for show in plex_shows}
    rows: list[tuple[dict, int, dict, str]] = []
    shows_seen: set[tuple] = set()
    seen = collected | engine.pushed.collected_episodes
    queued: dict[tuple, dict[str, Any]] = {}
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
        entry = next((queued[k] for k in ep_keys if k in queued), None)
        if entry is None:
            known = any(k in seen for k in ep_keys)
            # its other copies may carry only ids Trakt did not list
            seen.update(ep_keys)
            if known:
                continue
            entry = {"number": number}
            _, title, year = info
            rows.append(({"title": title, "year": year, "ids": dict(keys)}, season, entry, ""))
            shows_seen.add(keys[0])
        for k in ep_keys:
            queued.setdefault(k, entry)
        collected_at = _earliest(entry.get("collected_at"), plex_dt_to_utc_iso(getattr(episode, "addedAt", None)))
        if collected_at:
            entry["collected_at"] = collected_at
    engine.pushed.collected_episodes.update(queued)
    return rows, len(shows_seen), unnumbered


def _describe(added: int, upgraded: int, episodes: int, shows: int, fixing: bool) -> str:
    """Describe what the phase sends, such as 'Trakt +2 movies, 1 movie upgraded'."""
    sending = [report.plural(added, "movie")] if added else []
    if episodes:
        sending.append(f"{report.plural(episodes, 'episode')} of {report.plural(shows, 'show')}")
    upgrades = [f"{report.plural(upgraded, 'movie')} upgraded"] if upgraded else []
    if fixing and sending:
        return ", ".join([f"re-sending {' and '.join(sending)} with Plex dates", *upgrades])
    if not sending and not upgrades:
        return "no changes"
    return "Trakt " + ", ".join([f"+{s}" for s in sending] + upgrades)


def _list_items(added: list[dict], upgraded: list[dict], shows: list[dict]) -> None:
    """List each movie and show being sent, with up to five episode codes per show."""
    for movie in added:
        when = f", added {movie['collected_at'][:10]}" if movie.get("collected_at") else ""
        report.item(f"Trakt collected: {report.title(movie)}{when}")
    for movie in upgraded:
        quality = ", ".join(movie[k] for k in ("resolution", "hdr", "audio", "audio_channels") if movie.get(k))
        report.item(f"Trakt upgraded: {report.title(movie)} to {quality}")
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
    held_movies, collected_episodes = (
        ({}, set()) if fixing else _trakt_collection(trakt_collection_movies, trakt_collection_shows)
    )
    to_add, to_upgrade = await _movies_to_collect(engine, plex_movies, held_movies)
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
    planned = _describe(len(to_add), len(to_upgrade), len(rows), show_count, fixing)
    logger.info(f"Collection: {planned} ({report.duration(time.time() - phase_start)})")
    if engine.verbose:
        _list_items(to_add, to_upgrade, shows_to_collect)

    if not dry_run:
        engine.progress(3, 4, 80, "Adding to Trakt collection")
        try:
            response = await engine.push("collection", engine.trakt.add_to_collection, movies=to_add + to_upgrade)
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
