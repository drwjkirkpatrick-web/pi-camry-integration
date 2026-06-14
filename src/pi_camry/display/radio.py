"""
pi_camry/display/radio.py
─────────────────────────
FM/AM radio tuner control via head-unit integration or external Si4703 module.

Two modes:
1. Head-unit native radio: Pi sends RDS text and station presets via CAN bus
   to the JoyBring head unit, which has its own tuner.
2. External Si4703: Pi controls an FM tuner module via I2C for true
   software-defined radio.

Also manages:
- DAB+ digital radio (if head unit supports)
- Internet radio streaming (fallback via WiFi hotspot)
- Media player UI (local MP3 + streaming)
- RDS/RBDS text display on head unit

Hardware (external mode):
- Si4703 FM Radio Receiver Breakout (SparkFun)
- I2C connection to Pi (SDA/SCL)
- Audio output: Si4703 headphone → Pi ADC → DAC → head unit AUX
  OR: Si4703 I2S → Pi I2S input
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pi_camry.core.config import DisplayConfig

logger = logging.getLogger("camry.display.radio")


@dataclass(frozen=True)
class RadioStation:
    """Saved radio station preset."""
    name: str
    frequency_mhz: float
    genre: str = ""
    is_favorite: bool = False


class RadioTuner:
    """FM/AM radio tuner with head-unit or external Si4703 control."""

    # FM band: 87.5–108.0 MHz (US/Japan)
    FM_MIN = 87.5
    FM_MAX = 108.0
    FM_STEP = 0.1  # 100 kHz steps

    # Default presets (Portland, OR area)
    DEFAULT_PRESETS: list[RadioStation] = [
        RadioStation("KOPB", 91.5, "NPR / Classical", True),
        RadioStation("KQAC", 89.9, "Classical", True),
        RadioStation("KBOO", 90.7, "Community", False),
        RadioStation("KXL", 101.1, "News/Talk", False),
        RadioStation("KINK", 101.9, "Alternative", True),
        RadioStation("KGON", 92.3, "Classic Rock", False),
        RadioStation("KRSK", 105.1, "Adult Hits", False),
        RadioStation("KXZY", 104.3, "Country", False),
    ]

    def __init__(self, cfg: "DisplayConfig" | None = None) -> None:
        self.cfg = cfg
        self._running = False
        self._mode: str = "headunit"  # 'headunit', 'si4703', 'internet'
        self._current_freq: float = 101.1
        self._current_station: RadioStation | None = None
        self._presets: list[RadioStation] = list(self.DEFAULT_PRESETS)
        self._volume: int = 50  # 0-100
        self._muted: bool = False
        self._rds_text: str = ""

        # Si4703 state
        self._si4703: any = None  # type: ignore[annotation-unchecked]

    async def start(self) -> None:
        """Initialize radio based on configured mode."""
        logger.info("Radio: starting in %s mode...", self._mode)
        self._running = True

        if self._mode == "si4703":
            await self._init_si4703()
        elif self._mode == "headunit":
            await self._init_headunit_radio()
        elif self._mode == "internet":
            await self._init_internet_radio()

    async def stop(self) -> None:
        """Shutdown radio."""
        self._running = False
        if self._si4703:
            try:
                self._si4703.power_down()
            except Exception:
                pass
        logger.info("Radio: stopped")

    # ── Mode-specific init ────────────────────────────────────────────────────

    async def _init_si4703(self) -> None:
        """Initialize Si4703 FM tuner via I2C."""
        try:
            from pi_camry.display.si4703 import Si4703
            self._si4703 = Si4703(bus=1, reset_pin=4)
            self._si4703.power_on()
            self._si4703.set_volume(self._volume)
            self._si4703.tune(self._current_freq)
            logger.info("Radio: Si4703 tuned to %.1f MHz", self._current_freq)
            # Start RDS polling
            asyncio.create_task(self._rds_poll_loop())
        except ImportError:
            logger.warning("Radio: Si4703 module not available")
        except Exception as exc:
            logger.error("Radio: Si4703 init failed: %s", exc)

    async def _init_headunit_radio(self) -> None:
        """Send presets to head unit via CAN or ADB."""
        logger.info("Radio: head-unit mode — sending presets via CAN")
        # In head-unit mode, the head unit does the actual tuning.
        # We just send RDS text and station info for display.
        from pi_camry.core import EventType, bus
        await bus.publish(
            EventType.RADIO_PRESETS,
            {"presets": [(s.name, s.frequency_mhz) for s in self._presets]},
            source="radio",
        )

    async def _init_internet_radio(self) -> None:
        """Prepare internet radio streaming via WiFi hotspot."""
        logger.info("Radio: internet mode — streaming via WiFi")
        # Internet radio uses mpv/vlc to stream URLs
        # Station list loaded from config or default
        pass

    # ── Tuning ──────────────────────────────────────────────────────────────

    async def tune(self, frequency_mhz: float) -> bool:
        """Tune to a specific frequency."""
        if not (self.FM_MIN <= frequency_mhz <= self.FM_MAX):
            logger.warning("Radio: frequency %.1f out of range", frequency_mhz)
            return False

        self._current_freq = frequency_mhz

        if self._mode == "si4703" and self._si4703:
            self._si4703.tune(frequency_mhz)
            logger.info("Radio: Si4703 tuned to %.1f MHz", frequency_mhz)
        elif self._mode == "headunit":
            # Send CAN command to head unit
            from pi_camry.core import EventType, bus
            await bus.publish(
                EventType.RADIO_TUNE,
                {"frequency": frequency_mhz},
                source="radio",
            )
        elif self._mode == "internet":
            # Find matching internet station
            pass

        # Find matching preset
        for preset in self._presets:
            if abs(preset.frequency_mhz - frequency_mhz) < 0.15:
                self._current_station = preset
                break
        else:
            self._current_station = None

        return True

    async def seek_up(self) -> None:
        """Seek next station upward."""
        if self._mode == "si4703" and self._si4703:
            freq = self._si4703.seek_up()
            if freq:
                self._current_freq = freq
                logger.info("Radio: seek up → %.1f MHz", freq)
        else:
            # Manual step
            new_freq = min(self.FM_MAX, self._current_freq + self.FM_STEP)
            await self.tune(new_freq)

    async def seek_down(self) -> None:
        """Seek previous station downward."""
        if self._mode == "si4703" and self._si4703:
            freq = self._si4703.seek_down()
            if freq:
                self._current_freq = freq
                logger.info("Radio: seek down → %.1f MHz", freq)
        else:
            new_freq = max(self.FM_MIN, self._current_freq - self.FM_STEP)
            await self.tune(new_freq)

    async def set_preset(self, slot: int, station: RadioStation) -> None:
        """Save a station to preset slot (1-8)."""
        idx = slot - 1
        if 0 <= idx < len(self._presets):
            self._presets[idx] = station
            logger.info("Radio: preset %d = %s (%.1f MHz)", slot, station.name, station.frequency_mhz)

    async def recall_preset(self, slot: int) -> None:
        """Tune to preset slot (1-8)."""
        idx = slot - 1
        if 0 <= idx < len(self._presets):
            station = self._presets[idx]
            await self.tune(station.frequency_mhz)
            logger.info("Radio: preset %d — %s", slot, station.name)

    # ── Volume / mute ───────────────────────────────────────────────────────

    async def set_volume(self, level: int) -> None:
        """Set volume 0-100."""
        self._volume = max(0, min(100, level))
        if self._mode == "si4703" and self._si4703:
            self._si4703.set_volume(self._volume)
        logger.info("Radio: volume set to %d", self._volume)

    async def mute(self) -> None:
        """Mute audio."""
        self._muted = True
        if self._mode == "si4703" and self._si4703:
            self._si4703.mute()
        logger.info("Radio: muted")

    async def unmute(self) -> None:
        """Unmute audio."""
        self._muted = False
        if self._mode == "si4703" and self._si4703:
            self._si4703.unmute()
        logger.info("Radio: unmuted")

    # ── RDS polling ─────────────────────────────────────────────────────────

    async def _rds_poll_loop(self) -> None:
        """Poll RDS/RBDS text from Si4703."""
        if not self._si4703:
            return
        while self._running:
            try:
                rds = self._si4703.read_rds()
                if rds and rds != self._rds_text:
                    self._rds_text = rds
                    logger.info("Radio: RDS: %s", rds)
                    # Forward to head unit for display
                    from pi_camry.core import EventType, bus
                    await bus.publish(
                        EventType.RADIO_RDS,
                        {"text": rds, "frequency": self._current_freq},
                        source="radio",
                    )
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Radio: RDS poll error")
                await asyncio.sleep(5.0)

    # ── Status ──────────────────────────────────────────────────────────────

    def get_status(self) -> dict[str, any]:  # type: ignore[annotation-unchecked]
        """Return current radio status."""
        return {
            "mode": self._mode,
            "frequency_mhz": self._current_freq,
            "station": self._current_station.name if self._current_station else None,
            "volume": self._volume,
            "muted": self._muted,
            "rds": self._rds_text,
            "presets": [(s.name, s.frequency_mhz) for s in self._presets],
        }
