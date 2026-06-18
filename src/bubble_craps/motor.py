import logging
import time
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class MotorController(ABC):
    """Abstract interface for motor control."""

    @abstractmethod
    def start(self, rpm: float = 40.0) -> None:
        """Start the motor spinning at the given speed in RPM."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the motor immediately."""

    @abstractmethod
    def go_to_position(self, position: float, speed_limit: float = 44.0) -> None:
        """Command the motor to move to an absolute position (degrees).
        Non-blocking — call is_at_position() to poll for completion."""

    @abstractmethod
    def get_position(self) -> float | None:
        """Read the current motor position in degrees. Returns None on error."""

    @abstractmethod
    def is_at_position(self) -> bool:
        """Poll whether the motor has reached the target position."""

    @abstractmethod
    def is_running(self) -> bool:
        """Check if the motor is currently running."""

    @abstractmethod
    def shutdown(self) -> None:
        """Release hardware resources."""


class CANMotorController(MotorController):
    """Motor controller for LK motors via Waveshare RS485 CAN HAT (MCP2515).

    Uses python-can with the socketcan interface. The CAN HAT must be
    configured at the OS level before use (see README / test script).
    """

    DEFAULT_CAN_ID = 0x141

    def __init__(self, channel: str = "can0", can_id: int = DEFAULT_CAN_ID):
        import can

        self._can = can
        self._can_id = can_id
        self._running = False
        self._target_position: float | None = None

        logger.info("Opening CAN bus on %s", channel)
        self._bus = can.Bus(interface="socketcan", channel=channel)

        if not self._turn_on():
            logger.error("Failed to turn on motor")

    def start(self, rpm: float = 40.0) -> None:
        cdps = int(rpm * 10 * 360 / 60 * 100)
        data = [
            0xA2, 0x00, 0x00, 0x00,
            cdps & 0xFF,
            (cdps >> 8) & 0xFF,
            (cdps >> 16) & 0xFF,
            (cdps >> 24) & 0xFF,
        ]
        if self._send(data):
            self._read_response()
            self._running = True
            logger.info("Motor started at %.1f RPM", rpm)

    def stop(self) -> None:
        data = [0x81, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        if self._send(data):
            self._read_response()
            self._running = False
            logger.info("Motor stopped")

    def go_to_position(self, position: float, speed_limit: float = 44.0) -> None:
        speed_raw = int(speed_limit * 360 * 10 / 60)
        angle_raw = int(position * 10 * 100)
        data = [
            0xA4, 0x00,
            speed_raw & 0xFF,
            (speed_raw >> 8) & 0xFF,
            angle_raw & 0xFF,
            (angle_raw >> 8) & 0xFF,
            (angle_raw >> 16) & 0xFF,
            (angle_raw >> 24) & 0xFF,
        ]
        if self._send(data):
            self._read_response()
            self._target_position = position
            self._running = True
            logger.info("Motor moving to %.2f deg (speed limit %.1f RPM)", position, speed_limit)

    def get_position(self) -> float | None:
        data = [0x92, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        if not self._send(data):
            return None

        message = self._read_response()
        if message is None:
            return None

        d = message.data
        raw = (
            (d[7] << 48) | (d[6] << 40) | (d[5] << 32)
            | (d[4] << 24) | (d[3] << 16) | (d[2] << 8) | d[1]
        )
        if raw >= 0x80000000000000:
            raw -= 0x100000000000000
        return raw * 0.01 / 10

    def is_at_position(self) -> bool:
        if self._target_position is None:
            return True

        current = self.get_position()
        if current is None:
            return False

        at_target = abs(current - self._target_position) < 1.0
        if at_target:
            self._running = False
        return at_target

    def is_running(self) -> bool:
        return self._running

    def shutdown(self) -> None:
        self.stop()
        self._turn_off()
        self._bus.shutdown()
        logger.info("CAN bus shut down")

    def _turn_on(self) -> bool:
        data = [0x88, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        if self._send(data):
            if self._read_response() is not None:
                logger.info("Motor turned ON")
                return True
        return False

    def _turn_off(self) -> bool:
        data = [0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        if self._send(data):
            if self._read_response() is not None:
                logger.info("Motor turned OFF")
                return True
        return False

    def _send(self, data: list[int]) -> bool:
        msg = self._can.Message(
            arbitration_id=self._can_id, data=data, is_extended_id=False
        )
        try:
            self._bus.send(msg, timeout=2)
            return True
        except self._can.CanError:
            logger.error("Failed to send CAN message")
            return False

    def _read_response(self):
        message = self._bus.recv(timeout=2)
        if message is None:
            logger.warning("No CAN response received")
        return message


class MockMotorController(MotorController):
    """Mock motor controller for development and testing."""

    def __init__(self):
        self._running = False
        self._at_position = True

    def start(self, rpm: float = 40.0) -> None:
        logger.info("MockMotor: start (rpm=%.1f)", rpm)
        self._running = True
        self._at_position = False

    def stop(self) -> None:
        logger.info("MockMotor: stop")
        self._running = False

    def go_to_position(self, position: float, speed_limit: float = 44.0) -> None:
        logger.info("MockMotor: go_to_position (position=%.2f)", position)
        self._running = False
        self._at_position = True

    def get_position(self) -> float | None:
        return 0.0

    def is_at_position(self) -> bool:
        return self._at_position

    def is_running(self) -> bool:
        return self._running

    def shutdown(self) -> None:
        logger.info("MockMotor: shutdown")
