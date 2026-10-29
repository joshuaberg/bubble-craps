# Bubble Craps Machine — Architecture

## 1. Overview

The Bubble Craps Machine is a self-contained dice-rolling device. It physically juggles two dice via a crankshaft mechanism, captures an image of the resting dice, determines the pip count on each die using computer vision, and publishes the results over MQTT.

The machine does **not** contain any craps game logic. It is purely a dice roller and reporter. Game logic, betting, display, and statistics are handled by downstream services that subscribe to the MQTT broker. This separation means the Pi never needs to change when game rules evolve, and any number of consumers can independently process roll data.

### Design Goals

- Reliable, repeatable physical dice rolls with true randomness
- Accurate pip detection via classical computer vision
- Clean MQTT interface supporting multiple machines on one broker
- Configurable trigger modes (remote command or timed interval)

---

## 2. System Architecture

```
                         ┌──────────────────────┐
                         │    MQTT Broker        │
                         │  (Mosquitto)          │
                         └──────┬───────▲────────┘
                        subscribe│       │publish
                      (commands) │       │(results, images, status)
                         ┌───────▼───────┴────────┐
                         │   Raspberry Pi Zero 2W  │
                         │                         │
                         │  ┌───────────────────┐  │
                         │  │  Roll Supervisor   │  │
                         │  │  (timer, retry,    │  │
                         │  │   error policy,    │  │
                         │  │   status, publish) │  │
                         │  └────────┬──────────┘  │
                         │           │              │
                         │  ┌────────▼──────────┐  │
                         │  │   State Machine    │  │
                         │  │  (hardware only)   │  │
                         │  └─┬───┬───┬───┬─────┘  │
                         │    │   │   │   │         │
                 ┌───────┼────┘   │   │   └────┼────────────┐
                 │       │        │   │        │            │
           ┌─────▼──┐ ┌──▼───┐ ┌─▼───▼──┐ ┌───▼─────┐     │
           │ Motor   │ │Ring  │ │Pi Cam  │ │OpenCV   │     │
           │ (CAN)   │ │Light │ │Module  │ │Pipeline │     │
           │         │ │(GPIO)│ │        │ │         │     │
           └─────────┘ └──────┘ └────────┘ └─────────┘     │
                         │                         │
                         └─────────────────────────┘
```

The system is split into two layers:

- **Roll Supervisor** — the central coordinator. Owns the command queue, timer, retry/error policy, MQTT publishing, and error state.
- **State Machine** — drives hardware through roll phases and reports outcomes. Has no knowledge of MQTT, retries, or error policy.

This split keeps hardware sequencing testable in isolation (no broker needed) and puts all policy decisions in one place. When retry logic or error thresholds change, only the supervisor is touched.

### How They Communicate

The state machine's `run_roll()` method returns a `RollOutcome` dataclass describing what happened (ok, detection_failed, camera_failure, mechanism_failure) along with the detection result, image bytes, and attempt record. The supervisor inspects this outcome and decides what to do — publish, retry, or enter ERROR.

The state machine also accepts a single `on_transition` callback, called on every phase change (IDLE → TRIGGERED → ROLLING → etc.). The supervisor uses this to publish status updates over MQTT on each transition, so subscribers see the machine move through its phases in real time.

This means the state machine never imports or references the MQTT client. It only knows how to drive hardware and report what happened. All MQTT publishing, status building, result formatting, and error policy live in the supervisor.

Downstream consumers (game server, dashboard, SQL bridge) subscribe to the broker independently. The Pi has no knowledge of what is consuming its messages.

---

## 3. Hardware

### 3.1 Raspberry Pi Zero 2W

Main controller. Connects to the network via Wi-Fi for MQTT communication.

Interfaces: CSI (camera), GPIO (ring light), CAN bus (motor via Waveshare RS485 CAN HAT).

### 3.2 Pi Camera Module

Connected via CSI ribbon cable. Captures a still image of the dice after they settle at 1920x1080 (full res for the CV pipeline, downsampled for MQTT publish).

Exposure and white balance are locked (not auto-adjusting). Auto settings would cause frame-to-frame variation that breaks the fixed thresholds in the detection pipeline.

### 3.3 Motor + Crankshaft

LK brushless DC motor with integrated encoder (multi-turn position feedback), controlled via CAN bus (CAN ID 0x141, socketcan interface).

CAN was chosen because the motor has an integrated CAN controller with position feedback built in — no external encoder or stepper driver needed.

The motor drives a crankshaft that tumbles the dice. After rolling, the motor parks at a configurable angle so the dice are visible to the camera.

### 3.4 Ring Light

24-LED NeoPixel ring on GPIO 13 (rpi_ws281x). Provides consistent, even illumination for dice imaging. Different patterns indicate system state:

| Pattern | State(s) | Description |
|---------|----------|-------------|
| Idle | IDLE | Steady white at configured brightness |
| Rolling | TRIGGERED, ROLLING, SETTLING | Steady white at configured brightness |
| Full brightness | CAPTURE, ANALYZE | Steady, even light for imaging |
| Solid red | ERROR | System halted |
| Off | Shutdown | — |

