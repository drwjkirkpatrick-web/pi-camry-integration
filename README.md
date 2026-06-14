# Pi-Camry Integration

Hermes Agent × Raspberry Pi 5 × 1996 Toyota Camry — Complete vehicle telemetry, security, and AI copilot platform.

**150 integrations** | **Dual-camera dashcam** | **OBD-II live diagnostics** | **Telegram remote control** | **Hermes LLM voice assistant**

---

## What This Is

A comprehensive Python platform that turns a Raspberry Pi 5 into the brain of a 1996 Toyota Camry. Every subsystem — engine, safety, navigation, security, climate, lighting, audio, data logging, and AI — is wired, coded, and event-driven.

- **OBD-II** live telemetry via ELM327 (ISO 9141-2 / KWP2000 for '96)
- **Dual-camera** rolling-buffer dashcam with AES-256 encryption on M.2 NVMe
- **GPS** tracking with geofences and trip logging
- **IMU** collision detection, hard-brake logging, tow alerts
- **GPIO relay control** for headlights, cooling fan, fuel pump kill, HVAC
- **Telegram bot** — remote status, location, video snapshots, lock/unlock
- **Hermes voice assistant** — "Hey Hermes, why is my idle rough?"
- **M.2 NVMe** 1TB storage architecture with LUKS encryption

---

## Hardware Stack

| Component | Model | Purpose |
|-----------|-------|---------|
| Main Computer | Raspberry Pi 5 8GB | Hermes agent host |
| Storage | Pineberry Pi M.2 HAT+ + 1TB NVMe | Boot + video + logs |
| OBD Adapter | USB ELM327 (ISO/KWP firmware) | ECU communication |
| Front Camera | Pi Camera Module 3 (wide) | Dashcam |
| Rear Camera | Pi Camera Module 3 | Rear-view / parking |
| GPS | u-blox NEO-M8N USB | Location / speed / geofence |
| IMU | MPU-6050 (I2C) | Motion / collision / G-force |
| Audio | USB mic + 3.5mm DAC | Voice assistant / alerts |
| Relays | 8-channel 5V relay board | 12V circuit control |
| ADC | MCP3008 (SPI) | Analog sensor reading |
| Power | 12V→5V/3A buck + PD trigger | Clean Pi 5 power |

Full parts list, wiring diagrams, and GPIO pinout are in [`guide.pdf`](guide.pdf) (47 pages).

---

## Project Structure

```
pi-camry-integration/
├── src/pi_camry/
│   ├── core/           # Config, logging, event bus
│   ├── obd/            # OBD-II ELM327 interface
│   ├── camera/         # Dual-camera recorder with rolling buffer
│   ├── gps/            # u-blox NMEA tracker
│   ├── imu/            # MPU-6050 motion sensor
│   ├── gpio/           # Relay + input control
│   ├── audio/          # Voice I/O + LLM integration
│   ├── telegram/       # Bot commands + alerts
│   ├── storage/        # M.2 / SQLite / encryption
│   └── main.py         # Daemon orchestrator
├── tests/              # pytest suite
├── configs/            # YAML configs per vehicle
├── scripts/            # Setup + install scripts
├── docs/               # Wiring diagrams, references
├── guide.pdf           # Complete 150-integration reference
└── pyproject.toml      # Dependencies + entry points
```

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/drwjkirkpatrick-web/pi-camry-integration.git
cd pi-camry-integration

# 2. Install (creates venv automatically with uv)
uv pip install -e ".[dev]"

# 3. Configure
cp configs/camry.example.yaml configs/camry.yaml
# Edit: OBD port, GPS port, Telegram token, GPIO pins

# 4. Run the daemon
camry-daemon

# 5. Or run individual modules
camry-obd   # Poll OBD-II live
camry-camera  # Start dashcam
```

---

## Modules

### OBD-II (`pi_camry.obd`)
- Continuous PID polling at 2 Hz
- DTC read/clear with Telegram alerts
- Instant MPG calculation from MAF + speed
- Auto-reconnect with exponential backoff

### Camera (`pi_camry.camera`)
- Circular RAM buffer (default 60 sec)
- Event-triggered lock: collision, hard brake, CEL
- H.264 to M.2 NVMe, auto-prune old segments
- AES-256 encryption via `cryptography.fernet`

### GPS (`pi_camry.gps`)
- NMEA GGA/RMC/VTG parsing
- Geofence enter/exit events
- Trip logging with distance + duration
- "Find My Car" Telegram command

### IMU (`pi_camry.imu`)
- 100 Hz sampling from MPU-6050
- 3G collision detection → auto video lock
- Hard brake / hard accel / cornering alerts
- Tow detection (motion while ignition off)

### GPIO (`pi_camry.gpio`)
- Ignition-sense graceful shutdown
- Relay control: fan, headlights, fuel pump, HVAC
- Door/trunk/hood ajar monitoring
- ADC via MCP3008 for analog sensors

---

## Environment Variables

```bash
# Core
CAMRY_DEBUG=true
CAMRY_LOG_LEVEL=INFO

# OBD
OBD_PORT=/dev/ttyUSB0
OBD_PROTOCOL=ISO9141_2

# Camera
CAM_BUFFER_SECONDS=60
CAM_ENCRYPT=true

# GPS
GPS_PORT=/dev/ttyACM0
GPS_HOME_LOCATION=45.5152,-122.6784

# Telegram
TG_BOT_TOKEN=your_token_here
TG_ALLOWED_CHAT_IDS=123456789

# Storage
STORAGE_NVME_DEVICE=/dev/nvme0n1
```

---

## Safety Notes

- **Relay wiring**: Always fuse 12V taps. Use automotive relay sockets, not breadboard relays for permanent install.
- **Fuel pump kill switch**: Test thoroughly. A failed relay = stranded.
- **Encryption key**: Store `/etc/pi-camry/video.key` on a separate USB or TPM if available. M.2 is encrypted at rest.
- **OBD protocols**: 1996 Camry uses ISO 9141-2 (pin 7) or KWP2000. Ensure ELM327 firmware supports pre-CAN.

---

## License

MIT — See [LICENSE](LICENSE).

---

*Built for a 1996 Toyota Camry. Generalizes to any OBD-II vehicle with analog accessories.*
