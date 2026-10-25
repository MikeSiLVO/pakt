"""Plex API client wrapper."""

from __future__ import annotations

import functools
import ipaddress
import logging
import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import plexapi
from plexapi.exceptions import NotFound, Unauthorized
from plexapi.myplex import MyPlexAccount, MyPlexPinLogin, MyPlexResource
from plexapi.server import PlexServer
from plexapi.video import Episode, Movie, Show

from pakt import __version__
from pakt.config import Config, ServerConfig
from pakt.models import PlexIds

logger = logging.getLogger(__name__)

LEGACY_GUID = re.compile(r"^com\.plexapp\.agents\.(imdb|themoviedb|thetvdb)://([^/?]+)")


class PlexAuthError(Exception):
    """Error raised when Plex refuses the saved token."""

    def __init__(self, message: str = "Plex refused the saved token. Sign in again with 'pakt setup' "
                 "or Re-Link Account in the web UI."):
        """Default to the message that tells the user how to recover."""
        super().__init__(message)


@functools.cache
def _identify() -> None:
    """Identify Pakt to Plex: its name and saved device ID go on plexapi's request headers."""
    config = Config.load()
    if not config.plex_client_id:
        config.plex_client_id = str(plexapi.X_PLEX_IDENTIFIER)
        config.save()
    plexapi.BASE_HEADERS["X-Plex-Product"] = "Pakt"
    plexapi.BASE_HEADERS["X-Plex-Version"] = __version__
    plexapi.BASE_HEADERS["X-Plex-Client-Identifier"] = config.plex_client_id


def _disable_auto_reload(items: list) -> list:
    """Disable PlexAPI auto-reload on items to prevent network calls for None attributes."""
    for item in items:
        item._autoReload = False
    return items


@dataclass
class PlexPinAuth:
    """Plex PIN authentication state."""

    pin: str
    pin_id: int
    verification_url: str = "https://plex.tv/link"


def start_plex_pin_login() -> tuple[MyPlexPinLogin, PlexPinAuth]:
    """Start Plex PIN login, returning the login object to poll and the PIN to show."""
    _identify()
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
        # The PIN login token may be temporary - exchange it for permanent token
        # by creating a MyPlexAccount which fetches the account's auth token
        temp_token = login.token
        try:
            account = MyPlexAccount(token=temp_token)
            # authenticationToken is the permanent account token
            return account.authenticationToken
        except Exception:
            # Fall back to the PIN token if exchange fails
            return temp_token
    return None


@dataclass
class DiscoveredServer:
    """A Plex server discovered from the user's account."""

    name: str
    client_identifier: str
    provides: str  # "server" for media servers
    owned: bool
    connections: list[dict]  # List of {uri, local, relay} dicts
    access_token: str = ""

    @property
    def has_local_connection(self) -> bool:
        """Whether the server advertises a LAN address, which is faster than relay."""
        return any(c.get("local") for c in self.connections)

    @property
    def best_connection_url(self) -> str | None:
        """Get the best connection URL: LAN before remote, direct before relay, then HTTPS."""
        ranked = sorted(self.connections, key=lambda c: (
            not c.get("local"), bool(c.get("relay")), not str(c.get("uri") or "").startswith("https"),
        ))
        return ranked[0].get("uri") if ranked else None


def _connect_resource(resource: MyPlexResource) -> PlexServer:
    """Connect to a discovered server over HTTPS, using plain HTTP only for its LAN addresses."""
    try:
        return resource.connect(ssl=True)
    except NotFound:
        return resource.connect(ssl=False, locations=["local"])


def _is_lan_url(url: str) -> bool:
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
    """Discover all Plex servers accessible with the given account token."""
    _identify()
    account = MyPlexAccount(token=account_token)
    servers = []

    for resource in account.resources():
        # Only include actual servers
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


