import logging
import random
import time
import uuid
from dataclasses import dataclass
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
    motor_temp_c: int | None = None  # motor temperature after rolling, if available


class StateMachine:
    """Drives hardware through the roll cycle and reports outcomes.

    The state machine only does three things:
    1. Drive hardware (motor, camera, detector) through phases
    2. Update the ring light for each phase
    3. Notify a listener on each phase transition
    4. Return a RollOutcome describing what happened

    It does NOT own status, error policy, roll IDs, publishing, or retries.
    The roll supervisor handles all of that.
    """

    def __init__(self, config: AppConfig, motor, ring_light, camera, detector,
                 on_transition=None):
        self.config = config
        self.motor = motor
        self.ring_light = ring_light
        self.camera = camera
        self.detector = detector

        self._on_transition = on_transition
        self.state = State.IDLE

        # For mechanism integrity check
        self._previous_roll: dict | None = None
        self._last_motor_temp_c: int | None = None

    def transition(self, new_state: State) -> None:
        """Move to a new state, update ring light, notify listener."""
        logger.info("State transition: %s -> %s", self.state.value, new_state.value)
        self.state = new_state
        self._update_ring_light()
        if self._on_transition:
            self._on_transition(new_state)

    def run_roll(self, roll_id: str) -> RollOutcome:
        """Full roll cycle: motor on, spin, park, settle, capture, detect.

        Returns a RollOutcome. Motor is always stopped and turned off
        in the finally block. Unexpected exceptions propagate after
        motor cleanup so the supervisor can handle them.
        """
        self.transition(State.TRIGGERED)

        try:
            self.motor.turn_on()
            time.sleep(_MOTOR_ON_DELAY_SEC)
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

        # Read motor temperature right after spinning, while still on
        status = self.motor.get_status()
        self._last_motor_temp_c = status.temp_c if status else None

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

    _CAPTURE_LIGHT_SETTLE_SEC = 0.1

    def _do_capture_and_detect(self, roll_id: str) -> RollOutcome:
        """CAPTURE + ANALYZE: single attempt to photograph and detect dice."""
        self.transition(State.CAPTURE)
        time.sleep(self._CAPTURE_LIGHT_SETTLE_SEC)
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
                motor_temp_c=self._last_motor_temp_c,
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
                motor_temp_c=self._last_motor_temp_c,
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
                motor_temp_c=self._last_motor_temp_c,
            )

        self._previous_roll = result
        return RollOutcome(
            status="ok",
            roll_id=roll_id,
            result=result,
            image_bytes=image_bytes,
            attempt=CaptureAttempt(capture="ok", detect="ok"),
            motor_temp_c=self._last_motor_temp_c,
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
