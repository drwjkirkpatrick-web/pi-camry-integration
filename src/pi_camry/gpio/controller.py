"""
pi_camry/gpio/controller.py
───────────────────────────
GPIO relay control and digital input monitoring for Pi 5.

Uses lgpio (Pi 5 compatible) instead of the deprecated RPi.GPIO.
Handles:
- 8 relay outputs (active-low logic with relay board)
- 5 digital inputs (ignition, door, trunk, hood, seatbelt)
- Ignition-sense → graceful shutdown timer
- ADC via MCP3008 (SPI) for analog sensors
- Event emission on async bus

Wiring (relay board = active LOW):
    Relay ON  → GPIO output LOW  (0V sinks current through coil)
    Relay OFF → GPIO output HIGH (3.3V, no current)

Usage:
    from pi_camry.gpio.controller import GPIOController
    gpio = GPIOController()
    await gpio.start()
    await gpio.set_relay("headlights", on=True)
    await gpio.shutdown()
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable

from pi_camry.core import EventType, bus
from pi_camry.core.config import settings

logger = logging.getLogger("camry.gpio")


class RelayName(Enum):
    """Named relays for type-safe control."""
    COOLING_FAN = "cooling_fan"
    FUEL_PUMP = "fuel_pump"
    HEADLIGHTS = "headlights"
    DOME_LIGHT = "dome_light"
    HEATED_SEATS = "heated_seats"
    HVAC_COMPRESSOR = "hvac_compressor"
    BLOCK_HEATER = "block_heater"
    POWER_ANTENNA = "power_antenna"


class InputName(Enum):
    """Named digital inputs."""
    IGNITION = "ignition"
    DOOR = "door"
    TRUNK = "trunk"
    HOOD = "hood"
    SEATBELT = "seatbelt"


@dataclass
class RelayState:
    """Current state of a relay with metadata."""
    name: str
    on: bool = False
    last_change: datetime = field(default_factory=datetime.utcnow)
    auto_off_time: datetime | None = None  # for timed relays (heated seats, etc.)


@dataclass
class InputState:
    """Current state of a digital input."""
    name: str
    active: bool = False
    last_change: datetime = field(default_factory=datetime.utcnow)
    debounce_until: float = 0.0


class GPIOController:
    """Pi 5 GPIO controller using lgpio for relays, inputs, and SPI ADC."""

    def __init__(self) -> None:
        self.cfg = settings.gpio
        self._running = False
        self._task: asyncio.Task | None = None
        self._shutdown_task: asyncio.Task | None = None

        # lgpio handle
        self._h: int | None = None  # lgpio chip handle

        # Relay map: name -> pin
        self._relay_pins: dict[RelayName, int] = {
            RelayName.COOLING_FAN: self.cfg.relay_cooling_fan,
            RelayName.FUEL_PUMP: self.cfg.relay_fuel_pump,
            RelayName.HEADLIGHTS: self.cfg.relay_headlights,
            RelayName.DOME_LIGHT: self.cfg.relay_dome_light,
            RelayName.HEATED_SEATS: self.cfg.relay_heated_seats,
            RelayName.HVAC_COMPRESSOR: self.cfg.relay_hvac_compressor,
            RelayName.BLOCK_HEATER: self.cfg.relay_block_heater,
            RelayName.POWER_ANTENNA: self.cfg.relay_power_antenna,
        }

        # Input map: name -> pin
        self._input_pins: dict[InputName, int] = {
            InputName.IGNITION: self.cfg.ignition_sense,
            InputName.DOOR: self.cfg.door_ajar,
            InputName.TRUNK: self.cfg.trunk_ajar,
            InputName.HOOD: self.cfg.hood_ajar,
            InputName.SEATBELT: self.cfg.seatbelt,
        }

        # State tracking
        self._relay_states: dict[RelayName, RelayState] = {
            name: RelayState(name=name.value) for name in RelayName
        }
        self._input_states: dict[InputName, InputState] = {
            name: InputState(name=name.value) for name in InputName
        }

        # Ignition shutdown timer
        self._ignition_off_time: float | None = None
        self._shutdown_delay_sec = 60.0  # time to wait after ignition off before shutdown

        # SPI for MCP3008
        self._spi: any = None  # type: ignore[annotation-unchecked]

    # ── Public API ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize lgpio, configure pins, start input polling."""
        logger.info("GPIO: initializing lgpio...")
        try:
            import lgpio
            self._h = lgpio.gpiochip_open(0)  # /dev/gpiochip0
        except ImportError:
            logger.error("GPIO: lgpio not installed. Run: sudo apt install python3-lgpio")
            return
        except Exception as exc:
            logger.error("GPIO: failed to open gpiochip: %s", exc)
            return

        import lgpio as lg

        # Configure relay pins as outputs, default HIGH (relay OFF)
        for name, pin in self._relay_pins.items():
            lg.gpio_claim_output(self._h, pin, level=1)
            logger.debug("GPIO: relay %s on pin %d (default OFF)", name.value, pin)

        # Configure inputs with pull-down (most car switches go +12V when closed)
        for name, pin in self._input_pins.items():
            lg.gpio_claim_input(self._h, pin, lgpio.SET_PULL_DOWN)
            logger.debug("GPIO: input %s on pin %d (pull-down)", name.value, pin)

        # Initialize SPI for MCP3008
        await self._init_spi()

        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("GPIO: controller started")

    async def shutdown(self) -> None:
        """Graceful shutdown: all relays OFF, close lgpio."""
        logger.info("GPIO: shutting down — all relays OFF")
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._shutdown_task:
            self._shutdown_task.cancel()

        # All relays OFF (HIGH)
        for name in RelayName:
            await self.set_relay(name, on=False)

        if self._spi:
            self._spi.close()

        if self._h is not None:
            import lgpio
            lgpio.gpiochip_close(self._h)
            self._h = None

        logger.info("GPIO: controller stopped")

    async def set_relay(self, name: RelayName, on: bool) -> bool:
        """Set a relay ON or OFF. Returns success."""
        if self._h is None:
            logger.warning("GPIO: not initialized, cannot set relay")
            return False

        pin = self._relay_pins[name]
        import lgpio
        # Active-low: LOW = ON, HIGH = OFF
        level = 0 if on else 1
        lgpio.gpio_write(self._h, pin, level)

        state = self._relay_states[name]
        state.on = on
        state.last_change = datetime.utcnow()
        logger.info("GPIO: relay %s = %s", name.value, "ON" if on else "OFF")
        return True

    async def pulse_relay(self, name: RelayName, duration_sec: float) -> None:
        """Momentary ON then auto-OFF after duration."""
        await self.set_relay(name, on=True)
        await asyncio.sleep(duration_sec)
        await self.set_relay(name, on=False)

    async def set_headlights_auto(self, ambient_light: float) -> None:
        """Auto headlight control based on ambient light sensor (lux)."""
        # Threshold: < 50 lux = dusk/dark
        if ambient_light < 50:
            await self.set_relay(RelayName.HEADLIGHTS, on=True)
        else:
            await self.set_relay(RelayName.HEADLIGHTS, on=False)

    async def set_heated_seats_timer(self, duration_sec: float = 600) -> None:
        """Turn on heated seats, auto-off after duration."""
        await self.set_relay(RelayName.HEATED_SEATS, on=True)
        state = self._relay_states[RelayName.HEATED_SEATS]
        state.auto_off_time = datetime.utcnow() + __import__("datetime").timedelta(
            seconds=duration_sec
        )
        logger.info("GPIO: heated seats ON, auto-off in %.0f sec", duration_sec)

    def get_relay_state(self, name: RelayName) -> RelayState:
        return self._relay_states[name]

    def get_input_state(self, name: InputName) -> InputState:
        return self._input_states[name]

    # ── ADC (MCP3008 via SPI) ────────────────────────────────────────────────

    async def _init_spi(self) -> None:
        """Initialize SPI bus for MCP3008."""
        try:
            import spidev
            self._spi = spidev.SpiDev()
            self._spi.open(self.cfg.mcp3008_spi_bus, self.cfg.mcp3008_cs)
            self._spi.max_speed_hz = 1350000
            self._spi.mode = 0
            logger.info("GPIO: SPI initialized for MCP3008")
        except ImportError:
            logger.warning("GPIO: spidev not installed — ADC unavailable")
        except Exception as exc:
            logger.error("GPIO: SPI init failed: %s", exc)

    def read_adc(self, channel: int) -> int:
        """Read 10-bit ADC value from MCP3008 channel (0–7)."""
        if not self._spi:
            return 0
        if not 0 <= channel <= 7:
            raise ValueError("ADC channel must be 0–7")
        # MCP3008 single-ended read command
        adc = self._spi.xfer2([1, (8 + channel) << 4, 0])
        data = ((adc[1] & 3) << 8) + adc[2]
        return data

    def read_adc_voltage(self, channel: int, vref: float = 3.3) -> float:
        """Read ADC and convert to voltage."""
        raw = self.read_adc(channel)
        return (raw / 1023.0) * vref

    # ── Input polling loop ────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Poll digital inputs at 10 Hz, debounce, emit events."""
        import lgpio
        while self._running:
            try:
                for name, pin in self._input_pins.items():
                    state = self._input_states[name]
                    raw = lgpio.gpio_read(self._h, pin)
                    now = time.monotonic()

                    # Debounce: ignore changes within 100ms
                    if now < state.debounce_until:
                        continue

                    # Active-high inputs (switch closes = +12V = HIGH)
                    active = raw == 1
                    if active != state.active:
                        state.active = active
                        state.last_change = datetime.utcnow()
                        state.debounce_until = now + 0.1
                        await self._handle_input_change(name, active)

                # Check auto-off relays
                await self._check_auto_off_relays()

                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("GPIO: poll loop error")
                await asyncio.sleep(0.5)

    async def _handle_input_change(self, name: InputName, active: bool) -> None:
        """Emit events on input state changes."""
        logger.info("GPIO: input %s = %s", name.value, "ACTIVE" if active else "INACTIVE")

        if name == InputName.IGNITION:
            if active:
                await bus.publish(EventType.IGNITION_ON, {}, source="gpio")
                self._ignition_off_time = None
                if self._shutdown_task:
                    self._shutdown_task.cancel()
                    self._shutdown_task = None
            else:
                await bus.publish(EventType.IGNITION_OFF, {}, source="gpio")
                self._ignition_off_time = time.monotonic()
                # Start graceful shutdown timer
                self._shutdown_task = asyncio.create_task(self._shutdown_timer())

        elif name == InputName.DOOR:
            event = EventType.DOOR_OPEN if active else EventType.DOOR_CLOSED
            await bus.publish(event, {}, source="gpio")

        elif name == InputName.TRUNK and active:
            await bus.publish(EventType.TRUNK_OPEN, {}, source="gpio")

        elif name == InputName.HOOD and active:
            await bus.publish(EventType.HOOD_OPEN, {}, source="gpio")

    async def _shutdown_timer(self) -> None:
        """Wait N seconds after ignition off, then emit shutdown event."""
        try:
            await asyncio.sleep(self._shutdown_delay_sec)
            logger.info("GPIO: ignition off for %.0f sec — initiating graceful shutdown",
                        self._shutdown_delay_sec)
            await bus.publish(EventType.SYSTEM_SHUTDOWN, {"reason": "ignition_off"}, source="gpio")
        except asyncio.CancelledError:
            logger.info("GPIO: shutdown timer cancelled (ignition back on)")

    async def _check_auto_off_relays(self) -> None:
        """Turn off relays that have reached their auto-off time."""
        now = datetime.utcnow()
        for name, state in self._relay_states.items():
            if state.on and state.auto_off_time and now >= state.auto_off_time:
                await self.set_relay(name, on=False)
                state.auto_off_time = None
                logger.info("GPIO: auto-off relay %s", name.value)
