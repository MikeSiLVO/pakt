"""Episode phase: watched status and ratings per episode, compared in a worker thread."""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from pakt.log import console
from pakt.models import PlexIds, RatedItem, SyncResult, WatchedItem
from pakt.plex.media import extract_plex_ids
from pakt.sync import report
from pakt.sync.match import SHOW_ID_TYPES, group_by_show, id_keys, plex_dt_to_utc_iso, rating_direction

if TYPE_CHECKING:
    from pakt.sync.engine import PushedToTrakt, SyncEngine

logger = logging.getLogger(__name__)


@dataclass
class EpisodeProcessingResult:
    """Episode changes worked out in the worker thread, each with a display label."""

    watched_to_trakt: list[tuple[dict, int, dict, str]] = field(default_factory=list)
    watched_to_plex: list[tuple[Any, str]] = field(default_factory=list)
    rated_to_trakt: list[tuple[dict, int, dict, str]] = field(default_factory=list)
    rated_to_plex: list[tuple[Any, int, str]] = field(default_factory=list)
    watched_keys: set[tuple] = field(default_factory=set)
    rated_keys: set[tuple] = field(default_factory=set)
    skipped_no_ids: Counter[str] = field(default_factory=Counter)
    skipped_no_number: Counter[str] = field(default_factory=Counter)
    cancelled: bool = False


def _is_watched(ep_data: dict) -> bool:
    """Whether a Trakt episode is watched; extended=progress also lists unwatched ones."""
    completed = ep_data.get("completed")
    if completed is False:
        return False
    if completed is None:
        return bool(ep_data.get("last_watched_at") or ep_data.get("plays"))
    return True


def episode_index(shows: list[WatchedItem]) -> set[tuple]:
    """Keys of every watched episode in Trakt's watched shows, one per show id kind."""
    index: set[tuple] = set()
    for show_item in shows:
        keys = id_keys(show_item.show.get("ids", {}), SHOW_ID_TYPES) if show_item.show else []
        for season in show_item.seasons or []:
            for ep in season.get("episodes", []):
                if _is_watched(ep):
                    index.update((key, season.get("number", 0), ep.get("number", 0)) for key in keys)
    return index


def episode_ratings(ratings: list[RatedItem]) -> dict[tuple, int]:
    """Trakt episode ratings keyed like the watched episode index."""
    index: dict[tuple, int] = {}
    for item in ratings:
        if item.episode and item.show:
            season, number = item.episode.get("season", 0), item.episode.get("number", 0)
            for key in id_keys(item.show.get("ids", {}), SHOW_ID_TYPES):
                index[(key, season, number)] = item.rating
    return index


