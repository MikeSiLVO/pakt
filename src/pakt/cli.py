"""CLI interface for Pakt."""

from __future__ import annotations

import sys
import time

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from pakt import __version__
from pakt.config import Config, ServerConfig, get_config_dir

console = Console()


@click.group()
@click.version_option(version=__version__, prog_name="pakt")
def main():
    """Pakt: fast Plex-Trakt sync using batch operations."""
    pass


@main.command()
@click.option("--dry-run", is_flag=True, help="Show what would be synced without making changes")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed list of items to sync")
@click.option("--server", "-s", "servers", multiple=True, help="Sync specific server(s) only (can specify multiple)")
@click.option("--fix-collection-dates", is_flag=True,
              help="Re-send all collection items to Trakt with correct dates from Plex")
@click.option("--collection-only", is_flag=True, help="Only sync collection (skip watched, ratings, watchlist)")
@click.option("--no-movies", is_flag=True, help="Skip movie sync")
@click.option("--no-shows", is_flag=True, help="Skip show/episode sync")
def sync(dry_run: bool, verbose: bool, servers: tuple[str, ...], fix_collection_dates: bool,
         collection_only: bool, no_movies: bool, no_shows: bool):
    """Sync watched status, ratings, collection and watchlist between Plex and Trakt."""
    import asyncio

    from pakt.sync import run_multi_server_sync

    config = Config.load()

    if not config.trakt.access_token:
        console.print("[red]Error:[/] Not logged in to Trakt. Run 'pakt login' first.")
        sys.exit(1)

    if not config.servers:
        console.print("[red]Error:[/] No Plex servers configured. Run 'pakt setup' first.")
        sys.exit(1)

    server_names = list(servers) if servers else None
    result = asyncio.run(run_multi_server_sync(
        config,
        server_names=server_names,
        dry_run=dry_run,
        verbose=verbose,
        fix_collection_dates=fix_collection_dates,
        collection_only=collection_only,
        no_movies=no_movies,
        no_shows=no_shows,
        on_token_refresh=config.save_trakt_token,
    ))

    title = "[bold yellow]Sync finished with errors[/]" if result.errors else "[bold green]Sync complete![/]"
    console.print(f"\n{title}")
    console.print(f"  Added to Trakt: {result.added_to_trakt}")
    console.print(f"  Added to Plex: {result.added_to_plex}")
    console.print(f"  Ratings synced: {result.ratings_synced}")
    if result.collection_added or result.collection_updated:
        console.print(f"  Collection added: {result.collection_added}, updated: {result.collection_updated}")
    console.print(f"  Duration: {result.duration_seconds:.1f}s")

    if result.errors:
        console.print(f"\n[yellow]Errors ({len(result.errors)}):[/]")
        for error in result.errors[:10]:
            console.print(f"  - {escape(error)}")
        sys.exit(1)


async def _trakt_device_login(config: Config) -> bool:
    """Run Trakt's device login, showing the code; saves the tokens and returns True on success."""
    from pakt.trakt import DeviceAuthStatus, TraktClient

    client = TraktClient(config.trakt)
    console.print("[cyan]Getting device code...[/]")
    device = await client.device_code()

    console.print(f"\n[bold]→ Go to:[/] [cyan]{device['verification_url']}[/]")
    console.print(f"[bold]→ Enter:[/] [bold yellow]{device['user_code']}[/]")
    console.print("\n[dim]Waiting for authorization...[/]")

    result = await client.poll_device_token(
        device["device_code"],
        interval=device.get("interval", 5),
        expires_in=device.get("expires_in", 600),
    )
    if result.status == DeviceAuthStatus.SUCCESS and result.token:
        config.save_trakt_token(result.token)
        console.print("\n[green]✓ Trakt authenticated![/]")
        return True
    console.print(f"\n[red]✗ {result.message}[/]")
    return False


@main.command()
def login():
    """Authenticate with Trakt using device code flow."""
    import asyncio

    if not asyncio.run(_trakt_device_login(Config.load())):
        sys.exit(1)


