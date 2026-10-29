import logging
import time

import numpy as np

from bubble_craps.config import CameraConfig

logger = logging.getLogger(__name__)

_CONTROLS_SETTLE_SEC = 2.0


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

            # Apply exposure and white balance settings in one call
            controls = {}
            if self.config.exposure_mode == "fixed":
                controls["AeEnable"] = False
                controls["ExposureTime"] = self.config.exposure_time_us
                controls["AnalogueGain"] = 1.0
                logger.info("PiCamera: fixed exposure at %d us, gain 1.0", self.config.exposure_time_us)
            else:
                logger.info("PiCamera: auto exposure")

            if self.config.white_balance == "fixed":
                controls["AwbEnable"] = False
                controls["ColourGains"] = (
                    self.config.colour_gain_red,
                    self.config.colour_gain_blue,
                )
                logger.info("PiCamera: fixed white balance (red=%.2f, blue=%.2f)",
                            self.config.colour_gain_red, self.config.colour_gain_blue)
            else:
                logger.info("PiCamera: auto white balance")

            if controls:
                self._camera.set_controls(controls)
                time.sleep(_CONTROLS_SETTLE_SEC)

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
