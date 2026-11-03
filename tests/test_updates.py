import os
import time

from pakt import __version__, updates
from pakt.config import Config, get_config_dir

STALE = updates.CHECK_INTERVAL + 1


def save_latest(version: str, age: float = 0) -> None:
    """Save a check's answer as if it were made the given seconds ago."""
    path = get_config_dir() / updates.LATEST_FILE
    path.write_text(version, encoding="utf-8")
    then = time.time() - age
    os.utime(path, (then, then))


def fake_pypi(monkeypatch, version: str) -> list[bool]:
    """Make PyPI answer with a version, or fail when it is empty; returns the calls made."""
    calls: list[bool] = []

    def fetch() -> str:
        """Record the call and answer."""
        calls.append(True)
        return version

    monkeypatch.setattr(updates, "_fetch_latest", fetch)
    return calls


def test_newer_release_is_reported():
    """A saved release above the running version is offered."""
    save_latest("99.0.0")
    assert updates.newer_version(Config()) == "99.0.0"


def test_same_or_older_release_is_not_reported():
    """The running version and anything older are not offered."""
    for version in (__version__, "0.0.1"):
        save_latest(version)
        assert updates.newer_version(Config()) is None


def test_refresh_asks_pypi_at_most_once_a_day(monkeypatch):
    """A second refresh the same day reuses the answer; a day later PyPI is asked again."""
    calls = fake_pypi(monkeypatch, "99.0.0")
    updates.refresh(Config())
    updates.refresh(Config())
    assert len(calls) == 1 and updates.newer_version(Config()) == "99.0.0"
    save_latest("99.0.0", age=STALE)
    updates.refresh(Config())
    assert len(calls) == 2


def test_failed_check_keeps_the_last_answer_and_waits_a_day(monkeypatch):
    """When PyPI can't be reached the last answer stays, and the next try is a day away."""
    save_latest("99.0.0", age=STALE)
    calls = fake_pypi(monkeypatch, "")
    updates.refresh(Config())
    updates.refresh(Config())
    assert len(calls) == 1 and updates.newer_version(Config()) == "99.0.0"


def test_turning_the_check_off_stops_it(monkeypatch):
    """With check_updates off, PyPI is never asked and a saved answer is not offered."""
    save_latest("99.0.0", age=STALE)
    calls = fake_pypi(monkeypatch, "99.0.0")
    config = Config(check_updates=False)
    updates.refresh(config)
    assert calls == [] and updates.newer_version(config) is None
