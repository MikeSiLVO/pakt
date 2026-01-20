"""System tray integration for Pakt."""
# pyright: reportPossiblyUnboundVariable=false

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    import pystray
    from PIL import Image

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
        scheduler: "SyncScheduler | None" = None,
    ) -> None:
        """Initialize the system tray.

        Args:
            web_url: URL of the web interface
            sync_callback: Function to trigger sync
            shutdown_callback: Function to shutdown the application
            scheduler: Scheduler instance for status display
        """
        if not TRAY_AVAILABLE:
            raise ImportError("pystray and Pillow are required for system tray support")

        self.web_url = web_url
        self.sync_callback = sync_callback
        self.shutdown_callback = shutdown_callback
        self.scheduler = scheduler
        self._icon: pystray.Icon | None = None
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

    def _get_menu(self) -> "pystray.Menu":
        """Create the context menu."""
        items = [
            pystray.MenuItem("Open Web UI", self._open_web_ui, default=True),
            pystray.MenuItem("Sync Now", self._trigger_sync),
            pystray.Menu.SEPARATOR,
        ]

        # Add next run info if scheduler is available
        if self.scheduler and self.scheduler.is_enabled:
            next_run = self.scheduler.next_run
            if next_run:
                next_str = next_run.strftime("%H:%M")
                items.append(
                    pystray.MenuItem(f"Next sync: {next_str}", None, enabled=False)
                )
                items.append(pystray.Menu.SEPARATOR)

        items.append(pystray.MenuItem("Exit", self._exit))
        return pystray.Menu(*items)

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
