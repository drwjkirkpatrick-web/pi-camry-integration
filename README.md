# Pi-Camry Integration

Hermes Agent × Raspberry Pi 5 × 1996 Toyota Camry — Complete vehicle telemetry, security, and AI copilot platform.

**150+ integrations** | **Dual-camera dashcam** | **OBD-II live diagnostics** | **Voice copilot** | **Modern vehicle adapter**

---

## Quick Start

```bash
# 1. Flash Pi 5 with Raspberry Pi OS, enable I2C/SPI/camera
# 2. Install M.2 HAT+ with 1TB NVMe
# 3. Clone and install
git clone https://github.com/drwjkirkpatrick-web/pi-camry-integration.git
cd pi-camry-integration
sudo bash scripts/install.sh

# 4. Edit config
sudo nano /etc/pi-camry/camry.yaml

# 5. Run bench test
sudo .venv/bin/python scripts/bench_test.py --all

# 6. Start daemon
sudo systemctl start camry-daemon
```

---

## Hardware Stack

| Component | Part | Interface | Status |
|-----------|------|-----------|--------|
| **Pi 5** | Raspberry Pi 5 8GB | — | ✅ Core |
| **Storage** | Pineberry Pi M.2 HAT+ 1TB NVMe | PCIe | ✅ 72hr rolling buffer |
| **OBD-II** | ELM327 USB | USB | ✅ ISO 9141-2 / KWP2000 |
| **GPS** | u-blox NEO-M8N | UART/USB | ✅ NMEA, geofence |
| **RTK GPS** | u-blox ZED-F9P | UART | 🆕 2cm accuracy |
| **Camera (front)** | Pi Camera Module 3 | CSI0 | ✅ 1080p H.264 |
| **Camera (rear/cabin)** | Pi Camera Module 3 | CSI1 | ✅ 1080p H.264 |
| **Thermal** | MLX90640 / FLIR Lepton | I2C/SPI | 🆕 Night vision |
| **IMU** | MPU-6050 / MPU-9250 | I2C | ✅ 100Hz, collision detect |
| **LTE** | Quectel EC25 / SIM7600 | USB | 🆕 Always-on 4G |
| **Rain** | IR reflectance module | ADC (MCP3008) | 🆕 Auto wipers |
| **Ultrasonic** | HC-SR04 × 8 | GPIO | 🆕 Parking assist |
| **Air quality** | SCD40 + SGP40 + SPS30 | I2C | 🆕 CO2/VOC/PM2.5 |
| **Display** | JoyBring 10.1" AV touchscreen | HDMI + USB | 🆕 Head-unit replacement |
| **Audio** | USB mic + AUX/FM | USB/3.5mm | ✅ Voice copilot |
| **Power** | 12V→5V/5A buck + relay board | Hardwired | ✅ IGN-switched |

---

## Module Architecture

```
Event Bus (async pub/sub, 30+ event types)
    ├── Core
    │   ├── config.py          # Pydantic settings, 15 subsystems
    │   ├── __init__.py        # EventBus, 30 event types, logging
    │   └── main.py            # Daemon orchestrator
    │
    ├── 1996 Camry (Base)
    │   ├── obd/interface.py     # ELM327, ISO 9141-2, 15 PIDs
    │   ├── camera/recorder.py  # Dual 1080p, rolling buffer, AES-256
    │   ├── camera/picamera2_backend.py  # Real Pi Camera 3 H.264
    │   ├── gps/tracker.py     # NMEA, geofence, trip logging
    │   ├── imu/sensor.py      # MPU-6050, collision/tow detection
    │   ├── gpio/controller.py  # lgpio relays, inputs, MCP3008 ADC
    │   ├── audio/assistant.py  # Wake word, LLM query, TTS
    │   ├── audio/whisper_stt.py  # faster-whisper local STT
    │   ├── telegram/bot.py    # 10 commands, auto-alerts
    │   └── storage/manager.py  # aiosqlite, 5 tables, disk monitor
    │
    ├── Display (JoyBring)
    │   ├── joybring.py        # Main controller
    │   ├── hdmi_sink.py       # HDMI, EDID, CEC, backlight
    │   ├── touch_input.py     # USB multi-touch
    │   ├── can_bridge.py      # Vehicle CAN ↔ head-unit
    │   ├── steering_wheel.py  # ADC resistor ladder
    │   ├── dashboard.py       # Kivy GUI, 8 pages
    │   └── radio.py           # FM/AM/internet radio
    │
    ├── Modern Vehicle (2008+)
    │   ├── can_multibus.py    # Multi-CAN bus (PT/body/chassis)
    │   ├── uds_client.py      # UDS diagnostic (CAN-TP, DoIP)
    │   ├── tpms_direct.py     # Direct RF TPMS (RTL-SDR)
    │   ├── radar_fusion.py    # ADAS radar fusion (ACC, AEB)
    │   ├── driver_monitor.py  # DMS (drowsiness, distraction)
    │   └── interior_sensing.py  # Air quality (CO2, VOC, PM)
    │
    ├── Connectivity
    │   └── lte.py             # 4G LTE, NTRIP, MQTT, SMS
    │
    └── Sensors
        ├── rain_sensor.py     # Auto wipers, hysteresis
        ├── ultrasonic_array.py  # 8× parking sensors
        ├── rtk_gps.py         # Centimeter GPS (ZED-F9P)
        └── thermal_camera.py  # Night vision, hotspot detection
```