@main.command()
def logout():
    """Revoke Trakt authentication and clear tokens."""
    import asyncio

    config = Config.load()

    if not config.trakt.access_token:
        console.print("[yellow]Not currently logged in to Trakt.[/]")
        return

    async def do_logout():
        """Revoke the token on Trakt, then clear it locally either way."""
        import httpx

        from pakt.trakt import TraktClient

        console.print("[cyan]Revoking Trakt access token...[/]")
        try:
            success = await TraktClient(config.trakt).revoke_token()
        except httpx.HTTPError:
            success = False

        if success:
            console.print("[green]Token revoked successfully.[/]")
        else:
            console.print("[yellow]Token revocation failed, clearing local tokens anyway.[/]")

        config.trakt.access_token = ""
        config.trakt.refresh_token = ""
        config.trakt.expires_at = 0
        config.save()
        console.print("[green]Logged out from Trakt.[/]")

    asyncio.run(do_logout())


@main.command()
@click.option("--token", is_flag=True, help="Use manual token entry instead of PIN auth")
def setup(token: bool):
    """Interactive setup wizard: link Trakt, then Plex by PIN or a pasted token."""
    config = Config.load()

    console.print("\n[bold cyan]═══ Pakt Setup Wizard ═══[/]\n")

    trakt_done = bool(config.trakt.access_token)
    plex_done = bool(config.servers)

    if trakt_done and plex_done:
        console.print("[green]✓[/] Trakt: Authenticated")
        console.print("[green]✓[/] Plex: Configured")
        console.print(f"    Servers: {', '.join(s.name for s in config.servers)}")
        console.print("\nEverything is already set up! Run [cyan]pakt sync[/] to start syncing.")
        if click.confirm("\nReconfigure anyway?", default=False):
            trakt_done = False
            plex_done = False
        else:
            return

    if not trakt_done:
        console.print("[bold]Step 1: Trakt Authentication[/]\n")
        import asyncio

        if not asyncio.run(_trakt_device_login(config)):
            console.print("\n[yellow]Setup incomplete. Run 'pakt setup' to try again.[/]")
            return
    else:
        console.print("[green]✓[/] Trakt: Already authenticated\n")

    if not plex_done:
        console.print("\n[bold]Step 2: Plex Connection[/]\n")

        if token:
            _setup_plex_manual(config)
        else:
            _setup_plex_pin(config)

    else:
        console.print("[green]✓[/] Plex: Already configured")
        console.print(f"    Servers: {', '.join(s.name for s in config.servers)}")

    console.print("\n[bold green]═══ Setup Complete! ═══[/]\n")
    console.print("You're ready to sync. Try these commands:")
    console.print("  [cyan]pakt sync --dry-run[/]  - Preview what will sync")
    console.print("  [cyan]pakt sync[/]           - Run the sync")
    console.print("  [cyan]pakt serve[/]          - Start web interface")
    console.print("  [cyan]pakt servers list[/]   - View configured servers")


def _setup_plex_manual(config: Config) -> None:
    """Set up Plex from a pasted URL and token, testing the connection before saving."""
    from pakt.plex import PlexClient

    console.print("[dim]Checking for local Plex server...[/]")
    detected_url = None
    try:
        import httpx

        resp = httpx.get("http://localhost:32400/identity", timeout=2)
        if resp.status_code == 200:
            detected_url = "http://localhost:32400"
            console.print(f"[green]✓[/] Found Plex at {detected_url}")
    except Exception:
        console.print("[dim]No local server found[/]")

    default_url = detected_url or "http://localhost:32400"
    plex_url = click.prompt("\nPlex server URL", default=default_url)

    console.print("\n[dim]To get your Plex token:")
    console.print("1. Open Plex Web App and sign in")
    console.print("2. Open any media item")
    console.print("3. Click ⋮ → Get Info → View XML")
    console.print("4. In the URL, find X-Plex-Token=xxxxx")
    console.print("5. Copy just the token part after the =[/]\n")

    plex_token = click.prompt("Plex token")
    if plex_token.lower().startswith("x-plex-token="):
        plex_token = plex_token[13:]
    elif plex_token.startswith("="):
        plex_token = plex_token[1:]

    server_config = ServerConfig(
        name="default",
        url=plex_url,
        token=plex_token,
        enabled=True,
    )

    console.print("\n[dim]Testing connection...[/]")
    try:
        plex = PlexClient(server_config)
        plex.connect()
        console.print(f"[green]✓[/] Connected to: {plex.server.friendlyName}")
        libs = plex.get_movie_libraries() + plex.get_show_libraries()
        console.print(f"[green]✓[/] Libraries: {', '.join(libs)}")

        config.plex_token = plex_token
        config.servers = [s for s in config.servers if s.name != server_config.name] + [server_config]
        config.save()
    except Exception as e:
        console.print(f"[red]✗ Connection failed:[/] {e}")
        console.print("\n[yellow]Check your URL and token, then run 'pakt setup' again.[/]")