def process_in_thread(
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
    """Process Plex episodes against Trakt data into the changes each side needs."""
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
            ep.grandparentTitle, plex_dt_to_utc_iso(ep.lastViewedAt), plex_dt_to_utc_iso(ep.lastRatedAt), ep,
        ))
    if progress_callback:
        progress_callback(total, total)

    for i, (show_key, season, number, plex_watched, plex_rating, show_title,
            watched_at, rated_at, episode) in enumerate(rows):
        if i % 500 == 0 and cancel_event.is_set():
            result.cancelled = True
            return result
        show_ids = plex_show_ids_by_key.get(show_key)
        keys = id_keys(show_ids, SHOW_ID_TYPES) if show_ids else []
        if not keys:
            result.skipped_no_ids[show_title] += 1
            continue
        if season is None or number is None:
            result.skipped_no_number[show_title] += 1
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
        send, fetch = rating_direction(
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


async def run(engine: SyncEngine, result: SyncResult, dry_run: bool) -> bool:
    """Sync episodes. Returns False if cancelled."""
    phase_start = time.time()
    engine.progress(2, 4, 5, "Trakt watched shows")
    trakt_watched_shows = await engine.cache.get(engine.trakt.get_watched_shows)
    engine.progress(2, 4, 15, "Trakt episode ratings")
    trakt_episode_ratings = await engine.cache.get(engine.trakt.get_episode_ratings)
    engine.progress(2, 4, 25, "Plex shows")
    plex_shows, _ = await engine.fetch_plex(engine.plex.get_all_shows_with_counts, engine.show_libraries_to_sync())
    engine.progress(2, 4, 30, "Plex episodes")
    plex_episodes, episode_libs = await engine.fetch_plex(
        engine.plex.get_all_episodes_with_counts, engine.show_libraries_to_sync(), warn=False
    )

    libraries = ", ".join(f"{count} in '{name}'" for name, count in episode_libs.items()) or "no libraries"
    logger.info(
        f"Episodes: Trakt {len(trakt_watched_shows)} shows watched, "
        f"{len(trakt_episode_ratings)} episodes rated; Plex {libraries}"
    )

    plex_show_ids_by_key = {str(show.ratingKey): extract_plex_ids(show) for show in plex_shows}
    del plex_shows

    watched_index = episode_index(trakt_watched_shows)
    # shows without episodes mean a changed response shape; syncing on it would resend every play
    if trakt_watched_shows and not watched_index:
        logger.error(
            f"Episodes: Trakt listed {report.plural(len(trakt_watched_shows), 'watched show')} but no watched "
            "episodes; skipping episodes rather than act on that"
        )
        result.errors.append("Episodes: Trakt listed watched shows without episodes")
        return True
    ratings_index = episode_ratings(trakt_episode_ratings)

    total_episodes = len(plex_episodes)
    cancel_event = threading.Event()
    progress_state = {"processed": 0}

    def on_progress(processed: int, total: int) -> None:
        """Called from the worker thread; only touches plain dict entries."""
        progress_state["processed"] = processed

    process_task = asyncio.create_task(asyncio.to_thread(
        process_in_thread,
        plex_episodes, plex_show_ids_by_key, watched_index, ratings_index, engine.options(),
        engine.config.sync.rating_priority, cancel_event, engine.pushed, on_progress,
    ))

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(), console=console, transient=False
    ) as progress:
        task = progress.add_task(f"Episodes 0/{total_episodes}", total=total_episodes)
        while not process_task.done():
            if engine.is_cancelled():
                cancel_event.set()
            processed = progress_state["processed"]
            engine.progress(2, 4, 35 + (processed / max(1, total_episodes)) * 50,
                            f"Episodes {processed}/{total_episodes}")
            progress.update(task, completed=processed, description=f"Episodes {processed}/{total_episodes}")
            await asyncio.sleep(0.1)
        progress.update(task, completed=total_episodes, description=f"Episodes {total_episodes}/{total_episodes}")

    ep_result = await process_task
    del plex_episodes, plex_show_ids_by_key, watched_index, ratings_index
    gc.collect()
    if ep_result.cancelled or engine.is_cancelled():
        return False

    for skipped, reason in ((ep_result.skipped_no_ids, "whose show has no IMDb, TMDB or TVDB ID"),
                            (ep_result.skipped_no_number, "without a season or episode number")):
        if skipped:
            logger.warning(f"Episodes: skipped {report.plural(skipped.total(), 'episode')} {reason}")
            if engine.verbose:
                report.items([f"{show}: {report.plural(count, 'episode')}" for show, count in skipped.most_common()])
    planned = report.changes(
        {"watched": len(ep_result.watched_to_trakt), "rated": len(ep_result.rated_to_trakt)},
        {"watched": len(ep_result.watched_to_plex), "rated": len(ep_result.rated_to_plex)},
    )
    logger.info(f"Episodes: {planned} ({report.duration(time.time() - phase_start)})")

    if engine.verbose:
        for *_, label in ep_result.watched_to_trakt:
            report.item(f"Trakt watched: {label}")
        for _, label in ep_result.watched_to_plex:
            report.item(f"Plex watched: {label}")
        for *_, label in ep_result.rated_to_trakt:
            report.item(f"Trakt rated: {label}")
        for *_, label in ep_result.rated_to_plex:
            report.item(f"Plex rated: {label}")

    pushed = engine.pushed
    if not dry_run:
        engine.progress(2, 4, 90, "Applying episode changes")
        response = await engine.push(
            "history", engine.trakt.add_to_history, shows=group_by_show(ep_result.watched_to_trakt),
        )
        result.added_to_trakt += response.get("added", {}).get("episodes", 0)
    pushed.watched_episodes |= ep_result.watched_keys
    if not dry_run:
        response = await engine.push(
            "ratings", engine.trakt.add_ratings, shows=group_by_show(ep_result.rated_to_trakt),
        )
        result.ratings_to_trakt += response.get("added", {}).get("episodes", 0)
    pushed.rated_episodes |= ep_result.rated_keys
    if not dry_run:
        result.added_to_plex += await engine.write_plex(
            "episodes marked watched", engine.plex.mark_watched_batch,
            [ep for ep, _ in ep_result.watched_to_plex], result,
        )
        result.ratings_to_plex += await engine.write_plex(
            "episodes rated", engine.plex.rate_batch,
            [(ep, rating) for ep, rating, _ in ep_result.rated_to_plex], result,
        )

    engine.progress(2, 4, 100, "Episodes complete")
    return True