class PlexClient:
    """Plex API client optimized for batch operations."""

    def __init__(self, server_config: ServerConfig, account_token: str = ""):
        _identify()
        self._url = server_config.url
        self._token = server_config.token
        self._account_token = account_token or server_config.token
        self._server_name = server_config.server_name
        self.server_config = server_config
        self._server: PlexServer | None = None
        self._account: MyPlexAccount | None = None

    def connect(self) -> None:
        """Connect via plex.tv, else the saved URL; PlexAuthError if Plex refuses the token."""
        owned = False
        if self._account_token and self._server_name:
            try:
                logger.debug("Connecting to '%s' via plex.tv discovery", self._server_name)
                resource = MyPlexAccount(token=self._account_token).resource(self._server_name)
                owned = bool(resource.owned)
                self._token = str(resource.accessToken or "") or self._token
                self._server = _connect_resource(resource)
                self._persist_connection()
                return
            except Unauthorized as e:
                raise PlexAuthError() from e
            except Exception as e:
                if not self._url:
                    raise
                logger.warning(
                    "Discovery connect to '%s' failed (%s), trying direct URL",
                    self._server_name, e,
                )

        if not (self._url and self._token):
            raise ValueError("Need either URL+token or token+server_name")
        # a shared server's owner publishes its addresses; only a LAN address gets the account token
        if (self._server_name and not owned and self._token == self._account_token
                and not _is_lan_url(self._url)):
            raise ConnectionError(f"Could not reach '{self._server_name}' through plex.tv")
        logger.debug("Connecting directly to %s", self._url)
        try:
            self._server = PlexServer(self._url, self._token)
        except Unauthorized as e:
            raise PlexAuthError() from e
        self._persist_connection()

    def _persist_connection(self) -> None:
        """Save the URL and server token this connection used back to config.json."""
        if not self._server:
            return

        # plexapi types these loosely, but both are strings on a live connection
        new_url = str(self._server._baseurl or "")
        new_token = str(self._server._token or "")

        if not new_url or not new_token:
            return

        if new_url == self._url and new_token == self._token:
            return

        if new_url != self._url:
            logger.info("Server URL changed: %s -> %s", self._url, new_url)
            self._url = new_url
        if new_token != self._token:
            logger.info("Server access token updated for '%s'", self._server_name)
            self._token = new_token

        try:
            config = Config.load()
            server = config.get_server(self.server_config.name)
            if server:
                server.url = self._url
                server.token = self._token
                config.save()
        except Exception as e:
            logger.warning("Failed to persist updated connection: %s", e)

    @property
    def account_token(self) -> str:
        """Token for plex.tv account calls, shared by every server on the account."""
        return self._account_token

    @property
    def account(self) -> MyPlexAccount:
        """Get MyPlex account for watchlist operations."""
        if self._account is None:
            self._account = MyPlexAccount(token=self._account_token)
        return self._account

    @property
    def server(self) -> PlexServer:
        """Connected server, dialling out on first use."""
        if not self._server:
            self.connect()
        if not self._server:
            raise RuntimeError(f"Not connected to Plex server '{self.server_config.name}'")
        return self._server

    def get_movie_libraries(self) -> list[str]:
        """Get all movie library names."""
        return [lib.title for lib in self.server.library.sections() if lib.type == "movie"]

    def get_show_libraries(self) -> list[str]:
        """Get all TV show library names."""
        return [lib.title for lib in self.server.library.sections() if lib.type == "show"]

    def _fetch_sections(
        self, libtype: str, library_names: list[str] | None, episodes: bool = False
    ) -> tuple[list, dict[str, int]]:
        """Fetch all items of one library type, counted per library with clashing names numbered."""
        items: list = []
        lib_counts: dict[str, int] = {}
        for section in self.server.library.sections():
            if section.type != libtype or (library_names and section.title not in library_names):
                continue
            # large pages keep the request count down
            if episodes:
                found = section.searchEpisodes(container_size=1000)
            else:
                found = section.all(container_size=1000)
            _disable_auto_reload(found)
            key, i = section.title, 2
            while key in lib_counts:
                key, i = f"{section.title} ({i})", i + 1
            lib_counts[key] = len(found)
            items.extend(found)
        return items, lib_counts

    def get_all_movies_with_counts(self, library_names: list[str] | None = None) -> tuple[list[Movie], dict[str, int]]:
        """Get all movies from specified libraries with per-library counts."""
        return self._fetch_sections("movie", library_names)

    def get_all_shows_with_counts(self, library_names: list[str] | None = None) -> tuple[list[Show], dict[str, int]]:
        """Get all shows from specified libraries with per-library counts."""
        return self._fetch_sections("show", library_names)

    def get_all_episodes_with_counts(
        self, library_names: list[str] | None = None
    ) -> tuple[list[Episode], dict[str, int]]:
        """Get all episodes from specified libraries with per-library counts."""
        return self._fetch_sections("show", library_names, episodes=True)

    def mark_watched_batch(
        self, items: Sequence[Movie | Episode], max_workers: int = 10
    ) -> list[tuple[Movie | Episode, Exception]]:
        """Mark items watched concurrently; returns (item, error) for any failures."""
        if not items:
            return []

        failed: list[tuple[Movie | Episode, Exception]] = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_item = {
                executor.submit(item.markWatched): item
                for item in items
            }

            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    future.result()
                except Exception as e:
                    failed.append((item, e))
                    logger.warning(f"Failed to mark watched: {item.title} - {e}")

        if failed:
            logger.error(f"Batch mark watched: {len(failed)}/{len(items)} failed")

        return failed

    def rate_batch(
        self, items: Sequence[tuple[Movie | Show | Episode, int | float]], max_workers: int = 10
    ) -> list[tuple[Movie | Show | Episode, int | float, Exception]]:
        """Rate (item, rating) pairs concurrently; returns (item, rating, error) for failures."""
        if not items:
            return []

        failed: list[tuple[Movie | Show | Episode, int | float, Exception]] = []

        def rate_item(pair: tuple[Movie | Show | Episode, int | float]) -> None:
            """Runs on the thread pool, one item per worker."""
            item, rating = pair
            item.rate(rating)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_item = {
                executor.submit(rate_item, pair): pair
                for pair in items
            }

            for future in as_completed(future_to_item):
                item, rating = future_to_item[future]
                try:
                    future.result()
                except Exception as e:
                    failed.append((item, rating, e))
                    logger.warning(f"Failed to rate: {item.title} ({rating}) - {e}")

        if failed:
            logger.error(f"Batch rate: {len(failed)}/{len(items)} failed")

        return failed

    def get_watchlist(self) -> list[Movie | Show]:
        """Get account watchlist (includes items not in library)."""
        return self.account.watchlist()

    def add_to_watchlist_batch(self, items: Sequence[Movie | Show]) -> list[tuple[Movie | Show, Exception]]:
        """Add items to the account watchlist one by one; returns (item, error) for any failures."""
        failed: list[tuple[Movie | Show, Exception]] = []
        for item in items:
            try:
                self.account.addToWatchlist(item)
            except Exception as e:
                failed.append((item, e))
                logger.warning(f"Failed to add to watchlist: {item.title} - {e}")
        return failed

    def search_discover(self, query: str, libtype: str | None = None) -> list[Movie | Show]:
        """Search Plex Discover for items not in library."""
        return self.account.searchDiscover(query, libtype=libtype)


