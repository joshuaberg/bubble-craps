import logging

import cv2
import numpy as np

from bubble_craps.config import DetectionConfig

logger = logging.getLogger(__name__)


class DiceDetector:
    """Detects dice faces and pip counts from a captured image using OpenCV.

    Pipeline:
      1. Preprocess (grayscale, blur)
      2. Threshold
      3. Find square contours (dice faces)
      4. Extract centers
      5. Perspective-correct each die face
      6. Detect pips within each corrected ROI
      7. Validate (exactly 2 dice, 1-6 pips each)
    """

    def __init__(self, config: DetectionConfig):
        self.config = config
        # TODO: load calibration from config.calibration_file if it exists

    def detect(self, image: np.ndarray) -> dict | None:
        """Run the full detection pipeline on a captured image.

        Returns a dict with keys: die1, die2, positions
        or None if detection failed.
        """
        gray = self._preprocess(image)
        thresh = self._threshold(gray)
        squares = self._find_squares(thresh)

        if len(squares) != 2:
            logger.warning("Expected 2 dice, found %d squares", len(squares))
            return None

        results = []
        centers = []

        for contour in squares:
            center = self._get_center(contour)
            centers.append(center)

            warped = self._perspective_correct(image, contour)
            pips = self._count_pips(warped)

            if pips is None or pips < 1 or pips > 6:
                logger.warning("Invalid pip count: %s", pips)
                return None

            results.append(pips)

        return {
            "die1": results[0],
            "die2": results[1],
            "positions": centers,
        }

    def _preprocess(self, image: np.ndarray) -> np.ndarray:
        """Convert to grayscale and apply Gaussian blur."""
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        return blurred

    def _threshold(self, gray: np.ndarray) -> np.ndarray:
        """Global threshold to isolate white dice on green felt."""
        _, thresh = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=2)
        return thresh

    def _find_squares(self, thresh: np.ndarray) -> list[np.ndarray]:
        """Find contours that are square-shaped (dice faces)."""
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        squares = []

        for contour in contours:
            area = cv2.contourArea(contour)
            if area < 1000 or area > 500_000:
                continue

            rect = cv2.minAreaRect(contour)
            w, h = rect[1]
            if w == 0 or h == 0:
                continue
            aspect = max(w, h) / min(w, h)
            if aspect > 1.3:
                continue

            # Check rectangularity — contour should fill most of bounding rect
            extent = area / (w * h)
            if extent < 0.7:
                continue

            box = cv2.boxPoints(rect)
            box = np.intp(box)
            squares.append(box)

        return squares

    def _get_center(self, contour: np.ndarray) -> tuple[int, int]:
        """Calculate the centroid of a contour."""
        moments = cv2.moments(contour)
        if moments["m00"] == 0:
            rect = cv2.minAreaRect(contour)
            return (int(rect[0][0]), int(rect[0][1]))
        cx = int(moments["m10"] / moments["m00"])
        cy = int(moments["m01"] / moments["m00"])
        return (cx, cy)

    def _perspective_correct(self, image: np.ndarray, contour: np.ndarray) -> np.ndarray:
        """Warp the die face to a top-down square view."""
        pts = contour.reshape(4, 2).astype(np.float32)
        # Order points: top-left, top-right, bottom-right, bottom-left
        pts = self._order_points(pts)

        size = 200  # output square size in pixels
        dst = np.array(
            [[0, 0], [size, 0], [size, size], [0, size]], dtype=np.float32
        )
        matrix = cv2.getPerspectiveTransform(pts, dst)
        warped = cv2.warpPerspective(image, matrix, (size, size))
        return warped

    def _order_points(self, pts: np.ndarray) -> np.ndarray:
        """Order 4 points as: top-left, top-right, bottom-right, bottom-left."""
        rect = np.zeros((4, 2), dtype=np.float32)
        s = pts.sum(axis=1)
        rect[0] = pts[np.argmin(s)]
        rect[2] = pts[np.argmax(s)]
        d = np.diff(pts, axis=1)
        rect[1] = pts[np.argmin(d)]
        rect[3] = pts[np.argmax(d)]
        return rect

    def _count_pips(self, warped: np.ndarray) -> int | None:
        """Count the number of pips on a perspective-corrected die face."""
        gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)
        # TODO: tune blob detector params during calibration
        params = cv2.SimpleBlobDetector_Params()
        params.filterByArea = True
        params.minArea = 50
        params.maxArea = 2000
        params.filterByCircularity = True
        params.minCircularity = 0.5
        params.filterByConvexity = True
        params.minConvexity = 0.5
        params.filterByInertia = False

        detector = cv2.SimpleBlobDetector_create(params)
        keypoints = detector.detect(gray)
        count = len(keypoints)

        if count < 1 or count > 6:
            return None

        return count
