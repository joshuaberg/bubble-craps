"""SQL Bridge — subscribes to MQTT roll results and images, persists to Postgres and disk."""

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import psycopg2
from psycopg2.extras import Json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# -- Configuration (from environment variables) --------------------------------

MQTT_BROKER = os.environ.get("MQTT_BROKER", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

DB_HOST = os.environ.get("DB_HOST", "postgres")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "bubble_craps")
DB_USER = os.environ.get("DB_USER", "bubble_craps")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "bubble_craps")

IMAGE_DIR_OK = Path(os.environ.get("IMAGE_DIR_OK", "/images/ok"))
IMAGE_DIR_FAILED = Path(os.environ.get("IMAGE_DIR_FAILED", "/images/failed"))
IMAGE_MAX_COUNT = int(os.environ.get("IMAGE_MAX_COUNT", "300"))

# -- Database ------------------------------------------------------------------

def connect_db_startup():
    """Connect to Postgres with retries at startup (waits for container to be ready).
    Only called once before the MQTT loop starts. Safe to block here."""
    for attempt in range(30):
        try:
            conn = psycopg2.connect(
                host=DB_HOST, port=DB_PORT,
                dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
            )
            conn.autocommit = True
            logger.info("Connected to Postgres")
            return conn
        except psycopg2.OperationalError:
            logger.warning("Postgres not ready, retrying in 2s (attempt %d/30)", attempt + 1)
            time.sleep(2)
    logger.error("Could not connect to Postgres after 30 attempts")
    sys.exit(1)


def reconnect_db():
    """Reconnect to Postgres during operation. Short retry (3 attempts, ~6s)
    to avoid blocking the MQTT thread for too long."""
    global db_conn
    # Close the old connection
    if db_conn is not None:
        try:
            db_conn.close()
        except Exception:
            pass

    for attempt in range(3):
        try:
            conn = psycopg2.connect(
                host=DB_HOST, port=DB_PORT,
                dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD,
            )
            conn.autocommit = True
            logger.info("Reconnected to Postgres")
            db_conn = conn
            return True
        except psycopg2.OperationalError:
            logger.warning("Postgres reconnect failed (attempt %d/3)", attempt + 1)
            time.sleep(2)

    logger.error("Could not reconnect to Postgres after 3 attempts")
    db_conn = None
    return False


def with_db(fn):
    """Run fn(conn). On connection error, reconnect once and retry.
    Returns the result of fn, or None if both attempts fail."""
    global db_conn
    if db_conn is None:
        if not reconnect_db():
            return None
    try:
        return fn(db_conn)
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        logger.warning("DB connection lost, reconnecting")
        if reconnect_db():
            try:
                return fn(db_conn)
            except psycopg2.Error:
                logger.exception("DB operation failed after reconnect")
        return None


def insert_roll(conn, result):
    """Insert a roll result into the rolls table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO rolls (roll_id, device_id, die1, die2, total,
                               roll_status, roll_status_reason, attempts,
                               motor_temp_c, timestamp)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (roll_id) DO NOTHING
            """,
            (
                result["roll_id"],
                result["device_id"],
                result.get("die1"),
                result.get("die2"),
                result.get("total"),
                result["roll_status"],
                result.get("roll_status_reason"),
                Json(result.get("attempts")),
                result.get("motor_temp_c"),
                result["timestamp"],
            ),
        )
    logger.info("Inserted roll %s (status=%s)", result["roll_id"], result["roll_status"])


def get_roll_status(conn, roll_id):
    """Look up a roll's status from the database. Returns None if not found."""
    with conn.cursor() as cur:
        cur.execute("SELECT roll_status FROM rolls WHERE roll_id = %s", (roll_id,))
        row = cur.fetchone()
        return row[0] if row else None


