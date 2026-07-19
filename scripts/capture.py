#!/usr/bin/env python3
"""Capture a single image from the Pi camera and save it to disk.

Run this on the Pi to grab a frame for tuning the detector on your laptop.

Usage:
  python3 scripts/capture.py                       # saves to capture.jpg
  python3 scripts/capture.py --output my_shot.jpg  # custom filename
  python3 scripts/capture.py --width 1920 --height 1080
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bubble_craps.config import CameraConfig


def main():
    parser = argparse.ArgumentParser(description="Capture image from Pi camera")
    parser.add_argument("--output", default="capture.jpg", help="Output file path")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    args = parser.parse_args()

    config = CameraConfig(resolution=(args.width, args.height))

    try:
        from picamera2 import Picamera2
    except ImportError:
        print("ERROR: picamera2 not available. Run this script on the Pi.")
        sys.exit(1)

    print(f"Initializing camera at {args.width}x{args.height}...")
    cam = Picamera2()
    cam.configure(cam.create_still_configuration(main={"size": config.resolution}))
    cam.start()

    import time
    time.sleep(2)  # let auto-exposure settle

    output_path = Path(args.output)
    cam.capture_file(str(output_path))
    cam.stop()
    cam.close()

    print(f"Saved: {output_path.resolve()}")
    print(f"\nCopy to your laptop with:")
    print(f"  scp pi:~/bubble-craps/{output_path} .")


if __name__ == "__main__":
    main()
