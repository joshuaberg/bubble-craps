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

Multiple tables sharing one machine is expected and fine. A passive table with bots and a session table with friends can run simultaneously on the same machine. Both see every roll and track their own independent game state. The rolls are duplicated across tables — each table processes the same dice result, but bets, rounds, and outcomes are completely separate.

### Players

A player is the only entity the game server knows about. It is an account — nothing more.

```
player_id:  uuid
name:       "Leroy"
balance:    1000
```

A player does not make decisions. It does not contain logic. It is a row in the database that owns a balance and a history of bets. The game server charges it when bets are placed and pays it when bets are won.

Whatever controls a player — a human clicking buttons on a website, or a bot service running a strategy — uses the same REST API. The game server cannot tell the difference and does not try. The player is the shared interface between all clients and the game server.

### Bet Timing

Bets are placed **before** a roll, not after — just like a real craps table. A bot reacting to a "round entered come-out" event places its pass line bet *before* the next roll happens. It does not react to a roll and bet retroactively.

This is enforced by the command log — see section 3.

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

### Command Log

The table is a **deterministic state machine** with one input: an ordered command log. Every input — rolls, bets, joins, leaves, betting windows — is a command on a single per-table log. One worker per table pulls commands off in order and applies them through pure functions. Nothing else touches table state.

REST endpoints and the MQTT adapter are just that — adapters. They produce commands and (for REST) wait for the result. They do not mutate table state directly.

**Command types:**

| Command | Source | Description |
|---------|--------|-------------|
| `betting_open` | MQTT adapter | Betting window is open. Emitted after a roll is resolved. Bets placed after this are valid for the next roll. |
| `bet_placed` | REST adapter | Player places a bet (player_id, type, amount) |
| `bet_removed` | REST adapter | Player removes a bet (if rules allow) |
| `betting_locked` | MQTT adapter | Betting window closed. Emitted when the next roll arrives, just before the `roll` command. |
| `roll` | MQTT adapter | Roll result (die1, die2, total). Triggers bet resolution and round advancement. |
| `player_joined` | REST adapter | Player sits down at table |
| `player_left` | REST adapter | Player stands up from table |

The worker processes commands strictly in log order. A `bet_placed` that arrives after `betting_locked` but before `roll` is rejected — not because of a timing check, but because of its position in the log. Order of arrival is the order of truth.

**What this buys:**

- **No concurrency.** There is no lock design, no "what if a bet lands mid-resolution." One worker, one log, one order.
- **Betting window is a command, not a timing hazard.** `betting_open` and `betting_locked` are explicit entries in the log, not derived from wall-clock time or machine status.
- **Persistence is simple.** Append commands, snapshot state periodically. Recovery is "load snapshot, replay the tail." No reconstructing in-memory state from five separate tables hoping they agree.
- **Every bug reproduces.** A production issue is a command log you replay in a test. A test is a list of commands and an expected state.
- **Bots and humans are provably identical.** Both emit `bet_placed`. The server cannot treat them differently because there is no place in the code where it could.
- **Extension is clean.** A new bet type touches only the rules engine. A new client touches only an adapter. A second machine touches only the roll adapter.

**Example log — one normal round:**

```
betting_open                                    # table is open for bets
bet_placed      Leroy   pass_line    10         # bot reacts to come-out
bet_placed      Susan   dont_pass     5         # another bot
betting_locked                                  # machine is about to roll
roll            roll_id=...a1   4+2  total=6    # point established at 6
betting_open                                    # open for point-phase bets
bet_placed      Leroy   pass_odds   20          # bot adds odds
betting_locked                                  # next roll coming
roll            roll_id=...b3   5+1  total=6    # point hit!
  → resolve: Leroy pass_line WIN +10
  → resolve: Leroy pass_odds WIN +24
  → resolve: Susan dont_pass LOSE -5
  → round ended: point_hit
betting_open                                    # new come-out
...
```

**The flow:**

1. Previous roll resolved → adapter emits `betting_open`
2. Bots/humans place bets → `bet_placed` commands land on the log
3. Next roll arrives on MQTT → adapter emits `betting_locked`, then `roll`
4. Worker processes the `roll`: resolves bets, advances round, persists, publishes MQTT events
5. Back to step 1

