"""Detection and launching of the editor associated with GRAFENO.

Automatically opens an editor (GUI or console) when the TUI starts, according
to the ``[editor]`` configuration (global or per-project). Everything is
best-effort: if the editor cannot be opened, GRAFENO still starts.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass

from .config import EditorConfig

# Known GUI editors: config name -> binaries to try.
GUI_EDITORS: dict[str, tuple[str, ...]] = {
    "vscode": ("code",),
    "codium": ("codium",),
    "zed": ("zed",),
    "sublime": ("subl",),
    "cursor": ("cursor",),
}

# Known console editors (open inside a terminal).
CONSOLE_EDITORS: dict[str, tuple[str, ...]] = {
    "tode": ("tode",),
    "nvim": ("nvim",),
    "vim": ("vim",),
    "helix": ("hx",),
    "nano": ("nano",),
}


@dataclass
class TerminalInfo:
    """Detected terminal and its capabilities."""

    name: str = "unknown"   # ghostty | wezterm | kitty | iterm | terminal.app | tmux | alacritty | unknown
    supports_split: bool = False
    split_command: list[str] | None = None   # template; the editor command is appended at the end
    window_command: list[str] | None = None  # template; same


def _ghostty(env: dict[str, str]) -> TerminalInfo | None:
    if env.get("TERM_PROGRAM") != "ghostty" and not env.get("GHOSTTY_RESOURCES_DIR"):
        return None
    binary = shutil.which("ghostty")
    if binary is None:
        return None
    return TerminalInfo(
        name="ghostty",
        supports_split=True,
        split_command=[binary, "+new-split:<dir>"],
        window_command=[binary, "-e"],
    )


def _wezterm(env: dict[str, str]) -> TerminalInfo | None:
    if env.get("TERM_PROGRAM") != "WezTerm" and not env.get("WEZTERM_EXECUTABLE"):
        return None
    binary = shutil.which("wezterm")
    if binary is None:
        return None
    return TerminalInfo(
        name="wezterm",
        supports_split=True,
        split_command=[binary, "cli", "split-pane", "--<dir>"],
        window_command=[binary, "start", "--"],
    )


def _kitty(env: dict[str, str]) -> TerminalInfo | None:
    if env.get("TERM") != "xterm-kitty" and not env.get("KITTY_PID"):
        return None
    binary = shutil.which("kitty")
    if binary is None:
        return None
    template = [binary, "@", "launch", "--location=hsplit"]
    return TerminalInfo(
        name="kitty",
        supports_split=True,
        split_command=template,
        window_command=[binary],
    )


def _iterm(env: dict[str, str]) -> TerminalInfo | None:
    if env.get("TERM_PROGRAM") != "iTerm.app":
        return None
    if platform.system() != "Darwin":
        return None
    return TerminalInfo(
        name="iterm",
        supports_split=False,
        window_command=["open", "-na", "iTerm", "--args"],
    )


def _terminal_app(env: dict[str, str]) -> TerminalInfo | None:
    if env.get("TERM_PROGRAM") != "Apple_Terminal":
        return None
    if platform.system() != "Darwin":
        return None
    return TerminalInfo(
        name="terminal.app",
        supports_split=False,
        window_command=["open", "-na", "Terminal", "--args"],
    )


def _tmux(env: dict[str, str]) -> TerminalInfo | None:
    if not env.get("TMUX"):
        return None
    binary = shutil.which("tmux")
    if binary is None:
        return None
    return TerminalInfo(
        name="tmux",
        supports_split=True,
        split_command=[binary, "split-window", "-h"],
        window_command=[binary, "new-window"],
    )


def _alacritty(env: dict[str, str]) -> TerminalInfo | None:
    if env.get("TERM_PROGRAM") != "alacritty" and not env.get("ALACRITTY_SOCKET"):
        return None
    binary = shutil.which("alacritty")
    if binary is None:
        return None
    return TerminalInfo(
        name="alacritty",
        supports_split=False,
        window_command=[binary, "-e"],
    )


def detect_terminal(env: dict[str, str] | None = None) -> TerminalInfo:
    """Detect the terminal in use from environment variables."""
    resolved = os.environ if env is None else env
    for detector in (_ghostty, _wezterm, _kitty, _iterm, _terminal_app, _tmux, _alacritty):
        info = detector(resolved)
        if info is not None:
            return info
    return TerminalInfo()


def available_editors() -> list[str]:
    """Names of installed editors (GUI and console), in order of preference:
    GUI first, then console."""
    found: list[str] = []
    for table in (GUI_EDITORS, CONSOLE_EDITORS):
        for name, binaries in table.items():
            if any(shutil.which(binary) for binary in binaries):
                found.append(name)
    return found


def is_gui_editor(editor: str) -> bool:
    """True if the name corresponds to a known GUI editor."""
    return editor in GUI_EDITORS


def editor_binary(editor: str) -> str | None:
    """Executable binary for an editor name, or None if not present."""
    for table in (GUI_EDITORS, CONSOLE_EDITORS):
        binaries = table.get(editor)
        if binaries is None:
            continue
        for binary in binaries:
            path = shutil.which(binary)
            if path:
                return path
        return None
    return shutil.which(editor)


def _expand_direction(template: list[str], side: str) -> list[str]:
    """Substitute ``<dir>`` in the split template according to the side."""
    direction = "left" if side not in ("left", "right") else side
    return [part.replace("<dir>", direction) for part in template]


def _expand_split_template(template: list[str], side: str) -> list[str]:
    """Substitute the direction placeholder in the split template."""
    if any("--location=" in part for part in template):
        return list(template)
    return _expand_direction(template, side)


def _tmux_split(template: list[str], side: str) -> list[str]:
    """Tmux template: insert ``-b`` after ``split-window`` when side == left."""
    if "split-window" not in template:
        return list(template)
    expanded: list[str] = []
    inserted = False
    for part in template:
        expanded.append(part)
        if not inserted and part == "split-window" and side == "left":
            expanded.append("-b")
            inserted = True
    return expanded


def build_launch_command(
    editor_cfg: EditorConfig,
    terminal: TerminalInfo,
    workdir: str,
) -> list[str] | None:
    """Full command to open the editor, or None if not applicable."""
    if not editor_cfg.enabled or editor_cfg.mode == "none":
        return None

    name = editor_cfg.editor
    if not name:
        return None  # no editor configured: grafeno only

    binary = editor_binary(name)
    if binary is None:
        return None

    if is_gui_editor(name):
        return [binary, workdir]

    editor_cmd = [binary] if name == "nano" else [binary, workdir]

    if editor_cfg.mode == "split" and terminal.supports_split and terminal.split_command:
        template = terminal.split_command
        if terminal.name == "tmux":
            expanded = _tmux_split(template, editor_cfg.side)
        else:
            expanded = _expand_split_template(template, editor_cfg.side)
        return expanded + editor_cmd

    if terminal.window_command:
        return terminal.window_command + editor_cmd
    return None


def launch_editor(command: list[str], workdir: str) -> bool:
    """Launch the editor in the background. True if it started, False otherwise."""
    try:
        subprocess.Popen(
            command,
            cwd=workdir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return True


def maybe_open_editor(editor_cfg: EditorConfig, workdir: str) -> bool:
    """Open the configured editor if applicable. Returns True if it was opened."""
    command = build_launch_command(editor_cfg, detect_terminal(), workdir)
    if command is None:
        return False
    return launch_editor(command, workdir)
