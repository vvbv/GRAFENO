"""Read-only project file explorer (listing, metadata, text pages, raw bytes) for API clients."""

from __future__ import annotations

import json
import os
import re
import stat as stat_module
from dataclasses import dataclass
from datetime import datetime
from email.utils import formatdate
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode

from .. import media
from .actions import _MEDIA_TYPES, ApiError
from .httpcore import Response
from .projectdocs import _modified, resolve_project

if TYPE_CHECKING:
    from .service import ServerService


FEATURE = "project files"
LIST_DEFAULT_LIMIT = 500      # entries per listing page
LIST_MAX_LIMIT = 2000         # largest listing page a client may request
SCAN_MAX_ENTRIES = 10_000     # entries read from one directory; more -> truncated
SNIFF_BYTES = 8192            # bytes inspected to tell text from binary
TEXT_MAX_BYTES = 2 * 1024 * 1024  # bytes decoded by the text endpoint; larger files are clipped
TEXT_DEFAULT_LINES = 400
TEXT_MAX_LINES = 5000
TEXT_PAGE_CHARS = 256_000     # a page stops adding lines past this size (at least one line)
BLOCKED_NAMES = frozenset({".git"})  # never listed, described or served, at any depth
TEXT_TYPE = "text/plain; charset=utf-8"
PREVIEW_IMAGE_SUFFIXES = media.IMAGE_SUFFIXES + (".bmp", ".ico", ".avif", ".heic")
_EXTRA_TYPES = {
    ".bmp": "image/bmp", ".ico": "image/x-icon", ".avif": "image/avif", ".heic": "image/heic",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint", ".rtf": "application/rtf",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".gz": "application/gzip", ".tgz": "application/gzip", ".tar": "application/x-tar",
    ".7z": "application/x-7z-compressed",
}
SAFE_TYPES = {**_MEDIA_TYPES, **_EXTRA_TYPES}  # never text/html, image/svg+xml or javascript
TEXT_SUFFIXES = frozenset({
    ".txt", ".md", ".rst", ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".sh", ".bash",
    ".zsh", ".ps1", ".bat", ".kt", ".kts", ".java", ".gradle", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".rs", ".go", ".rb", ".php", ".swift", ".gd", ".brs", ".html", ".htm", ".css",
    ".scss", ".svelte", ".vue", ".xml", ".svg", ".sql", ".csv", ".tsv", ".log", ".lock",
    ".gitignore", ".dockerignore", ".editorconfig",
})  # listing hint only; file info sniffs the content
TEXT_NAMES = frozenset({"makefile", "dockerfile", "license", "readme", "procfile", "console"})


# ---------------------------------------------------------------------- #
# Path resolution
# ---------------------------------------------------------------------- #
def resolve_path(root: Path, raw: str) -> tuple[Path, str]:
    """Resolved path under ``root`` and its normalized POSIX relative form (``""`` = root)."""
    raw = raw or ""
    if "\0" in raw:
        raise ApiError(400, "invalid path")
    if os.name == "nt":
        raw = raw.replace("\\", "/")
    if raw.startswith("/") or raw.startswith(" /"):  # must run before joinpath, which would discard root
        raise ApiError(400, "path must be relative to the project folder")
    parts = [p for p in PurePosixPath(raw).parts if p != "."]
    if os.name == "nt" and any(":" in p for p in parts):
        raise ApiError(400, "invalid path")
    if ".." in parts:
        raise ApiError(400, "path must not contain '..'")
    if _is_blocked(parts):
        raise ApiError(403, "path not allowed: .git")
    try:
        real = root.joinpath(*parts).resolve(strict=True)
    except (OSError, RuntimeError) as exc:  # missing, unreadable or symlink loop
        raise ApiError(404, "path not found") from exc
    if not real.is_relative_to(root):
        raise ApiError(403, "path outside the project folder")
    if _is_blocked(real.relative_to(root).parts):  # symlink into .git
        raise ApiError(403, "path not allowed: .git")
    return real, "/".join(parts)


