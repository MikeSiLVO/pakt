"""Data models for Pakt."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PlexIds(BaseModel):
    """External IDs parsed off a Plex item's GUIDs."""

    plex: str  # rating key
    guid: str | None = None
    imdb: str | None = None
    tmdb: int | None = None
    tvdb: int | None = None


class SyncResult(BaseModel):
    """Result of a sync operation."""

    added_to_trakt: int = 0
    added_to_plex: int = 0
    ratings_synced: int = 0
    collection_added: int = 0
    collection_updated: int = 0
    watchlist_added_trakt: int = 0
    watchlist_added_plex: int = 0
    errors: list[str] = Field(default_factory=list)
    skipped: int = 0
    duration_seconds: float = 0.0


class WatchedItem(BaseModel):
    """Item from Trakt watched endpoint."""

    plays: int = 0
    last_watched_at: datetime | None = None
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
