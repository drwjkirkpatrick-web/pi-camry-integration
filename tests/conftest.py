"""
Shared pytest fixtures for pi-camry-integration test suite.
All hardware dependencies are mocked so tests run in CI without a Pi.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pi_camry.core import EventBus, EventType
from pi_camry.core.config import MainConfig


def pytest_configure(config: pytest.Config) -> None:
    """Pre-mock hardware-dependent modules before test collection."""
    import sys

    # Mock obd module so pi_camry.obd.interface can be imported in tests
    if "obd" not in sys.modules:
        mock_obd = MagicMock()
        mock_obd.Async = MagicMock()
        mock_obd.commands = MagicMock()
        mock_obd.commands.RPM = MagicMock()
        mock_obd.commands.SPEED = MagicMock()
        mock_obd.commands.COOLANT_TEMP = MagicMock()
        mock_obd.commands.INTAKE_TEMP = MagicMock()
        mock_obd.commands.THROTTLE_POS = MagicMock()
        mock_obd.commands.ENGINE_LOAD = MagicMock()
        mock_obd.commands.MAF = MagicMock()
        mock_obd.commands.TIMING_ADVANCE = MagicMock()
        mock_obd.commands.FUEL_LEVEL = MagicMock()
        mock_obd.commands.BAROMETRIC_PRESSURE = MagicMock()
        mock_obd.commands.O2_B1S1 = MagicMock()
        mock_obd.commands.O2_B1S2 = MagicMock()
        mock_obd.commands.FUEL_TRIM_BANK1_SHORT_TERM = MagicMock()
        mock_obd.commands.FUEL_TRIM_BANK1_LONG_TERM = MagicMock()
        mock_obd.commands.RUN_TIME = MagicMock()
        mock_obd.commands.FUEL_PRESSURE = MagicMock()
        mock_obd.commands.INTAKE_PRESSURE = MagicMock()
        mock_obd.commands.AMBIANT_AIR_TEMP = MagicMock()
        mock_obd.commands.GET_DTC = MagicMock()
        mock_obd.commands.CLEAR_DTC = MagicMock()
        mock_obd.commands.STATUS = MagicMock()
        sys.modules["obd"] = mock_obd

    # Mock lgpio so pi_camry.gpio.controller can be imported
    if "lgpio" not in sys.modules:
        mock_lgpio = MagicMock()
        mock_lgpio.SET_PULL_DOWN = 1
        sys.modules["lgpio"] = mock_lgpio

    # Mock pyaudio so pi_camry.audio.assistant can be imported
    if "pyaudio" not in sys.modules:
        mock_pyaudio = MagicMock()
        sys.modules["pyaudio"] = mock_pyaudio

    # Mock picamera2 so pi_camry.camera.recorder can be imported
    if "picamera2" not in sys.modules:
        sys.modules["picamera2"] = MagicMock()

    # Mock smbus2 so pi_camry.imu.sensor can be imported
    if "smbus2" not in sys.modules:
        mock_smbus2_mod = MagicMock()
        mock_smbus2_mod.SMBus = MagicMock
        sys.modules["smbus2"] = mock_smbus2_mod

    # Mock serial so pi_camry.gps.tracker can be imported
    if "serial" not in sys.modules:
        sys.modules["serial"] = MagicMock()

    # Mock pynmea2 so pi_camry.gps.tracker can be imported
    if "pynmea2" not in sys.modules:
        sys.modules["pynmea2"] = MagicMock()

    # Mock telegram so pi_camry.telegram.bot can be imported
    if "telegram" not in sys.modules:
        sys.modules["telegram"] = MagicMock()
        sys.modules["telegram.ext"] = MagicMock()


# ──────────────────────────────────────────────────────────────────────────────
# Event bus fixture
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def event_bus() -> EventBus:
    """Fresh EventBus instance for each test."""
    return EventBus()


@pytest.fixture
def captured_events(event_bus: EventBus) -> list:
    """EventBus with a catch-all subscriber that records every event."""
    events: list = []

    async def _catch(event: Any) -> None:
        events.append(event)

    for et in EventType:
        event_bus.subscribe(et, _catch)
    return events


# ──────────────────────────────────────────────────────────────────────────────
# Config fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MainConfig:
    """Return a MainConfig with all paths redirected to tmp_path."""
    cfg = MainConfig()
    cfg.data_dir = tmp_path / "data"
    cfg.camera.video_dir = tmp_path / "video"
    cfg.storage.log_dir = tmp_path / "logs"
    cfg.storage.sqlite_path = tmp_path / "camry.db"
    cfg.storage.mount_point = tmp_path / "mnt"
    cfg.storage.video_partition = tmp_path / "video"
    cfg.storage.luks_keyfile = tmp_path / "luks.key"
    cfg.camera.encryption_key_path = tmp_path / "video.key"
    cfg.telegram.bot_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    cfg.telegram.allowed_chat_ids = [123456789]
    return cfg


@pytest.fixture
def mock_config_factory(mock_config: MainConfig) -> Generator:
    """Yield a factory that returns fresh copies of mock_config."""
    import copy

    def _factory() -> MainConfig:
        return copy.deepcopy(mock_config)

    yield _factory


# ──────────────────────────────────────────────────────────────────────────────
# Temporary database fixture
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Path to a temporary SQLite database file."""
    db_path = tmp_path / "test_camry.db"
    return db_path


