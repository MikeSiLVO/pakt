# Troubleshooting

Common issues and solutions. See also the main [README](https://github.com/MikeSiLVO/Pakt#readme) for quick start and overview.

---

## Connection Issues

### "Not logged in to Trakt"

**Cause:** No Trakt access token in config.

**Solution:**
```bash
pakt login
```

---

### "No Plex servers configured"

**Cause:** No servers in config.json.

**Solution:**
```bash
pakt setup              # Full setup wizard
# or
pakt servers discover   # Find servers
pakt servers add NAME   # Add specific server
```

---

### Plex connection timeout / refused

**Possible causes:**
1. Server is offline or unreachable
2. Wrong URL in config
3. Firewall blocking connection
4. Invalid token

**Solutions:**

Test the connection:
```bash
pakt servers test MyServer
```

Check your server URL is accessible:
```bash
curl http://YOUR_PLEX_IP:32400/identity
```

Re-run setup to refresh tokens:
```bash
pakt setup
```

---

### Settings reset to defaults / "Could not read config.json"

**Cause:** `config.json` could not be parsed, usually a partial write from a machine losing power mid-save.

**What Pakt does:** the unreadable file is renamed to `config.json.corrupt` and defaults are used for that run, so the bad copy is never overwritten by a later save.

**Solution:**

Open `config.json.corrupt`, salvage anything you need (your `servers` list is the painful part to rebuild), then re-authenticate:
```bash
pakt login
pakt setup
```

---

### "Trakt refused the refresh token"

**Cause:** Trakt refresh token expired or revoked. Tokens issued before Trakt's login migration
show `session not found` and need one fresh login.

**Solution:**
```bash
pakt login
```

---

### "Another Pakt sync is already running"

**Cause:** A sync from another `pakt` process (the CLI, the web UI or the scheduler) is still running.
Only one sync runs at a time.

**Solution:** Wait for it to finish, or cancel it from the web UI.

---

### "Plex refused the saved token"

**Cause:** The Plex token was signed out, for example by a Plex password change or "Sign out of all
devices".

**Solution:** Sign in again with `pakt setup`, or Re-Link Account in the web UI settings.

---

## Web Server Issues

### "Host '...' is not allowed"

**Cause:** The web UI was opened by a host name other than `localhost` or an IP address.

**Solution:** Add the name to `web.allowed_hosts` in `config.json`:
```json
"web": {"host": "0.0.0.0", "port": 7258, "allowed_hosts": ["pakt.example.com"]}
```

---

### "Port already in use"

**Cause:** Another application (or another Pakt instance) is using the port.

**Solutions:**

1. Find and stop the other process
2. Use a different port:
   ```bash
   pakt serve --port 9000
   ```
3. Or set permanently in config.json:
   ```json
   {
     "web": {
       "port": 9000
     }
   }
   ```

---

### Tray icon doesn't appear (Windows)

**Possible causes:**
1. Dependencies not installed
2. Another instance already running

**Solutions:**

Install tray dependencies:
```bash
pip install pakt[tray]
# or
pip install pystray Pillow
```

Check for existing instance:
- Look in Task Manager for `pythonw.exe` or `python.exe` processes
- Check `debug.log` in config directory

---

### Can't access web UI from other devices

**Cause:** Server bound to localhost only.

**Solution:**

Bind to all interfaces:
```bash
pakt serve --host 0.0.0.0
```

Or in config.json:
```json
{
  "web": {
    "host": "0.0.0.0"
  }
}
```

Then access via `http://YOUR_IP:7258`, and set the password it asks for with `pakt password`.

---

### "Set a password before opening Pakt from another device"

**Cause:** The web UI was opened from another device and no password is set.

**Solution:** Run `pakt password` on the machine running Pakt (in Docker,
`docker-compose run --rm pakt pakt password`), then reload the page.

---

## Sync Issues

### Items not syncing

**Possible causes:**
1. Sync direction disabled in settings
2. Library not selected
3. Item not matched (missing IMDB/TMDB ID)

**Solutions:**

Check sync settings:
```bash
pakt status
```

Run with verbose to see what's happening:
```bash
pakt sync --verbose --dry-run
```

Check library selection:
```bash
pakt libraries --server MyServer
```

---

### "Trakt account limit reached" / HTTP 420

**Cause:** Free Trakt accounts have a 100-item limit on collections and watchlists.

**Solutions:**

1. Upgrade to [Trakt VIP](https://trakt.tv/vip) for unlimited
2. Disable collection/watchlist sync:
   ```json
   {
     "sync": {
       "collection_plex_to_trakt": false,
       "watchlist_plex_to_trakt": false
     }
   }
   ```

---

### Ratings not syncing correctly

**Cause:** Rating conflict between Plex and Trakt.

**Solution:**

Set rating priority in config:
```json
{
  "sync": {
    "rating_priority": "plex"
  }
}
```

Options:
- `"none"` - Skip conflicting ratings (default)
- `"plex"` - Plex rating always wins
- `"trakt"` - Trakt rating always wins

---

### Duplicate items / wrong matches

**Cause:** Media has incorrect or missing metadata (IMDB/TMDB IDs).

**Solution:**

1. Fix metadata in Plex (use "Fix Match" or "Refresh Metadata")
2. Re-run sync

---

## Scheduler Issues

### Scheduled sync not running

**Possible causes:**
1. Scheduler not enabled
2. `pakt serve` not running
3. Interval set to 0 (the web UI rejects anything below 1 hour)

After `pakt serve` starts, the first scheduled sync runs one interval after the last completed sync,
or a minute after start when that time has already passed.

**Solutions:**

Check scheduler config:
```json
{
  "scheduler": {
    "enabled": true,
    "interval_hours": 24
  }
}
```

The scheduler only runs while `pakt serve` is running. For persistent scheduling, see the [Automation Guide](https://github.com/MikeSiLVO/Pakt/blob/main/docs/automation.md) to run as a background service.

---

## Debug Logs

Every sync writes to `sync.log` in the config folder. It rolls over to `sync.log.1` at 5 MB.

When running in tray mode (`pythonw -m pakt serve --tray`), logs are written to:

- **Windows:** `%APPDATA%\pakt\debug.log`
- **Linux/macOS:** `~/.config/pakt/debug.log`

Additional files:
- `debug.log.1` - Previous session
- `debug.conflict.log` - Port conflict attempts

---

## Getting Help

If you're still stuck:

1. Run `pakt sync --verbose --dry-run` and check output
2. Check `debug.log` for errors
3. [Open an issue](https://github.com/MikeSiLVO/Pakt/issues) with:
   - Pakt version (`pakt --version`)
   - OS and Python version
   - Relevant log output
   - Steps to reproduce
