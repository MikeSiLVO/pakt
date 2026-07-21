"""System tray integration for Pakt."""
# pyright: reportPossiblyUnboundVariable=false

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from pakt.scheduler import SyncScheduler

try:
    import pystray
    from PIL import Image

    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False


def _get_icon_image() -> "Image.Image":
    """Load icon from assets or create fallback."""
    if not TRAY_AVAILABLE:
        raise ImportError("PIL is required")

    # Try to load from assets
    assets_dir = Path(__file__).parent / "assets"
    icon_path = assets_dir / "icon.png"
    if icon_path.exists():
        return Image.open(icon_path)

    # Fallback: create simple programmatic icon
    size = 64
    img = Image.new("RGBA", (size, size), (45, 212, 191, 255))  # Teal background
    return img


class PaktTray:
    """System tray icon for Pakt."""

    def __init__(
        self,
        web_url: str = "http://localhost:8080",
        sync_callback: Callable[[], None] | None = None,
        shutdown_callback: Callable[[], None] | None = None,
        scheduler_getter: Callable[[], "SyncScheduler | None"] | None = None,
    ) -> None:
        """Initialize the system tray.

        Args:
            web_url: URL of the web interface
            sync_callback: Function to trigger sync
            shutdown_callback: Function to shutdown the application
            scheduler_getter: Looks up the scheduler on demand, since it starts after the tray
        """
        if not TRAY_AVAILABLE:
            raise ImportError("pystray and Pillow are required for system tray support")

        self.web_url = web_url
        self.sync_callback = sync_callback
        self.shutdown_callback = shutdown_callback
        self.scheduler_getter = scheduler_getter
        # pystray is an optional import, so it cannot be named in a type expression
        self._icon: Any = None
        self._thread: threading.Thread | None = None

    def _open_web_ui(self) -> None:
        """Open the web UI in the default browser."""
        webbrowser.open(self.web_url)

    def _trigger_sync(self) -> None:
        """Trigger a sync operation."""
        if self.sync_callback:
            self.sync_callback()

    def _exit(self) -> None:
        """Exit the application."""
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

    def _get_menu(self) -> "pystray.Menu":
        """Create the context menu.

        Label and visibility are callables because the menu is built once at
        startup, before the scheduler exists.
        """
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
            pystray.MenuItem("Exit", self._exit),
        )

    def start(self) -> None:
        """Start the system tray icon in a background thread."""
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

    def stop(self) -> None:
        """Stop the system tray icon."""
        if self._icon:
            self._icon.stop()
            self._icon = None

    def update_menu(self) -> None:
        """Update the menu (e.g., after scheduler status changes)."""
        if self._icon:
            self._icon.menu = self._get_menu()
