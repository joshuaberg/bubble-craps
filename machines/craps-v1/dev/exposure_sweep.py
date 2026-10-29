#!/usr/bin/env python3
"""Exposure sweep tool — capture images at different manual exposure times.

Turns the ring light to capture brightness, then sweeps through exposure
values, saving an image for each. Run on the Pi with sudo.

Usage:
  sudo /path/to/venv/bin/python3 dev/exposure_sweep.py
  sudo /path/to/venv/bin/python3 dev/exposure_sweep.py --start 5000 --end 50000 --step 5000
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bubble_craps.config import load_config


def main():
    config = load_config()

    p = argparse.ArgumentParser(description="Exposure sweep")
    p.add_argument("--out-dir", default="debug_out", help="Output directory")
    p.add_argument("--start", type=int, default=5000, help="Starting exposure time in microseconds (default: 5000)")
    p.add_argument("--end", type=int, default=50000, help="Ending exposure time in microseconds (default: 50000)")
    p.add_argument("--step", type=int, default=5000, help="Step size in microseconds (default: 5000)")
    p.add_argument("--settle", type=float, default=3.0, help="Seconds to wait after changing exposure (default: 3.0)")
    args = p.parse_args()

    try:
        from picamera2 import Picamera2
    except ImportError:
        print("ERROR: picamera2 not available. Run on the Pi.")
        sys.exit(1)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Init ring light at capture brightness
    try:
        from bubble_craps.ring_light import NeoPixelRingLightController
        light = NeoPixelRingLightController(
            gpio_pin=config.ring_light.gpio_pin,
            num_leds=config.ring_light.num_leds,
            brightness=config.ring_light.idle_brightness,
        )
        light.set_pattern("capture")
        print("Ring light set to capture brightness")
    except Exception as e:
        print(f"Ring light not available: {e}")
        light = None

    # Init camera
    print("Initializing camera...")
    cam = Picamera2()
    cam.configure(cam.create_still_configuration(main={"size": config.camera.resolution}))
    cam.start()
    time.sleep(3)  # initial settle

    print(f"\nSweeping exposure {args.start} to {args.end} us (step {args.step}, settle {args.settle}s)")
    print(f"Output: {out_dir}/\n")

    for exposure in range(args.start, args.end + 1, args.step):
        cam.set_controls({
            "AeEnable": False,
            "ExposureTime": exposure,
            "AnalogueGain": 1.0,
        })
        print(f"  Exposure: {exposure} us — settling {args.settle}s...", end="", flush=True)
        time.sleep(args.settle)

        path = out_dir / f"exposure_{exposure:06d}.jpg"
        cam.capture_file(str(path))
        print(f" saved: {path}")

    print("\nDone!")

    cam.stop()
    cam.close()
    if light:
        light.off()


if __name__ == "__main__":
    main()
