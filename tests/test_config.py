import json
import os
from datetime import datetime, timezone

import pytest

from pakt.config import Config, get_config_dir, get_last_sync, set_last_sync

posix_only = pytest.mark.skipif(os.name == "nt", reason="file modes are POSIX only")


@posix_only
def test_config_files_are_private():
    """The config folder and config.json are readable by the owner only."""
    Config().save()
    folder = get_config_dir()
    assert folder.stat().st_mode & 0o777 == 0o700
    assert (folder / "config.json").stat().st_mode & 0o777 == 0o600


def test_save_leaves_no_temp_file():
    """A save swaps the new file in and leaves nothing beside it."""
    Config().save()
    assert sorted(p.name for p in get_config_dir().iterdir()) == ["config.json"]


def test_unreadable_config_is_moved_aside():
    """A broken config.json is kept as config.json.corrupt instead of being overwritten."""
    (get_config_dir() / "config.json").write_text("{broken", encoding="utf-8")
    assert Config.load().servers == []
    assert (get_config_dir() / "config.json.corrupt").read_text(encoding="utf-8") == "{broken"


def test_old_client_secret_is_dropped():
    """A config written by an older Pakt loads, and the next save drops its client secret."""
    (get_config_dir() / "config.json").write_text(json.dumps({"trakt": {"client_secret": "x"}}), encoding="utf-8")
    Config.load().save()
    assert "client_secret" not in (get_config_dir() / "config.json").read_text(encoding="utf-8")


def test_token_save_keeps_settings_changed_meanwhile():
    """A refreshed token saved from an old copy keeps a setting saved after that copy was read."""
    held = Config.load()
    newer = Config.load()
    newer.sync.collection_plex_to_trakt = True
    newer.save()
    held.save_trakt_token({"access_token": "a", "refresh_token": "r", "created_at": 10, "expires_in": 5})
    saved = Config.load()
    assert saved.sync.collection_plex_to_trakt and saved.trakt.access_token == "a"
    assert saved.trakt.expires_at == 15


def test_last_sync_round_trip():
    """The last sync time reads back as written, and is None before any sync."""
    assert get_last_sync() is None
    when = datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)
    set_last_sync(when)
    assert get_last_sync() == when
