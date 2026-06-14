# Pi-Camry Integration — Recommended Module Shopping List

## 1. 4G/LTE Connectivity (Always-On)

| Part | Supplier | Price | Interface | Notes |
|------|----------|-------|-----------|-------|
| **Quectel EC25-AUX** | SixFab / Waveshare | $60-80 | USB 2.0 / mini-PCIe | Best for Pi: USB mode, AT commands, GPS built-in |
| **SixFab 4G/LTE HAT** | sixfab.com | $45 | HAT (GPIO + USB) | Has active antenna, SIM slot, status LEDs |
| **SIM7600G-H HAT** | Waveshare / Amazon | $55 | USB / UART | Global bands, GNSS included, cheaper than EC25 |
| **Active GPS/Cell antenna** | Taoglas / SixFab | $15 | SMA | Required for good signal; passive won't work in car |
| **SIM card** | Hologram / 1NCE / Twilio | $3-5/mo | — | IoT data plans: 500MB-2GB/mo sufficient |

**Wiring:**
- USB HAT → Pi USB 3.0 port (or USB-C OTG with hub)
- SMA antenna → roof mount or rear window (magnetic base)
- Power: HAT draws 2-3W peak; ensure 12V→5V buck can supply 3A

**AT Commands to know:**
```
AT+CGDCONT=1,"IP","hologram"    # Set APN
AT+NETOPEN                      # Open network
AT+CIPOPEN=0,"TCP","api.telegram.org",443
```

---

## 2. Direct TPMS (315/433 MHz RF)

| Part | Supplier | Price | Interface | Notes |
|------|----------|-------|-----------|-------|
| **RTL-SDR v3** (R820T2) | RTL-SDR Blog / Amazon | $25-35 | USB 2.0 | 24-1766 MHz, covers 315/433 TPMS |
| **433 MHz antenna** | RTL-SDR Blog | $10 | SMA | ~17cm whip for 433 MHz; use for EU/JP |
| **315 MHz antenna** | Amazon / eBay | $10 | SMA | ~24cm whip for US TPMS |
| **TPMS activation tool** | VXDAS / JDiag | $20-40 | — | Triggers sensors to broadcast on demand |
| **Alternative: CC1101 module** | Waveshare / Amazon | $12 | SPI (GPIO) | Lower level, programmable freq, no SDR overhead |

**Wiring:**
- RTL-SDR → Pi USB 2.0 port
- Antenna → mounted near wheel wells (RF penetrates plastic, not metal)
- Optional: CC1101 → SPI0 (GPIO 9-11) + GPIO 8 (CE0)

**Software:**
- `rtl_433` (C, well-maintained) decodes 200+ RF protocols including TPMS
- Our Python wrapper reads `rtl_433` JSON output via pipe

---

## 3. Interior Air Quality (I2C)

| Part | Supplier | Price | Interface | Notes |
|------|----------|-------|-----------|-------|
| **Sensirion SCD40** (CO2 + RH + T) | Digi-Key / Mouser | $45-50 | I2C | Photoacoustic CO2, very accurate, small |
| **Sensirion SGP40** (VOC) | Digi-Key / Adafruit | $15 | I2C | VOC index 0-500, compensates with RH/T |
| **Sensirion SPS30** (PM) | Digi-Key / Mouser | $45 | I2C/UART | PM1.0/2.5/4.0/10, laser scattering |
| **Bosch BME680** (T/RH/P/VOC) | Adafruit / Pimoroni | $20 | I2C | Gas resistance ≈ VOC proxy, BSEC algorithm available |
| **Qwiic/Stemma QT cables** | Adafruit / SparkFun | $5 | JST-SH 4-pin | Chain all sensors on one I2C bus |

**Wiring:**
- All sensors share Pi I2C bus (GPIO 2=SDA, 3=SCL)
- SCD40 and SGP40 have different I2C addresses (0x62, 0x59) — no conflict
- SPS30 needs 5V (can use Pi 5V pin, draws ~60mA)
- Place sensors: one near HVAC intake (outside air), one center console (cabin)

