"""CLI interface for Pakt."""

from __future__ import annotations

import asyncio
import sys

import click
from rich.console import Console
from rich.table import Table

from pakt import __version__
from pakt.config import Config, get_config_dir
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
def sync(dry_run: bool, verbose: bool):
    """Sync watched status and ratings between Plex and Trakt."""
    from pakt.sync import run_sync

    config = Config.load()

    if not config.trakt.access_token:
        console.print("[red]Error:[/] Not logged in to Trakt. Run 'pakt login' first.")
        sys.exit(1)

    if not config.plex.url or not config.plex.token:
        console.print("[red]Error:[/] Plex not configured. Run 'pakt setup' first.")
        sys.exit(1)

    result = asyncio.run(run_sync(config, dry_run=dry_run, verbose=verbose, on_token_refresh=_make_token_refresh_callback(config)))

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
def setup():
    """Interactive setup wizard - configure Trakt and Plex in one go."""
    from pakt.plex import PlexClient

    config = Config.load()

    console.print("\n[bold cyan]═══ Pakt Setup Wizard ═══[/]\n")

    # Check what's already configured
    trakt_done = bool(config.trakt.access_token)
    plex_done = bool(config.plex.url and config.plex.token)

    if trakt_done and plex_done:
        console.print("[green]✓[/] Trakt: Authenticated")
        console.print("[green]✓[/] Plex: Configured")
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

        default_url = detected_url or config.plex.url or "http://localhost:32400"
        config.plex.url = click.prompt("\nPlex server URL", default=default_url)

        console.print("\n[dim]To get your Plex token:")
        console.print("1. Open Plex Web App and sign in")
        console.print("2. Open any media item")
        console.print("3. Click ⋮ → Get Info → View XML")
        console.print("4. In the URL, find X-Plex-Token=xxxxx")
        console.print("5. Copy just the token part after the =[/]\n")

        token = click.prompt("Plex token")
        # Auto-strip prefix if user pasted the whole thing
        if token.lower().startswith("x-plex-token="):
            token = token[13:]
        elif token.startswith("="):
            token = token[1:]
        config.plex.token = token
        config.save()

        # Test connection
        console.print("\n[dim]Testing connection...[/]")
        try:
            plex = PlexClient(config.plex)
            plex.connect()
            console.print(f"[green]✓[/] Connected to: {plex.server.friendlyName}")
            libs = plex.get_movie_libraries() + plex.get_show_libraries()
            console.print(f"[green]✓[/] Libraries: {', '.join(libs)}")
        except Exception as e:
            console.print(f"[red]✗ Connection failed:[/] {e}")
            console.print("\n[yellow]Check your URL and token, then run 'pakt setup' again.[/]")
            return
    else:
        console.print("[green]✓[/] Plex: Already configured")

    # Done!
    console.print("\n[bold green]═══ Setup Complete! ═══[/]\n")
    console.print("You're ready to sync. Try these commands:")
    console.print("  [cyan]pakt sync --dry-run[/]  - Preview what will sync")
    console.print("  [cyan]pakt sync[/]           - Run the sync")
    console.print("  [cyan]pakt serve[/]          - Start web interface")


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

    # Plex status
    table = Table(title="Plex")
    table.add_column("Setting", style="cyan")
    table.add_column("Value")

    table.add_row("URL", config.plex.url or "[red]Not set[/]")
    table.add_row("Token", "***" if config.plex.token else "[red]Not set[/]")
    console.print(table)

    console.print(f"\n[dim]Config directory: {config_dir}[/]")


@main.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8080, help="Port to bind to")
@click.option("--tray/--no-tray", default=None, help="Enable/disable system tray icon")
def serve(host: str, port: int, tray: bool | None):
    """Start the web interface."""
    import os
    import uvicorn

    from pakt.web import create_app

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

    try:
        log_level = "critical" if silent_mode else "warning"
        uvicorn.run(app, host=host, port=port, log_level=log_level)
    finally:
        if tray_instance:
            tray_instance.stop()


@main.command()
@click.option("--movie", "-m", multiple=True, help="Movie libraries to sync (can specify multiple)")
@click.option("--show", "-s", multiple=True, help="Show libraries to sync (can specify multiple)")
@click.option("--all", "sync_all", is_flag=True, help="Sync all libraries (clear selection)")
def libraries(movie: tuple[str, ...], show: tuple[str, ...], sync_all: bool):
    """Configure which Plex libraries to sync.

    Run without options to see available libraries and current selection.
    """
    from pakt.plex import PlexClient

    config = Config.load()

    if not config.plex.url or not config.plex.token:
        console.print("[red]Error:[/] Plex not configured. Run 'pakt setup' first.")
        return

    # Connect to Plex to get available libraries
    try:
        plex = PlexClient(config.plex)
        plex.connect()
    except Exception as e:
        console.print(f"[red]Error connecting to Plex:[/] {e}")
        return

    available_movie_libs = plex.get_movie_libraries()
    available_show_libs = plex.get_show_libraries()

    # If options provided, update config
    if movie or show or sync_all:
        if sync_all:
            config.sync.movie_libraries = []
            config.sync.show_libraries = []
            console.print("[green]✓[/] Set to sync all libraries")
        else:
            if movie:
                # Validate library names
                invalid = [m for m in movie if m not in available_movie_libs]
                if invalid:
                    console.print(f"[yellow]Warning:[/] Unknown movie libraries: {', '.join(invalid)}")
                config.sync.movie_libraries = [m for m in movie if m in available_movie_libs]

            if show:
                invalid = [s for s in show if s not in available_show_libs]
                if invalid:
                    console.print(f"[yellow]Warning:[/] Unknown show libraries: {', '.join(invalid)}")
                config.sync.show_libraries = [s for s in show if s in available_show_libs]

        config.save()
        console.print("[green]✓[/] Configuration saved")
        console.print()

    # Display current state
    table = Table(title="Plex Libraries")
    table.add_column("Type", style="cyan")
    table.add_column("Library")
    table.add_column("Status")

    current_movie = config.sync.movie_libraries or []
    current_show = config.sync.show_libraries or []
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


if __name__ == "__main__":
    main()
