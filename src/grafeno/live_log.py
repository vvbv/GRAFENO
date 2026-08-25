"""Persistence of the live (human-readable) task log.

Each formatted ``Text`` entry appended to ``TaskRuntime.log`` is also
written as one JSON line to ``logs/live.jsonl`` inside the task data
directory, so the log survives application restarts. All operations are
best-effort: a disk error never breaks the pipeline or the TUI.
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.text import Text

from . import paths

_LIVE_LOG_NAME = "live.jsonl"


def _log_path(task_id: str) -> Path:
    return paths.logs_dir(task_id) / _LIVE_LOG_NAME


def append(task_id: str, entry: Text) -> None:
    """Append one formatted entry to the persisted live log (best-effort)."""
    record = {"style": str(entry.style or ""), "text": entry.plain}
    try:
        with _log_path(task_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load(task_id: str, max_entries: int) -> list[Text]:
    """Load the last ``max_entries`` persisted entries (best-effort)."""
    try:
        lines = _log_path(task_id).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[Text] = []
    for line in lines[-max_entries:] if max_entries > 0 else lines:
        try:
            record = json.loads(line)
            entries.append(Text(record.get("text", ""), style=record.get("style") or ""))
        except (json.JSONDecodeError, AttributeError, TypeError):
            continue  # skip corrupt lines
    return entries


def clear(task_id: str) -> None:
    """Delete the persisted live log (used when resetting to DRAFT)."""
    try:
        _log_path(task_id).unlink(missing_ok=True)
    except OSError:
        pass
