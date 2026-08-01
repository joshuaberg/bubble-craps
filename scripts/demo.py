#!/usr/bin/env python3
"""Run the full roll-detect cycle without MQTT.

Loops: roll -> park -> capture -> detect -> print result -> repeat

Usage:
  python3 scripts/demo.py                # continuous rolls
  python3 scripts/demo.py --count 5      # 5 rolls then stop
  python3 scripts/demo.py --delay 5      # 5 seconds between rolls
"""

import argparse
import logging
import random
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bubble_craps.config import load_config
from bubble_craps.detector import DiceDetector
from bubble_craps.motor import CANMotorController
from bubble_craps.ring_light import NeoPixelRingLightController

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def init_camera(config):
    """Initialize the Pi camera."""
    from picamera2 import Picamera2
    import numpy as np

    cam = Picamera2()
    cam.configure(cam.create_still_configuration(main={"size": config.camera.resolution}))
    cam.start()
    time.sleep(2)  # let auto-exposure settle
    return cam


def capture(cam) -> "np.ndarray":
    """Capture a frame as a numpy array."""
    import numpy as np
    return cam.capture_array()


def do_roll(motor, config) -> None:
    """Spin, stop, park, settle."""
    mc = config.motor

    rpm = random.uniform(mc.roll_rpm_min, mc.roll_rpm_max)
    duration = random.uniform(mc.roll_duration_min_sec, mc.roll_duration_max_sec)

    print(f"  Rolling: {rpm:.0f} RPM for {duration:.1f}s")
    motor.start(rpm=rpm)
    time.sleep(duration)

    print("  Stopping...")
    motor.stop()
    time.sleep(0.5)

    print(f"  Parking at {mc.park_position} deg...")
    motor.go_to_angle(mc.park_position, speed_limit=mc.park_speed_limit)

    deadline = time.monotonic() + mc.park_timeout_sec
    while not motor.is_at_position():
        if time.monotonic() > deadline:
            print("  WARNING: Park timeout!")
            break
        time.sleep(mc.park_poll_interval_sec)

    print(f"  Settling for {mc.settling_time_sec}s...")
    time.sleep(mc.settling_time_sec)


def main():
    config = load_config()

    parser = argparse.ArgumentParser(description="Full roll-detect demo (no MQTT)")
    parser.add_argument("--count", type=int, default=0, help="Number of rolls (0 = infinite)")
    parser.add_argument("--delay", type=float, default=3.0, help="Seconds between rolls")
    parser.add_argument("--save-images", action="store_true", help="Save capture images to demo_out/")
    parser.add_argument("--channel", default=config.motor.can_channel)
    parser.add_argument("--can-id", default=f"0x{config.motor.can_id:X}")
    args = parser.parse_args()

    can_id = int(args.can_id, 16) if isinstance(args.can_id, str) else args.can_id

    # Init hardware
    print("Initializing motor...")
    try:
        motor = CANMotorController(channel=args.channel, can_id=can_id)
    except Exception as e:
        print(f"Failed to open CAN bus: {e}")
        sys.exit(1)

    print("Initializing camera...")
    cam = init_camera(config)

    print("Initializing ring light...")
    try:
        light = NeoPixelRingLightController(
            gpio_pin=config.ring_light.gpio_pin,
            num_leds=config.ring_light.num_leds,
            brightness=config.ring_light.idle_brightness,
        )
    except Exception as e:
        print(f"  Ring light failed: {e} (continuing without it)")
        from bubble_craps.ring_light import MockRingLightController
        light = MockRingLightController()

    detector = DiceDetector(config.detection)

    if args.save_images:
        out_dir = Path("demo_out")
        out_dir.mkdir(exist_ok=True)

    roll_num = 0
    results = []

    print("\n" + "=" * 50)
    print("  BUBBLE CRAPS DEMO")
    print("=" * 50)
    print(f"  Rolls: {'infinite' if args.count == 0 else args.count}")
    print(f"  Delay: {args.delay}s between rolls")
    print("  Press Ctrl+C to stop\n")

    try:
        while True:
            roll_num += 1
            if args.count > 0 and roll_num > args.count:
                break

            print(f"\n--- Roll #{roll_num} ---")

            # Roll
            do_roll(motor, config)

            # Capture — full brightness white for the photo
            light.set_pattern("capture")
            time.sleep(0.3)  # let LEDs stabilize
            print("  Capturing image...")
            image = capture(cam)
            light.set_pattern("idle")

            if args.save_images:
                cv2.imwrite(str(out_dir / f"roll_{roll_num:03d}.jpg"), image)
                print(f"  Saved: demo_out/roll_{roll_num:03d}.jpg")

            # Detect
            print("  Detecting dice...")
            result = detector.detect(image)

            if result is None:
                print("  DETECTION FAILED")
                if args.save_images:
                    cv2.imwrite(str(out_dir / f"roll_{roll_num:03d}_FAIL.jpg"), image)
            else:
                total = result["die1"] + result["die2"]
                print(f"  >> Die 1: {result['die1']}  Die 2: {result['die2']}  Total: {total}")
                results.append(total)

            # Wait before next roll
            if args.count == 0 or roll_num < args.count:
                print(f"  Waiting {args.delay}s...")
                time.sleep(args.delay)

    except KeyboardInterrupt:
        print("\n\nInterrupted!")

    finally:
        print("\nStopping motor...")
        motor.stop()
        motor.shutdown()
        light.off()
        cam.stop()
        cam.close()

        # Summary
        if results:
            print(f"\n{'=' * 50}")
            print(f"  SUMMARY: {len(results)} successful rolls")
            print(f"  Results: {results}")
            print(f"  Average: {sum(results) / len(results):.1f}")
            print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
