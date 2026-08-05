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


class NeoPixelRingLightController(RingLightController):
    """NeoPixel ring light controller using rpi_ws281x."""

    def __init__(self, gpio_pin: int = 13, num_leds: int = 24, brightness: float = 0.3):
        import threading
        from rpi_ws281x import PixelStrip, Color

        self._Color = Color
        self._brightness = brightness
        self._pattern = "off"
        self._chase_thread = None
        self._chase_stop = threading.Event()

        # PWM channel: GPIO 13/19 = channel 1, GPIO 12/18 = channel 0
        channel = 1 if gpio_pin in (13, 19) else 0

        self._strip = PixelStrip(
            num_leds, gpio_pin,
            freq_hz=800000, dma=10, invert=False,
            brightness=int(brightness * 255),
            channel=channel,
        )
        self._strip.begin()
        logger.info("NeoPixel ring light initialized: GPIO %d, %d LEDs", gpio_pin, num_leds)

    def _stop_chase(self) -> None:
        """Stop the chase animation if running."""
        if self._chase_thread and self._chase_thread.is_alive():
            self._chase_stop.set()
            self._chase_thread.join()
            self._chase_stop.clear()

    def _run_chase(self) -> None:
        """White chase animation loop — runs in background thread."""
        import time
        n = self._strip.numPixels()
        trail = 4  # number of LEDs in the trail
        self._strip.setBrightness(int(self._brightness * 255))

        i = 0
        while not self._chase_stop.is_set():
            for j in range(n):
                self._strip.setPixelColor(j, self._Color(0, 0, 0))

            for t in range(trail):
                idx = (i - t) % n
                fade = 255 - int((t / trail) * 200)
                self._strip.setPixelColor(idx, self._Color(fade, fade, fade))

            self._strip.show()
            i = (i + 1) % n
            self._chase_stop.wait(0.05)

    def set_pattern(self, pattern: str) -> None:
        import threading

        self._stop_chase()
        self._pattern = pattern

        if pattern == "capture":
            self._set_all(255, 255, 255)
            self._strip.setBrightness(255)
            self._strip.show()
        elif pattern == "idle":
            self._set_all(255, 255, 255)
            self._strip.setBrightness(int(self._brightness * 255))
            self._strip.show()
        elif pattern == "rolling":
            self._chase_thread = threading.Thread(target=self._run_chase, daemon=True)
            self._chase_thread.start()
        elif pattern == "success":
            self._set_all(0, 255, 0)
            self._strip.setBrightness(int(self._brightness * 255))
            self._strip.show()
        elif pattern == "error":
            self._set_all(255, 0, 0)
            self._strip.setBrightness(int(self._brightness * 255))
            self._strip.show()
        elif pattern == "off":
            self.off()
        logger.info("NeoPixel: pattern=%s", pattern)

    def set_brightness(self, brightness: float) -> None:
        self._brightness = brightness
        self._strip.setBrightness(int(brightness * 255))
        self._strip.show()
        logger.info("NeoPixel: brightness=%.2f", brightness)

    def off(self) -> None:
        self._stop_chase()
        self._set_all(0, 0, 0)
        self._strip.show()
        self._pattern = "off"
        logger.info("NeoPixel: off")

    def _set_all(self, r: int, g: int, b: int) -> None:
        color = self._Color(r, g, b)
        for i in range(self._strip.numPixels()):
            self._strip.setPixelColor(i, color)


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
