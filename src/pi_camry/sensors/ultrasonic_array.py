"""
pi_camry/sensors/ultrasonic_array.py
────────────────────────────────────
8× ultrasonic parking sensor array (front + rear).

Hardware:
- HC-SR04 × 8 (or US-100 / JSN-SR04T for waterproof)
- Each sensor: 1 trig pin (output) + 1 echo pin (input)
- Level shifter: HC-SR04 is 5V logic; Pi GPIO is 3.3V
- Optional: MCP23017 I2C GPIO expander if Pi pins run out
- Piezo buzzer or speaker for audio alerts

Placement:
- Front: 4 sensors — 2 corners + 2 mid-bumper
- Rear: 4 sensors — 2 corners + 2 mid-bumper
- Height: 45-60cm from ground
- Angle: 0-15° downward for ground clearance detection

Timing:
- Speed of sound: ~343 m/s at 20°C
- Round-trip to 2m: ~12ms
- 8 sensors × 30ms = 240ms cycle → ~4 Hz update
- Stagger triggers to avoid crosstalk

Audio alerts:
- >100cm: silent
- 60-100cm: slow beep (1 Hz)
- 30-60cm: medium beep (2 Hz)
- <30cm: fast beep (4 Hz) + continuous tone at <15cm
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pi_camry.core.config import UltrasonicConfig

logger = logging.getLogger("camry.sensors.ultrasonic")


class ParkingZone(IntEnum):
    """Parking zone classification."""
    CLEAR = 0       # > 100 cm
    FAR = 1         # 60-100 cm
    MID = 2         # 30-60 cm
    NEAR = 3        # 15-30 cm
    DANGER = 4      # < 15 cm


@dataclass(frozen=True)
class UltrasonicReading:
    """Single sensor reading."""
    sensor_id: int
    position: str   # "FL", "FR", "FC", "FRC", "RL", "RR", "RC", "RRC"
    distance_cm: float
    zone: ParkingZone
    valid: bool       # False if timeout (no echo)
    timestamp: float


class UltrasonicArray:
    """8-sensor parking array with audio alerts."""

    SPEED_OF_SOUND_CM_US = 0.0343  # cm per microsecond at 20°C
    MAX_PULSE_DURATION = 0.025      # 25ms timeout = ~4m max
    TRIGGER_PULSE_US = 10
    SETTLE_TIME = 0.06              # 60ms between sensors to avoid crosstalk

    # Alert tone frequencies
    TONE_FREQ = 2000  # Hz

    def __init__(self, cfg: "UltrasonicConfig" | None = None) -> None:
        self.cfg = cfg
        self._running = False
        self._sensors: list[dict[str, any]] = []  # type: ignore[annotation-unchecked]
        self._buzzer_pin: int | None = None
        self._gpio: any = None  # type: ignore[annotation-unchecked]
        self._last_readings: dict[int, UltrasonicReading] = {}

    async def start(self) -> None:
        """Initialize GPIO and sensor pins."""
        logger.info("UltrasonicArray: initializing %d sensors...",
                    (self.cfg.front_count if self.cfg else 4) +
                    (self.cfg.rear_count if self.cfg else 4))

        try:
            import lgpio
            self._gpio = lgpio.gpiochip_open(0)
        except ImportError:
            logger.warning("UltrasonicArray: lgpio not available, using mock")
            self._gpio = None

        # Define sensor layout
        self._sensors = self._build_sensor_map()
        for s in self._sensors:
            if self._gpio:
                lgpio.gpio_claim_output(self._gpio, s["trig"])
                lgpio.gpio_claim_input(self._gpio, s["echo"])
                lgpio.gpio_write(self._gpio, s["trig"], 0)

        self._running = True
        asyncio.create_task(self._poll_loop())
        asyncio.create_task(self._alert_loop())
        logger.info("UltrasonicArray: started")

    async def stop(self) -> None:
        """Shutdown sensors."""
        self._running = False
        if self._gpio:
            try:
                import lgpio
                lgpio.gpiochip_close(self._gpio)
            except Exception:
                pass
        logger.info("UltrasonicArray: stopped")

    def _build_sensor_map(self) -> list[dict[str, any]]:  # type: ignore[annotation-unchecked]
        """Build sensor ID → pin mapping."""
        # Default layout if no config
        default_front = [(17, 27), (22, 23), (24, 25), (5, 6)]   # FL, FR, FC, FRC
        default_rear = [(12, 13), (16, 19), (20, 21), (26, 4)]   # RL, RR, RC, RRC
        positions = ["FL", "FR", "FC", "FRC", "RL", "RR", "RC", "RRC"]

        front = self.cfg.front_pins if self.cfg and self.cfg.front_pins else default_front
        rear = self.cfg.rear_pins if self.cfg and self.cfg.rear_pins else default_rear

        sensors = []
        for i, (trig, echo) in enumerate(front + rear):
            sensors.append({
                "id": i,
                "position": positions[i] if i < len(positions) else f"S{i}",
                "trig": trig,
                "echo": echo,
            })
        return sensors

    # ── Measurement ───────────────────────────────────────────────────────────

    def _measure_one(self, sensor: dict[str, any]) -> float:  # type: ignore[annotation-unchecked]
        """Measure distance for one sensor using lgpio."""
        if not self._gpio:
            return -1.0

        import lgpio
        trig = sensor["trig"]
        echo = sensor["echo"]

        # Send trigger pulse
        lgpio.gpio_write(self._gpio, trig, 1)
        time.sleep(0.00001)  # 10 µs
        lgpio.gpio_write(self._gpio, trig, 0)

        # Wait for echo start
        start = time.monotonic()
        timeout = start + self.MAX_PULSE_DURATION
        while lgpio.gpio_read(self._gpio, echo) == 0:
            if time.monotonic() > timeout:
                return -1.0

        echo_start = time.monotonic()

        # Wait for echo end
        while lgpio.gpio_read(self._gpio, echo) == 1:
            if time.monotonic() > timeout:
                return -1.0

        echo_end = time.monotonic()
        duration = echo_end - echo_start
        distance = (duration * 1000000 * self.SPEED_OF_SOUND_CM_US) / 2
        return distance

    def _classify_zone(self, distance_cm: float) -> ParkingZone:
        """Classify distance into parking zone."""
        if distance_cm < 0 or distance_cm > 400:
            return ParkingZone.CLEAR
        elif distance_cm < 15:
            return ParkingZone.DANGER
        elif distance_cm < 30:
            return ParkingZone.NEAR
        elif distance_cm < 60:
            return ParkingZone.MID
        elif distance_cm < 100:
            return ParkingZone.FAR
        else:
            return ParkingZone.CLEAR

    # ── Polling loop ──────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Poll all sensors in round-robin with crosstalk avoidance."""
        while self._running:
            try:
                for sensor in self._sensors:
                    dist = await asyncio.to_thread(self._measure_one, sensor)
                    valid = dist >= 0
                    zone = self._classify_zone(dist if valid else 999)

                    reading = UltrasonicReading(
                        sensor_id=sensor["id"],
                        position=sensor["position"],
                        distance_cm=dist if valid else -1.0,
                        zone=zone,
                        valid=valid,
                        timestamp=time.monotonic(),
                    )
                    self._last_readings[sensor["id"]] = reading

                    # 60ms settle time between sensors
                    await asyncio.sleep(self.SETTLE_TIME)

                await self._publish_batch()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("UltrasonicArray: poll error")
                await asyncio.sleep(0.5)

    async def _publish_batch(self) -> None:
        """Publish all sensor readings."""
        from pi_camry.core import EventType, bus
        readings = [r.to_dict() if hasattr(r, "to_dict") else {
            "sensor_id": r.sensor_id,
            "position": r.position,
            "distance_cm": r.distance_cm,
            "zone": r.zone.name,
            "valid": r.valid,
        } for r in self._last_readings.values()]
        await bus.publish(EventType.ENVIRONMENT, {"ultrasonic": readings}, source="ultrasonic")

    # ── Audio alert loop ──────────────────────────────────────────────────────

    async def _alert_loop(self) -> None:
        """Generate audio beeps based on closest obstacle."""
        while self._running:
            try:
                # Find closest valid reading
                valid = [r for r in self._last_readings.values() if r.valid]
                if not valid:
                    await asyncio.sleep(0.5)
                    continue

                closest = min(valid, key=lambda r: r.distance_cm)
                zone = closest.zone

                # Determine beep pattern
                if zone == ParkingZone.CLEAR:
                    await asyncio.sleep(0.5)
                    continue
                elif zone == ParkingZone.FAR:
                    beep_duration = 0.1
                    pause = 0.9
                elif zone == ParkingZone.MID:
                    beep_duration = 0.1
                    pause = 0.4
                elif zone == ParkingZone.NEAR:
                    beep_duration = 0.15
                    pause = 0.15
                else:  # DANGER
                    beep_duration = 0.5
                    pause = 0.1

                await self._beep(beep_duration)
                await asyncio.sleep(pause)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.5)

    async def _beep(self, duration: float) -> None:
        """Actuate piezo buzzer."""
        # Placeholder: would use GPIO PWM or audio output
        # For now, publish event for audio module to handle
        from pi_camry.core import EventType, bus
        await bus.publish(
            EventType.AUDIO_ALERT,
            {"tone": self.TONE_FREQ, "duration": duration},
            source="ultrasonic",
        )

    # ── Public API ──────────────────────────────────────────────────────────

    def get_reading(self, position: str) -> UltrasonicReading | None:
        """Get reading for a specific position."""
        for r in self._last_readings.values():
            if r.position == position:
                return r
        return None

    def get_all_readings(self) -> dict[int, UltrasonicReading]:
        return dict(self._last_readings)

    def get_closest(self) -> UltrasonicReading | None:
        """Get closest valid reading."""
        valid = [r for r in self._last_readings.values() if r.valid]
        return min(valid, key=lambda r: r.distance_cm) if valid else None
