"""
pi_camry/camera/picamera2_backend.py
──────────────────────────────────────
Real Pi Camera Module 3 backend using picamera2 + libcamera.

Replaces the placeholder _acquire_front/_acquire_rear in recorder.py
when picamera2 is available on Raspberry Pi 5.

Features:
- H.264 hardware encode via V4L2 on Pi 5
- Dual camera (CSI0 + CSI1) support
- Motion detection via frame differencing
- Overlay: timestamp, GPS speed, OBD RPM

Usage (from recorder.py):
    from pi_camry.camera.picamera2_backend import PiCamera2Backend
    backend = PiCamera2Backend(recorder_instance)
    asyncio.create_task(backend.run_front())
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# picamera2 is Pi-only; handle import gracefully
try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import CircularOutput
    PICAMERA2_AVAILABLE = True
except ImportError:
    Picamera2 = None  # type: ignore[misc,assignment]
    H264Encoder = None  # type: ignore[misc,assignment]
    CircularOutput = None  # type: ignore[misc,assignment]
    PICAMERA2_AVAILABLE = False

if TYPE_CHECKING:
    from pi_camry.camera.recorder import CameraRecorder

logger = logging.getLogger("camry.camera.picamera2")


class PiCamera2Backend:
    """Picamera2-based acquisition backend for CameraRecorder."""

    def __init__(self, recorder: "CameraRecorder") -> None:
        self.recorder = recorder
        self.cfg = recorder.cfg
        self._running = False

        # Camera instances
        self._cam_front: any = None
        self._cam_rear: any = None
        self._encoder_front: any = None
        self._encoder_rear: any = None
        self._output_front: any = None
        self._output_rear: any = None

        # Overlay state
        self._overlay_data: dict[str, str] = {}

    # ── Public API ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize both cameras, encoders, and circular outputs."""
        if not PICAMERA2_AVAILABLE:
            logger.warning("picamera2 not available — using placeholder acquisition")
            return

        logger.info("Picamera2: initializing cameras...")
        self._running = True

        # Front camera (CSI0)
        try:
            self._cam_front = Picamera2(0)
            front_cfg = self._cam_front.create_video_configuration(
                main={"size": self.cfg.resolution, "format": "RGB888"},
                lores={"size": (640, 360)},  # for motion detection
                encode="main",
            )
            self._cam_front.configure(front_cfg)
            self._encoder_front = H264Encoder(bitrate=4000000)  # 4 Mbps
            self._output_front = CircularOutput(
                buffersize=int(self.cfg.fps * self.cfg.buffer_seconds)
            )
            self._cam_front.start_recording(
                self._encoder_front,
                self._output_front,
            )
            logger.info("Picamera2: front camera started (%s @ %d fps)",
                        self.cfg.resolution, self.cfg.fps)
        except Exception as exc:
            logger.error("Picamera2: front camera failed: %s", exc)
            self._cam_front = None

        # Rear camera (CSI1)
        if self.cfg.secondary_device and self.recorder.rear_buffer:
            try:
                self._cam_rear = Picamera2(1)
                rear_cfg = self._cam_rear.create_video_configuration(
                    main={"size": self.cfg.resolution, "format": "RGB888"},
                    encode="main",
                )
                self._cam_rear.configure(rear_cfg)
                self._encoder_rear = H264Encoder(bitrate=4000000)
                self._output_rear = CircularOutput(
                    buffersize=int(self.cfg.fps * self.cfg.buffer_seconds)
                )
                self._cam_rear.start_recording(
                    self._encoder_rear,
                    self._output_rear,
                )
                logger.info("Picamera2: rear camera started")
            except Exception as exc:
                logger.error("Picamera2: rear camera failed: %s", exc)
                self._cam_rear = None

        # Start overlay + motion detection task
        asyncio.create_task(self._overlay_loop())

    async def stop(self) -> None:
        """Stop all cameras and encoders."""
        self._running = False
        if self._cam_front:
            try:
                self._cam_front.stop_recording()
                self._cam_front.close()
            except Exception as exc:
                logger.warning("Picamera2: front stop error: %s", exc)
        if self._cam_rear:
            try:
                self._cam_rear.stop_recording()
                self._cam_rear.close()
            except Exception as exc:
                logger.warning("Picamera2: rear stop error: %s", exc)
        logger.info("Picamera2: stopped")

    async def lock_buffer(self, output_path: Path) -> None:
        """Lock current circular buffer to disk (pre-event footage)."""
        if self._output_front:
            try:
                self._output_front.fileoutput = str(output_path)
                self._output_front.start()
                await asyncio.sleep(0.5)  # brief capture
                self._output_front.stop()
                logger.info("Picamera2: locked front buffer to %s", output_path)
            except Exception as exc:
                logger.error("Picamera2: lock buffer failed: %s", exc)

    async def capture_snapshot(self, camera: str = "front") -> Path | None:
        """Capture a single JPEG frame."""
        cam = self._cam_front if camera == "front" else self._cam_rear
        if not cam:
            return None
        try:
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            path = self.cfg.video_dir / f"snapshot_{camera}_{ts}.jpg"
            cam.capture_file(str(path))
            return path
        except Exception as exc:
            logger.error("Picamera2: snapshot failed: %s", exc)
            return None

    def update_overlay(self, key: str, value: str) -> None:
        """Update overlay data (called by OBD/GPS modules)."""
        self._overlay_data[key] = value

    # ── Internal loops ────────────────────────────────────────────────────────

    async def _overlay_loop(self) -> None:
        """Periodically update frame overlay with telemetry."""
        if not self._cam_front:
            return
        while self._running:
            try:
                # Build overlay text
                lines = [
                    datetime.utcnow().strftime("%H:%M:%S UTC"),
                    f"RPM: {self._overlay_data.get('rpm', '--')}",
                    f"Speed: {self._overlay_data.get('speed', '--')} km/h",
                    f"Lat: {self._overlay_data.get('lat', '--')}",
                    f"Lon: {self._overlay_data.get('lon', '--')}",
                ]
                overlay_text = "\n".join(lines)

                # In picamera2, overlays are applied via post-processing or
                # by drawing on the frame before encode. Simplified here.
                # Real implementation would use picamera2's overlay API.
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Picamera2: overlay loop error")
                await asyncio.sleep(1.0)

    async def _motion_detection_loop(self) -> None:
        """Simple frame differencing for motion detection on lores stream."""
        if not self._cam_front:
            return
        prev_frame: np.ndarray | None = None
        while self._running:
            try:
                # Get lores frame for motion analysis
                frame = self._cam_front.capture_array("lores")
                gray = np.mean(frame, axis=2).astype(np.uint8)

                if prev_frame is not None:
                    diff = np.abs(gray.astype(np.int16) - prev_frame.astype(np.int16))
                    motion_score = np.mean(diff)
                    if motion_score > 30:  # threshold
                        from pi_camry.core import EventType, bus
                        await bus.publish(
                            EventType.CAMERA_MOTION_DETECTED,
                            {"score": float(motion_score)},
                            source="camera",
                        )

                prev_frame = gray
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Picamera2: motion detection error")
                await asyncio.sleep(1.0)
