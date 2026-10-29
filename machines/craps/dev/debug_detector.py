#!/usr/bin/env python3
"""Debug and tune the dice detector pipeline on a captured image.

Calls the real DiceDetector methods so the debug output exactly matches
production behavior. Saves intermediate images at each pipeline step.

Run on your laptop after copying an image from the Pi.

Usage:
  python3 dev/debug_detector.py capture.jpg
  python3 dev/debug_detector.py capture.jpg --out-dir my_debug
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bubble_craps.config import load_config
from bubble_craps.detector import DiceDetector


def save(out_dir: Path, name: str, img: np.ndarray) -> None:
    path = out_dir / name
    cv2.imwrite(str(path), img)
    print(f"  saved: {path}")


def main():
    config = load_config()

    parser = argparse.ArgumentParser(description="Debug dice detector pipeline")
    parser.add_argument("image", help="Path to input image")
    parser.add_argument("--out-dir", default="debug_out", help="Directory for output images")
    args = parser.parse_args()

    image_path = Path(args.image)
    if not image_path.exists():
        print(f"ERROR: image not found: {image_path}")
        sys.exit(1)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    det = DiceDetector(config.detection)

    print(f"\nLoading: {image_path}")
    image = cv2.imread(str(image_path))
    if image is None:
        print("ERROR: cv2.imread returned None")
        sys.exit(1)
    print(f"  image shape: {image.shape}")

    # ── 01: Apply ROI mask ───────────────────────────────────────────────────
    masked = det._apply_roi(image)
    print(f"\n[01] ROI mask (radius={config.detection.roi_radius})")
    save(out_dir, "01_roi.jpg", masked)

    # ── 02: Grayscale + blur + sharpen ───────────────────────────────────────
    sharpened = det._preprocess(masked)
    print("\n[02] Grayscale -> blur -> sharpen")
    save(out_dir, "02_sharp.jpg", sharpened)

    # ── 03: Threshold + cleanup ──────────────────────────────────────────────
    thresh = det._threshold(sharpened)
    print(f"\n[03] Threshold ({config.detection.threshold}) + morphological open ({config.detection.morph_kernel})")
    save(out_dir, "03_thresh.jpg", thresh)

    # ── 04: Find squares ─────────────────────────────────────────────────────
    squares = det._find_squares(thresh)
    print(f"\n[04] Finding squares (die_size={config.detection.die_size_px}px, "
          f"area={det.min_die_area}-{det.max_die_area})")

    # Visualize: re-run contour finding to show rejected vs accepted
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    print(f"  total contours: {len(contours)}")
    contour_vis = masked.copy()

    for contour in contours:
        area = cv2.contourArea(contour)
        if area < det.min_die_area or area > det.max_die_area:
            cv2.drawContours(contour_vis, [contour], -1, (0, 0, 255), 1)  # red = wrong size
            continue

        rect = cv2.minAreaRect(contour)
        w, h = rect[1]
        if w == 0 or h == 0:
            continue
        aspect = max(w, h) / min(w, h)
        if aspect > config.detection.max_aspect_ratio:
            cv2.drawContours(contour_vis, [contour], -1, (0, 165, 255), 1)  # orange = not square
            continue

        extent = area / (w * h)
        if extent < config.detection.min_extent:
            cv2.drawContours(contour_vis, [contour], -1, (0, 255, 255), 1)  # yellow = not filled
            print(f"    rejected: area={area:.0f} extent={extent:.2f}")
            continue

        box = cv2.boxPoints(rect)
        box = np.intp(box)
        cv2.drawContours(contour_vis, [box], -1, (0, 255, 0), 2)  # green = accepted
        detected_size = int(area ** 0.5)
        print(f"    accepted: area={area:.0f} die_size={detected_size}px aspect={aspect:.2f} extent={extent:.2f}")

    save(out_dir, "04_squares.jpg", contour_vis)
    print(f"  accepted squares: {len(squares)}")

    # ── 05: Perspective correct + count pips ─────────────────────────────────
    if len(squares) == 2:
        # Sort by x-position to match production behavior
        squares.sort(key=lambda c: det._get_center(c)[0])

        print("\n[05] Counting pips")
        result_vis = masked.copy()
        pip_counts = []

        for i, contour in enumerate(squares):
            center = det._get_center(contour)
            warped_thresh = det._perspective_correct(thresh, contour)
            save(out_dir, f"05_thresh_{i}.jpg", warped_thresh)

            pips = det._count_pips(warped_thresh)

            # Visualize pip keypoints
            params = cv2.SimpleBlobDetector_Params()
            params.filterByArea = True
            params.minArea = det.min_pip_area
            params.maxArea = det.max_pip_area
            params.filterByCircularity = True
            params.minCircularity = config.detection.min_circularity
            params.filterByConvexity = True
            params.minConvexity = config.detection.min_convexity
            params.filterByInertia = False
            blob_det = cv2.SimpleBlobDetector_create(params)
            keypoints = blob_det.detect(warped_thresh)

            pip_vis = cv2.cvtColor(warped_thresh, cv2.COLOR_GRAY2BGR)
            for kp in keypoints:
                x, y = int(kp.pt[0]), int(kp.pt[1])
                r = max(3, int(kp.size / 2))
                kp_area = 3.14159 * (kp.size / 2) ** 2
                cv2.circle(pip_vis, (x, y), r, (0, 255, 0), 2)
                print(f"    pip at ({x},{y}) size={kp.size:.1f} area={kp_area:.0f}")
            cv2.putText(pip_vis, f"pips: {len(keypoints)}", (5, 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
            save(out_dir, f"05_pips_{i}.jpg", pip_vis)

            valid = pips if pips is not None and 1 <= pips <= 6 else None
            pip_counts.append(valid)
            print(f"  die {i} at {center}: {pips} pips {'OK' if valid else 'FAIL'}")

            # Draw on result
            color = (0, 255, 0) if valid else (0, 0, 255)
            cv2.drawContours(result_vis, [contour], -1, color, 3)
            cx_d, cy_d = center
            label = f"die{i+1}: {pips if valid else '?'}"
            cv2.putText(result_vis, label, (cx_d - 30, cy_d - 15),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)

        save(out_dir, "06_result.jpg", result_vis)

        if all(c is not None for c in pip_counts):
            total = pip_counts[0] + pip_counts[1]
            print(f"\n  PASS: die1={pip_counts[0]} die2={pip_counts[1]} total={total}")
        else:
            print("\n  FAIL: pip detection incomplete")
    else:
        print(f"\n  Square detection found {len(squares)} (need 2)")

    print(f"\nAll debug images saved to: {out_dir}/")


if __name__ == "__main__":
    main()
