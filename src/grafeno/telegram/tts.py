"""Text-to-speech via an OpenAI-compatible ``/audio/speech`` endpoint.

Groq is the default provider (canopylabs/orpheus-v1-english, male voice
``troy``); the URL, key, model and voice are configurable. Blocking HTTP
(stdlib only); callers wrap it with ``asyncio.to_thread``. Best effort:
any failure returns ``None``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

from .api import default_opener

TTS_TIMEOUT = 60.0
MAX_TTS_INPUT = 1500  # chars sent to the provider (voice replies are summaries)

Opener = Callable[[urllib.request.Request, float], bytes]


def synthesize(
    *,
    url: str,
    api_key: str,
    model: str,
    voice: str,
    text: str,
    timeout: float = TTS_TIMEOUT,
    opener: Opener | None = None,
) -> bytes | None:
    """Generate speech audio (wav bytes) for ``text``.

    The input is truncated to MAX_TTS_INPUT chars: voice replies are short
    summaries, not the full text. Returns None when not configured or on
    any network/provider error.
    """
    if not url.strip() or not api_key.strip() or not text.strip():
        return None
    body = json.dumps(
        {
            "model": model,
            "input": text.strip()[:MAX_TTS_INPUT],
            "voice": voice,
            "response_format": "wav",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    open_fn = opener or default_opener
    try:
        data = open_fn(request, timeout)
    except (urllib.error.URLError, OSError):
        return None
    return data or None
