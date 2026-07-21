#!/usr/bin/env python3
"""Pop the dice! Does one fast revolution and parks back at home.

Usage:
  python3 scripts/pop.py
  python3 scripts/pop.py --rpm 800
  python3 scripts/pop.py --revolutions 2
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bubble_craps.config import load_config
from bubble_craps.motor import CANMotorController

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def main():
    config = load_config()
    mc = config.motor

    parser = argparse.ArgumentParser(description="Pop the dice with a fast revolution")
    parser.add_argument("--channel", default=mc.can_channel)
    parser.add_argument("--can-id", default=f"0x{mc.can_id:X}")
    parser.add_argument("--rpm", type=float, default=800.0, help="Speed for the pop (default: 800)")
    parser.add_argument("--revolutions", type=float, default=1.0, help="Number of revolutions (default: 1)")
    parser.add_argument("--park-speed", type=float, default=mc.park_speed_limit)
    args = parser.parse_args()

    can_id = int(args.can_id, 16) if isinstance(args.can_id, str) else args.can_id

    try:
        motor = CANMotorController(channel=args.channel, can_id=can_id)
    except Exception as e:
        print(f"Failed to open CAN bus: {e}")
        sys.exit(1)

    try:
        pos = motor.get_position()
        if pos is None:
            print("ERROR: Cannot read motor position")
            sys.exit(1)

        target = pos + (360.0 * args.revolutions)
        print(f"Pop! {args.revolutions} rev at {args.rpm} RPM")
        print(f"  Current: {pos:.2f} -> Target: {target:.2f}")

        # Fast move forward by N revolutions
        motor.go_to_position(target, speed_limit=args.rpm)

        deadline = time.monotonic() + 10.0
        while not motor.is_at_position():
            if time.monotonic() > deadline:
                print("WARNING: Timeout!")
                break
            time.sleep(0.05)

        # Park at home
        print(f"Parking at {mc.park_position} deg...")
        motor.go_to_angle(mc.park_position, speed_limit=args.park_speed)

        deadline = time.monotonic() + mc.park_timeout_sec
        while not motor.is_at_position():
            if time.monotonic() > deadline:
                print("WARNING: Park timeout!")
                break
            time.sleep(0.1)

        final = motor.get_position()
        if final is not None:
            print(f"Done! Position: {final:.2f} (angle: {final % 360:.2f})")

    except KeyboardInterrupt:
        print("\nInterrupted — stopping...")
        motor.stop()

    finally:
        motor.shutdown()


if __name__ == "__main__":
    main()