@pytest.fixture
def temp_db_conn(temp_db: Path) -> Generator[sqlite3.Connection, None, None]:
    """Open and yield a sqlite3 connection to temp_db, close on teardown."""
    conn = sqlite3.connect(str(temp_db))
    yield conn
    conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# OBD mock fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_obd_connection(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock the `obd` module so OBDInterface can start without hardware."""
    import sys
    mock_obd_mod = sys.modules.get("obd")
    if mock_obd_mod is None:
        mock_obd_mod = MagicMock()
        sys.modules["obd"] = mock_obd_mod

    mock_async_conn = MagicMock()
    mock_async_conn.is_connected.return_value = True
    mock_async_conn.protocol_id.return_value = "ISO9141_2"
    mock_async_conn.query.return_value = MagicMock(is_null=lambda: True, value=None)
    mock_async_conn.close = MagicMock()
    mock_obd_mod.Async.return_value = mock_async_conn

    # Ensure commands exist as distinct mock objects
    for cmd_name in [
        "RPM", "SPEED", "COOLANT_TEMP", "INTAKE_TEMP", "THROTTLE_POS",
        "ENGINE_LOAD", "MAF", "TIMING_ADVANCE", "FUEL_LEVEL",
        "BAROMETRIC_PRESSURE", "O2_B1S1", "O2_B1S2",
        "FUEL_TRIM_BANK1_SHORT_TERM", "FUEL_TRIM_BANK1_LONG_TERM", "RUN_TIME",
        "FUEL_PRESSURE", "INTAKE_PRESSURE", "AMBIANT_AIR_TEMP",
        "GET_DTC", "CLEAR_DTC", "STATUS",
    ]:
        if not hasattr(mock_obd_mod.commands, cmd_name):
            setattr(mock_obd_mod.commands, cmd_name, MagicMock())

    monkeypatch.setattr("pi_camry.obd.interface.obd", mock_obd_mod)
    return mock_async_conn


@pytest.fixture
def mock_obd_response_factory() -> Generator:
    """Factory for building mock OBD responses with magnitude values."""

    def _make(value: Any | None, is_null: bool = False) -> MagicMock:
        resp = MagicMock()
        resp.is_null.return_value = is_null
        if value is not None and hasattr(value, "magnitude"):
            resp.value = value
        elif value is not None:
            mock_val = MagicMock()
            mock_val.magnitude = value
            resp.value = mock_val
        else:
            resp.value = None
        return resp

    yield _make


# ──────────────────────────────────────────────────────────────────────────────
# Serial / GPS mock fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_serial(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock pyserial so GPSTracker can open a fake port."""
    mock_serial_mod = MagicMock()
    mock_instance = MagicMock()
    mock_instance.is_open = True
    mock_instance.close = MagicMock()
    mock_instance.readline = MagicMock(return_value=b"")
    mock_serial_mod.Serial.return_value = mock_instance
    mock_serial_mod.SerialException = Exception
    monkeypatch.setattr("pi_camry.gps.tracker.serial", mock_serial_mod)
    return mock_instance


