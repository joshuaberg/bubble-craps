#!/usr/bin/env python3
"""
Torque logging script for LK motor.

Spins the motor at a given RPM and logs torque, speed, and shaft angle
from the CAN responses as fast as the bus allows (~1kHz on MCP2515).

Usage:
    python torque_log.py --rpm 500 --duration 10
    python torque_log.py --rpm 450 --duration 15 --output my_log.csv
"""

import argparse
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bubble_craps.config import load_config
from bubble_craps.motor import CANMotorController


def main():
    parser = argparse.ArgumentParser(description="Log motor torque over time")
    parser.add_argument("--rpm", type=float, default=500.0, help="Motor speed in RPM (default: 500)")
    parser.add_argument("--duration", type=float, default=10.0, help="How long to log in seconds (default: 10)")
    parser.add_argument("--output", type=str, default=None, help="Output CSV filename")
    parser.add_argument("--channel", type=str, default=None, help="CAN channel (overrides config)")
    args = parser.parse_args()

    config = load_config()

    if args.output is None:
        args.output = f"torque_log_{int(args.rpm)}rpm_{int(args.duration)}s.csv"

    channel = args.channel or config.motor.can_channel
    motor = CANMotorController(
        channel=channel,
        can_id=config.motor.can_id,
        torque_scale_factor=config.motor.torque_scale_factor,
    )

    try:
        motor.spin(rpm=args.rpm)
        print(f"Motor spinning at {args.rpm} RPM")

        rows = []
        t_start = time.monotonic()
        print(f"Logging for {args.duration}s...")

        while True:
            elapsed = time.monotonic() - t_start
            if elapsed >= args.duration:
                break

            status = motor.get_status()
            if status:
                rows.append((
                    round(elapsed, 4),
                    status.temp_c,
                    status.torque_milli_nm,
                    status.speed_dps,
                    status.angle_deg,
                ))

        motor.stop()
        time.sleep(0.5)
        motor.shutdown()

    except Exception:
        motor.shutdown()
        raise

    # Write CSV
    fields = ["time_s", "temp_c", "torque_milli_nm", "speed_dps", "angle_deg"]
    if rows:
        with open(args.output, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(fields)
            writer.writerows(rows)

        print(f"\nWrote {len(rows)} samples to {args.output}")
        print(f"  Sample rate: {len(rows) / args.duration:.0f} Hz")

        torques = [r[2] for r in rows]
        print(f"  Torque — min: {min(torques)}, max: {max(torques)}, avg: {sum(torques)/len(torques):.1f} milli-Nm")
        print(f"  Temp   — {rows[-1][1]}°C")
    else:
        print("No data collected!")


if __name__ == "__main__":
    main()
