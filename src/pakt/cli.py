"""CLI interface for Pakt."""

from __future__ import annotations

import asyncio
import sys
import time

import click
from rich.console import Console
from rich.table import Table

from pakt import __version__
from pakt.config import Config, ServerConfig, get_config_dir
from pakt.trakt import DeviceAuthStatus, TraktClient

console = Console()


@click.group()
@click.version_option(version=__version__, prog_name="pakt")
def main():
    """Pakt - Fast Plex-Trakt sync using batch operations."""
    pass


def _make_token_refresh_callback(config: Config):
    """Create a callback that saves tokens when they're refreshed."""
    def on_token_refresh(token: dict):
        config.trakt.access_token = token["access_token"]
        config.trakt.refresh_token = token["refresh_token"]
        config.trakt.expires_at = token["created_at"] + token["expires_in"]
        config.save()
    return on_token_refresh


@main.command()
@click.option("--dry-run", is_flag=True, help="Show what would be synced without making changes")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed list of items to sync")
@click.option("--server", "-s", "servers", multiple=True, help="Sync specific server(s) only (can specify multiple)")
def sync(dry_run: bool, verbose: bool, servers: tuple[str, ...]):
    """Sync watched status and ratings between Plex and Trakt."""
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
        on_token_refresh=_make_token_refresh_callback(config),
    ))

    console.print("\n[bold green]Sync complete![/]")
    console.print(f"  Added to Trakt: {result.added_to_trakt}")
    console.print(f"  Added to Plex: {result.added_to_plex}")
    console.print(f"  Ratings synced: {result.ratings_synced}")
    console.print(f"  Duration: {result.duration_seconds:.1f}s")

    if result.errors:
        console.print(f"\n[yellow]Errors ({len(result.errors)}):[/]")
        for error in result.errors[:10]:
            console.print(f"  - {error}")


@main.command()
def login():
    """Authenticate with Trakt using device code flow."""
    config = Config.load()

    async def do_auth():
        async with TraktClient(config.trakt) as client:
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
                config.trakt.access_token = result.token["access_token"]
                config.trakt.refresh_token = result.token["refresh_token"]
                config.trakt.expires_at = result.token["created_at"] + result.token["expires_in"]
                config.save()
                console.print("\n[green]✓ Successfully authenticated with Trakt![/]")
            else:
                console.print(f"\n[red]✗ {result.message}[/]")
                sys.exit(1)

    asyncio.run(do_auth())


@main.command()
def logout():
    """Revoke Trakt authentication and clear tokens."""
    config = Config.load()

    if not config.trakt.access_token:
        console.print("[yellow]Not currently logged in to Trakt.[/]")
        return

    async def do_logout():
        async with TraktClient(config.trakt) as client:
            console.print("[cyan]Revoking Trakt access token...[/]")
            success = await client.revoke_token()

            if success:
                console.print("[green]Token revoked successfully.[/]")
            else:
                console.print("[yellow]Token revocation failed, clearing local tokens anyway.[/]")

            # Clear local tokens regardless
            config.trakt.access_token = ""
            config.trakt.refresh_token = ""
            config.trakt.expires_at = 0
            config.save()
            console.print("[green]Logged out from Trakt.[/]")

    asyncio.run(do_logout())


@main.command()
@click.option("--token", is_flag=True, help="Use manual token entry instead of PIN auth")
def setup(token: bool):
    """Interactive setup wizard - configure Trakt and Plex.

    By default uses Plex PIN authentication. Use --token for manual token entry.
    """
    config = Config.load()

    console.print("\n[bold cyan]═══ Pakt Setup Wizard ═══[/]\n")

    # Check what's already configured
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

    # Step 1: Trakt
    if not trakt_done:
        console.print("[bold]Step 1: Trakt Authentication[/]\n")

        async def do_auth():
            async with TraktClient(config.trakt) as client:
                console.print("[cyan]Getting device code...[/]")
                device = await client.device_code()

                console.print(f"\n[bold]→ Go to:[/] [cyan]{device['verification_url']}[/]")
                console.print(f"[bold]→ Enter:[/] [bold yellow]{device['user_code']}[/]")
                console.print("\n[dim]Waiting for you to authorize...[/]")

                result = await client.poll_device_token(
                    device["device_code"],
                    interval=device.get("interval", 5),
                    expires_in=device.get("expires_in", 600),
                )

                if result.status == DeviceAuthStatus.SUCCESS and result.token:
                    config.trakt.access_token = result.token["access_token"]
                    config.trakt.refresh_token = result.token["refresh_token"]
                    config.trakt.expires_at = result.token["created_at"] + result.token["expires_in"]
                    config.save()
                    console.print("\n[green]✓ Trakt authenticated![/]")
                    return True
                else:
                    console.print(f"\n[red]✗ {result.message}[/]")
                    return False

        if not asyncio.run(do_auth()):
            console.print("\n[yellow]Setup incomplete. Run 'pakt setup' to try again.[/]")
            return
    else:
        console.print("[green]✓[/] Trakt: Already authenticated\n")

    # Step 2: Plex
    if not plex_done:
        console.print("\n[bold]Step 2: Plex Connection[/]\n")

        if token:
            # Manual token entry (legacy flow)
            _setup_plex_manual(config)
        else:
            # PIN authentication (default)
            _setup_plex_pin(config)

    else:
        console.print("[green]✓[/] Plex: Already configured")
        console.print(f"    Servers: {', '.join(s.name for s in config.servers)}")

    # Done!
    console.print("\n[bold green]═══ Setup Complete! ═══[/]\n")
    console.print("You're ready to sync. Try these commands:")
    console.print("  [cyan]pakt sync --dry-run[/]  - Preview what will sync")
    console.print("  [cyan]pakt sync[/]           - Run the sync")
    console.print("  [cyan]pakt serve[/]          - Start web interface")
    console.print("  [cyan]pakt servers list[/]   - View configured servers")