def update_image_path(conn, roll_id, image_path):
    """Set the image_path for an existing roll. Returns True if the row existed."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE rolls SET image_path = %s WHERE roll_id = %s",
            (str(image_path), roll_id),
        )
        return cur.rowcount > 0


# -- Image storage -------------------------------------------------------------

def save_image(roll_id, image_bytes, roll_status):
    """Save image to the appropriate directory based on roll status.
    Returns (saved_path, list_of_pruned_roll_ids)."""
    if roll_status == "ok":
        directory = IMAGE_DIR_OK
    else:
        directory = IMAGE_DIR_FAILED

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{roll_id}.jpg"
    path.write_bytes(image_bytes)
    logger.info("Saved image %s (%d bytes)", path, len(image_bytes))

    pruned = prune_images(directory)
    return path, pruned


def prune_images(directory):
    """Delete oldest images if the directory exceeds IMAGE_MAX_COUNT.
    Returns list of roll_ids whose images were deleted."""
    images = sorted(directory.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    to_delete = len(images) - IMAGE_MAX_COUNT
    if to_delete <= 0:
        return []

    pruned_ids = []
    for path in images[:to_delete]:
        pruned_ids.append(path.stem)
        path.unlink()
        logger.info("Pruned image %s", path)
    return pruned_ids


# -- MQTT callbacks ------------------------------------------------------------

db_conn = None


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("Connected to MQTT broker")
        client.subscribe("bubble-craps/+/roll/result", qos=1)
        client.subscribe("bubble-craps/+/roll/image/+", qos=1)
        logger.info("Subscribed to roll result and image topics")
    else:
        logger.error("MQTT connection failed with code %d", rc)


def on_message(client, userdata, msg):
    try:
        topic_parts = msg.topic.split("/")
        # bubble-craps/{device_id}/roll/result
        # bubble-craps/{device_id}/roll/image/{roll_id}

        if len(topic_parts) == 4 and topic_parts[3] == "result":
            handle_result(msg)
        elif len(topic_parts) == 5 and topic_parts[3] == "image":
            handle_image(msg, topic_parts[4])
    except Exception:
        logger.exception("Unhandled error processing message on %s", msg.topic)


REQUIRED_RESULT_FIELDS = ("roll_id", "device_id", "roll_status", "timestamp")


def handle_result(msg):
    """Handle a roll result message."""
    global db_conn
    try:
        result = json.loads(msg.payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.error("Invalid roll result payload on %s", msg.topic)
        return

    if not isinstance(result, dict):
        logger.error("Roll result is not a JSON object on %s", msg.topic)
        return

    missing = [f for f in REQUIRED_RESULT_FIELDS if f not in result]
    if missing:
        logger.error("Roll result missing required fields %s on %s", missing, msg.topic)
        return

    roll_id = result["roll_id"]

    if with_db(lambda conn: insert_roll(conn, result)) is None:
        logger.error("Failed to insert roll %s", roll_id)


def handle_image(msg, roll_id):
    """Handle a roll image message."""
    global db_conn
    image_bytes = msg.payload
    if not image_bytes:
        logger.warning("Empty image payload for roll %s", roll_id)
        return

    # Look up status from the database — the source of truth
    roll_status = with_db(lambda conn: get_roll_status(conn, roll_id))

    if roll_status is None:
        # Result hasn't arrived yet, insert failed, or DB is down
        logger.warning("No roll record for image %s, saving to failed/", roll_id)
        roll_status = "unknown"

    path, pruned = save_image(roll_id, image_bytes, roll_status)

    # Update image_path in DB (may match zero rows if result hasn't arrived)
    updated = with_db(lambda conn: update_image_path(conn, roll_id, path))
    if updated is None:
        logger.error("Failed to update image_path for roll %s", roll_id)
    elif not updated:
        logger.warning("No roll row to update image_path for %s", roll_id)

    # Null out pruned images in DB
    if pruned:
        def null_pruned(conn):
            with conn.cursor() as cur:
                for pruned_roll_id in pruned:
                    cur.execute(
                        "UPDATE rolls SET image_path = NULL WHERE roll_id = %s",
                        (pruned_roll_id,),
                    )
            logger.info("Nulled image_path for %d pruned rolls", len(pruned))

        if with_db(null_pruned) is None:
            logger.error("Failed to null image_path for pruned rolls")


# -- Main ----------------------------------------------------------------------

def main():
    global db_conn

    logger.info("Starting SQL Bridge")

    # Connect to Postgres (blocks until ready, up to 60s)
    db_conn = connect_db_startup()

    # Set up MQTT
    client = mqtt.Client(client_id="sql-bridge", clean_session=True)
    client.on_connect = on_connect
    client.on_message = on_message

    logger.info("Connecting to MQTT broker at %s:%d", MQTT_BROKER, MQTT_PORT)
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)

    # Graceful shutdown
    def shutdown(signum, frame):
        logger.info("Shutting down")
        client.disconnect()
        client.loop_stop()
        if db_conn:
            db_conn.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Block forever — retry_first_connection handles Mosquitto not being ready yet
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
