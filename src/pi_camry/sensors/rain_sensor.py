"""
pi_camry/sensors/rain_sensor.py
───────────────────────────────
Rain detection via IR reflectance or capacitive sensor.

Hardware options:
- IR reflectance module: IR LED + photodiode, detects water on glass
- Capacitive sensor: detects water via capacitance change
- Aftermarket OEM sensor (e.g., Hella): analog 0-5V output

The sensor is mounted inside the windshield, behind the rearview mirror.
Dry glass reflects IR strongly; water droplets scatter IR, reducing signal.

Integration:
- Auto wiper control (GPIO relay to wiper motor or wiper switch)
- Auto headlight activation
- HVAC defog trigger (high humidity + rain)
- Smart interval wiper: adjust delay based on rain intensity

Wiring:
- Analog sensor → MCP3008 ADC (SPI) → Pi
- Digital sensor → GPIO input (with pull-up)
- Mount: behind rearview mirror, sensor facing windshield

Calibration (IR reflectance):
    Dry glass:    ADC > 800
    Light rain:   600-800
    Heavy rain:   < 400
    Hysteresis:   ON at 600, OFF at 700 (prevents wiper chatter)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pi_camry.core.config import RainSensorConfig

logger = logging.getLogger("camry.sensors.rain")


class RainIntensity(IntEnum):
    """Rain intensity classification."""
    NONE = 0
    DRIZZLE = 1
    LIGHT = 2
    MODERATE = 3
    HEAVY = 4
    STORM = 5


@dataclass(frozen=True)
class RainReading:
    """Rain sensor reading."""
    raw_adc: int          # 0-1023 (MCP3008) or 0-4095 (ADS1115)
    intensity: RainIntensity
    is_wet: bool
    wiper_speed: int      # 0-3 (off, slow, medium, fast)
    timestamp: float


class RainSensor:
    """Rain detection with smart wiper control."""

    # ADC thresholds (IR reflectance, inverted: lower = more rain)
    THRESHOLD_ON = 600      # Start wipers
    THRESHOLD_OFF = 700     # Stop wipers (hysteresis)
    THRESHOLD_LIGHT = 750   # Light rain boundary
    THRESHOLD_HEAVY = 400   # Heavy rain boundary
    THRESHOLD_STORM = 200   # Storm boundary

    # Wiper speed mapping
    WIPER_SPEEDS = [0, 1, 2, 3]  # off, slow, medium, fast

    def __init__(self, cfg: "RainSensorConfig" | None = None) -> None:
        self.cfg = cfg
        self._running = False
        self._adc_channel: int = 0  # MCP3008 channel
        self._mcp3008: any = None   # type: ignore[annotation-unchecked]
        self._last_reading: RainReading | None = None
        self._wiper_state: int = 0   # Current wiper speed
        self._last_wiper_change: float = 0.0
        self._hysteresis_active: bool = False

    async def start(self) -> None:
        """Initialize ADC and start polling."""
        logger.info("RainSensor: initializing...")
        try:
            import spidev
            self._mcp3008 = spidev.SpiDev()
            self._mcp3008.open(0, 0)  # SPI bus 0, device 0
            self._mcp3008.max_speed_hz = 1000000
            logger.info("RainSensor: MCP3008 ADC initialized")
        except ImportError:
            logger.warning("RainSensor: spidev not available, using mock")
        except Exception as exc:
            logger.error("RainSensor: ADC init failed: %s", exc)

        self._running = True
        asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Shutdown sensor."""
        self._running = False
        if self._mcp3008:
            self._mcp3008.close()
        logger.info("RainSensor: stopped")

    # ── ADC reading ───────────────────────────────────────────────────────────

    def _read_adc(self) -> int:
        """Read MCP3008 ADC channel."""
        if not self._mcp3008:
            # Mock: simulate light rain
            import random
            return 650 + random.randint(-50, 50)
        # MCP3008 single-ended read on channel
        cmd = [1, (8 + self._adc_channel) << 4, 0]
        resp = self._mcp3008.xfer2(cmd)
        adc = ((resp[1] & 3) << 8) + resp[2]
        return adc

    # ── Classification ──────────────────────────────────────────────────────

    def _classify(self, adc: int) -> RainIntensity:
        """Classify rain intensity from ADC value."""
        if adc > self.THRESHOLD_LIGHT:
            return RainIntensity.NONE
        elif adc > self.THRESHOLD_ON:
            return RainIntensity.DRIZZLE
        elif adc > self.THRESHOLD_HEAVY:
            return RainIntensity.LIGHT
        elif adc > self.THRESHOLD_STORM:
            return RainIntensity.MODERATE
        else:
            return RainIntensity.HEAVY

    def _wiper_speed_for(self, intensity: RainIntensity) -> int:
        """Map rain intensity to wiper speed."""
        mapping = {
            RainIntensity.NONE: 0,
            RainIntensity.DRIZZLE: 0,      # Intermittent (handled by delay)
            RainIntensity.LIGHT: 1,        # Slow
            RainIntensity.MODERATE: 2,     # Medium
            RainIntensity.HEAVY: 3,        # Fast
            RainIntensity.STORM: 3,        # Fast
        }
        return mapping.get(intensity, 0)

    # ── Polling loop ──────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Read sensor and control wipers."""
        while self._running:
            try:
                adc = await asyncio.to_thread(self._read_adc)
                intensity = self._classify(adc)
                is_wet = adc <= self.THRESHOLD_ON

                # Hysteresis: once ON, stay ON until above THRESHOLD_OFF
                if is_wet:
                    self._hysteresis_active = True
                elif adc > self.THRESHOLD_OFF:
                    self._hysteresis_active = False

                effective_wet = self._hysteresis_active and adc <= self.THRESHOLD_OFF
                wiper_speed = self._wiper_speed_for(intensity) if effective_wet else 0

                # Debounce wiper changes (min 3 sec between changes)
                now = time.monotonic()
                if wiper_speed != self._wiper_state and (now - self._last_wiper_change) > 3.0:
                    self._wiper_state = wiper_speed
                    self._last_wiper_change = now
                    await self._set_wiper_speed(wiper_speed)

                reading = RainReading(
                    raw_adc=adc,
                    intensity=intensity,
                    is_wet=effective_wet,
                    wiper_speed=self._wiper_state,
                    timestamp=now,
                )
                self._last_reading = reading

                await self._publish(reading)
                await asyncio.sleep(1.0)  # 1 Hz is sufficient
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("RainSensor: poll error")
                await asyncio.sleep(2.0)

    async def _set_wiper_speed(self, speed: int) -> None:
        """Actuate wiper relay."""
        from pi_camry.core import EventType, bus
        logger.info("RainSensor: wiper speed → %d", speed)
        await bus.publish(
            EventType.GPIO_COMMAND,
            {"relay": "wiper_speed", "state": speed > 0, "speed": speed},
            source="rain_sensor",
        )

    async def _publish(self, reading: RainReading) -> None:
        """Publish rain reading to EventBus."""
        from pi_camry.core import EventType, bus
        await bus.publish(
            EventType.ENVIRONMENT,
            {
                "rain_adc": reading.raw_adc,
                "rain_intensity": reading.intensity.name,
                "is_wet": reading.is_wet,
                "wiper_speed": reading.wiper_speed,
            },
            source="rain_sensor",
        )

    # ── Public API ──────────────────────────────────────────────────────────

    def get_reading(self) -> RainReading | None:
        return self._last_reading

    def is_raining(self) -> bool:
        return self._last_reading.is_wet if self._last_reading else False

    def get_wiper_speed(self) -> int:
        return self._wiper_state
