"""
pi_camry/display/joybring.py
────────────────────────────
JoyBring / aftermarket Android head-unit integration controller.

Manages the Pi 5 ↔ head-unit link over HDMI + USB touch back-channel.

Features:
- HDMI CEC power sync (head-unit on/off → Pi wake/sleep)
- EDID negotiation for optimal resolution (typically 1024×600 or 1280×720)
- USB touch input parsing (multi-touch capacitive)
- Backlight dimming via CEC or GPIO PWM
- CAN bridge (if head-unit exposes vehicle CAN via USB)
- Steering wheel button mapping via ADC resistor ladder

Hardware wiring (JoyBring 10.1" Android head unit):
┌─────────────────┐
│  JoyBring AV    │
│  Head Unit      │
│                 │
│  HDMI INPUT  ←──┼── HDMI0 (Pi 5)        [video]
│  USB OTG     ←──┼── USB-C (Pi 5)        [touch + ADB]
│  AUX IN      ←──┼── 3.5mm DAC (Pi)     [audio]
│  CAN H/L     ↔──┼── MCP2515 (Pi SPI)    [vehicle CAN]
│  SWC (ADC)   ←──┼── MCP3008 (Pi SPI)    [steering wheel]
│  12V POWER   ←──┼── Pi 5 buck converter  [shared power]
│  GND         ←──┼── Pi GND              [common ground]
└─────────────────┘

JoyBring models typically expose:
- HDMI input (primary display from Pi)
- USB OTG port (touch events + ADB debug access)
- RCA AUX input (Pi audio via DAC)
- Optional: CAN bus pins (for vehicle integration)
- Optional: SWC (steering wheel control) ADC pins
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from pi_camry.core import Event
    from pi_camry.core.config import DisplayConfig

logger = logging.getLogger("camry.display.joybring")


class TouchEventType(IntEnum):
    """Multi-touch event types from USB HID protocol."""
    DOWN = 0x01
    UP = 0x00
    MOVE = 0x02
    CANCEL = 0x03


@dataclass(frozen=True)
class TouchPoint:
    """Single touch point from capacitive panel."""
    id: int
    x: int
    y: int
    pressure: int
    event_type: TouchEventType


@dataclass(frozen=True)
class SteeringWheelButton:
    """Decoded steering wheel control button press."""
    name: str
    adc_voltage: float
    resistance_ohms: float


class JoyBringController:
    """Main controller for JoyBring / Android head-unit integration.

    Coordinates:
    - HDMI output (Pi → head unit display)
    - USB touch input (head unit → Pi)
    - CAN bridge (head unit ↔ vehicle CAN)
    - Steering wheel button ADC decoding
    - Audio routing (Pi DAC → head unit AUX)
    - CEC power management
    """

    # JoyBring common resolutions
    SUPPORTED_RESOLUTIONS: list[tuple[int, int]] = [
        (1024, 600),   # 10.1" JoyBring standard
        (1280, 720),   # 720p mode
        (1280, 800),   # 10.1" alternative
        (1920, 1080),  # Full HD (if head unit supports)
    ]

    # Steering wheel control resistor ladder (typical Toyota)
    # Voltage divider: 5V → button resistor → 1kΩ → GND
    # Measured at MCP3008 ADC
    SWC_BUTTONS: dict[str, tuple[float, float]] = {
        # name: (voltage_min, voltage_max) in volts
        "VOL_UP":    (3.8, 4.2),
        "VOL_DOWN":  (3.2, 3.6),
        "SEEK_UP":   (2.6, 3.0),
        "SEEK_DOWN": (2.0, 2.4),
        "MODE":      (1.4, 1.8),
        "MUTE":      (0.8, 1.2),
        "PICKUP":    (0.2, 0.6),
        "HANGUP":    (0.0, 0.2),
    }

    def __init__(self, cfg: "DisplayConfig" | None = None) -> None:
        self.cfg = cfg
        self._running = False

        # Sub-controllers (initialized in start())
        self._hdmi: "HDMISink" | None = None
        self._touch: "TouchInputHandler" | None = None
        self._can_bridge: "CANBridge" | None = None
        self._swc: "SteeringWheelController" | None = None

        # Event callbacks
        self._touch_callbacks: list[Callable[[list[TouchPoint]], None]] = []
        self._swc_callbacks: list[Callable[[SteeringWheelButton], None]] = []
        self._cec_callbacks: list[Callable[[str], None]] = []

        # State
        self._display_on = True
        self._current_resolution: tuple[int, int] = (1024, 600)
        self._backlight_level = 100  # percent

    # ── Lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize HDMI, touch, CAN bridge, and steering wheel control."""
        logger.info("JoyBring: initializing head-unit integration...")
        self._running = True

        # 1. HDMI sink — negotiate resolution, enable CEC
        from pi_camry.display.hdmi_sink import HDMISink
        self._hdmi = HDMISink(self.cfg)
        await self._hdmi.start()
        self._current_resolution = await self._hdmi.get_preferred_resolution()
        logger.info("JoyBring: HDMI negotiated %dx%d", *self._current_resolution)

        # 2. Touch input handler
        from pi_camry.display.touch_input import TouchInputHandler
        self._touch = TouchInputHandler(self.cfg)
        self._touch.register_callback(self._on_touch_event)
        await self._touch.start()

        # 3. CAN bridge (if head unit exposes CAN)
        if self.cfg and getattr(self.cfg, "can_enabled", False):
            from pi_camry.display.can_bridge import CANBridge
            self._can_bridge = CANBridge(self.cfg)
            await self._can_bridge.start()

        # 4. Steering wheel control ADC
        if self.cfg and getattr(self.cfg, "swc_enabled", False):
            from pi_camry.display.steering_wheel import SteeringWheelController
            self._swc = SteeringWheelController(self.cfg)
            self._swc.register_callback(self._on_swc_event)
            await self._swc.start()

        # 5. CEC power monitoring
        asyncio.create_task(self._cec_power_monitor())

        logger.info("JoyBring: all subsystems initialized")

    async def stop(self) -> None:
        """Graceful shutdown of all display subsystems."""
        self._running = False
        if self._hdmi:
            await self._hdmi.stop()
        if self._touch:
            await self._touch.stop()
        if self._can_bridge:
            await self._can_bridge.stop()
        if self._swc:
            await self._swc.stop()
        logger.info("JoyBring: stopped")

    # ── Public API ──────────────────────────────────────────────────────────

    def register_touch_callback(self, cb: Callable[[list[TouchPoint]], None]) -> None:
        """Register a callback for multi-touch events."""
        self._touch_callbacks.append(cb)

    def register_swc_callback(self, cb: Callable[[SteeringWheelButton], None]) -> None:
        """Register a callback for steering wheel button presses."""
        self._swc_callbacks.append(cb)

    def register_cec_callback(self, cb: Callable[[str], None]) -> None:
        """Register a callback for CEC events (power, volume, etc.)."""
        self._cec_callbacks.append(cb)

    async def set_resolution(self, width: int, height: int) -> bool:
        """Change HDMI output resolution and re-negotiate EDID."""
        if self._hdmi:
            ok = await self._hdmi.set_resolution(width, height)
            if ok:
                self._current_resolution = (width, height)
            return ok
        return False

    async def set_backlight(self, level_percent: int) -> None:
        """Dim head-unit backlight via CEC or GPIO PWM."""
        self._backlight_level = max(0, min(100, level_percent))
        if self._hdmi:
            await self._hdmi.set_backlight(self._backlight_level)
        logger.debug("JoyBring: backlight set to %d%%", self._backlight_level)

    async def send_cec_command(self, command: str) -> None:
        """Send CEC command to head unit (power, volume, input switch)."""
        if self._hdmi:
            await self._hdmi.send_cec(command)

    async def get_display_status(self) -> dict[str, any]:  # type: ignore[annotation-unchecked]
        """Return current display subsystem status."""
        return {
            "display_on": self._display_on,
            "resolution": self._current_resolution,
            "backlight": self._backlight_level,
            "hdmi_connected": self._hdmi.is_connected() if self._hdmi else False,
            "touch_active": self._touch.is_active() if self._touch else False,
            "can_bridge_active": self._can_bridge.is_connected() if self._can_bridge else False,
            "swc_active": self._swc.is_active() if self._swc else False,
        }

    # ── Internal event handlers ─────────────────────────────────────────────

    def _on_touch_event(self, points: list[TouchPoint]) -> None:
        """Forward touch events to registered callbacks."""
        for cb in self._touch_callbacks:
            try:
                cb(points)
            except Exception:
                logger.exception("JoyBring: touch callback error")

    def _on_swc_event(self, button: SteeringWheelButton) -> None:
        """Forward steering wheel button to registered callbacks."""
        logger.info("JoyBring: SWC button pressed: %s (%.2fV)",
                    button.name, button.adc_voltage)
        for cb in self._swc_callbacks:
            try:
                cb(button)
            except Exception:
                logger.exception("JoyBring: SWC callback error")

    async def _cec_power_monitor(self) -> None:
        """Monitor CEC power state from head unit.

        When head unit powers off (key off), Pi can sleep or switch to low-power.
        When head unit powers on, Pi wakes and resumes dashboard.
        """
        if not self._hdmi:
            return
        while self._running:
            try:
                power_state = await self._hdmi.get_cec_power_state()
                if power_state == "standby" and self._display_on:
                    self._display_on = False
                    logger.info("JoyBring: head unit powered off → dimming display")
                    await self.set_backlight(0)
                    # Publish event for other subsystems
                    from pi_camry.core import EventType, bus
                    await bus.publish(EventType.DISPLAY_STANDBY, {}, source="joybring")
                elif power_state == "on" and not self._display_on:
                    self._display_on = True
                    logger.info("JoyBring: head unit powered on → restoring display")
                    await self.set_backlight(100)
                    from pi_camry.core import EventType, bus
                    await bus.publish(EventType.DISPLAY_ON, {}, source="joybring")

                # Forward raw CEC events
                for cb in self._cec_callbacks:
                    cb(power_state)

                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("JoyBring: CEC monitor error")
                await asyncio.sleep(5.0)

    # ── Convenience: Map SWC to actions ─────────────────────────────────────

    async def handle_swc_default(self, button: SteeringWheelButton) -> None:
        """Default steering wheel button mapping.

        Maps Toyota SWC buttons to Hermes agent actions:
        - VOL_UP / VOL_DOWN → adjust TTS volume
        - SEEK_UP / SEEK_DOWN → next/prev track or camera view
        - MODE → switch dashboard page (OBD / GPS / camera / radio)
        - MUTE → mute voice assistant
        - PICKUP → accept Telegram call / voice command
        - HANGUP → end voice command / cancel
        """
        from pi_camry.core import EventType, bus

        if button.name == "VOL_UP":
            await bus.publish(EventType.AUDIO_VOLUME_UP, {}, source="swc")
        elif button.name == "VOL_DOWN":
            await bus.publish(EventType.AUDIO_VOLUME_DOWN, {}, source="swc")
        elif button.name == "SEEK_UP":
            await bus.publish(EventType.DISPLAY_NEXT_PAGE, {}, source="swc")
        elif button.name == "SEEK_DOWN":
            await bus.publish(EventType.DISPLAY_PREV_PAGE, {}, source="swc")
        elif button.name == "MODE":
            await bus.publish(EventType.DISPLAY_CHANGE_MODE, {}, source="swc")
        elif button.name == "MUTE":
            await bus.publish(EventType.AUDIO_MUTE, {}, source="swc")
        elif button.name == "PICKUP":
            await bus.publish(EventType.WAKE_WORD_DETECTED, {}, source="swc")
        elif button.name == "HANGUP":
            await bus.publish(EventType.AUDIO_CANCEL, {}, source="swc")