The betting window is the time between `betting_open` and `betting_locked` — however long until the machine rolls again. On a 1-hour timer, bots have an hour. On a 20-second timer, they have 20 seconds. Miss it and you miss, just like a real table.

### Roll Ingress

The MQTT adapter is a simple filter. It receives every roll result from the broker and applies one rule: **drop null rolls.** If `roll_status` is not `"ok"` (detection failed, camera failure, etc.), discard it. The dice couldn't be read — no-roll, just like a real craps table. Everything else is a valid roll and goes onto the table command logs.

There is no offline catch-up to handle. The machine is fire-and-forget — if the broker is down, rolls are lost. The game server uses a **clean MQTT session** (`clean_session=true`), so if the game server itself restarts, the broker does not hand it a backlog. Every roll the game server sees is live.

**What happens to bets during an outage?** If the machine or broker goes down mid-game, active bets stay on the table. The game is paused — no commands are emitted, no state changes. When the machine comes back and rolls again, the next live roll resolves whatever is on the table. For play money this is perfectly consistent.

The bridge's `rolls` table and the game's roll history may disagree after an outage — the bridge only records what reaches the broker, the game only processes what it sees live. This is fine. They are independent records with different purposes.

**Deliberate replay** is still possible for testing or simulation: pull rolls from the bridge's `rolls` table and feed them into a table's command log as `roll` commands. This is an offline operation, not live play.

### Fan-Out

One MQTT roll is distributed to every table subscribed to that device. Each table has its own command log. Tables are fully independent — a slow table does not block a fast one.

### Independence from SQL Bridge

The game server and SQL bridge are independent consumers of the same MQTT roll feed. The game server does not read from the bridge's `rolls` table. They share a Postgres instance but own separate tables.

---

## 4. Interface

### 4.1 REST API (Commands In)

REST endpoints are **adapters** — they produce commands on the table's command log and wait for the result. They do not mutate table state directly. All commands flow through the REST API with synchronous responses.

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

The command log is the source of truth. Persistence follows from it:

- **Commands are appended** to a durable log (Postgres) as they are processed.
- **State snapshots** are taken periodically — a snapshot is the full table state (round, phase, point, active bets, player balances) at a known position in the log.
- **Game history** (completed rounds, resolved bets, player stats) is derived from the log and written to query-friendly tables for analytics.

### Recovery on Restart

On startup, the game server:
1. Loads the most recent state snapshot for each active table
2. Replays any commands in the log after the snapshot to rebuild current state
3. Re-subscribes to MQTT for each table's device
4. Resumes processing — new commands append to the same log

No game state is lost on restart. Recovery is deterministic — the same log always produces the same state.

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

## 9. Players, Bots, and Behaviors

### Player

A player is the only entity the game server knows about. It is an account — a name, a balance, and a history of bets. A player does not make decisions. It is acted upon by whatever client controls it.

- A **human** controls a player through the web UI (sees game state on screen, clicks buttons)
- A **bot** controls a player through the bot service (receives game state via MQTT, runs behavior logic, calls REST API)

The game server cannot tell the difference and does not try. Both use the same REST API to place bets. The player is the shared interface between all clients and the game server.

### Bot

A bot is a **bot service** concept — the game server has no knowledge of bots. A bot is:

