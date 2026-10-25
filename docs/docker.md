# Docker Guide

Run Pakt in Docker with persistent configuration. See also the main [README](https://github.com/MikeSiLVO/Pakt#readme) for quick start and overview.

---

## Quick Start

```bash
# Clone the repo (for docker-compose.yml)
git clone https://github.com/MikeSiLVO/Pakt.git
cd Pakt

# Start the container
docker-compose up -d
```

Web UI available at http://localhost:7258

The image installs the released `pakt` package from PyPI rather than building the checked-out
source, so the clone only supplies `docker-compose.yml` and the `Dockerfile`. Rebuilding picks up
the latest published version.

---

## First-Time Setup

The container runs the web server by default, but you need to authenticate first:

```bash
# Run setup interactively
docker-compose run --rm pakt pakt setup

# Or login to Trakt separately
docker-compose run --rm pakt pakt login
```

Follow the prompts to:
1. Link your Trakt account at the address shown
2. Link your Plex account at plex.tv/link
3. Select servers to enable

---

## docker-compose.yml

The included `docker-compose.yml` runs Pakt as an unprivileged user on a read-only filesystem, with
its config in the `pakt-config` volume at `/home/pakt/.config/pakt`.

A compose file that mounts the volume at `/root/.config/pakt` stops the container with a message.
Point the mount at `/home/pakt/.config/pakt`; the files in the volume carry over unchanged.

---

## Configuration

### Ports

The web UI is published on the Docker host's `localhost:7258` only. To reach it from other devices,
publish it on every interface:

```yaml
ports:
  - "7258:7258"
```

Set a password first; the web UI stays closed until one is set:

```bash
docker-compose run --rm pakt pakt password
```

To use another port:

```yaml
ports:
  - "127.0.0.1:9000:7258"  # Access via localhost:9000
```

---

### Volumes

Config is stored in a named volume by default:

```yaml
volumes:
  - pakt-config:/home/pakt/.config/pakt
```

To use a local directory instead:

```yaml
volumes:
  - ./config:/home/pakt/.config/pakt
```

This makes it easier to edit `config.json` directly.

---

### Environment Variables

Currently, Pakt uses `config.json` for all settings. Environment variables are not supported for configuration.

---

## Running Commands

### Manual Sync

```bash
docker-compose run --rm pakt pakt sync
```

With options:
```bash
docker-compose run --rm pakt pakt sync --dry-run --verbose
```

---

### Check Status

```bash
docker-compose run --rm pakt pakt status
```

---

### Manage Servers

```bash
docker-compose run --rm pakt pakt servers list
docker-compose run --rm pakt pakt servers discover
docker-compose run --rm pakt pakt servers test MyServer
```

---

## Scheduled Sync

### Option 1: Built-in Scheduler

Enable in the web UI (Settings → Scheduled Sync) or edit config:

```json
{
  "scheduler": {
    "enabled": true,
    "interval_hours": 24
  }
}
```

The scheduler runs while the container is running.

---

### Option 2: Cron / External Scheduler

Run sync via cron on the host:

```bash
0 6 * * * docker-compose -f /path/to/docker-compose.yml run --rm pakt pakt sync
```

Or use a separate cron container.

---

## Networking

### Web UI Access

To open the web UI by another host name, such as `nas.local`, set `web.allowed_hosts` (see
[Configuration](configuration.md)).

---

### Accessing Local Plex Server

If Plex runs on the Docker host:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

Then use `http://host.docker.internal:32400` as the Plex URL.

---

### Bridge Network

If Plex is in another container on the same network:

```yaml
networks:
  - media

networks:
  media:
    external: true
```

Use the Plex container name as hostname: `http://plex:32400`

---

## Logs

View container logs:
```bash
docker-compose logs -f pakt
```

For debug logs, check the config volume:
```bash
docker-compose run --rm pakt cat /home/pakt/.config/pakt/debug.log
```

---

## Updating

```bash
git pull
docker-compose build --no-cache
docker-compose up -d
```

---

## Troubleshooting

### Container exits immediately

Check logs:
```bash
docker-compose logs pakt
```

Common causes:
- Port already in use
- Config file permissions

---

### Can't connect to Plex

1. Ensure Plex is accessible from inside the container
2. Check network configuration
3. Use `host.docker.internal` for host Plex
4. Try running setup again:
   ```bash
   docker-compose run --rm pakt pakt setup
   ```

---

### Permission denied on config volume

The container hands the config folder to its `pakt` user (uid 1000) on start. A bind-mounted folder
must exist on the host before the first start:

```bash
mkdir -p ./config
```
