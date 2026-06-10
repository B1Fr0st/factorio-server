# factorio-server

A Docker image wrapping [`factoriotools/factorio`](https://hub.docker.com/r/factoriotools/factorio) with a web-based management UI. Manage mods, saves, logs, server settings, and Factorio versions without touching the command line.

## Quick start

```yaml
# docker-compose.yml
services:
  factorio:
    image: ghcr.io/b1fr0st/factorio-server:latest
    platform: linux/amd64
    restart: unless-stopped
    ports:
      - "34197:34197/udp"  # game
      - "27015:27015/tcp"  # RCON (optional)
      - "8080:8080/tcp"    # manager UI
    volumes:
      - factorio-data:/factorio
    environment:
      MANAGER_PASSWORD: "changeme"

volumes:
  factorio-data:
```

```bash
docker compose up -d
```

Open **http://localhost:8080** to access the UI.

## Features

- **Server control** — start, stop, restart from the browser
- **Live logs** — real-time streaming with color-coded output (errors, warnings, chat, join/leave events)
- **Mod management**
  - Search and install mods from the mod portal
  - Automatic dependency resolution and version conflict detection
  - Bulk enable, disable, or remove via checkboxes
  - DLC toggles for Space Age, Quality, and Elevated Rails
- **Save management** — create new worlds, upload/download/delete saves
- **Server settings** — edit `server-settings.json` through a form or raw JSON
- **Version switching** — install any Factorio version (stable, experimental, or a specific build) from the Config tab; server stops, updates, and restarts automatically

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 34197 | UDP | Factorio game traffic |
| 27015 | TCP | RCON (optional) |
| 8080 | TCP | Manager web UI |

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MANAGER_PASSWORD` | _(none)_ | Password to protect the UI. Leave blank to disable auth. |
| `FACTORIO_USERNAME` | _(none)_ | Pre-fill mod portal username |
| `FACTORIO_TOKEN` | _(none)_ | Pre-fill mod portal API token (find it at factorio.com → Profile → API Key) |
| `FACTORIO_SAVE` | _(latest)_ | Path to the save file to load on start. Defaults to the most recently modified save. |
| `RCON_PORT` | _(none)_ | Enable RCON on this port |
| `RCON_PASSWORD` | _(none)_ | RCON password (required if `RCON_PORT` is set) |
| `MANAGER_PORT` | `8080` | Port the manager UI listens on |
| `FACTORIO_DIR` | `/factorio` | Data directory for saves, mods, and config |

## Mod portal credentials

Mod installation requires a Factorio account. Enter your username and API token in the **Mod Portal Credentials** card on the Mods tab. Credentials are saved in your browser's localStorage and are sent to the server when you install a mod — the server uses them to authenticate the download from the mod portal. You can also pre-fill them via the `FACTORIO_USERNAME` and `FACTORIO_TOKEN` environment variables.

## Building from source

```bash
git clone https://github.com/B1Fr0st/factorio-server.git
cd factorio-server
docker compose build
docker compose up -d
```

The image pins `linux/amd64` because the Factorio headless binary is x86-only. On Apple Silicon the container runs under Rosetta 2 automatically.

## CI/CD

Every push to `main` builds and publishes the image to the GitHub Container Registry:

```
ghcr.io/b1fr0st/factorio-server:latest
ghcr.io/b1fr0st/factorio-server:sha-<short-sha>
```

Pull requests get a build-only check with no publish.
