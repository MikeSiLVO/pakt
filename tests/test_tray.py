import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

# default Linux backends open a display on import, and CI runs headless
os.environ["PYSTRAY_BACKEND"] = "dummy"
pytest.importorskip("pystray")

from pakt import tray  # noqa: E402


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="time zones are switched with tzset")
def test_next_sync_label_is_in_local_time(monkeypatch):
    """The scheduler's UTC run time shows in the machine's own time zone."""
    next_run = datetime(2026, 11, 17, 6, 19, tzinfo=timezone.utc)
    paktray = tray.PaktTray(scheduler_getter=lambda: SimpleNamespace(next_run=next_run))
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        assert paktray._next_sync_text(None) == "Next sync: 01:19"
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_menu_is_rebuilt_only_when_a_label_changes(monkeypatch):
    """The tray menu is rebuilt each time what it shows changes, and not on the checks between."""
    monkeypatch.setattr(tray, "MENU_CHECK_SECONDS", 0)
    paktray = tray.PaktTray()
    shown = [("", False), ("", False), ("Next sync: 06:00", True), ("Next sync: 06:00", True)]

    def labels():
        """Hand out the next labels, stopping the watch at the last."""
        if len(shown) == 1:
            paktray._stopped.set()
        return shown.pop(0)

    monkeypatch.setattr(paktray, "_labels", labels)
    rebuilds = []
    paktray._watch_menu(SimpleNamespace(update_menu=lambda: rebuilds.append(True)))
    assert len(rebuilds) == 2
