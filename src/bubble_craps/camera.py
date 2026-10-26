import logging

import numpy as np

from bubble_craps.config import CameraConfig

logger = logging.getLogger(__name__)


class PiCamera:
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

            # Apply exposure and white balance settings
            if self.config.exposure_mode == "fixed":
                self._camera.set_controls({
                    "AeEnable": False,
                    "ExposureTime": self.config.exposure_time_us,
                })
                logger.info("PiCamera: fixed exposure at %d us", self.config.exposure_time_us)
            else:
                logger.info("PiCamera: auto exposure")

            if self.config.white_balance == "fixed":
                self._camera.set_controls({
                    "AwbEnable": False,
                })
                logger.info("PiCamera: fixed white balance")
            else:
                logger.info("PiCamera: auto white balance")

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
