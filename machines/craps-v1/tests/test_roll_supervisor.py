"""Tests for the roll supervisor — retry policy, error escalation, and publishing."""

import pytest
import time
from unittest.mock import MagicMock, patch

from bubble_craps.config import AppConfig, TriggerConfig, MqttConfig, OfflineQueueConfig
from bubble_craps.motor import MotorError
from bubble_craps.roll_supervisor import RollSupervisor
from bubble_craps.state_machine import CaptureAttempt, RollOutcome, State


def _ok_outcome(roll_id="test-roll"):
    return RollOutcome(
        status="ok", roll_id=roll_id,
        result={"die1": 3, "die2": 5, "positions": [(30, 30), (70, 70)]},
        image_bytes=b"fake-jpeg",
        attempt=CaptureAttempt(capture="ok", detect="ok"),
        motor_temp_c=42,
    )


def _detection_failed_outcome(roll_id="test-roll"):
    return RollOutcome(
        status="detection_failed", roll_id=roll_id,
        result=None, image_bytes=b"fake-jpeg",
        attempt=CaptureAttempt(capture="ok", detect="null"),
        motor_temp_c=42,
    )


def _camera_failure_outcome(roll_id="test-roll"):
    return RollOutcome(
        status="camera_failure", roll_id=roll_id,
        result=None, image_bytes=None,
        attempt=CaptureAttempt(capture="failed", detect="skipped"),
        motor_temp_c=42,
    )


def _mechanism_failure_outcome(roll_id="test-roll"):
    return RollOutcome(
        status="mechanism_failure", roll_id=roll_id,
        result={"die1": 3, "die2": 5, "positions": [(30, 30), (70, 70)]},
        image_bytes=b"fake-jpeg",
        attempt=CaptureAttempt(capture="ok", detect="ok"),
        motor_temp_c=42,
    )


@pytest.fixture
def supervisor(test_config, fake_mqtt):
    """Create a supervisor with a mock state machine, without starting the thread."""
    sm = MagicMock()
    sm.state = State.IDLE
    sup = RollSupervisor(test_config, sm, fake_mqtt)
    # Don't start the thread — we call _execute_roll directly
    return sup


class TestSuccessfulRoll:

    def test_publishes_result_and_image(self, supervisor, fake_mqtt):
        supervisor._sm.run_roll.return_value = _ok_outcome()

        supervisor._execute_roll()

        assert len(fake_mqtt.results) == 1
        assert fake_mqtt.results[0]["roll_status"] == "ok"
        assert fake_mqtt.results[0]["die1"] == 3
        assert fake_mqtt.results[0]["die2"] == 5
        assert fake_mqtt.results[0]["motor_temp_c"] == 42
        assert len(fake_mqtt.images) == 1

    def test_resets_failure_counters(self, supervisor):
        supervisor._consecutive_unexpected_failures = 2
        supervisor._consecutive_detection_failures = 3
        supervisor._sm.run_roll.return_value = _ok_outcome()

        supervisor._execute_roll()

        assert supervisor._consecutive_unexpected_failures == 0
        assert supervisor._consecutive_detection_failures == 0

    def test_updates_last_roll_id(self, supervisor):
        supervisor._sm.run_roll.return_value = _ok_outcome("roll-abc")

        supervisor._execute_roll()

        assert supervisor._last_roll_id == "roll-abc"


