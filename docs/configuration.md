# Configuration Reference

Complete reference for Pakt's `config.json` settings. See also the main [README](https://github.com/MikeSiLVO/Pakt#readme) for quick start and overview.

## Config Location

- **Windows:** `%APPDATA%\pakt\config.json`
- **Linux/macOS:** `~/.config/pakt/config.json`

## Full Example

```json
{
  "web": {
    "host": "127.0.0.1",
    "port": 7258
  },
  "sync": {
    "watched_plex_to_trakt": true,
    "watched_trakt_to_plex": true,
    "ratings_plex_to_trakt": true,
    "ratings_trakt_to_plex": true,
    "collection_plex_to_trakt": false,
    "watchlist_plex_to_trakt": false,
    "watchlist_trakt_to_plex": false,
    "rating_priority": "newest"
  },
  "scheduler": {
    "enabled": false,
    "interval_hours": 24,
    "run_on_startup": false
  },
  "plex_token": "your-plex-account-token",
  "servers": [
    {
      "name": "MyServer",
      "server_name": "MyServer",
      "url": "http://192.168.1.100:32400",
      "token": "server-token",
      "enabled": true,
      "movie_libraries": [],
      "show_libraries": [],
      "sync": null
    }
  ],
  "trakt": {
    "client_id": "...",
    "client_secret": "...",
    "access_token": "...",
    "refresh_token": "...",
    "expires_at": 1234567890
  }
}
```

---

## Sections

### `web`

Web server settings for `pakt serve`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `host` | string | `"127.0.0.1"` | IP address to bind to. Use `"0.0.0.0"` to allow external access. |
| `port` | integer | `7258` | Port number. Default is "PAKT" on phone keypad. |

---

### `sync`

Global sync behavior. These can be overridden per-server.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `watched_plex_to_trakt` | boolean | `true` | Sync watched status from Plex to Trakt |
| `watched_trakt_to_plex` | boolean | `true` | Sync watched status from Trakt to Plex |
| `ratings_plex_to_trakt` | boolean | `true` | Sync ratings from Plex to Trakt |
| `ratings_trakt_to_plex` | boolean | `true` | Sync ratings from Trakt to Plex |
| `collection_plex_to_trakt` | boolean | `false` | Sync Plex library to Trakt collection |
| `watchlist_plex_to_trakt` | boolean | `false` | Sync Plex watchlist to Trakt |
| `watchlist_trakt_to_plex` | boolean | `false` | Sync Trakt watchlist to Plex |
| `rating_priority` | string | `"newest"` | Which rating wins on conflict: `"plex"`, `"trakt"`, or `"newest"` |

**Notes:**
- Collection sync includes media metadata (resolution, HDR, audio codec)
- Watchlist sync uses Plex Discover for items not in your library
- Free Trakt accounts have a 100-item limit on collections and watchlists

---

### `scheduler`

Automatic sync scheduling. Only active while `pakt serve` is running.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | boolean | `false` | Enable automatic scheduled syncs |
| `interval_hours` | integer | `0` | Hours between syncs (0 = disabled) |
| `run_on_startup` | boolean | `false` | Run a sync immediately when server starts |

---

### `plex_token`

Your Plex account token, used for server discovery. Set automatically by `pakt setup`.

---

### `servers`

List of configured Plex servers.

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Unique identifier for this server config |
| `server_name` | string | Actual Plex server name (for discovery) |
| `url` | string | Direct URL to server (e.g., `http://192.168.1.100:32400`) |
| `token` | string | Server access token |
| `enabled` | boolean | Whether to include in sync |
| `movie_libraries` | array | Movie libraries to sync (empty = all) |
| `show_libraries` | array | Show libraries to sync (empty = all) |
| `sync` | object | Per-server sync overrides (see below) |

#### Per-Server Sync Overrides

The `sync` field can override global sync settings for a specific server:

```json
{
  "name": "4K Server",
  "sync": {
    "collection_plex_to_trakt": true,
    "watched_trakt_to_plex": false
  }
}
```

Any field set to `true` or `false` overrides the global setting. Fields set to `null` or omitted use the global default.

---

### `trakt`

Trakt API credentials and tokens. Managed automatically by `pakt login`.

| Field | Type | Description |
|-------|------|-------------|
| `client_id` | string | Trakt API client ID (default provided) |
| `client_secret` | string | Trakt API client secret (default provided) |
| `access_token` | string | OAuth access token |
| `refresh_token` | string | OAuth refresh token |
| `expires_at` | integer | Token expiration timestamp |

**Note:** Tokens are refreshed automatically when expired.

---

## Tips

### Sync Only Specific Libraries

Leave `movie_libraries` or `show_libraries` empty to sync all, or specify names:

```json
{
  "name": "MyServer",
  "movie_libraries": ["Movies", "4K Movies"],
  "show_libraries": ["TV Shows"]
}
```

### Different Settings Per Server

Use server-level `sync` overrides for different behavior:

```json
{
  "servers": [
    {
      "name": "Main",
      "sync": null
    },
    {
      "name": "4K Server",
      "sync": {
        "collection_plex_to_trakt": true
      }
    }
  ]
}
```

### Allow External Access

To access the web UI from other devices:

```json
{
  "web": {
    "host": "0.0.0.0",
    "port": 7258
  }
}
```

Then access via your machine's IP address.
