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
pakt setup    # Interactive setup wizard
pakt sync     # Run sync
```

## Commands

```bash
pakt setup              # Configure Plex and Trakt
pakt sync               # Sync everything
pakt sync --dry-run     # Preview without changes
pakt sync --verbose     # Show detailed item list
pakt serve              # Start web interface
pakt serve --tray       # Web interface with system tray (Windows)
pakt status             # Show configuration
pakt libraries          # Manage library selection
pakt login              # Authenticate with Trakt
pakt logout             # Clear Trakt authentication
```

## Web Interface

Start the web UI:
```bash
pakt serve
```

Open http://localhost:8080 in your browser.

Features:
- Run syncs with progress display
- Configure sync options
- View Trakt account status
- Set up scheduled syncs
- Select which libraries to sync

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

## Configuration

Config location:
- Windows: `%APPDATA%\pakt`
- Linux/macOS: `~/.config/pakt`

## Trakt Account Limits

Free Trakt accounts have a 100-item limit on collections and watchlists. Pakt will warn you if you hit these limits. Upgrade to [Trakt VIP](https://trakt.tv/vip) for unlimited.

## License

MIT
