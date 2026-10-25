import logging
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from bubble_craps.config import AppConfig

logger = logging.getLogger(__name__)

_MOTOR_ON_DELAY_SEC = 0.2


class State(Enum):
    IDLE = "IDLE"
    TRIGGERED = "TRIGGERED"
    ROLLING = "ROLLING"
    SETTLING = "SETTLING"
    CAPTURE = "CAPTURE"
    ANALYZE = "ANALYZE"
    ERROR = "ERROR"


@dataclass
class CaptureAttempt:
    """Record of a single capture+detect attempt."""
    capture: str   # "ok" or "failed"
    detect: str    # "ok", "null", or "skipped" (if capture failed)


@dataclass
class RollOutcome:
    """Result of a single roll or retry_capture call."""
    status: str             # "ok", "detection_failed", "camera_failure", "mechanism_failure"
    roll_id: str
    result: dict | None     # {"die1": int, "die2": int, "positions": [...]} or None
    image_bytes: bytes | None
    attempt: CaptureAttempt


class StateMachine:
    """Orchestrates hardware through the roll cycle and reports outcomes.

    The state machine drives hardware (motor, camera, ring light) and
    transitions through states for telemetry. It does NOT decide what to do
    about errors — it reports outcomes via RollOutcome and lets the caller
    (roll supervisor) decide retry/error policy.
    """

    def __init__(self, config: AppConfig, motor, ring_light, camera, detector,
                 on_status=None):
        self.config = config
        self.motor = motor
        self.ring_light = ring_light
        self.camera = camera
        self.detector = detector

        self.on_status = on_status

        self.state = State.IDLE
        self.last_roll_id: str | None = None
        self._error_reason: str | None = None
        self._start_time = time.monotonic()

        # For mechanism integrity check
        self._previous_roll: dict | None = None

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

    def run_roll(self) -> RollOutcome:
        """Full roll cycle: motor on, spin, park, settle, capture, detect.

        Returns a RollOutcome describing what happened. Motor is always
        stopped and turned off in the finally block. Unexpected exceptions
        propagate after motor cleanup so the supervisor can handle them.
        """
        roll_id = uuid.uuid4().hex
        self.transition(State.TRIGGERED)

        self.motor.turn_on()
        time.sleep(_MOTOR_ON_DELAY_SEC)

        try:
            self._do_rolling(roll_id)
            self._do_settling()
            return self._do_capture_and_detect(roll_id)
        finally:
            self.motor.stop()
            self.motor.turn_off()

    def retry_capture(self, roll_id: str) -> RollOutcome:
        """Re-capture and re-detect without re-rolling. Dice are still there."""
        return self._do_capture_and_detect(roll_id)

    def _do_rolling(self, roll_id: str) -> None:
        """ROLLING phase: spin motor at random RPM for random duration."""
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

    def _do_settling(self) -> None:
        """SETTLING phase: park motor and wait for dice to settle."""
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

    def _do_capture_and_detect(self, roll_id: str) -> RollOutcome:
        """CAPTURE + ANALYZE: single attempt to photograph and detect dice."""
        self.transition(State.CAPTURE)
        try:
            image = self.camera.capture()
        except Exception as exc:
            logger.warning("Camera capture failed: %s", exc)
            return RollOutcome(
                status="camera_failure",
                roll_id=roll_id,
                result=None,
                image_bytes=None,
                attempt=CaptureAttempt(capture="failed", detect="skipped"),
            )

        self.transition(State.ANALYZE)
        result = self.detector.detect(image)
        image_bytes = self._encode_image(image)

        if result is None:
            return RollOutcome(
                status="detection_failed",
                roll_id=roll_id,
                result=None,
                image_bytes=image_bytes,
                attempt=CaptureAttempt(capture="ok", detect="null"),
            )

        # Mechanism failure check
        if self._check_mechanism_failure(result):
            logger.error("Mechanism failure detected — same pips and positions as previous roll")
            return RollOutcome(
                status="mechanism_failure",
                roll_id=roll_id,
                result=result,
                image_bytes=image_bytes,
                attempt=CaptureAttempt(capture="ok", detect="ok"),
            )

        self._previous_roll = result
        return RollOutcome(
            status="ok",
            roll_id=roll_id,
            result=result,
            image_bytes=image_bytes,
            attempt=CaptureAttempt(capture="ok", detect="ok"),
        )

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

    def _update_ring_light(self) -> None:
        """Set the ring light pattern based on the current state."""
        patterns = {
            State.IDLE: "idle",
            State.TRIGGERED: "rolling",
            State.ROLLING: "rolling",
            State.SETTLING: "rolling",
            State.CAPTURE: "capture",
            State.ANALYZE: "capture",
            State.ERROR: "error",
        }
        pattern = patterns.get(self.state, "off")
        if self.ring_light:
            self.ring_light.set_pattern(pattern)

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
