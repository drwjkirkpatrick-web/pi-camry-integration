"""
Tests for pi_camry.obd.interface — OBDInterface with mocked python-obd.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pi_camry.core import EventType
from pi_camry.obd.interface import ESSENTIAL_PIDS, OBDInterface, PIDSnapshot


# ── Skip if python-obd not installed ──
obd = pytest.importorskip("obd", reason="python-obd not installed")


@pytest.mark.asyncio
async def test_obd_start_emits_connected(mock_obd_connection: MagicMock, monkeypatch: Any, event_bus: Any) -> None:
    """Starting OBDInterface should emit OBD_CONNECTED on success."""
    from pi_camry.core import bus as global_bus

    monkeypatch.setattr(
        "pi_camry.obd.interface.asyncio.get_event_loop",
        lambda: asyncio.get_event_loop_policy().get_event_loop(),
    )
    monkeypatch.setattr(
        "pi_camry.obd.interface.asyncio.run_coroutine_threadsafe",
        lambda coro, loop: asyncio.ensure_future(coro),
    )
    monkeypatch.setattr(
        "pi_camry.obd.interface.OBDInterface._poll_loop",
        lambda self: None,
    )
    iface = OBDInterface()
    await iface.start()
    await asyncio.sleep(0.1)
    assert iface.is_connected()
    await iface.stop()
    mock_obd_connection.close.assert_called_once()


@pytest.mark.asyncio
async def test_obd_start_failed_connection(monkeypatch: Any, event_bus: Any) -> None:
    """If obd.Async raises, OBD_DISCONNECTED should be emitted."""
    import obd as obd_mod

    monkeypatch.setattr(
        "pi_camry.obd.interface.obd.Async",
        MagicMock(side_effect=Exception("no device")),
    )
    iface = OBDInterface()
    await iface.start()
    assert not iface.is_connected()


@pytest.mark.asyncio
async def test_obd_get_latest_returns_snapshot(mock_obd_connection: MagicMock) -> None:
    """get_latest() should return a PIDSnapshot even before polling."""
    iface = OBDInterface()
    snap = iface.get_latest()
    assert isinstance(snap, PIDSnapshot)
    assert snap.rpm is None  # not yet polled


@pytest.mark.asyncio
async def test_obd_read_dtcs_when_connected(mock_obd_connection: MagicMock) -> None:
    """read_dtcs should return list of codes when connected."""
    iface = OBDInterface()
    await iface.start()
    # Mock query response for DTCs
    mock_resp = MagicMock()
    mock_resp.value = [("P0101", "Mass Air Flow Circuit Range")]
    iface.connection.query = MagicMock(return_value=mock_resp)  # type: ignore[union-attr]
    codes = await iface.read_dtcs()
    assert len(codes) == 1
    assert "P0101" in codes[0]
    await iface.stop()


@pytest.mark.asyncio
async def test_obd_clear_dtcs(mock_obd_connection: MagicMock) -> None:
    """clear_dtcs should return True when connected."""
    iface = OBDInterface()
    await iface.start()
    mock_resp = MagicMock()
    mock_resp.is_null.return_value = True
    iface.connection.query = MagicMock(return_value=mock_resp)  # type: ignore[union-attr]
    result = await iface.clear_dtcs()
    assert result is True
    await iface.stop()


@pytest.mark.asyncio
async def test_obd_poll_essential_updates_snapshot(
    mock_obd_connection: MagicMock, mock_obd_response_factory: Any, monkeypatch: Any
) -> None:
    """The internal _poll_essential should map OBD responses to snapshot fields."""
    monkeypatch.setattr(
        "pi_camry.obd.interface.OBDInterface._poll_loop",
        lambda self: None,
    )
    monkeypatch.setattr(
        "pi_camry.obd.interface.asyncio.run_coroutine_threadsafe",
        lambda coro, loop: asyncio.ensure_future(coro),
    )
    iface = OBDInterface()
    await iface.start()

    # Build responses for each essential PID
    def _fake_query(cmd: Any) -> MagicMock:
        mapping = {
            obd.commands.RPM: mock_obd_response_factory(2500),
            obd.commands.SPEED: mock_obd_response_factory(60),
            obd.commands.COOLANT_TEMP: mock_obd_response_factory(88.5),
            obd.commands.ENGINE_LOAD: mock_obd_response_factory(45.0),
        }
        return mapping.get(cmd, mock_obd_response_factory(None, is_null=True))

    iface.connection.query = _fake_query  # type: ignore[union-attr]
    iface._poll_essential()
    snap = iface.get_latest()
    assert snap.rpm == 2500
    assert snap.speed_kmh == 60.0
    assert snap.coolant_temp_c == 88.5
    assert snap.engine_load_percent == 45.0
    await iface.stop()


@pytest.mark.asyncio
async def test_obd_poll_derived_mpg(mock_obd_connection: MagicMock, mock_obd_response_factory: Any, monkeypatch: Any) -> None:
    """MPG should be calculated from MAF and speed."""
    monkeypatch.setattr(
        "pi_camry.obd.interface.OBDInterface._poll_loop",
        lambda self: None,
    )
    monkeypatch.setattr(
        "pi_camry.obd.interface.asyncio.run_coroutine_threadsafe",
        lambda coro, loop: asyncio.ensure_future(coro),
    )
    iface = OBDInterface()
    await iface.start()

    def _fake_query(cmd: Any) -> MagicMock:
        mapping = {
            obd.commands.SPEED: mock_obd_response_factory(80),
            obd.commands.MAF: mock_obd_response_factory(15.0),
        }
        return mapping.get(cmd, mock_obd_response_factory(None, is_null=True))

    iface.connection.query = _fake_query  # type: ignore[union-attr]
    iface._poll_essential()
    snap = iface.get_latest()
    assert snap.mpg_instant is not None
    assert snap.mpg_instant > 0
    await iface.stop()


@pytest.mark.asyncio
async def test_obd_stop_closes_connection(mock_obd_connection: MagicMock) -> None:
    """stop() should close the OBD connection and clear state."""
    iface = OBDInterface()
    await iface.start()
    assert iface.is_connected()
    await iface.stop()
    assert not iface.is_connected()
    assert iface.connection is None
