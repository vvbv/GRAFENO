"""Text-to-speech via an OpenAI-compatible ``/audio/speech`` endpoint.

Groq is the default provider (canopylabs/orpheus-v1-english, male voice
``troy``); the URL, key, model and voice are configurable. Blocking HTTP
(stdlib only); callers wrap it with ``asyncio.to_thread``. Best effort:
failures return ``None`` and are reported via ``on_error`` (same pattern
as ``stt.transcribe``). The provider's WAV is converted to OGG/OPUS with
an external ``ffmpeg`` (``to_ogg``) so ``sendVoice`` can play it; without
``ffmpeg`` the caller falls back to ``sendAudio``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .api import USER_AGENT, default_opener
from .stt import _http_error_detail

TTS_TIMEOUT = 60.0
MAX_TTS_INPUT = 1500  # chars sent to the provider (voice replies are summaries)
FFMPEG_TIMEOUT = 30.0  # seconds for the wav -> ogg/opus conversion

Opener = Callable[[urllib.request.Request, float], bytes]


def _report(on_error: Callable[[str], None] | None, message: str) -> None:
    if on_error is not None:
        on_error(message)


def synthesize(
    *,
    url: str,
    api_key: str,
    model: str,
    voice: str,
    text: str,
    timeout: float = TTS_TIMEOUT,
    opener: Opener | None = None,
    on_error: Callable[[str], None] | None = None,
) -> bytes | None:
    """Generate speech audio (wav bytes) for ``text``.

    The input is truncated to MAX_TTS_INPUT chars: voice replies are short
    summaries, not the full text. Returns None when not configured or on
    any network/provider error. ``on_error`` receives a short reason (HTTP
    code and provider message; the API key is redacted) for logging.
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
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    open_fn = opener or default_opener
    try:
        data = open_fn(request, timeout)
    except urllib.error.HTTPError as exc:
        _report(on_error, _http_error_detail(exc, api_key))
        return None
    except (urllib.error.URLError, OSError) as exc:
        _report(on_error, f"{type(exc).__name__}: {exc}"[:300])
        return None
    if not data:
        _report(on_error, "provider returned no audio")
        return None
    return data


def to_ogg(wav: bytes) -> bytes | None:
    """Convert WAV bytes to OGG/OPUS with an external ffmpeg (best effort).

    Telegram ``sendVoice`` only renders a playable voice bubble for
    OGG/OPUS (or MP3/M4A); Groq TTS returns WAV. Returns None when ffmpeg
    is not installed or the conversion fails; callers fall back to
    sending the WAV as an audio file.
    """
    if not wav or shutil.which("ffmpeg") is None:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="grafeno-tts-") as tmp:
            src = Path(tmp) / "in.wav"
            dst = Path(tmp) / "out.ogg"
            src.write_bytes(wav)
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(src), "-c:a", "libopus", "-b:a", "32k", str(dst)],
                check=True, capture_output=True, timeout=FFMPEG_TIMEOUT,
            )
            return dst.read_bytes() or None
    except (OSError, subprocess.SubprocessError):
        return None
