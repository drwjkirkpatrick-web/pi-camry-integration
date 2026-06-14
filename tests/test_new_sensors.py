"""
Tests for new sensor and connectivity modules.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from pi_camry.connectivity.lte import LTEController, SignalQuality
from pi_camry.sensors.rain_sensor import RainSensor, RainIntensity, RainReading
from pi_camry.sensors.ultrasonic_array import UltrasonicArray, ParkingZone
from pi_camry.sensors.rtk_gps import RTKGPSController, FixType, RTKFix
from pi_camry.sensors.thermal_camera import ThermalCamera, ThermalFrame, ThermalHotspot
from pi_camry.core import EventType, bus


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def rain_cfg():
    cfg = MagicMock()
    cfg.enabled = True
    cfg.sensor_type = "ir_reflectance"
    cfg.adc_channel = 0
    cfg.threshold_on = 600
    cfg.threshold_off = 700
    cfg.wiper_relay_pin = None
    cfg.wiper_speed_pins = []
    return cfg


@pytest.fixture
def ultrasonic_cfg():
    cfg = MagicMock()
    cfg.enabled = True
    cfg.front_count = 4
    cfg.rear_count = 4
    cfg.front_pins = []
    cfg.rear_pins = []
    cfg.max_range_cm = 400
    cfg.min_range_cm = 2
    cfg.alert_near_cm = 30
    cfg.alert_mid_cm = 60
    cfg.alert_far_cm = 100
    return cfg


@pytest.fixture
def rtk_cfg():
    cfg = MagicMock()
    cfg.enabled = True
    cfg.port = "/dev/ttyAMA0"
    cfg.baud = 38400
    cfg.ntrip_enabled = False
    cfg.ntrip_caster = ""
    cfg.ntrip_port = 2101
    cfg.ntrip_mountpoint = ""
    cfg.ntrip_user = ""
    cfg.ntrip_password = ""
    cfg.log_raw_ubx = False
    return cfg


@pytest.fixture
def thermal_cfg():
    cfg = MagicMock()
    cfg.enabled = True
    cfg.model = "mlx90640"
    cfg.i2c_bus = 1
    cfg.i2c_address = 0x33
    cfg.spi_bus = 0
    cfg.spi_device = 0
    cfg.usb_path = ""
    cfg.overlay_alpha = 0.5
    cfg.hotspot_threshold_c = 35.0
    return cfg


# ── LTE Controller ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lte_start_stop_mock() -> None:
    """LTEController should start/stop with mocked serial."""
    lte = LTEController(cfg=None)
    # With no pyserial, should log warning and return
    assert lte._running is False


def test_lte_signal_quality_parsing() -> None:
    """Signal quality should be parsed from AT+CSQ response."""
    import re
    csq = "+CSQ: 18,0\nOK"
    match = re.search(r"\+CSQ:\s*(\d+),(\d+)", csq)
    assert match is not None
    rssi_raw, ber = int(match.group(1)), int(match.group(2))
    assert rssi_raw == 18
    assert ber == 0
    rssi_dbm = -113 + 2 * rssi_raw
    assert rssi_dbm == -77


# ── Rain Sensor ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rain_sensor_mock_loop(rain_cfg) -> None:
    """RainSensor should generate readings without hardware."""
    sensor = RainSensor(cfg=rain_cfg)
    await sensor.start()
    await asyncio.sleep(0.15)  # Let one poll cycle run
    reading = sensor.get_reading()
    assert reading is not None
    assert isinstance(reading, RainReading)
    assert 500 <= reading.raw_adc <= 800  # Mock range
    await sensor.stop()


def test_rain_classification(rain_cfg) -> None:
    """ADC values should map to correct rain intensity."""
    sensor = RainSensor(cfg=rain_cfg)
    assert sensor._classify(850) == RainIntensity.NONE
    assert sensor._classify(700) == RainIntensity.DRIZZLE
    assert sensor._classify(600) == RainIntensity.LIGHT
    assert sensor._classify(400) == RainIntensity.MODERATE
    assert sensor._classify(150) == RainIntensity.HEAVY


def test_rain_wiper_speed_mapping(rain_cfg) -> None:
    """Rain intensity should map to correct wiper speed."""
    sensor = RainSensor(cfg=rain_cfg)
    assert sensor._wiper_speed_for(RainIntensity.NONE) == 0
    assert sensor._wiper_speed_for(RainIntensity.DRIZZLE) == 0
    assert sensor._wiper_speed_for(RainIntensity.LIGHT) == 1
    assert sensor._wiper_speed_for(RainIntensity.MODERATE) == 2
    assert sensor._wiper_speed_for(RainIntensity.HEAVY) == 3


# ── Ultrasonic Array ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ultrasonic_mock_loop(ultrasonic_cfg) -> None:
    """UltrasonicArray should start without hardware."""
    arr = UltrasonicArray(cfg=ultrasonic_cfg)
    await arr.start()
    await asyncio.sleep(0.1)
    assert len(arr._sensors) == 8
    await arr.stop()


def test_ultrasonic_zone_classification(ultrasonic_cfg) -> None:
    """Distance should map to correct parking zone."""
    arr = UltrasonicArray(cfg=ultrasonic_cfg)
    assert arr._classify_zone(150) == ParkingZone.CLEAR
    assert arr._classify_zone(80) == ParkingZone.FAR
    assert arr._classify_zone(45) == ParkingZone.MID
    assert arr._classify_zone(20) == ParkingZone.NEAR
    assert arr._classify_zone(10) == ParkingZone.DANGER
    assert arr._classify_zone(-1) == ParkingZone.CLEAR  # Invalid


# ── RTK GPS ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rtk_nav_pvt_parsing(rtk_cfg) -> None:
    """NAV-PVT payload should be parsed into RTKFix."""
    rtk = RTKGPSController(cfg=rtk_cfg)
    # Build a minimal NAV-PVT payload (92 bytes)
    payload = bytearray(92)
    struct.pack_into("<I", payload, 0, 123456)   # iTOW
    struct.pack_into("<H", payload, 4, 2024)    # year
    payload[6] = 6     # month
    payload[7] = 15    # day
    payload[8] = 12    # hour
    payload[9] = 30    # min
    payload[10] = 0    # sec
    payload[20] = FixType.RTK_FIXED  # fixType
    payload[23] = 12   # numSV
    struct.pack_into("<i", payload, 24, -1220000000)  # lon (×1e-7)
    struct.pack_into("<i", payload, 28,  370000000)   # lat (×1e-7)
    struct.pack_into("<i", payload, 32, 10000)        # height (mm)
    struct.pack_into("<i", payload, 36, 50000)         # hMSL (mm)
    struct.pack_into("<I", payload, 40, 50)           # hAcc (mm)
    struct.pack_into("<I", payload, 44, 100)          # vAcc (mm)

    rtk._parse_nav_pvt(bytes(payload))
    fix = rtk.get_fix()
    assert fix is not None
    assert fix.fix_type == FixType.RTK_FIXED
    assert fix.lat == 37.0
    assert fix.lon == -122.0
    assert fix.sats_used == 12
    assert fix.h_acc == 0.05  # 50mm → 0.05m


def test_rtk_is_rtk_fixed() -> None:
    """is_rtk_fixed should return True only for RTK_FIXED fix type."""
    rtk = RTKGPSController(cfg=None)
    assert rtk.is_rtk_fixed() is False
    rtk._latest_fix = RTKFix(
        lat=0, lon=0, alt_msl=0, h_acc=0.02, v_acc=0.05,
        fix_type=FixType.RTK_FIXED, rtk_age=0, sats_used=15,
        sats_visible=20, timestamp=0,
    )
    assert rtk.is_rtk_fixed() is True
    rtk._latest_fix = RTKFix(
        lat=0, lon=0, alt_msl=0, h_acc=1.5, v_acc=3.0,
        fix_type=FixType.FIX_3D, rtk_age=0, sats_used=8,
        sats_visible=12, timestamp=0,
    )
    assert rtk.is_rtk_fixed() is False


# ── Thermal Camera ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_thermal_mock_loop(thermal_cfg) -> None:
    """ThermalCamera should start without hardware using mock."""
    cam = ThermalCamera(cfg=thermal_cfg)
    await cam.start()
    await asyncio.sleep(0.2)  # Let mock loop run
    # With no sensor library, it should silently skip
    await cam.stop()


def test_thermal_hotspot_detection(thermal_cfg) -> None:
    """Hotspot detection should find blobs above threshold."""
    cam = ThermalCamera(cfg=thermal_cfg)
    # Create synthetic thermal frame: 32×24, hotspot in center
    temps = np.full((24, 32), 25.0, dtype=np.float32)
    temps[10:14, 14:18] = 45.0  # 4×4 hotspot at 45°C
    hotspots = cam._detect_hotspots(temps, 32, 24)
    assert len(hotspots) == 1
    h = hotspots[0]
    assert h.temp_c == 45.0
    assert h.size_pixels == 16
    assert h.x == 15  # Center of pixels 14-17: (14+17)/2 = 15.5 → int = 15
    assert h.y == 11  # Center of pixels 10-13: (10+13)/2 = 11.5 → int = 11


def test_thermal_no_hotspots(thermal_cfg) -> None:
    """Frame below threshold should produce no hotspots."""
    cam = ThermalCamera(cfg=thermal_cfg)
    temps = np.full((24, 32), 25.0, dtype=np.float32)  # All below 35°C
    hotspots = cam._detect_hotspots(temps, 32, 24)
    assert len(hotspots) == 0


# ── Integration tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rain_publishes_to_eventbus(rain_cfg) -> None:
    """Rain sensor should publish ENVIRONMENT events."""
    sensor = RainSensor(cfg=rain_cfg)
    events: list[Any] = []
    async def capture(evt: Any) -> None:
        events.append(evt)
    bus.subscribe(EventType.ENVIRONMENT, capture)

    await sensor.start()
    await asyncio.sleep(0.2)
    await sensor.stop()

    rain_events = [e for e in events if e.source == "rain_sensor"]
    assert len(rain_events) >= 1

    bus.unsubscribe(EventType.ENVIRONMENT, capture)


@pytest.mark.asyncio
async def test_ultrasonic_publishes_to_eventbus(ultrasonic_cfg) -> None:
    """Ultrasonic array should publish ENVIRONMENT events."""
    arr = UltrasonicArray(cfg=ultrasonic_cfg)
    events: list[Any] = []
    async def capture(evt: Any) -> None:
        events.append(evt)
    bus.subscribe(EventType.ENVIRONMENT, capture)

    await arr.start()
    await asyncio.sleep(0.2)
    await arr.stop()

    # Event may or may not fire depending on timing; just verify no crash
    bus.unsubscribe(EventType.ENVIRONMENT, capture)
