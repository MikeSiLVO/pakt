"""Running the web UI: uvicorn, the optional tray icon, and a Ctrl+C that cancels a sync first."""

from __future__ import annotations

import logging
import os
import socket
import sys
import time
from typing import TYPE_CHECKING

from pakt.log import console
from pakt.web import state

if TYPE_CHECKING:
    from pakt.tray import PaktTray

logger = logging.getLogger(__name__)

TRAY_MISSING = 'The system tray needs pystray and Pillow: pip install "pakt[tray]"'


def _port_in_use(host: str, port: int) -> bool:
    """Whether another process already listens on the address."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        sock.close()
    except OSError:
        return True
    return False


def _start_tray(web_url: str) -> PaktTray | None:
    """Start the tray icon for the web UI; None when its packages are missing or it fails."""
    try:
        from pakt.tray import TRAY_AVAILABLE, PaktTray

        if not TRAY_AVAILABLE:
            logger.warning(TRAY_MISSING)
            return None

        def on_exit():
            """Hard exit from the tray menu; uvicorn is on another thread."""
            logger.info("Quit from the tray")
            os._exit(0)

        def trigger_sync():
            """Start a sync over the local API, since the tray runs off the event loop."""
            import httpx

            try:
                httpx.post(f"{web_url}/api/sync", json={}, timeout=10.0)
            except Exception as e:
                logger.warning(f"The tray could not start a sync: {e}")

        tray = PaktTray(
            web_url=web_url,
            sync_callback=trigger_sync,
            shutdown_callback=on_exit,
            scheduler_getter=lambda: state.scheduler,
        )
        tray.start()
        time.sleep(0.5)
        return tray
    except ImportError:
        logger.warning(TRAY_MISSING)
    except Exception as e:
        logger.exception(f"The tray failed to start: {type(e).__name__}: {e}")
    return None


def run_web_ui(host: str, port: int, tray: bool) -> None:
    """Run the web UI until it stops; with the tray it runs silent, as pythonw has no console."""
    import asyncio

    import uvicorn

    from pakt.web.app import create_app

    local_url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::', '') else host}:{port}"
    port_in_use = _port_in_use(host, port)

    devnull = None
    if tray:
        try:
            devnull = open(os.devnull, 'w')
            sys.stdout = devnull
            sys.stderr = devnull
        except Exception:
            pass

    tray_icon = _start_tray(local_url) if tray else None

    # once the tray is up, the conflict can show as a notification
    if port_in_use:
        logger.error(f"Port {port} is already in use; another Pakt instance may be running")
        if tray_icon and tray_icon._icon:
            try:
                tray_icon._icon.notify(f"Port {port} already in use.\nAnother instance may be running.", "Pakt")
            except Exception as e:
                logger.warning(f"Tray notification failed: {e}")
            time.sleep(3)
        return

    logger.info(f"Web UI starting at {local_url}")
    if not tray:
        console.print("[dim]Open it in your browser. Press Ctrl+C to stop.[/]\n")

    sync_state = state.sync_state

    class PaktServer(uvicorn.Server):
        """Server whose first Ctrl+C cancels a running sync instead of cutting it mid-write."""

        def handle_exit(self, sig, frame) -> None:
            """Cancel a running sync on the first signal, shut down on the next."""
            if sync_state["running"] and not sync_state["cancelled"]:
                sync_state["cancelled"] = True
                if not tray:
                    console.print("\n[yellow]Cancelling sync... (Ctrl+C again to quit)[/]")
                return
            if not tray:
                console.print("\n[dim]Shutting down...[/]")
            super().handle_exit(sig, frame)

    try:
        server = PaktServer(uvicorn.Config(create_app(), host=host, port=port,
                                           log_level="critical" if tray else "warning"))
        if tray_icon and tray_icon._icon:
            try:
                tray_icon._icon.notify(f"Running on {local_url}", "Pakt")
            except Exception as e:
                logger.warning(f"Tray notification failed: {e}")
        asyncio.run(server.serve())
    except Exception as e:
        logger.exception(f"Web UI failed: {type(e).__name__}: {e}")
        raise
    finally:
        logger.info("Web UI stopped")
        if devnull:
            devnull.close()
        if tray_icon:
            tray_icon.stop()
