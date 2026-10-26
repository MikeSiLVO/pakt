"""How a Plex item matches Trakt: typed id keys, Trakt's shapes and which way a rating goes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pakt.models import PlexIds

MOVIE_ID_TYPES = ("imdb", "tmdb")
SHOW_ID_TYPES = ("tvdb", "imdb", "tmdb")


def id_keys(ids: PlexIds | dict[str, Any], kinds: tuple[str, ...]) -> list[tuple[str, Any]]:
    """Typed id pairs in order of preference; ids of different kinds never collide."""
    if isinstance(ids, dict):
        values = [ids.get(kind) for kind in kinds]
    else:
        values = [getattr(ids, kind, None) for kind in kinds]
    return [(kind, value) for kind, value in zip(kinds, values) if value]


def trakt_item(item: Any, plex_ids: PlexIds, kinds: tuple[str, ...]) -> dict | None:
    """Trakt movie or show object for a Plex item, or None when it carries none of the ids."""
    ids = dict(id_keys(plex_ids, kinds))
    if not ids:
        return None
    return {"title": item.title, "year": item.year, "ids": ids}


def group_by_show(rows: list[tuple[dict, int, dict, str]]) -> list[dict]:
    """Group episode entries under their show and season, the shape Trakt's sync endpoints take."""
    shows: dict[tuple, dict] = {}
    for show, season, episode, _label in rows:
        entry = shows.setdefault(tuple(sorted(show["ids"].items())), {**show, "seasons": {}})
        entry["seasons"].setdefault(season, []).append(episode)
    return [
        {**show, "seasons": [{"number": n, "episodes": eps} for n, eps in sorted(show["seasons"].items())]}
        for show in shows.values()
    ]


def rating_direction(
    plex: int | None, trakt: int | None, priority: str, to_trakt: bool, to_plex: bool
) -> tuple[bool, bool]:
    """Which way a rating should flow, as (send Plex's to Trakt, send Trakt's to Plex)."""
    if plex and not trakt:
        return to_trakt, False
    if trakt and not plex:
        return False, to_plex
    if plex and trakt and plex != trakt:
        return priority == "plex" and to_trakt, priority == "trakt" and to_plex
    return False, False


def plex_dt_to_utc_iso(dt: datetime | None) -> str | None:
    """Convert PlexAPI's naive local datetime to the UTC ISO 8601 form Trakt takes."""
    if dt is None:
        return None
    # PlexAPI gives naive local time; astimezone() assumes local if no tzinfo
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