@pytest.fixture
def mock_pynmea2(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock pynmea2 parser."""
    mock_mod = MagicMock()

    def _fake_parse(sentence: str) -> Any:
        msg = MagicMock()
        if sentence.startswith(("$GNGGA", "$GPGGA")):
            msg.latitude = 34.0522
            msg.longitude = -118.2437
            msg.altitude = 100.0
            msg.horizontal_dil = 1.2
            msg.num_sats = 8
            msg.gps_qual = 1
        elif sentence.startswith(("$GNRMC", "$GPRMC")):
            msg.latitude = 34.0522
            msg.longitude = -118.2437
            msg.spd_over_grnd = 30.0  # knots
            msg.true_course = 90.0
        elif sentence.startswith(("$GNVTG", "$GPVTG")):
            msg.true_track = 90.0
            msg.spd_over_grnd_kmph = 55.5
        else:
            msg.latitude = None
        return msg

    mock_mod.parse = _fake_parse
    monkeypatch.setattr("pi_camry.gps.tracker.parse_nmea", _fake_parse)
    return mock_mod


# ──────────────────────────────────────────────────────────────────────────────
# IMU / smbus2 mock fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_smbus2(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock smbus2 so IMUSensor can initialize without I2C hardware."""
    mock_mod = MagicMock()
    mock_bus = MagicMock()
    # Return 14 bytes of raw zeros (flat, still vehicle)
    mock_bus.read_i2c_block_data.return_value = [0] * 14
    mock_bus.write_byte_data = MagicMock()
    mock_bus.close = MagicMock()
    mock_mod.SMBus.return_value = mock_bus
    monkeypatch.setattr("smbus2.SMBus", mock_mod.SMBus)
    return mock_bus


# ──────────────────────────────────────────────────────────────────────────────
# GPIO / lgpio mock fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_lgpio(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock lgpio module for GPIOController tests."""
    mock_mod = MagicMock()
    mock_mod.SET_PULL_DOWN = 1
    mock_mod.gpiochip_open.return_value = 42  # fake handle
    mock_mod.gpiochip_close = MagicMock()
    mock_mod.gpio_claim_output = MagicMock()
    mock_mod.gpio_claim_input = MagicMock()
    mock_mod.gpio_write = MagicMock()
    mock_mod.gpio_read.return_value = 0
    monkeypatch.setattr("pi_camry.gpio.controller.lgpio", mock_mod)
    return mock_mod


# ──────────────────────────────────────────────────────────────────────────────
# Telegram mock fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_telegram_app(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock python-telegram-bot Application builder chain."""
    mock_app = MagicMock()
    mock_app.add_handler = MagicMock()
    mock_app.initialize = AsyncMock()
    mock_app.start = AsyncMock()
    mock_app.stop = AsyncMock()
    mock_app.bot = MagicMock()
    mock_app.bot.send_message = AsyncMock()
    mock_app.bot.send_photo = AsyncMock()

    mock_builder = MagicMock()
    mock_builder.token.return_value = mock_builder
    mock_builder.build.return_value = mock_app

    mock_application_cls = MagicMock()
    mock_application_cls.builder.return_value = mock_builder

    monkeypatch.setattr("pi_camry.telegram.bot.Application", mock_application_cls)
    return mock_app


# ──────────────────────────────────────────────────────────────────────────────
# PyAudio mock fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_pyaudio(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock PyAudio for VoiceAssistant tests."""
    mock_mod = MagicMock()
    mock_instance = MagicMock()
    mock_instance.get_device_count.return_value = 2
    mock_instance.get_device_info_by_index.side_effect = [
        {"name": " bcm2835", "index": 0},
        {"name": "USB Microphone", "index": 1},
    ]
    mock_instance.terminate = MagicMock()
    mock_mod.PyAudio.return_value = mock_instance

    mock_stream = MagicMock()
    mock_stream.stop_stream = MagicMock()
    mock_stream.close = MagicMock()
    mock_stream.read.return_value = b"\x00" * 2048
    mock_stream.write = MagicMock()
    mock_instance.open.return_value = mock_stream

    monkeypatch.setattr("pi_camry.audio.assistant.pyaudio", mock_mod)
    return mock_instance


# ──────────────────────────────────────────────────────────────────────────────
# Camera / picamera2 mock fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_picamera2(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Mock picamera2 if present; harmless if not installed."""
    mock_mod = MagicMock()
    mock_cam = MagicMock()
    mock_mod.Picamera2.return_value = mock_cam
    try:
        import picamera2
        monkeypatch.setattr(picamera2, "Picamera2", mock_mod.Picamera2)
    except ImportError:
        pass
    # Also patch the module path used in recorder if it imports directly
    monkeypatch.setitem(__import__("sys").modules, "picamera2", mock_mod)
    return mock_mod


# ──────────────────────────────────────────────────────────────────────────────
# Async event loop policy
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def _event_loop_policy() -> None:
    """Ensure asyncio tests use the default loop policy."""
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
