#!/usr/bin/env python3
"""Test script for the NeoPixel ring light.

Run on the Pi with sudo (required for NeoPixel):
  sudo python3 scripts/test_ring_light.py
  sudo python3 scripts/test_ring_light.py --num-leds 16
  sudo python3 scripts/test_ring_light.py --gpio 13 --num-leds 24

Tests:
  1. All white (verify wiring)
  2. Red / Green / Blue (verify color order)
  3. Brightness sweep
  4. Chase animation
  5. All off
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bubble_craps.config import load_config


def separator(title: str) -> None:
    print(f"\n{'='*50}")
    print(f"  {title}")
    print(f"{'='*50}\n")


def main():
    config = load_config()

    parser = argparse.ArgumentParser(description="Test NeoPixel ring light")
    parser.add_argument("--gpio", type=int, default=config.ring_light.gpio_pin,
                        help=f"GPIO pin (default: {config.ring_light.gpio_pin})")
    parser.add_argument("--num-leds", type=int, default=24,
                        help="Number of LEDs on the ring (default: 24)")
    parser.add_argument("--brightness", type=float, default=0.3,
                        help="Max brightness 0.0-1.0 (default: 0.3)")
    args = parser.parse_args()

    try:
        from rpi_ws281x import PixelStrip, Color
    except ImportError:
        print("ERROR: rpi_ws281x not installed. Run:")
        print("  sudo pip install rpi_ws281x")
        sys.exit(1)

    print(f"NeoPixel Ring Light Test")
    print(f"  GPIO pin: {args.gpio}")
    print(f"  LEDs: {args.num_leds}")
    print(f"  Brightness: {args.brightness}")

    strip = PixelStrip(
        args.num_leds,
        args.gpio,
        freq_hz=800000,
        dma=10,
        invert=False,
        brightness=int(args.brightness * 255),
        channel=1,  # PWM channel 1 for GPIO 13
    )

    try:
        strip.begin()
    except RuntimeError as e:
        print(f"\nERROR: {e}")
        print("NeoPixel requires root. Run with: sudo python3 scripts/test_ring_light.py")
        sys.exit(1)

    def set_all(color):
        for i in range(strip.numPixels()):
            strip.setPixelColor(i, color)
        strip.show()

    def clear():
        set_all(Color(0, 0, 0))

    try:
        # Test 1: All white
        separator("TEST 1: All White")
        print("All LEDs white — verify they all light up")
        set_all(Color(255, 255, 255))
        time.sleep(3)

        # Test 2: Colors
        separator("TEST 2: Red")
        print("All LEDs red")
        set_all(Color(255, 0, 0))
        time.sleep(2)

        separator("TEST 2: Green")
        print("All LEDs green")
        set_all(Color(0, 255, 0))
        time.sleep(2)

        separator("TEST 2: Blue")
        print("All LEDs blue")
        set_all(Color(0, 0, 255))
        time.sleep(2)

        # Test 3: Brightness sweep
        separator("TEST 3: Brightness Sweep")
        print("Sweeping brightness up and down (white)")
        for b in list(range(0, 256, 5)) + list(range(255, -1, -5)):
            strip.setBrightness(b)
            set_all(Color(255, 255, 255))
            time.sleep(0.02)
        strip.setBrightness(int(args.brightness * 255))

        # Test 4: Chase
        separator("TEST 4: Chase Animation")
        print("Green chase — 3 loops")
        for _ in range(3):
            for i in range(strip.numPixels()):
                clear()
                strip.setPixelColor(i, Color(0, 255, 0))
                strip.setPixelColor((i + 1) % strip.numPixels(), Color(0, 128, 0))
                strip.show()
                time.sleep(0.05)

        # Test 5: Off
        separator("TEST 5: All Off")
        clear()
        print("LEDs off")

        print("\nAll tests complete!")

    except KeyboardInterrupt:
        print("\nInterrupted — turning off LEDs")
        clear()


if __name__ == "__main__":
    main()
