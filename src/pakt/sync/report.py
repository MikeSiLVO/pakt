"""Log text for a sync: counts, change summaries, durations, item lists and the closing line."""

from __future__ import annotations

import logging

from pakt.models import SyncResult

logger = logging.getLogger(__name__)

MAX_LISTED = 20


def item(text: str) -> None:
    """Log one item of a list, indented under the line before it."""
    logger.info(text, extra={"item": True})


def items(lines: list[str]) -> None:
    """Log a list's items, the first MAX_LISTED and a count of the rest."""
    for line in lines[:MAX_LISTED]:
        item(line)
    if len(lines) > MAX_LISTED:
        item(f"and {len(lines) - MAX_LISTED} more")


def plural(count: int, noun: str) -> str:
    """Count with its noun, pluralized with an s."""
    return f"{count} {noun}{'' if count == 1 else 's'}"


def changes(trakt: dict[str, int], plex: dict[str, int]) -> str:
    """Describe changes per side, such as 'Trakt +2 watched, +1 rated; Plex +3 watched'."""
    sides = []
    for name, counts in (("Trakt", trakt), ("Plex", plex)):
        parts = [f"+{count} {what}" for what, count in counts.items() if count]
        if parts:
            sides.append(f"{name} {', '.join(parts)}")
    return "; ".join(sides) or "no changes"


def counted(counts: dict[str, int] | None) -> str:
    """Describe a Trakt response's counts, such as '2 movies, 1 episode', or 'nothing'."""
    parts = [plural(count, kind.removesuffix("s")) for kind, count in (counts or {}).items() if count]
    return ", ".join(parts) or "nothing"


def title(entry: dict) -> str:
    """Title and year of a Trakt object, or its ids when it has no title."""
    if not entry.get("title"):
        return str(entry.get("ids", entry))
    return f"{entry['title']} ({entry['year']})" if entry.get("year") else entry["title"]


def duration(seconds: float) -> str:
    """Duration as seconds under a minute, else minutes and seconds."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m{rest:02d}s"


def direction(to_trakt: bool, to_plex: bool) -> str:
    """Name which way a kind of data syncs."""
    if to_trakt and to_plex:
        return "both ways"
    return "to Trakt" if to_trakt else "to Plex" if to_plex else "off"


def summary(result: SyncResult, synced: int, server_count: int, dry_run: bool, cancelled: bool) -> None:
    """Log the line that closes a run: its duration, how many servers synced and what changed."""
    took = duration(result.duration_seconds)
    servers = f"{synced} of {plural(server_count, 'server')}"
    made = changes(
        {"watched": result.added_to_trakt, "rated": result.ratings_to_trakt, "collected": result.collection_added,
         "updated": result.collection_updated, "watchlisted": result.watchlist_added_trakt},
        {"watched": result.added_to_plex, "rated": result.ratings_to_plex, "watchlisted": result.watchlist_added_plex},
    )
    if cancelled:
        logger.warning(f"Sync cancelled after {took}, {servers}: {made}")
    elif result.errors:
        logger.error(f"Sync finished in {took} with {plural(len(result.errors), 'error')}, {servers}: {made}")
    elif dry_run:
        logger.info(f"Dry run finished in {took}, {servers}; nothing was changed", extra={"success": True})
    else:
        logger.info(f"Sync finished in {took}, {servers}: {made}", extra={"success": True})
