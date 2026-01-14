"""Configuration management for Pakt."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def get_config_dir() -> Path:
    """Get the configuration directory."""
    config_dir = Path.home() / ".config" / "pakt"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_cache_dir() -> Path:
    """Get the cache directory."""
    cache_dir = Path.home() / ".cache" / "pakt"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


class TraktConfig(BaseSettings):
    """Trakt API configuration."""

    model_config = SettingsConfigDict(env_prefix="TRAKT_")

    client_id: str = ""
    client_secret: str = ""
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

    # Sync directions
    watched_plex_to_trakt: bool = True
    watched_trakt_to_plex: bool = True
    ratings_plex_to_trakt: bool = True
    ratings_trakt_to_plex: bool = True

    # Rating priority when both have ratings
    rating_priority: Literal["plex", "trakt", "newest"] = "newest"

    # Libraries to sync (empty = all)
    movie_libraries: list[str] = Field(default_factory=list)
    show_libraries: list[str] = Field(default_factory=list)

    # Libraries to exclude
    excluded_libraries: list[str] = Field(default_factory=list)


class CacheConfig(BaseSettings):
    """Cache configuration."""

    model_config = SettingsConfigDict(env_prefix="PAKT_CACHE_")

    # Cache expiration in seconds
    trakt_ids_ttl: int = 365 * 24 * 3600  # 1 year - IDs don't change
    watched_ttl: int = 24 * 3600  # 1 day
    ratings_ttl: int = 24 * 3600  # 1 day
    plex_guid_ttl: int = 30 * 24 * 3600  # 30 days


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
    cache: CacheConfig = Field(default_factory=CacheConfig)

    @classmethod
    def load(cls, config_dir: Path | None = None) -> Config:
        """Load configuration from file and environment."""
        if config_dir is None:
            config_dir = get_config_dir()

        env_file = config_dir / ".env"
        if env_file.exists():
            return cls(_env_file=env_file)
        return cls()

    def save(self, config_dir: Path | None = None) -> None:
        """Save configuration to file."""
        if config_dir is None:
            config_dir = get_config_dir()

        env_file = config_dir / ".env"
        lines = [
            f"TRAKT_CLIENT_ID={self.trakt.client_id}",
            f"TRAKT_CLIENT_SECRET={self.trakt.client_secret}",
            f"TRAKT_ACCESS_TOKEN={self.trakt.access_token}",
            f"TRAKT_REFRESH_TOKEN={self.trakt.refresh_token}",
            f"TRAKT_EXPIRES_AT={self.trakt.expires_at}",
            f"PLEX_URL={self.plex.url}",
            f"PLEX_TOKEN={self.plex.token}",
            f"PLEX_SERVER_NAME={self.plex.server_name}",
        ]
        env_file.write_text("\n".join(lines))
