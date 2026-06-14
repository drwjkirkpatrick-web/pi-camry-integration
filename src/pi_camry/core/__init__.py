"""
pi_camry/core/__init__.py
─────────────────────────
Core utilities: logging, async event bus, and shared types.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable

from pi_camry.core.config import settings


# ──────────────────────────────────────────────────────────────────────────────
# Logging setup
# ──────────────────────────────────────────────────────────────────────────────

def setup_logging() -> None:
    """Configure structured logging to file + console."""
    log_dir = settings.storage.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            log_dir / f"camry_{datetime.now().strftime('%Y%m%d')}.log"
        ),
    ]

    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format=fmt,
        handlers=handlers,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Event bus (async pub/sub for inter-module communication)
# ──────────────────────────────────────────────────────────────────────────────

class EventType(Enum):
    """All event types that modules can publish/subscribe."""
    # OBD events
    OBD_CONNECTED = auto()
    OBD_DISCONNECTED = auto()
    OBD_PID_UPDATE = auto()
    OBD_CEL_ON = auto()
    OBD_CEL_OFF = auto()
    OBD_TROUBLE_CODE = auto()

    # Camera events
    CAMERA_RECORDING_START = auto()
    CAMERA_RECORDING_STOP = auto()
    CAMERA_MOTION_DETECTED = auto()
    CAMERA_BUFFER_LOCKED = auto()

    # GPS events
    GPS_FIX = auto()
    GPS_LOST = auto()
    GEOFENCE_ENTER = auto()
    GEOFENCE_EXIT = auto()

    # IMU events
    IMU_COLLISION = auto()
    IMU_HARD_BRAKE = auto()
    IMU_HARD_ACCEL = auto()
    IMU_TOW_DETECTED = auto()

    # GPIO events
    IGNITION_ON = auto()
    IGNITION_OFF = auto()
    DOOR_OPEN = auto()
    DOOR_CLOSED = auto()
    TRUNK_OPEN = auto()
    HOOD_OPEN = auto()

    # System events
    SYSTEM_SHUTDOWN = auto()
    SYSTEM_LOW_STORAGE = auto()
    SYSTEM_LOW_BATTERY = auto()

    # Display events
    DISPLAY_ON = auto()
    DISPLAY_STANDBY = auto()
    DISPLAY_NEXT_PAGE = auto()
    DISPLAY_PREV_PAGE = auto()
    DISPLAY_CHANGE_MODE = auto()

    # Audio events
    AUDIO_VOLUME_UP = auto()
    AUDIO_VOLUME_DOWN = auto()
    AUDIO_MUTE = auto()
    AUDIO_CANCEL = auto()
    WAKE_WORD_DETECTED = auto()

    # Radio events
    RADIO_PRESETS = auto()
    RADIO_TUNE = auto()
    RADIO_RDS = auto()

    # Modern vehicle events
    TPMS_READING = auto()
    RADAR_TARGET = auto()
    DRIVER_ALERT = auto()
    INTERIOR_AIR_QUALITY = auto()

    # Sensor / environment events
    ENVIRONMENT = auto()
    GPIO_COMMAND = auto()
    AUDIO_ALERT = auto()
    COLLISION_WARNING = auto()


@dataclass
class Event:
    """An event on the bus with optional payload."""
    event_type: EventType
    timestamp: datetime = field(default_factory=datetime.utcnow)
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"


class EventBus:
    """Async publish/subscribe event bus for loose module coupling.

    Usage:
        bus = EventBus()
        bus.subscribe(EventType.IMU_COLLISION, on_collision)
        bus.publish(EventType.IMU_COLLISION, {"g_force": 4.2})
    """

    def __init__(self) -> None:
        self._subs: dict[EventType, list[Callable[[Event], Any]]] = {
            et: [] for et in EventType
        }
        self._lock = asyncio.Lock()

    def subscribe(
        self,
        event_type: EventType,
        handler: Callable[[Event], Any],
    ) -> None:
        """Register a handler for an event type."""
        self._subs[event_type].append(handler)

    def unsubscribe(
        self,
        event_type: EventType,
        handler: Callable[[Event], Any],
    ) -> None:
        """Remove a handler."""
        if handler in self._subs[event_type]:
            self._subs[event_type].remove(handler)

    async def publish(
        self,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
        source: str = "unknown",
    ) -> None:
        """Publish an event to all subscribers (async-safe)."""
        event = Event(
            event_type=event_type,
            payload=payload or {},
            source=source,
        )
        handlers = self._subs[event_type][:]
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception:
                logging.getLogger("eventbus").exception(
                    "Handler failed for %s", event_type.name
                )


# Global event bus instance
bus = EventBus()
