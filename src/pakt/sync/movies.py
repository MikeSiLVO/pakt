"""Movie phase: watched status and ratings between Plex movies and Trakt."""

from __future__ import annotations

import gc
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from pakt.log import console
from pakt.models import SyncResult
from pakt.plex.media import extract_plex_ids
from pakt.sync import report
from pakt.sync.match import MOVIE_ID_TYPES, id_keys, plex_dt_to_utc_iso, rating_direction, trakt_item

if TYPE_CHECKING:
    from pakt.sync.engine import SyncEngine

logger = logging.getLogger(__name__)


@dataclass
class MoviePlan:
    """Movie changes each side needs, worked out from one server's library."""

    to_trakt_watched: list[dict] = field(default_factory=list)
    to_trakt_rated: list[dict] = field(default_factory=list)
    to_plex_watched: list[Any] = field(default_factory=list)
    to_plex_rated: list[tuple[Any, int]] = field(default_factory=list)
    no_ids: list[str] = field(default_factory=list)


async def plan(
    engine: SyncEngine, plex_movies: list, watched_keys: set[tuple], rating_by_key: dict[tuple, int],
    on_step: Callable[[int], None] | None = None,
) -> MoviePlan:
    """Plan each side's movie changes, marking what goes to Trakt as pushed; stops on a cancel."""
    options = engine.options()
    priority = engine.config.sync.rating_priority
    pushed = engine.pushed
    changes = MoviePlan()
    async for plex_movie in engine.paced(plex_movies, 1, 30, 40, "Movies", on_step):
        plex_ids = extract_plex_ids(plex_movie)
        keys = id_keys(plex_ids, MOVIE_ID_TYPES)
        if not keys:
            changes.no_ids.append(f"{plex_movie.title} ({plex_movie.year})")
            continue
        trakt_watched = any(key in watched_keys for key in keys)
        trakt_rating = next((rating_by_key[key] for key in keys if key in rating_by_key), None)

        if plex_movie.isWatched and not trakt_watched:
            # marked when queued, as a push can land in part and still fail
            if options["watched_plex_to_trakt"] and not any(key in pushed.watched_movies for key in keys):
                movie = trakt_item(plex_movie, plex_ids, MOVIE_ID_TYPES) or {}
                watched_at = plex_dt_to_utc_iso(plex_movie.lastViewedAt)
                if watched_at:
                    movie["watched_at"] = watched_at
                changes.to_trakt_watched.append(movie)
                pushed.watched_movies.update(keys)
        elif trakt_watched and not plex_movie.isWatched and options["watched_trakt_to_plex"]:
            changes.to_plex_watched.append(plex_movie)

        plex_rating = int(plex_movie.userRating) if plex_movie.userRating else None
        send, fetch = rating_direction(
            plex_rating, trakt_rating, priority,
            options["ratings_plex_to_trakt"], options["ratings_trakt_to_plex"],
        )
        if send and not any(key in pushed.rated_movies for key in keys):
            movie = trakt_item(plex_movie, plex_ids, MOVIE_ID_TYPES) or {}
            movie["rating"] = plex_rating
            rated_at = plex_dt_to_utc_iso(plex_movie.lastRatedAt)
            if rated_at:
                movie["rated_at"] = rated_at
            changes.to_trakt_rated.append(movie)
            pushed.rated_movies.update(keys)
        elif fetch and trakt_rating is not None:
            changes.to_plex_rated.append((plex_movie, trakt_rating))
    return changes


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

    total = len(plex_movies)
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(), console=console, transient=False
    ) as progress:
        task = progress.add_task(f"Movies 0/{total}", total=total)

        def step(i: int) -> None:
            progress.update(task, completed=i, description=f"Movies {i}/{total}")

        changes = await plan(engine, plex_movies, watched_keys, rating_by_key, step)
        if engine.is_cancelled():
            return False
        step(total)

    del plex_movies
    gc.collect()

    if changes.no_ids:
        logger.warning(f"Movies: skipped {report.plural(len(changes.no_ids), 'movie')} without an IMDb or TMDB ID")
        if engine.verbose:
            report.items(changes.no_ids)
    planned = report.changes(
        {"watched": len(changes.to_trakt_watched), "rated": len(changes.to_trakt_rated)},
        {"watched": len(changes.to_plex_watched), "rated": len(changes.to_plex_rated)},
    )
    logger.info(f"Movies: {planned} ({report.duration(time.time() - phase_start)})")

    if engine.verbose:
        for m in changes.to_trakt_watched:
            report.item(f"Trakt watched: {report.title(m)}")
        for m in changes.to_plex_watched:
            report.item(f"Plex watched: {m.title} ({m.year})")
        for m in changes.to_trakt_rated:
            report.item(f"Trakt rated: {report.title(m)} = {m.get('rating')}")
        for m, rating in changes.to_plex_rated:
            report.item(f"Plex rated: {m.title} ({m.year}) = {rating}")

    if not dry_run:
        engine.progress(1, 4, 75, "Applying movie changes")
        response = await engine.push("history", engine.trakt.add_to_history, movies=changes.to_trakt_watched)
        result.added_to_trakt += response.get("added", {}).get("movies", 0)
        response = await engine.push("ratings", engine.trakt.add_ratings, movies=changes.to_trakt_rated)
        result.ratings_to_trakt += response.get("added", {}).get("movies", 0)
        result.added_to_plex += await engine.write_plex(
            "movies marked watched", engine.plex.mark_watched_batch, changes.to_plex_watched, result,
        )
        result.ratings_to_plex += await engine.write_plex(
            "movies rated", engine.plex.rate_batch, changes.to_plex_rated, result,
        )

    engine.progress(1, 4, 100, "Movies complete")
    return True