**I2C Addresses:**
| Sensor | Address | Check with `i2cdetect -y 1` |
|--------|---------|------------------------------|
| SCD40 | 0x62 | Should appear after init |
| SGP40 | 0x59 | Should appear after init |
| SPS30 | 0x69 | Should appear after init |
| BME680 | 0x76 or 0x77 | Depends on SDO pin |

---

## 4. Rain Sensor (IR Reflectance)

| Part | Supplier | Price | Interface | Notes |
|------|----------|-------|-----------|-------|
| **Optical rain sensor module** | Amazon / AliExpress | $8-15 | Analog / Digital | IR LED + photodiode, detects water on glass |
| **HY-SRF05** (ultrasonic, repurposed) | Amazon | $5 | GPIO | Can detect water droplets on surface |
| **Capacitive rain sensor** | SparkFun | $12 | Analog | Detects water via capacitance change |
| **Aftermarket rain sensor** (e.g., Hella) | eBay | $40-80 | Analog 0-5V | OEM-grade, often from donor cars |

**Wiring:**
- Analog module → MCP3008 ADC (SPI) → Pi GPIO
- Digital module → direct GPIO input
- Mount: inside windshield, behind rearview mirror (where OEM would put it)
- Sensitivity: dry glass = high IR reflectance; water = scattered IR = low signal

**Calibration:**
- Dry threshold: >800 ADC counts
- Light rain: 600-800
- Heavy rain: <400
- Hysteresis: turn wipers on at 600, off at 700 (prevents chatter)

---

## 5. Ultrasonic Parking Array (8×)

| Part | Supplier | Price | Interface | Notes |
|------|----------|-------|-----------|-------|
| **HC-SR04** × 8 | Amazon / AliExpress | $2-3 each | GPIO (trig/echo) | Cheap, 2-400cm range, 15° beam |
| **US-100** (temp compensated) | Amazon | $4 each | GPIO / UART | More accurate than HC-SR04 |
| **JSN-SR04T** (waterproof) | Amazon | $6 each | GPIO | For external mounting, IP67 |
| **8-channel GPIO expander** (MCP23017) | Adafruit | $5 | I2C | If Pi GPIO pins run out |
| **level shifter** (3.3V↔5V) | Adafruit | $3 | — | HC-SR04 is 5V logic; Pi GPIO is 3.3V |

**Wiring:**
- Front: 4 sensors (2 bumper corners + 2 mid-bumper)
- Rear: 4 sensors (2 bumper corners + 2 mid-bumper)
- Each needs 1 trig (output) + 1 echo (input) GPIO
- HC-SR04 VCC = 5V (from Pi 5V pin), but echo must be level-shifted to 3.3V
- JSN-SR04T: single transducer, waterproof, better for exposed mounting

**Timing:**
- 8 sensors × 30ms each = 240ms cycle → ~4 Hz update rate
- Stagger triggers to avoid crosstalk (echo from one sensor heard by another)

---

## 6. RTK GPS (Centimeter-Level)

| Part | Supplier | Price | Interface | Notes |
|------|----------|-------|-----------|-------|
| **u-blox ZED-F9P** module | SparkFun / ArduSimple | $200-250 | UART / I2C / SPI | Multi-band L1/L2, RTK/PPK, raw carrier phase |
| **ANN-MB-00 antenna** | u-blox / Digi-Key | $50 | SMA | Multi-band active antenna, required for F9P |
| **RTK correction service** | PointOne / SwiftNav / free | $30-100/mo | NTRIP (TCP/IP) | Or free: RTK2go, NY CORS, your state's CORS network |
| **Survey tripod / magnetic mount** | Amazon | $20 | — | Antenna must have clear sky view; roof mount ideal |

