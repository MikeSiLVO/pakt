"""Shared fixtures for the Pakt tests."""

import logging

import pytest


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    """Give every test an empty home of its own for the config folder."""
    for name in ("HOME", "USERPROFILE", "APPDATA"):
        monkeypatch.setenv(name, str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def detach_log_handlers():
    """Detach the log handlers a CLI test installs, returning records to pytest's capture."""
    yield
    logger = logging.getLogger("pakt")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True
