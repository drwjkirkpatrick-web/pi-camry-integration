#!/usr/bin/env python3
"""
scripts/bench_test.py
─────────────────────
Hardware bench test script for Pi-Camry integration.

Run this on the Raspberry Pi 5 before vehicle installation to verify:
1. OBD-II connection
2. GPS fix
3. IMU sampling
4. GPIO relay toggling
5. Camera capture
6. Audio I/O
7. M.2 storage I/O
8. WiFi / hotspot connectivity

Usage:
    sudo python3 scripts/bench_test.py --all
    sudo python3 scripts/bench_test.py --obd --gps --gpio
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("bench_test")


# ── Test: OBD-II ──────────────────────────────────────────────────────────

async def test_obd(port: str = "/dev/ttyUSB0") -> bool:
    """Test OBD-II ELM327 connection."""
    logger.info("[OBD] Testing connection to %s...", port)
    try:
        import obd
        conn = obd.OBD(port, baudrate=38400, timeout=5)
        if conn.is_connected():
            proto = conn.protocol_id()
            logger.info("[OBD] ✓ Connected — protocol: %s", proto)
            # Read a few PIDs
            for cmd in [obd.commands.RPM, obd.commands.SPEED, obd.commands.COOLANT_TEMP]:
                resp = conn.query(cmd)
                if not resp.is_null():
                    logger.info("[OBD]   %s = %s", cmd.name, resp.value)
            conn.close()
            return True
        else:
            logger.error("[OBD] ✗ Connected to ELM but no car protocol")
            return False
    except Exception as exc:
        logger.error("[OBD] ✗ Failed: %s", exc)
        return False


# ── Test: GPS ─────────────────────────────────────────────────────────────

async def test_gps(port: str = "/dev/ttyACM0", timeout_sec: float = 30) -> bool:
    """Test GPS module — wait for fix."""
    logger.info("[GPS] Testing %s (timeout %.0fs)...", port, timeout_sec)
    try:
        import serial
        s = serial.Serial(port, 9600, timeout=1.0)
        start = time.monotonic()
        fix_found = False
        while time.monotonic() - start < timeout_sec:
            line = s.readline().decode("ascii", errors="ignore").strip()
            if line.startswith("$GNGGA") or line.startswith("$GPGGA"):
                parts = line.split(",")
                if len(parts) > 6 and parts[6] != "0":
                    lat = parts[2]
                    lon = parts[4]
                    sats = parts[7]
                    logger.info("[GPS] ✓ Fix acquired — %s %s (sats=%s)", lat, lon, sats)
                    fix_found = True
                    break
            await asyncio.sleep(0.01)
        s.close()
        if not fix_found:
            logger.warning("[GPS] ⚠ No fix within timeout (need sky view)")
        return fix_found
    except Exception as exc:
        logger.error("[GPS] ✗ Failed: %s", exc)
        return False


# ── Test: IMU ─────────────────────────────────────────────────────────────

async def test_imu(bus: int = 1, addr: int = 0x68, samples: int = 50) -> bool:
    """Test MPU-6050 I2C and sampling."""
    logger.info("[IMU] Testing MPU-6050 on I2C bus %d, addr 0x%02X...", bus, addr)
    try:
        from smbus2 import SMBus
        bus_handle = SMBus(bus)
        # Wake up MPU
        bus_handle.write_byte_data(addr, 0x6B, 0x00)
        time.sleep(0.1)
        # Read WHO_AM_I
        whoami = bus_handle.read_byte_data(addr, 0x75)
        if whoami != 0x68:
            logger.error("[IMU] ✗ WHO_AM_I = 0x%02X (expected 0x68)", whoami)
            return False
        logger.info("[IMU] ✓ MPU-6050 detected (WHO_AM_I = 0x%02X)", whoami)

        # Sample
        for _ in range(samples):
            data = bus_handle.read_i2c_block_data(addr, 0x3B, 14)
            ax = (data[0] << 8) | data[1]
            ay = (data[2] << 8) | data[3]
            az = (data[4] << 8) | data[5]
            logger.debug("[IMU]   ax=%d ay=%d az=%d", ax, ay, az)
            await asyncio.sleep(0.01)
        bus_handle.close()
        logger.info("[IMU] ✓ %d samples collected", samples)
        return True
    except Exception as exc:
        logger.error("[IMU] ✗ Failed: %s", exc)
        return False


# ── Test: GPIO Relays ─────────────────────────────────────────────────────

async def test_gpio() -> bool:
    """Test each relay by toggling and verifying with LED/click."""
    logger.info("[GPIO] Testing relay outputs...")
    logger.warning("[GPIO] You should hear relay clicks or see LEDs!")
    try:
        import lgpio
        h = lgpio.gpiochip_open(0)
        # Pins from config
        relay_pins = [17, 27, 22, 23, 24, 25, 5, 6]
        names = ["cooling_fan", "fuel_pump", "headlights", "dome_light",
                 "heated_seats", "hvac_compressor", "block_heater", "power_antenna"]
        for pin, name in zip(relay_pins, names):
            lgpio.gpio_claim_output(h, pin, level=1)  # start OFF
            logger.info("[GPIO]   Toggling %s (pin %d)...", name, pin)
            lgpio.gpio_write(h, pin, 0)  # ON
            await asyncio.sleep(0.3)
            lgpio.gpio_write(h, pin, 1)  # OFF
            await asyncio.sleep(0.2)
        lgpio.gpiochip_close(h)
        logger.info("[GPIO] ✓ All relays toggled")
        return True
    except Exception as exc:
        logger.error("[GPIO] ✗ Failed: %s", exc)
        return False


# ── Test: Camera ──────────────────────────────────────────────────────────

async def test_camera() -> bool:
    """Test Pi Camera Module 3 capture."""
    logger.info("[Camera] Testing Pi Camera...")
    try:
        from picamera2 import Picamera2
        cam = Picamera2(0)
        cfg = cam.create_still_configuration()
        cam.configure(cfg)
        cam.start()
        await asyncio.sleep(2)  # auto-exposure
        path = "/tmp/camry_test_snapshot.jpg"
        cam.capture_file(path)
        cam.stop()
        cam.close()
        if Path(path).exists():
            size = Path(path).stat().st_size
            logger.info("[Camera] ✓ Snapshot saved: %s (%d bytes)", path, size)
            return True
        return False
    except Exception as exc:
        logger.error("[Camera] ✗ Failed: %s", exc)
        return False


# ── Test: Audio ────────────────────────────────────────────────────────────

async def test_audio() -> bool:
    """Test USB mic + speaker."""
    logger.info("[Audio] Testing microphone and speaker...")
    try:
        import subprocess
        # Record 3 seconds
        rec_path = "/tmp/camry_test_audio.wav"
        logger.info("[Audio]   Recording 3 seconds... speak now!")
        proc = await asyncio.create_subprocess_exec(
            "arecord", "-D", "plughw:1,0", "-d", "3", "-f", "cd",
            rec_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await proc.wait()
        if Path(rec_path).exists():
            logger.info("[Audio]   Playing back...")
            proc2 = await asyncio.create_subprocess_exec(
                "aplay", "-D", "plughw:0,0", rec_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            await proc2.wait()
            logger.info("[Audio] ✓ Record/playback complete")
            return True
        return False
    except Exception as exc:
        logger.error("[Audio] ✗ Failed: %s", exc)
        return False


# ── Test: Storage ───────────────────────────────────────────────────────────

async def test_storage(mount: str = "/mnt/nvme0n1p2") -> bool:
    """Test M.2 NVMe read/write speed."""
    logger.info("[Storage] Testing M.2 NVMe at %s...", mount)
    try:
        import subprocess
        test_file = Path(mount) / ".bench_test_write"
        # Write 100MB
        proc = await asyncio.create_subprocess_exec(
            "dd", "if=/dev/zero", f"of={test_file}", "bs=1M", "count=100",
            "oflag=direct",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        write_result = stderr.decode().strip().split("\n")[-1]
        test_file.unlink(missing_ok=True)
        logger.info("[Storage] ✓ Write: %s", write_result)
        return True
    except Exception as exc:
        logger.error("[Storage] ✗ Failed: %s", exc)
        return False


# ── Test: WiFi ──────────────────────────────────────────────────────────────

async def test_wifi() -> bool:
    """Test WiFi connectivity and hotspot reachability."""
    logger.info("[WiFi] Testing connectivity...")
    try:
        import subprocess
        # Check current SSID
        proc = await asyncio.create_subprocess_exec(
            "iwgetid", "-r",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        ssid = stdout.decode().strip()
        if ssid:
            logger.info("[WiFi] ✓ Connected to: %s", ssid)
        else:
            logger.warning("[WiFi] ⚠ Not connected to any network")

        # Ping test
        proc2 = await asyncio.create_subprocess_exec(
            "ping", "-c", "3", "-W", "5", "8.8.8.8",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stdout2, _ = await proc2.communicate()
        if proc2.returncode == 0:
            logger.info("[WiFi] ✓ Internet reachable")
            return True
        else:
            logger.warning("[WiFi] ⚠ No internet (hotspot may be needed)")
            return False
    except Exception as exc:
        logger.error("[WiFi] ✗ Failed: %s", exc)
        return False


# ── Main ──────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="Pi-Camry hardware bench test")
    parser.add_argument("--all", action="store_true", help="Run all tests")
    parser.add_argument("--obd", action="store_true", help="Test OBD-II")
    parser.add_argument("--gps", action="store_true", help="Test GPS")
    parser.add_argument("--imu", action="store_true", help="Test IMU")
    parser.add_argument("--gpio", action="store_true", help="Test GPIO relays")
    parser.add_argument("--camera", action="store_true", help="Test camera")
    parser.add_argument("--audio", action="store_true", help="Test audio I/O")
    parser.add_argument("--storage", action="store_true", help="Test M.2 storage")
    parser.add_argument("--wifi", action="store_true", help="Test WiFi")
    args = parser.parse_args()

    if not any(vars(args).values()):
        parser.print_help()
        sys.exit(1)

    run_all = args.all
    results: dict[str, bool] = {}

    if run_all or args.obd:
        results["OBD-II"] = await test_obd()
    if run_all or args.gps:
        results["GPS"] = await test_gps()
    if run_all or args.imu:
        results["IMU"] = await test_imu()
    if run_all or args.gpio:
        results["GPIO"] = await test_gpio()
    if run_all or args.camera:
        results["Camera"] = await test_camera()
    if run_all or args.audio:
        results["Audio"] = await test_audio()
    if run_all or args.storage:
        results["Storage"] = await test_storage()
    if run_all or args.wifi:
        results["WiFi"] = await test_wifi()

    # Summary
    logger.info("=" * 50)
    logger.info("Bench Test Summary")
    logger.info("=" * 50)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    for name, ok in results.items():
        status = "✓ PASS" if ok else "✗ FAIL"
        logger.info("  %-12s %s", name + ":", status)
    logger.info("-" * 50)
    logger.info("Result: %d/%d tests passed", passed, total)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
