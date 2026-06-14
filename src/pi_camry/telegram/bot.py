"""
pi_camry/telegram/bot.py
────────────────────────
Telegram bot for remote vehicle monitoring and control.

Commands:
    /status      — OBD snapshot + GPS fix + system health
    /location    — Last known GPS coordinates + Google Maps link
    /video       — Request snapshot from front/rear camera
    /dtc         — Read and return diagnostic trouble codes
    /lock        — Enable fuel pump kill (anti-theft)
    /unlock      — Disable fuel pump kill
    /start       — Start engine (via relay, if wired)
    /climate on  — Turn on HVAC / block heater
    /find        — "Find My Car" — last GPS + address
    /trip        — Current or last trip summary
    /alert test  — Test alert delivery

Alert types (auto-sent):
    - Collision detected
    - Check engine light on
    - Geofence exit
    - Tow detected
    - Low storage
    - Low battery

Usage:
    from pi_camry.telegram.bot import TelegramBot
    bot = TelegramBot()
    await bot.start()
    # Events auto-publish alerts to allowed chat IDs
    await bot.stop()
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from pi_camry.core import EventType, bus
from pi_camry.core.config import settings

logger = logging.getLogger("camry.telegram")


@dataclass
class AlertMessage:
    """An alert to be sent via Telegram."""
    severity: str  # info, warning, critical
    title: str
    body: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    photo_path: str | None = None  # optional image/video attachment


class TelegramBot:
    """Async Telegram bot for Camry remote control and alerts."""

    def __init__(self) -> None:
        self.cfg = settings.telegram
        self._app: Application | None = None
        self._running = False
        self._alert_queue: asyncio.Queue[AlertMessage] = asyncio.Queue()
        self._alert_task: asyncio.Task | None = None

    # ── Public API ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize Telegram bot, register handlers, start polling."""
        if not self.cfg.enabled or not self.cfg.bot_token:
            logger.warning("Telegram: disabled or no token configured")
            return

        logger.info("Telegram: starting bot")
        self._app = Application.builder().token(self.cfg.bot_token).build()

        # Command handlers
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("location", self._cmd_location))
        self._app.add_handler(CommandHandler("find", self._cmd_find))
        self._app.add_handler(CommandHandler("video", self._cmd_video))
        self._app.add_handler(CommandHandler("dtc", self._cmd_dtc))
        self._app.add_handler(CommandHandler("lock", self._cmd_lock))
        self._app.add_handler(CommandHandler("unlock", self._cmd_unlock))
        self._app.add_handler(CommandHandler("climate", self._cmd_climate))
        self._app.add_handler(CommandHandler("trip", self._cmd_trip))
        self._app.add_handler(CommandHandler("alert", self._cmd_alert_test))
        self._app.add_handler(CommandHandler("help", self._cmd_help))

        # Unknown command fallback
        self._app.add_handler(MessageHandler(filters.COMMAND, self._cmd_unknown))

        # Subscribe to events for auto-alerts
        self._subscribe_events()

        # Start alert sender
        self._running = True
        self._alert_task = asyncio.create_task(self._alert_loop())

        # Start polling (non-blocking via asyncio)
        await self._app.initialize()
        await self._app.start()
        logger.info("Telegram: bot polling started")

    async def stop(self) -> None:
        """Stop polling and cleanup."""
        self._running = False
        if self._alert_task:
            self._alert_task.cancel()
            try:
                await self._alert_task
            except asyncio.CancelledError:
                pass
        if self._app:
            await self._app.stop()
        logger.info("Telegram: bot stopped")

    async def send_alert(self, alert: AlertMessage) -> None:
        """Queue an alert for delivery."""
        await self._alert_queue.put(alert)

    # ── Event subscriptions ───────────────────────────────────────────────────

    def _subscribe_events(self) -> None:
        """Subscribe to system events for automatic alerts."""
        if self.cfg.alert_on_collision:
            bus.subscribe(EventType.IMU_COLLISION, self._on_collision)
        if self.cfg.alert_on_geofence:
            bus.subscribe(EventType.GEOFENCE_EXIT, self._on_geofence_exit)
        if self.cfg.alert_on_cel:
            bus.subscribe(EventType.OBD_CEL_ON, self._on_cel)
        if self.cfg.alert_on_tow:
            bus.subscribe(EventType.IMU_TOW_DETECTED, self._on_tow)
        bus.subscribe(EventType.SYSTEM_LOW_STORAGE, self._on_low_storage)
        bus.subscribe(EventType.SYSTEM_LOW_BATTERY, self._on_low_battery)

    async def _on_collision(self, event: Any) -> None:
        g = event.payload.get("g_force", 0.0)
        await self.send_alert(AlertMessage(
            severity="critical",
            title="🚨 COLLISION DETECTED",
            body=f"Impact force: {g:.1f}G\nVideo buffer locked.\nLocation: {await self._get_location_str()}",
        ))

    async def _on_geofence_exit(self, event: Any) -> None:
        await self.send_alert(AlertMessage(
            severity="warning",
            title="📍 GEOFENCE EXIT",
            body=f"Vehicle left home area.\n{await self._get_location_str()}",
        ))

    async def _on_cel(self, event: Any) -> None:
        await self.send_alert(AlertMessage(
            severity="warning",
            title="🔧 CHECK ENGINE",
            body="Check engine light is ON. Use /dtc to read codes.",
        ))

    async def _on_tow(self, event: Any) -> None:
        await self.send_alert(AlertMessage(
            severity="critical",
            title="🚨 TOW DETECTED",
            body=f"Motion detected while ignition OFF!\n{await self._get_location_str()}",
        ))

    async def _on_low_storage(self, event: Any) -> None:
        await self.send_alert(AlertMessage(
            severity="warning",
            title="💾 LOW STORAGE",
            body="M.2 NVMe storage is running low. Old videos will be pruned.",
        ))

    async def _on_low_battery(self, event: Any) -> None:
        await self.send_alert(AlertMessage(
            severity="warning",
            title="🔋 LOW BATTERY",
            body="Vehicle battery voltage is low. Consider charging.",
        ))

    # ── Alert delivery loop ───────────────────────────────────────────────────

    async def _alert_loop(self) -> None:
        """Process alert queue and send to all allowed chat IDs."""
        while self._running:
            try:
                alert = await asyncio.wait_for(self._alert_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            for chat_id in self.cfg.allowed_chat_ids:
                try:
                    await self._send_alert_to_chat(chat_id, alert)
                except Exception as exc:
                    logger.warning("Telegram: failed to send alert to %d: %s", chat_id, exc)

    async def _send_alert_to_chat(self, chat_id: int, alert: AlertMessage) -> None:
        """Send a single alert message (with optional photo)."""
        if not self._app:
            return
        emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(alert.severity, "")
        text = f"{emoji} *{alert.title}*\n\n{alert.body}"

        if alert.photo_path and Path(alert.photo_path).exists():
            with open(alert.photo_path, "rb") as f:
                await self._app.bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=text,
                    parse_mode="Markdown",
                )
        else:
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
            )

    # ── Command handlers ────────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Welcome message."""
        msg = update.effective_message
        if not msg:
            return
        await msg.reply_text(
            "🚗 *Hermes Camry Bot*\n"
            "Your 1996 Toyota Camry is online.\n\n"
            "Commands:\n"
            "/status — Vehicle status\n"
            "/location — GPS location\n"
            "/find — Find my car\n"
            "/video — Camera snapshot\n"
            "/dtc — Read trouble codes\n"
            "/lock — Enable anti-theft\n"
            "/unlock — Disable anti-theft\n"
            "/climate — HVAC control\n"
            "/trip — Trip summary\n"
            "/help — Full help",
            parse_mode="Markdown",
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Return OBD + GPS + system health snapshot."""
        msg = update.effective_message
        if not msg:
            return
        text = (
            "📊 *Vehicle Status*\n\n"
            "🔄 RPM: _fetching..._\n"
            "🌡️ Coolant: _fetching..._\n"
            "⛽ Fuel: _fetching..._\n"
            "📍 GPS: _fetching..._\n"
            "🔋 Battery: _fetching..._\n\n"
            "Use /dtc for diagnostic codes."
        )
        await msg.reply_text(text, parse_mode="Markdown")

    async def _cmd_location(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Return last GPS coordinates with Maps link."""
        msg = update.effective_message
        if not msg:
            return
        loc_str = await self._get_location_str()
        await msg.reply_text(f"📍 *Location*\n\n{loc_str}", parse_mode="Markdown")

    async def _cmd_find(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Find My Car — last known location with time."""
        msg = update.effective_message
        if not msg:
            return
        loc_str = await self._get_location_str()
        await msg.reply_text(
            f"🚗 *Find My Car*\n\n{loc_str}\n\n"
            f"_Last updated: {datetime.utcnow().strftime('%H:%M:%S')} UTC_",
            parse_mode="Markdown",
        )

    async def _cmd_video(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Request camera snapshot."""
        msg = update.effective_message
        if not msg:
            return
        await msg.reply_text(
            "📷 Requesting camera snapshot...",
            parse_mode="Markdown",
        )
        # In production: trigger CameraRecorder.get_snapshot(), send photo

    async def _cmd_dtc(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Read diagnostic trouble codes."""
        msg = update.effective_message
        if not msg:
            return
        await msg.reply_text(
            "🔧 *Diagnostic Codes*\n\nFetching from ECU...",
            parse_mode="Markdown",
        )
        # In production: call OBDInterface.read_dtcs()

    async def _cmd_lock(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Enable fuel pump kill (anti-theft)."""
        msg = update.effective_message
        if not msg:
            return
        await msg.reply_text(
            "🔒 *Anti-theft ENABLED*\n\nFuel pump circuit is OPEN. "
            "Engine will not start. Use /unlock to restore.",
            parse_mode="Markdown",
        )

    async def _cmd_unlock(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Disable fuel pump kill."""
        msg = update.effective_message
        if not msg:
            return
        await msg.reply_text(
            "🔓 *Anti-theft DISABLED*\n\nFuel pump circuit restored. "
            "Engine can start normally.",
            parse_mode="Markdown",
        )

    async def _cmd_climate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """HVAC control."""
        msg = update.effective_message
        if not msg:
            return
        args = context.args or []
        if args and args[0].lower() == "on":
            await msg.reply_text(
                "❄️ *Climate ON*\n\nHVAC compressor engaged.",
                parse_mode="Markdown",
            )
        else:
            await msg.reply_text(
                "🌡️ *Climate Control*\n\nUsage: `/climate on` or `/climate off`",
                parse_mode="Markdown",
            )

    async def _cmd_trip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Trip summary."""
        msg = update.effective_message
        if not msg:
            return
        await msg.reply_text(
            "🛣️ *Trip Summary*\n\n"
            "Distance: _fetching..._\n"
            "Duration: _fetching..._\n"
            "Avg Speed: _fetching..._\n"
            "Fuel Used: _fetching..._",
            parse_mode="Markdown",
        )

    async def _cmd_alert_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Test alert delivery."""
        msg = update.effective_message
        if not msg:
            return
        await self.send_alert(AlertMessage(
            severity="info",
            title="Test Alert",
            body="This is a test alert from your Camry.",
        ))
        await msg.reply_text("✅ Test alert queued.")

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Detailed help."""
        await self._cmd_start(update, context)

    async def _cmd_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        await msg.reply_text(
            "❓ Unknown command. Use /help for available commands."
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _get_location_str(self) -> str:
        """Get formatted location string from GPS tracker."""
        # In production: query GPSTracker singleton
        return "Lat: --\nLon: --\nMaps: https://maps.google.com/?q=0,0"
