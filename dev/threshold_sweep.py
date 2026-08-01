#!/usr/bin/env python3
"""Threshold sweep tool — try a range of threshold values on an image.

Usage:
  python3 dev/threshold_sweep.py capture.jpg
  python3 dev/threshold_sweep.py capture.jpg --roi-radius 650
  python3 dev/threshold_sweep.py capture.jpg --start 100 --end 250 --step 10
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np


def main():
    p = argparse.ArgumentParser(description="Threshold sweep")
    p.add_argument("image", help="Path to input image")
    p.add_argument("--out-dir", default="debug_out", help="Output directory")
    p.add_argument("--start", type=int, default=80, help="Starting threshold (default: 80)")
    p.add_argument("--end", type=int, default=255, help="Ending threshold (default: 255)")
    p.add_argument("--step", type=int, default=15, help="Step size (default: 15)")

    # ROI
    p.add_argument("--roi-cx", type=int, default=0)
    p.add_argument("--roi-cy", type=int, default=0)
    p.add_argument("--roi-radius", type=int, default=0)

    args = p.parse_args()

    image = cv2.imread(args.image)
    if image is None:
        print(f"ERROR: could not load {args.image}")
        sys.exit(1)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ROI mask
    if args.roi_radius > 0:
        h, w = image.shape[:2]
        cx = args.roi_cx if args.roi_cx > 0 else w // 2
        cy = args.roi_cy if args.roi_cy > 0 else h // 2
        roi_mask = np.zeros((h, w), dtype=np.uint8)
        cv2.circle(roi_mask, (cx, cy), args.roi_radius, 255, -1)
        image = cv2.bitwise_and(image, image, mask=roi_mask)

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    print(f"Sweeping threshold {args.start} to {args.end} (step {args.step})")
    for val in range(args.start, args.end + 1, args.step):
        _, thresh = cv2.threshold(blurred, val, 255, cv2.THRESH_BINARY)
        path = out_dir / f"thresh_{val:03d}.jpg"
        cv2.imwrite(str(path), thresh)
        print(f"  saved: {path}")

    print(f"\nAll images saved to: {out_dir}/")


if __name__ == "__main__":
    main()
