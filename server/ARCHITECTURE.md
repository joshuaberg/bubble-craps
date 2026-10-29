# Bubble Craps Server — Product Requirements Document

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

## 3. Components

### 3.1 Mosquitto Broker

MQTT message bus. Currently running in a standalone Docker container — to be migrated into the project's Docker Compose stack.

- Port: 1883
- No auth currently (TODO: add username/password)
- Handles LWT for machine offline detection

### 3.2 SQL Bridge

Subscriber service that persists roll data.

- Subscribes to `bubble-craps/+/roll/result` and `bubble-craps/+/roll/image/+`
- Inserts roll results into the database
- Saves images to disk, linked by roll_id
- Routes failed roll images to a review folder for debugging

### 3.3 Database

- PostgreSQL (or SQLite for initial development)
- Source of truth for roll history, statistics, per-machine analytics
- Schema TBD

### 3.4 Game Server

- Subscribes to roll results
- Implements craps game logic (pass/don't pass, point, come bets, etc.)
- Publishes game state for display/dashboard consumption
- Manages game sessions

### 3.5 Dashboard

- Web UI for monitoring and analytics
- Machine status (online/offline/error, last roll, uptime)
- Roll history and statistics
- Live roll feed
- Detection health trends
- Game session view (when game server is running)

## 4. Deployment

All services managed via `docker-compose.yml` in the `server/` directory.

## 5. Status

Not yet implemented. This document will be expanded as each component is designed and built.
