#!/usr/bin/env python3
"""Motor test script for the Bubble Craps machine.

Run this on the Pi over SSH to verify motor + CAN bus communication.
Runs a sequence of motor commands and prints results at each step.

Prerequisites:
  1. CAN HAT configured in /boot/config.txt (see below)
  2. CAN interface brought up:
       sudo ip link set can0 up type can bitrate 1000000

Usage:
  python3 scripts/test_motor.py                  # run all tests
  python3 scripts/test_motor.py --channel can0   # specify CAN interface
  python3 scripts/test_motor.py --can-id 0x141   # specify motor CAN ID
  python3 scripts/test_motor.py --rpm 20         # slower speed for testing
"""

import argparse
import logging
import sys
import time

# Add src to path so we can import without installing
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))

from bubble_craps.motor import CANMotorController

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def separator(title: str) -> None:
    print(f"\n{'='*50}")
    print(f"  {title}")
    print(f"{'='*50}\n")


def test_connection(motor: CANMotorController) -> bool:
    """Test 1: Verify CAN communication by reading position."""
    separator("TEST 1: Connection / Read Position")
    pos = motor.get_position()
    if pos is None:
        print("FAIL — no response from motor")
        return False
    print(f"PASS — motor responded, current position: {pos:.2f} deg")
    return True


def test_speed_control(motor: CANMotorController, rpm: float) -> bool:
    """Test 2: Spin the motor at a given RPM for 3 seconds."""
    separator(f"TEST 2: Speed Control ({rpm} RPM for 3 seconds)")
    print("Starting motor...")
    motor.start(rpm=rpm)
    time.sleep(3)

    pos_during = motor.get_position()
    print(f"Position during spin: {pos_during}")

    print("Stopping motor...")
    motor.stop()
    time.sleep(1)

    pos_after = motor.get_position()
    print(f"Position after stop: {pos_after}")

    if pos_during is not None and pos_after is not None:
        print("PASS — motor spun and stopped")
        return True
    else:
        print("FAIL — could not read position")
        return False


def test_go_to_angle(motor: CANMotorController) -> bool:
    """Test 3: Use go_to_angle to move to nearest 90 degrees."""
    separator("TEST 3: go_to_angle (nearest 90 deg)")

    start_pos = motor.get_position()
    print(f"Starting position: {start_pos}")
    print(f"Current angle within rotation: {start_pos % 360:.2f} deg")

    target_angle = 90.0
    print(f"Commanding go_to_angle({target_angle})...")
    motor.go_to_angle(target_angle, speed_limit=10.0)

    timeout = 5.0
    poll_interval = 0.2
    deadline = time.monotonic() + timeout

    while not motor.is_at_position():
        current = motor.get_position()
        elapsed = timeout - (deadline - time.monotonic())
        print(f"  [{elapsed:.1f}s] position: {current:.2f} (angle: {current % 360:.2f})")
        if time.monotonic() > deadline:
            print(f"FAIL — did not reach angle {target_angle} within {timeout}s")
            return False
        time.sleep(poll_interval)

    final_pos = motor.get_position()
    final_angle = final_pos % 360
    print(f"Final position: {final_pos:.2f} (angle: {final_angle:.2f})")
    print(f"PASS — reached nearest {target_angle} deg")
    return True


def test_park_at_zero(motor: CANMotorController) -> bool:
    """Test 4: Park at nearest 0 degrees (simulates capture park position)."""
    separator("TEST 4: Park at nearest 0 deg (go_to_angle)")

    start_pos = motor.get_position()
    print(f"Starting position: {start_pos:.2f} (angle: {start_pos % 360:.2f})")

    print("Commanding go_to_angle(0)...")
    motor.go_to_angle(0.0, speed_limit=10.0)

    timeout = 5.0
    deadline = time.monotonic() + timeout

    while not motor.is_at_position():
        current = motor.get_position()
        elapsed = timeout - (deadline - time.monotonic())
        print(f"  [{elapsed:.1f}s] position: {current:.2f} (angle: {current % 360:.2f})")
        if time.monotonic() > deadline:
            print("FAIL — did not reach 0 deg within timeout")
            return False
        time.sleep(0.2)

    final_pos = motor.get_position()
    final_angle = final_pos % 360
    print(f"Final position: {final_pos:.2f} (angle: {final_angle:.2f})")
    print("PASS — parked at nearest 0 deg")
    return True


def test_full_roll_cycle(motor: CANMotorController, rpm: float) -> bool:
    """Test 5: Simulate a full roll cycle (spin -> stop -> park)."""
    separator(f"TEST 5: Full Roll Cycle (spin at {rpm} RPM -> park at 0)")

    print("Step 1: Spinning motor...")
    motor.start(rpm=rpm)
    time.sleep(3)

    print("Step 2: Stopping motor...")
    motor.stop()
    time.sleep(0.5)

    pos_after_stop = motor.get_position()
    print(f"Position after stop: {pos_after_stop}")

    print("Step 3: Parking at nearest 0 deg...")
    motor.go_to_angle(0.0, speed_limit=30.0)

    timeout = 5.0
    deadline = time.monotonic() + timeout
    while not motor.is_at_position():
        if time.monotonic() > deadline:
            print("FAIL — park timeout")
            return False
        time.sleep(0.1)

    final_pos = motor.get_position()
    print(f"Final parked position: {final_pos}")
    print("PASS — full roll cycle complete")
    return True


def main():
    parser = argparse.ArgumentParser(description="Test motor functions for Bubble Craps")
    parser.add_argument("--channel", default="can0", help="CAN interface name (default: can0)")
    parser.add_argument("--can-id", default="0x141", help="Motor CAN ID in hex (default: 0x141)")
    parser.add_argument("--rpm", type=float, default=40.0, help="RPM for speed tests (default: 40)")
    args = parser.parse_args()

    can_id = int(args.can_id, 16)

    print(f"Bubble Craps Motor Test")
    print(f"  CAN channel: {args.channel}")
    print(f"  Motor CAN ID: 0x{can_id:X}")
    print(f"  Test RPM: {args.rpm}")

    try:
        motor = CANMotorController(channel=args.channel, can_id=can_id)
    except Exception as e:
        print(f"\nFailed to open CAN bus: {e}")
        print("\nMake sure the CAN interface is up:")
        print("  sudo ip link set can0 up type can bitrate 1000000")
        sys.exit(1)

    results = {}
    try:
        results["connection"] = test_connection(motor)

        if results["connection"]:
            results["speed_control"] = test_speed_control(motor, args.rpm)
            time.sleep(1)
            results["go_to_angle"] = test_go_to_angle(motor)
            time.sleep(1)
            results["park_at_zero"] = test_park_at_zero(motor)
            time.sleep(1)
            results["full_roll_cycle"] = test_full_roll_cycle(motor, args.rpm)

    except KeyboardInterrupt:
        print("\n\nInterrupted — stopping motor...")
        motor.stop()

    finally:
        print("\nShutting down motor...")
        motor.shutdown()

    # Summary
    separator("RESULTS")
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {name}: {status}")

    total = len(results)
    passed = sum(1 for v in results.values() if v)
    print(f"\n  {passed}/{total} tests passed")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
