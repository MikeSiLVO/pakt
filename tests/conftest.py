"""Shared fixtures for the Pakt tests."""

import pytest


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    """Give every test an empty home of its own for the config folder."""
    for name in ("HOME", "USERPROFILE", "APPDATA"):
        monkeypatch.setenv(name, str(tmp_path))
    return tmp_path