**Wiring:**
- ZED-F9P UART → Pi UART0 (GPIO 14/15) or USB-to-UART adapter
- NTRIP corrections: via LTE or WiFi hotspot
- ANN-MB-00 → roof mount (magnetic base on trunk lid)

**Accuracy:**
- Standalone: 1.5m → RTK fixed: 2cm horizontal, 5cm vertical
- Time to fix: 10-60 seconds with good sky view

---

## 7. DMS IR Camera (Already partially built)

| Part | Supplier | Price | Interface | Notes |
|------|----------|-------|-----------|-------|
| **Raspberry Pi NoIR Camera V2** | PiShop / Adafruit | $25 | CSI | No IR filter, sees 940nm IR |
| **940nm IR LED array** (48 LEDs) | Amazon | $12 | 5V/GPIO | Illuminates driver face in darkness |
| **850nm IR LED array** | Amazon | $12 | 5V/GPIO | Brighter eye reflection (red-eye effect) |
| **IR pass filter** (850nm or 940nm) | Amazon / eBay | $8 | Screw-on | Blocks visible light, improves SNR |
| **USB IR camera** (e.g., ELP-USB) | Amazon | $30 | USB | Alternative to Pi CSI; easier cable routing |

**Wiring:**
- NoIR Camera → Pi CSI0 (or CSI1 for dual)
- IR LEDs → 5V with MOSFET or relay (PWM dimmable for comfort)
- Mount: top of steering column, facing driver, ~60cm from face
- Power: LEDs draw ~200mA; use GPIO-controlled MOSFET to turn on only when dark

**Privacy note:** All processing local. No images stored unless crash detected.

---

## 8. Thermal Camera (Night Vision)

| Part | Supplier | Price | Interface | Notes |
|------|----------|-------|-----------|-------|
| **FLIR Lepton 3.5** (160×120) | GroupGets / SparkFun | $200 | SPI (GPIO) | Radiometric, 14-bit, 8.7Hz, 51° FOV |
| **FLIR Lepton breakout board** | SparkFun | $30 | SPI + I2C | Level shifter, socket, easy wiring |
| **Seek Thermal Compact** | Amazon | $250 | USB-C | 206×156, higher res than Lepton, plug-and-play |
| **MLX90640** (32×24, cheaper) | Pimoroni / Adafruit | $60 | I2C | 110° FOV, 64 fps, enough for hot-spot detection |

**Wiring:**
- Lepton 3.5 → SPI0 (GPIO 9-11) + I2C for telemetry
- Seek → USB-C port on Pi 5
- MLX90640 → I2C bus (address 0x33)
- Mount: front grille or behind windshield (glass blocks LWIR; grille is better)

**Fusion:**
- Overlay thermal hotspots on Pi Camera 3 feed (OpenCV alpha blending)
- Detect pedestrians/cyclists/animals at 2-3× visible camera range in darkness

---

## 9. 12V Power Distribution (Required for all modules)

| Part | Supplier | Price | Notes |
|------|----------|-------|-------|
| **12V→5V/5A buck converter** (LM2596S) | Amazon | $8 | Powers Pi 5 + peripherals; 5A = 25W headroom |
| **12V→5V/3A USB-C PD trigger** | Amazon | $12 | Clean power, no wiring needed; plug into Pi 5 USB-C |
| **Fuse tap** (Add-a-circuit) | AutoZone / Amazon | $5 | Tap into existing fuse box (IGN-switched circuit) |
| ** Relay board (8-channel, 5V)** | Amazon | $10 | For switched 12V accessories |
| **Anderson Powerpole connectors** | Powerwerx | $15 | Standardized 12V connectors, easy to crimp |
| **Wire: 14 AWG primary, 18 AWG signal** | AutoZone | $20 | 14 AWG for 12V power, 18 AWG for relay control |