class TestDetectionFailure:

    def test_retries_capture_then_succeeds(self, supervisor, fake_mqtt):
        supervisor._sm.run_roll.return_value = _detection_failed_outcome()
        supervisor._sm.retry_capture.return_value = _ok_outcome()

        supervisor._execute_roll()

        supervisor._sm.retry_capture.assert_called_once()
        assert len(fake_mqtt.results) == 1
        assert fake_mqtt.results[0]["roll_status"] == "ok"
        # Should have 2 attempts recorded
        assert len(fake_mqtt.results[0]["attempts"]) == 2

    def test_retries_capture_still_fails_publishes_null(self, supervisor, fake_mqtt):
        supervisor._sm.run_roll.return_value = _detection_failed_outcome()
        supervisor._sm.retry_capture.return_value = _detection_failed_outcome()

        supervisor._execute_roll()

        assert len(fake_mqtt.results) == 1
        assert fake_mqtt.results[0]["roll_status"] == "detection_failed"
        assert fake_mqtt.results[0]["die1"] is None

    def test_no_auto_reroll_in_remote_mode(self, supervisor):
        supervisor._config.trigger.mode = "remote"
        supervisor._sm.run_roll.return_value = _detection_failed_outcome()
        supervisor._sm.retry_capture.return_value = _detection_failed_outcome()

        supervisor._execute_roll()

        # Queue should be empty — no auto-reroll in remote mode
        assert supervisor._queue.empty()

    def test_auto_reroll_in_timer_mode(self, supervisor):
        supervisor._config.trigger.mode = "timer"
        supervisor._sm.run_roll.return_value = _detection_failed_outcome()
        supervisor._sm.retry_capture.return_value = _detection_failed_outcome()

        supervisor._execute_roll()

        # Should have enqueued a reroll
        assert not supervisor._queue.empty()
        cmd, _ = supervisor._queue.get_nowait()
        assert cmd == "roll"

    def test_error_after_consecutive_detection_failures(self, supervisor):
        supervisor._sm.run_roll.return_value = _detection_failed_outcome()
        supervisor._sm.retry_capture.return_value = _detection_failed_outcome()
        supervisor._config.trigger.mode = "remote"  # no reroll to simplify

        for _ in range(5):
            supervisor._execute_roll()

        assert supervisor._error is True
        assert supervisor._error_reason == "repeated_detection_failure"

    def test_successful_roll_resets_detection_counter(self, supervisor):
        supervisor._sm.run_roll.return_value = _detection_failed_outcome()
        supervisor._sm.retry_capture.return_value = _detection_failed_outcome()
        supervisor._config.trigger.mode = "remote"

        # 4 failures
        for _ in range(4):
            supervisor._execute_roll()
        assert supervisor._consecutive_detection_failures == 4

        # 1 success resets
        supervisor._sm.run_roll.return_value = _ok_outcome()
        supervisor._execute_roll()
        assert supervisor._consecutive_detection_failures == 0


class TestCameraFailure:

    def test_retries_capture_then_succeeds(self, supervisor, fake_mqtt):
        supervisor._sm.run_roll.return_value = _camera_failure_outcome()
        supervisor._sm.retry_capture.return_value = _ok_outcome()

        supervisor._execute_roll()

        assert len(fake_mqtt.results) == 1
        assert fake_mqtt.results[0]["roll_status"] == "ok"

    def test_both_fail_enters_error(self, supervisor, fake_mqtt):
        supervisor._sm.run_roll.return_value = _camera_failure_outcome()
        supervisor._sm.retry_capture.return_value = _camera_failure_outcome()

        supervisor._execute_roll()

        assert supervisor._error is True
        assert supervisor._error_reason == "camera_failure"
        assert len(fake_mqtt.results) == 1
        assert fake_mqtt.results[0]["roll_status"] == "camera_failure"
        assert fake_mqtt.results[0]["die1"] is None

    def test_retry_returns_detection_failed(self, supervisor, fake_mqtt):
        """Camera works on retry but detection fails — publish null, not ERROR."""
        supervisor._sm.run_roll.return_value = _camera_failure_outcome()
        supervisor._sm.retry_capture.return_value = _detection_failed_outcome()

        supervisor._execute_roll()

        assert supervisor._error is False
        assert len(fake_mqtt.results) == 1
        assert fake_mqtt.results[0]["roll_status"] == "detection_failed"


class TestMechanismFailure:

    def test_enters_error_immediately(self, supervisor):
        supervisor._sm.run_roll.return_value = _mechanism_failure_outcome()

        supervisor._execute_roll()

        assert supervisor._error is True
        assert supervisor._error_reason == "mechanism_failure"

    def test_publishes_result_and_image(self, supervisor, fake_mqtt):
        supervisor._sm.run_roll.return_value = _mechanism_failure_outcome()

        supervisor._execute_roll()

        assert len(fake_mqtt.images) == 1
        assert len(fake_mqtt.results) == 1
        assert fake_mqtt.results[0]["roll_status"] == "mechanism_failure"
        assert fake_mqtt.results[0]["die1"] == 3
        assert fake_mqtt.results[0]["die2"] == 5


