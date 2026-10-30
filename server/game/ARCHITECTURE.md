# Game Server — Architecture

## 1. Overview

The game server manages craps game state. It consumes dice roll results from the MQTT broker, advances craps rounds, resolves bets, and publishes game events. It does not control the machine — it is a pure consumer of roll results.

The game server exposes a REST API for commands (create tables, place bets, manage players) and publishes live game events over MQTT for subscribers (web UI, bots, dashboard).

### Design Goals

- Faithful implementation of full craps table rules
- Multiple concurrent tables subscribed to the same machine
- Clean separation: game server knows nothing about bots, web UI, or machine control
- Recoverable — rebuilds in-progress game state from Postgres on restart

---

## 2. Key Concepts

### Terminology

- **Table** — a long-lived craps game session subscribed to one machine's roll feed. Does not end unless explicitly closed.
- **Round** — one shooter's turn. Starts with a come-out roll, ends when the shooter sevens-out or hits the point. A new round starts automatically.
- **Roll** — a single dice throw within a round. Comes from the machine via MQTT.
- **Player** — the universal account unit. The game server does not distinguish between humans and bots. Both are just players with a name and a balance.

### Tables

A table subscribes to one machine's roll feed (`bubble-craps/{device_id}/roll/result`). Multiple tables can subscribe to the same machine — each independently tracks its own craps game state from the same rolls.

Two lifecycle patterns, but identical to the game server:

- **Passive table** — created automatically on startup, runs forever. Auto-restarts rounds after seven-out. Intended for background play (bots betting at whatever pace the machine rolls).
- **Session table** — created on demand via API (e.g., friends playing via website). Closed explicitly when done.

The game server does not control the machine's roll timing or trigger mode. Whether the machine rolls once an hour or every 20 seconds, the game server simply processes each roll as it arrives. Changing the machine's mode (timer interval, remote trigger) is handled by whatever client manages the session — typically the web UI.

### Players

A player is a persistent account with a name and a play-money balance ("dollars"). Players sit down at tables to place bets and stand up when done. Balances persist across tables.

The game server only sees players. A **bot service** (separate, future) controls player accounts and places bets programmatically. The **web UI** (separate, future) lets humans do the same. Both use the same REST API. The game server is client-agnostic.

---

## 3. Architecture

```
Machine (Pi)
  │
  │ roll result (MQTT)
  ▼
Broker ──────────────────────────────────────────
  │                    │                        │
  ▼                    ▼                        ▼
SQL Bridge        Game Server              Bot Service (future)
(rolls → DB)          │                        │
                      │  game events (MQTT)    │
                      ├────────────────►───────┤
                      │                        │
                      │  bets/commands (REST)   │
                      │◄───────────────────────┤
                      │◄──────────────┐
                      │               │
                      ▼            Web UI (future)
                   Postgres
                 (game history)
```

### Data Flow

1. Machine publishes roll result to MQTT
2. Game server receives roll, advances all tables subscribed to that machine
3. For each table: resolve bets, update round/phase, check for round end
4. Game server publishes game events to MQTT (state updates, bet resolutions)
5. Game server persists round/bet outcomes to Postgres
6. Clients (web UI, bots) read game state from MQTT, place bets via REST

### Independence from SQL Bridge

The game server and SQL bridge are independent consumers of the same MQTT roll feed. The game server does not read from the bridge's `rolls` table. They share a Postgres instance but own separate tables.

---

## 4. Interface

### 4.1 REST API (Commands In)

All commands flow through the REST API with synchronous responses.

**Tables**
```
POST   /api/tables                  Create table (device_id, type, config)
GET    /api/tables                  List active tables
GET    /api/tables/{id}             Table state + current round
DELETE /api/tables/{id}             Close table
```

**Players**
```
POST   /api/players                 Create player (name, starting_balance)
GET    /api/players/{id}            Player info + balance
```

**Table Membership**
```
POST   /api/tables/{id}/join        Player sits down (player_id)
POST   /api/tables/{id}/leave       Player stands up (player_id)
```

**Bets**
```
POST   /api/tables/{id}/bets        Place bet (player_id, type, amount)
GET    /api/tables/{id}/bets        Active bets on table
DELETE /api/tables/{id}/bets/{id}   Remove bet (if rules allow)
```

### 4.2 MQTT (Events Out)

Game events are published to MQTT for any subscriber to consume.

| Topic | Retained | Description |
|-------|----------|-------------|
| `bubble-craps/game/{table_id}/state` | Yes | Full table state snapshot (phase, point, active bets, players) |
| `bubble-craps/game/{table_id}/events` | No | Individual events: `roll_processed`, `bet_resolved`, `round_ended`, `round_started` |
| `bubble-craps/game/{table_id}/players` | Yes | Who's at the table + balances |

### 4.3 MQTT (Roll Feed In)

Subscribes to:
```
bubble-craps/+/roll/result
```

Each table filters for its own `device_id`.

---

## 5. Game State Model

### Table State (in-memory + Postgres)
```
table_id        uuid        PK
device_id       text        which machine to subscribe to
type            text        "passive" | "session"
status          text        "active" | "closed"
config          jsonb       payout odds, min/max bets
current_round   Round       live state (in-memory, persisted on change)
created_at      timestamptz
closed_at       timestamptz?
```

