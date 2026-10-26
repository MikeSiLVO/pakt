"""Movie phase: watched status and ratings between Plex movies and Trakt."""

from __future__ import annotations

import asyncio
import gc
import logging
import time
from typing import TYPE_CHECKING, Any

from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from pakt.log import console
from pakt.models import SyncResult
from pakt.plex.media import extract_plex_ids
from pakt.sync import report
from pakt.sync.match import MOVIE_ID_TYPES, id_keys, plex_dt_to_utc_iso, rating_direction, trakt_item

if TYPE_CHECKING:
    from pakt.sync.engine import SyncEngine

logger = logging.getLogger(__name__)


async def run(engine: SyncEngine, result: SyncResult, dry_run: bool) -> bool:
    """Sync movies. Returns False if cancelled."""
    phase_start = time.time()
    engine.progress(1, 4, 5, "Trakt watched movies")
    trakt_watched_movies = await engine.cache.get(engine.trakt.get_watched_movies)
    engine.progress(1, 4, 15, "Trakt movie ratings")
    trakt_movie_ratings = await engine.cache.get(engine.trakt.get_movie_ratings)
    engine.progress(1, 4, 25, "Plex movies")
    plex_movies, movie_libs = await engine.fetch_plex(
        engine.plex.get_all_movies_with_counts, engine.movie_libraries_to_sync()
    )

    libraries = ", ".join(f"{count} in '{name}'" for name, count in movie_libs.items()) or "no libraries"
    logger.info(
        f"Movies: Trakt {len(trakt_watched_movies)} watched, {len(trakt_movie_ratings)} rated; Plex {libraries}"
    )

    watched_keys = {key for item in trakt_watched_movies if item.movie
                    for key in id_keys(item.movie.get("ids", {}), MOVIE_ID_TYPES)}
    rating_by_key = {key: item.rating for item in trakt_movie_ratings if item.movie
                     for key in id_keys(item.movie.get("ids", {}), MOVIE_ID_TYPES)}

    options = engine.options()
    priority = engine.config.sync.rating_priority
    pushed = engine.pushed
    to_trakt_watched: list[dict] = []
    to_trakt_rated: list[dict] = []
    to_plex_watched: list[Any] = []
    to_plex_rated: list[tuple[Any, int]] = []
    queued_watched: set[tuple] = set()
    queued_rated: set[tuple] = set()
    no_ids: list[str] = []

    total_movies = len(plex_movies)
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
                if engine.is_cancelled():
                    return False
            if i % update_interval == 0:
                engine.progress(1, 4, 30 + (i / total_movies) * 40, f"Movies {i}/{total_movies}")
                progress.update(task, completed=i, description=f"Movies {i}/{total_movies}")

            plex_ids = extract_plex_ids(plex_movie)
            keys = id_keys(plex_ids, MOVIE_ID_TYPES)
            if not keys:
                no_ids.append(f"{plex_movie.title} ({plex_movie.year})")
                continue
            movie_key = keys[0]
            trakt_watched = any(key in watched_keys for key in keys)
            trakt_rating = next((rating_by_key[key] for key in keys if key in rating_by_key), None)

            if plex_movie.isWatched and not trakt_watched:
                if (options["watched_plex_to_trakt"] and movie_key not in pushed.watched_movies
                        and movie_key not in queued_watched):
                    movie = trakt_item(plex_movie, plex_ids, MOVIE_ID_TYPES) or {}
                    watched_at = plex_dt_to_utc_iso(plex_movie.lastViewedAt)
                    if watched_at:
                        movie["watched_at"] = watched_at
                    to_trakt_watched.append(movie)
                    queued_watched.add(movie_key)
            elif trakt_watched and not plex_movie.isWatched and options["watched_trakt_to_plex"]:
                to_plex_watched.append(plex_movie)

            plex_rating = int(plex_movie.userRating) if plex_movie.userRating else None
            send, fetch = rating_direction(
                plex_rating, trakt_rating, priority,
                options["ratings_plex_to_trakt"], options["ratings_trakt_to_plex"],
            )
            if send and movie_key not in pushed.rated_movies and movie_key not in queued_rated:
                movie = trakt_item(plex_movie, plex_ids, MOVIE_ID_TYPES) or {}
                movie["rating"] = plex_rating
                rated_at = plex_dt_to_utc_iso(plex_movie.lastRatedAt)
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
        logger.warning(f"Movies: skipped {report.plural(len(no_ids), 'movie')} without an IMDb or TMDB ID")
        if engine.verbose:
            report.items(no_ids)
    planned = report.changes(
        {"watched": len(to_trakt_watched), "rated": len(to_trakt_rated)},
        {"watched": len(to_plex_watched), "rated": len(to_plex_rated)},
    )
    logger.info(f"Movies: {planned} ({report.duration(time.time() - phase_start)})")

    if engine.verbose:
        for m in to_trakt_watched:
            report.item(f"Trakt watched: {report.title(m)}")
        for m in to_plex_watched:
            report.item(f"Plex watched: {m.title} ({m.year})")
        for m in to_trakt_rated:
            report.item(f"Trakt rated: {report.title(m)} = {m.get('rating')}")
        for m, rating in to_plex_rated:
            report.item(f"Plex rated: {m.title} ({m.year}) = {rating}")

    if not dry_run:
        engine.progress(1, 4, 75, "Applying movie changes")
        response = await engine.push("history", engine.trakt.add_to_history, movies=to_trakt_watched)
        result.added_to_trakt += response.get("added", {}).get("movies", 0)
    pushed.watched_movies |= queued_watched
    if not dry_run:
        response = await engine.push("ratings", engine.trakt.add_ratings, movies=to_trakt_rated)
        result.ratings_to_trakt += response.get("added", {}).get("movies", 0)
    pushed.rated_movies |= queued_rated
    if not dry_run:
        result.added_to_plex += await engine.write_plex(
            "movies marked watched", engine.plex.mark_watched_batch, to_plex_watched, result,
        )
        result.ratings_to_plex += await engine.write_plex(
            "movies rated", engine.plex.rate_batch, to_plex_rated, result,
        )

    engine.progress(1, 4, 100, "Movies complete")
    return True