# Resolution mapping: Plex videoResolution -> Trakt resolution
RESOLUTION_MAP = {
    "4k": "uhd_4k",
    "1080": "hd_1080p",
    "720": "hd_720p",
    "576": "sd_576p",
    "480": "sd_480p",
    "sd": "sd_480p",
}

# Resolution ranking for scoring (higher = better)
RESOLUTION_RANK = {
    "uhd_4k": 5,
    "hd_1080p": 4,
    "hd_1080i": 4,
    "hd_720p": 3,
    "sd_576p": 2,
    "sd_576i": 2,
    "sd_480p": 1,
    "sd_480i": 1,
}

# HDR ranking (higher = better)
HDR_RANK = {
    "dolby_vision": 4,
    "hdr10_plus": 3,
    "hdr10": 2,
    "hlg": 1,
}

# Audio codec mapping: Plex audioCodec -> Trakt audio
AUDIO_CODEC_MAP = {
    "truehd": "dolby_truehd",
    "eac3": "dolby_digital_plus",
    "ac3": "dolby_digital",
    "dca": "dts",  # DTS core
    "dts": "dts",
    "dts-hd ma": "dts_ma",
    "dts-hd hra": "dts_hr",
    "aac": "aac",
    "flac": "flac",
    "pcm": "lpcm",
    "mp3": "mp3",
    "mp2": "mp2",
    "vorbis": "ogg",
    "opus": "ogg_opus",
    "wma": "wma",
}

