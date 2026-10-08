"""Tests of the TTS client (fake transport, no network)."""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
import wave
from pathlib import Path

from grafeno.telegram import tts


def _wav(frames: bytes, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)
    return buffer.getvalue()


class FakeHTTP:
    """Opener double: queues a response or raises, and records requests."""

    def __init__(self, response=b"", error: Exception | None = None):
        self.response = response
        self.error = error
        self.requests: list = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


def test_synthesize_ok():
    http = FakeHTTP(response=b"RIFF....WAVE")
    audio = tts.synthesize(
        url="https://api.groq.com/openai/v1/audio/speech",
        api_key="KEY",
        model="canopylabs/orpheus-v1-english",
        voice="troy",
        text="Hola",
        opener=http,
    )
    assert audio == b"RIFF....WAVE"
    request = http.requests[0]
    assert request.headers.get("Authorization") == "Bearer KEY"
    body = json.loads(request.data.decode())
    assert body == {
        "model": "canopylabs/orpheus-v1-english",
        "input": "Hola",
        "voice": "troy",
        "response_format": "wav",
    }


def test_input_is_truncated():
    http = FakeHTTP(response=b"A")
    long_text = "z" * (tts.MAX_TTS_INPUT + 500)
    tts.synthesize(
        url="https://u", api_key="K", model="m", voice="v", text=long_text, opener=http
    )
    inputs = [json.loads(r.data.decode())["input"] for r in http.requests]
    assert len(inputs) > 1
    assert all(len(item) <= tts.MAX_TTS_SEGMENT for item in inputs)
    assert sum(len(item) for item in inputs) == tts.MAX_TTS_INPUT


def test_not_configured_returns_none():
    assert tts.synthesize(url="", api_key="K", model="m", voice="v", text="x") is None
    assert tts.synthesize(url="https://u", api_key="K", model="m", voice="v", text="x") is None
    assert tts.synthesize(url="https://u", api_key="K", model="m", voice="v", text="  ") is None


def test_network_error_returns_none():
    http = FakeHTTP(error=urllib.error.URLError("down"))
    assert tts.synthesize(
        url="https://u", api_key="K", model="m", voice="v", text="x", opener=http
    ) is None


def test_http_error_reports_reason():
    body = json.dumps({"error": {"message": "model not found"}}).encode()
    exc = urllib.error.HTTPError("https://u", 404, "Not Found", None, io.BytesIO(body))
    http = FakeHTTP(error=exc)
    reasons: list[str] = []
    audio = tts.synthesize(
        url="https://u", api_key="K", model="m", voice="v", text="x",
        opener=http, on_error=reasons.append,
    )
    assert audio is None
    assert reasons and "404" in reasons[0] and "model not found" in reasons[0]


def test_empty_response_reports_reason():
    reasons: list[str] = []
    assert tts.synthesize(
        url="https://u", api_key="K", model="m", voice="v", text="x",
        opener=FakeHTTP(response=b""), on_error=reasons.append,
    ) is None
    assert reasons == ["provider returned no audio"]


def test_to_ogg_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(tts.shutil, "which", lambda name: None)
    assert tts.to_ogg(b"RIFF") is None


def test_to_ogg_conversion_failure(monkeypatch):
    monkeypatch.setattr(tts.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def boom(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "ffmpeg")

    monkeypatch.setattr(tts.subprocess, "run", boom)
    assert tts.to_ogg(b"RIFF") is None


def test_to_ogg_ok(monkeypatch):
    monkeypatch.setattr(tts.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).write_bytes(b"OggS")

    monkeypatch.setattr(tts.subprocess, "run", fake_run)
    assert tts.to_ogg(b"RIFF") == b"OggS"


def test_to_ogg_reports_missing_ffmpeg(monkeypatch):
    monkeypatch.setattr(tts.shutil, "which", lambda name: None)
    reasons: list[str] = []
    assert tts.to_ogg(b"RIFF", on_error=reasons.append) is None
    assert reasons == ["ffmpeg not installed"]


def test_to_ogg_reports_ffmpeg_stderr(monkeypatch):
    monkeypatch.setattr(tts.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def boom(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "ffmpeg", stderr=b"Unknown encoder 'libopus'")

    monkeypatch.setattr(tts.subprocess, "run", boom)
    reasons: list[str] = []
    assert tts.to_ogg(b"RIFF", on_error=reasons.append) is None
    assert "Unknown encoder" in reasons[0]


def test_split_speech_short_text():
    assert tts.split_speech("Hola") == ["Hola"]
    assert tts.split_speech("   ") == []


def test_split_speech_respects_limit_and_sentences():
    segments = tts.split_speech("Primera frase. " * 30)
    assert len(segments) > 1
    for segment in segments:
        assert len(segment) <= tts.MAX_TTS_SEGMENT
        assert segment == segment.strip()
        assert segment.endswith(".")


def test_split_speech_word_wrap_and_hard_cut():
    words = ["palabra%d" % i for i in range(60)]
    segments = tts.split_speech(" ".join(words), total=10000)
    assert len(segments) > 1
    assert all(len(seg) <= tts.MAX_TTS_SEGMENT for seg in segments)
    assert all(w in words for seg in segments for w in seg.split())
    huge = tts.split_speech("z" * 500, limit=100, total=10000)
    assert [len(seg) for seg in huge] == [100] * 5


def test_split_speech_total_budget():
    text = "Una frase de prueba bastante larga. " * 100
    assert sum(map(len, tts.split_speech(text))) <= tts.MAX_TTS_INPUT


def test_merge_wavs_concatenates_samples():
    merged = tts.merge_wavs([_wav(b"\x01\x00" * 10), _wav(b"\x02\x00" * 5)])
    with wave.open(io.BytesIO(merged)) as handle:
        assert handle.getnframes() == 15
        assert handle.getframerate() == 24000


def test_merge_wavs_streamed_header():
    first = bytearray(_wav(b"\x01\x00" * 4))
    offset = bytes(first).find(b"data") + 4
    first[offset:offset + 4] = b"\xff\xff\xff\xff"
    merged = tts.merge_wavs([bytes(first), _wav(b"\x02\x00" * 4)])
    with wave.open(io.BytesIO(merged)) as handle:
        assert handle.getnframes() == 8


def test_merge_wavs_rejects_mismatch_or_garbage():
    assert tts.merge_wavs([_wav(b"\x01\x00", 24000), _wav(b"\x01\x00", 16000)]) is None
    assert tts.merge_wavs([b"A", b"B"]) is None
    assert tts.merge_wavs([b"A"]) == b"A"


def test_synthesize_segments_are_merged():
    http = FakeHTTP(response=_wav(b"\x01\x00" * 3))
    text = " ".join(("Esta es una frase de prueba " + "x" * 90 + ".") for _ in range(3))
    audio = tts.synthesize(url="https://u", api_key="K", model="m", voice="v", text=text, opener=http)
    assert len(http.requests) >= 2
    for request in http.requests:
        assert len(json.loads(request.data.decode())["input"]) <= tts.MAX_TTS_SEGMENT
    with wave.open(io.BytesIO(audio)) as handle:
        assert handle.getnframes() == 3 * len(http.requests)


def test_synthesize_partial_on_later_failure():
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            return _wav(b"\x01\x00" * 3)
        raise urllib.error.URLError("down")

    reasons: list[str] = []
    text = "A" * 150 + ". " + "B" * 150 + "."
    audio = tts.synthesize(
        url="https://u", api_key="K", model="m", voice="v", text=text,
        opener=opener, on_error=reasons.append,
    )
    assert audio is not None
    assert "URLError" in reasons[0]
