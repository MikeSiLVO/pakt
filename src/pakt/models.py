"""Data models for Pakt."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MediaType(str, Enum):
    MOVIE = "movie"
    SHOW = "show"
    EPISODE = "episode"


class TraktIds(BaseModel):
    """Trakt ID container."""

    trakt: int | None = None
    slug: str | None = None
    imdb: str | None = None
    tmdb: int | None = None
    tvdb: int | None = None


class PlexIds(BaseModel):
    """Plex ID container."""

    plex: str  # rating key
    guid: str | None = None
    imdb: str | None = None
    tmdb: int | None = None
    tvdb: int | None = None


class MediaItem(BaseModel):
    """Unified media item representation."""

    title: str
    year: int | None = None
    media_type: MediaType
    trakt_ids: TraktIds | None = None
    plex_ids: PlexIds | None = None

    # Watch status
    watched: bool = False
    watched_at: datetime | None = None
    plays: int = 0

    # Rating
    rating: int | None = None  # 1-10
    rated_at: datetime | None = None

    # Episode-specific
    show_title: str | None = None
    season: int | None = None
    episode: int | None = None

    @property
    def trakt_id(self) -> int | None:
        return self.trakt_ids.trakt if self.trakt_ids else None

    @property
    def plex_key(self) -> str | None:
        return self.plex_ids.plex if self.plex_ids else None

    def __hash__(self) -> int:
        if self.trakt_ids and self.trakt_ids.trakt:
            return hash(("trakt", self.trakt_ids.trakt))
        if self.plex_ids:
            return hash(("plex", self.plex_ids.plex))
        return hash((self.title, self.year))


class SyncResult(BaseModel):
    """Result of a sync operation."""

    added_to_trakt: int = 0
    added_to_plex: int = 0
    ratings_synced: int = 0
    collection_added: int = 0
    watchlist_added_trakt: int = 0
    watchlist_added_plex: int = 0
    errors: list[str] = Field(default_factory=list)
    skipped: int = 0
    duration_seconds: float = 0.0


class WatchedItem(BaseModel):
    """Item from Trakt watched endpoint."""

    plays: int
    last_watched_at: datetime
    last_updated_at: datetime | None = None
    movie: dict[str, Any] | None = None
    show: dict[str, Any] | None = None
    seasons: list[dict[str, Any]] | None = None


class RatedItem(BaseModel):
    """Item from Trakt ratings endpoint."""

    rated_at: datetime
    rating: int
    movie: dict[str, Any] | None = None
    show: dict[str, Any] | None = None
    episode: dict[str, Any] | None = None
