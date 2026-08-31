"""Tests of the STT client (fake transport, no network)."""

from __future__ import annotations

import json
import urllib.error

from grafeno.telegram import stt


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


def test_transcribe_ok():
    http = FakeHTTP(json.dumps({"text": "  crea una tarea  "}).encode())
    text = stt.transcribe(
        url="https://stt.example.com/v1/audio/transcriptions",
        api_key="KEY",
        model="whisper-large-v3-turbo",
        data=b"AUDIO",
        opener=http,
    )
    assert text == "crea una tarea"
    request = http.requests[0]
    assert request.full_url == "https://stt.example.com/v1/audio/transcriptions"
    assert request.headers.get("Authorization") == "Bearer KEY"
    assert b"whisper-large-v3-turbo" in request.data
    assert b"AUDIO" in request.data


def test_not_configured_returns_none():
    assert stt.transcribe(url="", api_key="K", model="m", data=b"x") is None
    assert stt.transcribe(url="https://u", api_key="", model="m", data=b"x") is None
    assert stt.transcribe(url="https://u", api_key="K", model="m", data=b"") is None


def test_network_error_returns_none():
    http = FakeHTTP(error=urllib.error.URLError("down"))
    errors: list[str] = []
    assert stt.transcribe(
        url="https://u", api_key="K", model="m", data=b"x", opener=http,
        on_error=errors.append,
    ) is None
    assert errors and "down" in errors[0]


def test_request_carries_product_user_agent():
    """Cloudflare blocks urllib's default UA on some providers (e.g. Groq)."""
    http = FakeHTTP(json.dumps({"text": "ok"}).encode())
    stt.transcribe(url="https://u", api_key="K", model="m", data=b"x", opener=http)
    ua = http.requests[0].headers.get("User-agent", "")
    assert ua.startswith("grafeno/")
    assert "Python-urllib" not in ua


def test_http_error_reports_provider_message_and_redacts_key():
    import io

    def failing(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", {},
            io.BytesIO(b'{"error":{"message":"Invalid API Key gsk_secret"}}'),
        )

    errors: list[str] = []
    result = stt.transcribe(
        url="https://u", api_key="gsk_secret", model="m", data=b"x",
        opener=failing, on_error=errors.append,
    )
    assert result is None
    assert errors
    assert "401" in errors[0]
    assert "Invalid API Key" in errors[0]
    assert "gsk_secret" not in errors[0]  # the key is always redacted


def test_invalid_json_returns_none():
    http = FakeHTTP(response=b"not json")
    assert stt.transcribe(
        url="https://u", api_key="K", model="m", data=b"x", opener=http
    ) is None


def test_oversize_audio_returns_none():
    http = FakeHTTP()
    big = b"x" * (stt.MAX_AUDIO_BYTES + 1)
    assert stt.transcribe(
        url="https://u", api_key="K", model="m", data=big, opener=http
    ) is None
    assert http.requests == []  # rejected before any request
