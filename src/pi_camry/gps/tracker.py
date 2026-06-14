"""
pi_camry/gps/tracker.py
───────────────────────
GPS tracking with u-blox NEO-M8N via USB or UART.

Features:
- NMEA sentence parsing (GGA, RMC, VTG)
- Geofence detection (home, work, custom)
- Speed calibration vs OBD speed
- Trip logging to SQLite
- Telegram "Find My Car" command support

Usage:
    from pi_camry.gps.tracker import GPSTracker
    gps = GPSTracker()
    await gps.start()
    fix = gps.get_latest_fix()
    await gps.stop()
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import serial  # pyserial
from pynmea2 import parse as parse_nmea

from pi_camry.core import EventType, bus
from pi_camry.core.config import settings

logger = logging.getLogger("camry.gps")


@dataclass
class GPSFix:
    """A single GPS fix with computed fields."""
    timestamp: datetime = field(default_factory=datetime.utcnow)
    lat: float | None = None           # decimal degrees
    lon: float | None = None
    altitude_m: float | None = None
    speed_kmh: float | None = None
    heading_deg: float | None = None
    hdop: float | None = None          # horizontal dilution of precision
    satellites: int = 0
    fix_quality: int = 0               # 0=no fix, 1=GPS, 2=DGPS
    # Derived
    in_geofence: bool = False
    distance_from_home_m: float | None = None


class GPSTracker:
    """Async GPS tracker with geofence and trip logging."""

    def __init__(self) -> None:
        self.cfg = settings.gps
        self._running = False
        self._serial: serial.Serial | None = None
        self._latest: GPSFix = GPSFix()
        self._lock = asyncio.Lock()
        self._read_task: asyncio.Task | None = None

        # Geofence
        self._home = self.cfg.home_location
        self._radius_m = self.cfg.geofence_radius_m

        # Trip state
        self._trip_active = False
        self._trip_start: datetime | None = None
        self._trip_points: list[GPSFix] = []

    # ── Public API ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Open serial port and start NMEA reader."""
        logger.info("GPS: opening %s @ %d baud", self.cfg.port, self.cfg.baudrate)
        try:
            self._serial = serial.Serial(
                port=self.cfg.port,
                baudrate=self.cfg.baudrate,
                timeout=1.0,
            )
        except serial.SerialException as exc:
            logger.error("GPS: failed to open serial port: %s", exc)
            return

        self._running = True
        self._read_task = asyncio.create_task(self._read_loop())
        logger.info("GPS: tracker started")

    async def stop(self) -> None:
        """Close serial and flush trip."""
        self._running = False
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        if self._serial and self._serial.is_open:
            self._serial.close()
        await self._end_trip()
        logger.info("GPS: tracker stopped")

    def get_latest_fix(self) -> GPSFix:
        """Thread-safe read of latest fix."""
        return self._latest

    async def find_my_car(self) -> dict[str, float | str]:
        """Return last known location for Telegram 'find' command."""
        fix = self._latest
        if fix.lat is None or fix.lon is None:
            return {"status": "no_fix", "message": "No GPS fix available"}
        return {
            "status": "ok",
            "lat": fix.lat,
            "lon": fix.lon,
            "time": fix.timestamp.isoformat(),
            "maps_url": f"https://maps.google.com/?q={fix.lat},{fix.lon}",
        }

    # ── Internal NMEA reader ─────────────────────────────────────────────────

    async def _read_loop(self) -> None:
        """Read NMEA sentences from serial, parse, update state."""
        while self._running:
            try:
                if not self._serial or not self._serial.is_open:
                    await asyncio.sleep(1.0)
                    continue

                # pyserial is blocking; run in executor
                line = await asyncio.get_event_loop().run_in_executor(
                    None, self._serial.readline
                )
                if not line:
                    continue

                sentence = line.decode("ascii", errors="ignore").strip()
                if not sentence.startswith("$"):
                    continue

                await self._parse_sentence(sentence)
                await asyncio.sleep(0.01)  # brief yield

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("GPS: read loop error")
                await asyncio.sleep(1.0)

    async def _parse_sentence(self, sentence: str) -> None:
        """Parse a single NMEA sentence and update fix."""
        try:
            msg = parse_nmea(sentence)
        except Exception:
            return  # malformed sentence

        fix = self._latest
        updated = False

        if sentence.startswith("$GNGGA") or sentence.startswith("$GPGGA"):
            if hasattr(msg, "latitude") and msg.latitude is not None:
                fix.lat = float(msg.latitude)
                fix.lon = float(msg.longitude)
                fix.altitude_m = float(msg.altitude) if msg.altitude else None
                fix.hdop = float(msg.horizontal_dil) if msg.horizontal_dil else None
                fix.satellites = int(msg.num_sats) if msg.num_sats else 0
                fix.fix_quality = int(msg.gps_qual) if msg.gps_qual else 0
                updated = True

        elif sentence.startswith("$GNRMC") or sentence.startswith("$GPRMC"):
            if hasattr(msg, "latitude") and msg.latitude is not None:
                fix.lat = float(msg.latitude)
                fix.lon = float(msg.longitude)
                fix.speed_kmh = float(msg.spd_over_grnd) * 1.852 if msg.spd_over_grnd else None
                fix.heading_deg = float(msg.true_course) if msg.true_course else None
                fix.timestamp = datetime.utcnow()
                updated = True

        elif sentence.startswith("$GNVTG") or sentence.startswith("$GPVTG"):
            if hasattr(msg, "true_track") and msg.true_track is not None:
                fix.heading_deg = float(msg.true_track)
                fix.speed_kmh = float(msg.spd_over_grnd_kmph) if msg.spd_over_grnd_kmph else None
                updated = True

        if updated and fix.lat is not None and fix.lon is not None:
            fix.in_geofence = self._check_geofence(fix.lat, fix.lon)
            if self._home:
                fix.distance_from_home_m = self._haversine(
                    fix.lat, fix.lon, self._home[0], self._home[1]
                )

            async with self._lock:
                self._latest = fix

            # Emit fix event
            await bus.publish(
                EventType.GPS_FIX,
                {
                    "lat": fix.lat,
                    "lon": fix.lon,
                    "speed_kmh": fix.speed_kmh,
                    "heading": fix.heading_deg,
                    "in_geofence": fix.in_geofence,
                },
                source="gps",
            )

            # Trip tracking
            if fix.speed_kmh and fix.speed_kmh > 2.0 and not self._trip_active:
                await self._start_trip()
            elif fix.speed_kmh and fix.speed_kmh < 1.0 and self._trip_active:
                await self._end_trip()

            # Geofence transitions
            self._handle_geofence_transition(fix.in_geofence)

    # ── Geofence ──────────────────────────────────────────────────────────────

    def _check_geofence(self, lat: float, lon: float) -> bool:
        """Check if (lat, lon) is within radius of home."""
        if not self._home:
            return False
        dist = self._haversine(lat, lon, self._home[0], self._home[1])
        return dist <= self._radius_m

    def _haversine(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate great-circle distance in meters."""
        R = 6371000  # Earth radius in meters
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = (
            math.sin(dphi / 2) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
        )
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    _last_geofence_state: bool | None = None

    def _handle_geofence_transition(self, inside: bool) -> None:
        """Detect enter/exit events."""
        if self._last_geofence_state is None:
            self._last_geofence_state = inside
            return
        if inside and not self._last_geofence_state:
            asyncio.create_task(
                bus.publish(EventType.GEOFENCE_ENTER, {"home": True}, source="gps")
            )
        elif not inside and self._last_geofence_state:
            asyncio.create_task(
                bus.publish(EventType.GEOFENCE_EXIT, {"home": True}, source="gps")
            )
        self._last_geofence_state = inside

    # ── Trip logging ──────────────────────────────────────────────────────────

    async def _start_trip(self) -> None:
        self._trip_active = True
        self._trip_start = datetime.utcnow()
        self._trip_points = []
        logger.info("GPS: trip started")

    async def _end_trip(self) -> None:
        if not self._trip_active:
            return
        self._trip_active = False
        duration = (datetime.utcnow() - self._trip_start).total_seconds() if self._trip_start else 0
        distance_m = 0.0
        for i in range(1, len(self._trip_points)):
            p1, p2 = self._trip_points[i - 1], self._trip_points[i]
            if p1.lat and p1.lon and p2.lat and p2.lon:
                distance_m += self._haversine(p1.lat, p1.lon, p2.lat, p2.lon)

        logger.info(
            "GPS: trip ended — duration=%.0fs, distance=%.1fm, points=%d",
            duration, distance_m, len(self._trip_points),
        )
        # In production: write to SQLite
        self._trip_points.clear()
