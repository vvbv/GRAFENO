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


def test_opencode_command_with_effort_flag():
    """Si ``effort`` está fijado, OpenCode recibe ``--variant <nivel>``."""
    cmd = OpenCodeDriver().build_command(_request(model="opencode-go/kimi-k3", effort="high"))
    assert cmd[cmd.index("--variant") + 1] == "high"


def test_opencode_command_without_effort_has_no_variant_flag():
    """Sin ``effort`` no aparece ``--variant`` (compatibilidad con versiones antiguas)."""
    cmd = OpenCodeDriver().build_command(_request(model="opencode-go/kimi-k3", effort=""))
    assert "--variant" not in cmd


def test_opencode_decode_text_and_session():
    driver = OpenCodeDriver()
    line = json.dumps({"type": "text", "sessionID": "ses_9", "part": {"text": "hola"}})
    event, session, _ = driver.decode_line(line)
    assert session == "ses_9"
    assert event.kind is EventKind.TEXT
    assert event.text == "hola"


def test_opencode_decode_tool_and_noise():
    driver = OpenCodeDriver()
    tool, _, _ = driver.decode_line(
        json.dumps({"type": "tool_use", "part": {"tool": "bash", "state": {"title": "ls"}}})
    )
    assert tool.kind is EventKind.TOOL
    noise, _, _ = driver.decode_line(json.dumps({"type": "step_start"}))
    assert noise is None


def test_opencode_decode_plain_fallback():
    event, session, _ = OpenCodeDriver().decode_line("salida plana")
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


def test_kimi_command_ignores_effort():
    """Kimi no soporta nivel de trabajo: su comando no añade ningún flag nuevo."""
    base = KimiDriver().build_command(_request())
    with_effort = KimiDriver().build_command(_request(effort="high"))
    assert base == with_effort
    assert "high" not in with_effort
    assert "--variant" not in with_effort


def test_opencode_parse_variants_real_output():
    """Parseo de la salida real de ``opencode models --verbose``.

    Se omiten modelos con ``variants`` vacío y se ordenan los niveles.
    """
    sample = (
        "opencode/big-pickle\n"
        "{\n"
        '  "id": "big-pickle",\n'
        '  "variants": {}\n'
        "}\n"
        "opencode/hy3-free\n"
        "{\n"
        '  "id": "hy3-free",\n'
        '  "variants": {\n'
        '    "low": {"reasoningEffort": "low"},\n'
        '    "medium": {"reasoningEffort": "medium"},\n'
        '    "high": {"reasoningEffort": "high"}\n'
        "  }\n"
        "}\n"
    )
    result = OpenCodeDriver().parse_variants(sample)
    assert "opencode/big-pickle" not in result  # variants {} se omite
    assert result["opencode/hy3-free"] == ["high", "low", "medium"]


def test_opencode_parse_variants_empty_output():
    assert OpenCodeDriver().parse_variants("") == {}


def test_kimi_variants_command_empty():
    """Kimi no expone comando de variantes."""
    assert KimiDriver().variants_command() == []


def test_kimi_parse_variants_returns_empty():
    assert KimiDriver().parse_variants("cualquier cosa") == {}


def test_kimi_decode_real_event_shapes():
    """Eventos reales capturados de kimi 0.37 --output-format stream-json."""
    driver = KimiDriver()

    event, _, _ = driver.decode_line('{"role":"assistant","content":"ok"}')
    assert event.kind is EventKind.TEXT
    assert event.text == "ok"

    event, _, _ = driver.decode_line(
        '{"role":"assistant","tool_calls":[{"type":"function","id":"t1",'
        '"function":{"name":"Write","arguments":"{}"}}]}'
    )
    assert event.kind is EventKind.TOOL
    assert "Write" in event.text

    event, _, _ = driver.decode_line('{"role":"tool","content":"Wrote 4 bytes to /tmp/x"}')
    assert event.kind is EventKind.TOOL
    assert "Wrote 4 bytes" in event.text

    event, session, _ = driver.decode_line(
        '{"role":"meta","type":"session.resume_hint","session_id":"session_abc","command":"kimi -r session_abc"}'
    )
    assert event is None
    assert session == "session_abc"

    event, _, _ = driver.decode_line('{"role":"meta","type":"system.version","version":"0.37.2"}')
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
    event, session, _ = driver.decode_line(line)
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
# AGENTS.md
# ---------------------------------------------------------------------- #
def test_agents_md_prompt_opencode():
    driver = OpenCodeDriver()
    assert driver.init_command == "/init"
    prompt = driver.build_agents_md_prompt()
    assert "AGENTS.md" in prompt
    assert "/init" in prompt


def test_agents_md_prompt_kimi():
    """kimi no expone init nativo: el prompt no menciona `/init` propio."""
    driver = KimiDriver()
    assert driver.init_command == ""
    prompt = driver.build_agents_md_prompt()
    assert "AGENTS.md" in prompt


