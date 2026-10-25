import logging
import signal
import sys
import threading
import time
from pathlib import Path

from bubble_craps.camera import PiCamera
from bubble_craps.config import load_config, save_trigger_config
from bubble_craps.detector import DiceDetector
from bubble_craps.motor import CANMotorController
from bubble_craps.mqtt_client import MqttClient
from bubble_craps.ring_light import NeoPixelRingLightController
from bubble_craps.state_machine import State, StateMachine

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> None:
    setup_logging()

    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    config = load_config(config_path)
    logger.info("Configuration loaded")

    # Initialize hardware
    motor = CANMotorController(
        channel=config.motor.can_channel,
        can_id=config.motor.can_id,
        torque_scale_factor=config.motor.torque_scale_factor,
    )
    ring_light = NeoPixelRingLightController(
        gpio_pin=config.ring_light.gpio_pin,
        num_leds=config.ring_light.num_leds,
        brightness=config.ring_light.idle_brightness,
    )

    camera = PiCamera(config.camera)

    detector = DiceDetector(config.detection)

    # MQTT client and command handler
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
                save_trigger_config(config.trigger, config_path)
                if state_machine:
                    mqtt_client.publish_status(state_machine._build_status())
        elif action == "status":
            if state_machine:
                mqtt_client.publish_status(state_machine._build_status())
        elif action == "clear_error":
            if state_machine:
                state_machine.clear_error()
        else:
            logger.warning("Unknown command action: %s", action)

    mqtt_client = MqttClient(config.mqtt, on_command=handle_command)

    # Wire state machine events to MQTT
    state_machine = StateMachine(
        config, motor, ring_light, camera, detector,
        on_status=mqtt_client.publish_status,
        on_result=mqtt_client.publish_result,
        on_image=mqtt_client.publish_image,
    )

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

    _HEARTBEAT_INTERVAL_SEC = 300  # 5 minutes
    _last_heartbeat = time.monotonic()

    try:
        while not shutdown_event.is_set():
            if config.trigger.mode == "timer" and state_machine.state == State.IDLE:
                state_machine.trigger_roll()
                shutdown_event.wait(timeout=config.trigger.timer_interval_sec)
            else:
                shutdown_event.wait(timeout=1.0)

            # Heartbeat — publish status every 5 minutes while in IDLE
            if state_machine.state == State.IDLE:
                now = time.monotonic()
                if now - _last_heartbeat >= _HEARTBEAT_INTERVAL_SEC:
                    mqtt_client.publish_status(state_machine._build_status())
                    _last_heartbeat = now
    finally:
        logger.info("Shutting down...")
        ring_light.off()
        camera.close()
        mqtt_client.disconnect()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
