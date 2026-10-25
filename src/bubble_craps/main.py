import logging
import signal
import sys
import threading
from pathlib import Path

from bubble_craps.camera import PiCamera
from bubble_craps.config import load_config, save_trigger_config
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

    # State machine — only status callback (supervisor handles result/image)
    state_machine = StateMachine(
        config, motor, ring_light, camera, detector,
        on_status=mqtt_client.publish_status,
    )

    # Roll supervisor — owns timer, retry policy, command queue
    supervisor = RollSupervisor(config, state_machine, mqtt_client)

    def handle_command(payload: dict) -> None:
        action = payload.get("action")
        if action == "roll":
            supervisor.enqueue("roll")
        elif action == "set_mode":
            mode = payload.get("mode")
            if mode in ("timer", "remote"):
                config.trigger.mode = mode
                if mode == "timer" and "interval_sec" in payload:
                    config.trigger.timer_interval_sec = payload["interval_sec"]
                logger.info("Trigger mode changed to: %s", mode)
                save_trigger_config(config.trigger, config_path)
                mqtt_client.publish_status(state_machine._build_status())
                supervisor.reset_timer()
        elif action == "status":
            mqtt_client.publish_status(state_machine._build_status())
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
