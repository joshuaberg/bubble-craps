import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class MotorController(ABC):
    """Abstract interface for motor control."""

    @abstractmethod
    def start(self, speed: float = 1.0) -> None:
        """Start the motor at the given speed (0.0 to 1.0)."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the motor immediately."""

    @abstractmethod
    def go_to_position(self, position: float) -> None:
        """Command the motor to move to a specific position.
        Non-blocking — call is_at_position() to poll for completion."""

    @abstractmethod
    def is_at_position(self) -> bool:
        """Poll whether the motor has reached the target position."""

    @abstractmethod
    def is_running(self) -> bool:
        """Check if the motor is currently running."""


class MockMotorController(MotorController):
    """Mock motor controller for development and testing."""

    def __init__(self):
        self._running = False
        self._at_position = True

    def start(self, speed: float = 1.0) -> None:
        logger.info("MockMotor: start (speed=%.2f)", speed)
        self._running = True
        self._at_position = False

    def stop(self) -> None:
        logger.info("MockMotor: stop")
        self._running = False

    def go_to_position(self, position: float) -> None:
        logger.info("MockMotor: go_to_position (position=%.2f)", position)
        self._running = False
        self._at_position = True

    def is_at_position(self) -> bool:
        return self._at_position

    def is_running(self) -> bool:
        return self._running
