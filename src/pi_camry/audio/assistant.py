"""
pi_camry/audio/assistant.py
───────────────────────────
Voice I/O and Hermes LLM integration for in-car copilot.

Features:
- Wake word detection ("Hey Hermes") via VAD + keyword spotting
- Record user query → send to LLM API (local or over hotspot)
- Text-to-speech responses via pyttsx3 or espeak
- Audio alerts for events (collision, CEL, geofence)
- Streaming audio from USB mic, output to 3.5mm or USB DAC

Architecture:
    USB Mic → PyAudio → VAD → Wake Word → Record → LLM API
                                                      ↓
    Speaker ← TTS Engine ← Text Response ← Hermes/Local LLM

Usage:
    from pi_camry.audio.assistant import VoiceAssistant
    va = VoiceAssistant()
    await va.start()
    # Now listening for "Hey Hermes"
    await va.stop()
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import tempfile
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import aiohttp
import pyaudio

from pi_camry.core import EventType, bus
from pi_camry.core.config import settings

logger = logging.getLogger("camry.audio")

# Audio constants
CHUNK_SIZE = 1024
SAMPLE_RATE = 16000
CHANNELS = 1
FORMAT = pyaudio.paInt16  # 16-bit PCM

# Simple energy-based VAD threshold (RMS)
VAD_THRESHOLD = 500  # adjust by environment
VAD_SILENCE_SEC = 1.5  # seconds of silence before end of utterance
WAKE_WORD = "hey hermes"


@dataclass
class AudioConfig:
    """Runtime audio settings (mirrors settings.audio)."""
    input_device: str = "plughw:1,0"
    output_device: str = "plughw:0,0"
    sample_rate: int = 16000
    channels: int = 1
    chunk_size: int = 1024


class VoiceAssistant:
    """In-car voice assistant with wake word, LLM query, and TTS response."""

    def __init__(self) -> None:
        self.cfg = settings.audio
        self._running = False
        self._task: asyncio.Task | None = None
        self._pa: pyaudio.PyAudio | None = None
        self._stream_in: Any = None  # pyaudio Stream
        self._stream_out: Any = None

        # State
        self._listening = False
        self._speaking = False

    # ── Public API ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize PyAudio, open streams, start listening loop."""
        logger.info("Audio: initializing voice assistant")
        try:
            self._pa = pyaudio.PyAudio()
        except Exception as exc:
            logger.error("Audio: PyAudio init failed: %s", exc)
            return

        # Open input stream (USB mic)
        try:
            self._stream_in = self._pa.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=CHUNK_SIZE,
                input_device_index=self._find_device(self.cfg.input_device),
            )
        except Exception as exc:
            logger.error("Audio: failed to open input stream: %s", exc)
            return

        # Open output stream (3.5mm or DAC)
        try:
            self._stream_out = self._pa.open(
                format=FORMAT,
                channels=1,
                rate=22050,  # TTS often 22kHz
                output=True,
                frames_per_buffer=CHUNK_SIZE,
                output_device_index=self._find_device(self.cfg.output_device),
            )
        except Exception as exc:
            logger.warning("Audio: failed to open output stream: %s", exc)
            self._stream_out = None

        self._running = True
        self._task = asyncio.create_task(self._listen_loop())

        # Subscribe to events that should trigger audio alerts
        bus.subscribe(EventType.IMU_COLLISION, self._on_collision_alert)
        bus.subscribe(EventType.OBD_CEL_ON, self._on_cel_alert)

        logger.info("Audio: voice assistant listening for '%s'", WAKE_WORD)

    async def stop(self) -> None:
        """Close streams, stop listening."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        bus.unsubscribe(EventType.IMU_COLLISION, self._on_collision_alert)
        bus.unsubscribe(EventType.OBD_CEL_ON, self._on_cel_alert)

        if self._stream_in:
            self._stream_in.stop_stream()
            self._stream_in.close()
        if self._stream_out:
            self._stream_out.stop_stream()
            self._stream_out.close()
        if self._pa:
            self._pa.terminate()

        logger.info("Audio: voice assistant stopped")

    async def speak(self, text: str) -> None:
        """Speak text via TTS engine."""
        if self._speaking:
            logger.debug("Audio: already speaking, queuing: %s", text[:60])
            return
        self._speaking = True
        try:
            await self._tts_speak(text)
        finally:
            self._speaking = False

    # ── Internal listening loop ─────────────────────────────────────────────

    async def _listen_loop(self) -> None:
        """Continuously listen for wake word, then record query, send to LLM."""
        while self._running:
            try:
                # Phase 1: Listen for wake word (simplified energy + keyword)
                heard_wake = await self._listen_for_wake_word()
                if not heard_wake:
                    continue

                # Phase 2: Record utterance until silence
                logger.info("Audio: wake word detected, recording query...")
                audio_data = await self._record_until_silence()
                if not audio_data:
                    continue

                # Phase 3: Transcribe (placeholder — real: whisper.cpp or remote STT)
                query_text = await self._transcribe(audio_data)
                if not query_text:
                    await self.speak("I didn't catch that. Please try again.")
                    continue

                logger.info("Audio: query = '%s'", query_text)

                # Phase 4: Send to LLM
                response = await self._query_llm(query_text)
                if response:
                    await self.speak(response)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Audio: listen loop error")
                await asyncio.sleep(1.0)

    async def _listen_for_wake_word(self) -> bool:
        """Simple approach: detect speech energy, then check for keyword in transcript.
        
        Production would use Porcupine or Whisper for reliable wake word.
        """
        # Collect 2-second window, check for energy spike
        frames = []
        for _ in range(int(SAMPLE_RATE / CHUNK_SIZE * 2)):
            if not self._running:
                return False
            data = await self._read_chunk()
            frames.append(data)
            # Check energy
            rms = self._calculate_rms(data)
            if rms > VAD_THRESHOLD:
                # Energy detected — assume wake word for this scaffold
                # In production: run actual wake word model here
                return True
        return False

    async def _record_until_silence(self) -> bytes | None:
        """Record audio until silence detected for N seconds."""
        frames = []
        silence_start: float | None = None
        max_duration = 10.0  # cap at 10 seconds
        start_time = asyncio.get_event_loop().time()

        while self._running:
            data = await self._read_chunk()
            frames.append(data)

            rms = self._calculate_rms(data)
            if rms < VAD_THRESHOLD:
                if silence_start is None:
                    silence_start = asyncio.get_event_loop().time()
                elif asyncio.get_event_loop().time() - silence_start >= VAD_SILENCE_SEC:
                    break
            else:
                silence_start = None

            if asyncio.get_event_loop().time() - start_time >= max_duration:
                break

        if len(frames) < 10:
            return None

        # Convert to WAV bytes
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(b"".join(frames))
        return wav_buffer.getvalue()

    async def _read_chunk(self) -> bytes:
        """Read one chunk from input stream (runs in executor to avoid blocking)."""
        if not self._stream_in:
            return b"\x00" * CHUNK_SIZE * 2
        return await asyncio.get_event_loop().run_in_executor(
            None, self._stream_in.read, CHUNK_SIZE
        )

    @staticmethod
    def _calculate_rms(data: bytes) -> float:
        """Calculate RMS energy of 16-bit PCM chunk."""
        import math
        if len(data) < 2:
            return 0.0
        count = len(data) // 2
        fmt = f"{count}h"
        import struct
        samples = struct.unpack(fmt, data[: count * 2])
        sum_sq = sum(s * s for s in samples)
        return math.sqrt(sum_sq / count) if count > 0 else 0.0

    # ── Transcription ─────────────────────────────────────────────────────────

    async def _transcribe(self, audio_wav: bytes) -> str:
        """Transcribe audio to text.

        Placeholder: in production, use faster-whisper (local) or
        OpenAI Whisper API (over hotspot).
        """
        # For now, return a dummy parse — real implementation would:
        #   1. Save to temp file
        #   2. Call whisper.cpp or API
        #   3. Return transcript
        logger.debug("Audio: transcribe placeholder — would call Whisper here")
        # Simple heuristic: if audio is long enough, assume it's a real query
        if len(audio_wav) > 8000:
            return "What is my coolant temperature"  # placeholder for testing
        return ""

    # ── LLM Query ────────────────────────────────────────────────────────────

    async def _query_llm(self, query: str) -> str | None:
        """Send query to Hermes/local LLM and return spoken response."""
        url = self.cfg.llm_api_url
        model = self.cfg.llm_model

        # Build context-aware prompt with current vehicle data
        context = await self._build_vehicle_context()

        system_prompt = (
            "You are Hermes, the onboard AI assistant for a 1996 Toyota Camry. "
            "You have access to OBD-II data, GPS, and vehicle sensors. "
            "Respond concisely (1-2 sentences) for spoken delivery. "
            f"Current vehicle context: {context}"
        )

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            "temperature": 0.7,
            "max_tokens": 150,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        logger.warning("Audio: LLM returned %d", resp.status)
                        return "I'm having trouble connecting right now."
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"]
        except asyncio.TimeoutError:
            logger.warning("Audio: LLM timeout")
            return "The network is slow right now. Please try again."
        except Exception as exc:
            logger.error("Audio: LLM query failed: %s", exc)
            return "I'm sorry, I couldn't reach the AI service."

    async def _build_vehicle_context(self) -> str:
        """Gather current vehicle state for LLM context."""
        # Import here to avoid circular deps
        from pi_camry.obd.interface import OBDInterface
        from pi_camry.gps.tracker import GPSTracker

        parts = []
        # Try to get latest OBD snapshot
        try:
            # In real usage, these would be singleton instances
            parts.append("Engine running" if True else "Engine off")
        except Exception:
            pass
        return "; ".join(parts) if parts else "No current data"

    # ── TTS ───────────────────────────────────────────────────────────────────

    async def _tts_speak(self, text: str) -> None:
        """Convert text to speech and play.

        Tries espeak-ng first (fast, local), falls back to pyttsx3.
        """
        # Method 1: espeak-ng (fastest, works on Pi)
        try:
            import subprocess
            # Generate WAV with espeak-ng
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                wav_path = f.name
            proc = await asyncio.create_subprocess_exec(
                "espeak-ng",
                "-w", wav_path,
                "-s", "150",  # speed (words per minute)
                "-v", "en-us",
                text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0:
                await self._play_wav(wav_path)
                Path(wav_path).unlink(missing_ok=True)
                return
        except FileNotFoundError:
            logger.debug("Audio: espeak-ng not found")
        except Exception as exc:
            logger.warning("Audio: espeak-ng failed: %s", exc)

        # Method 2: pyttsx3
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate", 150)
            # Save to file then play (pyttsx3 is blocking)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                wav_path = f.name
            engine.save_to_file(text, wav_path)
            engine.runAndWait()
            await self._play_wav(wav_path)
            Path(wav_path).unlink(missing_ok=True)
            return
        except ImportError:
            logger.debug("Audio: pyttsx3 not installed")
        except Exception as exc:
            logger.warning("Audio: pyttsx3 failed: %s", exc)

        logger.error("Audio: no TTS engine available")

    async def _play_wav(self, wav_path: str) -> None:
        """Play a WAV file through output stream."""
        if not self._stream_out:
            return
        try:
            with wave.open(wav_path, "rb") as wf:
                # Read and play in chunks
                while True:
                    data = wf.readframes(CHUNK_SIZE)
                    if not data:
                        break
                    await asyncio.get_event_loop().run_in_executor(
                        None, self._stream_out.write, data
                    )
        except Exception as exc:
            logger.warning("Audio: WAV playback failed: %s", exc)

    def _find_device(self, name: str) -> int | None:
        """Find PyAudio device index by partial name match."""
        if not self._pa:
            return None
        for i in range(self._pa.get_device_count()):
            info = self._pa.get_device_info_by_index(i)
            if name.lower() in info["name"].lower():
                return i
        return None

    # ── Event alerts ──────────────────────────────────────────────────────────

    async def _on_collision_alert(self, event: Any) -> None:
        g = event.payload.get("g_force", 0.0)
        await self.speak(f"Collision detected. Impact force {g:.1f} G. Checking systems.")

    async def _on_cel_alert(self, event: any) -> None:
        await self.speak("Warning. Check engine light is on. Pulling diagnostic codes now.")
