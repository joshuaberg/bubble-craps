import logging
import queue
import threading
import time
import uuid
from datetime import datetime, timezone

from bubble_craps.config import AppConfig
from bubble_craps.state_machine import RollOutcome, State

logger = logging.getLogger(__name__)

_RETRY_CAPTURE_DELAY_SEC = 1.0
_REROLL_DELAY_SEC = 3.0
_MAX_CONSECUTIVE_UNEXPECTED_FAILURES = 3
_MAX_CONSECUTIVE_DETECTION_FAILURES = 5
_HEARTBEAT_INTERVAL_SEC = 300  # 5 minutes
_MIN_TIMER_INTERVAL_SEC = 60


class RollSupervisor:
    """Owns roll timing, retry policy, error state, and status publishing.

    A single worker thread consumes commands from a queue. The queue's
    get-timeout doubles as the timer mechanism — no separate timer loop.
    All retry and error-escalation logic lives here, keeping the state
    machine focused purely on driving hardware.
    """

    def __init__(self, config: AppConfig, state_machine, mqtt_client):
        self._config = config
        self._sm = state_machine
        self._mqtt = mqtt_client

        self._queue: queue.Queue = queue.Queue()
        self._shutdown = threading.Event()
        self._thread = threading.Thread(target=self._run, name="roll-supervisor", daemon=True)

        # Status state — supervisor is the single owner
        self._error = False
        self._error_reason: str | None = None
        self._last_roll_id: str | None = None
        self._start_time = time.monotonic()

        # Failure tracking
        self._consecutive_unexpected_failures = 0
        self._consecutive_detection_failures = 0

        # Timer
        self._next_timer_roll: float | None = None
        self._last_heartbeat = time.monotonic()

        # Roll-in-progress flag for queue dedup
        self._rolling = False

    def start(self) -> None:
        """Start the supervisor worker thread."""
        self._roll_immediately()
        self._thread.start()
        logger.info("Roll supervisor started")

    def stop(self) -> None:
        """Signal shutdown and wait for the worker thread to finish."""
        self._shutdown.set()
        self._queue.put(("_shutdown", {}))  # unblock get()
        self._thread.join(timeout=30)
        logger.info("Roll supervisor stopped")

    def enqueue(self, command: str, **kwargs) -> None:
        """Enqueue a command. Thread-safe, called from MQTT or main thread."""
        self._queue.put((command, kwargs))

    # -- Status (supervisor owns this) -------------------------------------

    def _on_transition(self, _new_state: State) -> None:
        """Called by state machine on every phase transition."""
        self._publish_status()

    def _build_status(self) -> dict:
        return {
            "state": self._sm.state.value,
            "trigger_mode": self._config.trigger.mode,
            "timer_interval_sec": self._config.trigger.timer_interval_sec,
            "uptime_sec": int(time.monotonic() - self._start_time),
            "last_roll_id": self._last_roll_id,
            "error_reason": self._error_reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "device_id": self._config.mqtt.client_id,
        }

    def _publish_status(self) -> None:
        self._mqtt.publish_status(self._build_status())

    # -- Timer management --------------------------------------------------

    def _reset_timer(self) -> None:
        if self._config.trigger.mode == "timer":
            self._next_timer_roll = time.monotonic() + self._config.trigger.timer_interval_sec
        else:
            self._next_timer_roll = None

    def _roll_immediately(self) -> None:
        """Set the next timer roll to now, triggering an immediate roll."""
        if self._config.trigger.mode == "timer":
            self._next_timer_roll = time.monotonic()

    def _time_until_next_roll(self) -> float:
        if self._next_timer_roll is None:
            return 1.0
        remaining = self._next_timer_roll - time.monotonic()
        return max(0.0, min(remaining, 1.0))

    def _should_auto_roll(self) -> bool:
        return (
            self._config.trigger.mode == "timer"
            and not self._error
            and self._next_timer_roll is not None
            and time.monotonic() >= self._next_timer_roll
        )

    # -- Worker loop -------------------------------------------------------

    def _run(self) -> None:
        while not self._shutdown.is_set():
            try:
                self._maybe_heartbeat()

                timeout = self._time_until_next_roll()

                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    if self._should_auto_roll():
                        self._execute_roll()
                    continue

                command, kwargs = item

                if command == "_shutdown":
                    break
                elif command == "roll":
                    self._execute_roll()
                elif command == "clear_error":
                    self._clear_error()
                elif command == "set_mode":
                    self._handle_set_mode(kwargs)
                else:
                    logger.warning("Unknown supervisor command: %s", command)
            except Exception:
                logger.exception("Unexpected error in supervisor loop, continuing")

    # -- Roll execution with retry policy ----------------------------------

    def _execute_roll(self) -> None:
        """Run a full roll and apply retry policy to the outcome."""
        if self._error:
            logger.warning("Cannot roll while in error state")
            return

        if self._rolling:
            logger.warning("Roll already in progress, ignoring")
            return

        self._rolling = True
        roll_id = uuid.uuid4().hex
        attempts = []

        try:
            outcome = self._sm.run_roll(roll_id)
            attempts.append(outcome.attempt)
        except Exception:
            logger.exception("Unexpected error during roll %s", roll_id)
            self._handle_unexpected_failure()
            return
        finally:
            self._rolling = False
            self._reset_timer()

        # If not ok, try recapture once (works for both detection_failed and camera_failure)
        if outcome.status in ("detection_failed", "camera_failure"):
            self._shutdown.wait(timeout=_RETRY_CAPTURE_DELAY_SEC)
            if self._shutdown.is_set():
                return
            try:
                retry = self._sm.retry_capture(roll_id)
                attempts.append(retry.attempt)
                outcome = retry
            except Exception:
                logger.exception("Unexpected error during retry_capture %s", roll_id)
                self._handle_unexpected_failure()
                return

        # Now decide based on final outcome
        if outcome.status == "ok":
            self._consecutive_unexpected_failures = 0
            self._consecutive_detection_failures = 0
            self._publish_result(outcome, attempts, "ok")
            self._sm.transition(State.IDLE)
            self._publish_status()
            return

        if outcome.status == "mechanism_failure":
            self._publish_image(outcome)
            self._enter_error("mechanism_failure")
            return

        if outcome.status == "camera_failure":
            # Camera failed on initial AND retry — genuinely broken
            self._enter_error("camera_failure")
            return

        if outcome.status == "detection_failed":
            # Publish the null result
            self._publish_result(outcome, attempts, "detection_failed")
            self._sm.transition(State.IDLE)
            self._publish_status()

            self._consecutive_detection_failures += 1
            if self._consecutive_detection_failures >= _MAX_CONSECUTIVE_DETECTION_FAILURES:
                logger.error(
                    "Entering ERROR after %d consecutive detection failures",
                    self._consecutive_detection_failures,
                )
                self._enter_error("repeated_detection_failure")
                return

            # In timer mode, schedule a re-roll. In remote mode, let the
            # caller decide — don't auto-reroll.
            if self._config.trigger.mode == "timer":
                self._shutdown.wait(timeout=_REROLL_DELAY_SEC)
                if not self._shutdown.is_set():
                    self._queue.put(("roll", {}))
            return

    def _handle_unexpected_failure(self) -> None:
        self._consecutive_unexpected_failures += 1
        if self._consecutive_unexpected_failures >= _MAX_CONSECUTIVE_UNEXPECTED_FAILURES:
            logger.error(
                "Entering ERROR after %d consecutive unexpected failures",
                self._consecutive_unexpected_failures,
            )
            self._enter_error("repeated_unexpected_failure")
        else:
            logger.warning(
                "Unexpected failure %d/%d, returning to IDLE",
                self._consecutive_unexpected_failures,
                _MAX_CONSECUTIVE_UNEXPECTED_FAILURES,
            )
            self._sm.transition(State.IDLE)
            self._publish_status()

    # -- Error state management --------------------------------------------

    def _enter_error(self, reason: str) -> None:
        self._error = True
        self._error_reason = reason
        self._sm.transition(State.ERROR)
        self._publish_status()

    def _clear_error(self) -> None:
        if not self._error:
            logger.warning("clear_error called but not in error state")
            return
        logger.info("Error cleared, returning to IDLE")
        self._error = False
        self._error_reason = None
        self._consecutive_unexpected_failures = 0
        self._consecutive_detection_failures = 0
        self._sm.transition(State.IDLE)
        self._reset_timer()
        self._publish_status()

    # -- set_mode (runs on worker thread) ----------------------------------

    def _handle_set_mode(self, kwargs: dict) -> None:
        mode = kwargs.get("mode")
        if mode not in ("timer", "remote"):
            logger.warning("Invalid mode: %s", mode)
            return
        self._config.trigger.mode = mode
        if mode == "timer" and "interval_sec" in kwargs:
            interval = kwargs["interval_sec"]
            if not isinstance(interval, (int, float)) or interval < _MIN_TIMER_INTERVAL_SEC:
                logger.warning("Rejected interval_sec=%s (minimum %ds)", interval, _MIN_TIMER_INTERVAL_SEC)
                return
            self._config.trigger.timer_interval_sec = int(interval)
        logger.info("Trigger mode changed to: %s", mode)
        self._roll_immediately()
        self._publish_status()

    # -- Publishing --------------------------------------------------------

    def _publish_result(self, outcome: RollOutcome, attempts: list, final_status: str) -> None:
        """Build and publish the enriched roll result payload."""
        timestamp = datetime.now(timezone.utc).isoformat()

        if outcome.result is not None:
            self._last_roll_id = outcome.roll_id
            roll_result = {
                "roll_id": outcome.roll_id,
                "die1": outcome.result["die1"],
                "die2": outcome.result["die2"],
                "total": outcome.result["die1"] + outcome.result["die2"],
                "roll_status": final_status,
                "roll_status_reason": None,
                "attempts": [{"capture": a.capture, "detect": a.detect} for a in attempts],
                "motor_temp_c": outcome.motor_temp_c,
                "timestamp": timestamp,
                "device_id": self._config.mqtt.client_id,
            }
        else:
            roll_result = {
                "roll_id": outcome.roll_id,
                "die1": None,
                "die2": None,
                "total": None,
                "roll_status": final_status,
                "roll_status_reason": "Could not detect exactly 2 dice",
                "attempts": [{"capture": a.capture, "detect": a.detect} for a in attempts],
                "motor_temp_c": outcome.motor_temp_c,
                "timestamp": timestamp,
                "device_id": self._config.mqtt.client_id,
            }

        self._mqtt.publish_result(roll_result)
        if outcome.image_bytes:
            self._mqtt.publish_image(outcome.image_bytes, outcome.roll_id)

    def _publish_image(self, outcome: RollOutcome) -> None:
        """Publish just the image (e.g. mechanism failure evidence)."""
        if outcome.image_bytes:
            self._mqtt.publish_image(outcome.image_bytes, outcome.roll_id)

    # -- Heartbeat ---------------------------------------------------------

    def _maybe_heartbeat(self) -> None:
        now = time.monotonic()
        if (self._sm.state == State.IDLE
                and now - self._last_heartbeat >= _HEARTBEAT_INTERVAL_SEC):
            self._publish_status()
            self._last_heartbeat = now
