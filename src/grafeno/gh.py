"""GitHub CLI (`gh`) integration: availability detection and issue listing.

Everything is best-effort and never raises: if `gh` is missing, the workdir
is not a git repository, there is no authenticated access to the remote or
the network fails, the functions return ``False`` / ``[]``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .pipeline.gitops import is_git_repo

_TIMEOUT = 30  # seconds per external command


@dataclass
class GhIssue:
    """A GitHub issue reduced to what the task form needs."""

    number: int
    title: str
    body: str


def _gh(workdir: Path, *args: str) -> tuple[bool, str]:
    """Run ``gh <args>`` in ``workdir``; return (success, stdout). Never raises."""
    try:
        completed = subprocess.run(
            ["gh", *args],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    return completed.returncode == 0, completed.stdout


def gh_available(workdir: Path) -> bool:
    """True if the workdir is a git repo with `gh` installed and repo access.

    The access probe is ``gh repo view --json name``: it fails when the user
    is not authenticated, the remote is not a GitHub repo or the repository
    is not reachable.
    """
    if shutil.which("gh") is None:
        return False
    if not is_git_repo(workdir):
        return False
    ok, _ = _gh(workdir, "repo", "view", "--json", "name")
    return ok


def list_issues(workdir: Path, *, limit: int = 50) -> list[GhIssue]:
    """Open issues of the repository (newest first). Empty list on any error."""
    ok, output = _gh(
        workdir,
        "issue", "list",
        "--state", "open",
        "--limit", str(limit),
        "--json", "number,title,body",
    )
    if not ok:
        return []
    try:
        raw = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return []
    issues: list[GhIssue] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            issues.append(GhIssue(
                number=int(item["number"]),
                title=str(item.get("title") or ""),
                body=str(item.get("body") or ""),
            ))
        except (KeyError, TypeError, ValueError):
            continue  # malformed entry: skipped, not fatal
    return issues
