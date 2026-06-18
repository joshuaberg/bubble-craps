import logging
import signal
import sys
import threading
import time
from pathlib import Path

from bubble_craps.camera import MockCamera, PiCamera
from bubble_craps.config import load_config
from bubble_craps.detector import DiceDetector
from bubble_craps.motor import CANMotorController, MockMotorController
from bubble_craps.mqtt_client import MqttClient
from bubble_craps.ring_light import MockRingLightController
from bubble_craps.state_machine import State, StateMachine

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def is_raspberry_pi() -> bool:
    """Check if we're running on a Raspberry Pi."""
    try:
        with open("/proc/device-tree/model") as f:
            return "raspberry pi" in f.read().lower()
    except FileNotFoundError:
        return False


def main() -> None:
    setup_logging()

    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    config = load_config(config_path)
    logger.info("Configuration loaded")

    # Initialize hardware — use real implementations on Pi, mocks otherwise
    on_pi = is_raspberry_pi()

    if on_pi:
        motor = CANMotorController(
            channel=config.motor.can_channel,
            can_id=config.motor.can_id,
        )
    else:
        motor = MockMotorController()

    ring_light = MockRingLightController()  # Mock until GPIO wiring is done

    if on_pi:
        camera = PiCamera(config.camera)
    else:
        camera = MockCamera(config.camera)

    detector = DiceDetector(config.detection)

    # State machine and MQTT client reference each other, so we wire them up in steps
    state_machine = None

    def handle_command(payload: dict) -> None:
        action = payload.get("action")
        if action == "roll":
            if state_machine:
                threading.Thread(target=state_machine.trigger_roll, daemon=True).start()
        elif action == "set_mode":
            mode = payload.get("mode")
            if mode in ("timer", "remote"):
                config.trigger.mode = mode
                if mode == "timer" and "interval_sec" in payload:
                    config.trigger.timer_interval_sec = payload["interval_sec"]
                logger.info("Trigger mode changed to: %s", mode)
        elif action == "status":
            if state_machine:
                state_machine.mqtt_client.publish_status(state_machine._build_status())
        elif action == "clear_error":
            if state_machine:
                state_machine.clear_error()
        else:
            logger.warning("Unknown command action: %s", action)

    mqtt_client = MqttClient(config.mqtt, on_command=handle_command)
    state_machine = StateMachine(config, motor, ring_light, camera, detector, mqtt_client)

    # Connect to MQTT
    mqtt_client.connect()

    # Graceful shutdown
    shutdown_event = threading.Event()

    def signal_handler(sig, frame):
        logger.info("Shutdown signal received")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("Bubble Craps Machine started (trigger_mode=%s)", config.trigger.mode)

    try:
        while not shutdown_event.is_set():
            if config.trigger.mode == "timer" and state_machine.state == State.IDLE:
                state_machine.trigger_roll()
                shutdown_event.wait(timeout=config.trigger.timer_interval_sec)
            else:
                # In remote mode or non-IDLE state, just wait
                shutdown_event.wait(timeout=1.0)
    finally:
        logger.info("Shutting down...")
        ring_light.off()
        camera.close()
        mqtt_client.disconnect()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
