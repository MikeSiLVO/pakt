"""Configuration management for Pakt."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


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


class TraktConfig(BaseModel):
    """Trakt API configuration."""

    client_id: str = "bc25ba5024e871104e3a090b98e44895670d66d89d5296fa6ed027d6e2a44f9d"
    client_secret: str = "0a3cb10a368e848ef67ebcbdb0f64b437ee5abf17e07e4f216c9aa7346f587f5"
    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0


class SyncConfig(BaseModel):
    """Sync behavior configuration."""

    watched_plex_to_trakt: bool = True
    watched_trakt_to_plex: bool = True
    ratings_plex_to_trakt: bool = True
    ratings_trakt_to_plex: bool = True
    collection_plex_to_trakt: bool = False
    watchlist_plex_to_trakt: bool = False
    watchlist_trakt_to_plex: bool = False
    rating_priority: Literal["plex", "trakt", "newest"] = "newest"


class SchedulerConfig(BaseModel):
    """Scheduler configuration."""

    enabled: bool = False
    interval_hours: int = 0
    run_on_startup: bool = False


class ServerSyncOverrides(BaseModel):
    """Per-server sync option overrides. None = use global setting."""

    watched_plex_to_trakt: bool | None = None
    watched_trakt_to_plex: bool | None = None
    ratings_plex_to_trakt: bool | None = None
    ratings_trakt_to_plex: bool | None = None
    collection_plex_to_trakt: bool | None = None
    watchlist_plex_to_trakt: bool | None = None
    watchlist_trakt_to_plex: bool | None = None


class ServerConfig(BaseModel):
    """Configuration for a single Plex server."""

    name: str
    url: str = ""
    token: str = ""
    server_name: str = ""
    enabled: bool = True
    movie_libraries: list[str] = Field(default_factory=list)
    show_libraries: list[str] = Field(default_factory=list)
    excluded_libraries: list[str] = Field(default_factory=list)
    sync: ServerSyncOverrides | None = None

    def get_sync_option(self, option: str, global_config: SyncConfig) -> bool:
        """Get effective sync option (server override or global fallback)."""
        if self.sync:
            override = getattr(self.sync, option, None)
            if override is not None:
                return override
        return getattr(global_config, option)


class Config(BaseModel):
    """Unified configuration stored in config.json."""

    trakt: TraktConfig = Field(default_factory=TraktConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    plex_token: str = ""
    servers: list[ServerConfig] = Field(default_factory=list)

    def get_server(self, name: str) -> ServerConfig | None:
        """Get server by name."""
        for server in self.servers:
            if server.name == name:
                return server
        return None

    def get_enabled_servers(self) -> list[ServerConfig]:
        """Get all enabled servers."""
        return [s for s in self.servers if s.enabled]

    @classmethod
    def load(cls, config_dir: Path | None = None) -> Config:
        """Load configuration from config.json, with migration from legacy formats."""
        if config_dir is None:
            config_dir = get_config_dir()

        config_file = config_dir / "config.json"

        if config_file.exists():
            try:
                data = json.loads(config_file.read_text(encoding="utf-8"))
                return cls(**data)
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"Failed to load config.json: {e}")
                return cls()

        # Migration: check for legacy .env and servers.json
        config = _migrate_legacy_config(config_dir)
        if config:
            config.save(config_dir)
            return config

        return cls()

    def save(self, config_dir: Path | None = None) -> None:
        """Save configuration to config.json."""
        if config_dir is None:
            config_dir = get_config_dir()

        config_file = config_dir / "config.json"
        config_file.write_text(
            self.model_dump_json(indent=2),
            encoding="utf-8",
        )


def _migrate_legacy_config(config_dir: Path) -> Config | None:
    """Migrate from legacy .env to config.json."""
    env_file = config_dir / ".env"

    if not env_file.exists():
        return None

    logger.info("Migrating .env to config.json")

    config = Config()
    env_vars = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env_vars[key] = value

    # Trakt config
    config.trakt.client_id = env_vars.get("TRAKT_CLIENT_ID", config.trakt.client_id)
    config.trakt.client_secret = env_vars.get("TRAKT_CLIENT_SECRET", config.trakt.client_secret)
    config.trakt.access_token = env_vars.get("TRAKT_ACCESS_TOKEN", "")
    config.trakt.refresh_token = env_vars.get("TRAKT_REFRESH_TOKEN", "")
    config.trakt.expires_at = int(env_vars.get("TRAKT_EXPIRES_AT", "0") or "0")

    # Sync config
    def parse_bool(val: str) -> bool:
        return val.lower() in ("true", "1", "yes")

    config.sync.watched_plex_to_trakt = parse_bool(env_vars.get("PAKT_SYNC_WATCHED_PLEX_TO_TRAKT", "true"))
    config.sync.watched_trakt_to_plex = parse_bool(env_vars.get("PAKT_SYNC_WATCHED_TRAKT_TO_PLEX", "true"))
    config.sync.ratings_plex_to_trakt = parse_bool(env_vars.get("PAKT_SYNC_RATINGS_PLEX_TO_TRAKT", "true"))
    config.sync.ratings_trakt_to_plex = parse_bool(env_vars.get("PAKT_SYNC_RATINGS_TRAKT_TO_PLEX", "true"))
    config.sync.collection_plex_to_trakt = parse_bool(env_vars.get("PAKT_SYNC_COLLECTION_PLEX_TO_TRAKT", "false"))
    config.sync.watchlist_plex_to_trakt = parse_bool(env_vars.get("PAKT_SYNC_WATCHLIST_PLEX_TO_TRAKT", "false"))
    config.sync.watchlist_trakt_to_plex = parse_bool(env_vars.get("PAKT_SYNC_WATCHLIST_TRAKT_TO_PLEX", "false"))

    # Scheduler config
    config.scheduler.enabled = parse_bool(env_vars.get("PAKT_SCHEDULER_ENABLED", "false"))
    config.scheduler.interval_hours = int(env_vars.get("PAKT_SCHEDULER_INTERVAL_HOURS", "0") or "0")
    config.scheduler.run_on_startup = parse_bool(env_vars.get("PAKT_SCHEDULER_RUN_ON_STARTUP", "false"))

    # Legacy Plex config - create a server if URL and token exist
    legacy_plex_url = env_vars.get("PLEX_URL", "")
    legacy_plex_token = env_vars.get("PLEX_TOKEN", "")
    legacy_server_name = env_vars.get("PLEX_SERVER_NAME", "")

    if legacy_plex_url and legacy_plex_token:
        config.plex_token = legacy_plex_token
        config.servers.append(
            ServerConfig(
                name="default",
                url=legacy_plex_url,
                token=legacy_plex_token,
                server_name=legacy_server_name,
                enabled=True,
            )
        )

    # Rename .env to .env.bak
    env_file.rename(config_dir / ".env.bak")
    logger.info("Renamed .env to .env.bak")

    logger.info("Migration complete: created config.json")
    return config
