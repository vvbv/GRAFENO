"""Speech-to-text via an OpenAI-compatible ``/audio/transcriptions`` endpoint.

Groq is the default provider (whisper-large-v3-turbo); the URL, key and
model are configurable. Blocking HTTP (stdlib only); callers wrap it with
``asyncio.to_thread``. Best effort: any failure returns ``None``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

from .api import _encode_multipart, USER_AGENT, default_opener

STT_TIMEOUT = 120.0
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # provider limit for audio uploads

Opener = Callable[[urllib.request.Request, float], bytes]


def _report(on_error: Callable[[str], None] | None, message: str) -> None:
    if on_error is not None:
        on_error(message)


def transcribe(
    *,
    url: str,
    api_key: str,
    model: str,
    data: bytes,
    filename: str = "voice.ogg",
    timeout: float = STT_TIMEOUT,
    opener: Opener | None = None,
    on_error: Callable[[str], None] | None = None,
) -> str | None:
    """Transcribe ``data`` (Telegram voice notes are opus/ogg) to text.

    Returns the transcription, or None when not configured or on any
    network/provider error. ``on_error`` receives a short reason (HTTP code
    and provider message; the API key is redacted) for logging/replies.
    """
    if not url.strip() or not api_key.strip() or not data:
        return None
    if len(data) > MAX_AUDIO_BYTES:
        _report(on_error, f"audio too large ({len(data)} bytes)")
        return None
    body, content_type = _encode_multipart(
        {"model": model, "response_format": "json"},
        {"file": (filename, data, "application/octet-stream")},
    )
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": content_type,
            "Authorization": f"Bearer {api_key}",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    open_fn = opener or default_opener
    try:
        raw = open_fn(request, timeout)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        _report(on_error, _http_error_detail(exc, api_key))
        return None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _report(on_error, f"{type(exc).__name__}: {exc}"[:300])
        return None
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    _report(on_error, "provider returned no text")
    return None


def _http_error_detail(exc: urllib.error.HTTPError, api_key: str) -> str:
    """Short provider error message (code + body message), key redacted."""
    detail = ""
    try:
        payload = json.loads(exc.read().decode("utf-8", errors="replace"))
        error = payload.get("error") or {}
        detail = str(error.get("message", "") or payload.get("message", ""))
    except (ValueError, AttributeError):
        pass
    message = f"HTTP {exc.code}: {detail or exc.reason or 'error'}"
    if api_key:
        message = message.replace(api_key, "***")  # never leak the key
    return message[:300]
