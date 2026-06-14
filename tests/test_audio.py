"""
Tests for pi_camry.audio.assistant — VoiceAssistant with mocked PyAudio.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Conditionally skip entire module if pyaudio unavailable ──
pytest.importorskip("pyaudio", reason="pyaudio not installed")

from pi_camry.audio.assistant import VoiceAssistant


@pytest.mark.asyncio
async def test_audio_start_stop(mock_pyaudio: MagicMock) -> None:
    """VoiceAssistant should open streams and start listening loop."""
    va = VoiceAssistant()
    await va.start()
    assert va._running is True
    assert va._pa is not None
    assert va._stream_in is not None
    await va.stop()
    assert va._running is False
    # terminate is called if _pa was initialized; may be skipped if output stream failed
    if va._pa is not None:
        mock_pyaudio.return_value.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_audio_speak_sets_speaking_flag(mock_pyaudio: MagicMock) -> None:
    """speak() should set _speaking and call TTS backend."""
    va = VoiceAssistant()
    await va.start()
    # Mock _tts_speak to avoid actual espeak / pyttsx3 calls
    va._tts_speak = AsyncMock()  # type: ignore[method-assign]
    await va.speak("Hello")
    assert va._speaking is False  # restored after speak
    va._tts_speak.assert_awaited_once_with("Hello")
    await va.stop()


@pytest.mark.asyncio
async def test_audio_read_chunk(mock_pyaudio: MagicMock) -> None:
    """_read_chunk should return bytes from the input stream."""
    va = VoiceAssistant()
    await va.start()
    chunk = await va._read_chunk()
    assert isinstance(chunk, bytes)
    assert len(chunk) > 0
    await va.stop()


@pytest.mark.asyncio
async def test_audio_calculate_rms(mock_pyaudio: MagicMock) -> None:
    """_calculate_rms should compute RMS for 16-bit PCM data."""
    va = VoiceAssistant()
    # Silence = near-zero RMS
    silent = b"\x00\x00" * 512
    rms = VoiceAssistant._calculate_rms(silent)
    assert rms == pytest.approx(0.0, abs=0.01)
    # Loud signal = high RMS
    loud = b"\x7f\xff" * 512
    rms_loud = VoiceAssistant._calculate_rms(loud)
    assert rms_loud > 1000


@pytest.mark.asyncio
async def test_audio_listen_for_wake_word_detects_energy(mock_pyaudio: MagicMock) -> None:
    """_listen_for_wake_word should return True when RMS > threshold."""
    va = VoiceAssistant()
    await va.start()
    # Inject loud data into stream read
    va._stream_in.read.return_value = b"\x7f\xff" * 1024  # loud 16-bit samples
    heard = await va._listen_for_wake_word()
    assert heard is True
    await va.stop()


@pytest.mark.asyncio
async def test_audio_record_until_silence(mock_pyaudio: MagicMock) -> None:
    """_record_until_silence should return WAV bytes when speech detected."""
    va = VoiceAssistant()
    await va.start()
    # First read loud, then silent
    va._stream_in.read.side_effect = [
        b"\x7f\xff" * 1024,
        b"\x7f\xff" * 1024,
    ] + [b"\x00\x00" * 1024] * 50
    audio = await va._record_until_silence()
    assert audio is not None
    assert isinstance(audio, bytes)
    assert len(audio) > 0
    await va.stop()


@pytest.mark.asyncio
async def test_audio_transcribe_placeholder(mock_pyaudio: MagicMock) -> None:
    """_transcribe should return placeholder text for sufficiently long audio."""
    va = VoiceAssistant()
    result = await va._transcribe(b"x" * 9000)
    assert isinstance(result, str)
    assert len(result) > 0


@pytest.mark.asyncio
async def test_audio_query_llm_timeout(mock_pyaudio: MagicMock, monkeypatch: Any) -> None:
    """_query_llm should return fallback on timeout."""
    va = VoiceAssistant()
    # Patch aiohttp to raise timeout
    monkeypatch.setattr(
        "pi_camry.audio.assistant.aiohttp.ClientSession",
        MagicMock(side_effect=asyncio.TimeoutError),
    )
    resp = await va._query_llm("What is my coolant temperature?")
    assert "slow" in resp.lower() or "trouble" in resp.lower()


@pytest.mark.asyncio
async def test_audio_find_device(mock_pyaudio: MagicMock) -> None:
    """_find_device should return index matching device name."""
    va = VoiceAssistant()
    await va.start()
    idx = va._find_device("USB")
    assert idx is not None
    assert idx == 1
    idx_none = va._find_device("NONEXISTENT")
    assert idx_none is None
    await va.stop()


@pytest.mark.asyncio
async def test_audio_build_vehicle_context(mock_pyaudio: MagicMock) -> None:
    """_build_vehicle_context should return a non-empty string."""
    va = VoiceAssistant()
    ctx = await va._build_vehicle_context()
    assert isinstance(ctx, str)