# Audio codec ranking (higher = better)
AUDIO_RANK = {
    "dolby_atmos": 7,
    "dolby_digital_plus_atmos": 6,
    "dts_x": 6,
    "dolby_truehd": 5,
    "dts_ma": 5,
    "dts_hr": 4,
    "dolby_digital_plus": 3,
    "dts": 3,
    "dolby_digital": 2,
    "flac": 2,
    "lpcm": 2,
    "aac": 1,
    "mp3": 0,
}

# Channel count to Trakt format
CHANNELS_MAP = {
    1: "1.0",
    2: "2.0",
    3: "2.1",
    6: "5.1",
    7: "6.1",
    8: "7.1",
}


def _get_video_stream(media) -> object | None:
    """Get the primary video stream from media."""
    try:
        for part in media.parts:
            for stream in part.streams:
                if stream.streamType == 1:  # Video stream
                    return stream
    except (AttributeError, TypeError):
        pass
    return None


def _get_audio_stream(media) -> object | None:
    """Get the primary audio stream from media."""
    try:
        for part in media.parts:
            for stream in part.streams:
                if stream.streamType == 2:  # Audio stream
                    return stream
    except (AttributeError, TypeError):
        pass
    return None


def _detect_hdr_type(video_stream) -> str | None:
    """Detect HDR type from video stream attributes."""
    if video_stream is None:
        return None

    # Check for Dolby Vision
    if getattr(video_stream, "DOVIPresent", False):
        return "dolby_vision"

    # Check colorTrc for HDR format
    color_trc = getattr(video_stream, "colorTrc", None)
    if color_trc:
        if color_trc == "smpte2084":
            # Could be HDR10 or HDR10+ - check displayTitle for HDR10+
            display_title = getattr(video_stream, "displayTitle", "") or ""
            if "HDR10+" in display_title or "HDR10Plus" in display_title.replace(" ", ""):
                return "hdr10_plus"
            return "hdr10"
        elif color_trc == "arib-std-b67":
            return "hlg"

    return None


def _detect_audio_codec(media, audio_stream) -> str | None:
    """Detect audio codec and check for Atmos/DTS:X."""
    codec = getattr(media, "audioCodec", None)
    if not codec:
        return None

    codec = codec.lower()

    # Check for Atmos in the audio stream
    if audio_stream:
        display_title = getattr(audio_stream, "displayTitle", "") or ""
        extended_display_title = getattr(audio_stream, "extendedDisplayTitle", "") or ""
        combined = f"{display_title} {extended_display_title}".lower()

        if "atmos" in combined:
            if codec == "truehd":
                return "dolby_atmos"
            elif codec == "eac3":
                return "dolby_digital_plus_atmos"

        if "dts:x" in combined or "dts-x" in combined:
            return "dts_x"

    # Fall back to standard codec mapping
    return AUDIO_CODEC_MAP.get(codec)


def _detect_audio_channels(media, audio_stream) -> str | None:
    """Detect audio channel configuration."""
    channels = getattr(media, "audioChannels", None)
    if not channels:
        return None

    # Check for Atmos height channels in stream layout
    if audio_stream:
        channel_layout = getattr(audio_stream, "audioChannelLayout", "") or ""
        display_title = getattr(audio_stream, "displayTitle", "") or ""
        combined = f"{channel_layout} {display_title}".lower()

        # Detect object-based audio with height channels
        if "atmos" in combined or "dts:x" in combined:
            if channels >= 8:
                if "7.1.4" in combined:
                    return "7.1.4"
                elif "7.1.2" in combined:
                    return "7.1.2"
                return "7.1"
            elif channels >= 6:
                if "5.1.4" in combined:
                    return "5.1.4"
                elif "5.1.2" in combined:
                    return "5.1.2"
                return "5.1"

    # Standard channel mapping
    return CHANNELS_MAP.get(channels)


