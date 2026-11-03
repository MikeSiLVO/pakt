from types import SimpleNamespace

import pytest

pytest.importorskip("pystray")

from pakt import tray  # noqa: E402


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
