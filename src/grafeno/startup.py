"""Startup commands: background processes launched when GRAFENO starts.

Each ``StartupAction`` (``[[startup]]`` in ``config.toml``) is split with
shlex and run WITHOUT a shell in its own process group (session on POSIX),
with stdout/stderr in ``~/.grafeno/startup/<slug>.log``. Before launching,
the optional JSON ``settings`` are merged into ``settings_file`` so the tool
(e.g. a web editor) always starts with them. Presets cover common web
editors, but any command works: nothing here is tied to VS Code. An action
with ``open_url`` is the web editor of the panel: it gets ``EDITOR_DEFAULTS``
(dark theme) under its ``settings`` and ``EDITOR_SETTINGS`` on top. Everything
is best effort: a failing command never prevents GRAFENO from starting.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import paths
from .config import StartupAction
from .projectsetup import _spawn_options

WORKDIR_PLACEHOLDER = "{workdir}"  # replaced in every argv item by the effective workdir
STOP_GRACE_SECONDS = 5.0           # SIGTERM -> SIGKILL grace when GRAFENO exits
EARLY_EXIT_CHECK_SECONDS = 5.0     # the App checks for crashed commands after this delay
EDITOR_SETTINGS: dict[str, Any] = {"git.repositoryScanMaxDepth": -1}  # always enforced on web editors
EDITOR_DEFAULTS: dict[str, Any] = {"workbench.colorTheme": "Default Dark Modern"}  # dark theme unless the command settings set another
PRESET_SETTINGS = json.dumps({**EDITOR_DEFAULTS, **EDITOR_SETTINGS})  # settings text prefilled by the presets
EDITOR_URL = "http://{host}:8080/?folder={folder}"  # preset link: ?folder= works in the three presets

PRESETS: dict[str, StartupAction] = {
    "vscode": StartupAction(
        name="vscode-web",
        command="code serve-web --host 0.0.0.0 --port 8080 --without-connection-token",
        settings_file="~/.vscode-server/data/Machine/settings.json",
        settings=PRESET_SETTINGS,
        open_url=EDITOR_URL,
    ),
    "code-server": StartupAction(
        name="code-server",
        command="code-server --bind-addr 0.0.0.0:8080 --auth none",
        settings_file="~/.local/share/code-server/User/settings.json",
        settings=PRESET_SETTINGS,
        open_url=EDITOR_URL,
    ),
    "openvscode-server": StartupAction(
        name="openvscode-server",
        command="openvscode-server --host 0.0.0.0 --port 8080 --without-connection-token",
        settings_file="~/.openvscode-server/data/Machine/settings.json",
        settings=PRESET_SETTINGS,
        open_url=EDITOR_URL,
    ),
}


def preset(key: str) -> StartupAction | None:
    """Copy of a preset (callers may edit it freely), or None if unknown."""
    base = PRESETS.get(key)
    return replace(base) if base is not None else None


def expand_path(text: str) -> Path:
    """Path with ``~`` and ``$VARS`` expanded."""
    return Path(os.path.expandvars(os.path.expanduser(text.strip())))


def parse_settings(text: str) -> dict[str, Any]:
    """Parse the ``settings`` field: empty = {}; anything but a JSON object raises ValueError."""
    if not text.strip():
        return {}
    data = json.loads(text)  # json.JSONDecodeError is a ValueError
    if not isinstance(data, dict):
        raise ValueError("settings must be a JSON object")
    return data


URL_PLACEHOLDERS = ("{host}", "{folder}", "{name}")
_HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}")
_IPV6_RE = re.compile(r"\[?([0-9A-Fa-f:.]{2,45})\]?")


def is_editor(action: StartupAction) -> bool:
    """True when the action declares a web panel link (``open_url``)."""
    return bool(action.open_url.strip())


def effective_settings(action: StartupAction) -> dict[str, Any]:
    """Settings merged into ``settings_file``.

    Editors get ``EDITOR_DEFAULTS`` (dark theme) underneath their own
    ``settings`` and ``EDITOR_SETTINGS`` on top: the command may pick another
    theme, but never another git scan depth. Raises ValueError when
    ``settings`` is not a JSON object.
    """
    settings = parse_settings(action.settings)
    if not is_editor(action):
        return settings
    return {**EDITOR_DEFAULTS, **settings, **EDITOR_SETTINGS}


def editor_action(actions: list[StartupAction]) -> StartupAction | None:
    """Web editor of the panel: the first enabled action with ``open_url``, else the first one at all."""
    editors = [action for action in actions if is_editor(action)]
    enabled = [action for action in editors if action.enabled]
    return (enabled or editors or [None])[0]


def normalize_host(host: str) -> str:
    """Host for ``{host}``: empty = localhost; IPv6 gets brackets. Raises ValueError when unsafe."""
    value = (host or "").strip()
    if not value:
        return "localhost"
    if _HOST_RE.fullmatch(value):
        return value
    match = _IPV6_RE.fullmatch(value)
    if match and ":" in match.group(1):
        return f"[{match.group(1)}]"
    raise ValueError("invalid host")


def folder_for_url(folder: str) -> str:
    """Absolute folder as a URL-safe path (``/`` kept; Windows ``C:\\x`` -> ``/C:/x``)."""
    text = folder.replace("\\", "/")
    if re.match(r"[A-Za-z]:/", text):
        text = "/" + text
    return quote(text, safe="/:")


def editor_url(action: StartupAction, folder: str, host: str = "") -> str:
    """``open_url`` with its placeholders substituted. Raises ValueError (bad host or not http(s))."""
    values = {
        "{host}": normalize_host(host),
        "{folder}": folder_for_url(folder),
        "{name}": quote(Path(folder).name, safe=""),
    }
    url = action.open_url.strip()
    for key, value in values.items():  # not str.format: the template may carry other braces
        url = url.replace(key, value)
    if not re.match(r"https?://", url, re.IGNORECASE):
        raise ValueError("open_url must be an http(s) URL")
    return url


def _strip_comments(text: str) -> str:
    out: list[str] = []
    index, size, in_string = 0, len(text), False
    while index < size:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < size:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
        elif text.startswith("//", index):
            end = text.find("\n", index)
            index = size if end == -1 else end
            continue
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = size if end == -1 else end + 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    out: list[str] = []
    index, size, in_string = 0, len(text), False
    while index < size:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < size:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
        elif char == ",":
            ahead = index + 1
            while ahead < size and text[ahead] in " \t\r\n":
                ahead += 1
            if ahead < size and text[ahead] in "}]":
                index += 1
                continue  # drop the trailing comma
        out.append(char)
        index += 1
    return "".join(out)


def strip_jsonc(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments and trailing commas outside strings (VS Code JSONC)."""
    return _strip_trailing_commas(_strip_comments(text))  # comments first: ", // note\n}" must lose its comma


