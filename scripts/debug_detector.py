#!/usr/bin/env python3
"""Debug and tune the dice detector pipeline on a captured image.

Run this on your laptop after copying an image from the Pi with:
  scp pi:~/bubble-craps/capture.jpg .

Each pipeline stage saves an annotated output image so you can see exactly
what the detector is doing at each step.

Usage:
  python3 scripts/debug_detector.py capture.jpg
  python3 scripts/debug_detector.py capture.jpg --out-dir debug_out
  python3 scripts/debug_detector.py capture.jpg --min-area 500 --max-area 50000

Output images (saved to --out-dir, default: debug_out/):
  01_gray.jpg          grayscale input
  02_blur.jpg          after Gaussian blur
  03_thresh.jpg        adaptive threshold result
  04_contours.jpg      all 4-vertex contours found (red = rejected, green = accepted)
  05_squares.jpg       accepted square contours with centers marked
  06_warped_0.jpg      perspective-corrected die face (die 0)
  06_warped_1.jpg      perspective-corrected die face (die 1)
  07_pips_0.jpg        pip blobs detected on die 0
  07_pips_1.jpg        pip blobs detected on die 1
  08_result.jpg        final annotated result on original image
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np


# ── Tunable parameters (override with CLI flags) ──────────────────────────────

DEFAULT_BLUR_KERNEL = 5
DEFAULT_THRESH_BLOCK = 11
DEFAULT_THRESH_C = 2
DEFAULT_MIN_AREA = 1000
DEFAULT_MAX_AREA = 500_000
DEFAULT_ASPECT_TOLERANCE = 1.3
DEFAULT_PIP_MIN_AREA = 50
DEFAULT_PIP_MAX_AREA = 2000
DEFAULT_PIP_MIN_CIRCULARITY = 0.5
DEFAULT_PIP_MIN_CONVEXITY = 0.5
WARP_SIZE = 200


def parse_args():
    p = argparse.ArgumentParser(description="Debug dice detector pipeline")
    p.add_argument("image", help="Path to input image (e.g. capture.jpg)")
    p.add_argument("--out-dir", default="debug_out", help="Directory for output images")

    # Square detection params
    p.add_argument("--blur-kernel", type=int, default=DEFAULT_BLUR_KERNEL)
    p.add_argument("--thresh-block", type=int, default=DEFAULT_THRESH_BLOCK,
                   help="Adaptive threshold block size (must be odd)")
    p.add_argument("--thresh-c", type=int, default=DEFAULT_THRESH_C,
                   help="Adaptive threshold C constant")
    p.add_argument("--min-area", type=float, default=DEFAULT_MIN_AREA,
                   help="Minimum contour area to consider as a die face")
    p.add_argument("--max-area", type=float, default=DEFAULT_MAX_AREA,
                   help="Maximum contour area to consider as a die face")
    p.add_argument("--aspect-tolerance", type=float, default=DEFAULT_ASPECT_TOLERANCE,
                   help="Max aspect ratio (long/short) for a square contour")

    # Pip detection params
    p.add_argument("--pip-min-area", type=float, default=DEFAULT_PIP_MIN_AREA)
    p.add_argument("--pip-max-area", type=float, default=DEFAULT_PIP_MAX_AREA)
    p.add_argument("--pip-min-circularity", type=float, default=DEFAULT_PIP_MIN_CIRCULARITY)
    p.add_argument("--pip-min-convexity", type=float, default=DEFAULT_PIP_MIN_CONVEXITY)

    return p.parse_args()


def save(out_dir: Path, name: str, img: np.ndarray) -> None:
    path = out_dir / name
    cv2.imwrite(str(path), img)
    print(f"  saved: {path}")


def order_points(pts: np.ndarray) -> np.ndarray:
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]   # top-left
    rect[2] = pts[np.argmax(s)]   # bottom-right
    d = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(d)]   # top-right
    rect[3] = pts[np.argmax(d)]   # bottom-left
    return rect


def perspective_correct(image: np.ndarray, contour: np.ndarray, size: int = WARP_SIZE) -> np.ndarray:
    pts = order_points(contour.reshape(4, 2).astype(np.float32))
    dst = np.array([[0, 0], [size, 0], [size, size], [0, size]], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(pts, dst)
    return cv2.warpPerspective(image, matrix, (size, size))


def get_center(contour: np.ndarray) -> tuple[int, int]:
    m = cv2.moments(contour)
    if m["m00"] == 0:
        rect = cv2.minAreaRect(contour)
        return (int(rect[0][0]), int(rect[0][1]))
    return (int(m["m10"] / m["m00"]), int(m["m01"] / m["m00"]))


def count_pips(warped: np.ndarray, args) -> tuple[int | None, np.ndarray]:
    """Returns (pip_count, annotated_warped_image)."""
    gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY)

    params = cv2.SimpleBlobDetector_Params()
    params.filterByArea = True
    params.minArea = args.pip_min_area
    params.maxArea = args.pip_max_area
    params.filterByCircularity = True
    params.minCircularity = args.pip_min_circularity
    params.filterByConvexity = True
    params.minConvexity = args.pip_min_convexity
    params.filterByInertia = False

    detector = cv2.SimpleBlobDetector_create(params)
    keypoints = detector.detect(gray)

    annotated = warped.copy()
    for kp in keypoints:
        cx, cy = int(kp.pt[0]), int(kp.pt[1])
        r = max(3, int(kp.size / 2))
        cv2.circle(annotated, (cx, cy), r, (0, 255, 0), 2)

    count = len(keypoints)
    label = f"pips: {count}"
    cv2.putText(annotated, label, (5, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

    return (count if 1 <= count <= 6 else None), annotated


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
        print("ERROR: cv2.imread returned None — is the file a valid image?")
        sys.exit(1)
    print(f"  image shape: {image.shape}")

    # ── Stage 1: Grayscale ────────────────────────────────────────────────────
    print("\n[1/7] Grayscale + blur")
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    save(out_dir, "01_gray.jpg", gray)

    k = args.blur_kernel | 1  # ensure odd
    blurred = cv2.GaussianBlur(gray, (k, k), 0)
    save(out_dir, "02_blur.jpg", blurred)

    # ── Stage 2: Threshold ────────────────────────────────────────────────────
    print("\n[2/7] Adaptive threshold")
    block = args.thresh_block | 1  # ensure odd
    thresh = cv2.adaptiveThreshold(
        blurred, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        block, args.thresh_c,
    )
    save(out_dir, "03_thresh.jpg", thresh)

    # ── Stage 3: Find squares ─────────────────────────────────────────────────
    print("\n[3/7] Finding square contours")
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    print(f"  total contours found: {len(contours)}")

    contour_vis = image.copy()
    squares = []

    for contour in contours:
        peri = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.04 * peri, True)

        if len(approx) != 4:
            continue

        area = cv2.contourArea(approx)
        if area < args.min_area or area > args.max_area:
            cv2.drawContours(contour_vis, [approx], -1, (0, 0, 255), 1)  # red = rejected
            continue

        rect = cv2.minAreaRect(approx)
        w, h = rect[1]
        if w == 0 or h == 0:
            continue
        aspect = max(w, h) / min(w, h)
        if aspect > args.aspect_tolerance:
            cv2.drawContours(contour_vis, [approx], -1, (0, 165, 255), 1)  # orange = wrong shape
            continue

        cv2.drawContours(contour_vis, [approx], -1, (0, 255, 0), 2)  # green = accepted
        squares.append(approx)

    save(out_dir, "04_contours.jpg", contour_vis)
    print(f"  accepted squares: {len(squares)}")

    if len(squares) == 0:
        print("\n  HINT: No squares found. Try:")
        print("    --min-area smaller (current: {})".format(args.min_area))
        print("    --thresh-block larger (current: {})".format(args.thresh_block))
        print("    --aspect-tolerance larger (current: {})".format(args.aspect_tolerance))

    # ── Stage 4: Centers ──────────────────────────────────────────────────────
    print("\n[4/7] Extracting centers")
    squares_vis = image.copy()
    centers = []
    for sq in squares:
        center = get_center(sq)
        centers.append(center)
        cv2.drawContours(squares_vis, [sq], -1, (0, 255, 0), 2)
        cv2.circle(squares_vis, center, 8, (0, 0, 255), -1)
        cv2.putText(squares_vis, str(center), (center[0] + 10, center[1]),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)
    save(out_dir, "05_squares.jpg", squares_vis)

    # ── Stage 5: Perspective correct + pip count ──────────────────────────────
    print("\n[5/7] Perspective correct + pip detection")
    pip_counts = []
    for i, sq in enumerate(squares):
        warped = perspective_correct(image, sq)
        save(out_dir, f"06_warped_{i}.jpg", warped)

        count, pip_vis = count_pips(warped, args)
        save(out_dir, f"07_pips_{i}.jpg", pip_vis)

        status = str(count) if count is not None else "FAIL (out of 1-6 range)"
        print(f"  die {i}: pips = {status}")
        pip_counts.append(count)

    # ── Stage 6: Annotated result ─────────────────────────────────────────────
    print("\n[6/7] Annotated result")
    result_vis = image.copy()
    for i, (sq, center, count) in enumerate(zip(squares, centers, pip_counts)):
        color = (0, 255, 0) if count is not None else (0, 0, 255)
        cv2.drawContours(result_vis, [sq], -1, color, 3)
        cv2.circle(result_vis, center, 8, (0, 0, 255), -1)
        label = f"die{i+1}: {count if count is not None else '?'}"
        cv2.putText(result_vis, label, (center[0] - 30, center[1] - 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, color, 2)
    save(out_dir, "08_result.jpg", result_vis)

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n[7/7] Summary")
    print(f"  squares detected: {len(squares)} (need exactly 2)")
    if len(pip_counts) == 2 and all(c is not None for c in pip_counts):
        total = pip_counts[0] + pip_counts[1]
        print(f"  die1={pip_counts[0]}  die2={pip_counts[1]}  total={total}")
        print("  PASS")
    else:
        print("  FAIL — detection incomplete")
        print("\n  Tuning hints:")
        if len(squares) != 2:
            print(f"    squares={len(squares)} — adjust --min-area / --max-area / --aspect-tolerance")
        for i, c in enumerate(pip_counts):
            if c is None:
                print(f"    die{i} pip count failed — adjust --pip-min-area / --pip-max-area / --pip-min-circularity")

    print(f"\nAll debug images saved to: {out_dir}/")
    print("Suggested next run if tuning:")
    print(f"  python3 scripts/debug_detector.py {args.image} --min-area 500 --pip-min-area 30")


if __name__ == "__main__":
    main()
