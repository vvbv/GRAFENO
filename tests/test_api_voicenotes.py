"""Tests of POST /api/v1/audio/interpret (STT and parser CLI are faked, no network)."""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import pytest

from grafeno import config as config_module
from grafeno import models, paths
from grafeno.config import ApiConfig, Config, save as save_config
from grafeno.drivers.base import CLIDriver, RunResult
from grafeno.server import httpcore, voicenotes
from grafeno.server.service import ServerService
from grafeno.telegram import intents

TRANSCRIPTION = "arregla el login de proj por favor"


def _run(coro):
    return asyncio.run(coro)


class FakeDriver(CLIDriver):
    """Parser CLI double: returns a canned result and records every request."""

    def __init__(self, result: RunResult | None = None, available: bool = True, delay: float = 0.0):
        self.name = "fake"
        self.display_name = "fake"
        self.executable = "fake"
        self._available = available
        self._result = result
        self._delay = delay
        self.requests: list = []

    def is_available(self) -> bool:
        return self._available

    def build_command(self, request):
        return []

    def models_command(self):
        return []

    def parse_models(self, output):
        return []

    async def run(self, request, on_event=None, on_activity=None):
        self.requests.append(request)
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._result or RunResult(ok=True, text='{"tasks": []}')


class Env:
    """Wires config, fake STT and fake driver for one test."""

    def __init__(self, tmp_path, monkeypatch):
        self.ws = tmp_path / "ws"
        self.proj = self.ws / "proj"
        self.proj.mkdir(parents=True)
        self.stt_calls: list[dict] = []
        self.stt_result: Optional[str] = TRANSCRIPTION
        self.stt_reason = ""
        self.driver = FakeDriver()
        self.cli_names: list[str] = []
        self.cfg = Config()
        self.cfg.workspaces = [str(self.ws)]
        self.cfg.telegram.stt_key = "STT_KEY"
        self.cfg.telegram.parser_cli = "claude"
        self.cfg.telegram.parser_model = "modelo"
        self.cfg.telegram.parser_effort = "high"
        monkeypatch.delenv(config_module.TELEGRAM_STT_KEY_ENV, raising=False)
        monkeypatch.setattr(intents, "PARSER_RETRY_DELAY", 0)
        env = self

        def fake_transcribe(**kwargs):
            env.stt_calls.append(kwargs)
            if env.stt_result is None:
                kwargs["on_error"](env.stt_reason)
            return env.stt_result

        def fake_get_driver(name):
            env.cli_names.append(name)
            return env.driver

        monkeypatch.setattr("grafeno.telegram.stt.transcribe", fake_transcribe)
        monkeypatch.setattr(voicenotes, "get_driver", fake_get_driver)

    def save(self) -> None:
        save_config(self.cfg)

    def answer(self, tasks: list[dict]) -> None:
        self.driver._result = RunResult(ok=True, text=json.dumps({"tasks": tasks}))


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


async def _start(tokens: str = "") -> tuple[ServerService, asyncio.Task]:
    service = ServerService(ApiConfig(enabled=True, host="127.0.0.1", port=0, tokens=tokens), app=None)
    task = asyncio.create_task(service.run())
    for _ in range(200):
        if service.server is not None:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("server did not bind in time")
    return service, task


def _stop(service: ServerService, task: asyncio.Task) -> None:
    if service.http is not None:
        service.http.close()
    if not task.done():
        task.cancel()


