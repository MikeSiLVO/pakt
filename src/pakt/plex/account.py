"""The plex.tv side: Pakt's identity, PIN login, server discovery and how a server is reached."""

from __future__ import annotations

import functools
import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

import plexapi
from plexapi.exceptions import NotFound
from plexapi.myplex import MyPlexAccount, MyPlexPinLogin, MyPlexResource
from plexapi.server import PlexServer

from pakt import __version__
from pakt.config import Config


class PlexAuthError(Exception):
    """Error raised when Plex refuses the saved token."""

    def __init__(self, message: str = "Plex refused the saved token. Sign in again with 'pakt setup' "
                 "or Re-Link Account in the web UI."):
        """Default to the message that tells the user how to recover."""
        super().__init__(message)


@functools.cache
def identify() -> None:
    """Identify Pakt to Plex once per run: its name and saved device ID go on plexapi's headers."""
    config = Config.load()
    if not config.plex_client_id:
        config.plex_client_id = str(plexapi.X_PLEX_IDENTIFIER)
        config.save()
    plexapi.BASE_HEADERS["X-Plex-Product"] = "Pakt"
    plexapi.BASE_HEADERS["X-Plex-Version"] = __version__
    plexapi.BASE_HEADERS["X-Plex-Client-Identifier"] = config.plex_client_id


@dataclass
class PlexPinAuth:
    """The PIN a user enters at plex.tv/link, and its ID for polling."""

    pin: str
    pin_id: int
    verification_url: str = "https://plex.tv/link"


def start_plex_pin_login() -> tuple[MyPlexPinLogin, PlexPinAuth]:
    """Start Plex PIN login, returning the login object to poll and the PIN to show."""
    identify()
    login = MyPlexPinLogin()

    if hasattr(login, '_getCode'):
        login._getCode()

    pin_code = getattr(login, '_code', None) or getattr(login, 'pin', None)
    pin_id = getattr(login, '_id', None)

    if not pin_code:
        try:
            pin_code = login.pin
        except Exception as e:
            raise RuntimeError(f"Failed to get PIN code from Plex: {e}")

    if not pin_code:
        raise RuntimeError("Failed to get PIN code from Plex - API returned empty response")

    return login, PlexPinAuth(
        pin=str(pin_code),
        pin_id=int(pin_id) if pin_id else 0,
    )


def check_plex_pin_login(login: MyPlexPinLogin) -> str | None:
    """Check if the PIN was approved; returns the account token, None while pending."""
    if login.checkLogin():
        # the PIN's token can be temporary; the account object carries the permanent one
        temp_token = login.token
        try:
            account = MyPlexAccount(token=temp_token)
            return account.authenticationToken
        except Exception:
            return temp_token
    return None


@dataclass
class DiscoveredServer:
    """A Plex server discovered from the user's account."""

    name: str
    client_identifier: str
    provides: str
    owned: bool
    connections: list[dict]  # {uri, local, relay} each
    access_token: str = ""

    @property
    def has_local_connection(self) -> bool:
        """Whether plex.tv lists a LAN address for the server."""
        return any(c.get("local") for c in self.connections)

    @property
    def best_connection_url(self) -> str | None:
        """Get the best connection URL: LAN before remote, direct before relay, then HTTPS."""
        ranked = sorted(self.connections, key=lambda c: (
            not c.get("local"), bool(c.get("relay")), not str(c.get("uri") or "").startswith("https"),
        ))
        return ranked[0].get("uri") if ranked else None


def connect_resource(resource: MyPlexResource) -> PlexServer:
    """Connect to a discovered server over HTTPS, using plain HTTP only for its LAN addresses."""
    try:
        return resource.connect(ssl=True)
    except NotFound:
        return resource.connect(ssl=False, locations=["local"])


def is_lan_url(url: str) -> bool:
    """True when a URL points at a loopback or private address, plex.direct names included."""
    host = urlsplit(url).hostname or ""
    if host.endswith(".plex.direct"):
        host = host.split(".", 1)[0].replace("-", ".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost"
    return address.is_private or address.is_loopback


def discover_servers(account_token: str) -> list[DiscoveredServer]:
    """Discover the Plex servers the account can reach, with each one's own access token."""
    identify()
    account = MyPlexAccount(token=account_token)
    servers = []

    for resource in account.resources():
        if "server" not in resource.provides:
            continue

        connections = [
            {"uri": conn.uri, "local": conn.local, "relay": conn.relay}
            for conn in resource.connections
            if conn is not None
        ]

        servers.append(DiscoveredServer(
            name=resource.name,
            client_identifier=resource.clientIdentifier,
            provides=resource.provides,
            owned=bool(resource.owned),
            connections=connections,
            access_token=str(resource.accessToken or ""),
        ))

    return servers
