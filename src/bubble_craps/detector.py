import logging

import cv2
import numpy as np

from bubble_craps.config import DetectionConfig

logger = logging.getLogger(__name__)


class DiceDetector:
    """Detects dice faces and pip counts from a captured image using OpenCV.

    Pipeline:
      1. Apply ROI mask (circular, to ignore area outside the bowl)
      2. Grayscale + blur + sharpen
      3. Global threshold (230) to isolate white dice
      4. Find square contours (dice faces) filtered by size/aspect/rectangularity
      5. Perspective-correct each die face
      6. Detect pips via blob detection
      7. Validate (exactly 2 dice, 1-6 pips each)
    """

    THRESHOLD = 230
    DIE_SIZE = 300  # approximate die face size in pixels
    WARP_SIZE = 200

    def __init__(self, config: DetectionConfig):
        self.config = config

    def detect(self, image: np.ndarray) -> dict | None:
        """Run the full detection pipeline on a captured image.

        Returns a dict with keys: die1, die2, positions
        or None if detection failed.
        """
        masked = self._apply_roi(image)
        sharpened = self._preprocess(masked)
        thresh = self._threshold(sharpened)
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

    def _apply_roi(self, image: np.ndarray) -> np.ndarray:
        """Apply circular ROI mask if configured."""
        r = self.config.roi_radius
        if r <= 0:
            return image

        h, w = image.shape[:2]
        cx = self.config.roi_center_x if self.config.roi_center_x > 0 else w // 2
        cy = self.config.roi_center_y if self.config.roi_center_y > 0 else h // 2

        mask = np.zeros((h, w), dtype=np.uint8)
        cv2.circle(mask, (cx, cy), r, 255, -1)
        return cv2.bitwise_and(image, image, mask=mask)

    def _preprocess(self, image: np.ndarray) -> np.ndarray:
        """Convert to grayscale, blur, and sharpen."""
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        sharpened = cv2.addWeighted(gray, 1.5, blurred, -0.5, 0)
        return sharpened

    def _threshold(self, gray: np.ndarray) -> np.ndarray:
        """Global threshold to isolate white dice on green felt."""
        _, thresh = cv2.threshold(gray, self.THRESHOLD, 255, cv2.THRESH_BINARY)
        return thresh

    def _find_squares(self, thresh: np.ndarray) -> list[np.ndarray]:
        """Find contours that are square-shaped (dice faces)."""
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        min_area = (self.DIE_SIZE * 0.5) ** 2
        max_area = (self.DIE_SIZE * 2.0) ** 2
        squares = []

        for contour in contours:
            area = cv2.contourArea(contour)
            if area < min_area or area > max_area:
                continue

            rect = cv2.minAreaRect(contour)
            w, h = rect[1]
            if w == 0 or h == 0:
                continue
            aspect = max(w, h) / min(w, h)
            if aspect > 1.4:
                continue

            extent = area / (w * h)
            if extent < 0.65:
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
        pts = self._order_points(pts)

        dst = np.array(
            [[0, 0], [self.WARP_SIZE, 0], [self.WARP_SIZE, self.WARP_SIZE], [0, self.WARP_SIZE]],
            dtype=np.float32,
        )
        matrix = cv2.getPerspectiveTransform(pts, dst)
        return cv2.warpPerspective(image, matrix, (self.WARP_SIZE, self.WARP_SIZE))

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
