"""Read-only project documents (README.md, AGENTS.md, GRAFENO.md) for API clients."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .. import models, remotesession
from ..mdnorm import markdown_outline, normalize_markdown
from .actions import ApiError, _norm_dir, list_projects

if TYPE_CHECKING:
    from .service import ServerService


DOC_NAMES = ("README.md", "AGENTS.md", "GRAFENO.md")  # the only readable files
DOC_MAX_BYTES = 1024 * 1024  # bytes read per file; larger files are clipped
DOC_DEFAULT_CHARS = 4000  # default page size (fits a voice tool budget)
DOC_MAX_CHARS = 200_000  # largest page a client may request
OUTLINE_MAX_ENTRIES = 40


def canonical_name(raw: str) -> str:
    """Whitelisted document name for ``raw`` (case-insensitive, ``.md`` optional); 400 otherwise."""
    key = (raw or "").strip().casefold()
    if key.endswith(".md"):
        key = key[:-3]
    for doc in DOC_NAMES:
        if doc[:-3].casefold() == key:
            return doc
    raise ApiError(400, "unknown document; allowed: " + ", ".join(DOC_NAMES))


def resolve_project(
    service: "ServerService", workdir: str = "", project: str = ""
) -> tuple[dict, Path]:
    """Catalogue item and resolved local folder of an existing project.

    Only projects listed by ``list_projects`` are accepted, never an
    arbitrary path. ``effective_workdir`` is deliberately not used: it
    would mount sshfs as a side effect.
    """
    if remotesession.active():
        raise ApiError(409, "project documents are not available in remote session mode")
    items = list_projects(service)["projects"]
    tasks = models.list_all()
    local_dirs = {_norm_dir(t.workdir) for t in tasks if t.workdir and not t.is_remote}
    remote_dirs = {_norm_dir(t.workdir) for t in tasks if t.workdir and t.is_remote}
    if workdir.strip():
        target = _norm_dir(workdir)
        matches = [item for item in items if _norm_dir(item["workdir"]) == target]
    elif project.strip():
        needle = project.strip().casefold()
        matches = [
            item for item in items
            if item["name"].casefold() == needle
            or Path(item["workdir"].rstrip("/\\")).name.casefold() == needle
        ]
    else:
        raise ApiError(400, "workdir or project is required")
    unique: dict[str, dict] = {}
    for item in matches:
        unique.setdefault(_norm_dir(item["workdir"]), item)
    matches = list(unique.values())
    if not matches:
        raise ApiError(404, "project not found")
    if len(matches) > 1:
        raise ApiError(409, "ambiguous project: " + ", ".join(i["workdir"] for i in matches[:5]))
    item = matches[0]
    key = _norm_dir(item["workdir"])
    if key in remote_dirs and key not in local_dirs and not item.get("workspace"):
        raise ApiError(409, "remote projects are not supported")
    try:
        root = Path(item["workdir"]).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ApiError(404, "project folder not found") from exc
    if not root.is_dir():
        raise ApiError(404, "project folder not found")
    return item, root


def _find_doc(root: Path, name: str) -> Path | None:
    """``root/name`` or a case-insensitive first-level match; None when absent."""
    exact = root / name
    if exact.is_file():
        return exact
    try:
        with os.scandir(root) as entries:
            found = sorted(
                Path(entry.path) for entry in entries
                if entry.name.casefold() == name.casefold() and entry.is_file()
            )
    except OSError:
        return None
    return found[0] if found else None


def _inspect(root: Path, name: str) -> tuple[str, Path | None]:
    """``(status, path)``: status is ok, missing, blocked or unreadable."""
    path = _find_doc(root, name)
    if path is None:
        return "missing", None
    try:
        real = path.resolve()
    except (OSError, RuntimeError):
        return "unreadable", path
    if not real.is_relative_to(root):  # symlink escaping the project folder
        return "blocked", None
    if not os.access(real, os.R_OK):
        return "unreadable", path
    return "ok", path


def _read_text(path: Path) -> tuple[str, bool]:
    """Normalized text of a document (at most DOC_MAX_BYTES) and whether it was clipped."""
    with path.open("rb") as handle:
        raw = handle.read(DOC_MAX_BYTES + 1)
    clipped = len(raw) > DOC_MAX_BYTES
    text = raw[:DOC_MAX_BYTES].decode("utf-8", errors="replace").lstrip("﻿")
    return normalize_markdown(text), clipped


def _modified(stat: os.stat_result) -> str:
    return datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")


def list_docs(service: "ServerService", workdir: str = "", project: str = "") -> dict:
    """Index of the project documents: presence, size and outline, no content."""
    item, root = resolve_project(service, workdir, project)
    docs: list[dict] = []
    available: list[str] = []
    for name in DOC_NAMES:
        status, path = _inspect(root, name)
        if status in ("missing", "blocked"):
            docs.append({"name": name, "exists": False, "status": status})
            continue
        try:
            stat = path.stat()
            text, clipped = _read_text(path)
        except OSError:
            docs.append({"name": name, "exists": True, "status": "unreadable"})
            continue
        if status != "ok":
            docs.append({"name": name, "exists": True, "status": status})
            continue
        outline, truncated = markdown_outline(text, limit=OUTLINE_MAX_ENTRIES)
        docs.append({
            "name": name, "exists": True, "status": "ok", "file": path.name,
            "size": stat.st_size, "modified": _modified(stat), "chars": len(text),
            "approx_tokens": (len(text) + 3) // 4, "clipped": clipped,
            "outline": outline, "outline_truncated": truncated,
        })
        available.append(name)
    return {
        "project": {"workdir": item["workdir"], "name": item["name"]},
        "docs": docs,
        "available": available,
        "default_max_chars": DOC_DEFAULT_CHARS,
        "max_chars_limit": DOC_MAX_CHARS,
    }


def read_doc(
    service: "ServerService",
    workdir: str = "",
    project: str = "",
    name: str = "",
    offset: int = 0,
    max_chars: int | None = None,
) -> dict:
    """One page of one project document."""
    doc = canonical_name(name)
    if offset < 0:
        raise ApiError(400, "offset must be >= 0")
    size = DOC_DEFAULT_CHARS if max_chars is None else max_chars
    if not 1 <= size <= DOC_MAX_CHARS:
        raise ApiError(400, f"max_chars must be between 1 and {DOC_MAX_CHARS}")
    item, root = resolve_project(service, workdir, project)
    status, path = _inspect(root, doc)
    if status == "missing":
        raise ApiError(404, f"document not found: {doc}")
    if status == "blocked":
        raise ApiError(403, f"document outside the project folder: {doc}")
    if status != "ok":
        raise ApiError(404, f"document not readable: {doc}")
    try:
        stat = path.stat()
        text, clipped = _read_text(path)
    except OSError as exc:
        raise ApiError(404, f"document not readable: {doc}") from exc
    total = len(text)
    if offset > total:
        raise ApiError(400, "offset beyond the end of the document")
    end = min(offset + size, total)
    if end < total:
        cut = text.rfind("\n", offset, end)  # avoid cutting a line in half
        if cut >= offset + size // 2:
            end = cut + 1
    return {
        "project": {"workdir": item["workdir"], "name": item["name"]},
        "name": doc, "file": path.name, "size": stat.st_size,
        "modified": _modified(stat), "total_chars": total,
        "approx_tokens": (total + 3) // 4,
        "offset": offset, "next_offset": end if end < total else None,
        "has_more": end < total, "clipped": clipped, "content": text[offset:end],
    }
