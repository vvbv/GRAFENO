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
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import _toml, models, paths

MEDIA_TOKEN_PREFIX = "media/"  # token inserted into texts: media/media-01.png
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")  # listed/injected as task images
AUDIO_SUFFIXES = (".ogg", ".oga", ".opus", ".mp3", ".wav", ".m4a", ".flac")  # transcribed on intake
VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v")
SPREADSHEET_SUFFIXES = (".xlsx", ".xlsm", ".xls", ".ods", ".csv", ".tsv")
DOCUMENT_SUFFIXES = (".docx", ".doc", ".odt", ".rtf", ".pdf", ".pptx", ".ppt", ".odp", ".txt", ".md")
DATA_SUFFIXES = (".json", ".xml", ".yaml", ".yml", ".toml", ".sql", ".log")
ARCHIVE_SUFFIXES = (".zip", ".tar", ".gz", ".tgz", ".7z")
ATTACHMENT_KINDS = (
    ("image", IMAGE_SUFFIXES),
    ("video", VIDEO_SUFFIXES),
    ("audio", AUDIO_SUFFIXES),
    ("spreadsheet", SPREADSHEET_SUFFIXES),
    ("document", DOCUMENT_SUFFIXES),
    ("data", DATA_SUFFIXES),
    ("archive", ARCHIVE_SUFFIXES),
)  # anything else is "other" and is stored as .bin
# No html/svg/js/sh/exe on purpose: the web panel serves media/ from its own origin.
SAFE_SUFFIXES = tuple(suffix for _, group in ATTACHMENT_KINDS for suffix in group)
MAX_ATTACHMENT_BYTES = 500 * 1024 * 1024  # per file, every intake channel
LARGE_ATTACHMENT_BYTES = 50 * 1024 * 1024   # prompts warn the agents not to read it whole
FREE_SPACE_MARGIN = 64 * 1024 * 1024        # disk space kept free when saving attachments

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


def attachment_kind(filename: str) -> str:
    """Kind of an attachment by its (original) file name: image, video, audio,
    spreadsheet, document, data, archive or other."""
    suffix = Path(filename).suffix.lower()
    for kind, group in ATTACHMENT_KINDS:
        if suffix in group:
            return kind
    return "other"


def display_name(filename: str) -> str:
    """Original file name made safe for prompts, TOML and UIs.

    Keeps only the last path segment (both separators), drops control
    characters and caps the length; empty input falls back to "attachment".
    """
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    return name[:120] or "attachment"


