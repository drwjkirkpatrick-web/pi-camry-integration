"""
Tests for pi_camry.modern_vehicle — Modern vehicle adapter with mocked hardware.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pi_camry.modern_vehicle.can_multibus import CANMultibusController, CANBusID, CANSignal
from pi_camry.modern_vehicle.uds_client import UDSClient, UDS_Service, UDS_Session
from pi_camry.modern_vehicle.radar_fusion import RadarFusionProcessor, RadarTarget
from pi_camry.modern_vehicle.interior_sensing import InteriorEnvironmentSensor, AirQualityReading
from pi_camry.core import EventType, bus


@pytest.fixture
def modern_cfg():
    """Mock modern vehicle config."""
    cfg = MagicMock()
    cfg.can_fd_enabled = False
    cfg.can_fd_data_rate = 2000000
    cfg.uds_transport = "can"
    cfg.doip_gateway = "192.168.1.1"
    cfg.tpms_receiver = "rtlsdr"
    cfg.tpms_frequency_hz = 315000000
    cfg.tpms_protocol = "schrader"
    cfg.tpms_sensor_ids = []
    cfg.radar_front_enabled = False
    cfg.radar_rear_enabled = False
    cfg.dms_enabled = False
    cfg.interior_sensing_enabled = False
    cfg.interior_sensors = ["scd4x", "sgp40"]
    return cfg


# ── CAN Multibus ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_can_multibus_start_stop(modern_cfg: Any) -> None:
    """CANMultibusController should initialize configured buses."""
    mock_can = MagicMock()
    mock_bus = MagicMock()
    mock_can.interface.Bus.return_value = mock_bus
    with patch.dict(sys.modules, {"can": mock_can}):
        ctrl = CANMultibusController(cfg=modern_cfg)
        await ctrl.start()
        assert ctrl._running is True
        await ctrl.stop()
        assert ctrl._running is False


@pytest.mark.asyncio
async def test_can_multibus_send_frame(modern_cfg: Any) -> None:
    """send should transmit a CAN frame."""
    mock_can = MagicMock()
    mock_bus = MagicMock()
    mock_can.interface.Bus.return_value = mock_bus
    with patch.dict(sys.modules, {"can": mock_can}):
        ctrl = CANMultibusController(cfg=modern_cfg)
        await ctrl.start()
        ok = await ctrl.send(CANBusID.POWERTRAIN, 0x123, b"\x01\x02\x03")
        assert ok is True
        await ctrl.stop()


@pytest.mark.asyncio
async def test_can_multibus_signal_callback(modern_cfg: Any) -> None:
    """Decoded signals should trigger registered callbacks."""
    mock_can = MagicMock()
    mock_bus = MagicMock()
    mock_can.interface.Bus.return_value = mock_bus
    with patch.dict(sys.modules, {"can": mock_can}):
        ctrl = CANMultibusController(cfg=modern_cfg)
        mock_cb = MagicMock()
        ctrl.register_signal_callback(mock_cb)
        await ctrl.start()
        await ctrl.stop()


# ── UDS Client ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_uds_client_start_stop(modern_cfg: Any) -> None:
    """UDSClient should initialize transport."""
    mock_isotp = MagicMock()
    mock_stack = MagicMock()
    mock_isotp.NotifierBasedCanStack.return_value = mock_stack
    with patch.dict(sys.modules, {"isotp": mock_isotp}):
        client = UDSClient(cfg=modern_cfg)
        await client.start()
        assert client._running is True
        await client.stop()
        assert client._running is False


@pytest.mark.asyncio
async def test_uds_change_session(modern_cfg: Any) -> None:
    """change_session should send DiagnosticSessionControl."""
    mock_isotp = MagicMock()
    mock_stack = MagicMock()
    mock_stack.recv = MagicMock(return_value=bytes([0x50, 0x03]))  # Positive response
    mock_isotp.NotifierBasedCanStack.return_value = mock_stack
    with patch.dict(sys.modules, {"isotp": mock_isotp}):
        client = UDSClient(cfg=modern_cfg)
        await client.start()
        ok = await client.change_session(0x7E0, UDS_Session.EXTENDED)
        assert ok is True
        assert client._session == UDS_Session.EXTENDED
        await client.stop()


@pytest.mark.asyncio
async def test_uds_read_vin(modern_cfg: Any) -> None:
    """read_vin should return VIN string."""
    mock_isotp = MagicMock()
    mock_stack = MagicMock()
    mock_stack.recv = MagicMock(return_value=bytes([0x62, 0xF1, 0x90]) + b"1HGCM82633A123456")
    mock_isotp.NotifierBasedCanStack.return_value = mock_stack
    with patch.dict(sys.modules, {"isotp": mock_isotp}):
        client = UDSClient(cfg=modern_cfg)
        await client.start()
        vin = await client.read_vin(0x7E0)
        assert vin == "1HGCM82633A123456"
        await client.stop()


# ── Radar Fusion ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_radar_fusion_process_targets() -> None:
    """Process targets should update state."""
    radar = RadarFusionProcessor()
    await radar.start()
    now = time.monotonic()
    targets = [
        RadarTarget(track_id=1, distance_m=25.0, relative_speed_ms=-5.0,
                    azimuth_deg=0.0, width_m=1.8, height_m=1.5, timestamp=now),
    ]
    await radar.process_targets("front_long", targets)
    state = radar.get_state()
    assert state.front_clear is False  # 25m < 30m threshold
    assert len(state.targets) == 1
    await radar.stop()


@pytest.mark.asyncio
async def test_radar_fusion_ttc_calculation() -> None:
    """TTC should be calculated correctly."""
    radar = RadarFusionProcessor()
    await radar.start()
    now = time.monotonic()
    targets = [
        RadarTarget(track_id=1, distance_m=50.0, relative_speed_ms=-10.0,
                    azimuth_deg=0.0, width_m=1.8, height_m=1.5, timestamp=now),
    ]
    await radar.process_targets("front_long", targets)
    state = radar.get_state()
    assert state.ttc_front_sec == 5.0  # 50m / 10 m/s
    await radar.stop()


@pytest.mark.asyncio
async def test_radar_fusion_acc_target_speed() -> None:
    """ACC should reduce speed when following close."""
    radar = RadarFusionProcessor()
    await radar.start()
    now = time.monotonic()
    targets = [
        RadarTarget(track_id=1, distance_m=20.0, relative_speed_ms=-5.0,
                    azimuth_deg=0.0, width_m=1.8, height_m=1.5, timestamp=now),
    ]
    await radar.process_targets("front_long", targets)
    # Set speed 30 m/s (~67 mph), front vehicle 5 m/s slower
    target_speed = await radar.acc_target_speed(30.0)
    assert target_speed <= 25.0  # Should match front vehicle speed (30 - 5)
    await radar.stop()


# ── Interior Sensing ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_interior_sensing_mock_loop() -> None:
    """Interior sensor should generate mock readings when no hardware."""
    sensor = InteriorEnvironmentSensor()
    await sensor.start()
    await asyncio.sleep(0.1)  # Let mock loop produce one reading
    reading = sensor.get_latest()
    assert reading is not None
    assert 400 <= reading.co2_ppm <= 1000
    assert 0 <= reading.pm2_5_ug_m3 <= 20
    await sensor.stop()


def test_interior_air_quality_score() -> None:
    """get_air_quality_score should return 0-100."""
    sensor = InteriorEnvironmentSensor()
    # Manually set a reading
    reading = AirQualityReading(
        co2_ppm=1200, voc_index=300, pm1_0_ug_m3=5,
        pm2_5_ug_m3=20, pm4_0_ug_m3=25, pm10_ug_m3=30,
        temperature_c=22, humidity_percent=50, pressure_hpa=1013,
        timestamp=0.0,
    )
    sensor._latest = reading
    score = sensor.get_air_quality_score()
    assert 0 <= score <= 100
    # High CO2 and VOC should reduce score
    assert score < 80


# ── Integration: EventBus ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_radar_publishes_fcw_event() -> None:
    """Radar should publish FCW event when TTC is low."""
    radar = RadarFusionProcessor()
    await radar.start()

    events: list[Any] = []
    async def capture(evt: Any) -> None:
        events.append(evt)
    bus.subscribe(EventType.RADAR_TARGET, capture)

    # Target at 10m approaching at 5 m/s = TTC = 2s (AEB threshold)
    now = time.monotonic()
    targets = [
        RadarTarget(track_id=1, distance_m=10.0, relative_speed_ms=-5.0,
                    azimuth_deg=0.0, width_m=1.8, height_m=1.5, timestamp=now),
    ]
    await radar.process_targets("front_long", targets)
    await asyncio.sleep(0.1)

    assert any(e.event_type == EventType.RADAR_TARGET for e in events)
    aeb_events = [e for e in events if e.payload.get("alert") == "AEB"]
    assert len(aeb_events) >= 1

    bus.unsubscribe(EventType.RADAR_TARGET, capture)
    await radar.stop()