**Fuse box mapping (1996 Camry):**
| Circuit | Fuse | Tap for |
|---------|------|---------|
| IGN-switched | 15A | Pi 5 power, relays |
| Always-on | 10A | RTC backup, alarm standby |
| Cigarette lighter | 15A | USB accessories |
| Headlights | 15A | Auto-headlight relay |
| HVAC blower | 20A | Smart HVAC control |

---

## Total Cost Estimate

| Category | Budget | Premium | Notes |
|----------|--------|---------|-------|
| LTE connectivity | $75 | $95 | SIM7600 HAT + antenna + SIM |
| Direct TPMS | $35 | $55 | RTL-SDR + antennas |
| Interior air quality | $80 | $110 | SCD40 + SGP40 + SPS30 |
| Rain sensor | $10 | $50 | DIY module vs. OEM donor |
| Parking ultrasonics | $25 | $55 | 8× HC-SR04 + expander + shifter |
| RTK GPS | $250 | $300 | ZED-F9P + antenna + NTRIP |
| DMS IR camera | $40 | $80 | NoIR + LEDs + filter |
| Thermal camera | $60 | $250 | MLX90640 vs. FLIR Lepton |
| Power distribution | $40 | $70 | Buck + fuses + relays + wire |
| **TOTAL** | **$615** | **$1,065** | Excluding Mobileye/LIDAR/C-V2X |

---

## Where to Buy (Recommended Vendors)

| Vendor | Best For | Shipping |
|--------|----------|----------|
| **Digi-Key** / **Mouser** | SCD40, SGP40, SPS30, ZED-F9P | Fast, professional |
| **Adafruit** / **SparkFun** | Breakout boards, Qwiic cables, tutorials | US-based, great docs |
| **Waveshare** / **SixFab** | Pi HATs (LTE, GPS, CAN) | China, good quality |
| **Amazon** | RTL-SDR, HC-SR04, IR LEDs, buck converters | Fast, returnable |
| **AliExpress** | Cheapest clones (EC25, sensors) | Slow, variable quality |
| **GroupGets** | FLIR Lepton (group buys) | Specialty thermal |
| **eBay** | OEM donor parts (rain sensors, TPMS receivers) | Variable |

---

## Wiring Diagram Summary

```
12V Battery / Fuse Box
    │
    ├── Fuse Tap (IGN-switched) ──→ 12V→5V Buck ──→ Pi 5 USB-C (5V/3A)
    │                                      │
    │                                      ├── 5V rail ──→ Sensors (I2C bus)
    │                                      │              ├── SCD40 (0x62)
    │                                      │              ├── SGP40 (0x59)
    │                                      │              ├── SPS30 (0x69)
    │                                      │              └── BME680 (0x76)
    │                                      │
    │                                      ├── 5V rail ──→ RTL-SDR (USB)
    │                                      │
    │                                      ├── 5V rail ──→ LTE HAT (USB)
    │                                      │
    │                                      └── 5V rail ──→ Relay Board (GPIO)
    │                                                     ├── Headlight relay
    │                                                     ├── Cooling fan relay
    │                                                     └── HVAC recirc relay
    │
    ├── Always-on ──→ Pi RTC battery ──→ GPS backup
    │
    └── Cigarette lighter ──→ Optional accessories

Pi 5 GPIO / Interfaces
    ├── I2C (GPIO 2/3) ──→ SCD40, SGP40, SPS30, BME680, MCP23017
    ├── SPI0 (GPIO 9-11) ──→ MCP3008 (ADC), CC1101 (alt. TPMS)
    ├── UART0 (GPIO 14/15) ──→ ZED-F9P RTK GPS
    ├── CSI0 ──→ Pi Camera 3 (front dashcam)
    ├── CSI1 ──→ Pi NoIR Camera (DMS, IR illuminated)
    ├── USB 3.0 ──→ LTE HAT, RTL-SDR, Seek Thermal
    ├── USB 2.0 ──→ JoyBring touchscreen, OBD ELM327
    └── GPIO (various) ──→ 8× HC-SR04, rain sensor, relays, SWC
```