---

## 4. State Machine

```
  ┌────────────────────────────────────────────────────────┐
  │                                                        │
  ▼                                                        │
IDLE ──► TRIGGERED ──► ROLLING ──► SETTLING ──► CAPTURE    │
                                                  │        │
                                                  ▼        │
                                               ANALYZE ────┘
                                                  │
                                        (supervisor decides)
                                                  │
                                               ERROR
                                            (solid red)
                                                  │
                                          power reset or
                                          clear_error cmd
                                                  │
                                                  ▼
                                                IDLE
```

| State | What happens | Duration |
|-------|-------------|----------|
| **IDLE** | Waiting for a trigger. Motor off. Ring light at idle brightness. | Until triggered |
| **TRIGGERED** | Trigger received (MQTT command or timer). | Immediate transition |
| **ROLLING** | Motor running at randomized RPM, dice tumbling. | Random 3–5s (configurable) |
| **SETTLING** | Motor commanded to park position. Polls until arrived or timeout. Waits for dice to stop moving. | Poll timeout (5s) + settling wait (2s) |
| **CAPTURE** | Ring light full brightness. Camera captures image. | < 1s |
| **ANALYZE** | OpenCV pipeline processes image. Integrity check against previous roll. | < 2s |
| **ERROR** | System halted. Ring light solid red. Set by the supervisor, not the state machine. | Until cleared |

The state machine returns a `RollOutcome` after ANALYZE. It does not retry or escalate — that's the supervisor's job. This means the state machine is a pure function of hardware: run the phases, report what happened.

### Trigger Modes

Two mutually exclusive modes, switchable at runtime via MQTT command:

- **Timer** — rolls automatically at a configurable interval
- **Remote** — rolls only when commanded via MQTT

A manual `roll` command is accepted in timer mode (if the machine is IDLE) and resets the timer countdown.

### Randomness

Motor run time and RPM both vary randomly within configurable ranges per roll. This ensures genuine physical randomness in dice outcomes — the motor doesn't repeat the same motion.

---

## 5. Dice Detection

Classical computer vision was chosen over ML because the environment is fully controlled (fixed camera, fixed lighting, white dice on a dark surface). Deterministic thresholds are reliable under these conditions, require no training data, and run comfortably on a Pi Zero.

### 5.1 Pipeline

1. **Capture** — acquire image at configured resolution
2. **Preprocessing** — grayscale, Gaussian blur, sharpen
3. **Thresholding** — global threshold to isolate white dice faces
4. **Square detection** — find contours, filter for quadrilaterals with ~1:1 aspect ratio and expected area
5. **Center extraction** — centroid of each detected square (used for integrity check)
6. **Perspective correction** — warp each die face to a clean top-down view
7. **Pip detection** — blob detection within each corrected ROI to count pips
8. **Validation** — exactly 2 dice, each with 1–6 pips
9. **Integrity check** — compare results and positions against previous roll

### 5.2 Lighting Coupling

The detection threshold and pip/die size parameters are tightly coupled to the ring light brightness and camera exposure settings. Changing either requires re-running the calibration routine (`scripts/calibrate.py`), which tunes threshold, blob size, square area range, and position tolerance. Settings are persisted to `calibration.json`.

### 5.3 Mechanism Integrity Check

