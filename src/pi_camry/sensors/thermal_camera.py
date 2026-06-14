"""
pi_camry/sensors/thermal_camera.py
──────────────────────────────────
Thermal camera fusion for night vision and hotspot detection.

Supports three hardware options:
- MLX90640 (I2C, 32×24, 110° FOV, ~$60) — cheapest, good for detection
- FLIR Lepton 3.5 (SPI, 160×120, 57° FOV, ~$200) — best quality
- Seek Thermal Compact (USB, 206×156, ~$250) — plug-and-play

Fusion with visible camera:
- Overlay thermal colormap on Pi Camera 3 feed
- Detect hotspots above threshold (pedestrians, animals, exhaust)
- Publish hotspot coordinates for alert system

Integration:
- Night driving: thermal + visible overlay on JoyBring display
- Security: detect warm bodies near parked car
- Maintenance: detect brake/ bearing overheating

Hardware wiring:
- MLX90640: I2C bus 1 (GPIO 2/3), address 0x33
- Lepton 3.5: SPI0 (GPIO 9-11) + I2C for telemetry
- Seek: USB-C to Pi 5 USB port
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pi_camry.core.config import ThermalCameraConfig

logger = logging.getLogger("camry.sensors.thermal")


@dataclass(frozen=True)
class ThermalHotspot:
    """Detected thermal hotspot."""
    x: int              # Pixel coordinate in thermal frame
    y: int
    temp_c: float
    size_pixels: int
    confidence: float   # 0-1


@dataclass(frozen=True)
class ThermalFrame:
    """Processed thermal frame with hotspots."""
    width: int
    height: int
    min_temp: float
    max_temp: float
    avg_temp: float
    hotspots: list[ThermalHotspot]
    timestamp: float


class ThermalCamera:
    """Thermal camera controller with visible fusion."""

    # Hotspot detection
    HOTSPOT_THRESHOLD_C = 35.0   # Human body temp
    HOTSPOT_MIN_SIZE = 4           # Minimum 2×2 pixels
    HOTSPOT_MAX_SIZE = 200         # Maximum blob size

    def __init__(self, cfg: "ThermalCameraConfig" | None = None) -> None:
        self.cfg = cfg
        self._running = False
        self._model: str = cfg.model if cfg else "mlx90640"
        self._i2c_bus: int = cfg.i2c_bus if cfg else 1
        self._sensor: any = None  # type: ignore[annotation-unchecked]
        self._latest_frame: ThermalFrame | None = None

    async def start(self) -> None:
        """Initialize thermal camera hardware."""
        logger.info("Thermal: initializing %s...", self._model)

        if self._model == "mlx90640":
            await self._init_mlx90640()
        elif self._model == "lepton35":
            await self._init_lepton35()
        elif self._model == "seek":
            await self._init_seek()
        else:
            logger.warning("Thermal: unknown model %s", self._model)
            return

        self._running = True
        asyncio.create_task(self._capture_loop())
        logger.info("Thermal: capture started")

    async def stop(self) -> None:
        """Shutdown thermal camera."""
        self._running = False
        if self._sensor and hasattr(self._sensor, "close"):
            self._sensor.close()
        logger.info("Thermal: stopped")

    # ── Hardware initialization ─────────────────────────────────────────────

    async def _init_mlx90640(self) -> None:
        """Initialize MLX90640 via I2C."""
        try:
            import mlx90640
            self._sensor = mlx90640.MLX90640()
            self._sensor.refresh_rate = mlx90640.RefreshRate.REFRESH_8_HZ
            logger.info("Thermal: MLX90640 initialized (32×24, 8 Hz)")
        except ImportError:
            logger.warning("Thermal: mlx90640 module not installed")
        except Exception as exc:
            logger.error("Thermal: MLX90640 init failed: %s", exc)

    async def _init_lepton35(self) -> None:
        """Initialize FLIR Lepton 3.5 via SPI."""
        try:
            from flirpy.camera.lepton import Lepton
            self._sensor = Lepton()
            logger.info("Thermal: Lepton 3.5 initialized (160×120)")
        except ImportError:
            logger.warning("Thermal: flirpy not installed")
        except Exception as exc:
            logger.error("Thermal: Lepton init failed: %s", exc)

    async def _init_seek(self) -> None:
        """Initialize Seek Thermal via USB."""
        try:
            from seekcamera import SeekCamera
            self._sensor = SeekCamera()
            logger.info("Thermal: Seek initialized (206×156)")
        except ImportError:
            logger.warning("Thermal: seekcamera not installed")
        except Exception as exc:
            logger.error("Thermal: Seek init failed: %s", exc)

    # ── Capture loop ──────────────────────────────────────────────────────────

    async def _capture_loop(self) -> None:
        """Continuous thermal frame capture and processing."""
        while self._running:
            try:
                if self._model == "mlx90640":
                    frame = await self._capture_mlx90640()
                elif self._model == "lepton35":
                    frame = await self._capture_lepton35()
                elif self._model == "seek":
                    frame = await self._capture_seek()
                else:
                    frame = None

                if frame:
                    self._latest_frame = frame
                    await self._publish(frame)

                # MLX90640 @ 8 Hz = 125ms; Lepton @ 8.7 Hz = 115ms
                await asyncio.sleep(0.125)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Thermal: capture error")
                await asyncio.sleep(0.5)

    async def _capture_mlx90640(self) -> ThermalFrame | None:
        """Capture frame from MLX90640."""
        if not self._sensor:
            return None
        try:
            import numpy as np
            frame = await asyncio.to_thread(self._sensor.get_frame)
            temps = np.array(frame)
            hotspots = self._detect_hotspots(temps, 32, 24)
            return ThermalFrame(
                width=32, height=24,
                min_temp=float(np.min(temps)),
                max_temp=float(np.max(temps)),
                avg_temp=float(np.mean(temps)),
                hotspots=hotspots,
                timestamp=time.monotonic(),
            )
        except Exception:
            return None

    async def _capture_lepton35(self) -> ThermalFrame | None:
        """Capture frame from FLIR Lepton 3.5."""
        if not self._sensor:
            return None
        try:
            import numpy as np
            image = await asyncio.to_thread(self._sensor.grab)
            temps = image  # Already in temperature units
            hotspots = self._detect_hotspots(temps, 160, 120)
            return ThermalFrame(
                width=160, height=120,
                min_temp=float(np.min(temps)),
                max_temp=float(np.max(temps)),
                avg_temp=float(np.mean(temps)),
                hotspots=hotspots,
                timestamp=time.monotonic(),
            )
        except Exception:
            return None

    async def _capture_seek(self) -> ThermalFrame | None:
        """Capture frame from Seek Thermal."""
        if not self._sensor:
            return None
        try:
            import numpy as np
            frame = await asyncio.to_thread(self._sensor.get_frame)
            temps = np.array(frame)
            hotspots = self._detect_hotspots(temps, 206, 156)
            return ThermalFrame(
                width=206, height=156,
                min_temp=float(np.min(temps)),
                max_temp=float(np.max(temps)),
                avg_temp=float(np.mean(temps)),
                hotspots=hotspots,
                timestamp=time.monotonic(),
            )
        except Exception:
            return None

    # ── Hotspot detection ───────────────────────────────────────────────────

    def _detect_hotspots(self, temps: any, width: int, height: int) -> list[ThermalHotspot]:  # type: ignore[annotation-unchecked]
        """Detect hotspots above threshold using simple blob detection."""
        import numpy as np
        threshold = self.cfg.hotspot_threshold_c if self.cfg else self.HOTSPOT_THRESHOLD_C
        hotspots: list[ThermalHotspot] = []

        # Create binary mask of hot pixels
        mask = temps > threshold
        if not mask.any():
            return hotspots

        # Simple connected component analysis (4-connectivity)
        visited = np.zeros_like(mask, dtype=bool)
        for y in range(height):
            for x in range(width):
                if mask[y, x] and not visited[y, x]:
                    # Flood fill
                    blob = []
                    stack = [(x, y)]
                    while stack:
                        cx, cy = stack.pop()
                        if cx < 0 or cx >= width or cy < 0 or cy >= height:
                            continue
                        if visited[cy, cx] or not mask[cy, cx]:
                            continue
                        visited[cy, cx] = True
                        blob.append((cx, cy))
                        stack.extend([(cx+1, cy), (cx-1, cy), (cx, cy+1), (cx, cy-1)])

                    if self.HOTSPOT_MIN_SIZE <= len(blob) <= self.HOTSPOT_MAX_SIZE:
                        avg_x = sum(p[0] for p in blob) / len(blob)
                        avg_y = sum(p[1] for p in blob) / len(blob)
                        max_temp = max(temps[py, px] for px, py in blob)
                        hotspots.append(ThermalHotspot(
                            x=int(avg_x), y=int(avg_y),
                            temp_c=float(max_temp),
                            size_pixels=len(blob),
                            confidence=min(1.0, len(blob) / 20.0),
                        ))

        return hotspots

    # ── Fusion ──────────────────────────────────────────────────────────────

    async def get_overlay_frame(self, visible_frame: any) -> any:  # type: ignore[annotation-unchecked]
        """Blend thermal colormap with visible camera frame.

        Returns composite frame for display.
        """
        if not self._latest_frame:
            return visible_frame

        import numpy as np
        import cv2

        # Resize thermal to match visible
        thermal_w, thermal_h = self._latest_frame.width, self._latest_frame.height
        vis_h, vis_w = visible_frame.shape[:2]

        # Create thermal colormap
        thermal_img = np.zeros((thermal_h, thermal_w, 3), dtype=np.uint8)
        # Would fill with actual thermal data; placeholder

        thermal_resized = cv2.resize(thermal_img, (vis_w, vis_h))
        alpha = self.cfg.overlay_alpha if self.cfg else 0.5
        blended = cv2.addWeighted(visible_frame, 1.0 - alpha, thermal_resized, alpha, 0)

        # Draw hotspot markers
        for h in self._latest_frame.hotspots:
            # Scale coordinates
            sx = int(h.x * vis_w / thermal_w)
            sy = int(h.y * vis_h / thermal_h)
            cv2.circle(blended, (sx, sy), 20, (0, 0, 255), 2)
            cv2.putText(blended, f"{h.temp_c:.1f}°C", (sx+25, sy),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

        return blended

    # ── Publishing ────────────────────────────────────────────────────────────

    async def _publish(self, frame: ThermalFrame) -> None:
        """Publish thermal frame and hotspot alerts."""
        from pi_camry.core import EventType, bus

        # Publish frame summary
        await bus.publish(
            EventType.ENVIRONMENT,
            {
                "thermal": {
                    "min_temp": frame.min_temp,
                    "max_temp": frame.max_temp,
                    "hotspot_count": len(frame.hotspots),
                }
            },
            source="thermal",
        )

        # Alert on significant hotspots
        for h in frame.hotspots:
            if h.temp_c > 50.0:  # Likely vehicle or animal
                await bus.publish(
                    EventType.COLLISION_WARNING,
                    {
                        "alert": "THERMAL_HOTSPOT",
                        "temp_c": h.temp_c,
                        "position": (h.x, h.y),
                        "confidence": h.confidence,
                    },
                    source="thermal",
                )

    # ── Public API ──────────────────────────────────────────────────────────

    def get_latest_frame(self) -> ThermalFrame | None:
        return self._latest_frame

    def get_hotspots(self) -> list[ThermalHotspot]:
        return self._latest_frame.hotspots if self._latest_frame else []

    def has_hotspot_above(self, temp_c: float) -> bool:
        """Check if any hotspot exceeds temperature."""
        if not self._latest_frame:
            return False
        return any(h.temp_c > temp_c for h in self._latest_frame.hotspots)
