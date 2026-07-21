# Changelog

## [0.4.0] - 2026-07-21

### Fixed
- **Trakt watched endpoints now paginate** - Trakt began enforcing pagination on `/sync/watched/*` on 30 June 2026. Watched movies were silently capped at the first 100 items, and watched shows came back with no season data at all, which stopped episode sync working entirely. Both endpoints now walk every page.
- **Web UI returned HTTP 500 on a fresh install** - starlette 1.x removed the `TemplateResponse` call signature the dashboard used
- **Watch and rating dates** - Items sent to Trakt now carry `watched_at` and `rated_at` taken from Plex, instead of Trakt defaulting them to the moment the sync ran
- **Episodes wrongly marked watched in Plex** - Trakt progress responses can list unwatched episodes, which were being treated as watched. Episode sync also stops now rather than acting on an empty index
- **Scheduler could not be enabled from the web UI** - Reading a queued job's next run time before the scheduler started raised an error
- **Config could be replaced with defaults** - An unreadable `config.json` fell back to defaults which the next save wrote over the top, losing tokens and servers
- **Trakt token refresh could revert a settings change** - Refreshed tokens are now written without flushing a stale config snapshot
- **Two syncs could run at once** - The web UI claimed the sync slot too late, so a second tab or a scheduled run could start alongside the first
- **Duplicate libraries only half synced** - With the same title in two libraries, only the first copy was written back to Plex
- **Repeated writes across servers** - A second Plex server could re-send items the first had already pushed to Trakt
- **Web UI froze mid-sync** - Applying changes and the whole watchlist phase blocked the event loop, so progress stalled and cancel did nothing
- **Ctrl+C did not cancel a running sync** - The handler was being replaced by uvicorn's own
- **Tray "Sync Now" did nothing** - The callback was never wired up, and "Next sync" never appeared
- **Server names containing quotes broke the settings buttons**
- **Dropped connections are retried** - Only HTTP errors were retried before, and a rate limit on the final attempt waited before failing anyway
- **A scheduler interval of 0 left the previous job running**

### Added
- **CI checks** - ruff and pyright run on push and pull request

### Changed
- **config.json** - Written atomically and with `0600` permissions on Linux and macOS. An unreadable config is moved aside to `config.json.corrupt` rather than overwritten
- **Sync log polling** - The web UI now requests only new lines instead of the whole log four times a second

### Removed
- Unused public helpers, a breaking change only if you import pakt as a library: `MediaItem`, `MediaType`, `TraktIds`, `extract_trakt_ids`, the Plex to `MediaItem` converters, and the per-show `get_watched_episodes` / `iter_*_by_library` methods

## [0.3.2] - 2026-03-05

### Fixed
- **Plex 401 Unauthorized** - Prefer plex.tv discovery over direct URL+token connection, since account tokens don't work for plex.direct URLs
- **Stale connection details** - Persist negotiated server URL and access token to config after discovery, keeping direct fallback current across PMS updates and IP changes

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
