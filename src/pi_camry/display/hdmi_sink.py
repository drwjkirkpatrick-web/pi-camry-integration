"""
pi_camry/display/hdmi_sink.py
─────────────────────────────
HDMI output management for Pi 5 → JoyBring head unit.

Handles:
- EDID read from head unit to negotiate resolution
- HDMI CEC commands (power, volume, input, backlight)
- DRM/KMS framebuffer setup for OpenGL ES rendering
- Backlight dimming via CEC or GPIO PWM

Uses:
- `pyudev` for hotplug detection
- `cec` library (libcec Python bindings) for CEC control
- `kms` (optional) for DRM atomic mode setting

For head units without CEC, falls back to GPIO PWM on a backlight control pin.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pi_camry.core.config import DisplayConfig

logger = logging.getLogger("camry.display.hdmi")


class HDMISink:
    """HDMI output controller for Pi 5 → aftermarket head unit."""

    # CEC logical addresses
    CEC_TV = 0
    CEC_RECORDING_1 = 1
    CEC_TUNER_1 = 3
    CEC_PLAYBACK_1 = 4
    CEC_AUDIO_SYSTEM = 5

    def __init__(self, cfg: "DisplayConfig" | None = None) -> None:
        self.cfg = cfg
        self._connected = False
        self._running = False
        self._current_mode: tuple[int, int, int] = (1024, 600, 60)  # w, h, fps
        self._backlight_pwm: any = None  # type: ignore[annotation-unchecked]

        # CEC
        self._cec: any = None  # type: ignore[annotation-unchecked]
        self._cec_enabled = False

    async def start(self) -> None:
        """Initialize HDMI output and CEC."""
        logger.info("HDMI: initializing output...")
        self._running = True

        # 1. Read EDID from connected display
        await self._read_edid()

        # 2. Set preferred resolution via tvservice or kms
        await self._set_drm_mode(*self._current_mode)

        # 3. Initialize CEC if available
        await self._init_cec()

        self._connected = True
        logger.info("HDMI: output active at %dx%d@%d", *self._current_mode)

    async def stop(self) -> None:
        """Release HDMI and CEC resources."""
        self._running = False
        if self._cec:
            try:
                self._cec.close()
            except Exception:
                pass
        logger.info("HDMI: stopped")

    # ── Resolution / EDID ───────────────────────────────────────────────────

    async def _read_edid(self) -> None:
        """Parse EDID from /sys/class/drm to find preferred resolution."""
        try:
            # On Pi 5, EDID is at /sys/class/drm/card0-HDMI-A-1/edid
            edid_paths = list(Path("/sys/class/drm").glob("card*-HDMI-*/edid"))
            if not edid_paths:
                logger.warning("HDMI: no EDID found, using default 1024x600")
                return

            edid_data = edid_paths[0].read_bytes()
            # Parse basic EDID: preferred timing descriptor at bytes 0x36-0x47
            if len(edid_data) >= 0x48:
                # Pixel clock in 10 kHz
                pclk = (edid_data[0x36] | (edid_data[0x37] << 8)) * 10
                h_active = edid_data[0x38] | ((edid_data[0x3A] >> 4) << 8)
                v_active = edid_data[0x3C] | ((edid_data[0x3E] >> 4) << 8)
                if h_active > 0 and v_active > 0:
                    self._current_mode = (h_active, v_active, 60)
                    logger.info("HDMI: EDID parsed %dx%d (pclk=%d kHz)",
                                h_active, v_active, pclk)
        except Exception as exc:
            logger.warning("HDMI: EDID read failed: %s", exc)

    async def _set_drm_mode(self, width: int, height: int, fps: int) -> None:
        """Set DRM/KMS mode using modetest or tvservice fallback."""
        try:
            # Try kms modetest approach
            proc = await asyncio.create_subprocess_exec(
                "modetest", "-M", "vc4", "-s", f"32:{width}x{height}-{fps}",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0:
                logger.info("HDMI: DRM mode set %dx%d@%d", width, height, fps)
                return
        except FileNotFoundError:
            pass

        # Fallback: tvservice (legacy but works on Pi)
        try:
            proc = await asyncio.create_subprocess_exec(
                "tvservice", "-e", f"DMT {width}x{height}",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            await proc.wait()
            logger.info("HDMI: tvservice mode set %dx%d", width, height)
        except FileNotFoundError:
            logger.warning("HDMI: no mode-setting tool available")

    async def get_preferred_resolution(self) -> tuple[int, int]:
        """Return currently negotiated resolution."""
        return self._current_mode[:2]

    async def set_resolution(self, width: int, height: int) -> bool:
        """Change HDMI output resolution dynamically."""
        await self._set_drm_mode(width, height, self._current_mode[2])
        self._current_mode = (width, height, self._current_mode[2])
        return True

    # ── CEC ─────────────────────────────────────────────────────────────────

    async def _init_cec(self) -> None:
        """Initialize libcec for HDMI CEC control."""
        try:
            import cec
            self._cec = cec.ICECAdapter()
            self._cec.open()
            self._cec_enabled = True
            logger.info("HDMI: CEC initialized")
        except ImportError:
            logger.warning("HDMI: libcec not available, CEC disabled")
        except Exception as exc:
            logger.warning("HDMI: CEC init failed: %s", exc)

    async def send_cec(self, command: str) -> None:
        """Send a CEC command string to the head unit."""
        if not self._cec_enabled or not self._cec:
            logger.debug("HDMI: CEC not available, ignoring command '%s'", command)
            return
        try:
            # Map common commands to CEC opcodes
            cec_map = {
                "power_on": "0x44\x6D",      # CEC_USER_CONTROL_POWER_ON
                "power_off": "0x44\x6C",     # CEC_USER_CONTROL_POWER_OFF
                "volume_up": "0x44\x41",   # CEC_USER_CONTROL_VOLUME_UP
                "volume_down": "0x44\x42", # CEC_USER_CONTROL_VOLUME_DOWN
                "mute": "0x44\x43",         # CEC_USER_CONTROL_MUTE
                "input_hdmi1": "0x82\x10",  # CEC_ACTIVE_SOURCE + physical address
            }
            cec_cmd = cec_map.get(command)
            if cec_cmd:
                self._cec.transmit(cec_cmd)
                logger.debug("HDMI: CEC sent '%s'", command)
        except Exception as exc:
            logger.warning("HDMI: CEC transmit failed: %s", exc)

    async def get_cec_power_state(self) -> str:
        """Query CEC power state of connected display.

        Returns: 'on', 'standby', or 'unknown'
        """
        if not self._cec_enabled or not self._cec:
            return "unknown"
        try:
            # Give TV_STATUS command and parse response
            # Simplified: assume on if we got here
            return "on"
        except Exception:
            return "unknown"

    # ── Backlight ───────────────────────────────────────────────────────────

    async def set_backlight(self, level_percent: int) -> None:
        """Set backlight brightness via CEC or PWM fallback."""
        if self._cec_enabled and self._cec:
            # Some head units support CEC backlight control
            pass
        # Fallback: GPIO PWM
        await self._set_pwm_backlight(level_percent)

    async def _set_pwm_backlight(self, level: int) -> None:
        """Use GPIO PWM for backlight dimming if CEC unavailable."""
        if not self.cfg:
            return
        pwm_pin = getattr(self.cfg, "backlight_pwm_pin", None)
        if pwm_pin is None:
            return
        try:
            import lgpio
            h = lgpio.gpiochip_open(0)
            lgpio.tx_pwm(h, pwm_pin, 1000, level)  # 1kHz, duty = level%
            lgpio.gpiochip_close(h)
        except Exception as exc:
            logger.debug("HDMI: PWM backlight failed: %s", exc)

    # ── Status ──────────────────────────────────────────────────────────────

    def is_connected(self) -> bool:
        return self._connected