async def _post(service: ServerService, query: str, body: bytes, content_type: str = "audio/mp4") -> tuple[int, dict]:
    raw = (
        f"POST /api/v1/audio/interpret{query} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode("ascii") + body
    reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
    try:
        writer.write(raw)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
        text = head.decode("iso-8859-1")
        status = int(text.split(" ", 2)[1])
        length = 0
        for line in text.split("\r\n")[1:]:
            key, _, value = line.partition(":")
            if key.strip().lower() == "content-length":
                length = int(value.strip())
        data = await asyncio.wait_for(reader.readexactly(length), timeout=10.0) if length else b""
        return status, json.loads(data) if data else {}
    finally:
        writer.close()
        await writer.wait_closed()


def _call(query: str = "?name=nota.m4a", body: bytes = b"audio", content_type: str = "audio/mp4"):
    async def scenario():
        service, task = await _start()
        try:
            return await _post(service, query, body, content_type)
        finally:
            _stop(service, task)

    return _run(scenario())


def test_interpret_ok_creates_nothing(env):
    env.save()
    env.answer([{"name": "Fix login", "description": "  Do it  ", "workdir": "proj"}])
    status, body = _call()
    assert status == 200
    assert body["transcription"] == TRANSCRIPTION
    assert body["tasks"] == [{
        "title": "Fix login",
        "workdir": str(env.proj.resolve()),
        "description": "Do it",
    }]
    assert models.list_all() == []
    call = env.stt_calls[0]
    assert call["filename"] == "voice.m4a"
    assert call["url"] == env.cfg.telegram.stt_url and call["model"] == env.cfg.telegram.stt_model
    request = env.driver.requests[0]
    assert request.model == "modelo" and request.effort == "high"
    assert TRANSCRIPTION in request.prompt and str(env.proj) in request.prompt


def test_interpret_uses_planner_when_parser_cli_empty(env):
    env.cfg.telegram.parser_cli = ""
    env.cfg.telegram.parser_model = ""
    env.save()
    status, _ = _call()
    assert status == 200
    assert env.cli_names == [env.cfg.planner.cli]
    assert env.driver.requests[0].model == env.cfg.planner.model


def test_interpret_no_tasks_is_ok(env):
    env.save()
    env.answer([])
    status, body = _call()
    assert status == 200 and body == {"transcription": TRANSCRIPTION, "tasks": []}


@pytest.mark.parametrize("raw,default,expected", [
    ("/no/such/dir", "", ""),
    ("user@host:/x", "", ""),
    ("rel/missing", "", ""),
    ("", "", ""),
    ("", "DEFAULT", "DEFAULT"),
    ("proj", "", "PROJ"),
    ("~nosuchuser_zz/app", "", ""),
    (None, "DEFAULT", "DEFAULT"),
])
def test_interpret_workdir_rules(env, tmp_path, raw, default, expected):
    default_dir = tmp_path / "default"
    default_dir.mkdir()
    if default:
        env.cfg.telegram.default_workdir = str(default_dir)
    env.save()
    env.answer([{"name": "T", "description": "d", "workdir": raw}])
    status, body = _call()
    assert status == 200
    want = {"DEFAULT": str(default_dir.resolve()), "PROJ": str(env.proj.resolve())}.get(expected, expected)
    assert body["tasks"][0]["workdir"] == want


def test_interpret_stt_not_configured(env):
    env.cfg.telegram.stt_key = ""
    env.save()
    status, body = _call()
    assert status == 503 and body["code"] == "stt_not_configured"
    assert env.stt_calls == []


def test_interpret_stt_failed(env):
    env.save()
    env.stt_result = None
    env.stt_reason = "HTTP 500: boom"
    status, body = _call()
    assert status == 502 and body["code"] == "stt_failed" and "HTTP 500" in body["error"]
    assert env.driver.requests == []


def test_interpret_cli_failure(env):
    env.save()
    env.driver._result = RunResult(ok=False, error="boom")
    status, body = _call()
    assert status == 502 and body["code"] == "interpret_failed"
    assert body["transcription"] == TRANSCRIPTION


def test_interpret_invalid_output(env):
    env.save()
    env.driver._result = RunResult(ok=True, text="not json")
    status, body = _call()
    assert status == 502 and body["code"] == "interpret_failed"
    assert body["transcription"] == TRANSCRIPTION


def test_interpret_timeout(env, monkeypatch):
    env.save()
    env.driver = FakeDriver(delay=5)
    monkeypatch.setattr(intents, "parser_timeout_for", lambda *a, **k: 0.01)
    status, body = _call()
    assert status == 502 and body["code"] == "interpret_failed" and "timeout" in body["error"]


def test_interpret_parser_unavailable(env):
    env.save()
    env.driver = FakeDriver(available=False)
    status, body = _call()
    assert status == 502 and body["code"] == "interpret_failed"
    assert "transcription" not in body
    assert env.stt_calls == []


def test_interpret_too_large(env, monkeypatch):
    env.save()
    monkeypatch.setattr(httpcore, "MAX_VOICE_NOTE_BODY", 4)
    status, body = _call(body=b"12345")
    assert status == 413 and body["code"] == "audio_too_large"
    assert env.stt_calls == []


def test_interpret_bad_requests(env):
    env.save()
    status, body = _call(query="")
    assert status == 400 and body["code"] == "audio_name_required"
    status, body = _call(body=b"")
    assert status == 400 and body["code"] == "empty_audio"
    status, body = _call(query="?name=a.pdf")
    assert status == 415 and body["code"] == "unsupported_audio"
    status, body = _call(body=b"{}", content_type="application/json")
    assert status == 415 and body["code"] == "unsupported_audio"


def test_interpret_remote_session(env, monkeypatch):
    env.save()
    monkeypatch.setattr("grafeno.remotesession.active", lambda: True)
    status, body = _call()
    assert status == 409 and body["code"] == "remote"


def test_interpret_unauthorized_before_body(env):
    env.save()

    async def scenario():
        service, task = await _start(tokens="t1")
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
            try:
                head = (
                    "POST /api/v1/audio/interpret?name=a.m4a HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    "Content-Type: audio/mp4\r\nContent-Length: 1000\r\nAuthorization: Bearer wrong\r\n\r\n"
                )
                writer.write(head.encode("ascii"))
                await writer.drain()
                data = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
                return int(data.decode("iso-8859-1").split(" ", 2)[1])
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            _stop(service, task)

    assert _run(scenario()) == 401


def test_interpret_does_not_log_transcription(env):
    env.save()
    env.answer([{"name": "T", "description": "d", "workdir": "proj"}])
    status, _ = _call()
    assert status == 200
    log = paths.api_log_path()
    if log.exists():
        assert TRANSCRIPTION not in log.read_text(encoding="utf-8")
