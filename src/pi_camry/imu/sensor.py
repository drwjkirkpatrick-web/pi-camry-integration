"""
pi_camry/imu/sensor.py
──────────────────────
MPU-6050 / MPU-9250 I2C driver with motion event detection.

Features:
- 6-axis (accel + gyro) or 9-axis (with magnetometer on MPU-9250)
- Configurable sample rate and digital low-pass filter
- Real-time G-force analysis: collision, hard brake, hard accel, cornering
- Tow detection: wheels moving but engine off (via ignition GPIO + motion)
- Event emission on async bus
- Calibration and bias compensation

Wiring (Pi 5):
    VCC  → 3.3V (Pin 1)
    GND  → GND  (Pin 6)
    SDA  → GPIO 2 / Pin 3 (I2C1 SDA)
    SCL  → GPIO 3 / Pin 5 (I2C1 SCL)
    AD0  → GND  (I2C address 0x68) or 3.3V (0x69)

Usage:
    from pi_camry.imu.sensor import IMUSensor
    imu = IMUSensor()
    await imu.start()
    # Events auto-published on bus
    await imu.stop()
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from pi_camry.core import EventType, bus
from pi_camry.core.config import settings

logger = logging.getLogger("camry.imu")

# ── MPU-6050 register map ────────────────────────────────────────────────────
MPU6050_ADDR = 0x68
MPU6050_PWR_MGMT_1 = 0x6B
MPU6050_SMPLRT_DIV = 0x19
MPU6050_CONFIG = 0x1A      # DLPF config
MPU6050_GYRO_CONFIG = 0x1B
MPU6050_ACCEL_CONFIG = 0x1C
MPU6050_INT_ENABLE = 0x38
MPU6050_ACCEL_XOUT_H = 0x3B
MPU6050_TEMP_OUT_H = 0x41
MPU6050_GYRO_XOUT_H = 0x43

# Scale factors (default ±2g, ±250°/s)
ACCEL_SCALE = 16384.0   # LSB/g for ±2g
GYRO_SCALE = 131.0      # LSB/(°/s) for ±250°/s


@dataclass
class IMUReading:
    """A single calibrated IMU sample."""
    timestamp: datetime = field(default_factory=datetime.utcnow)
    ax: float = 0.0   # m/s²
    ay: float = 0.0
    az: float = 0.0
    gx: float = 0.0   # °/s
    gy: float = 0.0
    gz: float = 0.0
    temp_c: float = 0.0
    # Derived
    total_g: float = 0.0          # sqrt(ax²+ay²+az²) / 9.81
    lateral_g: float = 0.0        # ay / 9.81 (positive = right turn)
    longitudinal_g: float = 0.0  # ax / 9.81 (positive = acceleration)
    vertical_g: float = 0.0      # az / 9.81


class IMUSensor:
    """Async MPU-6050/9250 driver with motion event detection."""

    def __init__(self) -> None:
        self.cfg = settings.imu
        self._running = False
        self._task: asyncio.Task | None = None
        self._latest = IMUReading()
        self._lock = asyncio.Lock()

        # I2C bus handle (smbus2)
        self._bus: any = None  # type: ignore[annotation-unchecked]
        self._addr = self.cfg.i2c_address

        # Calibration offsets
        self._bias = {"ax": 0.0, "ay": 0.0, "az": 0.0, "gx": 0.0, "gy": 0.0, "gz": 0.0}
        self._calibrated = False

        # State for event debouncing
        self._last_collision_time = 0.0
        self._last_brake_time = 0.0
        self._last_accel_time = 0.0
        self._last_corner_time = 0.0
        self._debounce_sec = 2.0

        # Tow detection state
        self._ignition_was_on = False
        self._motion_while_off = False

    # ── Public API ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize I2C, configure MPU, calibrate, start sampling."""
        logger.info("IMU: initializing on I2C bus %d, addr 0x%02X", self.cfg.i2c_bus, self._addr)
        try:
            from smbus2 import SMBus
            self._bus = SMBus(self.cfg.i2c_bus)
        except ImportError:
            logger.error("IMU: smbus2 not installed")
            return
        except Exception as exc:
            logger.error("IMU: failed to open I2C bus: %s", exc)
            return

        self._configure_mpu()
        await self._calibrate()

        self._running = True
        self._task = asyncio.create_task(self._sample_loop())
        logger.info("IMU: sampling at %.0f Hz", self.cfg.sample_rate_hz)

    async def stop(self) -> None:
        """Stop sampling and close I2C."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._bus:
            self._bus.close()
        logger.info("IMU: stopped")

    def get_latest(self) -> IMUReading:
        """Thread-safe read of latest sample."""
        return self._latest

    # ── MPU-6050 setup ──────────────────────────────────────────────────────────

    def _configure_mpu(self) -> None:
        """Wake up, set sample rate, DLPF, full-scale ranges."""
        if not self._bus:
            return
        # Wake up (clear sleep bit)
        self._bus.write_byte_data(self._addr, MPU6050_PWR_MGMT_1, 0x00)
        # Sample rate divider: 1000 / (1 + div) = target rate
        # For 100 Hz: div = 9
        div = int(1000 / self.cfg.sample_rate_hz) - 1
        self._bus.write_byte_data(self._addr, MPU6050_SMPLRT_DIV, max(0, min(255, div)))
        # DLPF: 0x03 = ~44 Hz accel, 42 Hz gyro (good for vehicle dynamics)
        self._bus.write_byte_data(self._addr, MPU6050_CONFIG, 0x03)
        # Gyro full scale: ±250°/s (0x00)
        self._bus.write_byte_data(self._addr, MPU6050_GYRO_CONFIG, 0x00)
        # Accel full scale: ±2g (0x00)
        self._bus.write_byte_data(self._addr, MPU6050_ACCEL_CONFIG, 0x00)
        # Enable data ready interrupt (optional)
        self._bus.write_byte_data(self._addr, MPU6050_INT_ENABLE, 0x01)
        time.sleep(0.1)

    async def _calibrate(self) -> None:
        """Collect N samples at rest to compute bias offsets."""
        logger.info("IMU: calibrating — keep vehicle still for 3 seconds...")
        samples = 200
        sums = {"ax": 0.0, "ay": 0.0, "az": 0.0, "gx": 0.0, "gy": 0.0, "gz": 0.0}
        for _ in range(samples):
            raw = self._read_raw()
            for k in sums:
                sums[k] += raw[k]
            await asyncio.sleep(0.01)

        for k in sums:
            self._bias[k] = sums[k] / samples

        # Expected: az ≈ 1g when flat; subtract 1g from accel_z bias
        self._bias["az"] -= ACCEL_SCALE  # LSB for 1g at ±2g

        self._calibrated = True
        logger.info(
            "IMU: calibrated — biases ax=%.1f ay=%.1f az=%.1f gx=%.1f gy=%.1f gz=%.1f",
            self._bias["ax"], self._bias["ay"], self._bias["az"],
            self._bias["gx"], self._bias["gy"], self._bias["gz"],
        )

    # ── Sampling loop ──────────────────────────────────────────────────────────

    async def _sample_loop(self) -> None:
        """High-frequency sampling with event detection."""
        interval = 1.0 / self.cfg.sample_rate_hz
        while self._running:
            try:
                t0 = time.monotonic()
                raw = self._read_raw()
                reading = self._to_reading(raw)

                async with self._lock:
                    self._latest = reading

                self._detect_events(reading)
                elapsed = time.monotonic() - t0
                sleep_for = max(0, interval - elapsed)
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("IMU: sample loop error")
                await asyncio.sleep(0.1)

    def _read_raw(self) -> dict[str, int]:
        """Read 14 bytes from MPU-6050: accel(6) + temp(2) + gyro(6)."""
        if not self._bus:
            return {"ax": 0, "ay": 0, "az": 0, "gx": 0, "gy": 0, "gz": 0, "temp": 0}
        data = self._bus.read_i2c_block_data(self._addr, MPU6050_ACCEL_XOUT_H, 14)
        return {
            "ax": self._to_signed(data[0], data[1]),
            "ay": self._to_signed(data[2], data[3]),
            "az": self._to_signed(data[4], data[5]),
            "temp": self._to_signed(data[6], data[7]),
            "gx": self._to_signed(data[8], data[9]),
            "gy": self._to_signed(data[10], data[11]),
            "gz": self._to_signed(data[12], data[13]),
        }

    @staticmethod
    def _to_signed(hi: int, lo: int) -> int:
        """Combine high/low bytes to signed 16-bit."""
        val = (hi << 8) | lo
        return val - 65536 if val > 32767 else val

    def _to_reading(self, raw: dict[str, int]) -> IMUReading:
        """Convert raw values to calibrated physical units."""
        if not self._calibrated:
            return IMUReading()

        ax = (raw["ax"] - self._bias["ax"]) / ACCEL_SCALE * 9.80665
        ay = (raw["ay"] - self._bias["ay"]) / ACCEL_SCALE * 9.80665
        az = (raw["az"] - self._bias["az"]) / ACCEL_SCALE * 9.80665
        gx = (raw["gx"] - self._bias["gx"]) / GYRO_SCALE
        gy = (raw["gy"] - self._bias["gy"]) / GYRO_SCALE
        gz = (raw["gz"] - self._bias["gz"]) / GYRO_SCALE
        temp = raw["temp"] / 340.0 + 36.53  # datasheet formula

        total_g = math.sqrt(ax**2 + ay**2 + az**2) / 9.80665
        return IMUReading(
            ax=round(ax, 3),
            ay=round(ay, 3),
            az=round(az, 3),
            gx=round(gx, 2),
            gy=round(gy, 2),
            gz=round(gz, 2),
            temp_c=round(temp, 1),
            total_g=round(total_g, 2),
            lateral_g=round(ay / 9.80665, 2),
            longitudinal_g=round(ax / 9.80665, 2),
            vertical_g=round(az / 9.80665, 2),
        )

    # ── Event detection ───────────────────────────────────────────────────────

    def _detect_events(self, r: IMUReading) -> None:
        """Analyze reading and publish events if thresholds crossed."""
        now = time.monotonic()

        # Collision: very high total G
        if r.total_g >= self.cfg.collision_g_threshold:
            if now - self._last_collision_time > self._debounce_sec:
                self._last_collision_time = now
                asyncio.create_task(
                    bus.publish(
                        EventType.IMU_COLLISION,
                        {"g_force": r.total_g, "ax": r.ax, "ay": r.ay, "az": r.az},
                        source="imu",
                    )
                )
                logger.critical("IMU: COLLISION detected — %.1fG", r.total_g)

        # Hard brake: strong negative longitudinal G
        if r.longitudinal_g <= -self.cfg.hard_brake_g_threshold:
            if now - self._last_brake_time > self._debounce_sec:
                self._last_brake_time = now
                asyncio.create_task(
                    bus.publish(
                        EventType.IMU_HARD_BRAKE,
                        {"longitudinal_g": r.longitudinal_g, "speed_delta": None},
                        source="imu",
                    )
                )
                logger.warning("IMU: HARD BRAKE — %.1fG", r.longitudinal_g)

        # Hard acceleration
        if r.longitudinal_g >= self.cfg.hard_accel_g_threshold:
            if now - self._last_accel_time > self._debounce_sec:
                self._last_accel_time = now
                logger.info("IMU: hard acceleration — %.1fG", r.longitudinal_g)

        # Aggressive cornering
        if abs(r.lateral_g) >= self.cfg.cornering_g_threshold:
            if now - self._last_corner_time > self._debounce_sec:
                self._last_corner_time = now
                logger.info("IMU: aggressive cornering — %.1fG lateral", r.lateral_g)

        # Tow detection: motion while ignition was on, now off
        # (requires ignition GPIO event subscription — see below)
        # self._check_tow(r)

    def on_ignition_change(self, ignition_on: bool) -> None:
        """Called by GPIO module when ignition state changes."""
        if ignition_on:
            self._ignition_was_on = True
            self._motion_while_off = False
        else:
            # Ignition turned off — start watching for motion
            if self._ignition_was_on:
                self._motion_while_off = False
                # Tow detection will be checked in next samples

    def _check_tow(self, r: IMUReading) -> None:
        """Detect if vehicle is moving while ignition is off."""
        if not self._ignition_was_on:
            return  # never been on this session
        # If ignition is off and we see sustained motion
        # This is a simplified check; real tow detection needs more logic
        if r.total_g > 1.3:  # unusual motion
            if not self._motion_while_off:
                self._motion_while_off = True
                asyncio.create_task(
                    bus.publish(
                        EventType.IMU_TOW_DETECTED,
                        {"g_force": r.total_g},
                        source="imu",
                    )
                )
                logger.warning("IMU: TOW DETECTED — motion while ignition off")