def test_agents_md_prompt_generic_sin_init_command():
    class BareDriver(CLIDriver):
        name = "bare"

    driver = BareDriver()
    assert driver.init_command == ""
    prompt = driver.build_agents_md_prompt()
    assert "AGENTS.md" in prompt
    assert "`/init`" not in prompt.split("convenciones habituales")[0]


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


# ---------------------------------------------------------------------- #
# Listado asíncrono de modelos
# ---------------------------------------------------------------------- #
def test_list_models_async_success(monkeypatch):
    class _Proc:
        returncode = 0

        async def communicate(self):
            return (b"opencode-go/kimi-k3\nopencode/big-pickle\n", b"")

    async def _fake_exec(*cmd, **kwargs):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    models = asyncio.run(OpenCodeDriver().list_models_async())
    assert models == ["opencode-go/kimi-k3", "opencode/big-pickle"]


def test_list_models_async_spawn_error(monkeypatch):
    async def _fake_exec(*cmd, **kwargs):
        raise OSError("no existe")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    assert asyncio.run(KimiDriver().list_models_async()) == []


def test_list_models_async_cancel_kills_process(monkeypatch):
    killed = False

    class _Proc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(60)

        def kill(self):
            nonlocal killed
            killed = True

        async def wait(self):
            return -9

    async def _fake_exec(*cmd, **kwargs):
        return _Proc()

    async def scenario():
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        task = asyncio.ensure_future(OpenCodeDriver().list_models_async(timeout=60))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
            raise AssertionError("debió lanzar CancelledError")
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert killed


def test_fetch_all_models(monkeypatch):
    from grafeno import drivers

    async def _fake(self, timeout=30.0):
        return [f"{self.name}/m1"]

    monkeypatch.setattr(CLIDriver, "list_models_async", _fake)
    result = asyncio.run(drivers.fetch_all_models(["opencode", "kimi"]))
    assert result == {"opencode": ["opencode/m1"], "kimi": ["kimi/m1"]}


def test_fetch_all_variants(monkeypatch):
    """``fetch_all_variants`` agrega por CLI; un fallo devuelve dict vacío."""
    from grafeno import drivers

    async def _fake(self, timeout=30.0):
        if self.name == "kimi":
            raise OSError("kimi no soporta variantes")
        return {"prov/m1": ["high", "low"]}

    monkeypatch.setattr(CLIDriver, "list_variants_async", _fake)
    result = asyncio.run(drivers.fetch_all_variants(["opencode", "kimi"]))
    assert result["opencode"] == {"prov/m1": ["high", "low"]}
    assert result["kimi"] == {}


def test_list_variants_async_spawn_error(monkeypatch):
    async def _fake_exec(*cmd, **kwargs):
        raise OSError("no existe")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    assert asyncio.run(OpenCodeDriver().list_variants_async()) == {}


def test_list_variants_async_kimi_no_command():
    """Kimi no expone ``variants_command``: devuelve ``{}`` sin spawn."""
    assert asyncio.run(KimiDriver().list_variants_async()) == {}


# ---------------------------------------------------------------------- #
# Token usage
# ---------------------------------------------------------------------- #
def test_opencode_extract_usage_step_finish():
    driver = OpenCodeDriver()
    line = json.dumps({
        "type": "step_finish",
        "sessionID": "ses_1",
        "part": {"tokens": {"input": 1200, "output": 340, "reasoning": 5}},
    })
    event, session, usage = driver.decode_line(line)
    assert event is None            # step_finish sigue siendo ruido
    assert session == "ses_1"
    assert usage is not None
    assert usage.input == 1200
    assert usage.output == 340


def test_opencode_extract_usage_absent():
    driver = OpenCodeDriver()
    _, _, usage = driver.decode_line(json.dumps({"type": "text", "part": {"text": "x"}}))
    assert usage is None


def test_kimi_extract_usage_variants():
    driver = KimiDriver()
    _, _, usage = driver.decode_line(json.dumps({"role": "meta", "usage": {"input_tokens": 10, "output_tokens": 4}}))
    assert usage is not None and (usage.input, usage.output) == (10, 4)
    _, _, usage = driver.decode_line(json.dumps({"message": {"usage": {"prompt_tokens": 7, "completion_tokens": 3}}}))
    assert usage is not None and (usage.input, usage.output) == (7, 3)
    _, _, usage = driver.decode_line(json.dumps({"role": "assistant", "content": "hola"}))
    assert usage is None


def test_token_usage_add():
    from grafeno.drivers.base import TokenUsage

    total = TokenUsage()
    total.add(TokenUsage(input=5, output=2))
    total.add(TokenUsage(input=3, output=1))
    assert (total.input, total.output) == (8, 3)
    assert not total.empty
    assert TokenUsage().empty
