"""A sync run: every chosen server in turn, one process at a time, closed by a summary line."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
from collections.abc import Generator
from datetime import datetime, timezone
from typing import Callable

from pakt import __version__
from pakt.config import Config, get_config_dir, set_last_sync
from pakt.log import forward_log, server_prefix
from pakt.models import SyncResult
from pakt.plex import PlexAuthError, PlexClient
from pakt.sync import report
from pakt.sync.engine import PushedToTrakt, SyncEngine, TraktCache
from pakt.trakt import TraktAuthError, TraktClient

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def sync_lock() -> Generator[bool, None, None]:
    """Hold the config folder's sync lock, one process at a time; yields False when taken."""
    handle = open(get_config_dir() / "sync.lock", "a+")
    try:
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        handle.close()


async def run_multi_server_sync(
    config: Config,
    server_names: list[str] | None = None,
    dry_run: bool = False,
    verbose: bool = False,
    fix_collection_dates: bool = False,
    collection_only: bool = False,
    no_movies: bool = False,
    no_shows: bool = False,
    on_token_refresh: Callable[[dict], None] | None = None,
    log_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    started_by: str = "CLI",
) -> SyncResult:
    """Run sync across multiple Plex servers, refusing to start while another process is syncing."""
    with forward_log(log_callback):
        if server_names:
            servers_to_sync = []
            for name in server_names:
                server = config.get_server(name)
                if server:
                    servers_to_sync.append(server)
                else:
                    logger.warning(f"Server '{name}' is not in the config")
        else:
            servers_to_sync = config.get_enabled_servers()

        total_result = SyncResult()
        if not servers_to_sync:
            logger.error("No servers configured or enabled for sync")
            total_result.errors.append("No servers configured or enabled for sync")
            return total_result

        server_count = len(servers_to_sync)
        synced = 0
        cancelled = False
        with sync_lock() as acquired:
            if not acquired:
                logger.error("Another Pakt sync is already running")
                total_result.errors.append("Another sync is already running")
                return total_result

            flags = [label for on, label in ((collection_only, "collection only"), (no_movies, "no movies"),
                                             (no_shows, "no shows"), (fix_collection_dates, "fixing collection dates"))
                     if on]
            names = ", ".join(server.name for server in servers_to_sync)
            mode = "dry run" if dry_run else "live sync"
            logger.info(f"pakt {__version__} {mode} started by {started_by}: {names}"
                        + (f" ({', '.join(flags)})" if flags else ""))

            start_time = time.time()
            pushed = PushedToTrakt()
            cache = TraktCache()
            try:
                async with TraktClient(config.trakt, on_token_refresh=on_token_refresh) as trakt:
                    for server_config in servers_to_sync:
                        if cancel_check and cancel_check():
                            cancelled = True
                            break
                        with server_prefix(server_config.name):
                            try:
                                plex = PlexClient(server_config, account_token=config.plex_token)
                                await asyncio.to_thread(plex.connect)
                                friendly = plex.server.friendlyName
                                to = "" if friendly == server_config.name else f" to '{friendly}'"
                                logger.info(f"Connected{to} {plex.route}")
                                engine = SyncEngine(
                                    config, trakt, plex,
                                    log_callback=log_callback,
                                    cancel_check=cancel_check,
                                    verbose=verbose,
                                    server_config=server_config,
                                    trakt_cache=cache,
                                    pushed=pushed,
                                )
                                result = await engine.sync(
                                    dry_run=dry_run,
                                    fix_collection_dates=fix_collection_dates,
                                    collection_only=collection_only,
                                    no_movies=no_movies,
                                    no_shows=no_shows,
                                )
                            except TraktAuthError as e:
                                logger.error(str(e))
                                total_result.errors.append(str(e))
                                break
                            except (PlexAuthError, OSError) as e:
                                logger.error(f"Sync failed: {e}")
                                total_result.errors.append(f"[{server_config.name}] {e}")
                                continue
                            except Exception as e:
                                logger.exception(f"Sync failed: {type(e).__name__}: {e}")
                                total_result.errors.append(f"[{server_config.name}] {type(e).__name__}: {e}")
                                continue

                        if result is None:
                            cancelled = True
                            break
                        synced += 1
                        total_result.added_to_trakt += result.added_to_trakt
                        total_result.added_to_plex += result.added_to_plex
                        total_result.ratings_to_trakt += result.ratings_to_trakt
                        total_result.ratings_to_plex += result.ratings_to_plex
                        total_result.collection_added += result.collection_added
                        total_result.collection_updated += result.collection_updated
                        total_result.watchlist_added_trakt += result.watchlist_added_trakt
                        total_result.watchlist_added_plex += result.watchlist_added_plex
                        total_result.errors.extend(f"[{server_config.name}] {error}" for error in result.errors)
            except TraktAuthError as e:
                logger.error(str(e))
                total_result.errors.append(str(e))

            total_result.duration_seconds = time.time() - start_time
            if synced and not dry_run:
                set_last_sync(datetime.now(timezone.utc))

        report.summary(total_result, synced, server_count, dry_run, cancelled)
        return total_result
