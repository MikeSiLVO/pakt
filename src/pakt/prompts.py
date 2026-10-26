"""Interactive setup steps for the CLI: Trakt device login, Plex sign-in and a web password."""

from __future__ import annotations

import time

import click

from pakt.config import Config, ServerConfig
from pakt.log import console


async def trakt_device_login(config: Config) -> bool:
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


def prompt_web_password(config: Config) -> None:
    """Prompt for a new web UI password and save its hash with a fresh session secret."""
    import secrets

    from pakt.web.auth import MIN_PASSWORD_LENGTH, hash_password

    while True:
        password = click.prompt("Web UI password", hide_input=True, confirmation_prompt=True)
        if len(password) >= MIN_PASSWORD_LENGTH:
            break
        console.print(f"[yellow]Use at least {MIN_PASSWORD_LENGTH} characters.[/]")
    config.web.password_hash = hash_password(password)
    config.web.session_secret = secrets.token_urlsafe(32)
    config.save()
    console.print("[green]✓[/] Password saved. Devices that were logged in must log in again.")


def setup_plex_manual(config: Config) -> None:
    """Set up Plex from a pasted URL and token, testing the connection before saving."""
    from pakt.plex.client import PlexClient

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


def setup_plex_pin(config: Config) -> None:
    """Set up Plex by PIN login, then add the servers picked from the account."""
    from pakt.plex.account import (
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