def _is_blocked(parts) -> bool:
    """True when any path part is a blocked name (case-insensitive: macOS and SMB are not case-sensitive)."""
    return any(p.casefold() in BLOCKED_NAMES for p in parts)


def _parent(rel: str) -> str | None:
    """Parent of ``rel``: None at the root, ``""`` for first level entries."""
    if rel == "":
        return None
    return "/".join(rel.split("/")[:-1])


# ---------------------------------------------------------------------- #
# Content classification
# ---------------------------------------------------------------------- #
def looks_like_text(sample: bytes) -> bool:
    """Heuristic: no NUL byte and UTF-8 decodable (a multibyte char cut at the end is tolerated)."""
    if not sample:
        return True
    if b"\0" in sample:
        return False
    for cut in range(4):
        try:
            sample[: len(sample) - cut].decode("utf-8")
            return True
        except UnicodeDecodeError:
            continue
    controls = sum(1 for b in sample if b < 32 and b not in (9, 10, 12, 13, 27))
    return controls / len(sample) < 0.02  # Latin-1 text is still text


def is_text_name(name: str) -> bool:
    """Listing hint by name: known text suffix or well-known extensionless names."""
    lowered = name.lower()
    return (
        Path(name).suffix.lower() in TEXT_SUFFIXES
        or lowered in TEXT_SUFFIXES  # dotfiles like .gitignore have no pathlib suffix
        or lowered in TEXT_NAMES
    )


def mime_for(name: str, is_text: bool) -> str:
    """Safe MIME type: SAFE_TYPES by suffix, else text/plain for text, else octet-stream."""
    mime = SAFE_TYPES.get(Path(name).suffix.lower())
    if mime:
        return mime
    return TEXT_TYPE if is_text else "application/octet-stream"


def preview_for(name: str, mime: str, is_text: bool | None) -> str:
    """Preview kind for the client: image, pdf, audio, video, text, download or unknown."""
    if Path(name).suffix.lower() in PREVIEW_IMAGE_SUFFIXES:
        return "image"
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if is_text is True:
        return "text"
    if is_text is None:
        return "unknown"
    return "download"


# ---------------------------------------------------------------------- #
# Directory listing
# ---------------------------------------------------------------------- #
def _entry(root: Path, entry: os.DirEntry, parent_rel: str) -> dict:
    rel = f"{parent_rel}/{entry.name}" if parent_rel else entry.name
    item = {
        "name": entry.name, "path": rel, "type": "other", "size": None, "modified": None,
        "hidden": entry.name.startswith("."), "symlink": entry.is_symlink(),
        "readable": False, "mime": None, "kind": None, "preview": None,
    }
    if item["symlink"]:
        try:
            target = Path(entry.path).resolve(strict=True)
        except (OSError, RuntimeError):
            item["type"] = "broken"
            return item
        if not target.is_relative_to(root) or _is_blocked(target.relative_to(root).parts):
            item["type"] = "blocked"  # never reveal the target
            return item
    try:
        st = entry.stat(follow_symlinks=True)
    except OSError:
        return item
    if stat_module.S_ISDIR(st.st_mode):
        item["type"] = "dir"
        item["modified"] = _modified(st)
        item["readable"] = os.access(entry.path, os.R_OK | os.X_OK)
    elif stat_module.S_ISREG(st.st_mode):
        text_hint = True if is_text_name(entry.name) else None
        item["type"] = "file"
        item["size"] = st.st_size
        item["modified"] = _modified(st)
        item["readable"] = os.access(entry.path, os.R_OK)
        item["mime"] = mime_for(entry.name, bool(text_hint))
        item["kind"] = media.attachment_kind(entry.name)
        item["preview"] = preview_for(entry.name, item["mime"], text_hint)
    return item


