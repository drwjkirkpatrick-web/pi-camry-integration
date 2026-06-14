"""
Tests for pi_camry.telegram.bot — TelegramBot with mocked python-telegram-bot.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pi_camry.core import EventType
from pi_camry.telegram.bot import AlertMessage, TelegramBot


# ── Conditionally skip if python-telegram-bot unavailable ──
pytest.importorskip("telegram", reason="python-telegram-bot not installed")


@pytest.mark.asyncio
async def test_telegram_start_stop(mock_telegram_app: MagicMock, mock_config: Any) -> None:
    """TelegramBot should initialize and start/stop without error."""
    bot = TelegramBot()
    # Ensure config has a token so start() proceeds
    bot.cfg.bot_token = "FAKE_TOKEN"
    bot.cfg.enabled = True
    await bot.start()
    assert bot._running is True
    mock_telegram_app.initialize.assert_awaited_once()
    mock_telegram_app.start.assert_awaited_once()
    await bot.stop()
    assert bot._running is False
    mock_telegram_app.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_disabled_when_no_token(mock_telegram_app: MagicMock) -> None:
    """If bot_token is empty, start() should return early."""
    bot = TelegramBot()
    bot.cfg.enabled = True
    bot.cfg.bot_token = ""
    await bot.start()
    assert bot._app is None
    assert bot._running is False


@pytest.mark.asyncio
async def test_telegram_send_alert_queued(mock_telegram_app: MagicMock) -> None:
    """send_alert should place message into the internal queue."""
    bot = TelegramBot()
    bot.cfg.allowed_chat_ids = [123456789]
    bot._running = True
    bot._alert_task = asyncio.create_task(bot._alert_loop())
    alert = AlertMessage(severity="warning", title="Test", body="Hello")
    await bot.send_alert(alert)
    # Give loop a tick to process
    await asyncio.sleep(0.1)
    bot._running = False
    bot._alert_task.cancel()
    try:
        await bot._alert_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_telegram_cmd_start(mock_telegram_app: MagicMock) -> None:
    """_cmd_start should reply with welcome text."""
    bot = TelegramBot()
    mock_update = MagicMock()
    mock_update.effective_message = MagicMock()
    mock_update.effective_message.reply_text = AsyncMock()
    mock_context = MagicMock()
    await bot._cmd_start(mock_update, mock_context)
    mock_update.effective_message.reply_text.assert_awaited_once()
    text = mock_update.effective_message.reply_text.await_args[0][0]
    assert "Hermes Camry Bot" in text


@pytest.mark.asyncio
async def test_telegram_cmd_location(mock_telegram_app: MagicMock) -> None:
    """_cmd_location should reply with location info."""
    bot = TelegramBot()
    mock_update = MagicMock()
    mock_update.effective_message = MagicMock()
    mock_update.effective_message.reply_text = AsyncMock()
    mock_context = MagicMock()
    await bot._cmd_location(mock_update, mock_context)
    mock_update.effective_message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_cmd_lock(mock_telegram_app: MagicMock) -> None:
    """_cmd_lock should reply with anti-theft enabled message."""
    bot = TelegramBot()
    mock_update = MagicMock()
    mock_update.effective_message = MagicMock()
    mock_update.effective_message.reply_text = AsyncMock()
    mock_context = MagicMock()
    await bot._cmd_lock(mock_update, mock_context)
    mock_update.effective_message.reply_text.assert_awaited_once()
    text = mock_update.effective_message.reply_text.await_args[0][0]
    assert "Anti-theft ENABLED" in text


@pytest.mark.asyncio
async def test_telegram_cmd_unlock(mock_telegram_app: MagicMock) -> None:
    """_cmd_unlock should reply with anti-theft disabled message."""
    bot = TelegramBot()
    mock_update = MagicMock()
    mock_update.effective_message = MagicMock()
    mock_update.effective_message.reply_text = AsyncMock()
    mock_context = MagicMock()
    await bot._cmd_unlock(mock_update, mock_context)
    mock_update.effective_message.reply_text.assert_awaited_once()
    text = mock_update.effective_message.reply_text.await_args[0][0]
    assert "Anti-theft DISABLED" in text


@pytest.mark.asyncio
async def test_telegram_cmd_climate_on(mock_telegram_app: MagicMock) -> None:
    """_cmd_climate with 'on' arg should reply with climate ON."""
    bot = TelegramBot()
    mock_update = MagicMock()
    mock_update.effective_message = MagicMock()
    mock_update.effective_message.reply_text = AsyncMock()
    mock_context = MagicMock()
    mock_context.args = ["on"]
    await bot._cmd_climate(mock_update, mock_context)
    mock_update.effective_message.reply_text.assert_awaited_once()
    text = mock_update.effective_message.reply_text.await_args[0][0]
    assert "Climate ON" in text


@pytest.mark.asyncio
async def test_telegram_event_handlers_subscribe(mock_telegram_app: MagicMock) -> None:
    """_subscribe_events should register bus handlers for configured alerts."""
    bot = TelegramBot()
    bot.cfg.alert_on_collision = True
    bot.cfg.alert_on_geofence = True
    bot.cfg.alert_on_cel = True
    bot.cfg.alert_on_tow = True
    bot._subscribe_events()
    # The handlers are registered as callbacks; no exception = success


@pytest.mark.asyncio
async def test_telegram_get_location_str_fallback() -> None:
    """_get_location_str should return fallback when no GPS fix."""
    bot = TelegramBot()
    loc = await bot._get_location_str()
    assert "Lat: --" in loc
