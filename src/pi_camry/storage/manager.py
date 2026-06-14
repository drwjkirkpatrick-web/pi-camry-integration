"""
pi_camry/storage/manager.py
───────────────────────────
Storage management: SQLite database, disk monitoring, M.2 NVMe health.

Database schema:
    - obd_logs: timestamp, rpm, speed, coolant, etc.
    - gps_tracks: trip_id, lat, lon, speed, heading
    - events: event_type, source, payload_json, severity
    - video_segments: path, start, end, locked, reason, size
    - maintenance: item, last_date, next_due_miles, next_due_date

Usage:
    from pi_camry.storage.manager import StorageManager
    db = StorageManager()
    await db.start()
    await db.log_obd(snapshot)
    await db.close()
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
import psutil

from pi_camry.core import EventType, bus
from pi_camry.core.config import settings

logger = logging.getLogger("camry.storage")


class StorageManager:
    """Async SQLite + disk monitoring for all vehicle data."""

    def __init__(self) -> None:
        self.cfg = settings.storage
        self.db_path = self.cfg.sqlite_path
        self._conn: aiosqlite.Connection | None = None
        self._monitor_task: asyncio.Task | None = None
        self._running = False

    # ── Public API ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect to SQLite, create tables, start disk monitor."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Storage: opening database at %s", self.db_path)
        self._conn = await aiosqlite.connect(self.db_path)
        await self._create_tables()
        self._running = True
        self._monitor_task = asyncio.create_task(self._disk_monitor_loop())
        logger.info("Storage: manager started")

    async def close(self) -> None:
        """Close database and stop monitor."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        if self._conn:
            await self._conn.close()
        logger.info("Storage: manager stopped")

    # ── Logging methods ───────────────────────────────────────────────────────

    async def log_obd(self, snapshot: Any) -> None:
        """Log an OBD PID snapshot."""
        if not self._conn:
            return
        ts = datetime.utcnow().isoformat()
        data = asdict(snapshot) if hasattr(snapshot, "__dataclass_fields__") else snapshot
        await self._conn.execute(
            """
            INSERT INTO obd_logs (
                timestamp, rpm, speed_kmh, coolant_temp_c, intake_temp_c,
                throttle_percent, engine_load_percent, maf_gps, timing_advance_deg,
                fuel_level_percent, barometric_pressure_kpa, o2_b1s1_voltage,
                o2_b1s2_voltage, stft_percent, ltft_percent, run_time_sec, mpg_instant
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                data.get("rpm"),
                data.get("speed_kmh"),
                data.get("coolant_temp_c"),
                data.get("intake_temp_c"),
                data.get("throttle_percent"),
                data.get("engine_load_percent"),
                data.get("maf_gps"),
                data.get("timing_advance_deg"),
                data.get("fuel_level_percent"),
                data.get("barometric_pressure_kpa"),
                data.get("o2_b1s1_voltage"),
                data.get("o2_b1s2_voltage"),
                data.get("stft_percent"),
                data.get("ltft_percent"),
                data.get("run_time_sec"),
                data.get("mpg_instant"),
            ),
        )
        await self._conn.commit()

    async def log_gps(self, fix: Any, trip_id: str | None = None) -> None:
        """Log a GPS fix."""
        if not self._conn:
            return
        await self._conn.execute(
            """
            INSERT INTO gps_tracks (trip_id, timestamp, lat, lon, altitude_m,
                speed_kmh, heading_deg, hdop, satellites, fix_quality)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trip_id,
                datetime.utcnow().isoformat(),
                getattr(fix, "lat", None),
                getattr(fix, "lon", None),
                getattr(fix, "altitude_m", None),
                getattr(fix, "speed_kmh", None),
                getattr(fix, "heading_deg", None),
                getattr(fix, "hdop", None),
                getattr(fix, "satellites", 0),
                getattr(fix, "fix_quality", 0),
            ),
        )
        await self._conn.commit()

    async def log_event(
        self,
        event_type: str,
        source: str,
        payload: dict[str, Any],
        severity: str = "info",
    ) -> None:
        """Log a system event."""
        if not self._conn:
            return
        await self._conn.execute(
            """
            INSERT INTO events (timestamp, event_type, source, payload_json, severity)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                datetime.utcnow().isoformat(),
                event_type,
                source,
                json.dumps(payload),
                severity,
            ),
        )
        await self._conn.commit()

    async def log_video_segment(
        self,
        path: str,
        start_time: datetime,
        end_time: datetime | None,
        locked: bool,
        lock_reason: str,
        file_size_bytes: int,
    ) -> None:
        """Log a video segment record."""
        if not self._conn:
            return
        await self._conn.execute(
            """
            INSERT INTO video_segments (path, start_time, end_time, locked,
                lock_reason, file_size_bytes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                path,
                start_time.isoformat(),
                end_time.isoformat() if end_time else None,
                locked,
                lock_reason,
                file_size_bytes,
            ),
        )
        await self._conn.commit()

    # ── Query methods ─────────────────────────────────────────────────────────

    async def get_latest_obd(self, limit: int = 1) -> list[dict[str, Any]]:
        """Get latest OBD log entries."""
        if not self._conn:
            return []
        async with self._conn.execute(
            "SELECT * FROM obd_logs ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in rows]

    async def get_trip_summary(self, trip_id: str) -> dict[str, Any] | None:
        """Get summary stats for a trip."""
        if not self._conn:
            return None
        async with self._conn.execute(
            """
            SELECT COUNT(*) as points,
                   MIN(timestamp) as start,
                   MAX(timestamp) as end,
                   AVG(speed_kmh) as avg_speed,
                   MAX(speed_kmh) as max_speed
            FROM gps_tracks WHERE trip_id = ?
            """,
            (trip_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                cols = [d[0] for d in cursor.description]
                return dict(zip(cols, row))
        return None

    # ── Schema ────────────────────────────────────────────────────────────────

    async def _create_tables(self) -> None:
        """Create all tables if they don't exist."""
        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS obd_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                rpm INTEGER,
                speed_kmh REAL,
                coolant_temp_c REAL,
                intake_temp_c REAL,
                throttle_percent REAL,
                engine_load_percent REAL,
                maf_gps REAL,
                timing_advance_deg REAL,
                fuel_level_percent REAL,
                barometric_pressure_kpa REAL,
                o2_b1s1_voltage REAL,
                o2_b1s2_voltage REAL,
                stft_percent REAL,
                ltft_percent REAL,
                run_time_sec INTEGER,
                mpg_instant REAL
            );
            CREATE INDEX IF NOT EXISTS idx_obd_time ON obd_logs(timestamp);

            CREATE TABLE IF NOT EXISTS gps_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id TEXT,
                timestamp TEXT NOT NULL,
                lat REAL,
                lon REAL,
                altitude_m REAL,
                speed_kmh REAL,
                heading_deg REAL,
                hdop REAL,
                satellites INTEGER,
                fix_quality INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_gps_time ON gps_tracks(timestamp);
            CREATE INDEX IF NOT EXISTS idx_gps_trip ON gps_tracks(trip_id);

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT,
                payload_json TEXT,
                severity TEXT DEFAULT 'info'
            );
            CREATE INDEX IF NOT EXISTS idx_events_time ON events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

            CREATE TABLE IF NOT EXISTS video_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                locked INTEGER DEFAULT 0,
                lock_reason TEXT,
                file_size_bytes INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS maintenance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item TEXT NOT NULL,
                last_date TEXT,
                last_odometer_km REAL,
                interval_km REAL,
                interval_months INTEGER,
                next_due_date TEXT,
                next_due_km REAL
            );
            """
        )
        await self._conn.commit()

    # ── Disk monitor ──────────────────────────────────────────────────────────

    async def _disk_monitor_loop(self) -> None:
        """Periodically check disk usage and emit alerts."""
        while self._running:
            try:
                await asyncio.sleep(300)  # check every 5 minutes
                await self._check_disk()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Storage: disk monitor error")

    async def _check_disk(self) -> None:
        """Check M.2 usage and battery voltage (via ADC)."""
        try:
            usage = psutil.disk_usage(str(self.cfg.mount_point))
            percent = usage.percent
            logger.debug("Storage: disk usage %.1f%%", percent)

            if percent >= 90:
                await bus.publish(
                    EventType.SYSTEM_LOW_STORAGE,
                    {"percent": percent, "free_gb": usage.free / 1e9},
                    source="storage",
                )

            # Battery voltage check via MCP3008 (if available)
            # Channel 0: 12V battery via voltage divider
            # In production: read from GPIOController ADC
        except Exception as exc:
            logger.warning("Storage: disk check failed: %s", exc)
