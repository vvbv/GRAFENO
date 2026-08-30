"""Clipboard images: read, store under the task's media dir, list and open.

All functions are best effort: any failure returns None / False and never
raises into the TUI.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import paths

MEDIA_TOKEN_PREFIX = "media/"  # token inserted into texts: media/media-01.png

_PNG_HEADER = b"\x89PNG\r\n\x1a\n"
_MAC_FALLBACK_PATH = "/tmp/grafeno-clipboard.png"
_MAC_OSASCRIPT = [
    "osascript",
    "-e",
    'set png_data to the clipboard as «class PNGf»',
    "-e",
    'set fp to open for access (POSIX file "/tmp/grafeno-clipboard.png") with write permission',
    "-e",
    "set eof fp to 0",
    "-e",
    "write png_data to fp",
    "-e",
    "close access fp",
]


def _clipboard_command() -> list[str] | None:
    """Return the argv to read a PNG from the system clipboard, or None."""
    if sys.platform == "darwin":
        if shutil.which("pngpaste"):
            return ["pngpaste", "-"]
        if shutil.which("osascript"):
            return list(_MAC_OSASCRIPT)
        return None
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
        return ["wl-paste", "--no-newline", "--type", "image/png"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]
    return None


def _is_macos_fallback(cmd: list[str]) -> bool:
    """The macOS osascript fallback writes the PNG to a temp file instead of stdout."""
    return cmd and cmd[0] == "osascript"


def read_clipboard_image() -> bytes | None:
    """Best-effort read of a PNG image from the OS clipboard."""
    cmd = _clipboard_command()
    if cmd is None:
        return None
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if _is_macos_fallback(cmd):
        try:
            data = Path(_MAC_FALLBACK_PATH).read_bytes()
        except OSError:
            return None
        finally:
            try:
                os.remove(_MAC_FALLBACK_PATH)
            except OSError:
                pass
        return data if data.startswith(_PNG_HEADER) else None
    if result.returncode != 0:
        return None
    data = result.stdout
    if not data or not data.startswith(_PNG_HEADER):
        return None
    return data


async def read_clipboard_image_async() -> bytes | None:
    """Non-blocking wrapper around read_clipboard_image (worker thread)."""
    return await asyncio.to_thread(read_clipboard_image)


def next_media_name(directory: Path) -> str:
    """First free media-NN.png name in ``directory`` (NN = 01..99; epoch fallback)."""
    for n in range(1, 100):
        name = f"media-{n:02d}.png"
        if not (directory / name).exists():
            return name
    return f"media-{int(time.time())}.png"


def next_pending_name(pending: list[tuple[str, bytes]]) -> str:
    """First free media-NN.png name among the buffered pending images."""
    used = {name for name, _ in pending}
    for n in range(1, 100):
        name = f"media-{n:02d}.png"
        if name not in used:
            return name
    return f"media-{int(time.time())}.png"


def save_image(task_id: str, data: bytes) -> Path | None:
    """Persist ``data`` as the next PNG in the task's media dir."""
    directory = paths.media_dir(task_id)
    target = directory / next_media_name(directory)
    try:
        target.write_bytes(data)
    except OSError:
        return None
    return target


def save_pending(task_id: str, pending: list[tuple[str, bytes]]) -> list[Path]:
    """Flush buffered (name, bytes) pairs into the task's media dir.

    If a suggested name collides with an existing file on disk, the collision
    is resolved via ``next_media_name``; the original token inserted into the
    text may then be out of sync with the actual file name. This is harmless
    in practice (the task directory did not exist when the paste happened),
    but the token will not be re-rewritten here.
    """
    directory = paths.media_dir(task_id)
    written: list[Path] = []
    for suggested, data in pending:
        target = directory / suggested
        if target.exists():
            target = directory / next_media_name(directory)
        try:
            target.write_bytes(data)
        except OSError:
            continue
        written.append(target)
    return written


def list_media(task_id: str) -> list[Path]:
    """Sorted PNG list in the task's media dir (empty if the dir does not yet exist)."""
    directory = paths.task_dir(task_id) / "media"
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.png"))


def open_media(path: Path) -> bool:
    """Open ``path`` with the OS default viewer (fire and forget)."""
    if sys.platform == "darwin":
        argv = ["open", str(path)]
    else:
        argv = ["xdg-open", str(path)]
    if not shutil.which(argv[0]):
        return False
    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return True


def inline_preview_supported() -> bool:
    """True if the terminal + optional textual-image package can render images inline."""
    term = (os.environ.get("TERM") or "").lower()
    kitty = bool(os.environ.get("KITTY_WINDOW_ID")) or "kitty" in term
    wezterm = os.environ.get("TERM_PROGRAM") == "WezTerm"
    iterm = os.environ.get("TERM_PROGRAM") == "iTerm.app"
    if not (kitty or wezterm or iterm):
        return False
    return importlib.util.find_spec("textual_image") is not None
