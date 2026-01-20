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

### "Token refresh failed"

**Cause:** Trakt refresh token expired or revoked.

**Solution:**
```bash
pakt logout
pakt login
```

---

## Web Server Issues

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

Then access via `http://YOUR_IP:7258`

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
- `"plex"` - Plex rating always wins
- `"trakt"` - Trakt rating always wins
- `"newest"` - Most recently changed rating wins

---

### Duplicate items / wrong matches

**Cause:** Media has incorrect or missing metadata (IMDB/TMDB IDs).

**Solution:**

1. Fix metadata in Plex (use "Fix Match" or "Refresh Metadata")
2. Clear Pakt's cache:
   ```bash
   pakt clear-cache
   ```
3. Re-run sync

---

## Scheduler Issues

### Scheduled sync not running

**Possible causes:**
1. Scheduler not enabled
2. `pakt serve` not running
3. Interval set to 0

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

The scheduler only runs while `pakt serve` is running. For persistent scheduling, run as a background service. See the [Automation Guide](https://github.com/MikeSiLVO/Pakt/blob/main/docs/automation.md).

---

## Debug Logs

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
