"""Git operations on the target project (branch per task). Never raises."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..i18n import t


def _git(workdir: Path, *args: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(workdir), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0, output


def is_git_repo(workdir: Path) -> bool:
    ok, output = _git(workdir, "rev-parse", "--is-inside-work-tree")
    return ok and output.strip() == "true"


def create_branch(workdir: Path, branch: str) -> tuple[bool, str]:
    """Create (or switch to) the task branch. Returns (success, message)."""
    ok, output = _git(workdir, "checkout", "-b", branch)
    if ok:
        return True, t("git.branch_created", branch=branch)
    if "already exists" in output:
        ok, output = _git(workdir, "checkout", branch)
        if ok:
            return True, t("git.branch_exists", branch=branch)
    return False, output or t("git.branch_failed", branch=branch)
