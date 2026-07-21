import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.yaml"


@dataclass
class TriggerConfig:
    mode: str = "timer"
    timer_interval_sec: int = 120


@dataclass
class MotorConfig:
    can_channel: str = "can0"
    can_id: int = 0x141
    roll_rpm_min: float = 300.0
    roll_rpm_max: float = 800.0
    roll_duration_min_sec: float = 3.0
    roll_duration_max_sec: float = 8.0
    park_position: float = 0.0
    park_speed_limit: float = 100.0
    park_timeout_sec: float = 5.0
    park_poll_interval_sec: float = 0.1
    settling_time_sec: float = 2.0


@dataclass
class CameraConfig:
    resolution: tuple[int, int] = (1920, 1080)
    exposure_mode: str = "fixed"
    white_balance: str = "fixed"


@dataclass
class RingLightConfig:
    gpio_pin: int = 18
    idle_brightness: float = 0.3


@dataclass
class MqttTopicsConfig:
    result: str = "bubble-craps/roll/result"
    status: str = "bubble-craps/status"
    error: str = "bubble-craps/roll/error"
    command: str = "bubble-craps/command"


@dataclass
class OfflineQueueConfig:
    max_size: int = 100
    persist_file: str = "pending_rolls.json"


@dataclass
class MqttConfig:
    broker: str = "192.168.1.100"
    port: int = 1883
    client_id: str = "bubble-craps-01"
    keepalive: int = 60
    topics: MqttTopicsConfig = field(default_factory=MqttTopicsConfig)
    offline_queue: OfflineQueueConfig = field(default_factory=OfflineQueueConfig)


@dataclass
class DetectionConfig:
    max_retries: int = 2
    calibration_file: str = "calibration.json"
    position_tolerance_px: int = 20


@dataclass
class AppConfig:
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    motor: MotorConfig = field(default_factory=MotorConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    ring_light: RingLightConfig = field(default_factory=RingLightConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    detection: DetectionConfig = field(default_factory=DetectionConfig)


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration from a YAML file and return an AppConfig instance."""
    config_path = path or DEFAULT_CONFIG_PATH

    if not config_path.exists():
        logger.warning("Config file not found at %s, using defaults", config_path)
        return AppConfig()

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    trigger = TriggerConfig(**raw.get("trigger", {}))
    motor = MotorConfig(**raw.get("motor", {}))

    camera_raw = raw.get("camera", {})
    if "resolution" in camera_raw:
        camera_raw["resolution"] = tuple(camera_raw["resolution"])
    camera = CameraConfig(**camera_raw)

    ring_light = RingLightConfig(**raw.get("ring_light", {}))

    mqtt_raw = raw.get("mqtt", {})
    topics = MqttTopicsConfig(**mqtt_raw.pop("topics", {}))
    offline_queue = OfflineQueueConfig(**mqtt_raw.pop("offline_queue", {}))
    mqtt = MqttConfig(**mqtt_raw, topics=topics, offline_queue=offline_queue)

    detection = DetectionConfig(**raw.get("detection", {}))

    return AppConfig(
        trigger=trigger,
        motor=motor,
        camera=camera,
        ring_light=ring_light,
        mqtt=mqtt,
        detection=detection,
    )
