"""What a Plex item carries: the quality of its media versions and its external ids."""

from __future__ import annotations

import re
from typing import Any

from plexapi.video import Episode, Movie, Show

from pakt.models import PlexIds

LEGACY_GUID = re.compile(r"^com\.plexapp\.agents\.(imdb|themoviedb|thetvdb)://([^/?]+)")
VIDEO_STREAM = 1
AUDIO_STREAM = 2

RESOLUTION_MAP = {
    "4k": "uhd_4k",
    "1080": "hd_1080p",
    "720": "hd_720p",
    "576": "sd_576p",
    "480": "sd_480p",
    "sd": "sd_480p",
}

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

HDR_RANK = {
    "dolby_vision": 4,
    "hdr10_plus": 3,
    "hdr10": 2,
    "hlg": 1,
}

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
                if stream.streamType == VIDEO_STREAM:
                    return stream
    except (AttributeError, TypeError):
        pass
    return None


def _get_audio_stream(media) -> object | None:
    """Get the primary audio stream from media."""
    try:
        for part in media.parts:
            for stream in part.streams:
                if stream.streamType == AUDIO_STREAM:
                    return stream
    except (AttributeError, TypeError):
        pass
    return None


def _detect_hdr_type(video_stream) -> str | None:
    """Detect HDR type from video stream attributes."""
    if video_stream is None:
        return None

    if getattr(video_stream, "DOVIPresent", False):
        return "dolby_vision"

    color_trc = getattr(video_stream, "colorTrc", None)
    if color_trc:
        if color_trc == "smpte2084":
            # HDR10 and HDR10+ share this transfer; only the title tells them apart
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

    return AUDIO_CODEC_MAP.get(codec)


def _detect_audio_channels(media, audio_stream) -> str | None:
    """Detect audio channel layout, including Atmos and DTS:X height channels."""
    channels = getattr(media, "audioChannels", None)
    if not channels:
        return None

    if audio_stream:
        channel_layout = getattr(audio_stream, "audioChannelLayout", "") or ""
        display_title = getattr(audio_stream, "displayTitle", "") or ""
        combined = f"{channel_layout} {display_title}".lower()

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

    return CHANNELS_MAP.get(channels)


def _score_media(media) -> int:
    """Score a media version for quality comparison, higher being better."""
    score = 0

    # weights keep resolution ahead of HDR, HDR ahead of audio, audio ahead of channels
    resolution = getattr(media, "videoResolution", None)
    if resolution:
        trakt_res = RESOLUTION_MAP.get(resolution.lower(), "sd_480p")
        score += RESOLUTION_RANK.get(trakt_res, 1) * 1000

    video_stream = _get_video_stream(media)
    hdr_type = _detect_hdr_type(video_stream)
    if hdr_type:
        score += HDR_RANK.get(hdr_type, 0) * 100

    audio_stream = _get_audio_stream(media)
    audio_codec = _detect_audio_codec(media, audio_stream)
    if audio_codec:
        score += AUDIO_RANK.get(audio_codec, 0) * 10

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

    resolution = getattr(best_media, "videoResolution", None)
    if resolution:
        trakt_res = RESOLUTION_MAP.get(resolution.lower())
        if trakt_res:
            metadata["resolution"] = trakt_res

    video_stream = _get_video_stream(best_media)
    hdr_type = _detect_hdr_type(video_stream)
    if hdr_type:
        metadata["hdr"] = hdr_type

    audio_stream = _get_audio_stream(best_media)
    audio_codec = _detect_audio_codec(best_media, audio_stream)
    if audio_codec:
        metadata["audio"] = audio_codec

    audio_channels = _detect_audio_channels(best_media, audio_stream)
    if audio_channels:
        metadata["audio_channels"] = audio_channels

    return metadata


def extract_plex_ids(item: Movie | Show | Episode) -> PlexIds:
    """Extract IDs from a Plex item, falling back to the legacy agent GUID when it lists none."""
    plex_id = PlexIds(plex=str(item.ratingKey), guid=item.guid)

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
