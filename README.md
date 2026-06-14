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
- **Voice assistant** — "Hey Hermes, why is my idle rough?" (faster-whisper + local LLM)
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

## Quick Start

### One-line install (on Pi 5)

```bash
curl -sSL https://raw.githubusercontent.com/drwjkirkpatrick-web/pi-camry-integration/main/scripts/install.sh | sudo bash
```

Or manual:

```bash
# 1. Clone
git clone https://github.com/drwjkirkpatrick-web/pi-camry-integration.git
cd pi-camry-integration

# 2. Install (creates venv automatically with uv)
uv pip install -e ".[dev]"

# 3. Configure
cp configs/camry.example.yaml /etc/pi-camry/camry.yaml
# Edit: OBD port, GPS port, Telegram token, GPIO pins

# 4. Run bench test
sudo .venv/bin/python scripts/bench_test.py --all

# 5. Start the daemon
camry-daemon

# 6. Or run individual modules
camry-obd --watch rpm,speed,coolant
camry-camera --snapshot
```

---

## Project Structure

```
pi-camry-integration/
├── src/pi_camry/
│   ├── core/           # Config, logging, async event bus
│   ├── obd/            # OBD-II ELM327 interface (ISO 9141-2 / KWP2000)
│   ├── camera/         # Dual-camera recorder + picamera2 backend
│   ├── gps/            # u-blox NMEA tracker
│   ├── imu/            # MPU-6050 motion sensor
│   ├── gpio/           # Relay + input control (lgpio)
│   ├── audio/          # Voice I/O + faster-whisper STT + LLM integration
│   ├── telegram/       # Bot commands + alerts
│   ├── storage/        # M.2 / SQLite / encryption
│   ├── cli.py          # camry-obd, camry-camera CLI tools
│   └── main.py         # Daemon orchestrator
├── tests/              # pytest suite (87 tests, mock-heavy)
├── configs/            # YAML configs + environment templates
├── scripts/            # install.sh, bench_test.py, systemd service
├── docs/               # Wiring diagrams, references
├── guide.pdf           # Complete 150-integration reference (47 pages)
└── pyproject.toml      # Dependencies + entry points
```

---

## Modules

### OBD-II (`pi_camry.obd`)
- Continuous PID polling at 2 Hz
- DTC read/clear with Telegram alerts
- Instant MPG calculation from MAF + speed
- Auto-reconnect with exponential backoff
- **CLI:** `camry-obd --watch rpm,speed,coolant --interval 1`

### Camera (`pi_camry.camera`)
- Circular RAM buffer (default 60 sec)
- Event-triggered lock: collision, hard brake, CEL
- H.264 hardware encode via picamera2 → M.2 NVMe
- AES-256 encryption via `cryptography.fernet`
- Motion detection via frame differencing
- **CLI:** `camry-camera --snapshot --camera front`

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
- Ignition-sense graceful shutdown (60s timer)
- Relay control: fan, headlights, fuel pump, HVAC
- Door/trunk/hood ajar monitoring
- ADC via MCP3008 for analog sensors

### Audio / Voice Assistant (`pi_camry.audio`)
- Wake word detection (energy-based, whisper for production)
- faster-whisper `tiny.en` model (local, ~75MB, real-time on Pi 5)
- LLM query via aiohttp (Hermes/local endpoint)
- TTS: espeak-ng (fast) → pyttsx3 fallback
- Event-triggered spoken alerts (collision, CEL)

### Telegram Bot (`pi_camry.telegram`)
- **Commands:** `/status`, `/location`, `/find`, `/video`, `/dtc`, `/lock`, `/unlock`, `/climate`, `/trip`
- **Auto-alerts:** collision, CEL, geofence exit, tow, low storage, low battery
- Photo support for video snapshots

### Storage (`pi_camry.storage`)
- aiosqlite async database
- Tables: obd_logs, gps_tracks, events, video_segments, maintenance
- psutil disk monitoring (auto-prune at 85%)
- 90-day log retention

---

## Testing

```bash
# Run full test suite (all hardware mocked)
python -m pytest tests/ -v

# Run specific module
python -m pytest tests/test_obd.py -v
python -m pytest tests/test_camera.py -v

# Hardware bench test (requires actual Pi + sensors)
sudo python scripts/bench_test.py --all
```

The test suite uses `pytest.importorskip` and `unittest.mock` to skip gracefully when hardware-specific dependencies (lgpio, pyaudio, picamera2) are unavailable.

---

## Systemd Service

```bash
# Install service
sudo cp scripts/camry-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable camry-daemon
sudo systemctl start camry-daemon

# View logs
sudo journalctl -u camry-daemon -f
```

The service runs as `pi` user, loads environment from `/etc/pi-camry/environment`, and handles SIGTERM for graceful shutdown.

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
TG_BOT_TOKEN=your_t...n

# Storage
STORAGE_NVME_DEVICE=/dev/nvme0n1
```

Secrets should go in `/etc/pi-camry/environment` (mode 600), sourced by systemd.

---

## Whisper STT Setup

```bash
# faster-whisper auto-downloads models on first use
# Or pre-download during install:
python -c "from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8')"

# Models available:
#   tiny.en  (~75MB)  — fastest, good accuracy, RECOMMENDED for Pi 5
#   base.en  (~150MB) — slightly better accuracy
#   small.en (~500MB) — best accuracy, slower
```

---

## Safety Notes

- **Relay wiring:** Always fuse 12V taps. Use automotive relay sockets, not breadboard relays for permanent install.
- **Fuel pump kill switch:** Test thoroughly. A failed relay = stranded.
- **Encryption key:** Store `/etc/pi-camry/video.key` on a separate USB or TPM if available. M.2 is encrypted at rest.
- **OBD protocols:** 1996 Camry uses ISO 9141-2 (pin 7) or KWP2000. Ensure ELM327 firmware supports pre-CAN.
- **Voice assistant:** The LLM endpoint should be your own (local Ollama, Hermes agent, etc.) — do not send vehicle data to third-party APIs without consent.

---

## License

MIT — See [LICENSE](LICENSE).

---

*Built for a 1996 Toyota Camry. Generalizes to any OBD-II vehicle with analog accessories.*