def _setup_plex_pin(config: Config) -> None:
    """Set up Plex by PIN login, then add the servers picked from the account."""
    from pakt.plex import (
        check_plex_pin_login,
        discover_servers,
        start_plex_pin_login,
    )

    console.print("Link your Plex account to discover your servers automatically.\n")
    console.print("[bold]→ Go to:[/] [cyan]https://plex.tv/link[/]")

    login_obj, auth_info = start_plex_pin_login()
    console.print(f"[bold]→ Enter:[/] [bold yellow]{auth_info.pin}[/]")
    console.print("\n[dim]Waiting for you to authorize...[/]")

    account_token = None
    max_attempts = 60
    for _ in range(max_attempts):
        account_token = check_plex_pin_login(login_obj)
        if account_token:
            break
        time.sleep(5)

    if not account_token:
        console.print("\n[red]✗ Authorization timed out[/]")
        console.print("[dim]Try again with 'pakt setup' or use 'pakt setup --token' for manual entry[/]")
        return

    console.print("\n[green]✓ Plex account linked![/]")

    config.plex_token = account_token
    config.save()

    console.print("\n[dim]Discovering servers...[/]")
    try:
        discovered = discover_servers(account_token)
    except Exception as e:
        console.print(f"[red]✗ Failed to discover servers:[/] {e}")
        return

    if not discovered:
        console.print("[yellow]No servers found on your account[/]")
        return

    console.print(f"\n[green]Found {len(discovered)} server(s):[/]\n")
    for i, server in enumerate(discovered, 1):
        owned = "[green]owned[/]" if server.owned else "[dim]shared[/]"
        local = "[cyan]local[/]" if server.has_local_connection else ""
        console.print(f"  {i}. {server.name} ({owned}) {local}")

    console.print("\n[dim]Enter server numbers to enable (comma-separated), 'owned' or 'all':[/]")
    selection = click.prompt("Enable servers", default="owned")

    if selection.lower() == "all":
        selected_servers = discovered
    elif selection.lower() == "owned":
        selected_servers = [s for s in discovered if s.owned]
    else:
        try:
            indices = [int(x.strip()) - 1 for x in selection.split(",")]
            selected_servers = [discovered[i] for i in indices if 0 <= i < len(discovered)]
        except (ValueError, IndexError):
            console.print("[yellow]Invalid selection, enabling owned servers[/]")
            selected_servers = [s for s in discovered if s.owned]

    for server in selected_servers:
        existing = config.get_server(server.name)
        if existing:
            console.print(f"  [dim]Server '{server.name}' already configured, updating...[/]")
            existing.server_name = server.name
            existing.url = server.best_connection_url or ""
            existing.token = server.access_token
        else:
            server_config = ServerConfig(
                name=server.name,
                server_name=server.name,
                url=server.best_connection_url or "",
                token=server.access_token,
                enabled=True,
            )
            config.servers.append(server_config)
            console.print(f"  [green]✓[/] Added server: {server.name}")

    config.save()
    console.print(f"\n[green]✓ Configured {len(selected_servers)} server(s)[/]")


