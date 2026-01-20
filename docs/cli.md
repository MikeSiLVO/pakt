# CLI Reference

Complete reference for all Pakt commands. See also the main [README](https://github.com/MikeSiLVO/Pakt#readme) for quick start and overview.

## Global Options

```
pakt --version    Show version
pakt --help       Show help
```

---

## Setup & Authentication

### `pakt setup`

Interactive setup wizard to configure Trakt and Plex.

```bash
pakt setup              # PIN authentication (recommended)
pakt setup --token      # Manual token entry
```

**Options:**
| Flag | Description |
|------|-------------|
| `--token` | Use manual token entry instead of PIN auth |

**What it does:**
1. Authenticates with Trakt via device code flow
2. Links your Plex account via plex.tv/link
3. Discovers available Plex servers
4. Lets you select which servers to enable

---

### `pakt login`

Authenticate with Trakt using device code flow.

```bash
pakt login
```

Opens Trakt device authorization. You'll get a code to enter at trakt.tv/activate.

---

### `pakt logout`

Revoke Trakt authentication and clear stored tokens.

```bash
pakt logout
```

---

### `pakt status`

Show current configuration status.

```bash
pakt status
```

Displays:
- Trakt authentication status
- Configured Plex servers and their status
- Config directory location

---

## Sync

### `pakt sync`

Sync watched status and ratings between Plex and Trakt.

```bash
pakt sync                        # Sync all enabled servers
pakt sync --dry-run              # Preview without changes
pakt sync --verbose              # Show detailed item list
pakt sync --server MyServer      # Sync specific server
pakt sync -s Server1 -s Server2  # Sync multiple servers
```

**Options:**
| Flag | Short | Description |
|------|-------|-------------|
| `--dry-run` | | Preview what would sync without making changes |
| `--verbose` | `-v` | Show detailed list of items being synced |
| `--server NAME` | `-s` | Sync specific server(s) only (can repeat) |

---

## Server Management

### `pakt servers list`

Show all configured Plex servers.

```bash
pakt servers list
```

---

### `pakt servers discover`

Discover available Plex servers from your linked account.

```bash
pakt servers discover
```

Lists all servers on your Plex account with ownership and connection status.

---

### `pakt servers add`

Add a Plex server to sync.

```bash
pakt servers add MyServer                    # Add discovered server by name
pakt servers add MyServer --url URL --token TOKEN  # Manual entry
pakt servers add MyServer --disabled         # Add but don't enable
```

**Options:**
| Flag | Description |
|------|-------------|
| `--url URL` | Server URL (for manual entry) |
| `--token TOKEN` | Server token (for manual entry) |
| `--disabled` | Add server as disabled |

---

### `pakt servers remove`

Remove a configured server.

```bash
pakt servers remove MyServer
```

---

### `pakt servers test`

Test connection to a configured server.

```bash
pakt servers test MyServer
```

Verifies connectivity and lists available libraries.

---

### `pakt servers enable`

Enable a server for syncing.

```bash
pakt servers enable MyServer
```

---

### `pakt servers disable`

Disable a server from syncing.

```bash
pakt servers disable MyServer
```

---

## Libraries

### `pakt libraries`

Configure which Plex libraries to sync.

```bash
pakt libraries                       # Show current selection
pakt libraries --server MyServer     # For specific server
pakt libraries -m "Movies"           # Select movie library
pakt libraries -s "TV Shows"         # Select show library
pakt libraries -m "4K Movies" -m "Movies"  # Multiple libraries
pakt libraries --all                 # Sync all libraries (clear selection)
```

**Options:**
| Flag | Short | Description |
|------|-------|-------------|
| `--server NAME` | | Target specific server (default: first enabled) |
| `--movie NAME` | `-m` | Movie library to sync (can repeat) |
| `--show NAME` | `-s` | Show library to sync (can repeat) |
| `--all` | | Sync all libraries (clears selection) |

By default, all libraries are synced. Use `-m` and `-s` to limit to specific libraries.

---

## Web Interface

### `pakt serve`

Start the web interface.

```bash
pakt serve                    # Start on default port (7258)
pakt serve --port 9000        # Custom port
pakt serve --host 0.0.0.0     # Listen on all interfaces
pakt serve --tray             # With system tray icon (Windows)
pakt serve --no-tray          # Explicitly disable tray
```

**Options:**
| Flag | Description |
|------|-------------|
| `--host HOST` | Host to bind to (default: from config or 127.0.0.1) |
| `--port PORT` | Port to bind to (default: from config or 7258) |
| `--tray` | Enable system tray icon (Windows) |
| `--no-tray` | Disable system tray icon |

**Notes:**
- Default port 7258 = "PAKT" on phone keypad
- Port/host can be configured in `config.json` under `web`
- Use `--tray` with `pythonw` for background mode on Windows

---

## Maintenance

### `pakt clear-cache`

Clear the Trakt API cache.

```bash
pakt clear-cache
```

Useful if you're seeing stale data or after making changes directly on Trakt.

---

## Examples

### First-time setup

```bash
pakt setup          # Link Trakt and Plex
pakt sync --dry-run # Preview what will sync
pakt sync           # Run actual sync
```

### Multi-server workflow

```bash
pakt servers discover           # See available servers
pakt servers add "Living Room"  # Add a server
pakt servers add "Bedroom"      # Add another
pakt sync                       # Syncs all enabled servers
pakt sync -s "Living Room"      # Sync just one
```

### Selective library sync

```bash
pakt libraries                          # See available libraries
pakt libraries -m "Movies" -s "TV"      # Only sync these
pakt libraries --all                    # Back to syncing all
```

### Background server (Windows)

```bash
pythonw -m pakt serve --tray    # Runs in background with tray icon
```

See the [Automation Guide](https://github.com/MikeSiLVO/Pakt/blob/main/docs/automation.md) for scheduled tasks and startup configuration.