def _scan(root: Path, real: Path, rel: str, hidden: bool) -> tuple[list[dict], bool]:
    """Entries of ``real`` and whether the scan stopped at SCAN_MAX_ENTRIES."""
    entries: list[dict] = []
    truncated = False
    try:
        with os.scandir(real) as scan:
            for count, entry in enumerate(scan):
                if count >= SCAN_MAX_ENTRIES:
                    truncated = True
                    break
                if _is_blocked((entry.name,)):
                    continue
                if entry.name.startswith(".") and not hidden:
                    continue
                entries.append(_entry(root, entry, rel))
    except PermissionError as exc:
        raise ApiError(403, "permission denied") from exc
    except OSError as exc:
        raise ApiError(404, "path not readable") from exc
    return entries, truncated


def list_dir(
    service: "ServerService", workdir: str = "", project: str = "", path: str = "",
    hidden: bool = False, limit: int | None = None, offset: int = 0,
) -> dict:
    """One page of the entries of a project directory (folders first, then by name)."""
    if offset < 0:
        raise ApiError(400, "offset must be >= 0")
    size = LIST_DEFAULT_LIMIT if limit is None else limit
    if not 1 <= size <= LIST_MAX_LIMIT:
        raise ApiError(400, f"limit must be between 1 and {LIST_MAX_LIMIT}")
    item, root = resolve_project(service, workdir, project, feature=FEATURE)
    real, rel = resolve_path(root, path)
    if not real.is_dir():
        raise ApiError(400, "path is not a directory")
    entries, truncated = _scan(root, real, rel, hidden)
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].casefold(), e["name"]))
    return {
        "project": {"workdir": item["workdir"], "name": item["name"]},
        "path": rel, "parent": _parent(rel),
        "entries": entries[offset:offset + size], "total": len(entries),
        "offset": offset, "limit": size,
        "has_more": offset + size < len(entries), "truncated": truncated, "hidden": hidden,
    }


# ---------------------------------------------------------------------- #
# File metadata
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProjectFile:
    """A readable regular file of a project (resolved and checked, not read)."""

    item: dict          # project catalogue item
    real: Path          # resolved path on disk
    rel: str            # POSIX path relative to the project root
    name: str
    size: int
    mtime: float
    is_text: bool
    mime: str

    @property
    def etag(self) -> str:
        return f'"{self.size:x}-{int(self.mtime * 1_000_000):x}"'

    @property
    def last_modified(self) -> str:
        return formatdate(self.mtime, usegmt=True)

    @property
    def modified(self) -> str:
        return datetime.fromtimestamp(self.mtime).isoformat(timespec="seconds")


def _project_ref(item: dict) -> dict:
    return {"workdir": item["workdir"], "name": item["name"]}


def _open_resolved(item: dict, real: Path, rel: str) -> ProjectFile:
    if rel == "" or not real.is_file():
        raise ApiError(400, "path is not a regular file")
    if not os.access(real, os.R_OK):
        raise ApiError(403, "permission denied")
    try:
        st = real.stat()
        with real.open("rb") as handle:
            sample = handle.read(SNIFF_BYTES)
    except PermissionError as exc:
        raise ApiError(403, "permission denied") from exc
    except OSError as exc:
        raise ApiError(404, "path not readable") from exc
    is_text = looks_like_text(sample)
    name = PurePosixPath(rel).name
    return ProjectFile(
        item=item, real=real, rel=rel, name=name, size=st.st_size, mtime=st.st_mtime,
        is_text=is_text, mime=mime_for(name, is_text),
    )


def open_file(service: "ServerService", workdir: str = "", project: str = "", path: str = "") -> ProjectFile:
    """Resolve and check a regular file of a project (400/403/404 on failure)."""
    item, root = resolve_project(service, workdir, project, feature=FEATURE)
    real, rel = resolve_path(root, path)
    return _open_resolved(item, real, rel)