def _same(left: Any, right: Any) -> bool:
    """Strict equality: True == 1 in Python, but not in a settings file."""
    return type(left) is type(right) and left == right


def apply_settings(settings_file: str, settings: dict[str, Any]) -> bool:
    """Merge ``settings`` into the JSON file; returns True if the file was written.

    Raises ValueError (unparseable file, not an object) or OSError. Never
    rewrites an unparseable file.
    """
    if not settings:
        return False
    path = expand_path(settings_file)
    data: dict[str, Any] = {}
    jsonc = False
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if text.strip():
            try:
                data = json.loads(text)
            except ValueError:
                data = json.loads(strip_jsonc(text))  # raises ValueError when still invalid
                jsonc = True
        if not isinstance(data, dict):
            raise ValueError(f"{path} is not a JSON object")
    if all(key in data and _same(data[key], value) for key, value in settings.items()):
        return False
    if jsonc:
        shutil.copy2(path, path.with_name(path.name + ".grafeno.bak"))  # comments are lost on rewrite
    merged = {**data, **settings}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".grafeno.tmp")
    tmp.write_text(json.dumps(merged, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return True


def build_argv(action: StartupAction, workdir: str) -> list[str]:
    """argv of the action with ``{workdir}`` substituted and the executable resolved.

    Raises ValueError (empty or unbalanced quotes) or FileNotFoundError
    (executable not on PATH).
    """
    argv = shlex.split(action.command, posix=os.name != "nt")  # ValueError on unbalanced quotes
    if not argv:
        raise ValueError("empty command")
    argv = [part.replace(WORKDIR_PLACEHOLDER, workdir) for part in argv]
    executable = shutil.which(os.path.expanduser(argv[0]))
    if executable is None:  # also covers Windows .cmd shims (which() adds PATHEXT)
        raise FileNotFoundError(argv[0])
    return [executable, *argv[1:]]


def log_path(action: StartupAction) -> Path:
    """``~/.grafeno/startup/<slug>.log`` (slug of the name, ``command`` when unnamed)."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", action.name.strip()).strip("-.") or "command"
    return paths.startup_dir() / f"{slug}.log"


def validation_error(action: StartupAction) -> str:
    """i18n-ready error text for an invalid action, or "" when it is valid."""
    from .i18n import t

    name = action.name.strip()
    if not name or not action.command.strip():
        return t("cfg.startup.error.required")
    if action.settings.strip():
        try:
            parse_settings(action.settings)
        except ValueError:
            return t("cfg.startup.error.settings", name=name)
        if not action.settings_file.strip():
            return t("cfg.startup.error.settings_file", name=name)
    url = action.open_url.strip()
    if url and (not re.match(r"https?://", url, re.IGNORECASE) or "{folder}" not in url):
        return t("cfg.startup.error.open_url", name=name)
    return ""


@dataclass
class StartupOutcome:
    """Result of launching one startup action (shown as notifications by the App)."""

    name: str
    ok: bool
    detail: str = ""          # error text when ok is False
    settings_error: str = ""  # settings could not be applied (the command still runs)
    settings_updated: bool = False
    log: str = ""             # log file path


@dataclass
class StartupProcess:
    """A running startup command."""

    action: StartupAction
    process: subprocess.Popen
    log: str
    reported: bool = False  # early exit already notified


def _terminate_tree(process: subprocess.Popen) -> None:
    """Stop a startup process and the children of its group (best effort, bounded)."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run([taskkill, "/F", "/T", "/PID", str(process.pid)],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=STOP_GRACE_SECONDS, **_spawn_options())
    else:
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGTERM)  # own session: pgid == pid
        try:
            process.wait(timeout=STOP_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1.0)


class StartupRunner:
    """Launches the enabled startup actions and stops them when GRAFENO exits."""

    def __init__(self) -> None:
        self.outcomes: list[StartupOutcome] = []
        self.processes: list[StartupProcess] = []

    def launch_all(self, actions: list[StartupAction], base_workdir: str) -> list[StartupOutcome]:
        """Launch every enabled action (best effort, never raises)."""
        results: list[StartupOutcome] = []
        for action in actions:
            if not (action.enabled and action.command.strip()):
                continue
            try:
                outcome = self.launch(action, base_workdir)
            except Exception as exc:  # unexpected failure: report it, keep going
                label = action.name.strip() or action.command.strip()
                outcome = StartupOutcome(label, False, detail=str(exc))
            results.append(outcome)
        self.outcomes.extend(results)
        return results

    def launch(self, action: StartupAction, base_workdir: str) -> StartupOutcome:
        """Apply the settings, then spawn the command detached with its log file."""
        label = action.name.strip() or action.command.strip()
        workdir = str(expand_path(action.workdir)) if action.workdir.strip() else base_workdir
        if not Path(workdir).is_dir():
            return StartupOutcome(label, False, detail=f"workdir not found: {workdir}")
        settings_error = ""
        settings_updated = False
        if action.settings_file.strip():  # never blocks the launch; editors always get EDITOR_SETTINGS
            try:
                settings_updated = apply_settings(action.settings_file, effective_settings(action))
            except (OSError, ValueError) as exc:
                settings_error = str(exc)
        try:
            argv = build_argv(action, workdir)
        except FileNotFoundError as exc:
            return StartupOutcome(label, False, detail=f"command not found: {exc}",
                                  settings_error=settings_error, settings_updated=settings_updated)
        except ValueError as exc:
            return StartupOutcome(label, False, detail=str(exc),
                                  settings_error=settings_error, settings_updated=settings_updated)
        log = log_path(action)
        try:
            with log.open("wb") as handle:  # the child inherits the descriptor
                process = subprocess.Popen(
                    argv, cwd=workdir, stdin=subprocess.DEVNULL,
                    stdout=handle, stderr=subprocess.STDOUT, **_spawn_options(),
                )
        except OSError as exc:
            return StartupOutcome(label, False, detail=str(exc),
                                  settings_error=settings_error, settings_updated=settings_updated)
        self.processes.append(StartupProcess(action, process, str(log)))
        return StartupOutcome(label, True, settings_error=settings_error,
                              settings_updated=settings_updated, log=str(log))

    def early_exits(self) -> list[tuple[StartupProcess, int]]:
        """Processes that already exited with a non-zero code, each reported once."""
        found: list[tuple[StartupProcess, int]] = []
        for item in self.processes:
            code = item.process.poll()
            if code not in (None, 0) and not item.reported:
                item.reported = True
                found.append((item, code))
        return found

    def stop_all(self) -> None:
        """Terminate the processes marked ``stop_on_exit`` (and their process groups)."""
        for item in self.processes:
            if item.action.stop_on_exit:
                try:
                    _terminate_tree(item.process)
                except Exception:
                    pass
        self.processes = [item for item in self.processes if not item.action.stop_on_exit]
