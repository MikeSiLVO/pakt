"""Update check: whether PyPI has a newer pakt release than the one running."""

from __future__ import annotations

import time

import httpx
from packaging.version import InvalidVersion, Version

from pakt import __version__
from pakt.config import Config, get_config_dir

PYPI_URL = "https://pypi.org/pypi/pakt/json"
CHECK_INTERVAL = 24 * 60 * 60
LATEST_FILE = "latest_version"


def release_url(version: str) -> str:
    """Release notes page of a version on GitHub."""
    return f"https://github.com/MikeSiLVO/pakt/releases/tag/v{version}"


def _fetch_latest() -> str:
    """Fetch the newest release's version from PyPI, or an empty string when that fails."""
    try:
        response = httpx.get(PYPI_URL, headers={"User-Agent": f"pakt/{__version__}"}, timeout=5.0)
        response.raise_for_status()
        return str(response.json()["info"]["version"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return ""


def refresh(config: Config) -> None:
    """Refresh the saved newest release from PyPI once the last check is a day old."""
    if not config.check_updates:
        return
    try:
        path = get_config_dir() / LATEST_FILE
        if path.exists() and time.time() - path.stat().st_mtime < CHECK_INTERVAL:
            return
        latest = _fetch_latest()
        if latest:
            path.write_text(latest, encoding="utf-8")
        else:
            # a failure counts as a check, or every offline call waits out the timeout
            path.touch()
    except OSError:
        pass


def newer_version(config: Config) -> str | None:
    """Newest release from the last check if newer than this one, else None."""
    if not config.check_updates:
        return None
    try:
        latest = Version((get_config_dir() / LATEST_FILE).read_text(encoding="utf-8").strip())
        running = Version(__version__)
    except (OSError, InvalidVersion):
        return None
    return str(latest) if latest > running else None