@main.command()
def status():
    """Show current configuration status."""
    config = Config.load()
    config_dir = get_config_dir()

    console.print("[bold]Pakt Status[/]\n")

    table = Table(title="Trakt")
    table.add_column("Setting", style="cyan")
    table.add_column("Value")

    table.add_row("Client ID", config.trakt.client_id[:20] + "..." if config.trakt.client_id else "[red]Not set[/]")
    table.add_row("Authenticated", "[green]Yes[/]" if config.trakt.access_token else "[red]No[/]")
    console.print(table)

    servers = config.servers
    if servers:
        table = Table(title="Plex Servers")
        table.add_column("Name", style="cyan")
        table.add_column("Status")
        table.add_column("Enabled")

        for server in servers:
            status = "[green]Configured[/]" if (server.url or server.server_name) else "[red]Not set[/]"
            enabled = "[green]Yes[/]" if server.enabled else "[dim]No[/]"
            table.add_row(server.name, status, enabled)
        console.print(table)
    else:
        console.print("\n[yellow]No Plex servers configured[/]")
        console.print("[dim]Run 'pakt setup' to configure[/]")

    console.print(f"\n[dim]Config directory: {config_dir}[/]")


@main.command()
@click.option("--host", default=None, help="Host to bind to (default: from config or 127.0.0.1)")
@click.option("--port", default=None, type=int, help="Port to bind to (default: from config or 7258)")
@click.option("--tray/--no-tray", default=None, help="Enable/disable system tray icon")
def serve(host: str | None, port: int | None, tray: bool | None):
    """Start the web interface."""
    import os

    import uvicorn

    from pakt.web.app import create_app, sync_state

    # save so config.json gains the web section
    config = Config.load()
    host = host or config.web.host
    port = port or config.web.port
    config.save()
    local_url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::', '') else host}:{port}"

    show_tray = tray is True

    # pythonw has no console
    silent_mode = tray is True

    # check before rotating debug.log, which a running instance still writes
    import socket
    port_in_use = False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        sock.close()
    except OSError:
        port_in_use = True

    debug_log = None
    def log(msg: str) -> None:
        """No-op stand-in used when debug logging is off."""

    if silent_mode:
        try:
            from datetime import datetime
            log_path = get_config_dir() / "debug.log"
            old_log_path = get_config_dir() / "debug.log.1"

            if port_in_use:
                conflict_log = get_config_dir() / "debug.conflict.log"
                debug_log = open(conflict_log, "w")
            else:
                if log_path.exists():
                    if old_log_path.exists():
                        old_log_path.unlink()
                    log_path.rename(old_log_path)
                debug_log = open(log_path, "w")

            def log(msg: str) -> None:
                """Write to the debug file; pythonw has no console to fall back on."""
                if debug_log:
                    debug_log.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
                    debug_log.flush()

            if port_in_use:
                log(f"Instance start blocked - port {port} in use")
            else:
                log(f"serve() started (host={host}, port={port})")
        except Exception:
            pass

    # never closed: stdout and stderr write to it until exit
    _devnull = None
    if silent_mode:
        try:
            _devnull = open(os.devnull, 'w')
            sys.stdout = _devnull
            sys.stderr = _devnull
        except Exception:
            pass

    tray_instance = None
    if show_tray:
        try:
            if not port_in_use:
                log("Importing tray module")
            from pakt.tray import TRAY_AVAILABLE, PaktTray

            if TRAY_AVAILABLE:
                if not port_in_use:
                    log("Creating PaktTray instance")
                web_url = local_url

                def on_exit():
                    """Hard exit from the tray menu; uvicorn is on another thread."""
                    log("Exit requested from tray")
                    os._exit(0)

                def trigger_sync():
                    """Start a sync over the local API, since the tray runs off the event loop."""
                    import httpx

                    try:
                        httpx.post(f"{web_url}/api/sync", json={}, timeout=10.0)
                    except Exception as e:
                        log(f"Tray sync request failed: {e}")

                def get_scheduler():
                    """Look up the scheduler, which the web app creates after the tray starts."""
                    from pakt.web import app as web_app

                    return web_app._scheduler

                tray_instance = PaktTray(
                    web_url=web_url,
                    sync_callback=trigger_sync,
                    shutdown_callback=on_exit,
                    scheduler_getter=get_scheduler,
                )
                if not port_in_use:
                    log("Starting tray")
                tray_instance.start()
                import time
                time.sleep(0.5)
                if not port_in_use:
                    log("Tray started")
            elif not silent_mode:
                console.print("[yellow]System tray requested but dependencies not installed.[/]")
                console.print("[yellow]Install with: pip install pystray Pillow[/]")
        except ImportError as e:
            log(f"ImportError: {e}")
            if not silent_mode:
                console.print("[yellow]System tray requested but dependencies not installed.[/]")
                console.print("[yellow]Install with: pip install pystray Pillow[/]")
        except Exception as e:
            log(f"Exception: {e}")

    # once the tray is up, the conflict can show as a notification
    if port_in_use:
        log("Attempting to show notification")
        if tray_instance and tray_instance._icon:
            try:
                tray_instance._icon.notify(
                    f"Port {port} already in use.\nAnother instance may be running.",
                    "Pakt"
                )
                log("Notification sent, waiting 3s")
            except Exception as e:
                log(f"Notification failed: {e}")
            import time
            time.sleep(3)
        else:
            log("No tray icon available for notification")
        if not silent_mode:
            console.print(f"[red]Error:[/] Port {port} already in use. Another instance may be running.")
        log("Exiting due to port conflict")
        if debug_log:
            debug_log.close()
        return

    if not silent_mode:
        console.print("[bold]Starting Pakt web interface...[/]")
        console.print(f"Open [cyan]{local_url}[/] in your browser")
        console.print("[dim]Press Ctrl+C to stop[/]\n")

    log("Creating app")
    app = create_app()

    class PaktServer(uvicorn.Server):
        """Server whose first Ctrl+C cancels a running sync instead of cutting it mid-write."""

        def handle_exit(self, sig, frame) -> None:
            """Cancel a running sync on the first signal, shut down on the next."""
            if sync_state["running"] and not sync_state["cancelled"]:
                sync_state["cancelled"] = True
                if not silent_mode:
                    console.print("\n[yellow]Cancelling sync... (Ctrl+C again to quit)[/]")
                return
            if not silent_mode:
                console.print("\n[dim]Shutting down...[/]")
            super().handle_exit(sig, frame)

    try:
        log(f"Starting uvicorn on {host}:{port}")
        uvi_log_level = "critical" if silent_mode else "warning"

        uvi_config = uvicorn.Config(app, host=host, port=port, log_level=uvi_log_level)
        server = PaktServer(uvi_config)

        if tray_instance and tray_instance._icon:
            try:
                log("Sending startup notification")
                tray_instance._icon.notify(f"Running on {local_url}", "Pakt")
            except Exception as e:
                log(f"Notification failed: {e}")

        log("Running uvicorn server")
        import asyncio
        asyncio.run(server.serve())

        log("uvicorn.serve() returned normally")
    except Exception as e:
        import traceback
        log(f"uvicorn error: {e}\n{traceback.format_exc()}")
        raise
    finally:
        log("Shutting down")
        if debug_log:
            debug_log.close()
        if _devnull:
            _devnull.close()
        if tray_instance:
            tray_instance.stop()


