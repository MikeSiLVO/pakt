"""CLI interface for Pakt."""

from __future__ import annotations

import logging
import os
import sys

import click
from rich.console import Console
from rich.table import Table

from pakt import __version__, prompts
from pakt.config import Config, ServerConfig, get_config_dir
from pakt.log import setup_logging

console = Console()
logger = logging.getLogger(__name__)


@click.group()
@click.version_option(version=__version__, prog_name="pakt")
def main():
    """Pakt: fast Plex-Trakt sync using batch operations."""
    setup_logging()


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

    from pakt.sync.run import run_multi_server_sync

    config = Config.load()

    if not config.trakt.access_token:
        logger.error("Not logged in to Trakt. Run 'pakt login' first.")
        sys.exit(1)

    if not config.servers:
        logger.error("No Plex servers configured. Run 'pakt setup' first.")
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
    if result.errors:
        sys.exit(1)


@main.command()
def login():
    """Log in to Trakt using device code flow."""
    import asyncio

    if not asyncio.run(prompts.trakt_device_login(Config.load())):
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
    """Interactive setup wizard: link Trakt and Plex, then offer a web UI password."""
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

        if not asyncio.run(prompts.trakt_device_login(config)):
            console.print("\n[yellow]Setup incomplete. Run 'pakt setup' to try again.[/]")
            return
    else:
        console.print("[green]✓[/] Trakt: Already authenticated\n")

    if not plex_done:
        console.print("\n[bold]Step 2: Plex Connection[/]\n")

        if token:
            prompts.setup_plex_manual(config)
        else:
            prompts.setup_plex_pin(config)

    else:
        console.print("[green]✓[/] Plex: Already configured")
        console.print(f"    Servers: {', '.join(s.name for s in config.servers)}")

    if not config.web.password_hash and click.confirm(
        "\nSet a password for opening the web UI from another device?",
        default=os.path.exists("/.dockerenv"),
    ):
        prompts.prompt_web_password(config)

    console.print("\n[bold green]═══ Setup Complete! ═══[/]\n")
    console.print("You're ready to sync. Try these commands:")
    console.print("  [cyan]pakt sync --dry-run[/]  - Preview what will sync")
    console.print("  [cyan]pakt sync[/]           - Run the sync")
    console.print("  [cyan]pakt serve[/]          - Start web interface")
    console.print("  [cyan]pakt servers list[/]   - View configured servers")


@main.command()
@click.option("--clear", is_flag=True, help="Remove the password; the web UI then only opens on this machine")
def password(clear: bool):
    """Set the password the web UI asks for when opened from another device, or remove it."""
    config = Config.load()
    if clear:
        config.web.password_hash = ""
        config.web.session_secret = ""
        config.save()
        console.print("[green]✓[/] Password removed. The web UI now only opens on this machine.")
        return
    prompts.prompt_web_password(config)


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
    """Serve the web interface, with a tray icon when asked."""
    from pakt.web.server import run_web_ui

    # save so config.json gains the web section
    config = Config.load()
    host = host or config.web.host
    port = port or config.web.port
    config.save()
    run_web_ui(host, port, tray is True)


@main.command()
@click.option("--server", help="Server name (defaults to first enabled server)")
@click.option("--movie", "-m", multiple=True, help="Movie libraries to sync (can specify multiple)")
@click.option("--show", "-s", multiple=True, help="Show libraries to sync (can specify multiple)")
@click.option("--all", "sync_all", is_flag=True, help="Sync all libraries (clear selection)")
def libraries(server: str | None, movie: tuple[str, ...], show: tuple[str, ...], sync_all: bool):
    """Configure which Plex libraries to sync, or list them when run without options."""
    from pakt.plex.client import PlexClient

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
    from pakt.plex.account import discover_servers

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
    from pakt.plex.account import discover_servers

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
    from pakt.plex.client import PlexClient

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