def _url(kind: str, item: dict, rel: str, **extra: str) -> str:
    query = urlencode({"workdir": item["workdir"], "path": rel, **extra})
    return f"/api/v1/projects/file/{kind}?{query}"  # never carries a token


def file_info(service: "ServerService", workdir: str = "", project: str = "", path: str = "") -> dict:
    """Metadata of one file or directory of a project."""
    item, root = resolve_project(service, workdir, project, feature=FEATURE)
    real, rel = resolve_path(root, path)
    if real.is_dir():
        try:
            st = real.stat()
        except OSError as exc:
            raise ApiError(404, "path not readable") from exc
        return {
            "project": _project_ref(item), "path": rel, "name": PurePosixPath(rel).name or item["name"],
            "type": "dir", "modified": _modified(st),
            "readable": os.access(real, os.R_OK | os.X_OK), "parent": _parent(rel),
        }
    f = _open_resolved(item, real, rel)
    candidate = root.joinpath(*rel.split("/"))
    return {
        "project": _project_ref(item), "path": f.rel, "name": f.name, "type": "file",
        "parent": _parent(f.rel), "size": f.size, "modified": f.modified, "etag": f.etag,
        "mime": f.mime, "kind": media.attachment_kind(f.name),
        "preview": preview_for(f.name, f.mime, f.is_text),
        "text": f.is_text, "text_clipped": f.is_text and f.size > TEXT_MAX_BYTES,
        "text_max_bytes": TEXT_MAX_BYTES, "symlink": candidate.is_symlink(),
        "urls": {
            "raw": _url("raw", item, f.rel),
            "download": _url("raw", item, f.rel, download="1"),
            "text": _url("text", item, f.rel) if f.is_text else None,
        },
    }


# ---------------------------------------------------------------------- #
# Text pages
# ---------------------------------------------------------------------- #
def _decode(data: bytes, clipped: bool) -> tuple[str, bool]:
    """Decoded text and whether invalid sequences were replaced."""
    candidates = [data]
    if clipped:  # a multibyte char cut by the window is not an error
        candidates += [data[: len(data) - cut] for cut in range(1, 4)]
    for candidate in candidates:
        try:
            return candidate.decode("utf-8").removeprefix("\ufeff"), False
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace").removeprefix("\ufeff"), True


def _split_lines(text: str) -> list[str]:
    """Lines split on ``\\n`` only (str.splitlines would also split on \\x0b, \\x0c, ...)."""
    if not text:
        return []
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def read_text(
    service: "ServerService", workdir: str = "", project: str = "", path: str = "",
    line: int = 1, max_lines: int | None = None,
) -> dict:
    """One page of lines of a text file.

    ``total_lines`` counts the lines inside the decoded window only; when
    ``clipped`` is true the rest is available through the raw endpoint.
    """
    if line < 1:
        raise ApiError(400, "line must be >= 1")
    count_max = TEXT_DEFAULT_LINES if max_lines is None else max_lines
    if not 1 <= count_max <= TEXT_MAX_LINES:
        raise ApiError(400, f"max_lines must be between 1 and {TEXT_MAX_LINES}")
    f = open_file(service, workdir, project, path)
    if not f.is_text:
        raise ApiError(415, "binary file; use /api/v1/projects/file/raw", code="binary_file")
    try:
        with f.real.open("rb") as handle:
            raw = handle.read(TEXT_MAX_BYTES + 1)
    except PermissionError as exc:
        raise ApiError(403, "permission denied") from exc
    except OSError as exc:
        raise ApiError(404, "path not readable") from exc
    clipped = len(raw) > TEXT_MAX_BYTES
    text, decode_errors = _decode(raw[:TEXT_MAX_BYTES], clipped)
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = _split_lines(text)
    total = len(lines)
    if line > max(total, 1):
        raise ApiError(400, "line beyond the end of the file")
    page: list[str] = []
    chars = 0
    for current in lines[line - 1:]:
        if len(page) >= count_max or (page and chars > TEXT_PAGE_CHARS):
            break
        page.append(current)
        chars += len(current) + 1
    end_line = line + len(page) - 1 if page else 0
    has_more = end_line < total
    return {
        "project": _project_ref(f.item), "path": f.rel, "name": f.name, "size": f.size,
        "modified": f.modified, "etag": f.etag, "mime": f.mime, "line": line,
        "end_line": end_line, "total_lines": total,
        "next_line": end_line + 1 if has_more else None, "has_more": has_more,
        "clipped": clipped, "decode_errors": decode_errors,
        "newline": "crlf" if newline == "\r\n" else "lf", "content": "\n".join(page),
    }