@main.command()
@click.option("--server", help="Server name (defaults to first enabled server)")
@click.option("--movie", "-m", multiple=True, help="Movie libraries to sync (can specify multiple)")
@click.option("--show", "-s", multiple=True, help="Show libraries to sync (can specify multiple)")
@click.option("--all", "sync_all", is_flag=True, help="Sync all libraries (clear selection)")
def libraries(server: str | None, movie: tuple[str, ...], show: tuple[str, ...], sync_all: bool):
    """Configure which Plex libraries to sync, or list them when run without options."""
    from pakt.plex import PlexClient

    config = Config.load()

    if server:
        server_config = config.get_server(server)
        if not server_config:
            console.print(f"[red]Error:[/] Server '{server}' not found")
            return
    else:
        enabled = config.get_enabled_servers()
        if not enabled:
            console.print("[red]Error:[/] No servers configured. Run 'pakt setup' first.")
            return
        server_config = enabled[0]

    console.print(f"[dim]Server: {server_config.name}[/]\n")

    try:
        plex = PlexClient(server_config, account_token=config.plex_token)
        plex.connect()
    except Exception as e:
        console.print(f"[red]Error connecting to Plex:[/] {e}")
        return

    available_movie_libs = plex.get_movie_libraries()
    available_show_libs = plex.get_show_libraries()

    if movie or show or sync_all:
        if sync_all:
            server_config.movie_libraries = []
            server_config.show_libraries = []
            console.print("[green]✓[/] Set to sync all libraries")
        else:
            invalid = [m for m in movie if m not in available_movie_libs]
            invalid += [s for s in show if s not in available_show_libs]
            if invalid:
                console.print(f"[red]Error:[/] Unknown libraries: {', '.join(invalid)}. Nothing was changed.")
                sys.exit(1)
            if movie:
                server_config.movie_libraries = list(movie)
            if show:
                server_config.show_libraries = list(show)

        config.save()
        console.print("[green]✓[/] Configuration saved")
        console.print()

    table = Table(title="Plex Libraries")
    table.add_column("Type", style="cyan")
    table.add_column("Library")
    table.add_column("Status")

    current_movie = server_config.movie_libraries or []
    current_show = server_config.show_libraries or []
    sync_all_movies = len(current_movie) == 0
    sync_all_shows = len(current_show) == 0

    for lib in available_movie_libs:
        if sync_all_movies or lib in current_movie:
            status = "[green]✓ Syncing[/]"
        else:
            status = "[dim]Skipped[/]"
        table.add_row("Movie", lib, status)

    for lib in available_show_libs:
        if sync_all_shows or lib in current_show:
            status = "[green]✓ Syncing[/]"
        else:
            status = "[dim]Skipped[/]"
        table.add_row("Show", lib, status)

    console.print(table)

    if sync_all_movies and sync_all_shows:
        console.print("\n[dim]All libraries selected (default)[/]")
    console.print("\n[dim]Use --movie/-m and --show/-s to select specific libraries[/]")
    console.print("[dim]Use --all to reset to syncing all libraries[/]")


