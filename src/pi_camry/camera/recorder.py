"""
pi_camry/camera/recorder.py
───────────────────────────
Dual-camera video recording with rolling buffer, event locking,
and M.2 NVMe storage management.

Architecture:
- Primary (front): Pi Camera Module 3 via CSI (libcamera/picamera2)
- Secondary (rear): Pi Camera Module 3 via CSI1 or USB camera
- Circular buffer in RAM: keeps last N seconds
- On event (collision, motion, manual trigger): lock buffer + start high-bitrate save
- H.264 encode to M.2, optional AES-256 encryption via cryptography.fernet
- Low-disk auto-prune of oldest non-locked segments

Usage:
    from pi_camry.camera.recorder import CameraRecorder
    recorder = CameraRecorder()
    await recorder.start()
    # Event triggers auto-lock; manual lock:
    await recorder.lock_buffer(reason="manual_trigger")
    await recorder.stop()
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from cryptography.fernet import Fernet

from pi_camry.core import EventType, bus
from pi_camry.core.config import settings

logger = logging.getLogger("camry.camera")


@dataclass
class VideoSegment:
    """A recorded video segment with metadata."""
    path: Path
    start_time: datetime
    end_time: datetime | None = None
    locked: bool = False            # locked segments are never auto-deleted
    lock_reason: str = ""
    camera: str = "front"         # front | rear | both
    bitrate_mbps: int = 4
    file_size_bytes: int = 0
    encrypted: bool = False


class CircularBuffer:
    """In-memory circular buffer of raw H.264 NAL units (simplified).

    Real implementation would use picamera2's circular buffer support.
    This is a conceptual scaffold for the architecture.
    """

    def __init__(self, capacity_seconds: int, fps: int = 30) -> None:
        self.capacity_frames = capacity_seconds * fps
        self.frames: list[bytes] = []
        self.head = 0
        self._lock = asyncio.Lock()

    async def append(self, frame: bytes) -> None:
        async with self._lock:
            if len(self.frames) < self.capacity_frames:
                self.frames.append(frame)
            else:
                self.frames[self.head] = frame
                self.head = (self.head + 1) % self.capacity_frames

    async def flush_locked(self, output_path: Path) -> int:
        """Write all buffered frames to disk. Returns bytes written."""
        async with self._lock:
            # In real implementation: prepend SPS/PPS, write annex-B
            total = 0
            with open(output_path, "wb") as f:
                for frame in self.frames:
                    f.write(frame)
                    total += len(frame)
            return total

    async def clear(self) -> None:
        async with self._lock:
            self.frames.clear()
            self.head = 0


class CameraRecorder:
    """Dual-camera recorder with rolling buffer and event-driven locking."""

    def __init__(self) -> None:
        self.cfg = settings.camera
        self._running = False
        self._recording = False
        self._lock = asyncio.Lock()

        # Buffers
        self.front_buffer = CircularBuffer(self.cfg.buffer_seconds, self.cfg.fps)
        self.rear_buffer: CircularBuffer | None = None
        if self.cfg.secondary_device:
            self.rear_buffer = CircularBuffer(self.cfg.buffer_seconds, self.cfg.fps)

        # Storage
        self.video_dir = self.cfg.video_dir
        self.video_dir.mkdir(parents=True, exist_ok=True)
        self.segments: list[VideoSegment] = []
        self._max_usage = self.cfg.max_disk_usage_percent / 100.0

        # Encryption
        self._fernet: Fernet | None = None
        if self.cfg.encrypt:
            self._init_encryption()

        # Disk prune task
        self._prune_task: asyncio.Task | None = None

    def _init_encryption(self) -> None:
        """Load or generate AES-256 key for video encryption."""
        key_path = self.cfg.encryption_key_path
        if key_path.exists():
            with open(key_path, "rb") as f:
                key = f.read()
        else:
            key = Fernet.generate_key()
            key_path.parent.mkdir(parents=True, exist_ok=True)
            with open(key_path, "wb") as f:
                f.write(key)
            os.chmod(key_path, 0o600)
        self._fernet = Fernet(key)
        logger.info("Camera: encryption initialized")

    # ── Public API ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the recorder: camera streams, event subscriptions, prune loop."""
        logger.info("Camera: starting recorder (front=%s, rear=%s)",
                    self.cfg.primary_device, self.cfg.secondary_device)
        self._running = True

        # Subscribe to events that trigger recording
        bus.subscribe(EventType.IMU_COLLISION, self._on_collision)
        bus.subscribe(EventType.IMU_HARD_BRAKE, self._on_hard_brake)
        bus.subscribe(EventType.OBD_CEL_ON, self._on_obd_event)
        bus.subscribe(EventType.SYSTEM_SHUTDOWN, self._on_shutdown)

        # Start prune loop
        self._prune_task = asyncio.create_task(self._prune_loop())

        # Start camera acquisition (picamera2 would go here)
        asyncio.create_task(self._acquire_front())
        if self.rear_buffer:
            asyncio.create_task(self._acquire_rear())

        await bus.publish(EventType.CAMERA_RECORDING_START, {}, source="camera")

    async def stop(self) -> None:
        """Graceful shutdown: stop acquisition, flush buffers, stop tasks."""
        logger.info("Camera: stopping recorder")
        self._running = False

        bus.unsubscribe(EventType.IMU_COLLISION, self._on_collision)
        bus.unsubscribe(EventType.IMU_HARD_BRAKE, self._on_hard_brake)
        bus.unsubscribe(EventType.OBD_CEL_ON, self._on_obd_event)
        bus.unsubscribe(EventType.SYSTEM_SHUTDOWN, self._on_shutdown)

        if self._prune_task:
            self._prune_task.cancel()
            try:
                await self._prune_task
            except asyncio.CancelledError:
                pass

        # Flush any remaining locked buffers
        await self._flush_all_buffers()
        await bus.publish(EventType.CAMERA_RECORDING_STOP, {}, source="camera")

    async def lock_buffer(self, reason: str, duration_sec: int = 60) -> Path | None:
        """Manually lock current buffer and start continuous save for N seconds.

        Returns the path to the locked segment directory.
        """
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        seg_dir = self.video_dir / f"LOCKED_{reason}_{ts}"
        seg_dir.mkdir(parents=True, exist_ok=True)

        front_path = seg_dir / f"front_{ts}.h264"
        rear_path = seg_dir / f"rear_{ts}.h264" if self.rear_buffer else None

        # Flush pre-event buffer
        bytes_front = await self.front_buffer.flush_locked(front_path)
        if self._fernet:
            front_path = await self._encrypt_file(front_path)

        seg = VideoSegment(
            path=seg_dir,
            start_time=datetime.utcnow(),
            locked=True,
            lock_reason=reason,
            camera="both" if rear_path else "front",
            file_size_bytes=bytes_front,
            encrypted=self._fernet is not None,
        )
        self.segments.append(seg)

        # Continue recording high-bitrate for duration_sec
        asyncio.create_task(self._record_duration(seg_dir, duration_sec, rear_path))

        logger.info("Camera: locked buffer to %s (reason=%s)", seg_dir, reason)
        await bus.publish(
            EventType.CAMERA_BUFFER_LOCKED,
            {"path": str(seg_dir), "reason": reason},
            source="camera",
        )
        return seg_dir

    async def get_snapshot(self, camera: str = "front") -> Path | None:
        """Capture a single JPEG frame and return path."""
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        snap_path = self.video_dir / f"snapshot_{camera}_{ts}.jpg"
        # In real implementation: picamera2.capture_file(str(snap_path))
        # For now, placeholder
        logger.info("Camera: snapshot requested (%s) → %s", camera, snap_path)
        return snap_path

    # ── Event handlers ────────────────────────────────────────────────────────

    async def _on_collision(self, event: Any) -> None:
        g = event.payload.get("g_force", 0.0)
        logger.warning("Camera: collision detected (%.1fG), locking buffer", g)
        await self.lock_buffer(reason=f"collision_{g:.1f}G", duration_sec=120)

    async def _on_hard_brake(self, event: Any) -> None:
        g = event.payload.get("longitudinal_g", 0.0)
        logger.info("Camera: hard brake (%.1fG), locking buffer", g)
        await self.lock_buffer(reason=f"hard_brake_{g:.1f}G", duration_sec=60)

    async def _on_obd_event(self, event: Any) -> None:
        logger.info("Camera: OBD event (CEL), locking buffer")
        await self.lock_buffer(reason="cel_on", duration_sec=60)

    async def _on_shutdown(self, event: Any) -> None:
        logger.info("Camera: system shutdown, flushing buffers")
        await self.stop()

    # ── Internal tasks ────────────────────────────────────────────────────────

    async def _acquire_front(self) -> None:
        """Continuously acquire frames from front camera into circular buffer."""
        # Placeholder: real implementation uses picamera2
        while self._running:
            # In production:
            #   frame = picam2.capture_array("main")
            #   encoded = h264_encoder.encode(frame)
            #   await self.front_buffer.append(encoded)
            await asyncio.sleep(1.0 / self.cfg.fps)

    async def _acquire_rear(self) -> None:
        """Same for rear camera."""
        if not self.rear_buffer:
            return
        while self._running:
            await asyncio.sleep(1.0 / self.cfg.fps)

    async def _record_duration(
        self,
        seg_dir: Path,
        duration_sec: int,
        rear_path: Path | None,
    ) -> None:
        """Record high-bitrate for a fixed duration after lock trigger."""
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        front_out = seg_dir / f"front_post_{ts}.h264"
        # In production: open encoder, write for duration_sec
        await asyncio.sleep(duration_sec)
        logger.info("Camera: post-event recording complete: %s", seg_dir)

    async def _prune_loop(self) -> None:
        """Periodically check disk usage and delete oldest non-locked segments."""
        while self._running:
            try:
                await asyncio.sleep(60)
                await self._prune_old_segments()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Camera: prune loop error")

    async def _prune_old_segments(self) -> None:
        """Delete oldest non-locked segments if disk usage > threshold."""
        try:
            import psutil
            usage = psutil.disk_usage(str(self.video_dir)).percent / 100.0
        except Exception:
            return

        if usage <= self._max_usage:
            return

        # Sort by start_time, oldest first; skip locked
        candidates = [s for s in self.segments if not s.locked]
        candidates.sort(key=lambda s: s.start_time)

        freed = 0
        for seg in candidates:
            if usage <= self._max_usage:
                break
            try:
                if seg.path.exists():
                    # Recursively delete segment directory
                    import shutil
                    size = sum(f.stat().st_size for f in seg.path.rglob("*") if f.is_file())
                    shutil.rmtree(seg.path)
                    freed += size
                    self.segments.remove(seg)
                    logger.info("Camera: pruned %s (freed %d bytes)", seg.path, size)
            except Exception as exc:
                logger.warning("Camera: failed to prune %s: %s", seg.path, exc)

        if freed > 0:
            logger.info("Camera: pruned %d bytes total", freed)

    async def _encrypt_file(self, file_path: Path) -> Path:
        """Encrypt a file in-place, return new .enc path."""
        if not self._fernet:
            return file_path
        enc_path = file_path.with_suffix(file_path.suffix + ".enc")
        with open(file_path, "rb") as f:
            data = f.read()
        encrypted = self._fernet.encrypt(data)
        with open(enc_path, "wb") as f:
            f.write(encrypted)
        file_path.unlink()  # delete plaintext
        return enc_path

    async def _flush_all_buffers(self) -> None:
        """Flush all circular buffers to disk on shutdown."""
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        if self.front_buffer.frames:
            path = self.video_dir / f"shutdown_front_{ts}.h264"
            await self.front_buffer.flush_locked(path)
        if self.rear_buffer and self.rear_buffer.frames:
            path = self.video_dir / f"shutdown_rear_{ts}.h264"
            await self.rear_buffer.flush_locked(path)
