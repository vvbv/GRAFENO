"""Tests of the editor detection and launching module."""

from __future__ import annotations

import subprocess

import pytest

from grafeno import editor
from grafeno.config import EditorConfig


@pytest.fixture
def fake_which(monkeypatch):
    """Patch shutil.which to return a path only for the given names."""

    def _set(available: set[str], path_prefix: str = "/usr/bin/") -> None:
        def fake_which(name: str) -> str | None:
            return path_prefix + name if name in available else None

        monkeypatch.setattr(editor.shutil, "which", fake_which)

    return _set


def test_detect_ghostty(fake_which):
    fake_which({"ghostty"})
    info = editor.detect_terminal({"TERM_PROGRAM": "ghostty"})
    assert info.name == "ghostty"
    assert info.supports_split is True
    assert info.split_command is not None
    assert info.window_command is not None


def test_detect_macos_terminal(monkeypatch):
    monkeypatch.setattr(editor.platform, "system", lambda: "Darwin")
    info = editor.detect_terminal({"TERM_PROGRAM": "Apple_Terminal"})
    assert info.name == "terminal.app"
    assert info.supports_split is False


def test_detect_unknown():
    info = editor.detect_terminal({})
    assert info.name == "unknown"
    assert info.supports_split is False
    assert info.window_command is None
    assert info.split_command is None


def test_detect_priority_ghostty_over_tmux(fake_which):
    fake_which({"ghostty"})
    info = editor.detect_terminal({"TERM_PROGRAM": "ghostty", "TMUX": "/tmp/tmux-1000/default,12345,0"})
    assert info.name == "ghostty"


def test_available_editors(fake_which):
    fake_which({"zed", "tode"})
    assert editor.available_editors() == ["zed", "tode"]


def test_build_command_gui(fake_which, monkeypatch):
    fake_which({"code"})
    cfg = EditorConfig(enabled=True, editor="vscode", mode="window")
    cmd = editor.build_launch_command(cfg, editor.detect_terminal({}), "/tmp/work")
    assert cmd == ["/usr/bin/code", "/tmp/work"]


def test_build_command_console_split_ghostty(fake_which):
    fake_which({"ghostty", "tode"})
    cfg = EditorConfig(enabled=True, editor="tode", mode="split", side="right")
    terminal = editor.detect_terminal({"TERM_PROGRAM": "ghostty"})
    assert terminal.name == "ghostty"
    cmd = editor.build_launch_command(cfg, terminal, "/tmp/work")
    assert cmd == ["/usr/bin/ghostty", "+new-split:right", "/usr/bin/tode", "/tmp/work"]


def test_build_command_console_window_fallback(fake_which):
    fake_which({"tode"})
    cfg = EditorConfig(enabled=True, editor="tode", mode="split", side="left")
    cmd = editor.build_launch_command(cfg, editor.detect_terminal({}), "/tmp/work")
    # Unknown terminal: neither split nor window -> None
    assert cmd is None


def test_build_command_disabled(fake_which):
    fake_which({"code"})
    cfg = EditorConfig(enabled=False, editor="vscode")
    assert editor.build_launch_command(cfg, editor.detect_terminal({}), "/tmp/work") is None

    cfg = EditorConfig(enabled=True, editor="vscode", mode="none")
    assert editor.build_launch_command(cfg, editor.detect_terminal({}), "/tmp/work") is None


def test_build_command_no_editor_configured(monkeypatch, fake_which):
    """Without a configured editor nothing is opened (even if editors are installed)."""
    fake_which({"zed"})
    cfg = EditorConfig(editor="", mode="window")
    cmd = editor.build_launch_command(cfg, editor.detect_terminal({}), "/tmp/work")
    assert cmd is None


def test_launch_editor_oserror(monkeypatch):
    def fake_popen(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(editor.subprocess, "Popen", fake_popen)
    assert editor.launch_editor(["nonexistent"], "/tmp/work") is False


def test_launch_editor_success(monkeypatch):
    captured: dict[str, object] = {}

    class FakeProcess:
        pass

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(editor.subprocess, "Popen", fake_popen)
    assert editor.launch_editor(["code", "/tmp/work"], "/tmp/work") is True
    assert captured["command"] == ["code", "/tmp/work"]
    kwargs = captured["kwargs"]
    assert kwargs["cwd"] == "/tmp/work"
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL


def test_maybe_open_editor_returns_false_when_no_command(monkeypatch):
    monkeypatch.setattr(editor, "available_editors", lambda: [])
    cfg = EditorConfig(enabled=True, editor="nonexistent", mode="window")
    assert editor.maybe_open_editor(cfg, "/tmp/work") is False
