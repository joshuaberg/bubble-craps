import logging
import random
import time
import uuid
from datetime import datetime, timezone
from enum import Enum

from bubble_craps.config import AppConfig

logger = logging.getLogger(__name__)


class State(Enum):
    IDLE = "IDLE"
    TRIGGERED = "TRIGGERED"
    ROLLING = "ROLLING"
    SETTLING = "SETTLING"
    CAPTURE = "CAPTURE"
    ANALYZE = "ANALYZE"
    PUBLISH = "PUBLISH"
    ERROR = "ERROR"


class StateMachine:
    """Core state machine controlling the bubble craps roll cycle."""

    def __init__(self, config: AppConfig, motor, ring_light, camera, detector, mqtt_client):
        self.config = config
        self.motor = motor
        self.ring_light = ring_light
        self.camera = camera
        self.detector = detector
        self.mqtt_client = mqtt_client

        self.state = State.IDLE
        self.total_rolls = 0
        self.error_count = 0
        self.last_roll_id: str | None = None
        self._start_time = time.monotonic()

        # For mechanism integrity check
        self._previous_roll: dict | None = None

    @property
    def uptime_sec(self) -> int:
        return int(time.monotonic() - self._start_time)

    def transition(self, new_state: State) -> None:
        """Transition to a new state, updating the ring light and publishing status."""
        logger.info("State transition: %s -> %s", self.state.value, new_state.value)
        self.state = new_state
        self._update_ring_light()
        self.mqtt_client.publish_status(self._build_status())

    def _update_ring_light(self) -> None:
        """Set the ring light pattern based on the current state."""
        patterns = {
            State.IDLE: "idle",
            State.TRIGGERED: "rolling",
            State.ROLLING: "rolling",
            State.SETTLING: "rolling",
            State.CAPTURE: "capture",
            State.ANALYZE: "capture",
            State.PUBLISH: "success",
            State.ERROR: "error",
        }
        pattern = patterns.get(self.state, "off")
        self.ring_light.set_pattern(pattern)

    def trigger_roll(self) -> None:
        """Initiate a roll cycle. Called by timer or MQTT command."""
        if self.state == State.ERROR:
            logger.warning("Cannot trigger roll while in ERROR state")
            return
        if self.state != State.IDLE:
            logger.warning("Cannot trigger roll from state %s", self.state.value)
            return

        self.transition(State.TRIGGERED)
        self._run_roll_cycle()

    def clear_error(self) -> None:
        """Clear the ERROR state and return to IDLE."""
        if self.state != State.ERROR:
            logger.warning("clear_error called but not in ERROR state")
            return
        logger.info("Error cleared, returning to IDLE")
        self.transition(State.IDLE)

    def _run_roll_cycle(self) -> None:
        """Execute the full roll cycle: ROLLING -> SETTLING -> CAPTURE -> ANALYZE -> PUBLISH."""
        roll_id = uuid.uuid4().hex[:8]

        # ROLLING — randomize both speed and duration for true randomness
        self.transition(State.ROLLING)
        rpm = random.uniform(
            self.config.motor.roll_rpm_min,
            self.config.motor.roll_rpm_max,
        )
        duration = random.uniform(
            self.config.motor.roll_duration_min_sec,
            self.config.motor.roll_duration_max_sec,
        )
        logger.info("Roll %s: rpm=%.1f, duration=%.1fs", roll_id, rpm, duration)
        self.motor.start(rpm=rpm)
        time.sleep(duration)
        self.motor.stop()

        # SETTLING — move to park position and wait
        self.transition(State.SETTLING)
        self.motor.go_to_angle(
            self.config.motor.park_position,
            speed_limit=self.config.motor.park_speed_limit,
        )

        deadline = time.monotonic() + self.config.motor.park_timeout_sec
        while not self.motor.is_at_position():
            if time.monotonic() > deadline:
                logger.error("Motor failed to reach park position within timeout")
                break
            time.sleep(self.config.motor.park_poll_interval_sec)

        time.sleep(self.config.motor.settling_time_sec)

        # CAPTURE + ANALYZE with retries
        result = None
        retries = self.config.detection.max_retries

        for attempt in range(1 + retries):
            self.transition(State.CAPTURE)
            image = self.camera.capture()

            self.transition(State.ANALYZE)
            result = self.detector.detect(image)

            if result is not None:
                break

            if attempt < retries:
                logger.warning("Detection failed, retrying (%d/%d)", attempt + 1, retries)

        if result is None:
            logger.error("Detection failed after %d retries", retries)
            self.error_count += 1
            self.mqtt_client.publish_error({
                "roll_id": roll_id,
                "error": "detection_failed",
                "message": f"Could not detect exactly 2 dice after {retries} retries",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "device_id": self.config.mqtt.client_id,
            })
            self.transition(State.ERROR)
            return

        # Integrity check
        if self._check_mechanism_failure(result):
            logger.error("Mechanism failure detected — same pips and positions as previous roll")
            self.error_count += 1
            self.mqtt_client.publish_error({
                "roll_id": roll_id,
                "error": "mechanism_failure",
                "message": "Consecutive rolls have identical pip values and dice positions",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "device_id": self.config.mqtt.client_id,
            })
            self.transition(State.ERROR)
            return

        self._previous_roll = result

        # PUBLISH
        self.transition(State.PUBLISH)
        self.total_rolls += 1
        self.last_roll_id = roll_id

        roll_result = {
            "roll_id": roll_id,
            "die1": result["die1"],
            "die2": result["die2"],
            "total": result["die1"] + result["die2"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "device_id": self.config.mqtt.client_id,
        }
        self.mqtt_client.publish_result(roll_result)

        self.transition(State.IDLE)

    def _check_mechanism_failure(self, current: dict) -> bool:
        """Compare current roll against previous to detect mechanism failure."""
        if self._previous_roll is None:
            return False

        prev = self._previous_roll
        tolerance = self.config.detection.position_tolerance_px

        # Check if pip values match
        pips_match = (
            current["die1"] == prev["die1"]
            and current["die2"] == prev["die2"]
        )
        if not pips_match:
            return False

        # Check if positions match (within tolerance)
        positions_match = all(
            abs(c[0] - p[0]) <= tolerance and abs(c[1] - p[1]) <= tolerance
            for c, p in zip(current["positions"], prev["positions"])
        )

        return positions_match

    def _build_status(self) -> dict:
        return {
            "state": self.state.value,
            "trigger_mode": self.config.trigger.mode,
            "timer_interval_sec": self.config.trigger.timer_interval_sec,
            "uptime_sec": self.uptime_sec,
            "total_rolls": self.total_rolls,
            "last_roll_id": self.last_roll_id,
            "errors": self.error_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "device_id": self.config.mqtt.client_id,
        }
