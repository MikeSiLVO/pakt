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
1. Link your Trakt account at trakt.tv/activate
2. Link your Plex account at plex.tv/link
3. Select servers to enable

---

## docker-compose.yml

```yaml
version: '3.8'

services:
  pakt:
    build: .
    container_name: pakt
    ports:
      - "7258:7258"
    volumes:
      - pakt-config:/root/.config/pakt
    restart: unless-stopped
    command: pakt serve --host 0.0.0.0

volumes:
  pakt-config:
```

---

## Configuration

### Ports

Default port is 7258. To change:

```yaml
ports:
  - "9000:7258"  # Access via localhost:9000
```

Or change both:

```yaml
ports:
  - "9000:9000"
command: pakt serve --host 0.0.0.0 --port 9000
```

---

### Volumes

Config is stored in a named volume by default:

```yaml
volumes:
  - pakt-config:/root/.config/pakt
```

To use a local directory instead:

```yaml
volumes:
  - ./config:/root/.config/pakt
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

### Clear Cache

```bash
docker-compose run --rm pakt pakt clear-cache
```

---

## Scheduled Sync

### Option 1: Built-in Scheduler

Enable in the web UI (Settings → Scheduled Sync) or edit config:

```json
{
  "scheduler": {
    "enabled": true,
    "interval_hours": 24,
    "run_on_startup": true
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
docker-compose run --rm pakt cat /root/.config/pakt/debug.log
```

---

## Updating

```bash
docker-compose pull
docker-compose up -d
```

Or rebuild from source:
```bash
docker-compose build --no-cache
docker-compose up -d
```

---

## Dockerfile

The included Dockerfile:

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

CMD ["pakt", "serve", "--host", "0.0.0.0"]
```

Builds a minimal image with Pakt installed.

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

If using a bind mount, ensure the directory exists and has correct permissions:

```bash
mkdir -p ./config
chmod 755 ./config
```
