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
- **Ephemeral data** (broker message cache, logs) uses Docker named volumes — managed by Docker, not in the repo, survives container rebuilds.
- **Persistent data** (database) will use bind mounts to a visible host path for easier backups.

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

Subscriber service that persists roll data to a database.

- Subscribes to `bubble-craps/+/roll/result` and `bubble-craps/+/roll/image/+`
- Inserts roll results into the database
- Saves images to disk, linked by roll_id
- Routes failed roll images to a review folder for debugging

### 4.3 Database

- PostgreSQL
- Source of truth for roll history, statistics, per-machine analytics
- Data stored via bind mount for backup visibility
- Schema TBD

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
