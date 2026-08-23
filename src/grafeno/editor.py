"""Detección y lanzamiento del editor asociado a GRAFENO.

Abre automáticamente un editor (GUI o de consola) al arrancar la TUI,
según la configuración `[editor]` (global o por proyecto). Todo es de
mejor esfuerzo: si no se puede abrir el editor, GRAFENO arranca igual.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass

from .config import EditorConfig

# Editores GUI conocidos: nombre de configuración -> binario(s) a probar.
GUI_EDITORS: dict[str, tuple[str, ...]] = {
    "vscode": ("code",),
    "codium": ("codium",),
    "zed": ("zed",),
    "sublime": ("subl",),
    "cursor": ("cursor",),
}

# Editores de consola conocidos (se abren dentro de una terminal).
CONSOLE_EDITORS: dict[str, tuple[str, ...]] = {
    "tode": ("tode",),
    "nvim": ("nvim",),
    "vim": ("vim",),
    "helix": ("hx",),
    "nano": ("nano",),
}


@dataclass
class TerminalInfo:
    """Terminal detectada y sus capacidades."""

    name: str = "unknown"   # ghostty | wezterm | kitty | iterm | terminal.app | tmux | alacritty | unknown
    supports_split: bool = False
    split_command: list[str] | None = None   # plantilla; el comando del editor se añade al final
    window_command: list[str] | None = None  # plantilla; idem


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
    """Detecta la terminal en uso a partir de variables de entorno."""
    resolved = os.environ if env is None else env
    for detector in (_ghostty, _wezterm, _kitty, _iterm, _terminal_app, _tmux, _alacritty):
        info = detector(resolved)
        if info is not None:
            return info
    return TerminalInfo()


def available_editors() -> list[str]:
    """Nombres de editores (GUI y consola) instalados, en orden de
    preferencia: primero GUI, luego consola."""
    found: list[str] = []
    for table in (GUI_EDITORS, CONSOLE_EDITORS):
        for name, binaries in table.items():
            if any(shutil.which(binary) for binary in binaries):
                found.append(name)
    return found


def is_gui_editor(editor: str) -> bool:
    """True si el nombre corresponde a un editor GUI conocido."""
    return editor in GUI_EDITORS


def editor_binary(editor: str) -> str | None:
    """Binario ejecutable para un nombre de editor, o None si no existe."""
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
    """Sustituye <dir> en la plantilla de split según el lado."""
    direction = "left" if side not in ("left", "right") else side
    return [part.replace("<dir>", direction) for part in template]


def _expand_split_template(template: list[str], side: str) -> list[str]:
    """Sustituye el marcador de dirección en la plantilla de split."""
    if any("--location=" in part for part in template):
        return list(template)
    return _expand_direction(template, side)


def _tmux_split(template: list[str], side: str) -> list[str]:
    """Plantilla de tmux: inserta -b tras `split-window` cuando side == left."""
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
    """Comando completo para abrir el editor, o None si no procede."""
    if not editor_cfg.enabled or editor_cfg.mode == "none":
        return None

    installed = available_editors()
    name = editor_cfg.editor or (installed[0] if installed else "")
    if not name:
        return None

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
    """Lanza el editor en segundo plano. True si arrancó, False si no."""
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
    """Abre el editor configurado si procede. Devuelve True si lo abrió."""
    command = build_launch_command(editor_cfg, detect_terminal(), workdir)
    if command is None:
        return False
    return launch_editor(command, workdir)