After each successful detection, the current result is compared to the previous roll to catch mechanism failures (e.g., the crankshaft didn't actually move the dice).

| Condition | Meaning |
|-----------|---------|
| Same pips, different positions | Legitimate repeat roll |
| Different pips, same positions | Coincidental landing spot |
| **Same pips AND same positions** | Dice didn't move — mechanism failure → ERROR |

Both signals must match to trigger an error. Either alone is expected to happen naturally.

---

## 6. MQTT Interface

MQTT was chosen over HTTP or WebSockets because the pub/sub model is a natural fit: one producer (the machine), many independent consumers. MQTT's Last Will and Testament (LWT) gives free offline detection, QoS levels provide delivery guarantees, and the protocol is lightweight enough for a Pi Zero.

### 6.1 Connection

| Setting | Value |
|---------|-------|
| Broker | Configurable (Mosquitto) |
| Client ID | From config (e.g., `bubble-craps-01`) — also used as `device_id` in payloads |
| Clean session | `true` |
| Keepalive | 60s |
| Auto-reconnect | Yes, with exponential backoff |
| LWT topic | `bubble-craps/{device_id}/status` |
| LWT payload | `{"state": "OFFLINE", "device_id": "..."}` retained, QoS 1 |

### 6.2 Topics

All topics are device-namespaced so multiple machines can share one broker. Subscribers use the `+` wildcard to receive from all machines (e.g., `bubble-craps/+/roll/result`).

| Topic | Direction | QoS | Retained | Why |
|---|---|---|---|---|
| `bubble-craps/{device_id}/roll/result` | Publish | 1 | No | Results are events — new subscribers shouldn't see stale rolls |
| `bubble-craps/{device_id}/roll/image/{roll_id}` | Publish | 1 | No | Paired with result by roll_id in the topic |
| `bubble-craps/{device_id}/status` | Publish | 1 | Yes | Retained so new subscribers immediately know machine state |
| `bubble-craps/{device_id}/command` | Subscribe | 1 | — | Each device subscribes only to its own topic |

### 6.3 Roll Result Payload

Published after every roll attempt — successful or not.

```json
{
  "roll_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
  "die1": 3,
  "die2": 5,
  "total": 8,
  "roll_status": "ok",
  "roll_status_reason": null,
  "attempts": [{"capture": "ok", "detect": "ok"}],
  "motor_temp_c": 42,
  "timestamp": "2026-05-29T14:30:00.000Z",
  "device_id": "bubble-craps-01"
}
```

On detection failure, `die1`/`die2`/`total` are `null` and `roll_status` is `"detection_failed"`. The machine continues rolling — detection failure is not a fatal error.

| Field | Description |
|---|---|
| `roll_id` | Full UUID4 hex — globally unique across machines |
| `die1` / `die2` / `total` | Pip values, null on detection failure |
| `roll_status` | `"ok"` or `"detection_failed"` |
| `roll_status_reason` | null on success, human-readable detail on failure |
| `attempts` | List of `{capture, detect}` per attempt. Rising attempt counts signal CV health degradation. |
| `motor_temp_c` | Motor temperature in Celsius after rolling, null if unavailable |

### 6.4 Roll Image

Raw binary JPEG bytes, downsampled from full capture resolution. Published alongside every roll result (success or failure). The `roll_id` in the topic matches the result payload, allowing consumers to pair them.

### 6.5 Status Payload

Published on every state transition, as a heartbeat every 5 minutes while IDLE, and in response to a `status` command.

```json
{
  "state": "IDLE",
  "trigger_mode": "timer",
  "timer_interval_sec": 120,
  "uptime_sec": 3600,
  "last_roll_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
  "error_reason": null,
  "timestamp": "2026-05-29T14:30:00.000Z",
  "device_id": "bubble-craps-01"
}
```

On unexpected disconnect, the broker publishes the LWT, overwriting the retained status with `{"state": "OFFLINE"}`.

`error_reason` is null unless state is ERROR. Possible values: `"mechanism_failure"`, `"camera_failure"`, `"repeated_unexpected_failure"`, `"repeated_detection_failure"`.

### 6.6 Commands

```json
{ "action": "roll" }
{ "action": "set_mode", "mode": "timer", "interval_sec": 120 }
{ "action": "set_mode", "mode": "remote" }
{ "action": "status" }
{ "action": "clear_error" }
```

`clear_error` returns the machine from ERROR to IDLE. Power reset has the same effect.

### 6.7 Offline Roll Queue

In timer mode, the machine keeps rolling even if the broker is unreachable. Results are queued in memory, backed by periodic flush to disk (`pending_rolls.json`), and published in order when the connection is restored. Timestamps reflect the actual capture time, not the publish time.

Images are **not** queued — only roll results are persisted offline. The queue has a configurable max size (default 100); oldest rolls are dropped if full.

Not applicable in remote mode — the broker must be up to receive roll commands.

---

## 7. Error Handling

All error handling is owned by the roll supervisor. The state machine reports outcomes; the supervisor decides what to do. This keeps policy in one place — changing retry thresholds or escalation behavior only touches one module.

| Outcome | Supervisor action |
|---------|------------------|
| **ok** | Publish result + image. Reset all failure counters. |
| **detection_failed** | Retry capture once (same dice — catches transient blur). If still failing, publish null result. Timer mode: schedule a re-roll. Remote mode: let the caller decide. ERROR after 5 consecutive failures. |
| **camera_failure** | Retry capture once. If still failing, enter ERROR. |
| **mechanism_failure** | Enter ERROR immediately. Publish image for evidence. |
| **Unexpected exception** | Return to IDLE. ERROR after 3 consecutive unexpected failures. |

### Motor Safety

- Motor is turned on at the start of each roll and always stopped + turned off in a `finally` block
- Motor is stopped and turned off at startup (safe state after crash)
- `motor.shutdown()` called on process exit

### ERROR State

Owned by the supervisor. Cleared by `clear_error` MQTT command or power reset.

Possible reasons: `"mechanism_failure"`, `"camera_failure"`, `"repeated_unexpected_failure"`, `"repeated_detection_failure"`.

---

## 8. Configuration

All configuration loaded from `config.yaml`. Runtime changes via MQTT command (trigger mode, timer interval) are in-memory only and revert to `config.yaml` defaults on reboot.

See `config.yaml` for the full schema and current values.

---

## 9. Tech Stack

| Component | Library |
|-----------|---------|
| Language | Python 3.11+ |
| Camera | picamera2 |
| Computer Vision | OpenCV (cv2) |
| MQTT | paho-mqtt |
| CAN Bus | python-can (socketcan) |
| Ring Light | rpi_ws281x |
| Configuration | PyYAML |
| Logging | Python stdlib `logging` |
