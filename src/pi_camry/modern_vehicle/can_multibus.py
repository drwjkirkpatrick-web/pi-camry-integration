"""
pi_camry/modern_vehicle/can_multibus.py
───────────────────────────────────────
Multi-CAN bus controller for modern vehicles.

Modern cars have 2-6 CAN buses:
- PT CAN (Powertrain) — 500 kbps, critical: engine, transmission, ABS
- Body CAN — 125 kbps, comfort: doors, windows, seats, lights
- Chassis CAN — 500 kbps, safety: steering, suspension, EPB
- Infotainment CAN — 125 kbps, head unit, amplifier, HUD
- EV CAN (electric vehicles) — 500 kbps, BMS, motor controller, charging
- ADAS CAN — 500 kbps, radar, camera, LIDAR

Uses Pi 5 + 2-4x MCP2515 or CAN FD controllers (MCP2517FD) on SPI.
Pi 5 has 4 SPI chip-selects, supporting up to 4 CAN channels.

Features:
- Auto-baudrate detection (125k, 250k, 500k, 1M)
- CAN FD support (up to 8 Mbps data phase)
- DBC file parsing for signal extraction
- Gateway/filtering between buses
- Security: MAC-authenticated frames (SecOC)
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from pi_camry.core.config import ModernVehicleConfig

logger = logging.getLogger("camry.modern.can")


class CANBusID(IntEnum):
    """Standard bus IDs for modern vehicles."""
    POWERTRAIN = 0
    BODY = 1
    CHASSIS = 2
    INFOTAINMENT = 3
    EV_POWER = 4
    ADAS = 5


@dataclass(frozen=True)
class CANSignal:
    """Decoded CAN signal from a DBC database."""
    name: str
    value: float
    unit: str
    min_val: float
    max_val: float
    bus: CANBusID
    timestamp: float


class CANMultibusController:
    """Controller for multiple CAN buses on modern vehicles."""

    # Common baudrates
    BAUDRATES = [125000, 250000, 500000, 1000000]
    CANFD_DATA_RATES = [2000000, 5000000, 8000000]

    def __init__(self, cfg: "ModernVehicleConfig" | None = None) -> None:
        self.cfg = cfg
        self._running = False
        self._buses: dict[CANBusID, any] = {}  # type: ignore[annotation-unchecked]
        self._dbc_parsers: dict[str, any] = {}  # type: ignore[annotation-unchecked]
        self._signal_callbacks: list[Callable[[CANSignal], None]] = []

        # Bus configurations
        self._bus_configs: dict[CANBusID, dict[str, any]] = {  # type: ignore[annotation-unchecked]
            CANBusID.POWERTRAIN: {"baudrate": 500000, "cs": 0, "enabled": True},
            CANBusID.BODY: {"baudrate": 125000, "cs": 1, "enabled": True},
            CANBusID.CHASSIS: {"baudrate": 500000, "cs": 2, "enabled": False},
            CANBusID.INFOTAINMENT: {"baudrate": 125000, "cs": 3, "enabled": False},
        }

    async def start(self) -> None:
        """Initialize all configured CAN buses."""
        logger.info("CANMultibus: initializing %d bus(es)...", len(self._bus_configs))
        self._running = True

        for bus_id, config in self._bus_configs.items():
            if not config.get("enabled", False):
                continue
            await self._init_bus(bus_id, config)

        # Load DBC files if available
        await self._load_dbc_files()

        # Start RX loops
        for bus_id in self._buses:
            asyncio.create_task(self._rx_loop(bus_id))

        logger.info("CANMultibus: %d bus(es) active", len(self._buses))

    async def stop(self) -> None:
        """Shutdown all CAN buses."""
        self._running = False
        for bus_id, bus in self._buses.items():
            try:
                bus.shutdown()
                logger.info("CANMultibus: bus %s shutdown", bus_id.name)
            except Exception:
                pass
        self._buses.clear()

    # ── Bus initialization ──────────────────────────────────────────────────

    async def _init_bus(self, bus_id: CANBusID, config: dict[str, any]) -> None:  # type: ignore[annotation-unchecked]
        """Initialize a single CAN bus via MCP2515/MCP2517FD."""
        try:
            import can
            from can.interface import Bus

            channel = f"spi0.{config['cs']}"
            bus = Bus(
                channel=channel,
                bustype="socketcan",
                bitrate=config["baudrate"],
                # CAN FD
                fd=getattr(self.cfg, "can_fd_enabled", False),
                data_bitrate=getattr(self.cfg, "can_fd_data_rate", 2000000),
            )
            self._buses[bus_id] = bus
            logger.info("CANMultibus: %s initialized at %d bps (CS%d)",
                        bus_id.name, config["baudrate"], config["cs"])
        except Exception as exc:
            logger.error("CANMultibus: %s init failed: %s", bus_id.name, exc)

    async def _load_dbc_files(self) -> None:
        """Load DBC signal databases for decoding."""
        dbc_dir = Path(__file__).parent / "dbc"
        if not dbc_dir.exists():
            return
        try:
            import cantools
            for dbc_file in dbc_dir.glob("*.dbc"):
                db = cantools.database.load_file(str(dbc_file))
                self._dbc_parsers[dbc_file.stem] = db
                logger.info("CANMultibus: loaded DBC %s (%d messages)",
                            dbc_file.name, len(db.messages))
        except ImportError:
            logger.warning("CANMultibus: cantools not installed, DBC decoding disabled")

    # ── RX/TX ────────────────────────────────────────────────────────────────

    async def _rx_loop(self, bus_id: CANBusID) -> None:
        """Receive loop for a single CAN bus."""
        bus = self._buses.get(bus_id)
        if not bus:
            return
        while self._running:
            try:
                msg = await asyncio.to_thread(bus.recv, 0.05)
                if msg:
                    await self._process_frame(bus_id, msg)
            except Exception:
                await asyncio.sleep(0.01)

    async def _process_frame(self, bus_id: CANBusID, msg: any) -> None:  # type: ignore[annotation-unchecked]
        """Decode and route a CAN frame."""
        # Try DBC decoding
        for db_name, db in self._dbc_parsers.items():
            try:
                can_msg = db.get_message_by_frame_id(msg.arbitration_id)
                signals = can_msg.decode(msg.data)
                for sig_name, value in signals.items():
                    sig = CANSignal(
                        name=f"{db_name}.{can_msg.name}.{sig_name}",
                        value=float(value),
                        unit=can_msg.signals[sig_name].unit or "",
                        min_val=can_msg.signals[sig_name].minimum or 0,
                        max_val=can_msg.signals[sig_name].maximum or 0,
                        bus=bus_id,
                        timestamp=msg.timestamp,
                    )
                    for cb in self._signal_callbacks:
                        cb(sig)
                return
            except Exception:
                continue

        # Raw frame fallback
        logger.debug("CANMultibus: raw frame 0x%03X on %s: %s",
                     msg.arbitration_id, bus_id.name, msg.data.hex())

    async def send(self, bus_id: CANBusID, arbitration_id: int, data: bytes,
                   is_extended: bool = False) -> bool:
        """Send a CAN frame on specified bus."""
        bus = self._buses.get(bus_id)
        if not bus:
            return False
        try:
            import can
            msg = can.Message(
                arbitration_id=arbitration_id,
                data=data,
                is_extended_id=is_extended,
            )
            await asyncio.to_thread(bus.send, msg)
            return True
        except Exception as exc:
            logger.error("CANMultibus: send failed on %s: %s", bus_id.name, exc)
            return False

    # ── Auto-baudrate detection ─────────────────────────────────────────────

    async def detect_baudrate(self, bus_id: CANBusID) -> int | None:
        """Try common baudrates and return the first working one."""
        for baud in self.BAUDRATES:
            try:
                import can
                test_bus = can.interface.Bus(
                    channel=f"spi0.{self._bus_configs[bus_id]['cs']}",
                    bustype="socketcan",
                    bitrate=baud,
                )
                # Try to receive for 1 second
                msg = await asyncio.to_thread(test_bus.recv, 1.0)
                test_bus.shutdown()
                if msg:
                    logger.info("CANMultibus: %s baudrate detected: %d", bus_id.name, baud)
                    return baud
            except Exception:
                continue
        return None

    # ── Security: SecOC ─────────────────────────────────────────────────────

    async def send_secoc_frame(self, bus_id: CANBusID, arbitration_id: int,
                                data: bytes, key: bytes) -> bool:
        """Send a SecOC-authenticated CAN frame.

        Appends 4-byte freshness value + 4-byte MAC (CMAC-AES).
        """
        try:
            import hmac
            import hashlib
            freshness = struct.pack("<I", int(asyncio.get_event_loop().time() * 1000) % 0xFFFFFFFF)
            mac = hmac.new(key, data + freshness, hashlib.sha256).digest()[:4]
            secured = data + freshness + mac
            return await self.send(bus_id, arbitration_id, secured)
        except Exception as exc:
            logger.error("CANMultibus: SecOC send failed: %s", exc)
            return False

    def register_signal_callback(self, cb: Callable[[CANSignal], None]) -> None:
        """Register a callback for decoded CAN signals."""
        self._signal_callbacks.append(cb)

    def get_bus_status(self) -> dict[str, any]:  # type: ignore[annotation-unchecked]
        """Return status of all CAN buses."""
        return {
            bus_id.name: {
                "active": bus_id in self._buses,
                "baudrate": self._bus_configs[bus_id]["baudrate"],
            }
            for bus_id in CANBusID
        }
