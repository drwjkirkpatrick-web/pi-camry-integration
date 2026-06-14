"""
pi_camry/display/steering_wheel.py
─────────────────────────────────
Steering wheel control (SWC) button decoder for Toyota Camry.

Most 1996 Camrys didn't have steering wheel audio controls, but aftermarket
head units expect them. This module reads an ADC resistor ladder that
simulates SWC buttons using the existing clock spring wiring.

Resistor ladder design (if adding SWC to a '96):
    5V → [button resistor] → ADC pin → 1kΩ → GND

    Button        Resistor   Voltage (approx)
    ─────────────────────────────────────────
    VOL_UP        2.2kΩ      3.8V
    VOL_DOWN      4.7kΩ      3.2V
    SEEK_UP       10kΩ       2.6V
    SEEK_DOWN     22kΩ       2.0V
    MODE          47kΩ       1.4V
    MUTE          100kΩ      0.8V
    PICKUP        220kΩ      0.3V
    HANGUP        470kΩ      0.1V

Reads via MCP3008 ADC (SPI) at 50 Hz with debouncing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

logger = logging.getLogger("camry.display.swc")


class SteeringWheelController:
    """Read and decode steering wheel control button presses."""

    # Voltage ranges for each button (min, max) in volts
    # These must be calibrated for your specific resistor ladder
    BUTTON_MAP: dict[str, tuple[float, float]] = {
        "VOL_UP":    (3.6, 4.2),
        "VOL_DOWN":  (3.0, 3.6),
        "SEEK_UP":   (2.4, 3.0),
        "SEEK_DOWN": (1.8, 2.4),
        "MODE":      (1.2, 1.8),
        "MUTE":      (0.6, 1.2),
        "PICKUP":    (0.2, 0.6),
        "HANGUP":    (0.0, 0.2),
    }

    # Debounce: button must be stable for this many consecutive reads
    DEBOUNCE_COUNT = 3
    # ADC sample rate
    SAMPLE_HZ = 50

    def __init__(self, cfg: any = None) -> None:  # type: ignore[annotation-unchecked]
        self.cfg = cfg
        self._running = False
        self._callbacks: list[Callable] = []

        # ADC configuration
        self._adc_channel = 0  # MCP3008 CH0
        self._spi_bus = 0
        self._spi_device = 0
        self._vref = 5.0  # ADC reference voltage

        # Debounce state
        self._last_raw: float = 0.0
        self._stable_count: int = 0
        self._last_button: str | None = None
        self._pressed: bool = False

    def register_callback(self, cb: Callable) -> None:
        """Register callback for decoded button presses."""
        self._callbacks.append(cb)

    async def start(self) -> None:
        """Start ADC polling loop."""
        logger.info("SWC: starting steering wheel control monitor...")
        self._running = True
        asyncio.create_task(self._adc_poll_loop())

    async def stop(self) -> None:
        """Stop ADC polling."""
        self._running = False
        logger.info("SWC: stopped")

    def is_active(self) -> bool:
        return self._running

    # ── ADC polling ─────────────────────────────────────────────────────────

    async def _adc_poll_loop(self) -> None:
        """Read MCP3008 ADC at 50 Hz and decode button presses."""
        try:
            import spidev
            spi = spidev.SpiDev()
            spi.open(self._spi_bus, self._spi_device)
            spi.max_speed_hz = 1000000  # 1 MHz

            while self._running:
                raw = self._read_mcp3008(spi, self._adc_channel)
                voltage = (raw / 1023.0) * self._vref

                button = self._decode_voltage(voltage)

                if button != self._last_button:
                    self._stable_count = 0
                    self._last_button = button
                else:
                    self._stable_count += 1

                # Debounced press
                if button and self._stable_count >= self.DEBOUNCE_COUNT and not self._pressed:
                    self._pressed = True
                    await self._emit_button(button, voltage)

                # Release detection
                if not button and self._pressed:
                    self._pressed = False
                    self._stable_count = 0

                self._last_raw = voltage
                await asyncio.sleep(1.0 / self.SAMPLE_HZ)

            spi.close()
        except ImportError:
            logger.warning("SWC: spidev not available, using mock mode")
            await self._mock_loop()
        except Exception as exc:
            logger.error("SWC: ADC loop failed: %s", exc)

    def _read_mcp3008(self, spi: any, channel: int) -> int:  # type: ignore[annotation-unchecked]
        """Read a single channel from MCP3008 via SPI."""
        # MCP3008 single-ended read command: 0x01, 0x80 | (channel << 4), 0x00
        cmd = [0x01, 0x80 | (channel << 4), 0x00]
        resp = spi.xfer2(cmd)
        # ADC value is in last 10 bits of response
        return ((resp[1] & 0x03) << 8) | resp[2]

    def _decode_voltage(self, voltage: float) -> str | None:
        """Map ADC voltage to button name."""
        for name, (v_min, v_max) in self.BUTTON_MAP.items():
            if v_min <= voltage <= v_max:
                return name
        return None

    async def _emit_button(self, name: str, voltage: float) -> None:
        """Emit decoded button press to callbacks."""
        from pi_camry.display.joybring import SteeringWheelButton
        # Calculate approximate resistance
        if voltage > 0.01:
            r_button = (voltage * 1000.0) / (self._vref - voltage)
        else:
            r_button = 0.0

        btn = SteeringWheelButton(
            name=name,
            adc_voltage=round(voltage, 3),
            resistance_ohms=round(r_button, 1),
        )
        logger.info("SWC: button pressed: %s (%.2fV, %.1fΩ)", name, voltage, r_button)
        for cb in self._callbacks:
            try:
                cb(btn)
            except Exception:
                logger.exception("SWC: callback error")

    # ── Mock mode for testing ───────────────────────────────────────────────

    async def _mock_loop(self) -> None:
        """Mock mode: simulate button presses for bench testing."""
        logger.info("SWC: running in mock mode (no hardware)")
        mock_sequence = ["VOL_UP", "VOL_DOWN", "MODE", None, "SEEK_UP", None]
        idx = 0
        while self._running:
            btn = mock_sequence[idx % len(mock_sequence)]
            if btn:
                await self._emit_button(btn, 3.5)
            idx += 1
            await asyncio.sleep(3.0)
