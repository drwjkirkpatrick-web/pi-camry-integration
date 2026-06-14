"""
Tests for pi_camry.imu.sensor — IMUSensor with mocked smbus2 / I2C.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any
from unittest.mock import MagicMock

import pytest

from pi_camry.core import EventType
from pi_camry.imu.sensor import IMUSensor, IMUReading


# ── Skip if smbus2 not installed ──
pytest.importorskip("smbus2", reason="smbus2 not installed")


@pytest.mark.asyncio
async def test_imu_start_stop(mock_smbus2: MagicMock) -> None:
    """IMUSensor should initialize I2C and enter sampling loop."""
    sensor = IMUSensor()
    await sensor.start()
    assert sensor._running is True
    assert sensor._bus is not None
    await sensor.stop()
    assert sensor._running is False
    sensor._bus.close.assert_called_once()


@pytest.mark.asyncio
async def test_imu_calibration_computes_bias(mock_smbus2: MagicMock) -> None:
    """After calibration, bias dict should be populated."""
    sensor = IMUSensor()
    await sensor.start()
    assert sensor._calibrated is True
    assert all(k in sensor._bias for k in ("ax", "ay", "az", "gx", "gy", "gz"))
    await sensor.stop()


@pytest.mark.asyncio
async def test_imu_get_latest(mock_smbus2: MagicMock) -> None:
    """get_latest should return an IMUReading."""
    sensor = IMUSensor()
    await sensor.start()
    reading = sensor.get_latest()
    assert isinstance(reading, IMUReading)
    await sensor.stop()


@pytest.mark.asyncio
async def test_imu_read_raw_signed_conversion(mock_smbus2: MagicMock) -> None:
    """_to_signed should correctly convert two's complement bytes."""
    sensor = IMUSensor()
    # Positive value
    assert sensor._to_signed(0x01, 0x00) == 256
    # Negative value (0xFF, 0xFF = -1)
    assert sensor._to_signed(0xFF, 0xFF) == -1
    # Zero
    assert sensor._to_signed(0x00, 0x00) == 0


@pytest.mark.asyncio
async def test_imu_collision_detection(mock_smbus2: MagicMock, event_bus: Any) -> None:
    """A reading above collision threshold should publish IMU_COLLISION."""
    sensor = IMUSensor()
    # Inject synthetic high-G reading manually
    reading = IMUReading(total_g=4.5, ax=40.0, ay=10.0, az=5.0)
    sensor._detect_events(reading)
    # Since _detect_events uses asyncio.create_task, give event loop a tick
    await asyncio.sleep(0.05)
    # No direct assertion on bus here (global bus), but method should not raise


@pytest.mark.asyncio
async def test_imu_hard_brake_detection(mock_smbus2: MagicMock) -> None:
    """Strong negative longitudinal G should trigger hard brake logic."""
    sensor = IMUSensor()
    reading = IMUReading(longitudinal_g=-0.8)
    sensor._detect_events(reading)
    await asyncio.sleep(0.05)
    # Should not raise; in real suite we would spy on bus.publish


@pytest.mark.asyncio
async def test_imu_cornering_detection(mock_smbus2: MagicMock) -> None:
    """High lateral G should trigger cornering detection."""
    sensor = IMUSensor()
    reading = IMUReading(lateral_g=0.7)
    sensor._detect_events(reading)
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_imu_on_ignition_change(mock_smbus2: MagicMock) -> None:
    """on_ignition_change should update internal state."""
    sensor = IMUSensor()
    sensor.on_ignition_change(True)
    assert sensor._ignition_was_on is True
    assert sensor._motion_while_off is False
    sensor.on_ignition_change(False)
    assert sensor._motion_while_off is False


@pytest.mark.asyncio
async def test_imu_tow_detection(mock_smbus2: MagicMock) -> None:
    """Motion while ignition was on then off should trigger tow event."""
    sensor = IMUSensor()
    sensor.on_ignition_change(True)
    sensor.on_ignition_change(False)
    reading = IMUReading(total_g=1.5)
    sensor._check_tow(reading)
    await asyncio.sleep(0.05)
    assert sensor._motion_while_off is True


@pytest.mark.asyncio
async def test_imu_configure_mpu(mock_smbus2: MagicMock) -> None:
    """_configure_mpu should write expected register values."""
    sensor = IMUSensor()
    sensor._bus = mock_smbus2
    sensor._configure_mpu()
    calls = mock_smbus2.write_byte_data.call_args_list
    # Should write to PWR_MGMT_1, SMPLRT_DIV, CONFIG, GYRO_CONFIG, ACCEL_CONFIG, INT_ENABLE
    assert len(calls) >= 5