# ---------------------------------------------------------------------- #
# Raw bytes
# ---------------------------------------------------------------------- #
class RangeNotSatisfiable(Exception):
    """The Range header cannot be served for the file size (HTTP 416)."""


_RANGE = re.compile(r"bytes=(\d{0,19})-(\d{0,19})", re.IGNORECASE)  # bounded: huge numbers are ignored


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Inclusive ``(start, end)`` of a single-range header; None = serve the whole file."""
    match = _RANGE.fullmatch((header or "").strip())
    if not match:  # empty, other units, multiple ranges or syntax errors are ignored
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    if not first:  # suffix range: the last N bytes
        count = int(last)
        if count == 0 or size == 0:
            raise RangeNotSatisfiable()
        return max(size - count, 0), size - 1
    start = int(first)
    if start >= size:
        raise RangeNotSatisfiable()
    if last:
        end = int(last)
        if end < start:
            return None
        return start, min(end, size - 1)
    return start, size - 1


def content_disposition(name: str, download: bool) -> str:
    """Header value with an ASCII fallback plus the RFC 5987 UTF-8 name (head is ASCII-only)."""
    fallback = "".join(
        "_" if (ord(c) < 0x20 or ord(c) > 0x7e or c in '"\\') else c for c in name
    ) or "file"
    kind = "attachment" if download else "inline"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def raw_response(
    f: ProjectFile, range_header: str = "", if_range: str = "", download: bool = False
) -> Response:
    """Streamed 200 / 206 / 416 response of a file; never serves active content types."""
    headers = {
        "Content-Type": f.mime, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'", "Accept-Ranges": "bytes",
        "ETag": f.etag, "Last-Modified": f.last_modified,
        "Content-Disposition": content_disposition(f.name, download),
    }
    if if_range and if_range not in (f.etag, f.last_modified):
        range_header = ""  # validator changed: send the whole file
    try:
        span = parse_range(range_header, f.size)
    except RangeNotSatisfiable:
        headers.update({"Content-Range": f"bytes */{f.size}", "Content-Type": "application/json"})
        headers.pop("Content-Disposition")
        body = json.dumps({"error": "range not satisfiable"}).encode("utf-8")
        return Response(416, headers=headers, body=body)
    if span is None:
        return Response(200, headers=headers, file=f.real)
    start, end = span
    headers["Content-Range"] = f"bytes {start}-{end}/{f.size}"
    return Response(206, headers=headers, file=f.real, file_offset=start, file_length=end - start + 1)


def read_raw(
    service: "ServerService", workdir: str = "", project: str = "", path: str = "",
    max_bytes: int | None = None,
) -> tuple[ProjectFile, bytes]:
    """File and its whole content; ``max_bytes`` caps one-message channels (413)."""
    f = open_file(service, workdir, project, path)
    if max_bytes is not None and f.size > max_bytes:
        raise ApiError(413, "file too large for this channel; use GET /api/v1/projects/file/raw")
    try:
        return f, f.real.read_bytes()
    except PermissionError as exc:
        raise ApiError(403, "permission denied") from exc
    except OSError as exc:
        raise ApiError(404, "path not readable") from exc
