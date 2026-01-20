# Automation Guide

Run Pakt automatically on startup or on a schedule. See also the main [README](https://github.com/MikeSiLVO/Pakt#readme) for quick start and overview.

## Windows

### Background Mode (System Tray)

Run the web server without a console window:

```bash
pythonw -m pakt serve --tray
```

If `pythonw` is not in your PATH, find the full path:
```
where pythonw
```

Example: `C:\Users\YourName\AppData\Local\Programs\Python\Python311\pythonw.exe`

Note: `-m pakt` works as long as pakt is installed in that Python environment. Verify with:
```
python -c "import pakt; print(pakt.__file__)"
```

If this prints a path, pakt is installed and `pythonw -m pakt` will work.

To start on login, create a shortcut in your Startup folder:

1. Press `Win+R`, type `shell:startup`, press Enter
2. Create a new shortcut with target:
   ```
   C:\full\path\to\pythonw.exe -m pakt serve --tray
   ```
3. Name it "Pakt"

### Scheduled Sync (Task Scheduler)

For scheduled syncs without the web server running:

1. Find pakt path: `where pakt` in CMD
2. Open Task Scheduler (`taskschd.msc`)
3. **Create Task** (not Basic Task for more options)
4. **General tab:**
   - Name: `Pakt Sync`
   - Select "Run whether user is logged on or not" (hides console window)
5. **Triggers tab:**
   - New → Daily/Weekly as needed
6. **Actions tab:**
   - Program/script: `C:\full\path\to\pakt.exe`
   - Arguments: `sync`
   - Or for specific server: `sync --server MyServer`
7. Click OK, enter your password when prompted

## macOS

### Background Mode (Login Item)

1. Create a shell script `~/bin/pakt-serve.sh`:
   ```bash
   #!/bin/bash
   /usr/local/bin/pakt serve --port 7258
   ```
2. Make executable: `chmod +x ~/bin/pakt-serve.sh`
3. System Settings → General → Login Items → Add the script

### Scheduled Sync (launchd)

Create `~/Library/LaunchAgents/com.pakt.sync.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.pakt.sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/pakt</string>
        <string>sync</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>6</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/pakt-sync.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/pakt-sync.log</string>
</dict>
</plist>
```

Load it:
```bash
launchctl load ~/Library/LaunchAgents/com.pakt.sync.plist
```

Adjust the path to `pakt` based on your installation (`which pakt`).

## Linux

### Background Mode (systemd service)

Create `~/.config/systemd/user/pakt.service`:

```ini
[Unit]
Description=Pakt Web Server
After=network.target

[Service]
ExecStart=/usr/local/bin/pakt serve --host 127.0.0.1 --port 7258
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Enable and start:
```bash
systemctl --user daemon-reload
systemctl --user enable pakt
systemctl --user start pakt
```

### Scheduled Sync (systemd timer)

Create `~/.config/systemd/user/pakt-sync.service`:

```ini
[Unit]
Description=Pakt Sync

[Service]
Type=oneshot
ExecStart=/usr/local/bin/pakt sync
```

Create `~/.config/systemd/user/pakt-sync.timer`:

```ini
[Unit]
Description=Run Pakt sync daily

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

Enable the timer:
```bash
systemctl --user daemon-reload
systemctl --user enable pakt-sync.timer
systemctl --user start pakt-sync.timer
```

### Scheduled Sync (cron)

```bash
crontab -e
```

Add (runs daily at 6 AM):
```
0 6 * * * /usr/local/bin/pakt sync >> /tmp/pakt-sync.log 2>&1
```

Adjust the path to `pakt` based on your installation (`which pakt`).

## Built-in Scheduler

Alternatively, use Pakt's built-in scheduler via the web UI:

1. Run `pakt serve` (or as a background service)
2. Open http://localhost:7258
3. Go to Settings → Scheduled Sync
4. Set interval in hours and enable

The built-in scheduler only runs while the web server is running.
