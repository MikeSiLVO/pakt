"""Sync logic for Pakt."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from pakt.cache import Cache
from pakt.config import Config
from pakt.models import MediaType, SyncResult
from pakt.plex import PlexClient, extract_plex_ids, plex_movie_to_media_item
from pakt.trakt import TraktClient, extract_trakt_ids

console = Console()


class SyncEngine:
    """Main sync engine coordinating Plex and Trakt."""

    def __init__(
        self,
        config: Config,
        trakt: TraktClient,
        plex: PlexClient,
        cache: Cache,
    ):
        self.config = config
        self.trakt = trakt
        self.plex = plex
        self.cache = cache

    async def sync(self, dry_run: bool = False) -> SyncResult:
        """Run full sync."""
        start_time = time.time()
        result = SyncResult()

        console.print("[bold]Starting Pakt sync...[/]")

        # Phase 1: Fetch all data from both services (batch operations)
        console.print("\n[cyan]Phase 1:[/] Fetching data from Trakt and Plex...")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
        ) as progress:
            # Fetch from Trakt (batch - single API call each)
            task = progress.add_task("Fetching Trakt watched movies...", total=None)
            trakt_watched_movies = await self.trakt.get_watched_movies()
            progress.update(task, description=f"Got {len(trakt_watched_movies)} watched movies from Trakt")

            task = progress.add_task("Fetching Trakt watched shows...", total=None)
            trakt_watched_shows = await self.trakt.get_watched_shows()
            progress.update(task, description=f"Got {len(trakt_watched_shows)} watched shows from Trakt")

            task = progress.add_task("Fetching Trakt movie ratings...", total=None)
            trakt_movie_ratings = await self.trakt.get_movie_ratings()
            progress.update(task, description=f"Got {len(trakt_movie_ratings)} movie ratings from Trakt")

            task = progress.add_task("Fetching Trakt show ratings...", total=None)
            trakt_show_ratings = await self.trakt.get_show_ratings()
            progress.update(task, description=f"Got {len(trakt_show_ratings)} show ratings from Trakt")

            # Fetch from Plex
            task = progress.add_task("Fetching Plex movies...", total=None)
            plex_movies = self.plex.get_all_movies(self.config.sync.movie_libraries or None)
            progress.update(task, description=f"Got {len(plex_movies)} movies from Plex")

            task = progress.add_task("Fetching Plex shows...", total=None)
            plex_shows = self.plex.get_all_shows(self.config.sync.show_libraries or None)
            progress.update(task, description=f"Got {len(plex_shows)} shows from Plex")

        console.print(f"  Trakt: {len(trakt_watched_movies)} movies, {len(trakt_watched_shows)} shows watched")
        console.print(f"  Trakt: {len(trakt_movie_ratings)} movie ratings, {len(trakt_show_ratings)} show ratings")
        console.print(f"  Plex: {len(plex_movies)} movies, {len(plex_shows)} shows")

        # Phase 2: Build lookup indices
        console.print("\n[cyan]Phase 2:[/] Building indices...")

        # Index Trakt watched by various IDs
        trakt_watched_by_imdb: dict[str, dict] = {}
        trakt_watched_by_tmdb: dict[int, dict] = {}
        trakt_watched_by_trakt: dict[int, dict] = {}

        for item in trakt_watched_movies:
            if item.movie:
                ids = item.movie.get("ids", {})
                data = {"item": item, "movie": item.movie}
                if ids.get("imdb"):
                    trakt_watched_by_imdb[ids["imdb"]] = data
                if ids.get("tmdb"):
                    trakt_watched_by_tmdb[ids["tmdb"]] = data
                if ids.get("trakt"):
                    trakt_watched_by_trakt[ids["trakt"]] = data

        # Index Trakt ratings
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

        console.print(f"  Indexed {len(trakt_watched_by_imdb)} by IMDB, {len(trakt_watched_by_tmdb)} by TMDB")

        # Phase 3: Compare and sync
        console.print("\n[cyan]Phase 3:[/] Syncing...")

        movies_to_mark_watched_trakt: list[dict] = []
        movies_to_mark_watched_plex: list[Any] = []
        movies_to_rate_trakt: list[dict] = []
        movies_to_rate_plex: list[tuple[Any, int]] = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
        ) as progress:
            task = progress.add_task("Processing movies...", total=len(plex_movies))

            for plex_movie in plex_movies:
                progress.advance(task)
                plex_ids = extract_plex_ids(plex_movie)

                # Find matching Trakt entry
                trakt_data = None
                if plex_ids.imdb and plex_ids.imdb in trakt_watched_by_imdb:
                    trakt_data = trakt_watched_by_imdb[plex_ids.imdb]
                elif plex_ids.tmdb and plex_ids.tmdb in trakt_watched_by_tmdb:
                    trakt_data = trakt_watched_by_tmdb[plex_ids.tmdb]

                # Find Trakt rating
                trakt_rating = None
                if plex_ids.imdb and plex_ids.imdb in trakt_ratings_by_imdb:
                    trakt_rating = trakt_ratings_by_imdb[plex_ids.imdb]
                elif plex_ids.tmdb and plex_ids.tmdb in trakt_ratings_by_tmdb:
                    trakt_rating = trakt_ratings_by_tmdb[plex_ids.tmdb]

                # Sync watched status
                plex_watched = plex_movie.isWatched
                trakt_watched = trakt_data is not None

                if plex_watched and not trakt_watched and self.config.sync.watched_plex_to_trakt:
                    # Mark watched on Trakt
                    movie_data = self._build_trakt_movie(plex_movie, plex_ids)
                    if movie_data:
                        movies_to_mark_watched_trakt.append(movie_data)

                elif trakt_watched and not plex_watched and self.config.sync.watched_trakt_to_plex:
                    # Mark watched on Plex
                    movies_to_mark_watched_plex.append(plex_movie)

                # Sync ratings
                plex_rating = int(plex_movie.userRating) if plex_movie.userRating else None
                trakt_rating_val = trakt_rating["rating"] if trakt_rating else None

                if plex_rating and not trakt_rating_val and self.config.sync.ratings_plex_to_trakt:
                    # Add rating to Trakt
                    movie_data = self._build_trakt_movie(plex_movie, plex_ids)
                    if movie_data:
                        movie_data["rating"] = plex_rating
                        movies_to_rate_trakt.append(movie_data)

                elif trakt_rating_val and not plex_rating and self.config.sync.ratings_trakt_to_plex:
                    # Add rating to Plex
                    movies_to_rate_plex.append((plex_movie, trakt_rating_val))

        console.print(f"  To mark watched on Trakt: {len(movies_to_mark_watched_trakt)}")
        console.print(f"  To mark watched on Plex: {len(movies_to_mark_watched_plex)}")
        console.print(f"  To rate on Trakt: {len(movies_to_rate_trakt)}")
        console.print(f"  To rate on Plex: {len(movies_to_rate_plex)}")

        # Phase 4: Apply changes
        if not dry_run:
            console.print("\n[cyan]Phase 4:[/] Applying changes...")

            # Batch update Trakt (single API call for all movies!)
            if movies_to_mark_watched_trakt:
                console.print(f"  Adding {len(movies_to_mark_watched_trakt)} movies to Trakt history...")
                response = await self.trakt.add_to_history(movies=movies_to_mark_watched_trakt)
                result.added_to_trakt = response.get("added", {}).get("movies", 0)

            if movies_to_rate_trakt:
                console.print(f"  Adding {len(movies_to_rate_trakt)} ratings to Trakt...")
                response = await self.trakt.add_ratings(movies=movies_to_rate_trakt)
                result.ratings_synced += response.get("added", {}).get("movies", 0)

            # Update Plex (individual calls, but usually fewer)
            for plex_movie in movies_to_mark_watched_plex:
                self.plex.mark_watched(plex_movie)
                result.added_to_plex += 1

            for plex_movie, rating in movies_to_rate_plex:
                self.plex.set_rating(plex_movie, rating)
                result.ratings_synced += 1
        else:
            console.print("\n[yellow]Dry run - no changes applied[/]")

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


async def run_sync(config: Config, dry_run: bool = False) -> SyncResult:
    """Run sync with all clients."""
    plex = PlexClient(config.plex)
    plex.connect()

    async with TraktClient(config.trakt) as trakt:
        async with Cache(config.cache) as cache:
            engine = SyncEngine(config, trakt, plex, cache)
            return await engine.sync(dry_run=dry_run)