class TestUnexpectedFailure:

    def test_returns_to_idle_on_first_failure(self, supervisor):
        supervisor._sm.run_roll.side_effect = RuntimeError("boom")

        supervisor._execute_roll()

        assert supervisor._error is False
        assert supervisor._consecutive_unexpected_failures == 1
        supervisor._sm.transition.assert_called_with(State.IDLE)

    def test_error_after_three_consecutive(self, supervisor):
        supervisor._sm.run_roll.side_effect = RuntimeError("boom")

        for _ in range(3):
            supervisor._execute_roll()

        assert supervisor._error is True
        assert supervisor._error_reason == "repeated_unexpected_failure"

    def test_success_resets_counter(self, supervisor):
        supervisor._sm.run_roll.side_effect = RuntimeError("boom")
        supervisor._execute_roll()
        supervisor._execute_roll()
        assert supervisor._consecutive_unexpected_failures == 2

        # Success resets
        supervisor._sm.run_roll.side_effect = None
        supervisor._sm.run_roll.return_value = _ok_outcome()
        supervisor._execute_roll()
        assert supervisor._consecutive_unexpected_failures == 0


class TestErrorState:

    def test_rejects_rolls_in_error(self, supervisor, fake_mqtt):
        supervisor._error = True

        supervisor._execute_roll()

        supervisor._sm.run_roll.assert_not_called()
        assert len(fake_mqtt.results) == 0

    def test_clear_error(self, supervisor):
        supervisor._error = True
        supervisor._error_reason = "camera_failure"
        supervisor._consecutive_unexpected_failures = 3

        supervisor._clear_error()

        assert supervisor._error is False
        assert supervisor._error_reason is None
        assert supervisor._consecutive_unexpected_failures == 0
        supervisor._sm.transition.assert_called_with(State.IDLE)


class TestTimerReset:

    def test_timer_reset_after_successful_roll(self, supervisor):
        supervisor._config.trigger.mode = "timer"
        supervisor._config.trigger.timer_interval_sec = 60
        supervisor._sm.run_roll.return_value = _ok_outcome()

        supervisor._execute_roll()

        assert supervisor._next_timer_roll is not None
        # Timer should be set ~60s in the future, not in the past
        assert supervisor._next_timer_roll > time.monotonic() + 30

    def test_timer_reset_after_failed_roll(self, supervisor):
        supervisor._config.trigger.mode = "timer"
        supervisor._config.trigger.timer_interval_sec = 60
        supervisor._sm.run_roll.return_value = _detection_failed_outcome()
        supervisor._sm.retry_capture.return_value = _detection_failed_outcome()

        supervisor._execute_roll()

        assert supervisor._next_timer_roll is not None
        assert supervisor._next_timer_roll > time.monotonic() + 30

    def test_timer_reset_after_exception(self, supervisor):
        supervisor._config.trigger.mode = "timer"
        supervisor._config.trigger.timer_interval_sec = 60
        supervisor._sm.run_roll.side_effect = RuntimeError("boom")

        supervisor._execute_roll()

        assert supervisor._next_timer_roll is not None
        assert supervisor._next_timer_roll > time.monotonic() + 30


class TestSetMode:

    def test_valid_mode_change(self, supervisor):
        supervisor._handle_set_mode({"mode": "timer"})
        assert supervisor._config.trigger.mode == "timer"

    def test_invalid_mode_rejected(self, supervisor):
        supervisor._handle_set_mode({"mode": "invalid"})
        assert supervisor._config.trigger.mode == "remote"  # unchanged from fixture

    def test_interval_below_minimum_rejected(self, supervisor):
        supervisor._config.trigger.timer_interval_sec = 120
        supervisor._handle_set_mode({"mode": "timer", "interval_sec": 5})
        assert supervisor._config.trigger.timer_interval_sec == 120  # unchanged

    def test_valid_interval_accepted(self, supervisor):
        supervisor._handle_set_mode({"mode": "timer", "interval_sec": 90})
        assert supervisor._config.trigger.timer_interval_sec == 90


