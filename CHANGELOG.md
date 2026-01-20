# Changelog

## [0.2.0] - 2025-01-20

### Added
- **Multi-server support** - Sync multiple Plex servers to a single Trakt account
- **Plex PIN authentication** - `pakt setup` now uses plex.tv/link PIN flow (use `--token` for manual entry)
- **Server management CLI** - New `pakt servers` command group:
  - `discover` - List available servers from your Plex account
  - `list` - Show configured servers
  - `add/remove` - Add or remove servers
  - `enable/disable` - Toggle servers without removing
  - `test` - Test server connection
- **Per-server configuration** - Each server can have independent library selection and sync option overrides
- **Server selection for sync** - `pakt sync --server NAME` to sync specific servers only
- **Deduplication** - Items on multiple servers are synced once (by Trakt ID)

### Performance
- **Significant speedup for remote Plex servers** - Disabled PlexAPI auto-reload to prevent unnecessary network calls during attribute access

### Changed
- Config now stored in `config.json` instead of `.env` file (auto-migrated on first run)
- Phase timing logged at end of each sync phase

## [0.1.1] - 2025-01-19

### Added
- Initial PyPI release
- Multi-server support
- Web UI with sync, stats, and settings
- System tray support (Windows)
- Scheduled sync via APScheduler

### Sync Features
- Watched status (bidirectional)
- Ratings (bidirectional)
- Collection sync (Plex → Trakt) with media metadata
- Watchlist sync (bidirectional)
