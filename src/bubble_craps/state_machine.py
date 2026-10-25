import logging
import random
import time
import uuid
from datetime import datetime, timezone
from enum import Enum

from bubble_craps.config import AppConfig

logger = logging.getLogger(__name__)

_CAPTURE_MAX_ATTEMPTS = 2
_CAPTURE_RETRY_DELAY_SEC = 1.0


class State(Enum):
    IDLE = "IDLE"
    TRIGGERED = "TRIGGERED"
    ROLLING = "ROLLING"
    SETTLING = "SETTLING"
    CAPTURE = "CAPTURE"
    ANALYZE = "ANALYZE"
    PUBLISH = "PUBLISH"
    ERROR = "ERROR"


_MAX_CONSECUTIVE_UNEXPECTED_FAILURES = 3
_MOTOR_ON_DELAY_SEC = 0.2


class StateMachine:
    """Core state machine controlling the bubble craps roll cycle.

    The state machine orchestrates hardware and emits events via callbacks.
    It has no knowledge of MQTT, databases, or any specific output — the
    caller wires callbacks to whatever output they need.
    """

    def __init__(self, config: AppConfig, motor, ring_light, camera, detector,
                 on_status=None, on_result=None, on_image=None):
        self.config = config
        self.motor = motor
        self.ring_light = ring_light
        self.camera = camera
        self.detector = detector

        self.on_status = on_status
        self.on_result = on_result
        self.on_image = on_image

        self.state = State.IDLE
        self.last_roll_id: str | None = None
        self._error_reason: str | None = None
        self._start_time = time.monotonic()

        # For mechanism integrity check
        self._previous_roll: dict | None = None

        self._consecutive_unexpected_failures = 0

    @property
    def uptime_sec(self) -> int:
        return int(time.monotonic() - self._start_time)

    def transition(self, new_state: State, error_reason: str | None = None) -> None:
        """Transition to a new state, updating the ring light and emitting status."""
        logger.info("State transition: %s -> %s", self.state.value, new_state.value)
        self.state = new_state
        if new_state == State.ERROR:
            self._error_reason = error_reason
        elif new_state == State.IDLE:
            self._error_reason = None
        self._update_ring_light()
        if self.on_status:
            self.on_status(self._build_status())

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
        if self.ring_light:
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
        roll_id = uuid.uuid4().hex

        self.motor.turn_on()
        time.sleep(_MOTOR_ON_DELAY_SEC)

        try:
            self._run_roll_cycle_inner(roll_id)
        except Exception:
            logger.exception("Unexpected error during roll cycle %s", roll_id)
            self._consecutive_unexpected_failures += 1
            if self._consecutive_unexpected_failures >= _MAX_CONSECUTIVE_UNEXPECTED_FAILURES:
                logger.error(
                    "Entering ERROR state after %d consecutive unexpected failures",
                    self._consecutive_unexpected_failures,
                )
                self.transition(State.ERROR, error_reason="repeated_unexpected_failure")
            else:
                logger.warning(
                    "Returning to IDLE (unexpected failure %d/%d)",
                    self._consecutive_unexpected_failures,
                    _MAX_CONSECUTIVE_UNEXPECTED_FAILURES,
                )
                self.transition(State.IDLE)
        finally:
            self.motor.stop()
            self.motor.turn_off()

    def _run_roll_cycle_inner(self, roll_id: str) -> None:
        """Inner roll cycle, called within motor on/off and error handling."""

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
        self.motor.spin(rpm=rpm)
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

        # CAPTURE — separate retry loop for hardware failures
        image = None
        for capture_attempt in range(_CAPTURE_MAX_ATTEMPTS):
            self.transition(State.CAPTURE)
            try:
                image = self.camera.capture()
                break
            except Exception as exc:
                logger.warning("Camera capture failed (attempt %d/%d): %s",
                               capture_attempt + 1, _CAPTURE_MAX_ATTEMPTS, exc)
                if capture_attempt < _CAPTURE_MAX_ATTEMPTS - 1:
                    time.sleep(_CAPTURE_RETRY_DELAY_SEC)

        if image is None:
            logger.error("Camera hardware failure after %d attempts", _CAPTURE_MAX_ATTEMPTS)
            self.transition(State.ERROR, error_reason="camera_failure")
            return

        # ANALYZE — detection retry loop
        result = None
        detection_attempts = self.config.detection.max_retries + 1

        for attempt in range(detection_attempts):
            self.transition(State.ANALYZE)
            result = self.detector.detect(image)

            if result is not None:
                break

            if attempt < detection_attempts - 1:
                logger.warning("Detection failed, retrying (%d/%d)", attempt + 1, detection_attempts - 1)
                # Re-capture for next detection attempt
                self.transition(State.CAPTURE)
                try:
                    image = self.camera.capture()
                except Exception as exc:
                    logger.error("Camera failed during detection retry: %s", exc)
                    self.transition(State.ERROR, error_reason="camera_failure")
                    return

        # Downsample image for output
        image_bytes = self._encode_image(image)

        # Integrity check — only on successful detection
        if result is not None and self._check_mechanism_failure(result):
            logger.error("Mechanism failure detected — same pips and positions as previous roll")
            if self.on_image:
                self.on_image(image_bytes)
            self.transition(State.ERROR, error_reason="mechanism_failure")
            return

        # PUBLISH
        self.transition(State.PUBLISH)

        timestamp = datetime.now(timezone.utc).isoformat()

        if result is not None:
            self._previous_roll = result
            self.last_roll_id = roll_id
            roll_result = {
                "roll_id": roll_id,
                "die1": result["die1"],
                "die2": result["die2"],
                "total": result["die1"] + result["die2"],
                "roll_status": "ok",
                "roll_status_reason": None,
                "attempts": attempt + 1,
                "timestamp": timestamp,
                "device_id": self.config.mqtt.client_id,
            }
        else:
            logger.warning("Detection failed after all attempts — publishing null roll")
            roll_result = {
                "roll_id": roll_id,
                "die1": None,
                "die2": None,
                "total": None,
                "roll_status": "detection_failed",
                "roll_status_reason": "Could not detect exactly 2 dice",
                "attempts": detection_attempts,
                "timestamp": timestamp,
                "device_id": self.config.mqtt.client_id,
            }

        if self.on_result:
            self.on_result(roll_result)
        if self.on_image:
            self.on_image(image_bytes)

        self._consecutive_unexpected_failures = 0
        self.transition(State.IDLE)

    def _encode_image(self, image) -> bytes:
        """Downsample and encode a numpy image to JPEG bytes for publishing."""
        import cv2
        target_width = 640
        h, w = image.shape[:2]
        scale = target_width / w
        small = cv2.resize(image, (target_width, int(h * scale)))
        _, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes()

    def _check_mechanism_failure(self, current: dict) -> bool:
        """Compare current roll against previous to detect mechanism failure."""
        if self._previous_roll is None:
            return False

        prev = self._previous_roll
        tolerance = self.config.detection.position_tolerance_px

        pips_match = (
            current["die1"] == prev["die1"]
            and current["die2"] == prev["die2"]
        )
        if not pips_match:
            return False

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
            "last_roll_id": self.last_roll_id,
            "error_reason": self._error_reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "device_id": self.config.mqtt.client_id,
        }
