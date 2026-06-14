"""
pi_camry/modern_vehicle/interior_sensing.py
───────────────────────────────────────────
Interior environment sensing: air quality, CO2, VOCs, particulates.

Modern vehicles monitor cabin air quality for:
- Automatic recirculation (pollution detected outside)
- CO2 buildup (drowsiness, especially with multiple passengers)
- VOC off-gassing (new car smell, adhesives, plastics)
- Particulate matter (PM2.5/PM10 from traffic/industry)
- Humidity (fogging prevention)
- Temperature stratification

Sensors for Pi 5 add-on:
- SCD40/SCD41 (Sensirion): CO2 + RH + T (I2C)
- SGP40 (Sensirion): VOC index (I2C)
- SPS30 (Sensirion): PM1.0, PM2.5, PM4.0, PM10 (I2C/UART)
- BME680 (Bosch): T + RH + pressure + VOC (I2C)
- CCS811 (ams): VOC + eCO2 (I2C, deprecated but common)

Placement:
- Dashboard intake (measures incoming air)
- Center console (measures cabin air)
- Near HVAC outlet (measures conditioned air)

Actions:
- Auto-recirculate when outside PM2.5 > 35 µg/m³
- Increase ventilation when CO2 > 1000 ppm
- Alert when VOC index > 300 (poor air quality)
- Defog prediction from humidity + temp differential
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pi_camry.core.config import ModernVehicleConfig

logger = logging.getLogger("camry.modern.interior")


@dataclass(frozen=True)
class AirQualityReading:
    """Comprehensive interior air quality snapshot."""
    co2_ppm: float
    voc_index: float       # 0-500, >250 = poor
    pm1_0_ug_m3: float
    pm2_5_ug_m3: float
    pm4_0_ug_m3: float
    pm10_ug_m3: float
    temperature_c: float
    humidity_percent: float
    pressure_hpa: float
    timestamp: float


class InteriorEnvironmentSensor:
    """Multi-sensor interior air quality monitor."""

    # WHO / EPA guidelines
    CO2_FRESH = 400          # Outdoor baseline
    CO2_ACCEPTABLE = 1000    # Indoor limit
    CO2_POOR = 2000          # Drowsiness threshold
    PM2_5_WHO_LIMIT = 15     # µg/m³ annual mean
    PM2_5_ALERT = 35         # µg/m³ 24-hour mean
    VOC_GOOD = 100
    VOC_POOR = 250
    VOC_HAZARDOUS = 400

    def __init__(self, cfg: "ModernVehicleConfig" | None = None) -> None:
        self.cfg = cfg
        self._running = False
        self._sensors: dict[str, any] = {}  # type: ignore[annotation-unchecked]
        self._latest: AirQualityReading | None = None

    async def start(self) -> None:
        """Initialize all air quality sensors."""
        logger.info("Interior: initializing air quality sensors...")
        self._running = True

        # Try each sensor, continue if unavailable
        await self._init_scd4x()
        await self._init_sgp40()
        await self._init_sps30()
        await self._init_bme680()

        if self._sensors:
            asyncio.create_task(self._poll_loop())
            logger.info("Interior: %d sensor(s) active", len(self._sensors))
        else:
            logger.warning("Interior: no sensors found, using mock data")
            asyncio.create_task(self._mock_loop())

    async def stop(self) -> None:
        """Shutdown sensors."""
        self._running = False
        for sensor in self._sensors.values():
            try:
                if hasattr(sensor, "stop"):
                    sensor.stop()
            except Exception:
                pass
        logger.info("Interior: stopped")

    # ── Sensor initialization ───────────────────────────────────────────────

    async def _init_scd4x(self) -> None:
        """Initialize Sensirion SCD40/SCD41 CO2 sensor."""
        try:
            import scd4x
            sensor = scd4x.SCD4x()
            sensor.start_periodic_measurement()
            self._sensors["scd4x"] = sensor
            logger.info("Interior: SCD4x CO2 sensor initialized")
        except ImportError:
            logger.debug("Interior: scd4x module not available")
        except Exception as exc:
            logger.warning("Interior: SCD4x init failed: %s", exc)

    async def _init_sgp40(self) -> None:
        """Initialize Sensirion SGP40 VOC sensor."""
        try:
            import sgp40
            sensor = sgp40.SGP40()
            self._sensors["sgp40"] = sensor
            logger.info("Interior: SGP40 VOC sensor initialized")
        except ImportError:
            logger.debug("Interior: sgp40 module not available")
        except Exception as exc:
            logger.warning("Interior: SGP40 init failed: %s", exc)

    async def _init_sps30(self) -> None:
        """Initialize Sensirion SPS30 particulate sensor."""
        try:
            import sps30
            sensor = sps30.SPS30()
            sensor.start_measurement()
            self._sensors["sps30"] = sensor
            logger.info("Interior: SPS30 particulate sensor initialized")
        except ImportError:
            logger.debug("Interior: sps30 module not available")
        except Exception as exc:
            logger.warning("Interior: SPS30 init failed: %s", exc)

    async def _init_bme680(self) -> None:
        """Initialize Bosch BME680 multi-sensor."""
        try:
            import bme680
            sensor = bme680.BME680()
            sensor.set_humidity_oversample(bme680.OS_2X)
            sensor.set_pressure_oversample(bme680.OS_4X)
            sensor.set_temperature_oversample(bme680.OS_8X)
            sensor.set_filter(bme680.FILTER_SIZE_3)
            sensor.set_gas_status(bme680.ENABLE_GAS_MEAS)
            self._sensors["bme680"] = sensor
            logger.info("Interior: BME680 initialized")
        except ImportError:
            logger.debug("Interior: bme680 module not available")
        except Exception as exc:
            logger.warning("Interior: BME680 init failed: %s", exc)

    # ── Polling loop ────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Read all sensors and publish air quality data."""
        while self._running:
            try:
                reading = await self._read_all_sensors()
                self._latest = reading
                await self._evaluate_and_act(reading)
                await self._publish(reading)
                await asyncio.sleep(5.0)  # 0.2 Hz is sufficient for air quality
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Interior: poll loop error")
                await asyncio.sleep(10.0)

    async def _mock_loop(self) -> None:
        """Generate mock air quality data for testing."""
        import random
        while self._running:
            reading = AirQualityReading(
                co2_ppm=400 + random.random() * 600,
                voc_index=50 + random.random() * 100,
                pm1_0_ug_m3=random.random() * 10,
                pm2_5_ug_m3=random.random() * 20,
                pm4_0_ug_m3=random.random() * 25,
                pm10_ug_m3=random.random() * 30,
                temperature_c=22 + random.random() * 5,
                humidity_percent=40 + random.random() * 20,
                pressure_hpa=1013.0,
                timestamp=asyncio.get_event_loop().time(),
            )
            self._latest = reading
            await self._publish(reading)
            await asyncio.sleep(5.0)

    async def _read_all_sensors(self) -> AirQualityReading:
        """Read all active sensors and merge into single reading."""
        co2 = 400.0
        voc = 0.0
        pm1 = pm2_5 = pm4 = pm10 = 0.0
        temp = 20.0
        rh = 50.0
        pressure = 1013.0

        if "scd4x" in self._sensors:
            try:
                scd = self._sensors["scd4x"]
                if scd.get_data_ready():
                    co2, temp, rh = scd.read_measurement()
            except Exception:
                pass

        if "sgp40" in self._sensors:
            try:
                sgp = self._sensors["sgp40"]
                voc = sgp.measure_raw(index=True)
            except Exception:
                pass

        if "sps30" in self._sensors:
            try:
                sps = self._sensors["sps30"]
                pm1, pm2_5, pm4, pm10 = sps.read_measurement()
            except Exception:
                pass

        if "bme680" in self._sensors:
            try:
                bme = self._sensors["bme680"]
                if bme.get_sensor_data():
                    temp = bme.data.temperature
                    rh = bme.data.humidity
                    pressure = bme.data.pressure
                    # Gas resistance can be mapped to VOC
            except Exception:
                pass

        return AirQualityReading(
            co2_ppm=co2,
            voc_index=voc,
            pm1_0_ug_m3=pm1,
            pm2_5_ug_m3=pm2_5,
            pm4_0_ug_m3=pm4,
            pm10_ug_m3=pm10,
            temperature_c=temp,
            humidity_percent=rh,
            pressure_hpa=pressure,
            timestamp=asyncio.get_event_loop().time(),
        )

    # ── Evaluation and actions ──────────────────────────────────────────────

    async def _evaluate_and_act(self, reading: AirQualityReading) -> None:
        """Evaluate air quality and trigger HVAC actions."""
        from pi_camry.core import EventType, bus

        # CO2 alert
        if reading.co2_ppm > self.CO2_POOR:
            await bus.publish(
                EventType.INTERIOR_AIR_QUALITY,
                {"alert": "CO2_HIGH", "co2_ppm": reading.co2_ppm, "action": "OPEN_WINDOWS"},
                source="interior",
            )
        elif reading.co2_ppm > self.CO2_ACCEPTABLE:
            await bus.publish(
                EventType.INTERIOR_AIR_QUALITY,
                {"alert": "CO2_ELEVATED", "co2_ppm": reading.co2_ppm, "action": "INCREASE_VENTILATION"},
                source="interior",
            )

        # PM2.5 alert
        if reading.pm2_5_ug_m3 > self.PM2_5_ALERT:
            await bus.publish(
                EventType.INTERIOR_AIR_QUALITY,
                {"alert": "PM25_HIGH", "pm2_5": reading.pm2_5_ug_m3, "action": "RECIRCULATE"},
                source="interior",
            )

        # VOC alert
        if reading.voc_index > self.VOC_HAZARDOUS:
            await bus.publish(
                EventType.INTERIOR_AIR_QUALITY,
                {"alert": "VOC_HAZARDOUS", "voc": reading.voc_index, "action": "MAX_VENTILATION"},
                source="interior",
            )
        elif reading.voc_index > self.VOC_POOR:
            await bus.publish(
                EventType.INTERIOR_AIR_QUALITY,
                {"alert": "VOC_POOR", "voc": reading.voc_index, "action": "INCREASE_VENTILATION"},
                source="interior",
            )

        # Fog prediction
        if reading.humidity_percent > 75 and reading.temperature_c < 18:
            await bus.publish(
                EventType.INTERIOR_AIR_QUALITY,
                {"alert": "FOG_RISK", "rh": reading.humidity_percent, "temp": reading.temperature_c},
                source="interior",
            )

    async def _publish(self, reading: AirQualityReading) -> None:
        """Publish air quality reading to EventBus."""
        from pi_camry.core import EventType, bus
        await bus.publish(
            EventType.INTERIOR_AIR_QUALITY,
            {
                "co2_ppm": reading.co2_ppm,
                "voc_index": reading.voc_index,
                "pm2_5": reading.pm2_5_ug_m3,
                "temperature_c": reading.temperature_c,
                "humidity_percent": reading.humidity_percent,
            },
            source="interior",
        )

    # ── Public API ──────────────────────────────────────────────────────────

    def get_latest(self) -> AirQualityReading | None:
        """Return latest air quality reading."""
        return self._latest

    def get_air_quality_score(self) -> int:
        """Return overall air quality score (0-100, higher=better)."""
        if not self._latest:
            return 0
        r = self._latest
        score = 100
        # Deduct for CO2
        if r.co2_ppm > 1000:
            score -= min(30, int((r.co2_ppm - 1000) / 50))
        # Deduct for PM2.5
        if r.pm2_5_ug_m3 > 15:
            score -= min(30, int((r.pm2_5_ug_m3 - 15) / 2))
        # Deduct for VOC
        if r.voc_index > 100:
            score -= min(20, int((r.voc_index - 100) / 10))
        return max(0, score)