@main.group()
def servers():
    """Manage Plex server configurations."""
    pass


@servers.command("list")
def servers_list():
    """List configured Plex servers."""
    config = Config.load()

    if not config.servers:
        console.print("[yellow]No servers configured.[/]")
        console.print("\nRun [cyan]pakt setup[/] to configure servers via PIN auth")
        console.print("Or run [cyan]pakt servers add[/] to add servers manually")
        return

    table = Table(title="Configured Servers")
    table.add_column("Name", style="cyan")
    table.add_column("Server Name")
    table.add_column("URL")
    table.add_column("Status")

    for server in config.servers:
        status = "[green]Enabled[/]" if server.enabled else "[dim]Disabled[/]"
        url = server.url[:40] + "..." if len(server.url) > 40 else server.url
        table.add_row(server.name, server.server_name or "-", url or "-", status)

    console.print(table)

    if config.plex_token:
        console.print("\n[dim]Account token: configured[/]")


@servers.command("discover")
def servers_discover():
    """Discover available Plex servers from your account."""
    from pakt.plex import discover_servers

    config = Config.load()

    if not config.plex_token:
        console.print("[red]Error:[/] No Plex account token configured.")
        console.print("Run [cyan]pakt setup[/] to link your Plex account.")
        return

    console.print("[dim]Discovering servers...[/]")
    try:
        discovered = discover_servers(config.plex_token)
    except Exception as e:
        console.print(f"[red]Error:[/] {e}")
        return

    if not discovered:
        console.print("[yellow]No servers found on your account[/]")
        return

    console.print(f"\n[green]Found {len(discovered)} server(s):[/]\n")

    table = Table()
    table.add_column("#", style="dim")
    table.add_column("Name", style="cyan")
    table.add_column("Owned")
    table.add_column("Local")
    table.add_column("Configured")

    for i, server in enumerate(discovered, 1):
        owned = "[green]Yes[/]" if server.owned else "[dim]No[/]"
        local = "[cyan]Yes[/]" if server.has_local_connection else "[dim]No[/]"
        configured = "[green]Yes[/]" if config.get_server(server.name) else "[dim]No[/]"
        table.add_row(str(i), server.name, owned, local, configured)

    console.print(table)
    console.print("\n[dim]Use 'pakt servers add NAME' to add a discovered server[/]")


