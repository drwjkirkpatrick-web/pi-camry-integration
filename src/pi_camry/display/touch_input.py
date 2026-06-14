"""
pi_camry/display/touch_input.py
───────────────────────────────
USB multi-touch input parser for JoyBring / Android head-unit touch panel.

Reads HID reports from the head unit's USB touch back-channel and converts
them to TouchPoint events.

Protocol: standard USB HID multi-touch digitizer
Report format (typical 10-point capacitive):
    Report ID: 1 byte
    Contact count: 1 byte
    Per contact (10 bytes each):
        - Contact ID: 1 byte
        - Status (tip switch): 1 byte
        - X coordinate: 2 bytes (0-4095 or 0-65535)
        - Y coordinate: 2 bytes
        - Width: 2 bytes
        - Height/Pressure: 2 bytes

Supports both direct USB HID access and evdev (Linux input event) interfaces.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from pathlib import Path
from typing import Callable

logger = logging.getLogger("camry.display.touch")


class TouchInputHandler:
    """Parse USB multi-touch HID reports from head-unit touch panel."""

    # Common HID report descriptor sizes
    HID_REPORT_SIZE = 64  # bytes per USB interrupt transfer
    MAX_CONTACTS = 10

    def __init__(self, cfg: any = None) -> None:  # type: ignore[annotation-unchecked]
        self.cfg = cfg
        self._running = False
        self._callbacks: list[Callable] = []

        # Input method
        self._use_evdev = True  # prefer evdev over raw HID
        self._device_path: str | None = None
        self._evdev: any = None  # type: ignore[annotation-unchecked]

        # Calibration: map touch coordinates to display resolution
        self._touch_max_x = 4095
        self._touch_max_y = 4095
        self._display_width = 1024
        self._display_height = 600

    def register_callback(self, cb: Callable) -> None:
        """Register a callback receiving list of TouchPoint objects."""
        self._callbacks.append(cb)

    async def start(self) -> None:
        """Discover and open touch input device."""
        logger.info("Touch: discovering input device...")
        self._running = True

        # Try evdev first (modern Linux input subsystem)
        await self._start_evdev()

        if not self._evdev:
            # Fallback: try raw USB HID
            logger.warning("Touch: evdev not available, trying raw HID")
            await self._start_hid_raw()

    async def stop(self) -> None:
        """Close touch input device."""
        self._running = False
        if self._evdev:
            try:
                self._evdev.close()
            except Exception:
                pass
        logger.info("Touch: stopped")

    def is_active(self) -> bool:
        return self._evdev is not None or self._running

    # ── evdev path ──────────────────────────────────────────────────────────

    async def _start_evdev(self) -> None:
        """Find and open the head-unit touch device via evdev."""
        try:
            import evdev
            # Look for devices with "touch" or the head unit vendor in name
            devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
            for dev in devices:
                name_lower = dev.name.lower()
                if any(k in name_lower for k in ("touch", "hid", "multi")):
                    # Check for ABS_MT_POSITION_X capability
                    if evdev.ecodes.EV_ABS in dev.capabilities():
                        abs_caps = dev.capabilities()[evdev.ecodes.EV_ABS]
                        if evdev.ecodes.ABS_MT_POSITION_X in abs_caps:
                            self._device_path = dev.path
                            self._evdev = dev
                            logger.info("Touch: found device %s at %s", dev.name, dev.path)
                            # Start reading loop
                            asyncio.create_task(self._evdev_read_loop())
                            return
            logger.warning("Touch: no multi-touch evdev device found")
        except ImportError:
            logger.warning("Touch: evdev module not installed")
        except Exception as exc:
            logger.warning("Touch: evdev init failed: %s", exc)

    async def _evdev_read_loop(self) -> None:
        """Asyncio-friendly evdev event reading loop."""
        if not self._evdev:
            return
        import evdev

        # Track active contacts
        contacts: dict[int, dict[str, int]] = {}
        current_slot = 0

        try:
            # Grab exclusive access
            self._evdev.grab()
            logger.info("Touch: grabbed device %s", self._device_path)

            while self._running:
                # Use select for async-friendly reading
                readable, _, _ = await asyncio.to_thread(
                    lambda: __import__("select").select([self._evdev], [], [], 0.1)
                )
                if not readable:
                    continue

                for event in self._evdev.read():
                    if event.type == evdev.ecodes.EV_ABS:
                        code = event.code
                        value = event.value

                        if code == evdev.ecodes.ABS_MT_SLOT:
                            current_slot = value
                            if current_slot not in contacts:
                                contacts[current_slot] = {}

                        elif code == evdev.ecodes.ABS_MT_TRACKING_ID:
                            if value >= 0:
                                contacts[current_slot]["id"] = value
                                contacts[current_slot]["active"] = True
                            else:
                                # Contact lifted
                                if current_slot in contacts:
                                    contacts[current_slot]["active"] = False

                        elif code == evdev.ecodes.ABS_MT_POSITION_X:
                            contacts[current_slot]["x"] = value

                        elif code == evdev.ecodes.ABS_MT_POSITION_Y:
                            contacts[current_slot]["y"] = value

                        elif code == evdev.ecodes.ABS_MT_PRESSURE:
                            contacts[current_slot]["pressure"] = value

                    elif event.type == evdev.ecodes.EV_SYN:
                        # Synchronization event — emit all active contacts
                        points = self._build_touch_points(contacts)
                        if points:
                            self._emit(points)
                        # Clean up released contacts
                        contacts = {k: v for k, v in contacts.items() if v.get("active", False)}

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Touch: evdev read loop error")
        finally:
            try:
                self._evdev.ungrab()
            except Exception:
                pass

    # ── Raw HID path ────────────────────────────────────────────────────────

    async def _start_hid_raw(self) -> None:
        """Fallback: read raw HID reports from /dev/hidraw*."""
        try:
            hid_paths = list(Path("/dev").glob("hidraw*"))
            for hid_path in hid_paths:
                # Try to identify by reading a report
                try:
                    with open(hid_path, "rb") as f:
                        report = f.read(self.HID_REPORT_SIZE)
                        if len(report) >= 10 and report[0] == 0x01:
                            self._device_path = str(hid_path)
                            logger.info("Touch: using raw HID %s", hid_path)
                            asyncio.create_task(self._hid_read_loop(hid_path))
                            return
                except PermissionError:
                    continue
            logger.warning("Touch: no suitable hidraw device found")
        except Exception as exc:
            logger.warning("Touch: raw HID init failed: %s", exc)

    async def _hid_read_loop(self, hid_path: Path) -> None:
        """Read raw HID reports and parse multi-touch data."""
        try:
            with open(hid_path, "rb") as f:
                while self._running:
                    try:
                        report = await asyncio.to_thread(f.read, self.HID_REPORT_SIZE)
                        if len(report) < 3:
                            continue
                        points = self._parse_hid_report(report)
                        if points:
                            self._emit(points)
                    except Exception:
                        await asyncio.sleep(0.01)
        except Exception as exc:
            logger.error("Touch: HID read loop failed: %s", exc)

    def _parse_hid_report(self, report: bytes) -> list[dict[str, int]]:
        """Parse a multi-touch HID report into contact dictionaries."""
        points: list[dict[str, int]] = []
        if len(report) < 3:
            return points

        # Common format: report_id(1) + contact_count(1) + contacts...
        contact_count = report[1]
        offset = 2
        for _ in range(min(contact_count, self.MAX_CONTACTS)):
            if offset + 8 > len(report):
                break
            contact_id = report[offset]
            status = report[offset + 1]
            x = struct.unpack_from("<H", report, offset + 2)[0]
            y = struct.unpack_from("<H", report, offset + 4)[0]
            pressure = struct.unpack_from("<H", report, offset + 6)[0]

            if status & 0x01:  # tip switch
                points.append({
                    "id": contact_id,
                    "x": self._scale_x(x),
                    "y": self._scale_y(y),
                    "pressure": pressure,
                    "active": True,
                })
            offset += 8
        return points

    # ── Coordinate scaling ──────────────────────────────────────────────────

    def _scale_x(self, raw: int) -> int:
        """Scale raw touch X to display width."""
        return int(raw * self._display_width / self._touch_max_x)

    def _scale_y(self, raw: int) -> int:
        """Scale raw touch Y to display height."""
        return int(raw * self._display_height / self._touch_max_y)

    def _build_touch_points(self, contacts: dict[int, dict[str, int]]) -> list[dict[str, int]]:
        """Convert evdev contact dicts to TouchPoint-compatible dicts."""
        points: list[dict[str, int]] = []
        for slot, data in contacts.items():
            if data.get("active") and "x" in data and "y" in data:
                points.append({
                    "id": data.get("id", slot),
                    "x": self._scale_x(data["x"]),
                    "y": self._scale_y(data["y"]),
                    "pressure": data.get("pressure", 255),
                    "active": True,
                })
        return points

    def _emit(self, points: list[dict[str, int]]) -> None:
        """Forward touch points to all registered callbacks."""
        from pi_camry.display.joybring import TouchPoint, TouchEventType
        touch_points = [
            TouchPoint(
                id=p["id"],
                x=p["x"],
                y=p["y"],
                pressure=p["pressure"],
                event_type=TouchEventType.MOVE if p.get("active") else TouchEventType.UP,
            )
            for p in points
        ]
        for cb in self._callbacks:
            try:
                cb(touch_points)
            except Exception:
                logger.exception("Touch: callback error")
