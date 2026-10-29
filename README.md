# Bubble Craps

A physical dice-rolling machine and server platform for playing craps.

## Project Structure

```
machines/craps/    Raspberry Pi dice-rolling machine (motor, camera, CV, MQTT publisher)
server/            Server-side services (broker, database bridge, game server, dashboard)
```

## Machines

### Craps Machine (`machines/craps/`)

A Raspberry Pi Zero 2W that physically rolls two dice via a crankshaft mechanism, captures an image, detects pip counts using OpenCV, and publishes results over MQTT.

See [machines/craps/ARCHITECTURE.md](machines/craps/ARCHITECTURE.md) for the full spec and [machines/craps/deploy/DEPLOY.txt](machines/craps/deploy/DEPLOY.txt) for setup instructions.

## Server

Server-side services that consume MQTT messages from the machine(s). Planned components:

- **Mosquitto broker** — MQTT message bus (Docker)
- **SQL bridge** — persists roll results to a database
- **Game server** — craps game logic
- **Dashboard** — web UI for monitoring and stats

See [server/ARCHITECTURE.md](server/ARCHITECTURE.md) for the server spec and roadmap.
