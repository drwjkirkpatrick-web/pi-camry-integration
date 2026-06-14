"""
pi_camry/obd/interface.py
─────────────────────────
OBD-II interface for 1996 Toyota Camry via ELM327 USB adapter.

Handles:
- Connection to ISO 9141-2 / KWP2000 (pre-CAN protocols)
- Continuous PID polling in a background thread
- DTC (trouble code) read/clear
- Event emission on the async event bus
- Automatic reconnection with backoff

Usage:
    from pi_camry.obd.interface import OBDInterface
    obd = OBDInterface()
    await obd.start()   # background polling begins
    await obd.stop()
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import obd  # python-obd library

from pi_camry.core import EventType, bus
from pi_camry.core.config import settings

logger = logging.getLogger("camry.obd")


# ──────────────────────────────────────────────────────────────────────────────
# PID definitions for 1996 Camry (5S-FE / 1MZ-FE)
# ──────────────────────────────────────────────────────────────────────────────

# Standard OBD-II PIDs that work on 1996 Toyota
ESSENTIAL_PIDS: list[obd.commands.Command] = [
    obd.commands.RPM,
    obd.commands.SPEED,
    obd.commands.COOLANT_TEMP,
    obd.commands.INTAKE_TEMP,
    obd.commands.THROTTLE_POS,
    obd.commands.ENGINE_LOAD,
    obd.commands.MAF,
    obd.commands.TIMING_ADVANCE,
    obd.commands.FUEL_LEVEL,       # may not respond on all '96 Toyotas
    obd.commands.BAROMETRIC_PRESSURE,
    obd.commands.O2_B1S1,         # pre-cat O2 voltage
    obd.commands.O2_B1S2,         # post-cat O2 voltage (if equipped)
    obd.commands.FUEL_TRIM_BANK1_SHORT_TERM,
    obd.commands.FUEL_TRIM_BANK1_LONG_TERM,
    obd.commands.RUN_TIME,
]

# Extended PIDs polled less frequently
EXTENDED_PIDS: list[obd.commands.Command] = [
    obd.commands.FUEL_PRESSURE,
    obd.commands.INTAKE_PRESSURE,
    obd.commands.AMBIANT_AIR_TEMP,
]


@dataclass
class PIDSnapshot:
    """A single frame of OBD data."""
    timestamp: datetime = field(default_factory=datetime.utcnow)
    rpm: int | None = None
    speed_kmh: float | None = None
    coolant_temp_c: float | None = None
    intake_temp_c: float | None = None
    throttle_percent: float | None = None
    engine_load_percent: float | None = None
    maf_gps: float | None = None
    timing_advance_deg: float | None = None
    fuel_level_percent: float | None = None
    barometric_pressure_kpa: float | None = None
    o2_b1s1_voltage: float | None = None
    o2_b1s2_voltage: float | None = None
    stft_percent: float | None = None
    ltft_percent: float | None = None
    run_time_sec: int | None = None
    # Extended
    fuel_pressure_kpa: float | None = None
    intake_pressure_kpa: float | None = None
    ambient_temp_c: float | None = None
    # Derived
    mpg_instant: float | None = None  # calculated from MAF + speed


class OBDInterface:
    """Async-friendly OBD-II interface with background polling."""

    def __init__(self) -> None:
        self.cfg = settings.obd
        self.connection: obd.Async | None = None
        self._running = False
        self._poll_thread: threading.Thread | None = None
        self._latest: PIDSnapshot = PIDSnapshot()
        self._lock = threading.Lock()
        self._last_poll = 0.0
        self._poll_interval = 0.5  # seconds between PID batches
        self._extended_interval = 10.0  # seconds between extended PID polls
        self._last_extended = 0.0
        self._dtc_history: list[str] = []

    # ── Public API ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect and begin background polling."""
        logger.info("OBD: connecting to %s", self.cfg.port)
        try:
            self.connection = obd.Async(
                self.cfg.port,
                baudrate=self.cfg.baudrate,
                protocol=self.cfg.protocol if self.cfg.protocol != "AUTO" else None,
                timeout=self.cfg.timeout,
            )
        except Exception as exc:
            logger.error("OBD connection failed: %s", exc)
            # Emit disconnected so other modules know
            await bus.publish(EventType.OBD_DISCONNECTED, {"error": str(exc)}, source="obd")
            return

        if not self.connection.is_connected():
            logger.error("OBD: ELM327 connected but no car protocol established")
            await bus.publish(EventType.OBD_DISCONNECTED, {}, source="obd")
            return

        proto = self.connection.protocol_id()
        logger.info("OBD: connected, protocol=%s", proto)
        await bus.publish(
            EventType.OBD_CONNECTED,
            {"protocol": proto, "port": self.cfg.port},
            source="obd",
        )

        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    async def stop(self) -> None:
        """Stop polling and close connection."""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5.0)
        if self.connection:
            self.connection.close()
            self.connection = None
        logger.info("OBD: stopped")

    def get_latest(self) -> PIDSnapshot:
        """Thread-safe read of latest snapshot."""
        with self._lock:
            return PIDSnapshot(
                timestamp=self._latest.timestamp,
                rpm=self._latest.rpm,
                speed_kmh=self._latest.speed_kmh,
                coolant_temp_c=self._latest.coolant_temp_c,
                intake_temp_c=self._latest.intake_temp_c,
                throttle_percent=self._latest.throttle_percent,
                engine_load_percent=self._latest.engine_load_percent,
                maf_gps=self._latest.maf_gps,
                timing_advance_deg=self._latest.timing_advance_deg,
                fuel_level_percent=self._latest.fuel_level_percent,
                barometric_pressure_kpa=self._latest.barometric_pressure_kpa,
                o2_b1s1_voltage=self._latest.o2_b1s1_voltage,
                o2_b1s2_voltage=self._latest.o2_b1s2_voltage,
                stft_percent=self._latest.stft_percent,
                ltft_percent=self._latest.ltft_percent,
                run_time_sec=self._latest.run_time_sec,
                fuel_pressure_kpa=self._latest.fuel_pressure_kpa,
                intake_pressure_kpa=self._latest.intake_pressure_kpa,
                ambient_temp_c=self._latest.ambient_temp_c,
                mpg_instant=self._latest.mpg_instant,
            )

    async def read_dtcs(self) -> list[str]:
        """Read diagnostic trouble codes from ECU."""
        if not self.connection or not self.connection.is_connected():
            logger.warning("OBD: not connected, cannot read DTCs")
            return []
        # python-obd synchronous call
        resp = self.connection.query(obd.commands.GET_DTC)
        codes: list[str] = []
        if resp.value:
            for code, desc in resp.value:
                codes.append(f"{code}: {desc}")
                await bus.publish(
                    EventType.OBD_TROUBLE_CODE,
                    {"code": code, "description": desc},
                    source="obd",
                )
        self._dtc_history.extend(codes)
        return codes

    async def clear_dtcs(self) -> bool:
        """Clear all DTCs and turn off CEL."""
        if not self.connection or not self.connection.is_connected():
            return False
        self.connection.query(obd.commands.CLEAR_DTC)
        await bus.publish(EventType.OBD_CEL_OFF, {}, source="obd")
        self._dtc_history.clear()
        logger.info("OBD: DTCs cleared")
        return True

    def is_connected(self) -> bool:
        return self.connection is not None and self.connection.is_connected()

    # ── Internal polling loop ───────────────────────────────────────────────

    def _poll_loop(self) -> None:
        """Background thread: poll PIDs continuously."""
        logger.info("OBD: poll thread started")
        while self._running:
            try:
                if not self.connection or not self.connection.is_connected():
                    logger.warning("OBD: lost connection, attempting reconnect...")
                    self._reconnect()
                    time.sleep(self.cfg.reconnect_interval)
                    continue

                self._poll_essential()

                now = time.monotonic()
                if now - self._last_extended >= self._extended_interval:
                    self._poll_extended()
                    self._last_extended = now

                # Check for CEL
                self._check_cel()

                time.sleep(self._poll_interval)
            except Exception:
                logger.exception("OBD poll loop error")
                time.sleep(self._poll_interval)

    def _poll_essential(self) -> None:
        """Query all essential PIDs and update snapshot."""
        snap = PIDSnapshot()
        for cmd in ESSENTIAL_PIDS:
            if not self.connection:
                break
            resp = self.connection.query(cmd)
            if resp.is_null():
                continue
            val = resp.value
            # Map response to snapshot fields
            if cmd == obd.commands.RPM and val is not None:
                snap.rpm = int(val.magnitude) if hasattr(val, "magnitude") else int(val)
            elif cmd == obd.commands.SPEED and val is not None:
                snap.speed_kmh = float(val.magnitude) if hasattr(val, "magnitude") else float(val)
            elif cmd == obd.commands.COOLANT_TEMP and val is not None:
                snap.coolant_temp_c = float(val.magnitude)
            elif cmd == obd.commands.INTAKE_TEMP and val is not None:
                snap.intake_temp_c = float(val.magnitude)
            elif cmd == obd.commands.THROTTLE_POS and val is not None:
                snap.throttle_percent = float(val.magnitude)
            elif cmd == obd.commands.ENGINE_LOAD and val is not None:
                snap.engine_load_percent = float(val.magnitude)
            elif cmd == obd.commands.MAF and val is not None:
                snap.maf_gps = float(val.magnitude)
            elif cmd == obd.commands.TIMING_ADVANCE and val is not None:
                snap.timing_advance_deg = float(val.magnitude)
            elif cmd == obd.commands.FUEL_LEVEL and val is not None:
                snap.fuel_level_percent = float(val.magnitude)
            elif cmd == obd.commands.BAROMETRIC_PRESSURE and val is not None:
                snap.barometric_pressure_kpa = float(val.magnitude)
            elif cmd == obd.commands.O2_B1S1 and val is not None:
                snap.o2_b1s1_voltage = float(val.magnitude)
            elif cmd == obd.commands.O2_B1S2 and val is not None:
                snap.o2_b1s2_voltage = float(val.magnitude)
            elif cmd == obd.commands.FUEL_TRIM_BANK1_SHORT_TERM and val is not None:
                snap.stft_percent = float(val.magnitude)
            elif cmd == obd.commands.FUEL_TRIM_BANK1_LONG_TERM and val is not None:
                snap.ltft_percent = float(val.magnitude)
            elif cmd == obd.commands.RUN_TIME and val is not None:
                snap.run_time_sec = int(val.magnitude)

        # Derived: instant MPG (US) from MAF + speed
        if snap.maf_gps and snap.speed_kmh and snap.speed_kmh > 0:
            # Simplified: MPG = (14.7 * 6.17 * 3600 * speed_mph) / (MAF * 454)
            # Using metric: L/100km = (MAF * 3600) / (air_fuel_ratio * fuel_density * speed)
            # Simplified heuristic for display
            speed_mph = snap.speed_kmh * 0.621371
            if snap.maf_gps > 0:
                mpg = (speed_mph * 3600) / (snap.maf_gps * 2.83)
                snap.mpg_instant = round(mpg, 1)

        with self._lock:
            self._latest = snap

        # Emit event
        asyncio.run_coroutine_threadsafe(
            bus.publish(
                EventType.OBD_PID_UPDATE,
                {
                    "rpm": snap.rpm,
                    "speed_kmh": snap.speed_kmh,
                    "coolant_c": snap.coolant_temp_c,
                    "throttle": snap.throttle_percent,
                    "mpg": snap.mpg_instant,
                },
                source="obd",
            ),
            asyncio.get_event_loop(),
        )

    def _poll_extended(self) -> None:
        """Poll extended PIDs less frequently."""
        if not self.connection:
            return
        for cmd in EXTENDED_PIDS:
            resp = self.connection.query(cmd)
            if resp.is_null():
                continue
            val = resp.value
            with self._lock:
                if cmd == obd.commands.FUEL_PRESSURE and val is not None:
                    self._latest.fuel_pressure_kpa = float(val.magnitude)
                elif cmd == obd.commands.INTAKE_PRESSURE and val is not None:
                    self._latest.intake_pressure_kpa = float(val.magnitude)
                elif cmd == obd.commands.AMBIANT_AIR_TEMP and val is not None:
                    self._latest.ambient_temp_c = float(val.magnitude)

    def _check_cel(self) -> None:
        """Check if CEL (MIL) is on."""
        if not self.connection:
            return
        resp = self.connection.query(obd.commands.STATUS)
        if resp.is_null():
            return
        mil_on = False
        try:
            # python-obd status object
            mil_on = resp.value.MIL  # type: ignore[attr-defined]
        except Exception:
            pass
        if mil_on:
            asyncio.run_coroutine_threadsafe(
                bus.publish(EventType.OBD_CEL_ON, {}, source="obd"),
                asyncio.get_event_loop(),
            )

    def _reconnect(self) -> None:
        """Attempt to re-establish OBD connection."""
        try:
            if self.connection:
                self.connection.close()
            self.connection = obd.Async(
                self.cfg.port,
                baudrate=self.cfg.baudrate,
                protocol=self.cfg.protocol if self.cfg.protocol != "AUTO" else None,
                timeout=self.cfg.timeout,
            )
            if self.connection.is_connected():
                logger.info("OBD: reconnected")
        except Exception as exc:
            logger.error("OBD reconnect failed: %s", exc)
            self.connection = None
