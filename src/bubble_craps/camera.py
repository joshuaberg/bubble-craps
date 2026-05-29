import logging
from abc import ABC, abstractmethod

import numpy as np

from bubble_craps.config import CameraConfig

logger = logging.getLogger(__name__)


class Camera(ABC):
    """Abstract interface for camera capture."""

    @abstractmethod
    def capture(self) -> np.ndarray:
        """Capture a single image and return it as a numpy array (BGR)."""

    @abstractmethod
    def close(self) -> None:
        """Release camera resources."""


class PiCamera(Camera):
    """Camera implementation using picamera2 on the Raspberry Pi."""

    def __init__(self, config: CameraConfig):
        self.config = config
        self._camera = None

    def _ensure_open(self) -> None:
        if self._camera is None:
            from picamera2 import Picamera2

            self._camera = Picamera2()
            self._camera.configure(
                self._camera.create_still_configuration(
                    main={"size": self.config.resolution}
                )
            )
            self._camera.start()
            logger.info("PiCamera: initialized at resolution %s", self.config.resolution)

    def capture(self) -> np.ndarray:
        self._ensure_open()
        image = self._camera.capture_array()
        logger.info("PiCamera: captured image shape=%s", image.shape)
        return image

    def close(self) -> None:
        if self._camera is not None:
            self._camera.stop()
            self._camera.close()
            self._camera = None
            logger.info("PiCamera: closed")


class MockCamera(Camera):
    """Mock camera for development and testing. Returns a blank image."""

    def __init__(self, config: CameraConfig):
        self.config = config

    def capture(self) -> np.ndarray:
        logger.info("MockCamera: capture")
        return np.zeros(
            (self.config.resolution[1], self.config.resolution[0], 3),
            dtype=np.uint8,
        )

    def close(self) -> None:
        logger.info("MockCamera: closed")