def _setup_plex_manual(config: Config) -> None:
    """Manual Plex token setup (legacy flow)."""
    from pakt.plex import PlexClient

    # Try to auto-detect local Plex
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
    # Auto-strip prefix if user pasted the whole thing
    if plex_token.lower().startswith("x-plex-token="):
        plex_token = plex_token[13:]
    elif plex_token.startswith("="):
        plex_token = plex_token[1:]

    # Create a server config for testing
    server_config = ServerConfig(
        name="default",
        url=plex_url,
        token=plex_token,
        enabled=True,
    )

    # Test connection
    console.print("\n[dim]Testing connection...[/]")
    try:
        plex = PlexClient(server_config)
        plex.connect()
        console.print(f"[green]✓[/] Connected to: {plex.server.friendlyName}")
        libs = plex.get_movie_libraries() + plex.get_show_libraries()
        console.print(f"[green]✓[/] Libraries: {', '.join(libs)}")

        # Save the server to config
        config.plex_token = plex_token
        config.servers.append(server_config)
        config.save()
    except Exception as e:
        console.print(f"[red]✗ Connection failed:[/] {e}")
        console.print("\n[yellow]Check your URL and token, then run 'pakt setup' again.[/]")


def _setup_plex_pin(config: Config) -> None:
    """Plex PIN authentication flow with server discovery."""
    from pakt.plex import (
        check_plex_pin_login,
        discover_servers,
        start_plex_pin_login,
    )

    console.print("Link your Plex account to discover your servers automatically.\n")
    console.print("[bold]→ Go to:[/] [cyan]https://plex.tv/link[/]")

    # Start PIN login
    login_obj, auth_info = start_plex_pin_login()
    console.print(f"[bold]→ Enter:[/] [bold yellow]{auth_info.pin}[/]")
    console.print("\n[dim]Waiting for you to authorize...[/]")

    # Poll for completion
    account_token = None
    max_attempts = 60  # 5 minutes at 5 second intervals
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

    # Save account token
    config.plex_token = account_token
    config.save()

    # Discover servers
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

    # Select servers to enable
    console.print("\n[dim]Enter server numbers to enable (comma-separated), or 'all':[/]")
    selection = click.prompt("Enable servers", default="all")

    if selection.lower() == "all":
        selected_servers = discovered
    else:
        try:
            indices = [int(x.strip()) - 1 for x in selection.split(",")]
            selected_servers = [discovered[i] for i in indices if 0 <= i < len(discovered)]
        except (ValueError, IndexError):
            console.print("[yellow]Invalid selection, enabling all servers[/]")
            selected_servers = discovered

    # Create ServerConfig for each selected server
    for server in selected_servers:
        # Check if already configured
        existing = config.get_server(server.name)
        if existing:
            console.print(f"  [dim]Server '{server.name}' already configured, updating...[/]")
            existing.server_name = server.name
            existing.url = server.best_connection_url or ""
            existing.token = account_token
        else:
            server_config = ServerConfig(
                name=server.name,
                server_name=server.name,
                url=server.best_connection_url or "",
                token=account_token,
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

    # Trakt status
    table = Table(title="Trakt")
    table.add_column("Setting", style="cyan")
    table.add_column("Value")

    table.add_row("Client ID", config.trakt.client_id[:20] + "..." if config.trakt.client_id else "[red]Not set[/]")
    table.add_row("Authenticated", "[green]Yes[/]" if config.trakt.access_token else "[red]No[/]")
    console.print(table)

    # Plex servers status
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
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8080, help="Port to bind to")
@click.option("--tray/--no-tray", default=None, help="Enable/disable system tray icon")
def serve(host: str, port: int, tray: bool | None):
    """Start the web interface."""
    import os
    import signal

    import uvicorn

    from pakt.web import create_app
    from pakt.web.app import sync_state

    # Only show tray when explicitly requested with --tray
    show_tray = tray is True

    # Suppress console output only when --tray explicitly passed (for pythonw compatibility)
    silent_mode = tray is True

    # Redirect stdout/stderr to devnull in silent mode (for pythonw)
    if silent_mode:
        try:
            devnull = open(os.devnull, 'w')
            sys.stdout = devnull
            sys.stderr = devnull
        except Exception:
            pass

    tray_instance = None
    if show_tray:
        try:
            from pakt.tray import TRAY_AVAILABLE, PaktTray

            if TRAY_AVAILABLE:
                web_url = f"http://{host}:{port}"
                tray_instance = PaktTray(
                    web_url=web_url,
                    shutdown_callback=lambda: sys.exit(0),
                )
                tray_instance.start()
            elif not silent_mode:
                console.print("[yellow]System tray requested but dependencies not installed.[/]")
                console.print("[yellow]Install with: pip install pystray Pillow[/]")
        except ImportError:
            if not silent_mode:
                console.print("[yellow]System tray requested but dependencies not installed.[/]")
                console.print("[yellow]Install with: pip install pystray Pillow[/]")

    if not silent_mode:
        console.print("[bold]Starting Pakt web interface...[/]")
        console.print(f"Open [cyan]http://{host}:{port}[/] in your browser")
        console.print("[dim]Press Ctrl+C to stop[/]\n")

    app = create_app()

    # Handle Ctrl+C gracefully - cancel sync if running
    def handle_sigint(signum, frame):
        if sync_state["running"]:
            if not silent_mode:
                console.print("\n[yellow]Cancelling sync...[/]")
            sync_state["cancelled"] = True
        else:
            if not silent_mode:
                console.print("\n[dim]Shutting down...[/]")
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        log_level = "critical" if silent_mode else "warning"
        uvicorn.run(app, host=host, port=port, log_level=log_level)
    finally:
        if tray_instance:
            tray_instance.stop()


