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

from .api import _encode_multipart, default_opener

STT_TIMEOUT = 120.0
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # provider limit for audio uploads

Opener = Callable[[urllib.request.Request, float], bytes]


def transcribe(
    *,
    url: str,
    api_key: str,
    model: str,
    data: bytes,
    filename: str = "voice.oga",
    timeout: float = STT_TIMEOUT,
    opener: Opener | None = None,
) -> str | None:
    """Transcribe ``data`` (Telegram voice notes are opus/ogg) to text.

    Returns the transcription, or None when not configured or on any
    network/provider error (the caller reports a friendly message).
    """
    if not url.strip() or not api_key.strip() or not data:
        return None
    if len(data) > MAX_AUDIO_BYTES:
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
        },
        method="POST",
    )
    open_fn = opener or default_opener
    try:
        raw = open_fn(request, timeout)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    text = payload.get("text")
    return text.strip() if isinstance(text, str) and text.strip() else None
