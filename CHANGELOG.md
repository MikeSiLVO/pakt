# Changelog

## [0.3.1] - 2026-02-14

### Fixed
- **Rating conflict resolution** - Rating priority (`plex`/`trakt`) now respects sync direction settings instead of always overwriting
- **Path traversal guard** - Asset endpoint validates resolved path stays within assets directory
- **PIN login memory leak** - Abandoned Plex PIN logins now expire after 10 minutes
- **File handle leak** - devnull handle in silent serve mode now properly closed on shutdown

### Removed
- **`rating_priority: "newest"`** - Removed unimplemented option; use `"none"`, `"plex"`, or `"trakt"`
- **`run_on_startup` scheduler setting** - Removed unused config field

### Docs
- Updated CLI reference with missing sync flags (`--collection-only`, `--no-movies`, `--no-shows`, `--fix-collection-dates`)
- Fixed stale version in `__init__.py` and FastAPI app

## [0.3.0] - 2026-02-12

### Added
- **Collection dates** - Collection sync now sends `collected_at` timestamps from Plex's `addedAt` field instead of using the sync date
- **Fix collection dates** - `pakt sync --fix-collection-dates` re-sends all collection items to update dates on Trakt
- **Sync filter flags** - `--collection-only`, `--no-movies`, `--no-shows` to control which sync phases run
- **Rating priority** - New `rating_priority` config setting (`none`/`plex`/`trakt`) to resolve conflicts when both sides have different ratings
- **Trakt 5xx retry** - Transient Trakt server errors (502/503/504) are now retried with exponential backoff

### Fixed
- **Watchlist sync crash** - Variable shadowing in Plex Discover search caused `AttributeError` when adding items to Plex watchlist
- **Verbose watchlist crash** - `.get()` called on PlexAPI objects instead of `getattr()` in verbose logging
- **Empty episode library crash** - `ZeroDivisionError` when show libraries exist but contain no episodes
- **Config cache** - Web API now invalidates config cache after saves
- **Log file missing dates** - Rich markup stripping was removing bracketed dates like `[2019-06-17]` from log file
- **Silent phase skips** - Disabled sync phases (collection, watchlist) now log skip reason instead of producing no output

## [0.2.2] - 2025-01-21

### Added
- **Docker support** - Dockerfile and docker-compose.yml for containerized deployment
- **Configurable port** - Web UI port now configurable via config file or `--port` flag (default: 7258)
- **Documentation** - Comprehensive docs for CLI, configuration, Docker, troubleshooting, and automation

### Fixed
- **Pythonw tray mode** - Fixed `serve --tray` under pythonw on Windows (silent mode, port conflict handling)

### Changed
- **Token refresh** - Improved Trakt OAuth token refresh with better error handling and retry logic

## [0.2.1] - 2025-01-20

### Fixed
- README changelog link now works on PyPI

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
