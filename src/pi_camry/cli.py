"""
pi_camry/cli.py
───────────────
Command-line tools for individual subsystem testing and debugging.

Entry points (from pyproject.toml):
    camry-obd     — Live OBD-II PID monitor
    camry-camera  — Camera test / snapshot capture

Usage:
    camry-obd --port /dev/ttyUSB0 --watch rpm,speed,coolant
    camry-camera --snapshot --camera front
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time

from pi_camry.camera.picamera2_backend import PiCamera2Backend
from pi_camry.camera.recorder import CameraRecorder
from pi_camry.core.config import settings
from pi_camry.obd.interface import OBDInterface

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("camry.cli")


# ── camry-obd ─────────────────────────────────────────────────────────────

def obd_cli() -> None:
    """Live OBD-II PID monitor."""
    parser = argparse.ArgumentParser(description="Live OBD-II monitor")
    parser.add_argument("--port", default=settings.obd.port, help="OBD adapter port")
    parser.add_argument("--baud", type=int, default=settings.obd.baudrate)
    parser.add_argument("--watch", default="rpm,speed,coolant", help="Comma-separated PIDs")
    parser.add_argument("--interval", type=float, default=1.0, help="Poll interval (sec)")
    parser.add_argument("--json", action="store_true", help="Output as JSON lines")
    args = parser.parse_args()

    settings.obd.port = args.port
    settings.obd.baudrate = args.baud
    watch_list = [p.strip().lower() for p in args.watch.split(",")]

    obd = OBDInterface()

    async def run() -> None:
        await obd.start()
        if not obd.is_connected():
            logger.error("Failed to connect to OBD adapter")
            sys.exit(1)

        logger.info("Connected. Watching: %s", ", ".join(watch_list))
        try:
            while True:
                snap = obd.get_latest()
                data: dict[str, any] = {}  # type: ignore[annotation-unchecked]
                if "rpm" in watch_list and snap.rpm is not None:
                    data["rpm"] = snap.rpm
                if "speed" in watch_list and snap.speed_kmh is not None:
                    data["speed_kmh"] = round(snap.speed_kmh, 1)
                if "coolant" in watch_list and snap.coolant_temp_c is not None:
                    data["coolant_c"] = round(snap.coolant_temp_c, 1)
                if "throttle" in watch_list and snap.throttle_percent is not None:
                    data["throttle_pct"] = round(snap.throttle_percent, 1)
                if "mpg" in watch_list and snap.mpg_instant is not None:
                    data["mpg"] = snap.mpg_instant
                if "maf" in watch_list and snap.maf_gps is not None:
                    data["maf_gps"] = round(snap.maf_gps, 2)

                if args.json:
                    print(json.dumps(data))
                else:
                    parts = [f"{k}={v}" for k, v in data.items()]
                    print("  ".join(parts))

                await asyncio.sleep(args.interval)
        except KeyboardInterrupt:
            pass
        finally:
            await obd.stop()

    asyncio.run(run())


# ── camry-camera ──────────────────────────────────────────────────────────

def camera_cli() -> None:
    """Camera test and snapshot capture."""
    parser = argparse.ArgumentParser(description="Camera test tool")
    parser.add_argument("--snapshot", action="store_true", help="Capture single snapshot")
    parser.add_argument("--camera", default="front", choices=["front", "rear"])
    parser.add_argument("--record", type=int, default=0, help="Record N seconds")
    parser.add_argument("--list", action="store_true", help="List camera devices")
    args = parser.parse_args()

    if args.list:
        try:
            from picamera2 import Picamera2
            for i in range(4):
                try:
                    cam = Picamera2(i)
                    print(f"Camera {i}: {cam.camera_properties}")
                    cam.close()
                except Exception:
                    break
        except ImportError:
            print("picamera2 not installed")
        sys.exit(0)

    async def run() -> None:
        recorder = CameraRecorder()
        backend = PiCamera2Backend(recorder)
        await backend.start()

        if args.snapshot:
            path = await backend.capture_snapshot(args.camera)
            if path:
                print(f"Snapshot saved: {path}")
            else:
                print("Failed to capture snapshot")

        if args.record > 0:
            print(f"Recording {args.record} seconds...")
            await asyncio.sleep(args.record)
            print("Recording complete")

        await backend.stop()

    asyncio.run(run())


# ── camry-status (bonus) ─────────────────────────────────────────────────

def status_cli() -> None:
    """Quick system status print."""
    print("Hermes Camry Integration — System Status")
    print("=" * 40)
    print(f"Config dir:  {settings.data_dir}")
    print(f"OBD port:    {settings.obd.port}")
    print(f"GPS port:    {settings.gps.port}")
    print(f"Video dir:   {settings.camera.video_dir}")
    print(f"Log dir:     {settings.storage.log_dir}")
    print(f"DB path:     {settings.storage.sqlite_path}")
    print(f"Encryption:  {'enabled' if settings.camera.encrypt else 'disabled'}")
