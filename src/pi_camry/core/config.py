"""
pi_camry/core/config.py
───────────────────────
Pydantic-based configuration for the Pi-Camry integration.
Loads from environment variables and config files.

Usage:
    from pi_camry.core.config import settings
    print(settings.obd.port)  # "/dev/ttyUSB0"
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class OBDConfig(BaseSettings):
    """OBD-II ELM327 adapter settings."""
    model_config = SettingsConfigDict(env_prefix="OBD_")

    port: str = "/dev/ttyUSB0"
    baudrate: int = 38400
    protocol: str = "AUTO"  # AUTO, ISO9141_2, KWP2000, etc.
    timeout: float = 5.0
    reconnect_interval: float = 10.0
    log_all_pids: bool = True
    # 1996 Camry uses ISO 9141-2 (pin 7) or KWP2000. Not CAN.
    # pyobd auto-detects; we override if needed.


class CameraConfig(BaseSettings):
    """Pi Camera Module 3 (and secondary) settings."""
    model_config = SettingsConfigDict(env_prefix="CAM_")

    enabled: bool = True
    primary_device: str = "/dev/video0"
    secondary_device: str | None = None  # rear camera
    resolution: tuple[int, int] = (1920, 1080)
    fps: int = 30
    # Rolling buffer: keep last N seconds in RAM, flush to disk on event
    buffer_seconds: int = 60
    # Recording triggers
    record_on_motion: bool = True
    record_on_hard_brake: bool = True  # IMU-detected
    record_on_obd_event: bool = True   # e.g., CEL, high coolant
    # Storage
    video_dir: Path = Path("/mnt/nvme0n1p2/video")
    max_disk_usage_percent: float = 85.0
    # Encryption
    encrypt: bool = True
    encryption_key_path: Path = Path("/etc/pi-camry/video.key")


class GPSConfig(BaseSettings):
    """u-blox NEO-M8N GPS settings."""
    model_config = SettingsConfigDict(env_prefix="GPS_")

    enabled: bool = True
    port: str = "/dev/ttyACM0"  # or /dev/ttyUSB1 for USB GPS
    baudrate: int = 9600
    sample_rate_hz: float = 1.0
    # Geofences (lat, lon, radius_m)
    home_location: tuple[float, float] | None = None
    geofence_radius_m: float = 500.0


class IMUConfig(BaseSettings):
    """MPU-6050 / MPU-9250 I2C settings."""
    model_config = SettingsConfigDict(env_prefix="IMU_")

    enabled: bool = True
    i2c_bus: int = 1  # Pi 5 I2C bus 1 (GPIO 2/3)
    i2c_address: int = 0x68  # 0x68 (AD0 low) or 0x69 (AD0 high)
    sample_rate_hz: float = 100.0
    # Motion thresholds for event detection
    collision_g_threshold: float = 3.0   # 3G = probable collision
    hard_brake_g_threshold: float = 0.7  # -0.7G longitudinal
    hard_accel_g_threshold: float = 0.5
    cornering_g_threshold: float = 0.6   # lateral


class GPIOConfig(BaseSettings):
    """GPIO relay and sensor pin assignments for Pi 5."""
    model_config = SettingsConfigDict(env_prefix="GPIO_")

    # Relay outputs (active-low with relay board)
    relay_cooling_fan: int = 17    # BCM GPIO 17 — engine cooling fan override
    relay_fuel_pump: int = 27      # BCM GPIO 27 — starter/fuel kill switch
    relay_headlights: int = 22     # BCM GPIO 22 — auto headlight relay
    relay_dome_light: int = 23     # BCM GPIO 23 — dome light control
    relay_heated_seats: int = 24   # BCM GPIO 24 — heated seat timer
    relay_hvac_compressor: int = 25  # BCM GPIO 25 — AC compressor
    relay_block_heater: int = 5    # BCM GPIO 5 — engine block heater
    relay_power_antenna: int = 6   # BCM GPIO 6 — power antenna

    # Inputs (pull-up/down)
    ignition_sense: int = 16       # BCM GPIO 16 — +12V when ignition ON
    door_ajar: int = 26            # BCM GPIO 26 — any door switch
    trunk_ajar: int = 12           # BCM GPIO 12 — trunk switch
    hood_ajar: int = 13            # BCM GPIO 13 — hood pin switch
    seatbelt: int = 19             # BCM GPIO 19 — seatbelt buckle

    # I2C devices
    i2c_bus: int = 1
    mcp3008_cs: int = 8            # SPI chip select for MCP3008 ADC
    mcp3008_spi_bus: int = 0


class AudioConfig(BaseSettings):
    """USB audio interface and voice settings."""
    model_config = SettingsConfigDict(env_prefix="AUDIO_")

    enabled: bool = True
    input_device: str = "plughw:1,0"  # USB mic (card 1)
    output_device: str = "plughw:0,0"  # 3.5mm or USB DAC
    sample_rate: int = 16000
    channels: int = 1
    chunk_size: int = 1024
    # Wake word
    wake_word: str = "hey hermes"
    # LLM endpoint (Hermes agent over hotspot)
    llm_api_url: str = "http://localhost:8080/v1/chat/completions"
    llm_model: str = "kimi-k2.6"


class TelegramConfig(BaseSettings):
    """Telegram bot for remote alerts and commands."""
    model_config = SettingsConfigDict(env_prefix="TG_")

    enabled: bool = True
    bot_token: str = Field(default="", repr=False)
    allowed_chat_ids: list[int] = Field(default_factory=list)
    # Alert types
    alert_on_collision: bool = True
    alert_on_geofence: bool = True
    alert_on_cel: bool = True
    alert_on_tow: bool = True
    # Commands
    command_status: bool = True
    command_location: bool = True
    command_video_snapshot: bool = True
    command_lock_unlock: bool = True


class StorageConfig(BaseSettings):
    """M.2 NVMe and storage management."""
    model_config = SettingsConfigDict(env_prefix="STORAGE_")

    nvme_device: Path = Path("/dev/nvme0n1")
    mount_point: Path = Path("/mnt/nvme0n1p2")
    # Partitioning (done once at setup)
    partition_boot: bool = False  # already booted from NVMe
    # Logging
    log_dir: Path = Field(default_factory=lambda: Path("/mnt/nvme0n1p2/logs"))
    log_retention_days: int = 90
    # Database
    sqlite_path: Path = Field(default_factory=lambda: Path("/mnt/nvme0n1p2/camry.db"))
    # Video
    video_partition: Path = Path("/mnt/nvme0n1p2/video")
    # Encryption
    luks_enabled: bool = True
    luks_keyfile: Path = Path("/etc/pi-camry/luks.key")


class VehicleConfig(BaseSettings):
    """Vehicle-specific constants."""
    model_config = SettingsConfigDict(env_prefix="VEHICLE_")

    year: int = 1996
    make: str = "Toyota"
    model: str = "Camry"
    engine: str = "5S-FE"  # 2.2L I4, or 1MZ-FE V6
    vin: str = ""  # optional
    # Known OBD-II PIDs for this era
    odometer_ecu: bool = False  # '96 may not expose true odometer via OBD
    fuel_level_pid: bool = False  # not standard on all '96 Toyotas
    # Calibration
    speedo_correction: float = 1.0  # GPS-corrected multiplier
    # Maintenance intervals (miles)
    oil_change_interval: int = 5000
    tire_rotation_interval: int = 7500


class MainConfig(BaseSettings):
    """Top-level configuration. All sub-configs nest here."""
    model_config = SettingsConfigDict(
        env_prefix="CAMRY_",
        env_nested_delimiter="__",
    )

    # Core
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    data_dir: Path = Path("/mnt/nvme0n1p2")

    # Subsystems
    obd: OBDConfig = Field(default_factory=OBDConfig)
    camera: CameraConfig = Field(default_factory=CameraConfig)
    gps: GPSConfig = Field(default_factory=GPSConfig)
    imu: IMUConfig = Field(default_factory=IMUConfig)
    gpio: GPIOConfig = Field(default_factory=GPIOConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    vehicle: VehicleConfig = Field(default_factory=VehicleConfig)

    @field_validator("data_dir", mode="before")
    @classmethod
    def _ensure_path(cls, v: str | Path) -> Path:
        return Path(v)

    def ensure_dirs(self) -> None:
        """Create all required directories. Call once at startup."""
        dirs = [
            self.data_dir,
            self.camera.video_dir,
            self.storage.log_dir,
            self.storage.sqlite_path.parent,
            self.storage.video_partition,
        ]
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)


# Singleton — import this instance
settings = MainConfig()