@servers.command("add")
@click.argument("name")
@click.option("--url", help="Server URL (for manual entry)")
@click.option("--token", "server_token", help="Server token (for manual entry)")
@click.option("--disabled", is_flag=True, help="Add server as disabled")
def servers_add(name: str, url: str | None, server_token: str | None, disabled: bool):
    """Add a Plex server, found through the account or entered by hand."""
    from pakt.plex import discover_servers

    config = Config.load()

    if config.get_server(name):
        console.print(f"[yellow]Server '{name}' already configured.[/]")
        console.print("Use [cyan]pakt servers remove {name}[/] to remove it first.")
        return

    if url and server_token:
        new_server = ServerConfig(
            name=name,
            url=url,
            token=server_token,
            enabled=not disabled,
        )
        config.servers.append(new_server)
        config.save()
        console.print(f"[green]✓[/] Added server: {name}")
        return

    if not config.plex_token:
        console.print("[red]Error:[/] No account token. Provide --url and --token for manual entry.")
        return

    console.print("[dim]Searching for server...[/]")
    try:
        discovered = discover_servers(config.plex_token)
    except Exception as e:
        console.print(f"[red]Error discovering servers:[/] {e}")
        return

    matching = None
    for server in discovered:
        if server.name.lower() == name.lower():
            matching = server
            break

    if not matching:
        console.print(f"[yellow]Server '{name}' not found in your account.[/]")
        console.print("\nAvailable servers:")
        for server in discovered:
            console.print(f"  - {server.name}")
        console.print("\nOr use --url and --token for manual entry.")
        return

    new_server = ServerConfig(
        name=matching.name,
        server_name=matching.name,
        url=matching.best_connection_url or "",
        token=matching.access_token,
        enabled=not disabled,
    )
    config.servers.append(new_server)
    config.save()
    console.print(f"[green]✓[/] Added server: {matching.name}")


@servers.command("remove")
@click.argument("name")
def servers_remove(name: str):
    """Remove a configured Plex server."""
    config = Config.load()

    server = config.get_server(name)
    if not server:
        console.print(f"[yellow]Server '{name}' not found.[/]")
        return

    config.servers = [s for s in config.servers if s.name != name]
    config.save()
    console.print(f"[green]✓[/] Removed server: {name}")


@servers.command("test")
@click.argument("name")
def servers_test(name: str):
    """Test connection to a configured Plex server."""
    from pakt.plex import PlexClient

    config = Config.load()
    server = config.get_server(name)

    if not server:
        console.print(f"[yellow]Server '{name}' not found.[/]")
        return

    console.print(f"[dim]Testing connection to {name}...[/]")

    try:
        plex = PlexClient(server, account_token=config.plex_token)
        plex.connect()
        console.print(f"[green]✓[/] Connected to: {plex.server.friendlyName}")

        movie_libs = plex.get_movie_libraries()
        show_libs = plex.get_show_libraries()
        console.print(f"[green]✓[/] Movie libraries: {', '.join(movie_libs) or 'none'}")
        console.print(f"[green]✓[/] Show libraries: {', '.join(show_libs) or 'none'}")
    except Exception as e:
        console.print(f"[red]✗ Connection failed:[/] {e}")


@servers.command("enable")
@click.argument("name")
def servers_enable(name: str):
    """Enable a server for syncing."""
    config = Config.load()
    server = config.get_server(name)

    if not server:
        console.print(f"[yellow]Server '{name}' not found.[/]")
        return

    server.enabled = True
    config.save()
    console.print(f"[green]✓[/] Enabled server: {name}")


@servers.command("disable")
@click.argument("name")
def servers_disable(name: str):
    """Disable a server from syncing."""
    config = Config.load()
    server = config.get_server(name)

    if not server:
        console.print(f"[yellow]Server '{name}' not found.[/]")
        return

    server.enabled = False
    config.save()
    console.print(f"[green]✓[/] Disabled server: {name}")


if __name__ == "__main__":
    main()
