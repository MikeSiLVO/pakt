"""CLI interface for Pakt."""

from __future__ import annotations

import asyncio
import sys

import click
from rich.console import Console
from rich.table import Table

from pakt import __version__
from pakt.cache import Cache
from pakt.config import Config, get_config_dir
from pakt.trakt import TraktClient

console = Console()


@click.group()
@click.version_option(version=__version__, prog_name="pakt")
def main():
    """Pakt - Fast Plex-Trakt sync using batch operations."""
    pass


@main.command()
@click.option("--dry-run", is_flag=True, help="Show what would be synced without making changes")
def sync(dry_run: bool):
    """Sync watched status and ratings between Plex and Trakt."""
    from pakt.sync import run_sync

    config = Config.load()

    if not config.trakt.access_token:
        console.print("[red]Error:[/] Not logged in to Trakt. Run 'pakt login' first.")
        sys.exit(1)

    if not config.plex.url or not config.plex.token:
        console.print("[red]Error:[/] Plex not configured. Run 'pakt setup' first.")
        sys.exit(1)

    result = asyncio.run(run_sync(config, dry_run=dry_run))

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

    if not config.trakt.client_id or not config.trakt.client_secret:
        console.print("[bold]Trakt API credentials required.[/]")
        console.print("Create an app at: https://trakt.tv/oauth/applications")
        console.print()

        config.trakt.client_id = click.prompt("Client ID")
        config.trakt.client_secret = click.prompt("Client Secret")

    async def do_auth():
        async with TraktClient(config.trakt) as client:
            # Get device code
            console.print("\n[cyan]Getting device code...[/]")
            device = await client.device_code()

            console.print(f"\n[bold]Go to:[/] {device['verification_url']}")
            console.print(f"[bold]Enter code:[/] {device['user_code']}")
            console.print("\nWaiting for authorization...")

            # Poll for token
            token = await client.poll_device_token(
                device["device_code"],
                interval=device.get("interval", 5),
                expires_in=device.get("expires_in", 600),
            )

            if token:
                config.trakt.access_token = token["access_token"]
                config.trakt.refresh_token = token["refresh_token"]
                config.trakt.expires_at = token["created_at"] + token["expires_in"]
                config.save()
                console.print("\n[green]Successfully authenticated with Trakt![/]")
            else:
                console.print("\n[red]Authentication failed or timed out.[/]")
                sys.exit(1)

    asyncio.run(do_auth())


@main.command()
def setup():
    """Configure Plex connection."""
    config = Config.load()

    console.print("[bold]Plex Configuration[/]\n")

    # Option 1: Direct URL + token
    console.print("Option 1: Direct connection (recommended for local servers)")
    console.print("Option 2: Plex account (for remote/shared servers)")
    console.print()

    choice = click.prompt("Choose option", type=click.Choice(["1", "2"]), default="1")

    if choice == "1":
        config.plex.url = click.prompt("Plex server URL", default=config.plex.url or "http://localhost:32400")
        config.plex.token = click.prompt("Plex token", default=config.plex.token or "")
    else:
        config.plex.token = click.prompt("Plex account token")
        config.plex.server_name = click.prompt("Server name")

    config.save()
    console.print("\n[green]Plex configuration saved![/]")

    # Test connection
    console.print("\nTesting connection...")
    try:
        from pakt.plex import PlexClient
        plex = PlexClient(config.plex)
        plex.connect()
        console.print(f"[green]Connected to:[/] {plex.server.friendlyName}")
        console.print(f"[green]Libraries:[/] {', '.join(plex.get_movie_libraries() + plex.get_show_libraries())}")
    except Exception as e:
        console.print(f"[red]Connection failed:[/] {e}")


@main.command()
def status():
    """Show current configuration and cache status."""
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

    # Cache stats
    async def get_cache_stats():
        async with Cache(config.cache) as cache:
            return await cache.get_stats()

    try:
        stats = asyncio.run(get_cache_stats())
        table = Table(title="Cache")
        table.add_column("Type", style="cyan")
        table.add_column("Entries")

        for key, value in stats.items():
            table.add_row(key, str(value))
        console.print(table)
    except Exception:
        console.print("[yellow]Cache not initialized[/]")

    console.print(f"\n[dim]Config directory: {config_dir}[/]")


@main.command()
def clear_cache():
    """Clear the cache."""
    config = Config.load()

    async def do_clear():
        async with Cache(config.cache) as cache:
            removed = await cache.clear_expired()
            stats = await cache.get_stats()
            return removed, stats

    removed, stats = asyncio.run(do_clear())

    console.print(f"[green]Cleared {removed} expired entries[/]")
    console.print(f"Remaining: {stats}")


if __name__ == "__main__":
    main()
