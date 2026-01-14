"""SQLite cache for Pakt."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from pakt.config import CacheConfig, get_cache_dir


class Cache:
    """Async SQLite cache for API responses."""

    def __init__(self, config: CacheConfig | None = None, db_path: Path | None = None):
        self.config = config or CacheConfig()
        self.db_path = db_path or (get_cache_dir() / "pakt_cache.db")
        self._db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> Cache:
        await self.connect()
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()

    async def connect(self) -> None:
        """Connect to the database and ensure tables exist."""
        self._db = await aiosqlite.connect(self.db_path)
        await self._create_tables()

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def _create_tables(self) -> None:
        """Create cache tables if they don't exist."""
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS id_mappings (
                external_type TEXT NOT NULL,
                external_id TEXT NOT NULL,
                media_type TEXT NOT NULL,
                trakt_id INTEGER,
                data TEXT,
                created_at INTEGER NOT NULL,
                expires_at INTEGER,
                PRIMARY KEY (external_type, external_id, media_type)
            )
        """)

        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS watched_cache (
                trakt_id INTEGER PRIMARY KEY,
                media_type TEXT NOT NULL,
                data TEXT,
                updated_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
        """)

        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS ratings_cache (
                trakt_id INTEGER PRIMARY KEY,
                media_type TEXT NOT NULL,
                rating INTEGER NOT NULL,
                rated_at INTEGER,
                updated_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )
        """)

        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at INTEGER NOT NULL
            )
        """)

        await self._db.commit()

    # =========================================================================
    # ID Mappings (external ID -> Trakt ID) - Long TTL
    # =========================================================================

    async def get_trakt_id(
        self,
        external_type: str,
        external_id: str,
        media_type: str,
    ) -> int | None:
        """Get cached Trakt ID for an external ID."""
        now = int(time.time())
        cursor = await self._db.execute(
            """
            SELECT trakt_id FROM id_mappings
            WHERE external_type = ? AND external_id = ? AND media_type = ?
            AND (expires_at IS NULL OR expires_at > ?)
            """,
            (external_type, external_id, media_type, now),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_trakt_id(
        self,
        external_type: str,
        external_id: str,
        media_type: str,
        trakt_id: int,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Cache a Trakt ID mapping. IDs effectively never expire."""
        now = int(time.time())
        # IDs don't change, so set very long expiration (10 years)
        expires_at = now + (10 * 365 * 24 * 3600)

        await self._db.execute(
            """
            INSERT OR REPLACE INTO id_mappings
            (external_type, external_id, media_type, trakt_id, data, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                external_type,
                external_id,
                media_type,
                trakt_id,
                json.dumps(data) if data else None,
                now,
                expires_at,
            ),
        )
        await self._db.commit()

    async def get_id_mapping_data(
        self,
        external_type: str,
        external_id: str,
        media_type: str,
    ) -> dict[str, Any] | None:
        """Get full cached data for an external ID."""
        cursor = await self._db.execute(
            """
            SELECT data FROM id_mappings
            WHERE external_type = ? AND external_id = ? AND media_type = ?
            """,
            (external_type, external_id, media_type),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return json.loads(row[0])
        return None

    # =========================================================================
    # Watched Status Cache - Short TTL
    # =========================================================================

    async def get_watched(self, trakt_id: int) -> dict[str, Any] | None:
        """Get cached watched status."""
        now = int(time.time())
        cursor = await self._db.execute(
            """
            SELECT data FROM watched_cache
            WHERE trakt_id = ? AND expires_at > ?
            """,
            (trakt_id, now),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return json.loads(row[0])
        return None

    async def set_watched(
        self,
        trakt_id: int,
        media_type: str,
        data: dict[str, Any],
    ) -> None:
        """Cache watched status."""
        now = int(time.time())
        expires_at = now + self.config.watched_ttl

        await self._db.execute(
            """
            INSERT OR REPLACE INTO watched_cache
            (trakt_id, media_type, data, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (trakt_id, media_type, json.dumps(data), now, expires_at),
        )
        await self._db.commit()

    async def bulk_set_watched(
        self,
        items: list[tuple[int, str, dict[str, Any]]],
    ) -> None:
        """Bulk cache watched status."""
        now = int(time.time())
        expires_at = now + self.config.watched_ttl

        await self._db.executemany(
            """
            INSERT OR REPLACE INTO watched_cache
            (trakt_id, media_type, data, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(tid, mt, json.dumps(d), now, expires_at) for tid, mt, d in items],
        )
        await self._db.commit()

    # =========================================================================
    # Ratings Cache - Short TTL
    # =========================================================================

    async def get_rating(self, trakt_id: int) -> tuple[int, int] | None:
        """Get cached rating. Returns (rating, rated_at) or None."""
        now = int(time.time())
        cursor = await self._db.execute(
            """
            SELECT rating, rated_at FROM ratings_cache
            WHERE trakt_id = ? AND expires_at > ?
            """,
            (trakt_id, now),
        )
        row = await cursor.fetchone()
        return (row[0], row[1]) if row else None

    async def set_rating(
        self,
        trakt_id: int,
        media_type: str,
        rating: int,
        rated_at: int | None = None,
    ) -> None:
        """Cache a rating."""
        now = int(time.time())
        expires_at = now + self.config.ratings_ttl

        await self._db.execute(
            """
            INSERT OR REPLACE INTO ratings_cache
            (trakt_id, media_type, rating, rated_at, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (trakt_id, media_type, rating, rated_at, now, expires_at),
        )
        await self._db.commit()

    async def bulk_set_ratings(
        self,
        items: list[tuple[int, str, int, int | None]],
    ) -> None:
        """Bulk cache ratings."""
        now = int(time.time())
        expires_at = now + self.config.ratings_ttl

        await self._db.executemany(
            """
            INSERT OR REPLACE INTO ratings_cache
            (trakt_id, media_type, rating, rated_at, updated_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [(tid, mt, r, ra, now, expires_at) for tid, mt, r, ra in items],
        )
        await self._db.commit()

    # =========================================================================
    # Sync State - For resume capability
    # =========================================================================

    async def get_sync_state(self, key: str) -> str | None:
        """Get a sync state value."""
        cursor = await self._db.execute(
            "SELECT value FROM sync_state WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_sync_state(self, key: str, value: str) -> None:
        """Set a sync state value."""
        now = int(time.time())
        await self._db.execute(
            """
            INSERT OR REPLACE INTO sync_state (key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            (key, value, now),
        )
        await self._db.commit()

    async def clear_sync_state(self) -> None:
        """Clear all sync state."""
        await self._db.execute("DELETE FROM sync_state")
        await self._db.commit()

    # =========================================================================
    # Maintenance
    # =========================================================================

    async def clear_expired(self) -> int:
        """Remove expired cache entries. Returns count removed."""
        now = int(time.time())
        total = 0

        cursor = await self._db.execute(
            "DELETE FROM id_mappings WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        total += cursor.rowcount

        cursor = await self._db.execute(
            "DELETE FROM watched_cache WHERE expires_at < ?",
            (now,),
        )
        total += cursor.rowcount

        cursor = await self._db.execute(
            "DELETE FROM ratings_cache WHERE expires_at < ?",
            (now,),
        )
        total += cursor.rowcount

        await self._db.commit()
        return total

    async def get_stats(self) -> dict[str, int]:
        """Get cache statistics."""
        stats = {}

        cursor = await self._db.execute("SELECT COUNT(*) FROM id_mappings")
        stats["id_mappings"] = (await cursor.fetchone())[0]

        cursor = await self._db.execute("SELECT COUNT(*) FROM watched_cache")
        stats["watched_cache"] = (await cursor.fetchone())[0]

        cursor = await self._db.execute("SELECT COUNT(*) FROM ratings_cache")
        stats["ratings_cache"] = (await cursor.fetchone())[0]

        return stats
