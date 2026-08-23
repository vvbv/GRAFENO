"""Tests de los drivers (sin subprocesos reales)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from grafeno.drivers import available_clis, get_driver
from grafeno.drivers.base import CLIDriver, EventKind, RunRequest, read_lines
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
    # kimi -p no admite --auto ni -y (verificado contra kimi 0.37)
    assert "--auto" not in cmd
    assert "-y" not in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert cmd[cmd.index("-m") + 1] == "kimi-code/k3"
    assert cmd[cmd.index("-S") + 1] == "s1"


def test_kimi_command_minimal():
    cmd = KimiDriver().build_command(_request())
    assert "-m" not in cmd
    assert "-S" not in cmd


def test_kimi_decode_real_event_shapes():
    """Eventos reales capturados de kimi 0.37 --output-format stream-json."""
    driver = KimiDriver()

    event, _ = driver.decode_line('{"role":"assistant","content":"ok"}')
    assert event.kind is EventKind.TEXT
    assert event.text == "ok"

    event, _ = driver.decode_line(
        '{"role":"assistant","tool_calls":[{"type":"function","id":"t1",'
        '"function":{"name":"Write","arguments":"{}"}}]}'
    )
    assert event.kind is EventKind.TOOL
    assert "Write" in event.text

    event, _ = driver.decode_line('{"role":"tool","content":"Wrote 4 bytes to /tmp/x"}')
    assert event.kind is EventKind.TOOL
    assert "Wrote 4 bytes" in event.text

    event, session = driver.decode_line(
        '{"role":"meta","type":"session.resume_hint","session_id":"session_abc","command":"kimi -r session_abc"}'
    )
    assert event is None
    assert session == "session_abc"

    event, _ = driver.decode_line('{"role":"meta","type":"system.version","version":"0.37.2"}')
    assert event is None


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


# ---------------------------------------------------------------------- #
# read_lines (stream reading without line-length limit)
# ---------------------------------------------------------------------- #
async def _collect(*chunks: bytes) -> list[str]:
    # The reader is created inside the event loop on purpose: building an
    # asyncio.StreamReader outside a running loop leaves it bound to the
    # loop later closed by asyncio.run(), which makes subsequent reads fail.
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()
    return [line async for line in read_lines(reader)]


def test_read_lines_long_line_beyond_asyncio_limit():
    """A single line longer than the 64 KiB asyncio limit must not fail."""
    long_line = "x" * 200_000
    payload = (long_line + "\ncorta\n").encode()
    lines = asyncio.run(_collect(payload))
    assert lines == [long_line, "corta"]


def test_read_lines_multibyte_split_across_chunks():
    """A multi-byte UTF-8 char split across reads must decode correctly."""
    payload = "áé\nfin".encode()  # á and é are 2 bytes each in UTF-8
    pieces = [payload[i : i + 1] for i in range(len(payload))]  # 1-byte chunks
    lines = asyncio.run(_collect(*pieces))
    assert lines == ["áé", "fin"]


def test_read_lines_no_trailing_newline_and_empty():
    lines = asyncio.run(_collect(b"sin salto final"))
    assert lines == ["sin salto final"]
    assert asyncio.run(_collect(b"")) == []


def test_run_with_cli_line_beyond_64k(tmp_path):
    """End-to-end: a CLI printing a >64 KiB line must complete without error.

    Reproduces the original bug: readline() raised ValueError("Separator is
    found, but chunk is longer than limit") for lines above the asyncio
    stream limit.
    """
    import sys

    class EchoDriver(CLIDriver):
        name = "echo"
        display_name = "Echo"
        executable = sys.executable

        def build_command(self, request: RunRequest) -> list[str]:
            return [sys.executable, "-c", "print('x' * 200000)"]

        def decode_event(self, payload):  # no se usa: la salida no es JSON
            raise NotImplementedError

        def list_models(self) -> list[str]:
            return []

    driver = EchoDriver()
    request = _request(workdir=tmp_path)
    result = asyncio.run(driver.run(request))
    assert result.ok, result.error
    assert result.text == "x" * 200_000
