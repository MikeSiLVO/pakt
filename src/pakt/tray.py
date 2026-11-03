"""System tray integration for Pakt."""
# pyright: reportPossiblyUnboundVariable=false

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from pakt import updates

if TYPE_CHECKING:
    from pakt.scheduler import SyncScheduler

try:
    import pystray
    from PIL import Image

    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

MENU_CHECK_SECONDS = 60


def _get_icon_image() -> "Image.Image":
    """Get the tray icon from assets, or draw a plain one."""
    if not TRAY_AVAILABLE:
        raise ImportError("PIL is required")

    assets_dir = Path(__file__).parent / "assets"
    icon_path = assets_dir / "icon.png"
    if icon_path.exists():
        return Image.open(icon_path)

    size = 64
    img = Image.new("RGBA", (size, size), (45, 212, 191, 255))
    return img


class PaktTray:
    """Tray icon; it looks up the scheduler and newer release on demand, as both come later."""

    def __init__(
        self,
        web_url: str = "http://localhost:8080",
        sync_callback: Callable[[], None] | None = None,
        shutdown_callback: Callable[[], None] | None = None,
        scheduler_getter: Callable[[], "SyncScheduler | None"] | None = None,
        update_getter: Callable[[], str | None] | None = None,
    ) -> None:
        if not TRAY_AVAILABLE:
            raise ImportError("pystray and Pillow are required for system tray support")

        self.web_url = web_url
        self.sync_callback = sync_callback
        self.shutdown_callback = shutdown_callback
        self.scheduler_getter = scheduler_getter
        self.update_getter = update_getter
        # pystray is an optional import, so it cannot be named in a type expression
        self._icon: Any = None
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()

    def _open_web_ui(self) -> None:
        """Open the web UI in the default browser."""
        webbrowser.open(self.web_url)

    def _trigger_sync(self) -> None:
        """Trigger a sync through the callback the app supplied."""
        if self.sync_callback:
            self.sync_callback()

    def _exit(self) -> None:
        """Exit the application: remove the icon, then run the shutdown callback."""
        if self._icon:
            self._icon.stop()
        if self.shutdown_callback:
            self.shutdown_callback()

    def _scheduler(self) -> "SyncScheduler | None":
        """Current scheduler, or None if one was never started."""
        return self.scheduler_getter() if self.scheduler_getter else None

    def _next_sync_text(self, _item: Any) -> str:
        """Menu label showing when the next scheduled sync lands."""
        scheduler = self._scheduler()
        next_run = scheduler.next_run if scheduler else None
        return f"Next sync: {next_run:%H:%M}" if next_run else ""

    def _has_next_sync(self, _item: Any) -> bool:
        """Whether there is a scheduled run worth showing."""
        scheduler = self._scheduler()
        return bool(scheduler and scheduler.is_enabled and scheduler.next_run)

    def _newer_version(self) -> str | None:
        """Newer pakt release the update check found, or None."""
        return self.update_getter() if self.update_getter else None

    def _update_text(self, _item: Any) -> str:
        """Menu label naming the newer release."""
        newer = self._newer_version()
        return f"Update available: {newer}" if newer else ""

    def _has_update(self, _item: Any) -> bool:
        """Whether there is a newer release worth showing."""
        return self._newer_version() is not None

    def _open_release(self) -> None:
        """Open the newer release's notes in the default browser."""
        newer = self._newer_version()
        if newer:
            webbrowser.open(updates.release_url(newer))

    def _labels(self) -> tuple[str, bool, str]:
        """What the menu's changing items show, compared to tell when it needs rebuilding."""
        return self._next_sync_text(None), self._has_next_sync(None), self._update_text(None)

    def _watch_menu(self, icon: Any) -> None:
        """Watch the labels and rebuild the menu on a change, as Windows builds it once."""
        shown = None
        while not self._stopped.wait(MENU_CHECK_SECONDS):
            labels = self._labels()
            if labels != shown:
                shown = labels
                icon.update_menu()

    def _get_menu(self) -> "pystray.Menu":
        """Get the context menu; labels are callables, built before the scheduler exists."""
        return pystray.Menu(
            pystray.MenuItem("Open Web UI", self._open_web_ui, default=True),
            pystray.MenuItem("Sync Now", self._trigger_sync),
            pystray.Menu.SEPARATOR,
            # pystray wraps callables for text/visible, but ships no types so they look like bool
            pystray.MenuItem(
                self._next_sync_text,
                None,
                enabled=False,
                visible=self._has_next_sync,  # type: ignore[arg-type]
            ),
            pystray.MenuItem(self._update_text, self._open_release,
                             visible=self._has_update),  # type: ignore[arg-type]
            pystray.MenuItem("Exit", self._exit),
        )

    def start(self) -> None:
        """Start the system tray icon and its menu watcher in background threads."""
        if not TRAY_AVAILABLE:
            return

        icon_image = _get_icon_image()
        icon = pystray.Icon(
            name="Pakt",
            icon=icon_image,
            title="Pakt - Plex/Trakt Sync",
            menu=self._get_menu(),
        )
        self._icon = icon

        def run_icon():
            """pystray blocks, so it owns this thread."""
            icon.run()

        self._thread = threading.Thread(target=run_icon, daemon=True)
        self._thread.start()
        threading.Thread(target=self._watch_menu, args=(icon,), daemon=True).start()

    def stop(self) -> None:
        """Stop the system tray icon and its menu watcher."""
        self._stopped.set()
        if self._icon:
            self._icon.stop()
            self._icon = None
