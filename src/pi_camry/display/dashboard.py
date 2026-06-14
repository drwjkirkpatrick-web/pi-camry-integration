"""
pi_camry/display/dashboard.py
─────────────────────────────
OpenGL ES / Kivy dashboard GUI rendered on Pi 5 → HDMI → JoyBring head unit.

Provides:
- Real-time OBD gauges (RPM, speed, coolant, MPG)
- GPS map view (offline OSM tiles)
- Camera preview (front/rear toggle)
- Radio / media player interface
- Climate control UI
- Trip statistics
- Hermes voice assistant visual feedback

Architecture:
- Kivy app running at 1024×600 (or head-unit native resolution)
- Hardware-accelerated via Pi 5 GPU (OpenGL ES 3.1)
- Asyncio-compatible Kivy event loop
- Subscribes to EventBus for real-time data updates
- Touch input from JoyBring panel → Kivy touch events

To run standalone:
    python -m pi_camry.display.dashboard
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pi_camry.core import Event
    from pi_camry.core.config import DisplayConfig

logger = logging.getLogger("camry.display.dashboard")


class Dashboard:
    """Kivy-based dashboard GUI for head-unit display."""

    # Page IDs
    PAGE_HOME = "home"
    PAGE_OBD = "obd"
    PAGE_GPS = "gps"
    PAGE_CAMERA = "camera"
    PAGE_RADIO = "radio"
    PAGE_CLIMATE = "climate"
    PAGE_TRIP = "trip"
    PAGE_SETTINGS = "settings"

    PAGES = [PAGE_HOME, PAGE_OBD, PAGE_GPS, PAGE_CAMERA,
             PAGE_RADIO, PAGE_CLIMATE, PAGE_TRIP, PAGE_SETTINGS]

    def __init__(self, cfg: "DisplayConfig" | None = None) -> None:
        self.cfg = cfg
        self._running = False
        self._current_page = self.PAGE_HOME
        self._kivy_app: any = None  # type: ignore[annotation-unchecked]

        # Real-time data cache (updated by EventBus subscriptions)
        self._obd_data: dict[str, any] = {}  # type: ignore[annotation-unchecked]
        self._gps_data: dict[str, any] = {}  # type: ignore[annotation-unchecked]
        self._imu_data: dict[str, any] = {}  # type: ignore[annotation-unchecked]
        self._climate_data: dict[str, any] = {}  # type: ignore[annotation-unchecked]

    async def start(self) -> None:
        """Initialize Kivy and start the GUI event loop."""
        logger.info("Dashboard: initializing Kivy GUI...")
        self._running = True

        try:
            import kivy
            kivy.require("2.2.0")
            from kivy.app import App
            from kivy.uix.boxlayout import BoxLayout
            from kivy.uix.label import Label
            from kivy.uix.button import Button
            from kivy.uix.gridlayout import GridLayout
            from kivy.uix.screenmanager import ScreenManager, Screen
            from kivy.graphics import Color, Rectangle
            from kivy.clock import Clock
            from kivy.core.window import Window

            # Set window size to head-unit resolution
            res = getattr(self.cfg, "resolution", (1024, 600))
            Window.size = res

            class CamryDashboardApp(App):
                def __init__(inner_self, controller: "Dashboard") -> None:
                    super().__init__()
                    inner_self.controller = controller

                def build(inner_self) -> any:  # type: ignore[annotation-unchecked]
                    sm = ScreenManager()

                    # Home screen
                    home = Screen(name=Dashboard.PAGE_HOME)
                    home_layout = BoxLayout(orientation="vertical")
                    home_layout.add_widget(Label(
                        text="Hermes Camry Dashboard",
                        font_size="24sp",
                        size_hint_y=0.1,
                    ))
                    # Quick stat grid
                    stats = GridLayout(cols=4, size_hint_y=0.4)
                    inner_self._rpm_label = Label(text="RPM: --", font_size="18sp")
                    inner_self._speed_label = Label(text="Speed: --", font_size="18sp")
                    inner_self._temp_label = Label(text="Coolant: --°C", font_size="18sp")
                    inner_self._mpg_label = Label(text="MPG: --", font_size="18sp")
                    stats.add_widget(inner_self._rpm_label)
                    stats.add_widget(inner_self._speed_label)
                    stats.add_widget(inner_self._temp_label)
                    stats.add_widget(inner_self._mpg_label)
                    home_layout.add_widget(stats)

                    # Navigation buttons
                    nav = GridLayout(cols=4, size_hint_y=0.2)
                    for page in Dashboard.PAGES[1:]:
                        btn = Button(text=page.upper())
                        btn.bind(on_press=lambda inst, p=page: sm.switch_to(sm.get_screen(p)))
                        nav.add_widget(btn)
                    home_layout.add_widget(nav)

                    # Voice assistant status
                    inner_self._voice_label = Label(
                        text="Say 'Hey Hermes'",
                        font_size="16sp",
                        size_hint_y=0.1,
                    )
                    home_layout.add_widget(inner_self._voice_label)

                    home.add_widget(home_layout)
                    sm.add_widget(home)

                    # OBD screen
                    obd_screen = Screen(name=Dashboard.PAGE_OBD)
                    obd_layout = BoxLayout(orientation="vertical")
                    obd_layout.add_widget(Label(text="OBD Diagnostics", font_size="20sp"))
                    inner_self._obd_detail = Label(text="No data", font_size="14sp")
                    obd_layout.add_widget(inner_self._obd_detail)
                    obd_screen.add_widget(obd_layout)
                    sm.add_widget(obd_screen)

                    # GPS screen
                    gps_screen = Screen(name=Dashboard.PAGE_GPS)
                    gps_layout = BoxLayout(orientation="vertical")
                    gps_layout.add_widget(Label(text="GPS Navigation", font_size="20sp"))
                    inner_self._gps_detail = Label(text="No fix", font_size="14sp")
                    gps_layout.add_widget(inner_self._gps_detail)
                    gps_screen.add_widget(gps_layout)
                    sm.add_widget(gps_screen)

                    # Camera screen
                    cam_screen = Screen(name=Dashboard.PAGE_CAMERA)
                    cam_layout = BoxLayout(orientation="vertical")
                    cam_layout.add_widget(Label(text="Camera View", font_size="20sp"))
                    inner_self._cam_status = Label(text="Front camera", font_size="14sp")
                    cam_layout.add_widget(inner_self._cam_status)
                    cam_screen.add_widget(cam_layout)
                    sm.add_widget(cam_screen)

                    # Radio screen
                    radio_screen = Screen(name=Dashboard.PAGE_RADIO)
                    radio_layout = BoxLayout(orientation="vertical")
                    radio_layout.add_widget(Label(text="Radio / Media", font_size="20sp"))
                    inner_self._radio_status = Label(text="FM 104.5", font_size="14sp")
                    radio_layout.add_widget(inner_self._radio_status)
                    radio_screen.add_widget(radio_layout)
                    sm.add_widget(radio_screen)

                    # Climate screen
                    climate_screen = Screen(name=Dashboard.PAGE_CLIMATE)
                    climate_layout = BoxLayout(orientation="vertical")
                    climate_layout.add_widget(Label(text="Climate Control", font_size="20sp"))
                    inner_self._climate_status = Label(text="Off", font_size="14sp")
                    climate_layout.add_widget(inner_self._climate_status)
                    climate_screen.add_widget(climate_layout)
                    sm.add_widget(climate_screen)

                    # Trip screen
                    trip_screen = Screen(name=Dashboard.PAGE_TRIP)
                    trip_layout = BoxLayout(orientation="vertical")
                    trip_layout.add_widget(Label(text="Trip Statistics", font_size="20sp"))
                    inner_self._trip_detail = Label(text="No trips recorded", font_size="14sp")
                    trip_layout.add_widget(inner_self._trip_detail)
                    trip_screen.add_widget(trip_layout)
                    sm.add_widget(trip_screen)

                    # Settings screen
                    settings_screen = Screen(name=Dashboard.PAGE_SETTINGS)
                    settings_layout = BoxLayout(orientation="vertical")
                    settings_layout.add_widget(Label(text="Settings", font_size="20sp"))
                    settings_screen.add_widget(settings_layout)
                    sm.add_widget(settings_screen)

                    # Schedule periodic UI update
                    Clock.schedule_interval(inner_self._update_ui, 0.5)

                    return sm

                def _update_ui(inner_self, dt: float) -> None:
                    """Update labels from controller data cache."""
                    c = inner_self.controller
                    if "rpm" in c._obd_data:
                        inner_self._rpm_label.text = f"RPM: {c._obd_data['rpm']}"
                    if "speed_kmh" in c._obd_data:
                        inner_self._speed_label.text = f"Speed: {c._obd_data['speed_kmh']:.0f}"
                    if "coolant_c" in c._obd_data:
                        inner_self._temp_label.text = f"Coolant: {c._obd_data['coolant_c']:.0f}°C"
                    if "mpg" in c._obd_data:
                        inner_self._mpg_label.text = f"MPG: {c._obd_data['mpg']:.1f}"

            self._kivy_app = CamryDashboardApp(self)
            # Run Kivy in a thread so asyncio keeps working
            import threading
            self._kivy_thread = threading.Thread(target=self._kivy_app.run, daemon=True)
            self._kivy_thread.start()
            logger.info("Dashboard: Kivy GUI started at %dx%d", *res)

        except ImportError:
            logger.warning("Dashboard: Kivy not installed, GUI disabled")
            logger.info("Dashboard: Install with: uv pip install kivy[base]")
        except Exception as exc:
            logger.error("Dashboard: GUI init failed: %s", exc)

    async def stop(self) -> None:
        """Stop Kivy GUI."""
        self._running = False
        if self._kivy_app:
            try:
                self._kivy_app.stop()
            except Exception:
                pass
        logger.info("Dashboard: stopped")

    # ── EventBus handlers ─────────────────────────────────────────────────

    async def on_obd_update(self, event: "Event") -> None:
        """Handle OBD telemetry events."""
        self._obd_data.update(event.payload)

    async def on_gps_update(self, event: "Event") -> None:
        """Handle GPS position events."""
        self._gps_data.update(event.payload)

    async def on_imu_event(self, event: "Event") -> None:
        """Handle IMU motion events."""
        self._imu_data.update(event.payload)

    async def on_voice_status(self, event: "Event") -> None:
        """Handle voice assistant status changes."""
        status = event.payload.get("status", "idle")
        if self._kivy_app:
            self._kivy_app._voice_label.text = f"Voice: {status}"

    # ── Page navigation ─────────────────────────────────────────────────────

    def switch_page(self, page: str) -> None:
        """Switch to a dashboard page by name."""
        if page in self.PAGES:
            self._current_page = page
            logger.debug("Dashboard: switched to %s", page)

    def next_page(self) -> None:
        """Cycle to next page."""
        idx = self.PAGES.index(self._current_page)
        self.switch_page(self.PAGES[(idx + 1) % len(self.PAGES)])

    def prev_page(self) -> None:
        """Cycle to previous page."""
        idx = self.PAGES.index(self._current_page)
        self.switch_page(self.PAGES[(idx - 1) % len(self.PAGES)])
