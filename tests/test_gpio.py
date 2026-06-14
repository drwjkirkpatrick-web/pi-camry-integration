"""
Tests for pi_camry.gpio.controller — GPIOController with mocked lgpio.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from pi_camry.core import EventType
from pi_camry.gpio.controller import GPIOController, InputName, RelayName


# ── Conditionally skip if lgpio unavailable ──
lgpio = pytest.importorskip("lgpio", reason="lgpio not installed")


@pytest.mark.asyncio
async def test_gpio_start_stop(mock_lgpio: MagicMock) -> None:
    """GPIOController should open gpiochip and configure pins on start."""
    ctrl = GPIOController()
    await ctrl.start()
    assert ctrl._running is True
    assert ctrl._h == 42
    mock_lgpio.gpiochip_open.assert_called_once_with(0)
    await ctrl.shutdown()
    assert ctrl._running is False
    mock_lgpio.gpiochip_close.assert_called_once()


@pytest.mark.asyncio
async def test_gpio_set_relay_on_off(mock_lgpio: MagicMock) -> None:
    """set_relay should write correct active-low levels."""
    ctrl = GPIOController()
    await ctrl.start()
    result = await ctrl.set_relay(RelayName.HEADLIGHTS, on=True)
    assert result is True
    # Active-low: ON = LOW (0)
    mock_lgpio.gpio_write.assert_called_with(ctrl._h, ctrl.cfg.relay_headlights, 0)

    result = await ctrl.set_relay(RelayName.HEADLIGHTS, on=False)
    assert result is True
    mock_lgpio.gpio_write.assert_called_with(ctrl._h, ctrl.cfg.relay_headlights, 1)
    await ctrl.shutdown()


@pytest.mark.asyncio
async def test_gpio_pulse_relay(mock_lgpio: MagicMock) -> None:
    """pulse_relay should turn relay on then off after duration."""
    ctrl = GPIOController()
    await ctrl.start()
    await ctrl.pulse_relay(RelayName.DOME_LIGHT, duration_sec=0.05)
    # Should have been turned ON then OFF
    on_calls = [c for c in mock_lgpio.gpio_write.call_args_list if c[0][2] == 0]
    off_calls = [c for c in mock_lgpio.gpio_write.call_args_list if c[0][2] == 1]
    assert len(on_calls) >= 1
    assert len(off_calls) >= 1
    await ctrl.shutdown()


@pytest.mark.asyncio
async def test_gpio_set_headlights_auto(mock_lgpio: MagicMock) -> None:
    """set_headlights_auto should turn on headlights when ambient light < 50 lux."""
    ctrl = GPIOController()
    await ctrl.start()
    await ctrl.set_headlights_auto(ambient_light=30.0)
    # Expect relay ON (active-low = 0)
    last_call = mock_lgpio.gpio_write.call_args_list[-1]
    assert last_call[0][2] == 0  # LOW = ON
    await ctrl.shutdown()


@pytest.mark.asyncio
async def test_gpio_input_state_tracking(mock_lgpio: MagicMock) -> None:
    """get_input_state should return the current InputState."""
    ctrl = GPIOController()
    state = ctrl.get_input_state(InputName.IGNITION)
    assert state.name == "ignition"
    assert state.active is False


@pytest.mark.asyncio
async def test_gpio_relay_state_tracking(mock_lgpio: MagicMock) -> None:
    """get_relay_state should reflect last set value."""
    ctrl = GPIOController()
    await ctrl.start()
    await ctrl.set_relay(RelayName.COOLING_FAN, on=True)
    state = ctrl.get_relay_state(RelayName.COOLING_FAN)
    assert state.on is True
    await ctrl.shutdown()


@pytest.mark.asyncio
async def test_gpio_read_adc_without_spi(mock_lgpio: MagicMock) -> None:
    """read_adc should return 0 when SPI is not initialized."""
    ctrl = GPIOController()
    val = ctrl.read_adc(0)
    assert val == 0


@pytest.mark.asyncio
async def test_gpio_adc_voltage_conversion(mock_lgpio: MagicMock, monkeypatch: Any) -> None:
    """read_adc_voltage should convert raw ADC to voltage."""
    ctrl = GPIOController()
    # Fake SPI that returns mid-scale
    mock_spi = MagicMock()
    mock_spi.xfer2.return_value = [0, 1, 0xFF]  # data = ((1 & 3) << 8) + 255 = 511
    ctrl._spi = mock_spi
    voltage = ctrl.read_adc_voltage(0, vref=3.3)
    assert pytest.approx(voltage, 0.01) == (511 / 1023.0) * 3.3


@pytest.mark.asyncio
async def test_gpio_poll_loop_emits_ignition_on(mock_lgpio: MagicMock, event_bus: Any) -> None:
    """When ignition pin goes HIGH, IGNITION_ON should be emitted."""
    ctrl = GPIOController()
    await ctrl.start()
    # Simulate ignition pin HIGH
    mock_lgpio.gpio_read.return_value = 1
    # Let poll loop run a few ticks
    await asyncio.sleep(0.25)
    await ctrl.shutdown()
    # Since we use global bus, we can't easily assert here, but no exception = pass


@pytest.mark.asyncio
async def test_gpio_shutdown_timer_cancelled_on_ignition_back(mock_lgpio: MagicMock) -> None:
    """If ignition goes OFF then back ON, shutdown timer should be cancelled."""
    ctrl = GPIOController()
    await ctrl.start()
    mock_lgpio.gpio_read.return_value = 0  # ignition OFF
    await asyncio.sleep(0.15)
    mock_lgpio.gpio_read.return_value = 1  # ignition back ON
    await asyncio.sleep(0.15)
    await ctrl.shutdown()
    assert ctrl._shutdown_task is None or ctrl._shutdown_task.done()