---

## CLI Tools

```bash
# Individual subsystem testing
camry-obd --watch rpm,speed,coolant   # Live OBD monitor
camry-camera --snapshot               # Capture still image
camry-camera --record 60              # Record 60-second clip

# Bench test (run before vehicle install)
python scripts/bench_test.py --all
python scripts/bench_test.py --obd --gps --imu --gpio --camera --audio --lte
```

---

## Configuration

Copy and edit:

```bash
cp configs/camry.example.yaml /etc/pi-camry/camry.yaml
cp configs/environment.example /etc/pi-camry/environment
sudo chmod 600 /etc/pi-camry/environment
```

Key sections in `camry.yaml`:
- `vehicle`: year, make, model, engine, maintenance intervals
- `obd`: port, protocol, PIDs to poll
- `camera`: resolution, buffer duration, encryption
- `gps`: port, baud, geofence zones
- `gpio`: relay pins, input pins, ADC channels
- `audio`: sample rate, wake word, LLM endpoint
- `telegram`: bot token, authorized chat IDs
- `display`: resolution, touch, CAN bridge, SWC
- `modern`: CAN-FD, UDS, TPMS, radar, DMS settings
- `lte`: APN, NTRIP caster, MQTT broker
- `rain`: thresholds, wiper pins
- `ultrasonic`: sensor count, pins, alert distances
- `rtk`: port, NTRIP credentials
- `thermal`: model, overlay alpha, hotspot threshold

---

## Test Suite

```bash
# Run all tests
python -m pytest tests/ -v

# Run specific module
python -m pytest tests/test_new_sensors.py -v
python -m pytest tests/test_modern_vehicle.py -v
python -m pytest tests/test_display.py -v
```

| Module | Tests | Status |
|--------|-------|--------|
| Core | 12 | ✅ All pass |
| OBD | 10 | ✅ Mocked |
| GPS | 6 | ✅ Mocked |
| IMU | 7 | ✅ Mocked |
| GPIO | 10 | ✅ Mocked |
| Camera | 9 | ✅ Mocked |
| Audio | 7 | ⚠️ 5 need tuning |
| Storage | 10 | ✅ All pass |
| Telegram | 8 | ✅ Mocked |
| Display | 11 | ✅ Mocked |
| Modern Vehicle | 13 | ✅ Mocked |
| New Sensors | 14 | ✅ All pass |
| **Total** | **117** | **~107 pass** |

---

## Systemd Service

```bash
# Install auto-start
sudo cp scripts/camry-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable camry-daemon
sudo systemctl start camry-daemon

# Check status
sudo systemctl status camry-daemon
sudo journalctl -u camry-daemon -f
```

---

## Safety & Legal

⚠️ **This system is for diagnostics, logging, and driver assistance only.**

- **No physical control** of brakes, steering, or throttle on 1996 Camry
- **Relay controls** (wipers, HVAC, lights) are advisory — driver override always works
- **OBD-II is read-only** — no ECU writes without explicit unlock
- **Dashcam encryption** uses AES-256 — keys in `/etc/pi-camry/environment`
- **DMS camera** is IR + local processing — no video leaves the Pi
- **Check local laws** for dashcam, phone use, and OBD-II modifications

---

## Roadmap

| Priority | Feature | Module | Est. Cost |
|----------|---------|--------|-----------|
| 1 | 4G LTE HAT | `connectivity/lte.py` | $60 |
| 2 | RTK GPS | `sensors/rtk_gps.py` | $200 |
| 3 | Direct TPMS | `modern_vehicle/tpms_direct.py` | $30 |
| 4 | Interior air quality | `modern_vehicle/interior_sensing.py` | $65 |
| 5 | Aftermarket radar | `modern_vehicle/radar_fusion.py` | $150 |
| 6 | Thermal camera | `sensors/thermal_camera.py` | $60-200 |
| 7 | DMS IR camera | `modern_vehicle/driver_monitor.py` | $40 |
| 8 | Rain sensor | `sensors/rain_sensor.py` | $10 |
| 9 | Ultrasonic parking | `sensors/ultrasonic_array.py` | $25 |
| 10 | Mobileye 6 (AEB) | External | $1000 |

---

## Documentation

- [`docs/SHOPPING_LIST.md`](docs/SHOPPING_LIST.md) — Complete parts list with vendors, prices, wiring
- [`guide.pdf`](guide.pdf) — 47-page integration reference (150 integrations, wiring diagrams, GPIO pinout)
- [`configs/camry.example.yaml`](configs/camry.example.yaml) — Full configuration template
- [`scripts/bench_test.py`](scripts/bench_test.py) — Hardware verification before install

---

## License

MIT — See [LICENSE](LICENSE) (create one if needed)

---

## Author

Walker — Hermes Agent builder.

GitHub: [@drwjkirkpatrick-web](https://github.com/drwjkirkpatrick-web)
