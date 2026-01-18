"""Configuration management for Pakt."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def get_config_dir() -> Path:
    """Get the configuration directory (platform-appropriate)."""
    if os.name == "nt":  # Windows
        base = Path(os.environ.get("APPDATA", Path.home()))
        config_dir = base / "pakt"
    else:  # Linux/macOS
        config_dir = Path.home() / ".config" / "pakt"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_cache_dir() -> Path:
    """Get the cache directory (platform-appropriate)."""
    if os.name == "nt":  # Windows
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        cache_dir = base / "pakt" / "cache"
    else:  # Linux/macOS
        cache_dir = Path.home() / ".cache" / "pakt"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


class TraktConfig(BaseSettings):
    """Trakt API configuration."""

    model_config = SettingsConfigDict(env_prefix="TRAKT_")

    # Default app credentials - users get their own rate limits after auth
    client_id: str = "bc25ba5024e871104e3a090b98e44895670d66d89d5296fa6ed027d6e2a44f9d"
    client_secret: str = "0a3cb10a368e848ef67ebcbdb0f64b437ee5abf17e07e4f216c9aa7346f587f5"
    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0


class PlexConfig(BaseSettings):
    """Plex configuration."""

    model_config = SettingsConfigDict(env_prefix="PLEX_")

    url: str = ""
    token: str = ""
    server_name: str = ""


class SyncConfig(BaseSettings):
    """Sync behavior configuration."""

    model_config = SettingsConfigDict(env_prefix="PAKT_SYNC_")

    # Sync directions - watched
    watched_plex_to_trakt: bool = True
    watched_trakt_to_plex: bool = True

    # Sync directions - ratings
    ratings_plex_to_trakt: bool = True
    ratings_trakt_to_plex: bool = True

    # Sync directions - collection (Plex library -> Trakt collection)
    collection_plex_to_trakt: bool = False

    # Sync directions - watchlist
    watchlist_plex_to_trakt: bool = False
    watchlist_trakt_to_plex: bool = False

    # Rating priority when both have ratings
    rating_priority: Literal["plex", "trakt", "newest"] = "newest"

    # Libraries to sync (empty = all)
    movie_libraries: list[str] = Field(default_factory=list)
    show_libraries: list[str] = Field(default_factory=list)

    # Libraries to exclude
    excluded_libraries: list[str] = Field(default_factory=list)

    @field_validator("movie_libraries", "show_libraries", "excluded_libraries", mode="before")
    @classmethod
    def parse_library_list(cls, v: str | list[str]) -> list[str]:
        """Parse library list from env var (JSON array or comma-separated)."""
        if isinstance(v, list):
            return v
        if not v or (isinstance(v, str) and v.strip() == ""):
            return []
        # Try JSON first (new format), fall back to comma-separated (legacy)
        v_str = v.strip()
        if v_str.startswith("["):
            try:
                parsed = json.loads(v_str)
                return parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                pass
        return [lib.strip() for lib in v.split(",") if lib.strip()]


class SchedulerConfig(BaseSettings):
    """Scheduler configuration."""

    model_config = SettingsConfigDict(env_prefix="PAKT_SCHEDULER_")

    enabled: bool = False
    interval_hours: int = 0  # 0 = disabled
    run_on_startup: bool = False


class Config(BaseSettings):
    """Main configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    trakt: TraktConfig = Field(default_factory=TraktConfig)
    plex: PlexConfig = Field(default_factory=PlexConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)

    @classmethod
    def load(cls, config_dir: Path | None = None) -> Config:
        """Load configuration from file and environment."""
        if config_dir is None:
            config_dir = get_config_dir()

        # Fields that need empty string -> [] conversion for pydantic-settings
        list_fields = {
            "PAKT_SYNC_MOVIE_LIBRARIES",
            "PAKT_SYNC_SHOW_LIBRARIES",
            "PAKT_SYNC_EXCLUDED_LIBRARIES",
        }

        # Fix any existing empty list env vars (pydantic-settings needs valid JSON)
        for key in list_fields:
            if key in os.environ and os.environ[key].strip() == "":
                os.environ[key] = "[]"

        env_file = config_dir / ".env"
        if env_file.exists():
            # Load env vars into os.environ so nested configs pick them up
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    # Convert empty strings to JSON empty array for list fields
                    if key in list_fields and value.strip() == "":
                        value = "[]"
                    os.environ[key] = value

        return cls()

    def save(self, config_dir: Path | None = None) -> None:
        """Save configuration to file."""
        if config_dir is None:
            config_dir = get_config_dir()

        env_file = config_dir / ".env"
        lines = [
            # Trakt
            f"TRAKT_CLIENT_ID={self.trakt.client_id}",
            f"TRAKT_CLIENT_SECRET={self.trakt.client_secret}",
            f"TRAKT_ACCESS_TOKEN={self.trakt.access_token}",
            f"TRAKT_REFRESH_TOKEN={self.trakt.refresh_token}",
            f"TRAKT_EXPIRES_AT={self.trakt.expires_at}",
            # Plex
            f"PLEX_URL={self.plex.url}",
            f"PLEX_TOKEN={self.plex.token}",
            f"PLEX_SERVER_NAME={self.plex.server_name}",
            # Sync - watched
            f"PAKT_SYNC_WATCHED_PLEX_TO_TRAKT={str(self.sync.watched_plex_to_trakt).lower()}",
            f"PAKT_SYNC_WATCHED_TRAKT_TO_PLEX={str(self.sync.watched_trakt_to_plex).lower()}",
            # Sync - ratings
            f"PAKT_SYNC_RATINGS_PLEX_TO_TRAKT={str(self.sync.ratings_plex_to_trakt).lower()}",
            f"PAKT_SYNC_RATINGS_TRAKT_TO_PLEX={str(self.sync.ratings_trakt_to_plex).lower()}",
            # Sync - collection
            f"PAKT_SYNC_COLLECTION_PLEX_TO_TRAKT={str(self.sync.collection_plex_to_trakt).lower()}",
            # Sync - watchlist
            f"PAKT_SYNC_WATCHLIST_PLEX_TO_TRAKT={str(self.sync.watchlist_plex_to_trakt).lower()}",
            f"PAKT_SYNC_WATCHLIST_TRAKT_TO_PLEX={str(self.sync.watchlist_trakt_to_plex).lower()}",
            # Sync - libraries (JSON format for pydantic-settings)
            f"PAKT_SYNC_MOVIE_LIBRARIES={json.dumps(self.sync.movie_libraries)}",
            f"PAKT_SYNC_SHOW_LIBRARIES={json.dumps(self.sync.show_libraries)}",
            # Scheduler
            f"PAKT_SCHEDULER_ENABLED={str(self.scheduler.enabled).lower()}",
            f"PAKT_SCHEDULER_INTERVAL_HOURS={self.scheduler.interval_hours}",
            f"PAKT_SCHEDULER_RUN_ON_STARTUP={str(self.scheduler.run_on_startup).lower()}",
        ]
        env_file.write_text("\n".join(lines))
