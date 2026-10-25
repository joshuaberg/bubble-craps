import logging
import signal
import sys
import threading
from pathlib import Path

from bubble_craps.camera import PiCamera
from bubble_craps.config import load_config
from bubble_craps.detector import DiceDetector
from bubble_craps.motor import CANMotorController
from bubble_craps.mqtt_client import MqttClient
from bubble_craps.ring_light import NeoPixelRingLightController
from bubble_craps.roll_supervisor import RollSupervisor
from bubble_craps.state_machine import StateMachine

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

    # MQTT client — command handler wired after supervisor is created
    mqtt_client = MqttClient(config.mqtt, on_command=lambda p: handle_command(p))

    # Roll supervisor — owns status, timer, retry policy, command queue
    supervisor = RollSupervisor(config, state_machine=None, mqtt_client=mqtt_client,
                                config_path=config_path)

    # State machine — transition callback wired to supervisor
    state_machine = StateMachine(
        config, motor, ring_light, camera, detector,
        on_transition=supervisor._on_transition,
    )
    supervisor._sm = state_machine

    # All commands go through the supervisor queue
    def handle_command(payload: dict) -> None:
        action = payload.get("action")
        if action == "roll":
            supervisor.enqueue("roll")
        elif action == "set_mode":
            supervisor.enqueue("set_mode",
                               mode=payload.get("mode"),
                               interval_sec=payload.get("interval_sec"))
        elif action == "status":
            supervisor._publish_status()
        elif action == "clear_error":
            supervisor.enqueue("clear_error")
        else:
            logger.warning("Unknown command action: %s", action)

    # Connect and start
    mqtt_client.connect()
    supervisor.start()

    logger.info("Bubble Craps Machine started (trigger_mode=%s)", config.trigger.mode)

    # Graceful shutdown — just wait for signal
    shutdown_event = threading.Event()

    def signal_handler(sig, frame):
        logger.info("Shutdown signal received")
        shutdown_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        shutdown_event.wait()
    finally:
        logger.info("Shutting down...")
        supervisor.stop()
        motor.shutdown()
        ring_light.off()
        camera.close()
        mqtt_client.disconnect()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
