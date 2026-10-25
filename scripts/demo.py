#!/usr/bin/env python3
"""Run the full roll-detect cycle without MQTT.

Uses the same state machine as production, but prints results to console
and optionally saves images instead of publishing via MQTT.

Usage:
  python3 scripts/demo.py                # continuous rolls
  python3 scripts/demo.py --count 5      # 5 rolls then stop
  python3 scripts/demo.py --delay 5      # 5 seconds between rolls
  python3 scripts/demo.py --save-images  # save captures to demo_out/
"""

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bubble_craps.camera import PiCamera
from bubble_craps.config import load_config
from bubble_craps.detector import DiceDetector
from bubble_craps.motor import CANMotorController
from bubble_craps.ring_light import NeoPixelRingLightController
from bubble_craps.state_machine import State, StateMachine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


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

    if args.save_images:
        out_dir = Path("demo_out")
        out_dir.mkdir(exist_ok=True)

    # Init hardware
    print("Initializing motor...")
    try:
        motor = CANMotorController(channel=args.channel, can_id=can_id, torque_scale_factor=config.motor.torque_scale_factor)
    except Exception as e:
        print(f"Failed to open CAN bus: {e}")
        sys.exit(1)

    print("Initializing camera...")
    camera = PiCamera(config.camera)

    print("Initializing ring light...")
    try:
        light = NeoPixelRingLightController(
            gpio_pin=config.ring_light.gpio_pin,
            num_leds=config.ring_light.num_leds,
            brightness=config.ring_light.idle_brightness,
        )
    except Exception as e:
        print(f"  Ring light failed: {e} (continuing without it)")
        light = None

    detector = DiceDetector(config.detection)

    # Track results for summary
    results = []
    roll_num = 0

    def on_status(status):
        logger.info("Status: state=%s", status["state"])

    state_machine = StateMachine(
        config, motor, light, camera, detector,
        on_status=on_status,
    )

    # Graceful shutdown
    shutdown_event = threading.Event()

    def signal_handler(sig, frame):
        print("\n\nInterrupted!")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)

    print("\n" + "=" * 50)
    print("  BUBBLE CRAPS DEMO")
    print("=" * 50)
    print(f"  Rolls: {'infinite' if args.count == 0 else args.count}")
    print(f"  Delay: {args.delay}s between rolls")
    print("  Press Ctrl+C to stop\n")

    try:
        while not shutdown_event.is_set():
            if args.count > 0 and roll_num >= args.count:
                break

            roll_num += 1
            print(f"\n--- Roll #{roll_num} ---")

            try:
                outcome = state_machine.run_roll()
            except Exception:
                logger.exception("Unexpected error during roll")
                continue

            if outcome.status == "ok":
                r = outcome.result
                print(f"  >> Die 1: {r['die1']}  Die 2: {r['die2']}  Total: {r['die1'] + r['die2']}")
                results.append(r["die1"] + r["die2"])
            elif outcome.status == "detection_failed":
                print("  >> DETECTION FAILED: Could not detect exactly 2 dice")
            elif outcome.status == "camera_failure":
                print("  >> CAMERA FAILURE")
            elif outcome.status == "mechanism_failure":
                print("  >> MECHANISM FAILURE: same dice as previous roll")

            if args.save_images and outcome.image_bytes:
                path = out_dir / f"roll_{roll_num:03d}.jpg"
                path.write_bytes(outcome.image_bytes)
                print(f"  Saved: {path}")

            state_machine.transition(State.IDLE)

            if not shutdown_event.is_set():
                shutdown_event.wait(timeout=args.delay)

    finally:
        print("\nShutting down...")
        try:
            motor.stop()
            motor.shutdown()
        except Exception as e:
            logger.error("Motor shutdown failed: %s", e)
        try:
            if light:
                light.off()
        except Exception as e:
            logger.error("Ring light shutdown failed: %s", e)
        try:
            camera.close()
        except Exception as e:
            logger.error("Camera shutdown failed: %s", e)

        # Summary
        if results:
            print(f"\n{'=' * 50}")
            print(f"  SUMMARY: {len(results)} successful rolls")
            print(f"  Results: {results}")
            print(f"  Average: {sum(results) / len(results):.1f}")
            print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