### Round
```
round_id        uuid        PK
table_id        uuid        FK → tables
phase           text        "come_out" | "point"
point           int?        null during come-out; 4,5,6,8,9,10 during point phase
rolls           jsonb       ordered list of {roll_id, die1, die2, total}
outcome         text?       null | "natural" | "craps" | "point_hit" | "seven_out"
started_at      timestamptz
ended_at        timestamptz?
```

### Player
```
player_id       uuid        PK
name            text
balance         decimal     current play-money balance
created_at      timestamptz
```

### Table Membership
```
table_id        uuid        FK → tables
player_id       uuid        FK → players
joined_at       timestamptz
left_at         timestamptz?
```

### Bet
```
bet_id          uuid        PK
table_id        uuid        FK → tables
round_id        uuid        FK → rounds
player_id       uuid        FK → players
type            text        "pass_line", "dont_pass", "come", "place_6", "field", "hard_8", etc.
amount          decimal
status          text        "active" | "won" | "lost" | "push" | "returned"
payout          decimal?    amount won (null while active)
placed_at       timestamptz
resolved_at     timestamptz?
sub_point       int?        for come/don't come bets that establish their own point
sub_phase       text?       "come_out" | "point" (for come/don't come tracking)
```

---

## 6. Craps Rules Engine

### Come-Out Roll

| Total | Result | Pass Line | Don't Pass |
|-------|--------|-----------|------------|
| 7, 11 | Natural | Win 1:1 | Lose |
| 2, 3 | Craps | Lose | Win 1:1 |
| 12 | Craps | Lose | Push (bar 12) |
| 4, 5, 6, 8, 9, 10 | Point established | Stays | Stays |

After a natural or craps, the round stays in come-out phase (same round, new come-out roll). The round only ends on a seven-out or point hit during the point phase.

### Point Phase

| Roll | Result | Pass Line | Don't Pass |
|------|--------|-----------|------------|
| Point number | Point hit | Win 1:1 | Lose |
| 7 | Seven-out | Lose | Win 1:1 |
| Anything else | Continue | — | — |

### Bet Types and Payouts

Payouts are configurable per table. Defaults are standard casino odds.

| Bet | When Placeable | Resolves On | Default Payout |
|-----|---------------|-------------|----------------|
| Pass Line | Come-out only | Natural/craps or point/7-out | 1:1 |
| Don't Pass | Come-out only | Inverse (bar 12) | 1:1 |
| Pass Odds | Point phase, behind pass bet | Point or 7 | True odds |
| Don't Pass Odds | Point phase, behind don't pass | Point or 7 | True odds |
| Come | Point phase | Own come-out, then own point/7 | 1:1 |
| Don't Come | Point phase | Inverse of come | 1:1 |
| Come Odds | After come point set | Come point or 7 | True odds |
| Don't Come Odds | After DC point set | DC point or 7 | True odds |
| Place 4/10 | Anytime | Number or 7 | 9:5 |
| Place 5/9 | Anytime | Number or 7 | 7:5 |
| Place 6/8 | Anytime | Number or 7 | 7:6 |
| Field | Any roll | Immediate | 1:1 (2:1 on 2, 2:1 on 12) |
| Big 6 / Big 8 | Anytime | 6/8 or 7 | 1:1 |
| Hardway 4/10 | Anytime | Hard total, easy total, or 7 | 7:1 |
| Hardway 6/8 | Anytime | Hard total, easy total, or 7 | 9:1 |
| Any 7 | Any roll | Immediate | 4:1 |
| Any Craps | Any roll | Immediate (2, 3, 12) | 7:1 |
| Yo (11) | Any roll | Immediate | 15:1 |
| Hi-Lo (2 or 12) | Any roll | Immediate | 15:1 |
| Horn | Any roll | Immediate (2, 3, 11, 12) | Split, various |

**True odds (for reference):**

| Point | Pass Odds Payout | Don't Pass Odds Payout |
|-------|-----------------|----------------------|
| 4, 10 | 2:1 | 1:2 |
| 5, 9 | 3:2 | 2:3 |
| 6, 8 | 6:5 | 5:6 |

---

## 7. Persistence and Recovery

### What Gets Persisted

All game state is persisted to Postgres as it changes:
- Tables, rounds, bets, players, and membership are written on creation and updated on state changes.
- This provides full game history for stats and analytics.

### Recovery on Restart

On startup, the game server:
1. Loads all active tables from Postgres
2. Rebuilds in-memory state (current round, active bets, phase, point)
3. Re-subscribes to MQTT for each table's device
4. Resumes processing rolls

No game state is lost on restart.

---

## 8. Tech Stack

| Component | Choice |
|-----------|--------|
| Language | Python 3.11+ |
| Web framework | TBD (FastAPI or Flask) |
| MQTT | paho-mqtt |
| Database | PostgreSQL (shared instance with SQL bridge) |
| ORM / DB access | TBD |
| Deployment | Docker container in `server/docker-compose.yml` |

---

## 9. Future Services

### Bot Service

A separate service that controls player accounts and places bets automatically. The game server does not know bots exist — it just sees players.

- Creates player accounts via game server REST API
- Subscribes to game state via MQTT
- Runs configurable betting strategies
- Places bets via game server REST API

### Web UI

A website where humans play craps. Handles:
- Player account creation
- Table browsing, creation, joining
- Bet placement via game server REST API
- Live game state via MQTT over WebSockets
- Machine control (trigger mode, roll commands) via machine MQTT topics
