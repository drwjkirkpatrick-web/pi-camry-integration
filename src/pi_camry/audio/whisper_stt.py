"""
pi_camry/audio/whisper_stt.py
─────────────────────────────
faster-whisper integration for wake word detection and transcription.

Replaces the placeholder _transcribe() in assistant.py.
Runs locally on Pi 5 CPU (Jetson Orin GPU if available).

Model: faster-whisper tiny or base (quantized int8 for speed)
Languages: English primary, auto-detect if needed

Usage (from assistant.py):
    from pi_camry.audio.whisper_stt import WhisperSTT
    stt = WhisperSTT(model_size="tiny")
    text = await stt.transcribe(audio_wav_bytes)
"""

from __future__ import annotations

import io
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

# faster-whisper is optional; gracefully degrade if unavailable
try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    WhisperModel = None  # type: ignore[misc,assignment]
    WHISPER_AVAILABLE = False

logger = logging.getLogger("camry.audio.whisper")


class WhisperSTT:
    """Local speech-to-text using faster-whisper."""

    # Model size → approximate memory, speed on Pi 5
    MODEL_PROFILES: dict[str, dict[str, any]] = {  # type: ignore[annotation-unchecked]
        "tiny": {"mem_mb": 75, "rtf": 0.3, "dir": "tiny.en"},
        "base": {"mem_mb": 150, "rtf": 0.5, "dir": "base.en"},
        "small": {"mem_mb": 500, "rtf": 1.0, "dir": "small.en"},
    }

    def __init__(
        self,
        model_size: str = "tiny",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str = "en",
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self._model: any = None  # type: ignore[annotation-unchecked]
        self._initialized = False

    # ── Public API ──────────────────────────────────────────────────────────

    async def load(self) -> bool:
        """Load the Whisper model. Returns True if successful."""
        if not WHISPER_AVAILABLE:
            logger.warning("WhisperSTT: faster-whisper not installed. "
                          "Install with: uv pip install faster-whisper")
            return False

        if self._initialized:
            return True

        profile = self.MODEL_PROFILES.get(self.model_size, self.MODEL_PROFILES["tiny"])
        logger.info(
            "WhisperSTT: loading model '%s' on %s (%s) — ~%d MB",
            self.model_size, self.device, self.compute_type, profile["mem_mb"],
        )

        try:
            self._model = WhisperModel(
                profile["dir"],
                device=self.device,
                compute_type=self.compute_type,
                download_root=str(Path.home() / ".cache" / "whisper"),
            )
            self._initialized = True
            logger.info("WhisperSTT: model loaded successfully")
            return True
        except Exception as exc:
            logger.error("WhisperSTT: failed to load model: %s", exc)
            return False

    async def transcribe(self, audio_wav: bytes) -> str:
        """Transcribe WAV audio bytes to text.

        Args:
            audio_wav: Raw WAV file bytes (PCM 16-bit, 16kHz mono)

        Returns:
            Transcribed text, or empty string on failure.
        """
        if not self._initialized or not self._model:
            logger.debug("WhisperSTT: model not loaded, returning empty")
            return ""

        # Write to temp file (faster-whisper needs a path)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio_wav)
            wav_path = f.name

        try:
            segments, info = self._model.transcribe(
                wav_path,
                language=self.language,
                task="transcribe",
                vad_filter=True,  # remove non-speech
                vad_parameters=dict(min_silence_duration_ms=500),
            )
            text_parts = [seg.text.strip() for seg in segments]
            result = " ".join(text_parts).strip()
            logger.debug("WhisperSTT: transcribed '%s...' (lang=%s, prob=%.2f)",
                         result[:40], info.language, info.language_probability)
            return result
        except Exception as exc:
            logger.error("WhisperSTT: transcription failed: %s", exc)
            return ""
        finally:
            Path(wav_path).unlink(missing_ok=True)

    async def transcribe_file(self, wav_path: str | Path) -> str:
        """Transcribe from a file path directly."""
        if not self._initialized or not self._model:
            return ""
        try:
            segments, info = self._model.transcribe(
                str(wav_path),
                language=self.language,
                task="transcribe",
                vad_filter=True,
            )
            return " ".join(seg.text.strip() for seg in segments).strip()
        except Exception as exc:
            logger.error("WhisperSTT: file transcription failed: %s", exc)
            return ""

    # ── Wake word detection ─────────────────────────────────────────────────

    async def detect_wake_word(self, audio_wav: bytes, wake_phrases: list[str] | None = None) -> bool:
        """Quick transcription to check if wake word is present.

        More efficient than full VAD + keyword spotting for simple cases.
        """
        if wake_phrases is None:
            wake_phrases = ["hey hermes", "hermes", "hey car"]

        text = await self.transcribe(audio_wav)
        text_lower = text.lower()
        for phrase in wake_phrases:
            if phrase in text_lower:
                logger.info("WhisperSTT: wake word detected: '%s'", phrase)
                return True
        return False

    def unload(self) -> None:
        """Free model memory."""
        if self._model:
            del self._model
            self._model = None
        self._initialized = False
        logger.info("WhisperSTT: model unloaded")
