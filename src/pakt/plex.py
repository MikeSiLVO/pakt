"""Plex API client wrapper."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer
from plexapi.video import Episode, Movie, Show

from pakt.config import PlexConfig
from pakt.models import MediaItem, MediaType, PlexIds, TraktIds


class PlexClient:
    """Plex API client optimized for batch operations."""

    def __init__(self, config: PlexConfig):
        self.config = config
        self._server: PlexServer | None = None

    def connect(self) -> None:
        """Connect to Plex server."""
        if self.config.url and self.config.token:
            self._server = PlexServer(self.config.url, self.config.token)
        elif self.config.token and self.config.server_name:
            account = MyPlexAccount(token=self.config.token)
            self._server = account.resource(self.config.server_name).connect()
        else:
            raise ValueError("Need either URL+token or token+server_name")

    @property
    def server(self) -> PlexServer:
        if not self._server:
            self.connect()
        return self._server

    def get_movie_libraries(self) -> list[str]:
        """Get all movie library names."""
        return [lib.title for lib in self.server.library.sections() if lib.type == "movie"]

    def get_show_libraries(self) -> list[str]:
        """Get all TV show library names."""
        return [lib.title for lib in self.server.library.sections() if lib.type == "show"]

    def get_all_movies(self, library_names: list[str] | None = None) -> list[Movie]:
        """Get all movies from specified libraries."""
        movies = []
        for section in self.server.library.sections():
            if section.type != "movie":
                continue
            if library_names and section.title not in library_names:
                continue
            movies.extend(section.all())
        return movies

    def get_all_shows(self, library_names: list[str] | None = None) -> list[Show]:
        """Get all shows from specified libraries."""
        shows = []
        for section in self.server.library.sections():
            if section.type != "show":
                continue
            if library_names and section.title not in library_names:
                continue
            shows.extend(section.all())
        return shows

    def get_watched_movies(self, library_names: list[str] | None = None) -> list[Movie]:
        """Get all watched movies."""
        movies = []
        for section in self.server.library.sections():
            if section.type != "movie":
                continue
            if library_names and section.title not in library_names:
                continue
            movies.extend(section.search(unwatched=False))
        return movies

    def get_watched_episodes(self, library_names: list[str] | None = None) -> list[Episode]:
        """Get all watched episodes."""
        episodes = []
        for section in self.server.library.sections():
            if section.type != "show":
                continue
            if library_names and section.title not in library_names:
                continue
            # Get all episodes that are watched
            for show in section.all():
                for episode in show.episodes():
                    if episode.isWatched:
                        episodes.append(episode)
        return episodes

    def mark_watched(self, item: Movie | Episode) -> None:
        """Mark an item as watched."""
        item.markWatched()

    def mark_unwatched(self, item: Movie | Episode) -> None:
        """Mark an item as unwatched."""
        item.markUnwatched()

    def set_rating(self, item: Movie | Show | Episode, rating: float) -> None:
        """Set rating for an item (1-10 scale)."""
        item.rate(rating)


def extract_plex_ids(item: Movie | Show | Episode) -> PlexIds:
    """Extract IDs from a Plex item."""
    plex_id = PlexIds(plex=str(item.ratingKey), guid=item.guid)

    # Parse GUIDs for external IDs
    for guid in getattr(item, "guids", []):
        guid_str = str(guid.id)
        if guid_str.startswith("imdb://"):
            plex_id.imdb = guid_str.replace("imdb://", "")
        elif guid_str.startswith("tmdb://"):
            try:
                plex_id.tmdb = int(guid_str.replace("tmdb://", ""))
            except ValueError:
                pass
        elif guid_str.startswith("tvdb://"):
            try:
                plex_id.tvdb = int(guid_str.replace("tvdb://", ""))
            except ValueError:
                pass

    return plex_id


def plex_movie_to_media_item(movie: Movie) -> MediaItem:
    """Convert Plex movie to MediaItem."""
    plex_ids = extract_plex_ids(movie)

    return MediaItem(
        title=movie.title,
        year=movie.year,
        media_type=MediaType.MOVIE,
        plex_ids=plex_ids,
        watched=movie.isWatched,
        watched_at=movie.lastViewedAt,
        plays=movie.viewCount or 0,
        rating=int(movie.userRating) if movie.userRating else None,
    )


def plex_episode_to_media_item(episode: Episode) -> MediaItem:
    """Convert Plex episode to MediaItem."""
    plex_ids = extract_plex_ids(episode)

    return MediaItem(
        title=episode.title,
        year=episode.year,
        media_type=MediaType.EPISODE,
        plex_ids=plex_ids,
        watched=episode.isWatched,
        watched_at=episode.lastViewedAt,
        plays=episode.viewCount or 0,
        rating=int(episode.userRating) if episode.userRating else None,
        show_title=episode.grandparentTitle,
        season=episode.seasonNumber,
        episode=episode.episodeNumber,
    )
