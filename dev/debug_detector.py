#!/usr/bin/env python3
"""Debug and tune the dice detector pipeline on a captured image.

Run on your laptop after copying an image from the Pi.

Usage:
  python3 dev/debug_detector.py capture.jpg --roi-radius 650
  python3 dev/debug_detector.py capture.jpg --roi-radius 650 --roi-cx 960 --roi-cy 540
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np


def parse_args():
    p = argparse.ArgumentParser(description="Debug dice detector pipeline")
    p.add_argument("image", help="Path to input image")
    p.add_argument("--out-dir", default="debug_out", help="Directory for output images")

    # Die and pip sizes
    p.add_argument("--die-size", type=int, default=305, help="Die face width in pixels")
    p.add_argument("--pip-diameter", type=int, default=25, help="Pip diameter in pixels")

    # ROI mask
    p.add_argument("--roi-cx", type=int, default=0, help="ROI circle center X (0 = image center)")
    p.add_argument("--roi-cy", type=int, default=0, help="ROI circle center Y (0 = image center)")
    p.add_argument("--roi-radius", type=int, default=0, help="ROI circle radius (0 = disabled)")

    return p.parse_args()


def save(out_dir: Path, name: str, img: np.ndarray) -> None:
    path = out_dir / name
    cv2.imwrite(str(path), img)
    print(f"  saved: {path}")


def main():
    args = parse_args()

    image_path = Path(args.image)
    if not image_path.exists():
        print(f"ERROR: image not found: {image_path}")
        sys.exit(1)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nLoading: {image_path}")
    image = cv2.imread(str(image_path))
    if image is None:
        print("ERROR: cv2.imread returned None")
        sys.exit(1)
    print(f"  image shape: {image.shape}")

    # ── 01: Apply ROI mask ───────────────────────────────────────────────────
    h, w = image.shape[:2]
    cx = args.roi_cx if args.roi_cx > 0 else w // 2
    cy = args.roi_cy if args.roi_cy > 0 else h // 2

    if args.roi_radius > 0:
        roi_mask = np.zeros((h, w), dtype=np.uint8)
        cv2.circle(roi_mask, (cx, cy), args.roi_radius, 255, -1)
        masked = cv2.bitwise_and(image, image, mask=roi_mask)
        print(f"\n[01] ROI mask: center=({cx}, {cy}) radius={args.roi_radius}")
    else:
        masked = image.copy()
        print("\n[01] ROI mask: disabled (use --roi-radius)")

    save(out_dir, "01_roi.jpg", masked)

    # ── 02: Grayscale + blur + sharpen ───────────────────────────────────────
    print("\n[02] Grayscale -> blur -> sharpen")
    gray = cv2.cvtColor(masked, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    sharpened = cv2.addWeighted(gray, 1.5, blurred, -0.5, 0)
    save(out_dir, "02_sharp.jpg", sharpened)

    # ── 03: Threshold + cleanup ────────────────────────────────────────────
    print("\n[03] Threshold (230) + morphological open")
    _, thresh = cv2.threshold(sharpened, 230, 255, cv2.THRESH_BINARY)
    # Open to remove small speckle noise, keep big squares
    open_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, open_kernel)
    save(out_dir, "03_thresh.jpg", thresh)

    # ── 04: Find squares ─────────────────────────────────────────────────────
    # Derive area bounds from die size
    die = args.die_size
    min_die_area = int((die * 0.85) ** 2)
    max_die_area = int((die * 1.1) ** 2)

    print(f"\n[04] Finding squares (die_size={die}px, area={min_die_area}-{max_die_area})")
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    print(f"  total contours: {len(contours)}")
    squares = []
    contour_vis = masked.copy()

    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_die_area or area > max_die_area:
            cv2.drawContours(contour_vis, [contour], -1, (0, 0, 255), 1)  # red = wrong size
            continue

        rect = cv2.minAreaRect(contour)
        w, h = rect[1]
        if w == 0 or h == 0:
            continue
        aspect = max(w, h) / min(w, h)
        if aspect > 1.4:
            cv2.drawContours(contour_vis, [contour], -1, (0, 165, 255), 1)  # orange = not square
            continue

        extent = area / (w * h)
        if extent < 0.65:
            cv2.drawContours(contour_vis, [contour], -1, (0, 255, 255), 1)  # yellow = not filled
            print(f"    rejected: area={area:.0f} extent={extent:.2f}")
            continue

        box = cv2.boxPoints(rect)
        box = np.intp(box)
        cv2.drawContours(contour_vis, [box], -1, (0, 255, 0), 2)  # green = accepted
        squares.append(box)
        detected_size = int(area ** 0.5)
        print(f"    accepted: area={area:.0f} die_size={detected_size}px aspect={aspect:.2f} extent={extent:.2f}")

    save(out_dir, "04_squares.jpg", contour_vis)
    print(f"  accepted squares: {len(squares)}")

    # ── 05: Count pips in each square ────────────────────────────────────────
    if len(squares) == 2:
        print("\n[05] Counting pips")
        result_vis = masked.copy()
        pip_counts = []

        for i, box in enumerate(squares):
            # Perspective correct to flat square
            pts = box.astype(np.float32)
            s = pts.sum(axis=1)
            rect_ordered = np.zeros((4, 2), dtype=np.float32)
            rect_ordered[0] = pts[np.argmin(s)]
            rect_ordered[2] = pts[np.argmax(s)]
            d = np.diff(pts, axis=1)
            rect_ordered[1] = pts[np.argmin(d)]
            rect_ordered[3] = pts[np.argmax(d)]

            size = 200
            dst = np.array([[0, 0], [size, 0], [size, size], [0, size]], dtype=np.float32)
            matrix = cv2.getPerspectiveTransform(rect_ordered, dst)
            warped_thresh = cv2.warpPerspective(thresh, matrix, (size, size),
                                                borderMode=cv2.BORDER_CONSTANT, borderValue=255)
            save(out_dir, f"05_thresh_{i}.jpg", warped_thresh)

            # Derive pip area bounds from pip diameter
            pip_r = args.pip_diameter / 2
            pip_area = 3.14159 * pip_r ** 2
            min_pip_area = int(pip_area * 0.6)
            max_pip_area = int(pip_area * 1.6)

            params = cv2.SimpleBlobDetector_Params()
            params.filterByArea = True
            params.minArea = min_pip_area
            params.maxArea = max_pip_area
            params.filterByCircularity = True
            params.minCircularity = 0.2
            params.filterByConvexity = True
            params.minConvexity = 0.2
            params.filterByInertia = False

            detector = cv2.SimpleBlobDetector_create(params)
            keypoints = detector.detect(warped_thresh)
            count = len(keypoints)

            pip_vis = cv2.cvtColor(warped_thresh, cv2.COLOR_GRAY2BGR)
            for kp in keypoints:
                x, y = int(kp.pt[0]), int(kp.pt[1])
                r = max(3, int(kp.size / 2))
                area = 3.14159 * (kp.size / 2) ** 2
                cv2.circle(pip_vis, (x, y), r, (0, 255, 0), 2)
                print(f"    pip at ({x},{y}) size={kp.size:.1f} area={area:.0f}")
            cv2.putText(pip_vis, f"pips: {count}", (5, 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
            save(out_dir, f"05_pips_{i}.jpg", pip_vis)

            valid = count if 1 <= count <= 6 else None
            pip_counts.append(valid)
            print(f"  die {i}: {count} pips {'OK' if valid else 'FAIL'}")

            # Draw on result
            color = (0, 255, 0) if valid else (0, 0, 255)
            cv2.drawContours(result_vis, [box], -1, color, 3)
            m = cv2.moments(box)
            if m["m00"] != 0:
                cx_d = int(m["m10"] / m["m00"])
                cy_d = int(m["m01"] / m["m00"])
                label = f"die{i+1}: {count if valid else '?'}"
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
        print("  TODO: fallback to backup path")

    print(f"\nAll debug images saved to: {out_dir}/")


if __name__ == "__main__":
    main()
