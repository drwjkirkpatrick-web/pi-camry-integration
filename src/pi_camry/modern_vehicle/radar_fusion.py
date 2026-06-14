"""
pi_camry/modern_vehicle/radar_fusion.py
───────────────────────────────────────
Radar fusion processor for modern ADAS radar modules.

Modern vehicles have 2-6 radar sensors:
- Front long-range radar (77 GHz, 200m range) — adaptive cruise, AEB
- Front short-range radar (24 GHz, 30m) — parking, cross-traffic
- Rear corner radars (2×, 24/77 GHz) — blind spot, rear cross-traffic
- Side radars (2×, 77 GHz) — lane change assist

Reads radar object lists from CAN bus and fuses with camera data.

Hardware:
- Modern vehicle: radar modules on ADAS CAN bus (decoded via DBC)
- Aftermarket: add-on radar modules with CAN/serial output
  (e.g., Bosch FR5, Continental ARS430, or cheap aftermarket units)

Features:
- Object tracking (ID, distance, relative speed, angle)
- Collision time-to-collision (TTC) calculation
- Adaptive cruise control following distance
- Blind spot vehicle detection
- Rear cross-traffic alert
- Pedestrian/cyclist classification (if radar supports)
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pi_camry.core.config import ModernVehicleConfig

logger = logging.getLogger("camry.modern.radar")


@dataclass(frozen=True)
class RadarTarget:
    """Tracked radar object."""
    track_id: int
    distance_m: float
    relative_speed_ms: float  # Negative = approaching
    azimuth_deg: float        # -90 to +90, 0 = straight ahead
    width_m: float
    height_m: float
    classification: str = "unknown"  # vehicle, pedestrian, cyclist, stationary
    confidence: float = 0.0
    timestamp: float = 0.0


@dataclass
class RadarFusionState:
    """Current fused radar scene."""
    targets: dict[int, RadarTarget] = field(default_factory=dict)
    front_clear: bool = True
    rear_clear: bool = True
    left_blind_spot_clear: bool = True
    right_blind_spot_clear: bool = True
    ttc_front_sec: float = float("inf")
    following_distance_m: float = float("inf")


class RadarFusionProcessor:
    """Process and fuse radar data from multiple sensors."""

    # Safety thresholds
    TTC_AEB_THRESHOLD = 2.5       # seconds — emergency braking
    TTC_FCW_THRESHOLD = 3.5       # seconds — forward collision warning
    MIN_FOLLOWING_DISTANCE = 2.0  # seconds at current speed
    BLIND_SPOT_RANGE_M = 20.0
    BLIND_SPOT_WIDTH_M = 5.0

    def __init__(self, cfg: "ModernVehicleConfig" | None = None) -> None:
        self.cfg = cfg
        self._running = False
        self._state = RadarFusionState()

        # Sensor positions
        self._sensors: dict[str, dict[str, any]] = {  # type: ignore[annotation-unchecked]
            "front_long": {"active": False, "max_range": 200.0, "angle_fov": 30.0},
            "front_short": {"active": False, "max_range": 30.0, "angle_fov": 90.0},
            "rear_left": {"active": False, "max_range": 50.0, "angle_fov": 60.0},
            "rear_right": {"active": False, "max_range": 50.0, "angle_fov": 60.0},
        }

    async def start(self) -> None:
        """Initialize radar processing."""
        logger.info("RadarFusion: initializing with %d sensor(s)...", len(self._sensors))
        self._running = True
        # In a real implementation, subscribe to CAN signals from radar modules
        # For now, the module is ready to receive targets via process_targets()

    async def stop(self) -> None:
        """Shutdown radar processing."""
        self._running = False
        logger.info("RadarFusion: stopped")

    # ── Target processing ─────────────────────────────────────────────────────

    async def process_targets(self, sensor_id: str, targets: list[RadarTarget]) -> None:
        """Process a batch of targets from a radar sensor."""
        if sensor_id not in self._sensors:
            return

        for target in targets:
            self._state.targets[target.track_id] = target

        # Update derived safety states
        await self._update_safety_state()

    async def _update_safety_state(self) -> None:
        """Recalculate safety booleans and TTC."""
        now = asyncio.get_event_loop().time()
        # Remove stale targets (>1 sec old)
        stale = [tid for tid, t in self._state.targets.items()
                 if now - t.timestamp > 1.0]
        for tid in stale:
            del self._state.targets[tid]

        # Front targets (azimuth near 0, positive distance)
        front_targets = [
            t for t in self._state.targets.values()
            if abs(t.azimuth_deg) < 15 and t.distance_m > 0
        ]
        if front_targets:
            closest = min(front_targets, key=lambda t: t.distance_m)
            self._state.ttc_front_sec = self._calculate_ttc(closest)
            self._state.following_distance_m = closest.distance_m
            self._state.front_clear = closest.distance_m > 30.0
        else:
            self._state.ttc_front_sec = float("inf")
            self._state.following_distance_m = float("inf")
            self._state.front_clear = True

        # Blind spot checks
        left_targets = [
            t for t in self._state.targets.values()
            if -90 < t.azimuth_deg < -10 and 0 < t.distance_m < self.BLIND_SPOT_RANGE_M
        ]
        right_targets = [
            t for t in self._state.targets.values()
            if 10 < t.azimuth_deg < 90 and 0 < t.distance_m < self.BLIND_SPOT_RANGE_M
        ]
        self._state.left_blind_spot_clear = len(left_targets) == 0
        self._state.right_blind_spot_clear = len(right_targets) == 0

        # Publish alerts if needed
        await self._publish_alerts()

    def _calculate_ttc(self, target: RadarTarget) -> float:
        """Calculate time-to-collision in seconds.

        TTC = distance / relative_speed (when closing)
        Returns inf if not closing.
        """
        if target.relative_speed_ms >= 0:  # Not closing or same speed
            return float("inf")
        ttc = target.distance_m / abs(target.relative_speed_ms)
        return ttc

    async def _publish_alerts(self) -> None:
        """Publish safety alerts to EventBus."""
        from pi_camry.core import EventType, bus

        if self._state.ttc_front_sec <= self.TTC_AEB_THRESHOLD:
            await bus.publish(
                EventType.RADAR_TARGET,
                {
                    "alert": "AEB",
                    "ttc": self._state.ttc_front_sec,
                    "distance_m": self._state.following_distance_m,
                },
                source="radar",
            )
        elif self._state.ttc_front_sec <= self.TTC_FCW_THRESHOLD:
            await bus.publish(
                EventType.RADAR_TARGET,
                {
                    "alert": "FCW",
                    "ttc": self._state.ttc_front_sec,
                    "distance_m": self._state.following_distance_m,
                },
                source="radar",
            )

        if not self._state.left_blind_spot_clear:
            await bus.publish(
                EventType.RADAR_TARGET,
                {"alert": "BLIND_SPOT_LEFT"},
                source="radar",
            )
        if not self._state.right_blind_spot_clear:
            await bus.publish(
                EventType.RADAR_TARGET,
                {"alert": "BLIND_SPOT_RIGHT"},
                source="radar",
            )

    # ── ACC interface ─────────────────────────────────────────────────────────

    async def get_recommended_following_distance(self, own_speed_ms: float) -> float:
        """Calculate recommended following distance for adaptive cruise.

        Uses 2-second rule: distance = speed * 2
        """
        return own_speed_ms * self.MIN_FOLLOWING_DISTANCE

    async def acc_target_speed(self, set_speed_ms: float) -> float:
        """Return target speed for adaptive cruise.

        If front vehicle is slower and within following distance,
        match their speed. Otherwise, maintain set speed.
        """
        front_targets = [
            t for t in self._state.targets.values()
            if abs(t.azimuth_deg) < 10 and t.distance_m > 0
        ]
        if not front_targets:
            return set_speed_ms

        closest = min(front_targets, key=lambda t: t.distance_m)
        recommended_dist = await self.get_recommended_following_distance(set_speed_ms)

        if closest.distance_m < recommended_dist and closest.relative_speed_ms < 0:
            # Front vehicle is slower and too close — match speed
            own_speed_ms = set_speed_ms
            front_speed_ms = own_speed_ms + closest.relative_speed_ms
            return max(0, front_speed_ms)

        return set_speed_ms

    # ── Status ──────────────────────────────────────────────────────────────

    def get_state(self) -> RadarFusionState:
        """Return current fused radar state."""
        return self._state

    def get_target_count(self) -> int:
        """Return number of tracked targets."""
        return len(self._state.targets)
