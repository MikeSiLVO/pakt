"""State shared across web requests: the running sync, the scheduler and short-lived caches."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Callable

from pakt.config import Config

if TYPE_CHECKING:
    from pakt.scheduler import SyncScheduler

sync_state: dict[str, Any] = {
    "running": False,
    "cancelled": False,
    "last_run": None,
    "last_result": None,
    "logs": [],
}

scheduler: SyncScheduler | None = None

# Trakt account and Plex library lookups are slow to repeat on every page load
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 300
CONFIG_CACHE_TTL = 2  # one page load reads config many times

_config_cache: tuple[float, Config | None] = (0, None)


def get_cached(key: str) -> Any | None:
    """Get cached value if not expired."""
    if key in _cache:
        ts, value = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return value
    return None


def set_cached(key: str, value: Any) -> None:
    """Cache a value with the current time, for get_cached to expire."""
    _cache[key] = (time.time(), value)


def drop_cached(key: str) -> None:
    """Drop a cached value; the next read fetches it fresh."""
    _cache.pop(key, None)


def load_config_cached() -> Config:
    """Load config, reusing the copy read within CONFIG_CACHE_TTL."""
    global _config_cache
    ts, config = _config_cache
    if config is not None and time.time() - ts < CONFIG_CACHE_TTL:
        return config
    config = Config.load()
    _config_cache = (time.time(), config)
    return config


def invalidate_config_cache() -> None:
    """Invalidate config cache after saves."""
    global _config_cache
    _config_cache = (0, None)


def token_saver(config: Config) -> Callable[[dict], None]:
    """Build a callback that persists rotated Trakt tokens and drops the cached config."""
    def save(token: dict) -> None:
        config.save_trakt_token(token)
        invalidate_config_cache()
    return save
