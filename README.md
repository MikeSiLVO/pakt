# <img src="src/pakt/assets/icon.png" width="32" alt=""> Pakt

Sync watched status, ratings, and collections between Plex and Trakt.

## Installation

```bash
pip install pakt
```

With system tray support (Windows):
```bash
pip install pakt[tray]
```

## Quick Start

```bash
pakt setup    # Interactive setup wizard (links Plex via PIN)
pakt sync     # Run sync
```

## Commands

### Setup & Authentication

```bash
pakt setup              # Link Plex account via PIN authentication
pakt setup --token      # Manual token entry instead of PIN
pakt login              # Authenticate with Trakt
pakt logout             # Clear Trakt authentication
pakt status             # Show configuration status
```

### Server Management

```bash
pakt servers discover   # List available Plex servers from your account
pakt servers list       # Show configured servers
pakt servers add NAME   # Add server from discovered list
pakt servers add NAME --url URL --token TOKEN  # Add server manually
pakt servers remove NAME
pakt servers enable NAME
pakt servers disable NAME
pakt servers test NAME  # Test server connection
```

### Sync

```bash
pakt sync                    # Sync all enabled servers
pakt sync --server NAME      # Sync specific server(s) only
pakt sync -s NAME -s NAME2   # Sync multiple specific servers
pakt sync --dry-run          # Preview without making changes
pakt sync --verbose          # Show detailed item list
```

### Libraries

```bash
pakt libraries               # Show library selection for default server
pakt libraries --all         # Sync all libraries (clear selection)
pakt libraries -m "Movies"   # Select specific movie library
pakt libraries -s "TV Shows" # Select specific show library
```

### Web Interface

```bash
pakt serve                   # Start web UI at localhost:8080
pakt serve --host 0.0.0.0    # Listen on all interfaces
pakt serve --port 9000       # Use custom port
pakt serve --tray            # With system tray icon (Windows)
pakt serve --no-tray         # Without system tray (default)
```

### Maintenance

```bash
pakt clear-cache             # Clear Trakt API cache
```

## Web Interface

Start the web UI:
```bash
pakt serve
```

Open http://localhost:8080 in your browser.

### Views

**Sync** - Run manual syncs with real-time progress and console output. Use "Dry Run" to preview changes without applying them. Enable "Verbose" to see individual items being synced.

**Stats** - Shows connection status for Trakt and all configured Plex servers, library counts, and last sync results.

**Settings** - Configure sync options, manage servers, and set up scheduling.

### Sync Options

Options are organized by direction:
- **Plex → Trakt**: Watched, Ratings, Collection, Watchlist
- **Trakt → Plex**: Watched, Ratings, Watchlist

Global defaults can be overridden per-server in the Server Settings section.

### Scheduled Sync

Enable automatic syncing at a set interval (in hours).

**Important**: The scheduler only runs while the web server is running. If you stop `pakt serve`, scheduled syncs will not occur. For persistent scheduling, run the server in the background (see Background Mode below).

### Server Settings

Each Plex server has independent configuration:
- **Libraries**: Select which movie/show libraries to sync (empty = all)
- **Sync Options**: Override global defaults with per-server settings (Global/On/Off)

## Background Mode (Windows)

Run without a console window:
```bash
pythonw -m pakt serve --tray
```

## What Gets Synced

| Data | Plex → Trakt | Trakt → Plex |
|------|:------------:|:------------:|
| Watched status | ✓ | ✓ |
| Ratings | ✓ | ✓ |
| Collection | ✓ | - |
| Watchlist | ✓ | ✓ |

Collection sync includes media info (resolution, HDR, audio codec).

## Multi-Server Support

Pakt supports syncing multiple Plex servers to a single Trakt account:

1. Run `pakt setup` to link your Plex account via PIN
2. Select which servers to enable
3. Run `pakt sync` to sync all enabled servers

Each server can have:
- Independent library selection
- Per-server sync option overrides

Servers are synced sequentially. Items that exist on multiple servers are deduplicated by external ID.

## Configuration

Config location:
- Windows: `%APPDATA%\pakt`
- Linux/macOS: `~/.config/pakt`

All configuration is stored in `config.json`.

## Trakt Account Limits

Free Trakt accounts have a 100-item limit on collections and watchlists. Pakt will warn you if you hit these limits. Upgrade to [Trakt VIP](https://trakt.tv/vip) for unlimited.

## Changelog

See [CHANGELOG.md](https://github.com/MikeSiLVO/Pakt/blob/main/CHANGELOG.md) for release history.

## License

MIT
