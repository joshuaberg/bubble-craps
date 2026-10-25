import logging
import struct
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class MotorStatus:
    """Parsed status from a CAN response frame."""
    temp_c: int
    torque_milli_nm: int
    speed_dps: int
    angle_deg: float


class CANMotorController:
    """Motor controller for LK motors via Waveshare RS485 CAN HAT (MCP2515).

    Uses python-can with the socketcan interface. The CAN HAT must be
    configured at the OS level before use (see README / test script).
    """

    DEFAULT_CAN_ID = 0x141

    def __init__(self, channel: str = "can0", can_id: int = DEFAULT_CAN_ID,
                 torque_scale_factor: int = 645):
        import can

        self._can = can
        self._can_id = can_id
        self._torque_scale_factor = torque_scale_factor  # "Taylor factor" — changes per motor model
        self._running = False
        self._target_position: float | None = None

        logger.info("Opening CAN bus on %s", channel)
        self._bus = can.Bus(interface="socketcan", channel=channel)

        if not self._turn_on():
            logger.error("Failed to turn on motor")

    def spin(self, rpm: float = 550.0) -> None:
        cdps = int(rpm * 360 / 60 * 100)
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

    def go_to_abs_position(self, position: float, speed_limit: float = 100.0) -> None:
        self._go_to_target(position, speed_limit)

    def go_to_angle(self, angle: float, speed_limit: float = 100.0) -> None:
        current = self.get_position()
        if current is None:
            logger.error("Cannot go_to_angle: failed to read current position")
            return

        target = self._nearest_target(current, angle)
        logger.info(
            "go_to_angle: current=%.2f, target_angle=%.2f, absolute_target=%.2f",
            current, angle, target,
        )
        self._go_to_target(target, speed_limit)

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
        return raw * 0.01

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

    def get_status(self) -> MotorStatus | None:
        """Read motor state: temperature, torque, speed, and encoder position."""
        data = [0x9C, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        if not self._send(data):
            return None

        message = self._bus.recv(timeout=2)
        if message is None:
            logger.warning("No CAN response for status read")
            return None

        d = message.data
        temp = struct.unpack_from("b", d, 1)[0]
        torque_raw = struct.unpack_from("<h", d, 2)[0]
        speed_dps = struct.unpack_from("<h", d, 4)[0]
        encoder_14bit = struct.unpack_from("<H", d, 6)[0] >> 2
        return MotorStatus(
            temp_c=temp,
            torque_milli_nm=torque_raw * self._torque_scale_factor // 1000,
            speed_dps=speed_dps,
            angle_deg=round(encoder_14bit * 360 / 16383, 2),
        )

    def is_running(self) -> bool:
        return self._running

    def shutdown(self) -> None:
        self.stop()
        self._turn_off()
        self._bus.shutdown()
        logger.info("CAN bus shut down")

    def _go_to_target(self, position: float, speed_limit: float) -> None:
        """Send the absolute position command to the motor."""
        speed_raw = int(speed_limit * 360 / 60)
        angle_raw = int(position * 100)
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

    @staticmethod
    def _nearest_target(current: float, angle: float) -> float:
        """Find the nearest absolute position that matches the desired angle."""
        base = (current // 360) * 360
        candidate_a = base + angle
        candidate_b = candidate_a + 360
        candidate_c = candidate_a - 360
        candidates = [candidate_a, candidate_b, candidate_c]
        return min(candidates, key=lambda c: abs(c - current))

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
