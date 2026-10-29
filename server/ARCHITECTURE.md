# Bubble Craps Server — Architecture

## 1. Overview

The server-side platform consumes MQTT messages from one or more Bubble Craps machines and provides data persistence, game logic, and monitoring. All services run on a home server via Docker Compose.

## 2. Architecture

```
                    ┌──────────────────────────────────────────┐
                    │              Home Server (Docker)         │
                    │                                          │
  Pi(s) ──MQTT──►  │  ┌────────────┐    ┌─────────────────┐   │
                    │  │ Mosquitto  │───►│   SQL Bridge     │   │
                    │  │  Broker    │    │  (roll results   │   │
                    │  │            │    │   → database)    │   │
                    │  └─────┬──────┘    └────────┬────────┘   │
                    │        │                    │             │
                    │        │              ┌─────▼────────┐   │
                    │        │              │  Database     │   │
                    │        │              │  (PostgreSQL) │   │
                    │        │              └─────┬────────┘   │
                    │        │                    │             │
                    │  ┌─────▼──────┐    ┌───────▼────────┐   │
                    │  │  Game      │    │   Dashboard     │   │
                    │  │  Server    │    │   (Web UI)      │   │
                    │  └────────────┘    └────────────────┘   │
                    └──────────────────────────────────────────┘
```

## 3. Infrastructure

### Host Environment

The server runs inside a Proxmox LXC container (Debian 12) with Docker Engine installed. Container features `nesting` and `keyctl` must be enabled for Docker to work inside LXC.

Specs: 2 cores, 2 GB RAM, 512 MB swap, 16 GB disk. Network via DHCP reservation for a stable IP.

### Docker Compose

All services are defined in `docker-compose.yml` in this directory. New services are added to the same compose file — Compose only recreates containers whose config changed, so adding a service doesn't disrupt what's already running.

The repo is cloned to `/opt/bubble-craps` on the server. To deploy or update:

```bash
cd /opt/bubble-craps/server
git pull origin main
docker compose up -d
```

### Configuration and Data

- **Config files** (e.g., `mosquitto/mosquitto.conf`) are version-controlled in this repo and bind-mounted into containers.
- **Secrets** — Postgres password is set directly in the compose file. Sufficient for a home network where the database is only accessible locally.
- **Ephemeral data** (broker message cache, logs) uses Docker named volumes — managed by Docker, not in the repo, survives container rebuilds.
- **Persistent data** (database) uses bind mounts to a visible host path for easier backups.
- **Images** saved by the bridge use bind-mounted directories on the host, capped by count to prevent disk growth.

## 4. Components

### 4.1 Mosquitto Broker

MQTT message bus. All communication between the Pi(s) and server-side services flows through the broker.

- Image: `eclipse-mosquitto:2`
- Port: 1883
- Persistence enabled — retained messages (like machine status) survive broker restarts
- Anonymous access (no auth) — sufficient for a home network, auth is a future TODO
- Config: `mosquitto/mosquitto.conf`

The broker handles:
- Roll results and images from the machine(s)
- Status updates and LWT (Last Will and Testament) for offline detection
- Commands from any client to any machine

### 4.2 SQL Bridge

Python service (paho-mqtt) that subscribes to MQTT and persists data to PostgreSQL and disk.

**Subscriptions:**
- `bubble-craps/+/roll/result` — roll results → `rolls` table
- `bubble-craps/+/roll/image/+` — roll images → disk, path stored in `rolls.image_path`
- `bubble-craps/+/status` — future: log ERROR and OFFLINE events to a `status_events` table

**Image routing and retention:**
- Successful rolls (`roll_status: "ok"`) → `images/ok/{roll_id}.jpg`
- Failed rolls (`roll_status: "detection_failed"`) → `images/failed/{roll_id}.jpg`

Each folder is capped at a configurable max count (default 300). When the limit is reached, the oldest images are deleted and their `image_path` in the rolls table is set to null. The roll data itself is kept forever — only the image file is pruned. Failed images are valuable for understanding detection issues; successful images are useful for debugging and potential future training data.

Images are paired with results by `roll_id`. The bridge receives the result first (knows the status), then the image (knows where to save it).

### 4.3 Database

PostgreSQL, running as a Docker container. Data stored via bind mount (`./data/postgres`) for backup visibility. Port 5432 exposed to the host network for debugging access from a SQL client.

- Image: `postgres:16`
- Database: `bubble_craps`
- User/password: set directly in `docker-compose.yml`
- Internal access: other containers connect via hostname `postgres` on the Docker network
- External access: available at `<server-ip>:5432` for debugging from your laptop

**`rolls` table:**

| Column | Type | Notes |
|--------|------|-------|
| roll_id | text, PK | UUID from the machine |
| device_id | text | Which machine rolled |
| die1 | integer, nullable | Null on detection failure |
| die2 | integer, nullable | |
| total | integer, nullable | |
| roll_status | text | "ok" or "detection_failed" |
| roll_status_reason | text, nullable | |
| attempts | jsonb | Capture/detect attempt list |
| motor_temp_c | integer, nullable | |
| image_path | text, nullable | Path to saved JPEG |
| timestamp | timestamptz | Capture time from the machine |

**`status_events` table:**

| Column | Type | Notes |
|--------|------|-------|
| id | serial, PK | Auto-increment |
| device_id | text | Which machine |
| state | text | "ERROR" or "OFFLINE" |
| error_reason | text, nullable | e.g., "mechanism_failure" |
| payload | jsonb | Full status payload for context |
| timestamp | timestamptz | From the status message |

**Deferred:** `machines` table (device registry, last seen) — not needed until the dashboard is built. Current machine state is available from the retained MQTT status topic.

### 4.4 Game Server

- Subscribes to roll results
- Implements craps game logic (pass/don't pass, point, come bets, etc.)
- Publishes game state for display/dashboard consumption
- Manages game sessions

### 4.5 Dashboard

- Web UI for monitoring and analytics
- Machine status (online/offline/error, last roll, uptime)
- Roll history and statistics
- Live roll feed
- Detection health trends
- Game session view (when game server is running)
