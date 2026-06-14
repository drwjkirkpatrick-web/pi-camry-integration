"""
Tests for pi_camry.camera.recorder — CameraRecorder with mocked hardware.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pi_camry.camera.recorder import CameraRecorder, CircularBuffer, VideoSegment
from pi_camry.core import EventType


@pytest.mark.asyncio
async def test_camera_start_stop(mock_config: Any) -> None:
    """CameraRecorder should start/stop cleanly and manage subscriptions."""
    recorder = CameraRecorder()
    await recorder.start()
    assert recorder._running is True
    await recorder.stop()
    assert recorder._running is False


@pytest.mark.asyncio
async def test_camera_lock_buffer_creates_segment(mock_config: Any) -> None:
    """lock_buffer should create a locked VideoSegment and return its path."""
    recorder = CameraRecorder()
    await recorder.start()
    seg_dir = await recorder.lock_buffer(reason="test_event", duration_sec=1)
    assert seg_dir is not None
    assert seg_dir.exists()
    # Should have at least one locked segment
    locked = [s for s in recorder.segments if s.locked]
    assert len(locked) >= 1
    assert locked[0].lock_reason == "test_event"
    await recorder.stop()


@pytest.mark.asyncio
async def test_camera_get_snapshot_returns_path(mock_config: Any) -> None:
    """get_snapshot should return a Path with expected naming."""
    recorder = CameraRecorder()
    path = await recorder.get_snapshot(camera="front")
    assert isinstance(path, Path)
    assert "snapshot_front_" in path.name


@pytest.mark.asyncio
async def test_camera_event_handlers_dont_raise(mock_config: Any) -> None:
    """Collision / hard-brake / CEL handlers should run without error."""
    recorder = CameraRecorder()
    await recorder.start()
    # Simulate event payloads
    await recorder._on_collision(MagicMock(payload={"g_force": 4.2}))
    await recorder._on_hard_brake(MagicMock(payload={"longitudinal_g": -0.8}))
    await recorder._on_obd_event(MagicMock(payload={}))
    await recorder.stop()


@pytest.mark.asyncio
async def test_camera_shutdown_handler(mock_config: Any) -> None:
    """_on_shutdown should trigger recorder stop."""
    recorder = CameraRecorder()
    await recorder.start()
    await recorder._on_shutdown(MagicMock(payload={}))
    assert recorder._running is False


@pytest.mark.asyncio
async def test_circular_buffer_append_and_flush(mock_config: Any, tmp_path: Path) -> None:
    """CircularBuffer should append frames and flush to disk."""
    buf = CircularBuffer(capacity_seconds=2, fps=10)
    for i in range(25):
        await buf.append(f"frame_{i}".encode())
    out_path = tmp_path / "buffer.h264"
    written = await buf.flush_locked(out_path)
    assert written > 0
    assert out_path.exists()


@pytest.mark.asyncio
async def test_circular_buffer_overwrite(mock_config: Any) -> None:
    """CircularBuffer should overwrite old frames when full."""
    buf = CircularBuffer(capacity_seconds=1, fps=10)  # capacity = 10 frames
    for i in range(20):
        await buf.append(f"frame_{i}".encode())
    assert len(buf.frames) == 10


@pytest.mark.asyncio
async def test_camera_encrypt_file(mock_config: Any, tmp_path: Path) -> None:
    """_encrypt_file should produce an .enc file and delete plaintext."""
    recorder = CameraRecorder()
    # Ensure encryption key exists
    recorder._init_encryption()
    plain = tmp_path / "test.h264"
    plain.write_bytes(b"secret video data")
    enc_path = await recorder._encrypt_file(plain)
    assert enc_path.suffix == ".enc"
    assert enc_path.exists()
    assert not plain.exists()


@pytest.mark.asyncio
async def test_camera_prune_old_segments(mock_config: Any, tmp_path: Path) -> None:
    """_prune_old_segments should remove oldest non-locked segments."""
    recorder = CameraRecorder()
    recorder.video_dir = tmp_path
    # Create fake segments
    seg1 = tmp_path / "seg1"
    seg1.mkdir()
    (seg1 / "front.h264").write_bytes(b"x" * 1000)
    recorder.segments.append(
        VideoSegment(path=seg1, start_time=__import__("datetime").datetime.utcnow())
    )
    seg2 = tmp_path / "seg2"
    seg2.mkdir()
    (seg2 / "front.h264").write_bytes(b"x" * 1000)
    recorder.segments.append(
        VideoSegment(path=seg2, start_time=__import__("datetime").datetime.utcnow())
    )
    # Force prune by setting very low threshold
    recorder._max_usage = 0.0
    await recorder._prune_old_segments()
    # At least one segment should have been removed
    remaining = [s for s in recorder.segments if s.path.exists()]
    assert len(remaining) < 2


@pytest.mark.asyncio
async def test_camera_flush_all_buffers(mock_config: Any, tmp_path: Path) -> None:
    """_flush_all_buffers should write remaining frames on shutdown."""
    recorder = CameraRecorder()
    recorder.video_dir = tmp_path
    recorder.front_buffer.frames = [b"frame1", b"frame2"]
    await recorder._flush_all_buffers()
    files = list(tmp_path.glob("shutdown_front_*.h264"))
    assert len(files) >= 1
