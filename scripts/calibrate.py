#!/usr/bin/env python3
"""Interactive calibration tool for the Bubble Craps platform.

Run this on the Pi to find the ideal park position for the dice platform,
then save it to config.yaml.

Prerequisites:
  sudo ip link set can0 up type can bitrate 1000000

Usage:
  python3 scripts/calibrate.py
  python3 scripts/calibrate.py --channel can0 --can-id 0x141

Controls:
  Left/Right arrows   Nudge by 1 degree
  Up/Down arrows       Nudge by 10 degrees
  p                    Print current position
  s                    Save current angle as park_position
  q                    Stop motor and exit
"""

import argparse
import sys
import termios
import time
import tty
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml

from bubble_craps.motor import CANMotorController
from bubble_craps.config import DEFAULT_CONFIG_PATH

SMALL_STEP = 1.0   # degrees per left/right arrow
BIG_STEP = 10.0    # degrees per up/down arrow


def get_key():
    """Read a single keypress, handling arrow key escape sequences."""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            seq = sys.stdin.read(2)
            if seq == "[A":
                return "up"
            elif seq == "[B":
                return "down"
            elif seq == "[C":
                return "right"
            elif seq == "[D":
                return "left"
            return None
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def update_config_park_position(angle: float, config_path: Path) -> None:
    """Update park_position in config.yaml while preserving the rest of the file."""
    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    raw.setdefault("motor", {})["park_position"] = round(angle, 2)

    with open(config_path, "w") as f:
        yaml.dump(raw, f, default_flow_style=False, sort_keys=False)

    print(f"\r  Saved park_position: {angle:.2f} to {config_path}")


def main():
    parser = argparse.ArgumentParser(description="Calibrate dice platform park position")
    parser.add_argument("--channel", default="can0", help="CAN interface (default: can0)")
    parser.add_argument("--can-id", default="0x141", help="Motor CAN ID in hex (default: 0x141)")
    parser.add_argument("--speed", type=float, default=30.0, help="Speed for movements (RPM, default: 30)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to config.yaml")
    args = parser.parse_args()

    can_id = int(args.can_id, 16)
    config_path = Path(args.config)

    print("Bubble Craps Platform Calibration")
    print(f"  CAN: {args.channel}  ID: 0x{can_id:X}  Speed: {args.speed} RPM")
    print(f"  Config: {config_path}")
    print()

    try:
        motor = CANMotorController(channel=args.channel, can_id=can_id)
    except Exception as e:
        print(f"Failed to open CAN bus: {e}")
        print("Make sure: sudo ip link set can0 up type can bitrate 1000000")
        sys.exit(1)

    pos = motor.get_position()
    if pos is None:
        print("ERROR: Cannot read motor position")
        motor.shutdown()
        sys.exit(1)

    print(f"Current position: {pos:.2f} deg (angle: {pos % 360:.2f})")
    print()
    print("Controls:")
    print("  Left/Right   Nudge ±1 deg")
    print("  Up/Down      Nudge ±10 deg")
    print("  p            Show position")
    print("  s            Save as park_position")
    print("  q            Quit")
    print()

    def nudge(delta: float):
        """Move motor by delta degrees from current position."""
        pos = motor.get_position()
        if pos is None:
            print("\r  ERROR: could not read position")
            return
        target = pos + delta
        motor.go_to_position(target, speed_limit=args.speed)
        # Wait for move
        deadline = time.monotonic() + 10.0
        while not motor.is_at_position():
            if time.monotonic() > deadline:
                print("\r  Timeout!")
                return
            time.sleep(0.1)
        final = motor.get_position()
        if final is not None:
            print(f"\r  {delta:+.1f} -> Position: {final:.2f} deg (angle: {final % 360:.2f})    ")

    def show_position():
        pos = motor.get_position()
        if pos is not None:
            print(f"\r  Position: {pos:.2f} deg (angle: {pos % 360:.2f})    ")
        else:
            print("\r  ERROR: could not read position")

    def save_position():
        pos = motor.get_position()
        if pos is None:
            print("\r  ERROR: could not read position")
            return
        angle = round(pos % 360, 2)
        print(f"\r  Current angle: {angle:.2f}")
        print(f"\r  Save as park_position? [y/N] ", end="", flush=True)
        key = get_key()
        if key == "y":
            print()
            update_config_park_position(angle, config_path)
        else:
            print("\r  Cancelled                      ")

    try:
        while True:
            key = get_key()

            if key == "q" or key == "\x03":  # q or Ctrl+C
                break
            elif key == "right":
                nudge(SMALL_STEP)
            elif key == "left":
                nudge(-SMALL_STEP)
            elif key == "up":
                nudge(BIG_STEP)
            elif key == "down":
                nudge(-BIG_STEP)
            elif key == "p":
                show_position()
            elif key == "s":
                save_position()

    except KeyboardInterrupt:
        print("\nInterrupted")

    finally:
        print("\nStopping motor...")
        motor.stop()
        motor.shutdown()
        print("Done")


if __name__ == "__main__":
    main()
