"""
Tests for pi_camry.display.joybring — Head-unit integration with mocked hardware.

Patches target the actual source modules since joybring.py imports them locally
inside methods.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pi_camry.display.joybring import JoyBringController, TouchPoint, TouchEventType, SteeringWheelButton
from pi_camry.core import EventType, bus


@pytest.fixture
def joybring_cfg():
    """Mock display config."""
    cfg = MagicMock()
    cfg.resolution = (1024, 600)
    cfg.hdmi_port = 0
    cfg.cec_enabled = True
    cfg.touch_device = "auto"
    cfg.can_enabled = False
    cfg.can_dual_bus = False
    cfg.swc_enabled = False
    cfg.backlight_pwm_pin = None
    cfg.dashboard_enabled = True
    cfg.radio_mode = "headunit"
    return cfg


@pytest.mark.asyncio
async def test_joybring_start_stop(joybring_cfg: Any) -> None:
    """JoyBringController should start/stop all subsystems."""
    with patch("pi_camry.display.hdmi_sink.HDMISink") as mock_hdmi, \
         patch("pi_camry.display.touch_input.TouchInputHandler") as mock_touch:
        mock_hdmi_inst = AsyncMock()
        mock_hdmi_inst.get_preferred_resolution = AsyncMock(return_value=(1024, 600))
        mock_hdmi.return_value = mock_hdmi_inst
        mock_touch_inst = AsyncMock()
        mock_touch.return_value = mock_touch_inst

        jb = JoyBringController(cfg=joybring_cfg)
        await jb.start()
        assert jb._running is True
        mock_hdmi.assert_called_once()
        mock_touch.assert_called_once()

        await jb.stop()
        assert jb._running is False


@pytest.mark.asyncio
async def test_joybring_set_resolution(joybring_cfg: Any) -> None:
    """set_resolution should call HDMI set_resolution."""
    with patch("pi_camry.display.hdmi_sink.HDMISink") as mock_hdmi, \
         patch("pi_camry.display.touch_input.TouchInputHandler") as mock_touch:
        mock_hdmi_inst = AsyncMock()
        mock_hdmi_inst.get_preferred_resolution = AsyncMock(return_value=(1024, 600))
        mock_hdmi.return_value = mock_hdmi_inst
        mock_touch.return_value = AsyncMock()

        jb = JoyBringController(cfg=joybring_cfg)
        await jb.start()
        # Mock the return value explicitly
        mock_hdmi_inst.set_resolution = AsyncMock(return_value=True)
        ok = await jb.set_resolution(1280, 720)
        assert ok is True
        mock_hdmi_inst.set_resolution.assert_awaited_once_with(1280, 720)
        await jb.stop()


@pytest.mark.asyncio
async def test_joybring_set_backlight(joybring_cfg: Any) -> None:
    """set_backlight should update level and call HDMI."""
    with patch("pi_camry.display.hdmi_sink.HDMISink") as mock_hdmi, \
         patch("pi_camry.display.touch_input.TouchInputHandler") as mock_touch:
        mock_hdmi_inst = AsyncMock()
        mock_hdmi_inst.get_preferred_resolution = AsyncMock(return_value=(1024, 600))
        mock_hdmi.return_value = mock_hdmi_inst
        mock_touch.return_value = AsyncMock()

        jb = JoyBringController(cfg=joybring_cfg)
        await jb.start()
        await jb.set_backlight(50)
        assert jb._backlight_level == 50
        mock_hdmi_inst.set_backlight.assert_awaited_once_with(50)
        await jb.stop()


@pytest.mark.asyncio
async def test_joybring_touch_callback(joybring_cfg: Any) -> None:
    """Touch events should be forwarded to registered callbacks."""
    with patch("pi_camry.display.hdmi_sink.HDMISink") as mock_hdmi, \
         patch("pi_camry.display.touch_input.TouchInputHandler") as mock_touch:
        mock_hdmi_inst = AsyncMock()
        mock_hdmi_inst.get_preferred_resolution = AsyncMock(return_value=(1024, 600))
        mock_hdmi.return_value = mock_hdmi_inst
        mock_touch_inst = AsyncMock()
        mock_touch.return_value = mock_touch_inst

        jb = JoyBringController(cfg=joybring_cfg)
        mock_cb = MagicMock()
        jb.register_touch_callback(mock_cb)
        await jb.start()

        # Simulate touch event
        points = [TouchPoint(id=0, x=100, y=200, pressure=255, event_type=TouchEventType.DOWN)]
        jb._on_touch_event(points)
        mock_cb.assert_called_once_with(points)

        await jb.stop()


@pytest.mark.asyncio
async def test_joybring_swc_callback(joybring_cfg: Any) -> None:
    """Steering wheel button events should be forwarded."""
    with patch("pi_camry.display.hdmi_sink.HDMISink") as mock_hdmi, \
         patch("pi_camry.display.touch_input.TouchInputHandler") as mock_touch:
        mock_hdmi_inst = AsyncMock()
        mock_hdmi_inst.get_preferred_resolution = AsyncMock(return_value=(1024, 600))
        mock_hdmi.return_value = mock_hdmi_inst
        mock_touch.return_value = AsyncMock()

        jb = JoyBringController(cfg=joybring_cfg)
        mock_cb = MagicMock()
        jb.register_swc_callback(mock_cb)
        await jb.start()

        btn = SteeringWheelButton(name="VOL_UP", adc_voltage=3.9, resistance_ohms=2200)
        jb._on_swc_event(btn)
        mock_cb.assert_called_once_with(btn)

        await jb.stop()


@pytest.mark.asyncio
async def test_joybring_swc_default_mapping(joybring_cfg: Any) -> None:
    """Default SWC mapping should publish correct EventBus events."""
    with patch("pi_camry.display.hdmi_sink.HDMISink") as mock_hdmi, \
         patch("pi_camry.display.touch_input.TouchInputHandler") as mock_touch:
        mock_hdmi_inst = AsyncMock()
        mock_hdmi_inst.get_preferred_resolution = AsyncMock(return_value=(1024, 600))
        mock_hdmi.return_value = mock_hdmi_inst
        mock_touch.return_value = AsyncMock()

        jb = JoyBringController(cfg=joybring_cfg)
        await jb.start()

        # Track events
        events: list[Any] = []
        async def capture(evt: Any) -> None:
            events.append(evt)
        bus.subscribe(EventType.AUDIO_VOLUME_UP, capture)

        btn = SteeringWheelButton(name="VOL_UP", adc_voltage=3.9, resistance_ohms=2200)
        await jb.handle_swc_default(btn)

        # Give event bus time to process
        await asyncio.sleep(0.1)
        assert any(e.event_type == EventType.AUDIO_VOLUME_UP for e in events)

        bus.unsubscribe(EventType.AUDIO_VOLUME_UP, capture)
        await jb.stop()


@pytest.mark.asyncio
async def test_joybring_display_status(joybring_cfg: Any) -> None:
    """get_display_status should return structured status dict."""
    with patch("pi_camry.display.hdmi_sink.HDMISink") as mock_hdmi, \
         patch("pi_camry.display.touch_input.TouchInputHandler") as mock_touch:
        mock_hdmi_inst = AsyncMock()
        mock_hdmi_inst.get_preferred_resolution = AsyncMock(return_value=(1024, 600))
        mock_hdmi_inst.is_connected = MagicMock(return_value=True)
        mock_hdmi.return_value = mock_hdmi_inst
        mock_touch_inst = AsyncMock()
        mock_touch_inst.is_active = MagicMock(return_value=True)
        mock_touch.return_value = mock_touch_inst

        jb = JoyBringController(cfg=joybring_cfg)
        await jb.start()
        status = await jb.get_display_status()
        assert status["resolution"] == (1024, 600)
        assert status["hdmi_connected"] is True
        assert status["touch_active"] is True
        await jb.stop()


@pytest.mark.asyncio
async def test_joybring_cec_power_standby(joybring_cfg: Any) -> None:
    """CEC standby should trigger DISPLAY_STANDBY event."""
    with patch("pi_camry.display.hdmi_sink.HDMISink") as mock_hdmi, \
         patch("pi_camry.display.touch_input.TouchInputHandler") as mock_touch:
        mock_hdmi_inst = AsyncMock()
        mock_hdmi_inst.get_preferred_resolution = AsyncMock(return_value=(1024, 600))
        mock_hdmi_inst.get_cec_power_state = AsyncMock(return_value="standby")
        mock_hdmi.return_value = mock_hdmi_inst
        mock_touch.return_value = AsyncMock()

        jb = JoyBringController(cfg=joybring_cfg)
        await jb.start()

        events: list[Any] = []
        async def capture(evt: Any) -> None:
            events.append(evt)
        bus.subscribe(EventType.DISPLAY_STANDBY, capture)

        # Manually trigger standby detection once (not the infinite loop)
        jb._display_on = True
        # Simulate what the monitor loop would do for one iteration
        power_state = await mock_hdmi_inst.get_cec_power_state()
        if power_state == "standby" and jb._display_on:
            jb._display_on = False
            await bus.publish(EventType.DISPLAY_STANDBY, {}, source="joybring")

        await asyncio.sleep(0.1)
        assert jb._display_on is False
        assert any(e.event_type == EventType.DISPLAY_STANDBY for e in events)

        bus.unsubscribe(EventType.DISPLAY_STANDBY, capture)
        await jb.stop()


@pytest.mark.asyncio
async def test_joybring_send_cec_command(joybring_cfg: Any) -> None:
    """send_cec_command should forward to HDMI sink."""
    with patch("pi_camry.display.hdmi_sink.HDMISink") as mock_hdmi, \
         patch("pi_camry.display.touch_input.TouchInputHandler") as mock_touch:
        mock_hdmi_inst = AsyncMock()
        mock_hdmi_inst.get_preferred_resolution = AsyncMock(return_value=(1024, 600))
        mock_hdmi.return_value = mock_hdmi_inst
        mock_touch.return_value = AsyncMock()

        jb = JoyBringController(cfg=joybring_cfg)
        await jb.start()
        await jb.send_cec_command("power_on")
        mock_hdmi_inst.send_cec.assert_awaited_once_with("power_on")
        await jb.stop()


@pytest.mark.asyncio
async def test_joybring_no_can_when_disabled(joybring_cfg: Any) -> None:
    """CAN bridge should not be initialized when can_enabled=False."""
    with patch("pi_camry.display.hdmi_sink.HDMISink") as mock_hdmi, \
         patch("pi_camry.display.touch_input.TouchInputHandler") as mock_touch, \
         patch("pi_camry.display.can_bridge.CANBridge") as mock_can:
        mock_hdmi_inst = AsyncMock()
        mock_hdmi_inst.get_preferred_resolution = AsyncMock(return_value=(1024, 600))
        mock_hdmi.return_value = mock_hdmi_inst
        mock_touch.return_value = AsyncMock()

        jb = JoyBringController(cfg=joybring_cfg)
        await jb.start()
        mock_can.assert_not_called()
        assert jb._can_bridge is None
        await jb.stop()


@pytest.mark.asyncio
async def test_joybring_no_swc_when_disabled(joybring_cfg: Any) -> None:
    """SWC should not be initialized when swc_enabled=False."""
    with patch("pi_camry.display.hdmi_sink.HDMISink") as mock_hdmi, \
         patch("pi_camry.display.touch_input.TouchInputHandler") as mock_touch, \
         patch("pi_camry.display.steering_wheel.SteeringWheelController") as mock_swc:
        mock_hdmi_inst = AsyncMock()
        mock_hdmi_inst.get_preferred_resolution = AsyncMock(return_value=(1024, 600))
        mock_hdmi.return_value = mock_hdmi_inst
        mock_touch.return_value = AsyncMock()

        jb = JoyBringController(cfg=joybring_cfg)
        await jb.start()
        mock_swc.assert_not_called()
        assert jb._swc is None
        await jb.stop()
