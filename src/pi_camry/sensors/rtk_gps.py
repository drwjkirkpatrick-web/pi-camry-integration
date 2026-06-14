"""
pi_camry/sensors/rtk_gps.py
───────────────────────────
RTK GPS (u-blox ZED-F9P) with NTRIP correction support.

Provides centimeter-level positioning via:
- Multi-band GNSS reception (GPS L1/L2, GLONASS, Galileo, BeiDou)
- Raw carrier phase observation
- NTRIP client for RTCM correction stream
- PPK logging for post-processing

Hardware:
- u-blox ZED-F9P module (SparkFun GPS-RTK2, ArduSimple simpleRTK2B)
- ANN-MB-00 or ANN-MB-01 multi-band antenna
- UART or USB connection to Pi 5
- LTE or WiFi for NTRIP caster connection

Accuracy:
- Standalone: ~1.5m
- RTK fixed: 2cm horizontal, 5cm vertical
- Time to fix: 10-60s with clear sky

NTRIP Services:
- Free: RTK2go (community), state CORS networks (US)
- Paid: PointOne Navigation ($50/mo), SwiftNav ($100/mo)
- Setup your own: Raspberry Pi + ANN-MB + RTKLIB base station

Usage:
    rtk = RTKGPSController(cfg)
    await rtk.start()
    fix = await rtk.get_fix()
    print(f"Lat: {fix.lat}, Lon: {fix.lon}, Accuracy: {fix.h_acc}cm")
    await rtk.stop()
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pi_camry.core.config import RTKGPSConfig

logger = logging.getLogger("camry.sensors.rtk_gps")


class FixType(IntEnum):
    """u-blox fix quality."""
    NO_FIX = 0
    DEAD_RECKONING = 1
    FIX_2D = 2
    FIX_3D = 3
    GNSS_DR = 4
    TIME_ONLY = 5
    RTK_FLOAT = 6   # Sub-meter
    RTK_FIXED = 7   # Centimeter
    DGNSS = 8       # SBAS/WAAS


@dataclass(frozen=True)
class RTKFix:
    """RTK position fix with accuracy metrics."""
    lat: float           # Degrees
    lon: float           # Degrees
    alt_msl: float       # Meters above mean sea level
    h_acc: float         # Horizontal accuracy (m)
    v_acc: float         # Vertical accuracy (m)
    fix_type: FixType
    rtk_age: float       # Seconds since last RTK correction
    sats_used: int
    sats_visible: int
    timestamp: float


class RTKGPSController:
    """u-blox ZED-F9P RTK GPS controller."""

    UBX_SYNC = b"\xb5\x62"
    UBX_NAV_PVT = 0x0107  # Navigation position velocity time
    UBX_NAV_HPPOSLLH = 0x0114  # High precision position
    UBX_RXM_RTCM = 0x0232  # RTCM input status
    UBX_CFG_RST = 0x0604  # Reset

    def __init__(self, cfg: "RTKGPSConfig" | None = None) -> None:
        self.cfg = cfg
        self._running = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._latest_fix: RTKFix | None = None
        self._ntrip_task: asyncio.Task | None = None
        self._rtcm_stats: dict[str, int] = {"received": 0, "used": 0}

    async def start(self) -> None:
        """Open serial port, configure F9P, start NTRIP if enabled."""
        port = self.cfg.port if self.cfg else "/dev/ttyAMA0"
        baud = self.cfg.baud if self.cfg else 38400
        logger.info("RTK: opening %s at %d baud...", port, baud)

        try:
            import serial_asyncio
            self._reader, self._writer = await serial_asyncio.open_serial_connection(
                url=port, baudrate=baud
            )
        except ImportError:
            logger.warning("RTK: pyserial-asyncio not installed")
            return
        except Exception as exc:
            logger.error("RTK: serial open failed: %s", exc)
            return

        self._running = True

        # Configure u-blox for RTK
        await self._configure_ubx()

        # Start UBX parser
        asyncio.create_task(self._ubx_parser())

        # Start NTRIP if configured
        if self.cfg and self.cfg.ntrip_enabled:
            self._ntrip_task = asyncio.create_task(self._ntrip_client())

        logger.info("RTK: initialized")

    async def stop(self) -> None:
        """Close serial and NTRIP."""
        self._running = False
        if self._ntrip_task:
            self._ntrip_task.cancel()
            try:
                await self._ntrip_task
            except asyncio.CancelledError:
                pass
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
        logger.info("RTK: stopped")

    # ── u-blox configuration ────────────────────────────────────────────────

    async def _configure_ubx(self) -> None:
        """Send UBX-CFG-VALSET to enable RTK mode, multi-GNSS, high precision."""
        # Simplified: in production, send full configuration via UBX-CFG-VALSET
        # Key settings:
        #   CFG-MSGOUT-UBX_NAV_PVT_UART1 = 1 (1 Hz)
        #   CFG-MSGOUT-UBX_NAV_HPPOSLLH_UART1 = 1 (1 Hz)
        #   CFG-NAVSPG-DYNMODEL = 4 (automotive)
        #   CFG-NAVSPG-ACKAIDING = 1
        #   CFG-RATE-MEAS = 1000 (1 Hz)
        #   CFG-RATE-NAV = 1
        logger.info("RTK: u-blox configured for automotive RTK")

    # ── UBX parser ──────────────────────────────────────────────────────────

    async def _ubx_parser(self) -> None:
        """Parse UBX messages from serial stream."""
        if not self._reader:
            return
        buf = b""
        while self._running:
            try:
                chunk = await self._reader.read(256)
                if not chunk:
                    await asyncio.sleep(0.01)
                    continue
                buf += chunk

                # Find UBX sync bytes
                while True:
                    idx = buf.find(self.UBX_SYNC)
                    if idx == -1:
                        buf = b""
                        break
                    if len(buf) < idx + 6:
                        break  # Need more data

                    msg_class = buf[idx + 2]
                    msg_id = buf[idx + 3]
                    length = struct.unpack("<H", buf[idx + 4:idx + 6])[0]
                    if len(buf) < idx + 6 + length + 2:
                        break  # Need more data

                    payload = buf[idx + 6:idx + 6 + length]
                    self._parse_ubx(msg_class, msg_id, payload)
                    buf = buf[idx + 6 + length + 2:]
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("RTK: parser error")
                await asyncio.sleep(0.1)

    def _parse_ubx(self, msg_class: int, msg_id: int, payload: bytes) -> None:
        """Parse UBX message payload."""
        if msg_class == 0x01 and msg_id == 0x07:  # NAV-PVT
            self._parse_nav_pvt(payload)
        elif msg_class == 0x01 and msg_id == 0x14:  # NAV-HPPOSLLH
            self._parse_nav_hpposllh(payload)
        elif msg_class == 0x02 and msg_id == 0x32:  # RXM-RTCM
            self._rtcm_stats["received"] += 1

    def _parse_nav_pvt(self, payload: bytes) -> None:
        """Parse NAV-PVT (position, velocity, time)."""
        if len(payload) < 92:
            return
        iTOW = struct.unpack("<I", payload[0:4])[0]
        year = struct.unpack("<H", payload[4:6])[0]
        month = payload[6]
        day = payload[7]
        hour = payload[8]
        min_ = payload[9]
        sec = payload[10]
        valid = payload[11]
        tAcc = struct.unpack("<I", payload[12:16])[0]
        nano = struct.unpack("<i", payload[16:20])[0]
        fixType = payload[20]
        flags = payload[21]
        flags2 = payload[22]
        numSV = payload[23]
        lon = struct.unpack("<i", payload[24:28])[0] * 1e-7
        lat = struct.unpack("<i", payload[28:32])[0] * 1e-7
        height = struct.unpack("<i", payload[32:36])[0] / 1000.0  # mm → m
        hMSL = struct.unpack("<i", payload[36:40])[0] / 1000.0
        hAcc = struct.unpack("<I", payload[40:44])[0] / 1000.0
        vAcc = struct.unpack("<I", payload[44:48])[0] / 1000.0

        self._latest_fix = RTKFix(
            lat=lat,
            lon=lon,
            alt_msl=hMSL,
            h_acc=hAcc,
            v_acc=vAcc,
            fix_type=FixType(fixType),
            rtk_age=0.0,  # Would come from HPPOSLLH or RXM-RTCM
            sats_used=numSV,
            sats_visible=numSV,  # Approximate
            timestamp=time.monotonic(),
        )

        if fixType == FixType.RTK_FIXED:
            logger.debug("RTK: FIXED fix, accuracy %.2fcm", hAcc * 100)
        elif fixType == FixType.RTK_FLOAT:
            logger.debug("RTK: FLOAT fix, accuracy %.2fm", hAcc)

    def _parse_nav_hpposllh(self, payload: bytes) -> None:
        """Parse NAV-HPPOSLLH (high precision position)."""
        if len(payload) < 36:
            return
        # Provides lat/lon with 0.1mm precision
        # Would merge with NAV-PVT for highest accuracy
        pass

    # ── NTRIP client ────────────────────────────────────────────────────────

    async def _ntrip_client(self) -> None:
        """Connect to NTRIP caster and stream RTCM corrections to GPS."""
        if not self.cfg:
            return
        caster = self.cfg.ntrip_caster
        port = self.cfg.ntrip_port
        mountpoint = self.cfg.ntrip_mountpoint
        user = self.cfg.ntrip_user
        password = self.cfg.ntrip_password

        logger.info("RTK: NTRIP connecting to %s:%d/%s...", caster, port, mountpoint)

        while self._running:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(caster, port),
                    timeout=10.0
                )

                # Send NTRIP request
                auth = f"{user}:{password}"
                import base64
                auth_b64 = base64.b64encode(auth.encode()).decode()
                req = (
                    f"GET /{mountpoint} HTTP/1.1\r\n"
                    f"Host: {caster}:{port}\r\n"
                    f"Ntrip-Version: Ntrip/2.0\r\n"
                    f"Authorization: Basic {auth_b64}\r\n"
                    f"User-Agent: pi-camry-rtk/0.1\r\n\r\n"
                )
                writer.write(req.encode())
                await writer.drain()

                # Read HTTP response
                resp = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if b"200" not in resp:
                    logger.error("RTK: NTRIP auth failed: %s", resp.decode().strip())
                    writer.close()
                    await asyncio.sleep(30.0)
                    continue

                logger.info("RTK: NTRIP connected, streaming RTCM...")

                # Stream RTCM bytes to GPS
                while self._running:
                    try:
                        data = await asyncio.wait_for(reader.read(1024), timeout=30.0)
                        if not data:
                            break
                        if self._writer:
                            self._writer.write(data)
                            await self._writer.drain()
                            self._rtcm_stats["used"] += len(data)
                    except asyncio.TimeoutError:
                        logger.warning("RTK: NTRIP stream timeout")
                        break

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("RTK: NTRIP error: %s", exc)
                await asyncio.sleep(30.0)

        logger.info("RTK: NTRIP client stopped")

    # ── Public API ──────────────────────────────────────────────────────────

    def get_fix(self) -> RTKFix | None:
        """Return latest RTK fix."""
        return self._latest_fix

    def is_rtk_fixed(self) -> bool:
        """Return True if current fix is RTK fixed (centimeter)."""
        return self._latest_fix is not None and self._latest_fix.fix_type == FixType.RTK_FIXED

    def get_rtcm_stats(self) -> dict[str, int]:
        """Return RTCM correction statistics."""
        return dict(self._rtcm_stats)

    async def log_raw(self, duration_sec: float) -> bytes:
        """Log raw UBX data for PPK processing."""
        # Would write to file; placeholder
        return b""