@main.command()
@click.option("--server", help="Server name (defaults to first enabled server)")
@click.option("--movie", "-m", multiple=True, help="Movie libraries to sync (can specify multiple)")
@click.option("--show", "-s", multiple=True, help="Show libraries to sync (can specify multiple)")
@click.option("--all", "sync_all", is_flag=True, help="Sync all libraries (clear selection)")
def libraries(server: str | None, movie: tuple[str, ...], show: tuple[str, ...], sync_all: bool):
    """Configure which Plex libraries to sync.

    Run without options to see available libraries and current selection.
    """
    from pakt.plex import PlexClient

    config = Config.load()

    # Find the target server
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

    # Connect to Plex to get available libraries
    try:
        plex = PlexClient(server_config)
        plex.connect()
    except Exception as e:
        console.print(f"[red]Error connecting to Plex:[/] {e}")
        return

    available_movie_libs = plex.get_movie_libraries()
    available_show_libs = plex.get_show_libraries()

    # If options provided, update config
    if movie or show or sync_all:
        if sync_all:
            server_config.movie_libraries = []
            server_config.show_libraries = []
            console.print("[green]✓[/] Set to sync all libraries")
        else:
            if movie:
                # Validate library names
                invalid = [m for m in movie if m not in available_movie_libs]
                if invalid:
                    console.print(f"[yellow]Warning:[/] Unknown movie libraries: {', '.join(invalid)}")
                server_config.movie_libraries = [m for m in movie if m in available_movie_libs]

            if show:
                invalid = [s for s in show if s not in available_show_libs]
                if invalid:
                    console.print(f"[yellow]Warning:[/] Unknown show libraries: {', '.join(invalid)}")
                server_config.show_libraries = [s for s in show if s in available_show_libs]

        config.save()
        console.print("[green]✓[/] Configuration saved")
        console.print()

    # Display current state
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
    """Add a Plex server.

    NAME can be:
    - The name of a discovered server (from 'pakt servers discover')
    - Any name if --url and --token are provided for manual entry
    """
    from pakt.plex import discover_servers

    config = Config.load()

    # Check if already exists
    if config.get_server(name):
        console.print(f"[yellow]Server '{name}' already configured.[/]")
        console.print("Use [cyan]pakt servers remove {name}[/] to remove it first.")
        return

    if url and server_token:
        # Manual entry
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

    # Try to find from discovered servers
    if not config.plex_token:
        console.print("[red]Error:[/] No account token. Provide --url and --token for manual entry.")
        return

    console.print("[dim]Searching for server...[/]")
    try:
        discovered = discover_servers(config.plex_token)
    except Exception as e:
        console.print(f"[red]Error discovering servers:[/] {e}")
        return

    # Find matching server
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

    # Add the server
    new_server = ServerConfig(
        name=matching.name,
        server_name=matching.name,
        url=matching.best_connection_url or "",
        token=config.plex_token,
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
        plex = PlexClient(server)
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
