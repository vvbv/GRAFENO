"""Tests of the TTS client (fake transport, no network)."""

from __future__ import annotations

import json
import urllib.error

from grafeno.telegram import tts


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
    body = json.loads(http.requests[0].data.decode())
    assert len(body["input"]) == tts.MAX_TTS_INPUT


def test_not_configured_returns_none():
    assert tts.synthesize(url="", api_key="K", model="m", voice="v", text="x") is None
    assert tts.synthesize(url="https://u", api_key="K", model="m", voice="v", text="x") is None
    assert tts.synthesize(url="https://u", api_key="K", model="m", voice="v", text="  ") is None


def test_network_error_returns_none():
    http = FakeHTTP(error=urllib.error.URLError("down"))
    assert tts.synthesize(
        url="https://u", api_key="K", model="m", voice="v", text="x", opener=http
    ) is None
