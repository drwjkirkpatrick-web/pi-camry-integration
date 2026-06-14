"""
pi_camry/modern_vehicle/uds_client.py
─────────────────────────────────────
Unified Diagnostic Services (UDS) client for modern ECU communication.

UDS (ISO 14229) is the modern replacement for OBD-II on CAN.
It provides:
- ReadDataByIdentifier (0x22) — read any ECU parameter
- WriteDataByIdentifier (0x2E) — write configuration
- RoutineControl (0x31) — run ECU routines (calibration, tests)
- RequestDownload (0x34) / TransferData (0x36) — ECU flashing
- SecurityAccess (0x27) — unlock ECU with seed/key
- CommunicationControl (0x28) — enable/disable CAN TX/RX
- TesterPresent (0x3E) — keep session alive

Supports:
- CAN-based UDS (ISO 15765-2 / ISO-TP)
- DoIP (Diagnostics over IP — Ethernet, modern VW/Audi/BMW)
- LIN-based UDS (door modules, seat modules)

Hardware:
- Pi 5 + MCP2515 (CAN) for ISO-TP
- Pi 5 Ethernet + DoIP for modern vehicles
- USB-LIN adapter (e.g., PCAN-USB Pro) for LIN diagnostics
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pi_camry.core.config import ModernVehicleConfig

logger = logging.getLogger("camry.modern.uds")


class UDS_Service(IntEnum):
    """UDS service IDs (ISO 14229-1)."""
    DIAGNOSTIC_SESSION_CONTROL = 0x10
    ECU_RESET = 0x11
    SECURITY_ACCESS = 0x27
    COMMUNICATION_CONTROL = 0x28
    TESTER_PRESENT = 0x3E
    READ_DATA_BY_IDENTIFIER = 0x22
    READ_MEMORY_BY_ADDRESS = 0x23
    WRITE_DATA_BY_IDENTIFIER = 0x2E
    WRITE_MEMORY_BY_ADDRESS = 0x3D
    ROUTINE_CONTROL = 0x31
    REQUEST_DOWNLOAD = 0x34
    REQUEST_UPLOAD = 0x35
    TRANSFER_DATA = 0x36
    REQUEST_TRANSFER_EXIT = 0x37
    CONTROL_DTC_SETTING = 0x85


class UDS_Session(IntEnum):
    """UDS diagnostic session types."""
    DEFAULT = 0x01
    PROGRAMMING = 0x02
    EXTENDED = 0x03
    SAFETY_SYSTEM = 0x04


@dataclass(frozen=True)
class ECUParameter:
    """Decoded ECU parameter from UDS ReadDataByIdentifier."""
    did: int  # Data Identifier (e.g., 0xF190 = VIN)
    name: str
    value: any  # type: ignore[annotation-unchecked]
    unit: str
    ecu_address: int


class UDSClient:
    """UDS diagnostic client for modern vehicle ECUs."""

    # Common DIDs (Data Identifiers)
    DID_VIN = 0xF190
    DID_ECU_NAME = 0xF197
    DID_SOFTWARE_VERSION = 0xF188
    DID_HARDWARE_VERSION = 0xF1A3
    DID_ODOMETER = 0xF260
    DID_BATTERY_VOLTAGE = 0xF401
    DID_ENGINE_TEMP = 0xF405
    DID_TIRE_PRESSURE_FL = 0xF408
    DID_TIRE_PRESSURE_FR = 0xF409
    DID_TIRE_PRESSURE_RL = 0xF40A
    DID_TIRE_PRESSURE_RR = 0xF40B

    def __init__(self, cfg: "ModernVehicleConfig" | None = None) -> None:
        self.cfg = cfg
        self._running = False
        self._transport: any = None  # type: ignore[annotation-unchecked]
        self._session: UDS_Session = UDS_Session.DEFAULT
        self._security_level: int = 0
        self._ecu_addresses: list[int] = []  # Discovered ECUs

    async def start(self) -> None:
        """Initialize UDS transport (CAN-TP or DoIP)."""
        logger.info("UDS: initializing client...")
        self._running = True

        transport_type = getattr(self.cfg, "uds_transport", "can")
        if transport_type == "can":
            await self._init_can_tp()
        elif transport_type == "doip":
            await self._init_doip()
        elif transport_type == "lin":
            await self._init_lin()

        # Discover ECUs
        await self._discover_ecus()

    async def stop(self) -> None:
        """Close UDS transport."""
        self._running = False
        if self._transport:
            try:
                self._transport.close()
            except Exception:
                pass
        logger.info("UDS: stopped")

    # ── Transport init ────────────────────────────────────────────────────────

    async def _init_can_tp(self) -> None:
        """Initialize ISO-TP over CAN (ISO 15765-2)."""
        try:
            import isotp
            # isotp requires a CAN socket
            self._transport = isotp.NotifierBasedCanStack(
                bus=None,  # Will be injected from CANMultibusController
                address=isotp.Address(
                    isotp.AddressingMode.Normal_11bits,
                    txid=0x7E0,  # Tester address
                    rxid=0x7E8,  # ECU response
                ),
            )
            logger.info("UDS: CAN-TP initialized (Tester 0x7E0 → ECU 0x7E8)")
        except ImportError:
            logger.warning("UDS: isotp not installed, CAN-TP unavailable")

    async def _init_doip(self) -> None:
        """Initialize Diagnostics over IP (ISO 13400)."""
        try:
            import doip
            # Connect to vehicle gateway
            gateway_ip = getattr(self.cfg, "doip_gateway", "192.168.1.1")
            self._transport = doip.DoIPClient(gateway_ip, 13400)
            await self._transport.connect()
            logger.info("UDS: DoIP connected to %s", gateway_ip)
        except ImportError:
            logger.warning("UDS: doip library not installed")
        except Exception as exc:
            logger.error("UDS: DoIP init failed: %s", exc)

    async def _init_lin(self) -> None:
        """Initialize UDS over LIN."""
        logger.info("UDS: LIN transport not yet implemented")

    # ── ECU discovery ─────────────────────────────────────────────────────────

    async def _discover_ecus(self) -> None:
        """Scan for responding ECUs on the bus."""
        logger.info("UDS: discovering ECUs...")
        # Common ECU addresses
        test_addresses = [0x7E0, 0x7E1, 0x7E2, 0x7E3, 0x7E4, 0x7E5, 0x7E6, 0x7E7]
        for addr in test_addresses:
            try:
                resp = await self._send_receive(addr, bytes([0x10, 0x01]))  # DefaultSession
                if resp:
                    self._ecu_addresses.append(addr)
                    logger.info("UDS: ECU found at 0x%03X", addr)
            except Exception:
                pass
        logger.info("UDS: discovered %d ECU(s)", len(self._ecu_addresses))

    # ── Core UDS services ────────────────────────────────────────────────────

    async def read_data_by_identifier(self, ecu_addr: int, did: int) -> bytes | None:
        """Read data by identifier (0x22)."""
        request = bytes([UDS_Service.READ_DATA_BY_IDENTIFIER, (did >> 8) & 0xFF, did & 0xFF])
        return await self._send_receive(ecu_addr, request)

    async def write_data_by_identifier(self, ecu_addr: int, did: int, data: bytes) -> bool:
        """Write data by identifier (0x2E). Requires unlocked session."""
        if self._security_level < 1:
            logger.warning("UDS: write requires security unlock")
            return False
        request = bytes([UDS_Service.WRITE_DATA_BY_IDENTIFIER,
                         (did >> 8) & 0xFF, did & 0xFF]) + data
        resp = await self._send_receive(ecu_addr, request)
        return resp is not None and len(resp) > 0 and resp[0] == 0x6E

    async def routine_control(self, ecu_addr: int, routine_id: int,
                              control_type: int = 0x01, data: bytes = b"") -> bytes | None:
        """Execute routine control (0x31).

        control_type: 0x01=start, 0x02=stop, 0x03=requestResult
        """
        request = bytes([UDS_Service.ROUTINE_CONTROL, control_type,
                         (routine_id >> 8) & 0xFF, routine_id & 0xFF]) + data
        return await self._send_receive(ecu_addr, request)

    async def security_access(self, ecu_addr: int, level: int) -> bool:
        """Unlock ECU security (0x27)."""
        # Request seed
        seed_resp = await self._send_receive(ecu_addr,
                                              bytes([UDS_Service.SECURITY_ACCESS, 0x01]))
        if not seed_resp or len(seed_resp) < 3:
            return False
        seed = seed_resp[2:]
        # Compute key (simplified — real implementation needs OEM algorithm)
        key = self._compute_key(seed, level)
        key_resp = await self._send_receive(ecu_addr,
                                             bytes([UDS_Service.SECURITY_ACCESS, 0x02]) + key)
        if key_resp and key_resp[0] == 0x67:
            self._security_level = level
            return True
        return False

    async def change_session(self, ecu_addr: int, session: UDS_Session) -> bool:
        """Change diagnostic session (0x10)."""
        resp = await self._send_receive(ecu_addr,
                                         bytes([UDS_Service.DIAGNOSTIC_SESSION_CONTROL, session]))
        if resp and resp[0] == 0x50:
            self._session = session
            return True
        return False

    # ── Low-level transport ─────────────────────────────────────────────────

    async def _send_receive(self, ecu_addr: int, request: bytes, timeout: float = 2.0) -> bytes | None:
        """Send UDS request and wait for response."""
        if not self._transport:
            return None
        try:
            if hasattr(self._transport, "send"):
                self._transport.send(request)
                # Wait for response
                start = asyncio.get_event_loop().time()
                while asyncio.get_event_loop().time() - start < timeout:
                    resp = self._transport.recv()
                    if resp:
                        return resp
                    await asyncio.sleep(0.01)
            return None
        except Exception as exc:
            logger.debug("UDS: send_receive failed: %s", exc)
            return None

    def _compute_key(self, seed: bytes, level: int) -> bytes:
        """Compute security key from seed.

        WARNING: This is a placeholder. Real OEM algorithms are proprietary.
        For aftermarket/research use, common algorithms are known for some vehicles.
        """
        # Simple XOR key for demonstration — NOT SECURE
        key = bytearray(seed)
        for i in range(len(key)):
            key[i] ^= (0xAB + level) & 0xFF
        return bytes(key)

    # ── Convenience: common reads ───────────────────────────────────────────

    async def read_vin(self, ecu_addr: int = 0x7E0) -> str:
        """Read VIN from ECU."""
        data = await self.read_data_by_identifier(ecu_addr, self.DID_VIN)
        if data:
            # UDS response: 0x62 0xF1 0x90 [VIN bytes...]
            # Skip the 3-byte service + DID header
            vin_bytes = data[3:] if len(data) > 3 else data
            return vin_bytes.decode("ascii", errors="ignore").strip()
        return ""

    async def read_odometer(self, ecu_addr: int = 0x7E0) -> int | None:
        """Read odometer from ECU."""
        data = await self.read_data_by_identifier(ecu_addr, self.DID_ODOMETER)
        if data and len(data) >= 4:
            return struct.unpack(">I", data[:4])[0]
        return None

    async def read_battery_voltage(self, ecu_addr: int = 0x7E0) -> float | None:
        """Read battery voltage from ECU."""
        data = await self.read_data_by_identifier(ecu_addr, self.DID_BATTERY_VOLTAGE)
        if data and len(data) >= 2:
            return struct.unpack(">H", data[:2])[0] / 1000.0  # mV → V
        return None

    async def read_tire_pressures(self, ecu_addr: int = 0x7E0) -> dict[str, float]:
        """Read all tire pressures from ECU."""
        result: dict[str, float] = {}
        dids = {
            "FL": self.DID_TIRE_PRESSURE_FL,
            "FR": self.DID_TIRE_PRESSURE_FR,
            "RL": self.DID_TIRE_PRESSURE_RL,
            "RR": self.DID_TIRE_PRESSURE_RR,
        }
        for tire, did in dids.items():
            data = await self.read_data_by_identifier(ecu_addr, did)
            if data and len(data) >= 2:
                result[tire] = struct.unpack(">H", data[:2])[0] / 100.0  # kPa/100
        return result
