#!/usr/bin/env python3
"""
Torque logging script for MG4010 motor.

Spins the motor at a given RPM and logs torque, speed, and shaft angle
from the CAN responses as fast as the bus allows (~1kHz on MCP2515).

Usage:
    python torque_log.py --rpm 500 --duration 10
    python torque_log.py --rpm 450 --duration 15 --output my_log.csv
"""

import argparse
import csv
import struct
import time

import can


CAN_ID = 0x141
TAYLOR_FACTOR = 645. # For MF5005, this changes with motor 


def send(bus: can.Bus, data: list[int]) -> bool:
    msg = can.Message(arbitration_id=CAN_ID, data=data, is_extended_id=False)
    try:
        bus.send(msg, timeout=2)
        return True
    except can.CanError:
        print("Failed to send CAN message")
        return False


def read_response(bus: can.Bus, timeout: float = 2.0):
    return bus.recv(timeout=timeout)


def motor_on(bus: can.Bus):
    send(bus, [0x88, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    read_response(bus)
    print("Motor ON")


def motor_off(bus: can.Bus):
    send(bus, [0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    read_response(bus)
    print("Motor OFF")


def motor_stop(bus: can.Bus):
    send(bus, [0x81, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    read_response(bus)
    print("Motor stopped")


def build_speed_command(rpm: float) -> list[int]:
    cdps = int(rpm * 360 / 60 * 100)
    return [
        0xA2, 0x00, 0x00, 0x00,
        cdps & 0xFF,
        (cdps >> 8) & 0xFF,
        (cdps >> 16) & 0xFF,
        (cdps >> 24) & 0xFF,
    ]


def parse_response(msg) -> dict | None:
    if msg is None:
        return None
    d = msg.data
    temp = struct.unpack_from("b", d, 1)[0]
    torque_raw = struct.unpack_from("<h", d, 2)[0]
    speed_dps = struct.unpack_from("<h", d, 4)[0]
    encoder_14bit = struct.unpack_from("<H", d, 6)[0] >> 2
    return {
        "temp_c": temp,
        "torque_milli_nm": torque_raw * TAYLOR_FACTOR // 1000,
        "speed_dps": speed_dps,
        "angle_deg": round(encoder_14bit * 360 / 16383, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="Log motor torque over time")
    parser.add_argument("--rpm", type=float, default=500.0, help="Motor speed in RPM (default: 500)")
    parser.add_argument("--duration", type=float, default=10.0, help="How long to log in seconds (default: 10)")
    parser.add_argument("--output", type=str, default=None, help="Output CSV filename")
    parser.add_argument("--channel", type=str, default="can0", help="CAN channel (default: can0)")
    args = parser.parse_args()

    if args.output is None:
        args.output = f"torque_log_{int(args.rpm)}rpm_{int(args.duration)}s.csv"

    bus = can.Bus(interface="socketcan", channel=args.channel)
    speed_cmd = build_speed_command(args.rpm)

    try:
        motor_on(bus)
        time.sleep(0.2)

        # First command to get it spinning
        send(bus, speed_cmd)
        read_response(bus)
        print(f"Motor started at {args.rpm} RPM")

        rows = []
        t_start = time.monotonic()
        print(f"Logging for {args.duration}s...")

        while True:
            elapsed = time.monotonic() - t_start
            if elapsed >= args.duration:
                break

            send(bus, speed_cmd)
            resp = read_response(bus)
            info = parse_response(resp)
            if info:
                rows.append((round(elapsed, 4), info["temp_c"], info["torque_milli_nm"], info["speed_dps"], info["angle_deg"]))

        motor_stop(bus)
        time.sleep(0.5)
        motor_off(bus)

    finally:
        bus.shutdown()

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
