"""Shared test fixtures — mock hardware objects for state machine and supervisor tests."""

import pytest
import numpy as np

from bubble_craps.config import (
    AppConfig, TriggerConfig, MotorConfig, CameraConfig,
    RingLightConfig, MqttConfig, DetectionConfig, OfflineQueueConfig,
)
from bubble_craps.motor import MotorStatus


class FakeMotor:
    """Mock motor that tracks calls and can be told to fail."""

    def __init__(self):
        self.calls = []
        self._position = 0.0
        self._target = None
        self._fail_spin = False
        self._fail_turn_on = False
        self._temp_c = 42

    def turn_on(self):
        self.calls.append("turn_on")
        if self._fail_turn_on:
            from bubble_craps.motor import MotorError
            raise MotorError("turn_on failed: fake")

    def turn_off(self):
        self.calls.append("turn_off")

    def spin(self, rpm=550.0):
        self.calls.append(("spin", rpm))
        if self._fail_spin:
            from bubble_craps.motor import MotorError
            raise MotorError("spin failed: fake")

    def stop(self):
        self.calls.append("stop")

    def go_to_angle(self, angle, speed_limit=100.0):
        self.calls.append(("go_to_angle", angle, speed_limit))
        self._target = angle

    def is_at_position(self):
        return True

    def get_position(self):
        return self._position

    def get_status(self):
        return MotorStatus(
            temp_c=self._temp_c,
            torque_milli_nm=0,
            speed_dps=0,
            angle_deg=self._position,
        )

    def shutdown(self):
        self.calls.append("shutdown")


class FakeCamera:
    """Mock camera that returns a test image or raises."""

    def __init__(self):
        self._fail = False
        self._image = np.zeros((100, 100, 3), dtype=np.uint8)

    def capture(self):
        if self._fail:
            raise RuntimeError("Camera capture failed: fake")
        return self._image

    def close(self):
        pass


class FakeDetector:
    """Mock detector that returns a configurable result."""

    def __init__(self):
        self._result = {"die1": 3, "die2": 5, "positions": [(30, 30), (70, 70)]}

    def detect(self, image):
        return self._result


class FakeRingLight:
    """Mock ring light that tracks pattern changes."""

    def __init__(self):
        self.pattern = "off"
        self.patterns = []

    def set_pattern(self, pattern):
        self.pattern = pattern
        self.patterns.append(pattern)

    def off(self):
        self.pattern = "off"


class FakeMqttClient:
    """Mock MQTT client that records published messages."""

    def __init__(self):
        self.statuses = []
        self.results = []
        self.images = []

    def publish_status(self, status):
        self.statuses.append(status)

    def publish_result(self, result):
        self.results.append(result)

    def publish_image(self, image_bytes, roll_id):
        self.images.append({"roll_id": roll_id, "size": len(image_bytes)})

    def connect(self):
        pass

    def disconnect(self):
        pass


@pytest.fixture
def fake_motor():
    return FakeMotor()


@pytest.fixture
def fake_camera():
    return FakeCamera()


@pytest.fixture
def fake_detector():
    return FakeDetector()


@pytest.fixture
def fake_ring_light():
    return FakeRingLight()


@pytest.fixture
def fake_mqtt():
    return FakeMqttClient()


@pytest.fixture
def test_config():
    """Minimal config with fast timings for tests."""
    return AppConfig(
        trigger=TriggerConfig(mode="remote", timer_interval_sec=60),
        motor=MotorConfig(
            roll_rpm_min=300, roll_rpm_max=300,
            roll_duration_min_sec=0.01, roll_duration_max_sec=0.01,
            park_position=0.0, park_speed_limit=100,
            park_timeout_sec=0.1, park_poll_interval_sec=0.01,
            settling_time_sec=0.01,
        ),
        camera=CameraConfig(),
        ring_light=RingLightConfig(),
        mqtt=MqttConfig(offline_queue=OfflineQueueConfig()),
        detection=DetectionConfig(),
    )


@pytest.fixture
def state_machine(test_config, fake_motor, fake_ring_light, fake_camera, fake_detector):
    from bubble_craps.state_machine import StateMachine
    transitions = []
    sm = StateMachine(
        test_config, fake_motor, fake_ring_light, fake_camera, fake_detector,
        on_transition=lambda s: transitions.append(s),
    )
    sm._transitions = transitions
    return sm
