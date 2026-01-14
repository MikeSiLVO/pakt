# Pakt

Plex-Trakt sync tool using batch API operations.

## Installation

```bash
pip install pakt
```

Or with pipx:

```bash
pipx install pakt
```

## Usage

```bash
# Authenticate with Trakt
pakt login

# Configure Plex connection
pakt setup

# Run sync
pakt sync

# Preview changes without applying
pakt sync --dry-run

# View configuration and cache status
pakt status

# Clear expired cache entries
pakt clear-cache
```

## Configuration

Configuration is stored in `~/.config/pakt/.env`:

```bash
TRAKT_CLIENT_ID=your_client_id
TRAKT_CLIENT_SECRET=your_client_secret
PLEX_URL=http://localhost:32400
PLEX_TOKEN=your_plex_token
```

Create Trakt API credentials at: https://trakt.tv/oauth/applications

## Features

- Bidirectional sync (Plex ↔ Trakt)
- Watched status sync
- Rating sync
- SQLite caching
- Async operations
- Dry-run mode

## License

MIT
