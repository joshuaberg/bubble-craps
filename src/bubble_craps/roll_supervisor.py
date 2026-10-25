import logging
import queue
import threading
import time
from datetime import datetime, timezone

from bubble_craps.state_machine import RollOutcome, State

logger = logging.getLogger(__name__)

_RETRY_CAPTURE_DELAY_SEC = 1.0
_REROLL_DELAY_SEC = 3.0
_MAX_CONSECUTIVE_UNEXPECTED_FAILURES = 3
_HEARTBEAT_INTERVAL_SEC = 300  # 5 minutes


class RollSupervisor:
    """Owns roll timing, retry policy, and error state.

    A single worker thread consumes commands from a queue. The queue's
    get-timeout doubles as the timer mechanism — no separate timer loop.
    All retry and error-escalation logic lives here in _apply_policy(),
    keeping the state machine focused on hardware orchestration.
    """

    def __init__(self, config, state_machine, mqtt_client):
        self._config = config
        self._sm = state_machine
        self._mqtt = mqtt_client

        self._queue: queue.Queue = queue.Queue()
        self._shutdown = threading.Event()
        self._thread = threading.Thread(target=self._run, name="roll-supervisor", daemon=True)

        self._error = False
        self._error_reason: str | None = None
        self._consecutive_unexpected_failures = 0
        self._next_timer_roll: float | None = None
        self._last_heartbeat = time.monotonic()

    def start(self) -> None:
        """Start the supervisor worker thread."""
        self._reset_timer()
        self._thread.start()
        logger.info("Roll supervisor started")

    def stop(self) -> None:
        """Signal shutdown and wait for the worker thread to finish."""
        self._shutdown.set()
        self._queue.put(None)  # unblock get()
        self._thread.join(timeout=30)
        logger.info("Roll supervisor stopped")

    def enqueue(self, command: str) -> None:
        """Enqueue a command. Thread-safe, called from MQTT or main thread."""
        self._queue.put(command)

    # -- Timer management --------------------------------------------------

    def reset_timer(self) -> None:
        """Recalculate next auto-roll time. Called when mode/interval changes."""
        self._reset_timer()

    def _reset_timer(self) -> None:
        if self._config.trigger.mode == "timer":
            self._next_timer_roll = time.monotonic() + self._config.trigger.timer_interval_sec
        else:
            self._next_timer_roll = None

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
            self._maybe_heartbeat()

            timeout = self._time_until_next_roll()

            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                if self._should_auto_roll():
                    self._execute_roll()
                continue

            if item is None:  # shutdown sentinel
                break

            if item == "roll":
                self._execute_roll()
            elif item == "clear_error":
                self._clear_error()
            else:
                logger.warning("Unknown supervisor command: %s", item)

    # -- Roll execution with retry policy ----------------------------------

    def _execute_roll(self) -> None:
        """Run a full roll and apply retry policy to the outcome."""
        if self._error:
            logger.warning("Cannot roll while in error state")
            return

        attempts = []

        try:
            outcome = self._sm.run_roll()
        except Exception:
            logger.exception("Unexpected error during roll")
            self._handle_unexpected_failure()
            return

        attempts.append(outcome.attempt)
        self._apply_policy(outcome, attempts)

    def _apply_policy(self, outcome: RollOutcome, attempts: list) -> None:
        """Central retry/error policy. All roll-level decisions live here."""

        if outcome.status == "ok":
            self._consecutive_unexpected_failures = 0
            self._publish_result(outcome, attempts, "ok")
            self._reset_timer()
            return

        if outcome.status == "detection_failed":
            # Dice might be blurry or cocked — retry capture once
            time.sleep(_RETRY_CAPTURE_DELAY_SEC)
            retry = self._try_retry_capture(outcome.roll_id)
            if retry is None:
                # retry_capture itself threw an unexpected exception
                return

            attempts.append(retry.attempt)

            if retry.status == "ok":
                self._consecutive_unexpected_failures = 0
                self._publish_result(retry, attempts, "ok")
                self._reset_timer()
                return

            if retry.status == "mechanism_failure":
                self._publish_image(retry)
                self._enter_error("mechanism_failure")
                return

            # Still unreadable — publish null result, schedule a fresh re-roll
            self._publish_result(outcome, attempts, "detection_failed")
            self._enqueue_reroll()
            return

        if outcome.status == "camera_failure":
            # Camera hiccup? retry capture once
            time.sleep(_RETRY_CAPTURE_DELAY_SEC)
            retry = self._try_retry_capture(outcome.roll_id)
            if retry is None:
                return

            attempts.append(retry.attempt)

            if retry.status == "ok":
                self._consecutive_unexpected_failures = 0
                self._publish_result(retry, attempts, "ok")
                self._reset_timer()
                return

            if retry.status == "mechanism_failure":
                self._publish_image(retry)
                self._enter_error("mechanism_failure")
                return

            if retry.status == "camera_failure":
                # Camera genuinely broken
                self._enter_error("camera_failure")
                return

            # Camera worked on retry but detection failed — publish null, reroll
            self._publish_result(retry, attempts, "detection_failed")
            self._enqueue_reroll()
            return

        if outcome.status == "mechanism_failure":
            self._publish_image(outcome)
            self._enter_error("mechanism_failure")
            return

    def _try_retry_capture(self, roll_id: str) -> RollOutcome | None:
        """Attempt a retry_capture, handling unexpected exceptions.

        Returns the RollOutcome, or None if an unexpected exception occurred
        (in which case _handle_unexpected_failure has already been called).
        """
        try:
            return self._sm.retry_capture(roll_id)
        except Exception:
            logger.exception("Unexpected error during retry_capture")
            self._handle_unexpected_failure()
            return None

    def _handle_unexpected_failure(self) -> None:
        self._consecutive_unexpected_failures += 1
        if self._consecutive_unexpected_failures >= _MAX_CONSECUTIVE_UNEXPECTED_FAILURES:
            logger.error(
                "Entering ERROR state after %d consecutive unexpected failures",
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

    def _enqueue_reroll(self) -> None:
        """Schedule a re-roll after a brief pause.

        Uses the shutdown event for the delay so we don't block shutdown.
        """
        self._shutdown.wait(timeout=_REROLL_DELAY_SEC)
        if not self._shutdown.is_set():
            self._queue.put("roll")

    # -- Error state management --------------------------------------------

    def _enter_error(self, reason: str) -> None:
        self._error = True
        self._error_reason = reason
        self._sm.transition(State.ERROR, error_reason=reason)

    def _clear_error(self) -> None:
        if not self._error:
            logger.warning("clear_error called but not in error state")
            return
        logger.info("Error cleared, returning to IDLE")
        self._error = False
        self._error_reason = None
        self._consecutive_unexpected_failures = 0
        self._sm.transition(State.IDLE)
        self._reset_timer()

    # -- Publishing --------------------------------------------------------

    def _publish_result(self, outcome: RollOutcome, attempts: list, final_status: str) -> None:
        """Build and publish the enriched roll result payload."""
        timestamp = datetime.now(timezone.utc).isoformat()

        if outcome.result is not None:
            self._sm.last_roll_id = outcome.roll_id
            roll_result = {
                "roll_id": outcome.roll_id,
                "die1": outcome.result["die1"],
                "die2": outcome.result["die2"],
                "total": outcome.result["die1"] + outcome.result["die2"],
                "roll_status": final_status,
                "roll_status_reason": None,
                "attempts": [{"capture": a.capture, "detect": a.detect} for a in attempts],
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
                "timestamp": timestamp,
                "device_id": self._config.mqtt.client_id,
            }

        self._mqtt.publish_result(roll_result)
        if outcome.image_bytes:
            self._mqtt.publish_image(outcome.image_bytes)

    def _publish_image(self, outcome: RollOutcome) -> None:
        """Publish just the image (e.g. mechanism failure evidence)."""
        if outcome.image_bytes:
            self._mqtt.publish_image(outcome.image_bytes)

    # -- Heartbeat ---------------------------------------------------------

    def _maybe_heartbeat(self) -> None:
        now = time.monotonic()
        if (self._sm.state == State.IDLE
                and now - self._last_heartbeat >= _HEARTBEAT_INTERVAL_SEC):
            self._mqtt.publish_status(self._sm._build_status())
            self._last_heartbeat = now
