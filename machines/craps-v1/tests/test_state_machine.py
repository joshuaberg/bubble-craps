"""Tests for the state machine — hardware orchestration and RollOutcome reporting."""

import pytest

from bubble_craps.motor import MotorError
from bubble_craps.state_machine import CaptureAttempt, RollOutcome, State


class TestRunRoll:
    """Tests for StateMachine.run_roll()."""

    def test_successful_roll(self, state_machine, fake_motor):
        outcome = state_machine.run_roll("roll-001")

        assert outcome.status == "ok"
        assert outcome.roll_id == "roll-001"
        assert outcome.result["die1"] == 3
        assert outcome.result["die2"] == 5
        assert outcome.image_bytes is not None
        assert outcome.attempt.capture == "ok"
        assert outcome.attempt.detect == "ok"
        assert outcome.motor_temp_c == 42

    def test_transitions_through_all_phases(self, state_machine):
        state_machine.run_roll("roll-001")

        states = [s.value for s in state_machine._transitions]
        assert states == ["TRIGGERED", "ROLLING", "SETTLING", "CAPTURE", "ANALYZE"]

    def test_motor_on_off_per_roll(self, state_machine, fake_motor):
        state_machine.run_roll("roll-001")

        assert "turn_on" in fake_motor.calls
        assert "stop" in fake_motor.calls
        assert "turn_off" in fake_motor.calls
        # turn_off should come after stop
        stop_idx = fake_motor.calls.index("stop")
        off_idx = fake_motor.calls.index("turn_off")
        assert off_idx > stop_idx

    def test_motor_stopped_on_exception(self, state_machine, fake_motor):
        fake_motor._fail_spin = True

        with pytest.raises(MotorError):
            state_machine.run_roll("roll-001")

        assert "stop" in fake_motor.calls
        assert "turn_off" in fake_motor.calls

    def test_motor_turn_on_failure(self, state_machine, fake_motor):
        fake_motor._fail_turn_on = True

        with pytest.raises(MotorError):
            state_machine.run_roll("roll-001")

        # Motor should still be stopped and turned off in finally
        assert "stop" in fake_motor.calls
        assert "turn_off" in fake_motor.calls

    def test_camera_failure(self, state_machine, fake_camera):
        fake_camera._fail = True

        outcome = state_machine.run_roll("roll-001")

        assert outcome.status == "camera_failure"
        assert outcome.result is None
        assert outcome.image_bytes is None
        assert outcome.attempt.capture == "failed"
        assert outcome.attempt.detect == "skipped"

    def test_detection_failure(self, state_machine, fake_detector):
        fake_detector._result = None

        outcome = state_machine.run_roll("roll-001")

        assert outcome.status == "detection_failed"
        assert outcome.result is None
        assert outcome.image_bytes is not None  # image was captured
        assert outcome.attempt.capture == "ok"
        assert outcome.attempt.detect == "null"

    def test_mechanism_failure(self, state_machine, fake_detector):
        # First roll establishes the baseline
        fake_detector._result = {"die1": 3, "die2": 5, "positions": [(30, 30), (70, 70)]}
        outcome1 = state_machine.run_roll("roll-001")
        assert outcome1.status == "ok"

        # Second roll with identical result and positions
        outcome2 = state_machine.run_roll("roll-002")
        assert outcome2.status == "mechanism_failure"
        assert outcome2.result is not None  # result is included for evidence

    def test_mechanism_failure_not_triggered_on_different_pips(self, state_machine, fake_detector):
        fake_detector._result = {"die1": 3, "die2": 5, "positions": [(30, 30), (70, 70)]}
        state_machine.run_roll("roll-001")

        # Different pips, same positions — not a mechanism failure
        fake_detector._result = {"die1": 2, "die2": 6, "positions": [(30, 30), (70, 70)]}
        outcome = state_machine.run_roll("roll-002")
        assert outcome.status == "ok"

    def test_mechanism_failure_not_triggered_on_different_positions(self, state_machine, fake_detector):
        fake_detector._result = {"die1": 3, "die2": 5, "positions": [(30, 30), (70, 70)]}
        state_machine.run_roll("roll-001")

        # Same pips, different positions — legitimate repeat
        fake_detector._result = {"die1": 3, "die2": 5, "positions": [(100, 100), (200, 200)]}
        outcome = state_machine.run_roll("roll-002")
        assert outcome.status == "ok"

    def test_ring_light_patterns(self, state_machine, fake_ring_light):
        state_machine.run_roll("roll-001")

        # Should see rolling pattern for TRIGGERED/ROLLING/SETTLING, capture for CAPTURE/ANALYZE
        assert "rolling" in fake_ring_light.patterns
        assert "capture" in fake_ring_light.patterns


class TestRetryCapture:
    """Tests for StateMachine.retry_capture()."""

    def test_successful_retry(self, state_machine):
        # Do a full roll first so there's a previous_roll
        state_machine.run_roll("roll-001")

        # Change detector result so mechanism check doesn't trigger
        state_machine.detector._result = {"die1": 1, "die2": 4, "positions": [(40, 40), (60, 60)]}
        outcome = state_machine.retry_capture("roll-001")

        assert outcome.status == "ok"
        assert outcome.roll_id == "roll-001"

    def test_retry_does_not_touch_motor(self, state_machine, fake_motor):
        state_machine.run_roll("roll-001")
        fake_motor.calls.clear()

        state_machine.detector._result = {"die1": 1, "die2": 4, "positions": [(40, 40), (60, 60)]}
        state_machine.retry_capture("roll-001")

        # No motor calls during retry_capture
        assert fake_motor.calls == []

    def test_retry_camera_failure(self, state_machine, fake_camera):
        state_machine.run_roll("roll-001")
        fake_camera._fail = True

        outcome = state_machine.retry_capture("roll-001")
        assert outcome.status == "camera_failure"

    def test_retry_detection_failure(self, state_machine, fake_detector):
        state_machine.run_roll("roll-001")
        fake_detector._result = None

        outcome = state_machine.retry_capture("roll-001")
        assert outcome.status == "detection_failed"

    def test_retry_no_motor_temp(self, state_machine):
        """retry_capture should still have motor temp from the original roll."""
        state_machine.run_roll("roll-001")

        state_machine.detector._result = {"die1": 1, "die2": 4, "positions": [(40, 40), (60, 60)]}
        outcome = state_machine.retry_capture("roll-001")
        assert outcome.motor_temp_c == 42
