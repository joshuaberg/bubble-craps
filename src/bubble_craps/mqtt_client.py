import json
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

from bubble_craps.config import MqttConfig

logger = logging.getLogger(__name__)


class MqttClient:
    """MQTT client for publishing results/status and receiving commands."""

    def __init__(self, config: MqttConfig, on_command=None):
        self.config = config
        self._on_command = on_command
        self._connected = False

        # Offline roll queue
        self._queue: deque[dict] = deque(maxlen=config.offline_queue.max_size)
        self._queue_lock = threading.Lock()
        self._persist_path = Path(config.offline_queue.persist_file)
        self._load_queue()

        # Set up MQTT client
        self._client = mqtt.Client(
            client_id=config.client_id,
            clean_session=True,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

        # Configure LWT — broker publishes this automatically on unexpected disconnect
        lwt_payload = json.dumps({
            "state": "OFFLINE",
            "device_id": config.client_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        self._client.will_set(
            topic=config.topic_status,
            payload=lwt_payload,
            qos=1,
            retain=True,
        )

    def connect(self) -> None:
        """Connect to the MQTT broker."""
        logger.info("Connecting to MQTT broker at %s:%d", self.config.broker, self.config.port)
        self._client.connect_async(self.config.broker, self.config.port, self.config.keepalive)
        self._client.loop_start()

    def disconnect(self) -> None:
        """Disconnect from the MQTT broker, publishing OFFLINE status first."""
        self._persist_queue()
        # Publish retained OFFLINE status before disconnecting so subscribers
        # see OFFLINE on graceful shutdown, not just unexpected disconnects (LWT).
        offline_payload = json.dumps({
            "state": "OFFLINE",
            "device_id": self.config.client_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        self._client.publish(self.config.topic_status, offline_payload, qos=1, retain=True)
        self._client.disconnect()
        self._client.loop_stop()
        logger.info("Disconnected from MQTT broker")

    def publish_result(self, result: dict) -> None:
        """Publish a roll result. Queues locally if disconnected."""
        if self._connected:
            self._flush_queue()
            self._publish(self.config.topic_result, result, qos=1, retain=False)
        else:
            logger.warning("MQTT disconnected, queuing roll result")
            with self._queue_lock:
                self._queue.append(result)
            self._persist_queue()

    def publish_image(self, image_bytes: bytes) -> None:
        """Publish a roll image as raw JPEG bytes. Best-effort — not queued offline."""
        if self._connected:
            self._client.publish(self.config.topic_image, image_bytes, qos=1, retain=False)
            logger.debug("Published image (%d bytes) to %s", len(image_bytes), self.config.topic_image)

    def publish_status(self, status: dict) -> None:
        """Publish machine status (retained)."""
        if self._connected:
            self._publish(self.config.topic_status, status, qos=1, retain=True)

    def _publish(self, topic: str, payload: dict, qos: int = 1, retain: bool = False) -> None:
        message = json.dumps(payload)
        self._client.publish(topic, message, qos=qos, retain=retain)
        logger.debug("Published to %s: %s", topic, message)

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            logger.info("Connected to MQTT broker")
            self._connected = True
            client.subscribe(self.config.topic_command, qos=1)
            self._flush_queue()
        else:
            logger.error("MQTT connection failed with code %d", rc)

    def _on_disconnect(self, client, userdata, rc) -> None:
        self._connected = False
        if rc != 0:
            logger.warning("Unexpected MQTT disconnect (rc=%d), will auto-reconnect", rc)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload.decode())
            logger.info("Received command: %s", payload)
            if self._on_command:
                self._on_command(payload)
        except json.JSONDecodeError:
            logger.error("Invalid JSON on command topic: %s", msg.payload)

    def _flush_queue(self) -> None:
        """Publish all queued roll results in order."""
        with self._queue_lock:
            while self._queue:
                result = self._queue.popleft()
                self._publish(self.config.topic_result, result, qos=1, retain=False)
                logger.info("Flushed queued roll: %s", result.get("roll_id"))
        self._persist_queue()

    def _persist_queue(self) -> None:
        """Save the current queue to disk."""
        with self._queue_lock:
            data = list(self._queue)
        try:
            self._persist_path.write_text(json.dumps(data))
        except OSError:
            logger.error("Failed to persist offline queue to %s", self._persist_path)

    def _load_queue(self) -> None:
        """Load any previously persisted queue from disk."""
        if not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text())
            for item in data:
                self._queue.append(item)
            logger.info("Loaded %d queued rolls from disk", len(data))
        except (json.JSONDecodeError, OSError):
            logger.error("Failed to load offline queue from %s", self._persist_path)
