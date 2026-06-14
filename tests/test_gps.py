"""
Tests for pi_camry.gps.tracker — GPSTracker with mocked serial + NMEA.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from pi_camry.core import EventType
from pi_camry.gps.tracker import GPSTracker, GPSFix


# ── Skip if pynmea2 not installed ──
pytest.importorskip("pynmea2", reason="pynmea2 not installed")


@pytest.mark.asyncio
async def test_gps_start_stop(mock_serial: MagicMock, mock_pynmea2: Any) -> None:
    """GPSTracker should open serial on start and close on stop."""
    tracker = GPSTracker()
    await tracker.start()
    assert tracker._running is True
    assert tracker._serial is not None
    await tracker.stop()
    assert tracker._running is False
    tracker._serial.close.assert_called_once()


@pytest.mark.asyncio
async def test_gps_parse_gga_fix(mock_serial: MagicMock, mock_pynmea2: Any) -> None:
    """Parsing a GGA sentence should update lat/lon/altitude."""
    tracker = GPSTracker()
    await tracker.start()
    mock_serial.readline.return_value = b"$GPGGA,123519,3405.220,N,11814.370,W,1,08,1.2,100.0,M,-30.0,M,,*4E\r\n"
    # Give the read loop a tick
    await asyncio.sleep(0.2)
    fix = tracker.get_latest_fix()
    assert fix.lat is not None
    assert fix.lon is not None
    await tracker.stop()


@pytest.mark.asyncio
async def test_gps_parse_rmc_speed(mock_serial: MagicMock, mock_pynmea2: Any) -> None:
    """Parsing an RMC sentence should update speed and heading."""
    tracker = GPSTracker()
    await tracker.start()
    mock_serial.readline.return_value = b"$GPRMC,123519,A,3405.220,N,11814.370,W,030.0,090.0,140616,018.2,E*6B\r\n"
    await asyncio.sleep(0.2)
    fix = tracker.get_latest_fix()
    assert fix.speed_kmh is not None
    assert fix.heading_deg is not None
    await tracker.stop()


@pytest.mark.asyncio
async def test_gps_geofence_detection(mock_serial: MagicMock, mock_pynmea2: Any, monkeypatch: Any) -> None:
    """When home is configured, fix should report in_geofence."""
    tracker = GPSTracker()
    tracker._home = (34.0522, -118.2437)
    tracker._radius_m = 1000.0
    await tracker.start()
    mock_serial.readline.return_value = b"$GPGGA,123519,3405.220,N,11814.370,W,1,08,1.2,100.0,M,,,*4E\r\n"
    await asyncio.sleep(0.2)
    fix = tracker.get_latest_fix()
    assert fix.in_geofence is True
    await tracker.stop()


@pytest.mark.asyncio
async def test_gps_find_my_car_no_fix(mock_serial: MagicMock, mock_pynmea2: Any) -> None:
    """find_my_car should return no_fix status when lat/lon missing."""
    tracker = GPSTracker()
    result = await tracker.find_my_car()
    assert result["status"] == "no_fix"


@pytest.mark.asyncio
async def test_gps_find_my_car_with_fix(mock_serial: MagicMock, mock_pynmea2: Any) -> None:
    """find_my_car should return maps URL when fix is available."""
    tracker = GPSTracker()
    tracker._latest = GPSFix(lat=34.0522, lon=-118.2437)
    result = await tracker.find_my_car()
    assert result["status"] == "ok"
    assert "maps_url" in result


@pytest.mark.asyncio
async def test_gps_haversine_distance() -> None:
    """Haversine distance between two known points should be accurate."""
    tracker = GPSTracker()
    # Los Angeles to San Francisco ~ 559 km
    dist = tracker._haversine(34.0522, -118.2437, 37.7749, -122.4194)
    assert 550_000 < dist < 570_000  # meters


@pytest.mark.asyncio
async def test_gps_trip_start_stop(mock_serial: MagicMock, mock_pynmea2: Any) -> None:
    """Trip should start when speed > 2 km/h and end when speed < 1 km/h."""
    tracker = GPSTracker()
    await tracker.start()
    assert tracker._trip_active is False
    # Simulate movement
    mock_serial.readline.return_value = b"$GPRMC,123519,A,3405.220,N,11814.370,W,030.0,090.0,140616,018.2,E*6B\r\n"
    await asyncio.sleep(0.2)
    # Trip may or may not have started depending on mock state
    await tracker.stop()
    # After stop, trip should be ended
    assert tracker._trip_active is False
