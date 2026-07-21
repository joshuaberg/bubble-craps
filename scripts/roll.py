#!/usr/bin/env python3
"""Execute a single dice roll on the Bubble Craps machine.

Spins the motor at a random RPM for a random duration, then parks
at the calibrated position. No camera/detection/MQTT — just the motor.

Usage:
  python3 scripts/roll.py
  python3 scripts/roll.py --rpm-min 200 --rpm-max 600
  python3 scripts/roll.py --duration-min 2 --duration-max 5
"""

import argparse
import logging
import random
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
logger = logging.getLogger(__name__)


def main():
    config = load_config()
    mc = config.motor

    parser = argparse.ArgumentParser(description="Execute a single dice roll")
    parser.add_argument("--channel", default=mc.can_channel)
    parser.add_argument("--can-id", default=f"0x{mc.can_id:X}")
    parser.add_argument("--rpm-min", type=float, default=mc.roll_rpm_min)
    parser.add_argument("--rpm-max", type=float, default=mc.roll_rpm_max)
    parser.add_argument("--duration-min", type=float, default=mc.roll_duration_min_sec)
    parser.add_argument("--duration-max", type=float, default=mc.roll_duration_max_sec)
    parser.add_argument("--park-position", type=float, default=mc.park_position)
    parser.add_argument("--park-speed", type=float, default=mc.park_speed_limit)
    parser.add_argument("--settling-time", type=float, default=mc.settling_time_sec)
    args = parser.parse_args()

    can_id = int(args.can_id, 16) if isinstance(args.can_id, str) else args.can_id

    rpm = random.uniform(args.rpm_min, args.rpm_max)
    duration = random.uniform(args.duration_min, args.duration_max)

    print(f"Roll parameters:")
    print(f"  RPM:      {rpm:.1f}  (range: {args.rpm_min}-{args.rpm_max})")
    print(f"  Duration: {duration:.1f}s (range: {args.duration_min}-{args.duration_max})")
    print(f"  Park at:  {args.park_position} deg")
    print()

    try:
        motor = CANMotorController(channel=args.channel, can_id=can_id)
    except Exception as e:
        print(f"Failed to open CAN bus: {e}")
        print("Make sure: sudo ip link set can0 up type can bitrate 1000000")
        sys.exit(1)

    try:
        # Spin
        print(f"Spinning at {rpm:.1f} RPM for {duration:.1f}s...")
        motor.start(rpm=rpm)
        time.sleep(duration)

        # Stop
        print("Stopping...")
        motor.stop()
        time.sleep(0.5)

        # Park
        print(f"Parking at {args.park_position} deg...")
        motor.go_to_angle(args.park_position, speed_limit=args.park_speed)

        deadline = time.monotonic() + mc.park_timeout_sec
        while not motor.is_at_position():
            if time.monotonic() > deadline:
                print("WARNING: Park timeout!")
                break
            time.sleep(0.1)

        # Settle
        print(f"Settling for {args.settling_time}s...")
        time.sleep(args.settling_time)

        pos = motor.get_position()
        if pos is not None:
            print(f"\nFinal position: {pos:.2f} deg (angle: {pos % 360:.2f})")

        print("Roll complete!")

    except KeyboardInterrupt:
        print("\nInterrupted — stopping motor...")
        motor.stop()

    finally:
        motor.shutdown()


if __name__ == "__main__":
    main()
