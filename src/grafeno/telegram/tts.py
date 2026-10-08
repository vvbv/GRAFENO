"""Text-to-speech via an OpenAI-compatible ``/audio/speech`` endpoint.

Groq is the default provider (canopylabs/orpheus-v1-english, male voice
``troy``); the URL, key, model and voice are configurable. Blocking HTTP
(stdlib only); callers wrap it with ``asyncio.to_thread``. Best effort:
failures return ``None`` and are reported via ``on_error`` (same pattern
as ``stt.transcribe``). The provider's WAV is converted to OGG/OPUS with
an external ``ffmpeg`` (``to_ogg``) so ``sendVoice`` can play it; without
``ffmpeg`` the caller falls back to ``sendAudio``. Providers like Groq
Orpheus reject inputs of 200+ chars, so the text is split into short
segments (one request each) and the WAVs are merged; ``to_ogg`` reports
why a conversion failed via ``on_error``.
"""

from __future__ import annotations

import json
import re
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
MAX_TTS_INPUT = 800    # total chars spoken per reply (voice replies are summaries)
MAX_TTS_SEGMENT = 190  # chars per provider request (Groq Orpheus: under 200)
FFMPEG_TIMEOUT = 30.0  # seconds for the wav -> ogg/opus conversion

Opener = Callable[[urllib.request.Request, float], bytes]


def _report(on_error: Callable[[str], None] | None, message: str) -> None:
    if on_error is not None:
        on_error(message)


_SPEECH_BREAK = re.compile(r"(?<=[.!?;:])\s+|\n+")


def split_speech(text: str, limit: int = MAX_TTS_SEGMENT, total: int = MAX_TTS_INPUT) -> list[str]:
    """Split ``text`` into speech segments of at most ``limit`` chars.

    Cuts at sentence ends and line breaks, then at spaces (hard cut only
    for a single word longer than ``limit``); whitespace is collapsed and
    the result is capped at ``total`` chars overall. Providers like Groq
    Orpheus reject inputs of 200+ chars, so each segment is one request.
    """
    pieces: list[str] = []
    for raw in _SPEECH_BREAK.split(text):
        piece = " ".join(raw.split())  # collapse whitespace
        while len(piece) > limit:  # word-wrap long sentences
            cut = piece.rfind(" ", 0, limit + 1)
            if cut <= 0:
                cut = limit  # single huge word: hard cut
            pieces.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            pieces.append(piece)
    segments: list[str] = []
    current = ""
    for piece in pieces:  # greedy packing of whole sentences
        candidate = f"{current} {piece}" if current else piece
        if len(candidate) <= limit:
            current = candidate
        else:
            segments.append(current)
            current = piece
    if current:
        segments.append(current)
    capped: list[str] = []
    used = 0
    for segment in segments:  # overall budget
        room = total - used
        if room <= 0:
            break
        segment = segment[:room].strip()
        if segment:
            capped.append(segment)
            used += len(segment)
    return capped


def _parse_wav(data: bytes) -> tuple[bytes, bytes] | None:
    """Return (fmt chunk body, sample data) of a RIFF/WAVE buffer, or None."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos, fmt = 12, b""
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        start = pos + 8
        if chunk_id == b"data":
            end = start + size
            if size in (0, 0xFFFFFFFF) or end > len(data):
                end = len(data)  # streamed WAV: unknown size, take the rest
            return (fmt, data[start:end]) if fmt else None
        if chunk_id == b"fmt ":
            fmt = data[start:start + size]
        pos = start + size + (size & 1)  # chunks are word-aligned
    return None


def merge_wavs(parts: list[bytes]) -> bytes | None:
    """Concatenate WAV buffers with the same format into one WAV (None if impossible)."""
    if len(parts) == 1:
        return parts[0]
    parsed = [_parse_wav(part) for part in parts]
    chunks = [item for item in parsed if item is not None]
    if not chunks or len(chunks) != len(parsed):
        return None
    fmt = chunks[0][0]
    if any(item[0] != fmt for item in chunks):
        return None
    samples = b"".join(item[1] for item in chunks)
    return (
        b"RIFF" + (4 + 8 + len(fmt) + 8 + len(samples)).to_bytes(4, "little") + b"WAVE"
        + b"fmt " + len(fmt).to_bytes(4, "little") + fmt
        + b"data" + len(samples).to_bytes(4, "little") + samples
    )


def _synthesize_one(
    *,
    url: str,
    api_key: str,
    model: str,
    voice: str,
    text: str,
    timeout: float,
    opener: Opener,
    on_error: Callable[[str], None] | None,
) -> bytes | None:
    """Request the audio of a single (already short) segment."""
    body = json.dumps(
        {"model": model, "input": text, "voice": voice, "response_format": "wav"},
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
    try:
        data = opener(request, timeout)
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

    The text is split into segments of up to MAX_TTS_SEGMENT chars (one
    request each, MAX_TTS_INPUT chars in total: voice replies are short
    summaries) and the resulting WAVs are merged into one. If a segment
    after the first fails, the partial audio is returned and the reason
    still reaches ``on_error``; if the WAVs cannot be merged, the first
    segment is returned. Returns None when not configured or on any
    network/provider error. ``on_error`` receives a short reason (HTTP
    code and provider message; the API key is redacted) for logging.
    """
    segments = split_speech(text)
    if not url.strip() or not api_key.strip() or not segments:
        return None
    open_fn = opener or default_opener
    parts: list[bytes] = []
    for segment in segments:
        data = _synthesize_one(
            url=url, api_key=api_key, model=model, voice=voice,
            text=segment, timeout=timeout, opener=open_fn, on_error=on_error,
        )
        if data is None:
            break  # keep what was synthesized so far; on_error has the reason
        parts.append(data)
    if not parts:
        return None
    merged = merge_wavs(parts)
    return merged if merged is not None else parts[0]


def ffmpeg_available() -> bool:
    """Return True when an ffmpeg executable is on PATH."""
    return shutil.which("ffmpeg") is not None


def to_ogg(wav: bytes, *, on_error: Callable[[str], None] | None = None) -> bytes | None:
    """Convert WAV bytes to OGG/OPUS with an external ffmpeg (best effort).

    Telegram ``sendVoice`` only renders a playable voice bubble for
    OGG/OPUS (or MP3/M4A); Groq TTS returns WAV. Returns None when ffmpeg
    is not installed or the conversion fails; callers fall back to
    sending the WAV as an audio file. ``on_error`` receives the reason.
    """
    if not wav:
        return None
    if not ffmpeg_available():
        _report(on_error, "ffmpeg not installed")
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
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        _report(on_error, f"ffmpeg failed: {stderr[-200:] or f'exit {exc.returncode}'}")
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        _report(on_error, f"ffmpeg failed: {type(exc).__name__}: {exc}"[:300])
        return None
