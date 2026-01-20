"""Plex API client wrapper."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from plexapi.myplex import MyPlexAccount, MyPlexPinLogin
from plexapi.server import PlexServer
from plexapi.video import Episode, Movie, Show

from pakt.config import ServerConfig
from pakt.models import MediaItem, MediaType, PlexIds

logger = logging.getLogger(__name__)


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
    """Start Plex PIN login flow.

    Returns the login object (for polling) and auth info (to display to user).
    """
    login = MyPlexPinLogin()

    # Explicitly trigger PIN fetch if not already done
    # The pin property should call _getCode() but let's be explicit
    if hasattr(login, '_getCode'):
        login._getCode()

    pin_code = getattr(login, '_code', None) or getattr(login, 'pin', None)
    pin_id = getattr(login, '_id', None)

    if not pin_code:
        # Try accessing the pin property which might trigger the fetch
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
    """Check if PIN login has been authorized.

    Returns the permanent account token if authorized, None if still pending.
    The initial PIN login may return a temporary token, so we exchange it
    for a permanent one via MyPlexAccount.
    """
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

    @property
    def has_local_connection(self) -> bool:
        return any(c.get("local") for c in self.connections)

    @property
    def best_connection_url(self) -> str | None:
        """Get the best connection URL (prefer local, non-relay)."""
        # Prefer local non-relay connections
        for conn in self.connections:
            if conn.get("local") and not conn.get("relay"):
                return conn.get("uri")
        # Then non-relay
        for conn in self.connections:
            if not conn.get("relay"):
                return conn.get("uri")
        # Fall back to any connection
        if self.connections:
            return self.connections[0].get("uri")
        return None


def discover_servers(account_token: str) -> list[DiscoveredServer]:
    """Discover all Plex servers accessible with the given account token."""
    account = MyPlexAccount(token=account_token)
    servers = []

    for resource in account.resources():
        # Only include actual servers
        if "server" not in resource.provides:
            continue

        connections = []
        for conn in resource.connections:
            connections.append({
                "uri": conn.uri,
                "local": conn.local,
                "relay": conn.relay,
            })

        servers.append(DiscoveredServer(
            name=resource.name,
            client_identifier=resource.clientIdentifier,
            provides=resource.provides,
            owned=resource.owned,
            connections=connections,
        ))

    return servers


def test_server_connection(account_token: str, server_name: str) -> tuple[bool, str]:
    """Test connection to a specific server.

    Returns (success, message).
    """
    try:
        account = MyPlexAccount(token=account_token)
        resource = account.resource(server_name)
        server = resource.connect()
        return True, f"Connected to {server.friendlyName}"
    except Exception as e:
        return False, str(e)


class PlexClient:
    """Plex API client optimized for batch operations."""

    def __init__(self, server_config: ServerConfig):
        """Initialize client with server configuration."""
        self._url = server_config.url
        self._token = server_config.token
        self._server_name = server_config.server_name
        self.server_config = server_config
        self._server: PlexServer | None = None
        self._account: MyPlexAccount | None = None

    def connect(self) -> None:
        """Connect to Plex server."""
        if self._url and self._token:
            self._server = PlexServer(self._url, self._token)
        elif self._token and self._server_name:
            account = MyPlexAccount(token=self._token)
            self._server = account.resource(self._server_name).connect()
        else:
            raise ValueError("Need either URL+token or token+server_name")

    @property
    def account(self) -> MyPlexAccount:
        """Get MyPlex account for watchlist operations."""
        if self._account is None:
            self._account = MyPlexAccount(token=self._token)
        return self._account

    @property
    def server(self) -> PlexServer:
        if not self._server:
            self.connect()
        return self._server

    def get_movie_libraries(self) -> list[str]:
        """Get all movie library names."""
        return [lib.title for lib in self.server.library.sections() if lib.type == "movie"]

    def get_show_libraries(self) -> list[str]:
        """Get all TV show library names."""
        return [lib.title for lib in self.server.library.sections() if lib.type == "show"]

    def get_all_movies(self, library_names: list[str] | None = None) -> list[Movie]:
        """Get all movies from specified libraries."""
        movies, _ = self.get_all_movies_with_counts(library_names)
        return movies

    def get_all_movies_with_counts(self, library_names: list[str] | None = None) -> tuple[list[Movie], dict[str, int]]:
        """Get all movies from specified libraries with per-library counts."""
        movies = []
        lib_counts: dict[str, int] = {}
        for section in self.server.library.sections():
            if section.type != "movie":
                continue
            if library_names and section.title not in library_names:
                continue
            # Use large container_size to reduce HTTP requests
            section_movies = section.all(container_size=1000)
            _disable_auto_reload(section_movies)
            # Handle duplicate library names by appending count
            key = section.title
            if key in lib_counts:
                i = 2
                while f"{section.title} ({i})" in lib_counts:
                    i += 1
                key = f"{section.title} ({i})"
            lib_counts[key] = len(section_movies)
            movies.extend(section_movies)
        return movies, lib_counts

    def get_all_shows(self, library_names: list[str] | None = None) -> list[Show]:
        """Get all shows from specified libraries."""
        shows, _ = self.get_all_shows_with_counts(library_names)
        return shows

    def get_all_shows_with_counts(self, library_names: list[str] | None = None) -> tuple[list[Show], dict[str, int]]:
        """Get all shows from specified libraries with per-library counts."""
        shows = []
        lib_counts: dict[str, int] = {}
        for section in self.server.library.sections():
            if section.type != "show":
                continue
            if library_names and section.title not in library_names:
                continue
            # Use large container_size to reduce HTTP requests
            section_shows = section.all(container_size=1000)
            _disable_auto_reload(section_shows)
            # Handle duplicate library names
            key = section.title
            if key in lib_counts:
                i = 2
                while f"{section.title} ({i})" in lib_counts:
                    i += 1
                key = f"{section.title} ({i})"
            lib_counts[key] = len(section_shows)
            shows.extend(section_shows)
        return shows, lib_counts

    def get_all_episodes(self, library_names: list[str] | None = None) -> list[Episode]:
        """Get ALL episodes from specified libraries in a single batch per library."""
        episodes = []
        for section in self.server.library.sections():
            if section.type != "show":
                continue
            if library_names and section.title not in library_names:
                continue
            # Batch fetch all episodes - use large container_size to reduce HTTP requests
            section_episodes = section.searchEpisodes(container_size=1000)
            _disable_auto_reload(section_episodes)
            episodes.extend(section_episodes)
        return episodes

    def get_all_episodes_with_counts(
        self, library_names: list[str] | None = None
    ) -> tuple[list[Episode], dict[str, int]]:
        """Get ALL episodes from specified libraries with per-library counts."""
        episodes = []
        lib_counts: dict[str, int] = {}
        for section in self.server.library.sections():
            if section.type != "show":
                continue
            if library_names and section.title not in library_names:
                continue
            # Batch fetch all episodes - use large container_size to reduce HTTP requests
            section_episodes = section.searchEpisodes(container_size=1000)
            _disable_auto_reload(section_episodes)
            # Handle duplicate library names
            key = section.title
            if key in lib_counts:
                i = 2
                while f"{section.title} ({i})" in lib_counts:
                    i += 1
                key = f"{section.title} ({i})"
            lib_counts[key] = len(section_episodes)
            episodes.extend(section_episodes)
        return episodes, lib_counts

    def iter_movies_by_library(self, library_names: list[str] | None = None) -> Iterator[tuple[str, list[Movie]]]:
        """Yield movies one library at a time for memory efficiency."""
        for section in self.server.library.sections():
            if section.type != "movie":
                continue
            if library_names and section.title not in library_names:
                continue
            yield section.title, section.all()

    def iter_episodes_by_library(self, library_names: list[str] | None = None) -> Iterator[tuple[str, list[Episode]]]:
        """Yield episodes one library at a time for memory efficiency."""
        for section in self.server.library.sections():
            if section.type != "show":
                continue
            if library_names and section.title not in library_names:
                continue
            yield section.title, section.searchEpisodes()

    def get_watched_movies(self, library_names: list[str] | None = None) -> list[Movie]:
        """Get all watched movies."""
        movies = []
        for section in self.server.library.sections():
            if section.type != "movie":
                continue
            if library_names and section.title not in library_names:
                continue
            movies.extend(section.search(unwatched=False))
        return movies

    def get_watched_episodes(self, library_names: list[str] | None = None) -> list[Episode]:
        """Get all watched episodes."""
        episodes = []
        for section in self.server.library.sections():
            if section.type != "show":
                continue
            if library_names and section.title not in library_names:
                continue
            # Get all episodes that are watched
            for show in section.all():
                for episode in show.episodes():
                    if episode.isWatched:
                        episodes.append(episode)
        return episodes

    def mark_watched(self, item: Movie | Episode) -> None:
        """Mark an item as watched."""
        item.markWatched()

    def mark_unwatched(self, item: Movie | Episode) -> None:
        """Mark an item as unwatched."""
        item.markUnwatched()

    def set_rating(self, item: Movie | Show | Episode, rating: float) -> None:
        """Set rating for an item (1-10 scale)."""
        item.rate(rating)

    def mark_watched_batch(
        self, items: Sequence[Movie | Episode], max_workers: int = 10
    ) -> list[tuple[Movie | Episode, Exception]]:
        """Mark multiple items as watched concurrently.

        Returns list of (item, error) tuples for any failures.
        """
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
        """Rate multiple items concurrently.

        Args:
            items: List of (item, rating) tuples

        Returns list of (item, rating, error) tuples for any failures.
        """
        if not items:
            return []

        failed: list[tuple[Movie | Show | Episode, int | float, Exception]] = []

        def rate_item(pair: tuple[Movie | Show | Episode, int | float]) -> None:
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

    def add_to_watchlist(self, item: Movie | Show) -> None:
        """Add item to account watchlist."""
        self.account.addToWatchlist(item)

    def remove_from_watchlist(self, item: Movie | Show) -> None:
        """Remove item from account watchlist."""
        self.account.removeFromWatchlist(item)

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


def extract_media_metadata(item: Movie | Episode) -> dict:
    """Extract best quality media metadata for Trakt collection.

    Examines all media versions and returns metadata for the best quality one.
    """
    if not hasattr(item, "media") or not item.media:
        return {}

    # Find best quality media version
    best_media = None
    best_score = -1
    for media in item.media:
        score = _score_media(media)
        if score > best_score:
            best_score = score
            best_media = media

    if not best_media:
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
    """Extract IDs from a Plex item."""
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

    return plex_id


def plex_movie_to_media_item(movie: Movie) -> MediaItem:
    """Convert Plex movie to MediaItem."""
    plex_ids = extract_plex_ids(movie)

    return MediaItem(
        title=movie.title,
        year=movie.year,
        media_type=MediaType.MOVIE,
        plex_ids=plex_ids,
        watched=movie.isWatched,
        watched_at=movie.lastViewedAt,
        plays=movie.viewCount or 0,
        rating=int(movie.userRating) if movie.userRating else None,
    )


def plex_episode_to_media_item(episode: Episode) -> MediaItem:
    """Convert Plex episode to MediaItem."""
    plex_ids = extract_plex_ids(episode)

    return MediaItem(
        title=episode.title,
        year=episode.year,
        media_type=MediaType.EPISODE,
        plex_ids=plex_ids,
        watched=episode.isWatched,
        watched_at=episode.lastViewedAt,
        plays=episode.viewCount or 0,
        rating=int(episode.userRating) if episode.userRating else None,
        show_title=episode.grandparentTitle,
        season=episode.seasonNumber,
        episode=episode.episodeNumber,
    )
