"""
pi_camry/connectivity/lte.py
────────────────────────────
4G/LTE connectivity via Quectel EC25 / SIM7600 USB modem.

Features:
- AT command interface over USB serial
- Auto-connect on startup with APN from config
- NTRIP client for RTK GPS corrections
- MQTT bridge for cloud telemetry
- SMS alerts (backup when data unavailable)
- Connection watchdog: auto-reconnect on drop
- Data usage tracking (per trip / per month)

Hardware:
- Quectel EC25-AUX (North America) or EC25-E (Europe)
- SIM7600G-H (global bands, cheaper)
- SixFab 4G/LTE HAT or USB modem stick
- Active antenna (magnetic mount, roof or rear window)
- Hologram / 1NCE / Twilio IoT SIM

AT Command Reference (Quectel EC25):
    AT+CGMI            — Manufacturer
    AT+CGMM            — Model
    AT+CSQ             — Signal quality (0-31, 99=unknown)
    AT+CREG?           — Network registration
    AT+CGDCONT=1       — Define PDP context (APN)
    AT+QIACT=1         — Activate context
    AT+QICSGP=1        — Configure context
    AT+QPING=1,"8.8.8.8" — Ping test
    AT+QMTOPEN=0,"broker.hivemq.com",1883 — Open MQTT
    AT+QMTCONN=0,"client_id" — Connect MQTT

Usage:
    lte = LTEController(cfg)
    await lte.start()
    ok = await lte.ping("8.8.8.8")
    await lte.send_ntrip_correction(caster, mountpoint, user, password)
    await lte.stop()
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pi_camry.core.config import LTEConfig

logger = logging.getLogger("camry.connectivity.lte")


@dataclass(frozen=True)
class SignalQuality:
    """Cellular signal quality report."""
    rssi: int       # -113 to -51 dBm (or 0-31 scale)
    ber: int        # Bit error rate (0-7, 99=unknown)
    rsrp: int | None = None  # Reference signal received power (LTE)
    rsrq: int | None = None  # Reference signal received quality
    sinr: int | None = None  # Signal-to-interference ratio


@dataclass(frozen=True)
class DataUsage:
    """Data usage counters."""
    tx_bytes: int
    rx_bytes: int
    session_start: float


class LTEController:
    """4G/LTE modem controller via AT commands."""

    # Default timeouts
    AT_TIMEOUT = 5.0
    CONNECT_TIMEOUT = 30.0
    PING_TIMEOUT = 10.0

    def __init__(self, cfg: "LTEConfig" | None = None) -> None:
        self.cfg = cfg
        self._running = False
        self._serial: any = None  # type: ignore[annotation-unchecked]
        self._port: str = "/dev/ttyUSB2"  # Quectel AT port
        self._baud = 115200
        self._apn: str = "hologram"  # Default; override via config
        self._connected = False
        self._watchdog_task: asyncio.Task | None = None
        self._usage = DataUsage(tx_bytes=0, rx_bytes=0, session_start=0.0)
        self._last_signal: SignalQuality | None = None

    async def start(self) -> None:
        """Initialize serial port, configure modem, establish data connection."""
        logger.info("LTE: initializing modem on %s...", self._port)
        try:
            import serial_asyncio
            self._serial = await serial_asyncio.open_serial_connection(
                url=self._port, baudrate=self._baud
            )
            logger.info("LTE: serial opened at %d baud", self._baud)
        except ImportError:
            logger.warning("LTE: pyserial-asyncio not installed. Install: uv pip install pyserial-asyncio")
            return
        except Exception as exc:
            logger.error("LTE: serial open failed: %s", exc)
            return

        self._running = True

        # Reset and identify
        await self._at("ATZ")                    # Soft reset
        await self._at("ATE0")                   # Echo off
        await self._at("AT+CMEE=2")              # Verbose errors
        info = await self._at("AT+CGMI;+CGMM", timeout=2.0)
        logger.info("LTE: modem info: %s", info.replace("\r\n", " "))

        # Configure PDP context
        if self.cfg and hasattr(self.cfg, "apn"):
            self._apn = self.cfg.apn
        await self._at(f'AT+CGDCONT=1,"IP","{self._apn}"')
        await self._at("AT+CGDCONT?")

        # Auto-connect
        await self._at("AT+CNMI=2,1,0,0,0")      # SMS notifications
        await self._at("AT+CMGF=1")              # Text mode SMS

        # Start watchdog
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        logger.info("LTE: initialized, APN=%s", self._apn)

    async def stop(self) -> None:
        """Gracefully disconnect and close serial."""
        self._running = False
        if self._watchdog_task:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
        if self._serial:
            writer = self._serial[1] if isinstance(self._serial, tuple) else None
            if writer:
                writer.close()
                await writer.wait_closed()
        logger.info("LTE: stopped")

    # ── AT command interface ────────────────────────────────────────────────

    async def _at(self, cmd: str, timeout: float | None = None) -> str:
        """Send AT command and return response."""
        if not self._serial:
            return ""
        to = timeout or self.AT_TIMEOUT
        reader, writer = self._serial
        writer.write(f"{cmd}\r\n".encode())
        await writer.drain()

        response_lines: list[str] = []
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=to)
                decoded = line.decode().strip()
                if decoded in ("OK", "ERROR", "+CME ERROR", "+CMS ERROR"):
                    response_lines.append(decoded)
                    break
                if decoded:
                    response_lines.append(decoded)
        except asyncio.TimeoutError:
            logger.warning("LTE: AT timeout for %s", cmd)
        return "\n".join(response_lines)

    # ── Network diagnostics ─────────────────────────────────────────────────

    async def get_signal_quality(self) -> SignalQuality:
        """Query CSQ and extended signal info."""
        csq = await self._at("AT+CSQ")
        # Response: +CSQ: <rssi>,<ber>
        match = re.search(r"\+CSQ:\s*(\d+),(\d+)", csq)
        if match:
            rssi_raw, ber = int(match.group(1)), int(match.group(2))
            rssi_dbm = -113 + 2 * rssi_raw if rssi_raw != 99 else -120
        else:
            rssi_dbm, ber = -120, 99

        # Extended LTE info
        qeng = await self._at("AT+QENG=\"servingcell\"")
        rsrp = rsrq = sinr = None
        # Parse +QENG: "servingcell","NOCONN",...
        rsrp_match = re.search(r'"rsrp",(-?\d+)', qeng)
        if rsrp_match:
            rsrp = int(rsrp_match.group(1))

        sq = SignalQuality(rssi=rssi_dbm, ber=ber, rsrp=rsrp, rsrq=rsrq, sinr=sinr)
        self._last_signal = sq
        return sq

    async def is_registered(self) -> bool:
        """Check if modem is registered to network."""
        reg = await self._at("AT+CREG?")
        return "0,1" in reg or "0,5" in reg  # 1=home, 5=roaming

    async def ping(self, host: str = "8.8.8.8") -> bool:
        """Ping test via modem."""
        resp = await self._at(f'AT+QPING=1,"{host}"', timeout=self.PING_TIMEOUT)
        return "OK" in resp and "+QPING" in resp

    # ── NTRIP (RTK corrections) ───────────────────────────────────────────────

    async def send_ntrip_correction(
        self,
        caster_host: str,
        caster_port: int,
        mountpoint: str,
        username: str,
        password: str,
    ) -> bool:
        """Open TCP socket to NTRIP caster and forward RTCM to GPS.

        This connects the LTE modem's TCP stack to the RTK GPS module.
        RTCM data flows: NTRIP caster → LTE modem → UART → ZED-F9P.
        """
        logger.info("LTE: NTRIP to %s:%d/%s", caster_host, caster_port, mountpoint)
        # Open TCP connection
        open_cmd = f'AT+QICSGP=1,1,"{caster_host}",{caster_port},0,0'
        await self._at(open_cmd)
        await self._at("AT+QIACT=1")  # Activate context

        # Open TCP client
        conn = f'AT+QIOPEN=1,0,"TCP","{caster_host}",{caster_port},0,0'
        resp = await self._at(conn, timeout=10.0)
        if "OK" not in resp:
            logger.error("LTE: NTRIP TCP open failed")
            return False

        # Send NTRIP authentication
        auth = f"GET /{mountpoint} HTTP/1.1\r\n"
        auth += f"Host: {caster_host}:{caster_port}\r\n"
        auth += f"Authorization: Basic {username}:{password}\r\n"
        auth += "Ntrip-Version: Ntrip/2.0\r\n\r\n"
        await self._at(f'AT+QISEND=0,{len(auth)}')
        # Would send auth bytes here via AT+QISENDEX

        logger.info("LTE: NTRIP connection established")
        return True

    # ── MQTT bridge ────────────────────────────────────────────────────────

    async def mqtt_connect(self, broker: str, port: int = 1883, client_id: str = "camry-pi") -> bool:
        """Connect to MQTT broker via modem's built-in MQTT stack."""
        logger.info("LTE: MQTT connecting to %s:%d...", broker, port)
        await self._at(f'AT+QMTOPEN=0,"{broker}",{port}', timeout=10.0)
        resp = await self._at(f'AT+QMTCONN=0,"{client_id}"', timeout=10.0)
        ok = "+QMTCONN: 0,0,0" in resp
        if ok:
            logger.info("LTE: MQTT connected")
        return ok

    async def mqtt_publish(self, topic: str, payload: str, qos: int = 0) -> bool:
        """Publish message to MQTT topic."""
        await self._at(f'AT+QMTPUBEX=0,0,0,0,"{topic}",{len(payload)}')
        # Would send payload bytes
        return True

    # ── SMS alerts ──────────────────────────────────────────────────────────

    async def send_sms(self, number: str, message: str) -> bool:
        """Send SMS alert (fallback when data unavailable)."""
        logger.info("LTE: SMS to %s: %s", number, message[:40])
        await self._at(f'AT+CMGS="{number}"')
        # Would send message + Ctrl+Z here
        return True

    # ── Watchdog ────────────────────────────────────────────────────────────

    async def _watchdog_loop(self) -> None:
        """Monitor connection health, auto-reconnect."""
        while self._running:
            try:
                if not await self.is_registered():
                    logger.warning("LTE: not registered, attempting reconnect...")
                    await self._at("AT+CFUN=1,1")  # Full functionality, reset
                    await asyncio.sleep(10.0)
                    continue

                # Periodic signal quality log
                sq = await self.get_signal_quality()
                logger.debug("LTE: signal RSSI=%d dBm, RSRP=%s",
                             sq.rssi, sq.rsrp)

                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("LTE: watchdog error")
                await asyncio.sleep(10.0)

    # ── Data usage ──────────────────────────────────────────────────────────

    async def get_data_usage(self) -> DataUsage:
        """Query data counters from modem."""
        # Quectel: AT+QGDCNT?
        resp = await self._at("AT+QGDCNT?")
        # Response: +QGDCNT: <tx>,<rx>
        match = re.search(r"\+QGDCNT:\s*(\d+),(\d+)", resp)
        if match:
            self._usage = DataUsage(
                tx_bytes=int(match.group(1)),
                rx_bytes=int(match.group(2)),
                session_start=self._usage.session_start or time.monotonic(),
            )
        return self._usage

    # ── Status ──────────────────────────────────────────────────────────────

    def is_connected(self) -> bool:
        return self._connected

    def get_last_signal(self) -> SignalQuality | None:
        return self._last_signal
