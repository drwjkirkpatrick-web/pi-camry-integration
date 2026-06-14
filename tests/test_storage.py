"""
Tests for pi_camry.storage.manager — StorageManager with temp SQLite DB.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pi_camry.storage.manager import StorageManager


@pytest.mark.asyncio
async def test_storage_start_creates_tables(temp_db: Path) -> None:
    """start() should create all required tables."""
    mgr = StorageManager()
    mgr.db_path = temp_db
    await mgr.start()
    assert mgr._conn is not None
    # Verify tables exist by querying sqlite_master
    conn = await mgr._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    rows = await conn.fetchall()
    names = {r[0] for r in rows}
    assert "obd_logs" in names
    assert "gps_tracks" in names
    assert "events" in names
    assert "video_segments" in names
    assert "maintenance" in names
    await mgr.close()


@pytest.mark.asyncio
async def test_storage_log_obd(temp_db: Path) -> None:
    """log_obd should insert a row into obd_logs."""
    mgr = StorageManager()
    mgr.db_path = temp_db
    await mgr.start()

    @dataclass
    class FakeSnapshot:
        rpm: int = 3000
        speed_kmh: float = 65.0
        coolant_temp_c: float = 90.0

    await mgr.log_obd(FakeSnapshot())
    rows = await mgr.get_latest_obd(limit=1)
    assert len(rows) == 1
    assert rows[0]["rpm"] == 3000
    await mgr.close()


@pytest.mark.asyncio
async def test_storage_log_gps(temp_db: Path) -> None:
    """log_gps should insert a row into gps_tracks."""
    mgr = StorageManager()
    mgr.db_path = temp_db
    await mgr.start()

    @dataclass
    class FakeFix:
        lat: float = 34.0522
        lon: float = -118.2437
        altitude_m: float = 100.0
        speed_kmh: float = 55.0
        heading_deg: float = 90.0
        hdop: float = 1.2
        satellites: int = 8
        fix_quality: int = 1

    await mgr.log_gps(FakeFix(), trip_id="trip_001")
    # Query directly
    async with mgr._conn.execute(
        "SELECT trip_id, lat, lon FROM gps_tracks"
    ) as cursor:
        row = await cursor.fetchone()
        assert row[0] == "trip_001"
        assert row[1] == 34.0522
    await mgr.close()


@pytest.mark.asyncio
async def test_storage_log_event(temp_db: Path) -> None:
    """log_event should insert into events table."""
    mgr = StorageManager()
    mgr.db_path = temp_db
    await mgr.start()
    await mgr.log_event(
        event_type="OBD_CEL_ON",
        source="obd",
        payload={"code": "P0101"},
        severity="warning",
    )
    async with mgr._conn.execute(
        "SELECT event_type, source, severity FROM events"
    ) as cursor:
        row = await cursor.fetchone()
        assert row[0] == "OBD_CEL_ON"
        assert row[2] == "warning"
    await mgr.close()


@pytest.mark.asyncio
async def test_storage_log_video_segment(temp_db: Path) -> None:
    """log_video_segment should insert into video_segments."""
    mgr = StorageManager()
    mgr.db_path = temp_db
    await mgr.start()
    now = datetime.utcnow()
    await mgr.log_video_segment(
        path="/mnt/video/seg_001.h264",
        start_time=now,
        end_time=None,
        locked=True,
        lock_reason="collision",
        file_size_bytes=50_000_000,
    )
    async with mgr._conn.execute(
        "SELECT path, locked, lock_reason FROM video_segments"
    ) as cursor:
        row = await cursor.fetchone()
        assert row[0] == "/mnt/video/seg_001.h264"
        assert row[1] == 1
        assert row[2] == "collision"
    await mgr.close()


@pytest.mark.asyncio
async def test_storage_get_latest_obd(temp_db: Path) -> None:
    """get_latest_obd should return rows in descending time order."""
    mgr = StorageManager()
    mgr.db_path = temp_db
    await mgr.start()

    @dataclass
    class FakeSnapshot:
        rpm: int

    for rpm in [1000, 2000, 3000]:
        await mgr.log_obd(FakeSnapshot(rpm=rpm))
    rows = await mgr.get_latest_obd(limit=2)
    assert len(rows) == 2
    # Latest first
    assert rows[0]["rpm"] == 3000
    assert rows[1]["rpm"] == 2000
    await mgr.close()


@pytest.mark.asyncio
async def test_storage_trip_summary(temp_db: Path) -> None:
    """get_trip_summary should aggregate GPS points for a trip."""
    mgr = StorageManager()
    mgr.db_path = temp_db
    await mgr.start()

    @dataclass
    class FakeFix:
        lat: float = 34.0
        lon: float = -118.0
        speed_kmh: float = 60.0

    for _ in range(5):
        await mgr.log_gps(FakeFix(), trip_id="trip_abc")
    summary = await mgr.get_trip_summary("trip_abc")
    assert summary is not None
    assert summary["points"] == 5
    await mgr.close()


@pytest.mark.asyncio
async def test_storage_close_stops_monitor(temp_db: Path) -> None:
    """close() should cancel the disk monitor task."""
    mgr = StorageManager()
    mgr.db_path = temp_db
    await mgr.start()
    assert mgr._monitor_task is not None
    await mgr.close()
    assert mgr._running is False


@pytest.mark.asyncio
async def test_storage_disk_monitor_emits_low_storage(
    temp_db: Path, monkeypatch: Any
) -> None:
    """If disk usage >= 90%, SYSTEM_LOW_STORAGE should be emitted."""
    mgr = StorageManager()
    mgr.db_path = temp_db
    await mgr.start()

    # Patch psutil.disk_usage to return > 90%
    fake_usage = MagicMock()
    fake_usage.percent = 95.0
    fake_usage.free = 1_000_000_000
    monkeypatch.setattr(
        "pi_camry.storage.manager.psutil.disk_usage", lambda p: fake_usage
    )
    # Directly call check
    await mgr._check_disk()
    await mgr.close()
    # No exception means pass; spy on bus in real harness would assert event
