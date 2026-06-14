"""
pi_camry/display/can_bridge.py
──────────────────────────────
Bridge between JoyBring head-unit CAN bus and vehicle OBD-II/CAN.

Some Android head units expose vehicle CAN pins (CAN H / CAN L) that can
be wired to an MCP2515 CAN controller on the Pi. This bridge:
- Reads vehicle CAN frames (engine, ABS, body, HVAC)
- Translates and forwards to head unit for native display
- Reads head-unit commands (volume, input, climate) and acts on them
- Acts as a CAN gateway/filter for security

Hardware:
- MCP2515 + TJA1050 CAN transceiver on Pi SPI (bus 0, CS0)
- Wired to head-unit CAN H/L pins (if present)
- Optional: second MCP2515 for dual-CAN (vehicle + head unit isolation)

The 1996 Camry doesn't have CAN, so this module also bridges
OBD-II K-line/ISO 9141-2 data into CAN-like frames for the head unit.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pi_camry.core.config import DisplayConfig

logger = logging.getLogger("camry.display.can")


@dataclass(frozen=True)
class CANFrame:
    """Standard CAN 2.0A frame."""
    arbitration_id: int
    data: bytes
    is_extended_id: bool = False
    is_remote_frame: bool = False
    dlc: int = 0


class CANBridge:
    """CAN bus bridge between head unit and vehicle networks."""

    # Common arbitration IDs (SAE J1979 / OEM)
    ID_ENGINE_RPM = 0x0C
    ID_VEHICLE_SPEED = 0x0D
    ID_COOLANT_TEMP = 0x05
    ID_THROTTLE = 0x11
    ID_FUEL_LEVEL = 0x2F
    ID_ODOMETER = 0xA6
    ID_VIN = 0xF0
    ID_HVAC_TEMP = 0x320
    ID_HVAC_FAN = 0x321
    ID_DOOR_STATUS = 0x3B0
    ID_LIGHT_STATUS = 0x3B1

    def __init__(self, cfg: "DisplayConfig" | None = None) -> None:
        self.cfg = cfg
        self._running = False
        self._mcp2515: any = None  # type: ignore[annotation-unchecked]
        self._vehicle_bus: any = None  # type: ignore[annotation-unchecked]
        self._headunit_bus: any = None  # type: ignore[annotation-unchecked]

        # Frame queues
        self._vehicle_rx_queue: asyncio.Queue[CANFrame] = asyncio.Queue()
        self._headunit_rx_queue: asyncio.Queue[CANFrame] = asyncio.Queue()

    async def start(self) -> None:
        """Initialize MCP2515 and start bridge loops."""
        logger.info("CAN: initializing bridge...")
        self._running = True

        try:
            import can
            # Vehicle bus (OBD-II side — via MCP2515 on SPI)
            self._vehicle_bus = can.interface.Bus(
                channel="spi0.0",
                bustype="socketcan",
                bitrate=500000,
            )
            # Head unit bus (if isolated — second MCP2515)
            if getattr(self.cfg, "can_dual_bus", False):
                self._headunit_bus = can.interface.Bus(
                    channel="spi0.1",
                    bustype="socketcan",
                    bitrate=125000,  # head units often use 125k
                )
            else:
                self._headunit_bus = self._vehicle_bus  # shared

            # Start RX loops
            asyncio.create_task(self._vehicle_rx_loop())
            asyncio.create_task(self._headunit_rx_loop())
            asyncio.create_task(self._bridge_loop())

            logger.info("CAN: bridge active at 500k/125k baud")
        except ImportError:
            logger.warning("CAN: python-can not installed")
        except Exception as exc:
            logger.error("CAN: init failed: %s", exc)

    async def stop(self) -> None:
        """Shutdown CAN interfaces."""
        self._running = False
        for bus in (self._vehicle_bus, self._headunit_bus):
            if bus and bus != self._vehicle_bus:
                try:
                    bus.shutdown()
                except Exception:
                    pass
        logger.info("CAN: stopped")

    def is_connected(self) -> bool:
        return self._vehicle_bus is not None

    # ── RX loops ────────────────────────────────────────────────────────────

    async def _vehicle_rx_loop(self) -> None:
        """Read frames from vehicle CAN bus."""
        if not self._vehicle_bus:
            return
        while self._running:
            try:
                msg = await asyncio.to_thread(self._vehicle_bus.recv, 0.1)
                if msg:
                    frame = CANFrame(
                        arbitration_id=msg.arbitration_id,
                        data=msg.data,
                        is_extended_id=msg.is_extended_id,
                        dlc=msg.dlc,
                    )
                    await self._vehicle_rx_queue.put(frame)
            except Exception:
                await asyncio.sleep(0.01)

    async def _headunit_rx_loop(self) -> None:
        """Read frames from head-unit CAN bus."""
        if not self._headunit_bus or self._headunit_bus == self._vehicle_bus:
            return
        while self._running:
            try:
                msg = await asyncio.to_thread(self._headunit_bus.recv, 0.1)
                if msg:
                    frame = CANFrame(
                        arbitration_id=msg.arbitration_id,
                        data=msg.data,
                        is_extended_id=msg.is_extended_id,
                        dlc=msg.dlc,
                    )
                    await self._headunit_rx_queue.put(frame)
            except Exception:
                await asyncio.sleep(0.01)

    # ── Bridge / translation ──────────────────────────────────────────────────

    async def _bridge_loop(self) -> None:
        """Main bridge: translate and forward frames between buses."""
        while self._running:
            try:
                # Process vehicle → head unit
                if not self._vehicle_rx_queue.empty():
                    frame = await asyncio.wait_for(self._vehicle_rx_queue.get(), timeout=0.05)
                    translated = self._translate_vehicle_to_headunit(frame)
                    if translated and self._headunit_bus:
                        await self._send_frame(self._headunit_bus, translated)

                # Process head unit → vehicle
                if not self._headunit_rx_queue.empty():
                    frame = await asyncio.wait_for(self._headunit_rx_queue.get(), timeout=0.05)
                    translated = self._translate_headunit_to_vehicle(frame)
                    if translated and self._vehicle_bus:
                        await self._send_frame(self._vehicle_bus, translated)

                await asyncio.sleep(0.001)
            except asyncio.TimeoutError:
                continue
            except Exception:
                logger.exception("CAN: bridge loop error")
                await asyncio.sleep(0.1)

    def _translate_vehicle_to_headunit(self, frame: CANFrame) -> CANFrame | None:
        """Translate vehicle PID values to head-unit display format."""
        # Pass-through for now; head units often understand standard OBD-CAN PIDs
        return frame

    def _translate_headunit_to_vehicle(self, frame: CANFrame) -> CANFrame | None:
        """Translate head-unit commands to vehicle-safe frames.

        Security: whitelist only known command IDs to prevent injection.
        """
        # Whitelist head-unit → vehicle commands
        allowed_ids = {0x320, 0x321, 0x3B0, 0x3B1}  # HVAC, doors, lights
        if frame.arbitration_id not in allowed_ids:
            logger.warning("CAN: blocked head-unit frame 0x%03X", frame.arbitration_id)
            return None
        return frame

    async def _send_frame(self, bus: any, frame: CANFrame) -> None:  # type: ignore[annotation-unchecked]
        """Send a CAN frame."""
        try:
            import can
            msg = can.Message(
                arbitration_id=frame.arbitration_id,
                data=frame.data,
                is_extended_id=frame.is_extended_id,
            )
            await asyncio.to_thread(bus.send, msg)
        except Exception as exc:
            logger.warning("CAN: send failed: %s", exc)

    # ── Public: inject OBD data as CAN frames ─────────────────────────────────

    async def inject_obd_snapshot(self, rpm: int | None, speed: float | None,
                                   coolant: int | None) -> None:
        """Convert OBD-II data to CAN frames for head-unit display.

        Useful for 1996 Camry which has OBD-II but not CAN.
        """
        if rpm is not None:
            data = struct.pack(">H", rpm * 4)  # OBD-CAN convention: RPM * 4
            await self._vehicle_rx_queue.put(CANFrame(self.ID_ENGINE_RPM, data))
        if speed is not None:
            data = struct.pack(">B", int(speed))  # km/h
            await self._vehicle_rx_queue.put(CANFrame(self.ID_VEHICLE_SPEED, data))
        if coolant is not None:
            data = struct.pack(">b", coolant)  # °C offset
            await self._vehicle_rx_queue.put(CANFrame(self.ID_COOLANT_TEMP, data))