def format_size(size: int) -> str:
    """Human readable byte count (512 B, 24.1 KB, 3.2 MB)."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def base64_length(size: int) -> int:
    """Length of the standard (padded) base64 encoding of ``size`` bytes."""
    return 4 * ((size + 2) // 3)


def has_free_space(directory: Path, size: int) -> bool:
    """True when ``directory`` can hold ``size`` more bytes plus FREE_SPACE_MARGIN.

    Best effort: an unreadable filesystem reports True (the write itself will
    fail and be handled by the caller).
    """
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        return True
    return free >= size + FREE_SPACE_MARGIN


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
    """Sorted image list in the task's media dir (empty if it does not exist).

    Accepts png/jpg/jpeg: Telegram photos arrive as JPEG while pasted
    clipboard images are PNG.
    """
    directory = paths.task_dir(task_id) / "media"
    if not directory.is_dir():
        return []
    return sorted(
        entry
        for entry in directory.iterdir()
        if entry.is_file() and entry.suffix.lower() in IMAGE_SUFFIXES
    )


@dataclass
class Attachment:
    """Reference file attached to a task (stored under the task media dir)."""

    file: str                 # stored name inside media/ (media-NN<ext>)
    name: str = ""            # original file name as provided by the user
    kind: str = "other"       # see ATTACHMENT_KINDS
    size: int = 0             # bytes
    origin: str = ""          # tui | api | telegram ("" = pasted/legacy)
    cycle: int = 1            # task cycle the file was attached in
    added_at: str = ""        # ISO timestamp (seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file, "name": self.name, "kind": self.kind, "size": self.size,
            "origin": self.origin, "cycle": self.cycle, "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Attachment":
        return cls(
            file=str(data.get("file", "")),
            name=str(data.get("name", "")),
            kind=str(data.get("kind", "other")),
            size=int(data.get("size", 0) or 0),
            origin=str(data.get("origin", "")),
            cycle=int(data.get("cycle", 1) or 1),
            added_at=str(data.get("added_at", "")),
        )


def _load_manifest(task_id: str) -> dict[str, Attachment]:
    """Manifest entries by stored file name ({} when missing or unreadable)."""
    path = paths.attachments_manifest_path(task_id)
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    entries: dict[str, Attachment] = {}
    for raw in data.get("attachment", []):
        if not isinstance(raw, dict):
            continue
        try:
            entry = Attachment.from_dict(raw)
        except (TypeError, ValueError):
            continue
        if entry.file:
            entries[entry.file] = entry
    return entries


def _save_manifest(task_id: str, entries: list[Attachment]) -> None:
    """Rewrite the manifest (removes the file when ``entries`` is empty)."""
    path = paths.attachments_manifest_path(task_id)
    try:
        if not entries:
            path.unlink(missing_ok=True)
            return
        payload = {"attachment": [entry.to_dict() for entry in entries]}
        path.write_text(_toml.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def record_attachment(task_id: str, entry: Attachment) -> None:
    """Add or replace the manifest entry of ``entry.file``."""
    manifest = _load_manifest(task_id)
    manifest[entry.file] = entry
    _save_manifest(task_id, list(manifest.values()))


def list_attachments(task_id: str) -> list[Attachment]:
    """Every file of the task media dir (sorted by name) with its metadata.

    Files are the source of truth: files missing from the manifest (pasted
    images, legacy attachments) get default metadata (name = stored name,
    kind by suffix, size from disk); manifest entries without a file are
    ignored. Hidden files are skipped.
    """
    directory = paths.task_dir(task_id) / "media"
    if not directory.is_dir():
        return []
    manifest = _load_manifest(task_id)
    result: list[Attachment] = []
    for entry in sorted(directory.iterdir(), key=lambda e: e.name):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        meta = manifest.get(entry.name) or Attachment(
            file=entry.name, name=entry.name, kind=attachment_kind(entry.name)
        )
        try:
            meta.size = entry.stat().st_size
        except OSError:
            pass
        result.append(meta)
    return result


def is_stored_name(file: str) -> bool:
    """True for a plain stored file name (no separators, not hidden)."""
    return bool(file) and "/" not in file and "\\" not in file and not file.startswith(".")


def attachment_path(task_id: str, file: str) -> Path:
    """Absolute path of a stored attachment (no existence check)."""
    return (paths.task_dir(task_id) / "media" / file).resolve()


def delete_attachment(task_id: str, file: str) -> bool:
    """Remove a stored attachment and its manifest entry (False if missing)."""
    if not is_stored_name(file):
        return False
    target = attachment_path(task_id, file)
    if not target.is_file():
        return False
    try:
        target.unlink()
    except OSError:
        return False
    manifest = _load_manifest(task_id)
    if file in manifest:
        del manifest[file]
        _save_manifest(task_id, list(manifest.values()))
    return True


def save_attachment(
    task_id: str,
    filename: str,
    data: bytes,
    *,
    origin: str = "",
    cycle: int = 1,
) -> Path | None:
    """Persist arbitrary attachment bytes under the task's media dir.

    The original extension is kept (sanitized to ``SAFE_SUFFIXES``, anything
    else becomes ``.bin``); the name follows the ``media-NN<ext>`` pattern to
    avoid collisions and the original name, kind, size, origin and cycle are
    recorded in the task manifest. Files above ``MAX_ATTACHMENT_BYTES`` are
    rejected (None).
    """
    if len(data) > MAX_ATTACHMENT_BYTES:
        return None
    directory = paths.media_dir(task_id)
    if not has_free_space(directory, len(data)):
        return None
    target = _next_target(directory, filename)
    try:
        target.write_bytes(data)
    except OSError:
        return None
    _record_saved(task_id, target, filename, len(data), origin, cycle)
    return target


def _next_target(directory: Path, filename: str) -> Path:
    """Free ``media-NN<ext>`` path for ``filename`` (unsafe suffixes become .bin)."""
    suffix = Path(filename).suffix.lower()
    if suffix not in SAFE_SUFFIXES:
        suffix = ".bin"
    for n in range(1, 100):
        candidate = directory / f"media-{n:02d}{suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"media-{int(time.time())}{suffix}"


def _record_saved(
    task_id: str, target: Path, filename: str, size: int, origin: str, cycle: int
) -> None:
    """Add the manifest entry of a freshly stored attachment."""
    record_attachment(task_id, Attachment(
        file=target.name,
        name=display_name(filename),
        kind=attachment_kind(filename),  # by the ORIGINAL name: evil.sh -> "other"
        size=size,
        origin=origin,
        cycle=cycle,
        added_at=datetime.now().isoformat(timespec="seconds"),
    ))


def save_attachment_file(
    task_id: str,
    filename: str,
    source: Path,
    *,
    origin: str = "",
    cycle: int = 1,
    move: bool = False,
) -> Path | None:
    """Persist a file from disk under the task media dir without loading it.

    ``filename`` is the original name (kept in the manifest, its suffix picks
    the stored extension). ``move=True`` moves ``source`` (a rename on the same
    filesystem: used for spooled API uploads); otherwise it is copied with
    ``shutil.copyfile`` (in-kernel copy on Linux). Returns None when the file
    is missing, above MAX_ATTACHMENT_BYTES, the disk is full or the copy fails.
    """
    try:
        size = source.stat().st_size
    except OSError:
        return None
    if size > MAX_ATTACHMENT_BYTES:
        return None
    directory = paths.media_dir(task_id)
    if not move and not has_free_space(directory, size):
        return None
    target = _next_target(directory, filename)
    try:
        if move:
            shutil.move(str(source), str(target))
        else:
            shutil.copyfile(source, target)
    except OSError:
        target.unlink(missing_ok=True)
        return None
    _record_saved(task_id, target, filename, size, origin, cycle)
    return target


def reference_line(saved: Path, original: str = "") -> str:
    """Description line pointing at a saved attachment: media/ token for
    images, absolute path (plus the original name when it differs) otherwise."""
    if saved.suffix.lower() in IMAGE_SUFFIXES:
        return f"- {MEDIA_TOKEN_PREFIX}{saved.name}"
    label = display_name(original) if original else ""
    return f"- {saved} ({label})" if label and label != saved.name else f"- {saved}"


def attach_files(
    task: models.Task,
    attachments: list[tuple[str, bytes]],
    header: str,
    origin: str = "",
) -> list[Path]:
    """Save attachments into the task's media dir and append references to the
    task description (image tokens for image suffixes, absolute paths for the
    rest). Best effort: a failed write only drops that file.

    Shared by the Telegram bot (Spanish header) and the API server (English
    header) so both persist attachments the exact same way.
    """
    references: list[str] = []
    saved_paths: list[Path] = []
    for name, data in attachments:
        saved = save_attachment(task.id, name, data, origin=origin, cycle=task.cycle)
        if saved is None:
            continue
        saved_paths.append(saved)
        references.append(reference_line(saved, name))
    if references:
        task.description += "\n\n" + header + "\n" + "\n".join(references) + "\n"
        models.save(task)
    return saved_paths


def is_audio_name(filename: str) -> bool:
    """True when ``filename`` ends in a known audio suffix (see AUDIO_SUFFIXES)."""
    return Path(filename).suffix.lower() in AUDIO_SUFFIXES


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
