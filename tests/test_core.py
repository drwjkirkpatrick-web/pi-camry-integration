"""
Tests for pi_camry.core — EventBus and configuration.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from pi_camry.core import Event, EventBus, EventType, bus, setup_logging
from pi_camry.core.config import MainConfig, settings


# ═══════════════════════════════════════════════════════════════════════════════
# EventBus tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_event_bus_publish_subscribe(event_bus: EventBus) -> None:
    """Handlers receive published events with correct payload."""
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    event_bus.subscribe(EventType.OBD_CONNECTED, handler)
    await event_bus.publish(
        EventType.OBD_CONNECTED,
        payload={"protocol": "ISO9141_2"},
        source="test",
    )
    await asyncio.sleep(0.05)  # let async handler run
    assert len(received) == 1
    assert received[0].event_type == EventType.OBD_CONNECTED
    assert received[0].payload["protocol"] == "ISO9141_2"
    assert received[0].source == "test"


@pytest.mark.asyncio
async def test_event_bus_unsubscribe(event_bus: EventBus) -> None:
    """Unsubscribed handler must not receive events."""
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    event_bus.subscribe(EventType.GPS_FIX, handler)
    event_bus.unsubscribe(EventType.GPS_FIX, handler)
    await event_bus.publish(EventType.GPS_FIX, {"lat": 34.0})
    await asyncio.sleep(0.05)
    assert len(received) == 0


@pytest.mark.asyncio
async def test_event_bus_multiple_subscribers(event_bus: EventBus) -> None:
    """Multiple handlers for the same event type all get called."""
    results: list[str] = []

    async def handler_a(event: Event) -> None:
        results.append("a")

    async def handler_b(event: Event) -> None:
        results.append("b")

    event_bus.subscribe(EventType.IMU_COLLISION, handler_a)
    event_bus.subscribe(EventType.IMU_COLLISION, handler_b)
    await event_bus.publish(EventType.IMU_COLLISION, {"g_force": 3.5})
    await asyncio.sleep(0.05)
    assert sorted(results) == ["a", "b"]


@pytest.mark.asyncio
async def test_event_bus_sync_handler(event_bus: EventBus) -> None:
    """Sync callable handlers are supported too."""
    called = False

    def sync_handler(event: Event) -> None:
        nonlocal called
        called = True

    event_bus.subscribe(EventType.SYSTEM_SHUTDOWN, sync_handler)
    await event_bus.publish(EventType.SYSTEM_SHUTDOWN, {})
    await asyncio.sleep(0.05)
    assert called


@pytest.mark.asyncio
async def test_event_bus_handler_exception_does_not_crash(event_bus: EventBus) -> None:
    """A failing handler must not prevent other handlers from running."""
    results: list[str] = []

    async def bad_handler(event: Event) -> None:
        raise RuntimeError("boom")

    async def good_handler(event: Event) -> None:
        results.append("ok")

    event_bus.subscribe(EventType.OBD_PID_UPDATE, bad_handler)
    event_bus.subscribe(EventType.OBD_PID_UPDATE, good_handler)
    await event_bus.publish(EventType.OBD_PID_UPDATE, {"rpm": 3000})
    await asyncio.sleep(0.05)
    assert results == ["ok"]


@pytest.mark.asyncio
async def test_event_bus_no_subscribers_noop(event_bus: EventBus) -> None:
    """Publishing an event with no subscribers is harmless."""
    await event_bus.publish(EventType.CAMERA_MOTION_DETECTED, {})


# ═══════════════════════════════════════════════════════════════════════════════
# Config tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_config_defaults() -> None:
    """MainConfig should provide sensible defaults."""
    cfg = MainConfig()
    assert cfg.obd.port == "/dev/ttyUSB0"
    assert cfg.gps.baudrate == 9600
    assert cfg.imu.i2c_address == 0x68
    assert cfg.vehicle.year == 1996
    assert cfg.vehicle.make == "Toyota"


@pytest.mark.asyncio
async def test_config_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables with subsystem prefix override nested fields.

    Each subsystem config has its own env_prefix:
    - OBD_   for OBDConfig
    - GPS_   for GPSConfig
    - etc.
    """
    import importlib

    monkeypatch.setenv("OBD_PORT", "/dev/ttyOBD")
    monkeypatch.setenv("GPS_BAUDRATE", "115200")
    import pi_camry.core.config as config_mod
    importlib.reload(config_mod)
    cfg = config_mod.MainConfig()
    assert cfg.obd.port == "/dev/ttyOBD"
    assert cfg.gps.baudrate == 115200


@pytest.mark.asyncio
async def test_config_ensure_dirs(mock_config: MainConfig, tmp_path: Path) -> None:
    """ensure_dirs() should create required directories."""
    cfg = mock_config
    cfg.ensure_dirs()
    assert cfg.data_dir.exists()
    assert cfg.camera.video_dir.exists()
    assert cfg.storage.log_dir.exists()
    assert cfg.storage.sqlite_path.parent.exists()


@pytest.mark.asyncio
async def test_config_path_validator() -> None:
    """data_dir validator should accept strings and Paths."""
    cfg = MainConfig(data_dir="/tmp/camry_test")  # type: ignore[arg-type]
    assert isinstance(cfg.data_dir, Path)
    assert str(cfg.data_dir) == "/tmp/camry_test"


@pytest.mark.asyncio
async def test_global_bus_is_eventbus() -> None:
    """The module-level `bus` must be an EventBus instance."""
    assert isinstance(bus, EventBus)


@pytest.mark.asyncio
async def test_setup_logging_creates_file(mock_config: MainConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """setup_logging should create a log file under log_dir."""
    monkeypatch.setattr(
        "pi_camry.core.settings",
        mock_config,
    )
    setup_logging()
    log_files = list(mock_config.storage.log_dir.glob("camry_*.log"))
    assert len(log_files) >= 1
