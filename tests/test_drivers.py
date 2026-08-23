"""Tests de los drivers (sin subprocesos reales)."""

from __future__ import annotations

import json
from pathlib import Path

from grafeno.drivers import available_clis, get_driver
from grafeno.drivers.base import EventKind, RunRequest
from grafeno.drivers.kimi import KimiDriver
from grafeno.drivers.opencode import OpenCodeDriver


def _request(**overrides) -> RunRequest:
    base = dict(prompt="hola", model="", workdir=Path("/tmp/x"), session_id=None, title="t")
    base.update(overrides)
    return RunRequest(**base)


# ---------------------------------------------------------------------- #
# OpenCode
# ---------------------------------------------------------------------- #
def test_opencode_command_full():
    driver = OpenCodeDriver()
    cmd = driver.build_command(
        _request(model="opencode-go/kimi-k3", session_id="ses_1")
    )
    assert cmd[:2] == ["opencode", "run"]
    assert "hola" in cmd
    assert cmd[cmd.index("-m") + 1] == "opencode-go/kimi-k3"
    assert "--auto" in cmd
    assert cmd[cmd.index("--format") + 1] == "json"
    assert cmd[cmd.index("--dir") + 1] == "/tmp/x"
    assert cmd[cmd.index("--session") + 1] == "ses_1"
    assert cmd[cmd.index("--title") + 1] == "t"


def test_opencode_command_minimal():
    cmd = OpenCodeDriver().build_command(_request())
    assert "-m" not in cmd
    assert "--session" not in cmd


def test_opencode_decode_text_and_session():
    driver = OpenCodeDriver()
    line = json.dumps({"type": "text", "sessionID": "ses_9", "part": {"text": "hola"}})
    event, session = driver.decode_line(line)
    assert session == "ses_9"
    assert event.kind is EventKind.TEXT
    assert event.text == "hola"


def test_opencode_decode_tool_and_noise():
    driver = OpenCodeDriver()
    tool, _ = driver.decode_line(
        json.dumps({"type": "tool_use", "part": {"tool": "bash", "state": {"title": "ls"}}})
    )
    assert tool.kind is EventKind.TOOL
    noise, _ = driver.decode_line(json.dumps({"type": "step_start"}))
    assert noise is None


def test_opencode_decode_plain_fallback():
    event, session = OpenCodeDriver().decode_line("salida plana")
    assert event.kind is EventKind.TEXT
    assert event.text == "salida plana"
    assert session is None


def test_opencode_list_models(monkeypatch):
    driver = OpenCodeDriver()
    monkeypatch.setattr(
        driver, "_run_sync", lambda cmd: "opencode-go/kimi-k3\nopencode/big-pickle\n\n"
    )
    assert driver.list_models() == ["opencode-go/kimi-k3", "opencode/big-pickle"]


# ---------------------------------------------------------------------- #
# Kimi
# ---------------------------------------------------------------------- #
def test_kimi_command_full():
    driver = KimiDriver()
    cmd = driver.build_command(_request(model="kimi-code/k3", session_id="s1"))
    assert cmd[0] == "kimi"
    assert cmd[cmd.index("-p") + 1] == "hola"
    assert "--auto" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert cmd[cmd.index("-m") + 1] == "kimi-code/k3"
    assert cmd[cmd.index("-S") + 1] == "s1"


def test_kimi_command_minimal():
    cmd = KimiDriver().build_command(_request())
    assert "-m" not in cmd
    assert "-S" not in cmd


def test_kimi_decode_assistant_message():
    driver = KimiDriver()
    line = json.dumps(
        {
            "type": "assistant",
            "session_id": "s1",
            "message": {"content": [{"type": "text", "text": "hola"}]},
        }
    )
    event, session = driver.decode_line(line)
    assert session == "s1"
    assert event.kind is EventKind.TEXT
    assert event.text == "hola"


def test_kimi_list_models(monkeypatch):
    driver = KimiDriver()
    payload = json.dumps({"models": {"kimi-code/k3": {}, "kimi-code/kimi-for-coding": {}}})
    monkeypatch.setattr(driver, "_run_sync", lambda cmd: payload)
    assert driver.list_models() == ["kimi-code/k3", "kimi-code/kimi-for-coding"]


def test_kimi_list_models_failure(monkeypatch):
    driver = KimiDriver()
    monkeypatch.setattr(driver, "_run_sync", lambda cmd: None)
    assert driver.list_models() == []


# ---------------------------------------------------------------------- #
# Registro
# ---------------------------------------------------------------------- #
def test_registry():
    assert get_driver("opencode").name == "opencode"
    assert get_driver("kimi").name == "kimi"
    try:
        get_driver("codex")
        raise AssertionError("debió lanzar NotImplementedError")
    except NotImplementedError:
        pass
    try:
        get_driver("inexistente")
        raise AssertionError("debió lanzar KeyError")
    except KeyError:
        pass
    assert isinstance(available_clis(), list)