def _score_media(media) -> int:
    """Score a media version for quality comparison. Higher = better."""
    score = 0

    # Resolution score (0-5000)
    resolution = getattr(media, "videoResolution", None)
    if resolution:
        trakt_res = RESOLUTION_MAP.get(resolution.lower(), "sd_480p")
        score += RESOLUTION_RANK.get(trakt_res, 1) * 1000

    # HDR score (0-400)
    video_stream = _get_video_stream(media)
    hdr_type = _detect_hdr_type(video_stream)
    if hdr_type:
        score += HDR_RANK.get(hdr_type, 0) * 100

    # Audio score (0-70)
    audio_stream = _get_audio_stream(media)
    audio_codec = _detect_audio_codec(media, audio_stream)
    if audio_codec:
        score += AUDIO_RANK.get(audio_codec, 0) * 10

    # Channels score (0-8)
    channels = getattr(media, "audioChannels", 0) or 0
    score += min(channels, 8)

    return score


def _best_media(item: Movie | Episode) -> tuple[int, Any]:
    """Highest scoring media version of an item with its score, or (-1, None) when it has none."""
    best: tuple[int, Any] = (-1, None)
    for media in getattr(item, "media", None) or []:
        score = _score_media(media)
        if score > best[0]:
            best = (score, media)
    return best


def media_score(item: Movie | Episode) -> int:
    """Quality score of an item's best media version, -1 when it has none."""
    return _best_media(item)[0]


def extract_media_metadata(item: Movie | Episode) -> dict:
    """Extract metadata of the item's best media version for the Trakt collection."""
    best_media = _best_media(item)[1]
    if best_media is None:
        return {}

    metadata: dict = {"media_type": "digital"}

    # Resolution
    resolution = getattr(best_media, "videoResolution", None)
    if resolution:
        trakt_res = RESOLUTION_MAP.get(resolution.lower())
        if trakt_res:
            metadata["resolution"] = trakt_res

    # HDR
    video_stream = _get_video_stream(best_media)
    hdr_type = _detect_hdr_type(video_stream)
    if hdr_type:
        metadata["hdr"] = hdr_type

    # Audio codec
    audio_stream = _get_audio_stream(best_media)
    audio_codec = _detect_audio_codec(best_media, audio_stream)
    if audio_codec:
        metadata["audio"] = audio_codec

    # Audio channels
    audio_channels = _detect_audio_channels(best_media, audio_stream)
    if audio_channels:
        metadata["audio_channels"] = audio_channels

    return metadata


def extract_plex_ids(item: Movie | Show | Episode) -> PlexIds:
    """Extract IDs from a Plex item, falling back to the legacy agent GUID when it lists none."""
    plex_id = PlexIds(plex=str(item.ratingKey), guid=item.guid)

    # Parse GUIDs for external IDs
    for guid in getattr(item, "guids", []):
        guid_str = str(guid.id)
        if guid_str.startswith("imdb://"):
            plex_id.imdb = guid_str.replace("imdb://", "")
        elif guid_str.startswith("tmdb://"):
            try:
                plex_id.tmdb = int(guid_str.replace("tmdb://", ""))
            except ValueError:
                pass
        elif guid_str.startswith("tvdb://"):
            try:
                plex_id.tvdb = int(guid_str.replace("tvdb://", ""))
            except ValueError:
                pass

    legacy = LEGACY_GUID.match(item.guid or "")
    if legacy and not (plex_id.imdb or plex_id.tmdb or plex_id.tvdb):
        agent, value = legacy.groups()
        if agent == "imdb":
            plex_id.imdb = value
        elif value.isdigit():
            setattr(plex_id, "tmdb" if agent == "themoviedb" else "tvdb", int(value))

    return plex_id

