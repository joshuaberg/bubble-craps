import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class RingLightController(ABC):
    """Abstract interface for ring light control."""

    @abstractmethod
    def set_pattern(self, pattern: str) -> None:
        """Set the ring light to a named pattern.
        Valid patterns: idle, rolling, capture, success, error, off
        """

    @abstractmethod
    def set_brightness(self, brightness: float) -> None:
        """Set brightness level (0.0 to 1.0)."""

    @abstractmethod
    def off(self) -> None:
        """Turn the ring light off."""


class MockRingLightController(RingLightController):
    """Mock ring light controller for development and testing."""

    def __init__(self):
        self._pattern = "off"
        self._brightness = 0.0

    def set_pattern(self, pattern: str) -> None:
        logger.info("MockRingLight: set_pattern(%s)", pattern)
        self._pattern = pattern

    def set_brightness(self, brightness: float) -> None:
        logger.info("MockRingLight: set_brightness(%.2f)", brightness)
        self._brightness = brightness

    def off(self) -> None:
        logger.info("MockRingLight: off")
        self._pattern = "off"
        self._brightness = 0.0