- A reference to a **player account** (the bot doesn't own money — the player does)
- A **behavior** (the betting strategy — what to do given the current round)
- A **run loop** (watches game events via MQTT, evaluates behavior, calls REST API)

A bot does not need to understand craps rules. The game server enforces all rules. If a bot tries to place an illegal bet (wrong phase, insufficient balance), the API rejects it. The bot just follows its behavior recipe and lets the game server sort out validity.

### Behavior

A behavior is a **Python function** that decides what bets to place given the current round context. The bot service calls it on every game event and sends the resulting actions to the game server via REST.

Python was chosen over a declarative config (YAML) because the language already has everything behaviors need — conditionals, variables, math, state. Simple strategies are still short, and complex strategies (martingale, press bets, stop-loss) are possible without inventing a custom rule language.

**Simple — conservative pass line with odds:**

```python
# behaviors/conservative.py

def decide(ctx):
    bets = []
    if ctx.phase == "come_out" and "pass_line" not in ctx.my_bets:
        bets.append(Bet("pass_line", 10))
    if ctx.phase == "point" and "pass_line" in ctx.my_bets and "pass_odds" not in ctx.my_bets:
        bets.append(Bet("pass_odds", 20))
    return bets
```

**Complex — martingale (double after a loss):**

```python
# behaviors/martingale.py

last_bet = 10

def decide(ctx):
    global last_bet
    bets = []

    for r in ctx.resolutions:
        if r.type == "pass_line" and r.status == "lost":
            last_bet = last_bet * 2
        elif r.type == "pass_line" and r.status == "won":
            last_bet = 10

    if ctx.phase == "come_out" and "pass_line" not in ctx.my_bets:
        bets.append(Bet("pass_line", last_bet))

    return bets
```

Every behavior has the same interface: receive context, return a list of bet actions. The bot service doesn't need to interpret rules or run an evaluator — it just calls `decide()`.

### Context Available to Behaviors

The bot service builds this context from MQTT game events and passes it to the behavior function:

| Field | Description |
|-------|-------------|
| `phase` | Current round phase (`come_out` or `point`) |
| `point` | Current point number (null during come-out) |
| `last_roll` | Most recent roll (die1, die2, total) |
| `my_bets` | Bot's active bets on this table |
| `resolutions` | Bets resolved on the last roll (type, status, payout) |

The bot does not need to know its balance — the player has the balance, and the game server enforces it. If a bet is rejected for insufficient funds, the bot simply moves on.

### How It All Connects

```
Behavior (Python)             Bot Service                   Game Server
┌──────────────────┐    ┌─────────────────────┐    ┌──────────────────┐
│ conservative.py  │    │                     │    │                  │
│  decide(ctx) ────│───►│ Bot "Leroy"         │    │  Player "Leroy"  │
│  → [Bet(...)]    │    │   behavior: conserv. │──►│  balance: $1000  │
│                  │    │   player_id: 732    │REST│  bets: [...]     │
│                  │    │                     │    │                  │
│ martingale.py    │    │ Bot "Susan"         │    │  Player "Susan"  │
│  decide(ctx) ────│───►│   behavior: marting. │──►│  balance: $500   │
│  → [Bet(...)]    │    │   player_id: 733    │REST│  bets: [...]     │
│                  │    │                     │    │                  │
└──────────────────┘    │     ▲               │    │       │          │
                        │     │ MQTT game      │    │       │ MQTT     │
                        │     │ events         │◄───│───────┘ events   │
                        └─────────────────────┘    └──────────────────┘
```

1. Game server publishes game state (phase, point, resolutions) to MQTT
2. Bot service receives event, builds context, calls each bot's `decide(ctx)`
3. Returned bet actions are sent to game server via REST API
4. Game server validates and accepts/rejects — bot doesn't need to know why
5. Machine rolls → game server resolves bets → publishes results → back to step 1

### Bot Configuration

Bots are defined in a config file that maps names to player accounts and behaviors:

```yaml
bots:
  - name: Leroy
    player_id: "732"
    behavior: behaviors/conservative.py
    table_id: "abc-123"

  - name: Susan
    player_id: "733"
    behavior: behaviors/martingale.py
    table_id: "abc-123"
```

### Naming

- **Behavior** has a descriptive name matching its strategy: `conservative.py`, `martingale.py`, `iron_cross.py`
- **Bot** (and its player account) gets a fun name: "Leroy", "Susan", "Big Al"
- Multiple bots can run the same behavior with different names and bankrolls

---

## 10. Future Services

### Bot Service

Separate service, described above. Loads Python behavior files, runs bots, places bets via REST API. See section 9.

### Web UI

A website where humans play craps. Handles:
- Player account creation
- Table browsing, creation, joining
- Bet placement via game server REST API
- Live game state via MQTT over WebSockets
- Machine control (trigger mode, roll commands) via machine MQTT topics
