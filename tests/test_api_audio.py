"""Tests of the audio endpoints of the REST API.

Harnesses an in-process HTTP/1.1 server (same pattern as
``tests/test_api_rest_write.py``) and monkeypatches the TTS / STT
providers so no network traffic happens during the suite.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from typing import Any, Optional

import pytest

from grafeno.config import ApiConfig, Config, save as save_config
from grafeno.models import Task, TaskState
from grafeno.server.service import ServerService
from grafeno.tui.runtime import TaskRuntime


def _run(coro):
    return asyncio.run(coro)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


class FakeApp:
    """Minimal App stub with runtimes + runtime_for + notify + run_worker."""

    def __init__(self) -> None:
        self.runtimes: dict[str, TaskRuntime] = {}
        self.notified: list[str] = []
        self.workers: list[asyncio.Task] = []

    def runtime_for(self, task: Task) -> TaskRuntime:
        runtime = self.runtimes.get(task.id)
        if runtime is None:
            runtime = TaskRuntime(task, orchestrator_factory=_fake_orchestrator)
            self.runtimes[task.id] = runtime
        return runtime

    def notify(self, message: str, **_: Any) -> None:
        self.notified.append(message)

    def run_worker(self, coro, **_: Any):  # noqa: ANN001 - mirrors Textual signature
        """Run the coroutine in the background; emulate Textual's run_worker."""
        task = asyncio.ensure_future(coro)
        self.workers.append(task)
        return task


def _fake_orchestrator(task: Task, **callbacks):
    """Orchestrator stub: records runner invocations on the task."""
    from grafeno.pipeline.orchestrator import Orchestrator

    class _Stub:
        def __init__(self, task: Task) -> None:
            self.task = task
            self.calls: list[str] = []

        async def run_automode(self) -> None:
            self.calls.append("run_automode")
            self.task.state = TaskState.DONE

        async def run_automode_plan(self) -> None:
            self.calls.append("run_automode_plan")
            self.task.state = TaskState.PLANNED

        async def run_automode_resume(self) -> None:
            self.calls.append("run_automode_resume")
            self.task.state = TaskState.DONE

        async def run_automode_continue(self) -> None:
            self.calls.append("run_automode_continue")

        async def run_reevaluate_plan(self) -> None:
            self.calls.append("run_reevaluate_plan")

    return _Stub(task)


async def _start_service(app: Optional[FakeApp]) -> tuple[ServerService, asyncio.Task]:
    cfg = ApiConfig(enabled=True, host="127.0.0.1", port=0, tokens="")
    service = ServerService(cfg, app=app)
    task = asyncio.create_task(service.run())
    for _ in range(200):
        if service.server is not None:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("server did not bind in time")
    await asyncio.sleep(0)
    return service, task


def _stop(service: ServerService, task: asyncio.Task) -> None:
    if service.http is not None:
        service.http.close()
    if not task.done():
        task.cancel()


async def _request(
    service: ServerService,
    method: str,
    path: str,
    body: bytes = b"",
    headers: Optional[dict[str, str]] = None,
) -> tuple[int, dict]:
    extra: dict[str, str] = dict(headers or {})
    if body:
        extra["Content-Type"] = "application/json"
        extra["Content-Length"] = str(len(body))
    raw = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        + "".join(f"{k}: {v}\r\n" for k, v in extra.items())
        + "\r\n"
    ).encode("ascii") + body
    reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
    try:
        writer.write(raw)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
        status = int(head.decode("iso-8859-1").split(" ", 2)[1])
        length = 0
        for line in head.decode("iso-8859-1").split("\r\n")[1:]:
            if not line:
                continue
            key, _, value = line.partition(":")
            if key.strip().lower() == "content-length":
                length = int(value.strip())
        body_bytes = b"" if length == 0 else await asyncio.wait_for(reader.readexactly(length), timeout=5.0)
        return status, json.loads(body_bytes) if body_bytes else {}
    finally:
        writer.close()
        await writer.wait_closed()


async def _request_raw(
    service: ServerService,
    method: str,
    path: str,
    body: bytes = b"",
    headers: Optional[dict[str, str]] = None,
) -> tuple[int, dict[str, str], bytes]:
    """Like _request but returns (status, headers, raw body bytes)."""
    extra: dict[str, str] = dict(headers or {})
    if body:
        extra["Content-Type"] = "application/json"
        extra["Content-Length"] = str(len(body))
    raw = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        + "".join(f"{k}: {v}\r\n" for k, v in extra.items())
        + "\r\n"
    ).encode("ascii") + body
    reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
    try:
        writer.write(raw)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
        text = head.decode("iso-8859-1")
        status = int(text.split(" ", 2)[1])
        resp_headers: dict[str, str] = {}
        length = 0
        for line in text.split("\r\n")[1:]:
            if not line:
                continue
            key, _, value = line.partition(":")
            lk = key.strip().lower()
            lv = value.strip()
            resp_headers[lk] = lv
            if lk == "content-length":
                length = int(lv)
        body_bytes = b"" if length == 0 else await asyncio.wait_for(reader.readexactly(length), timeout=5.0)
        return status, resp_headers, body_bytes
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.fixture
def telegram_config():
    """Persist a config with fake STT/TTS keys so providers are considered configured."""
    cfg = Config()
    cfg.telegram.stt_key = "STT_KEY"
    cfg.telegram.tts_key = "TTS_KEY"
    save_config(cfg)
    return cfg


# ---------------------------------------------------------------------- #
# /api/v1/audio/speech
# ---------------------------------------------------------------------- #
def test_speech_wav_ok(monkeypatch, telegram_config):
    """WAV response from synthesize is returned as audio/wav bytes."""

    def fake_synthesize(**_kwargs):
        return b"RIFF....WAVE"

    monkeypatch.setattr("grafeno.telegram.tts.synthesize", fake_synthesize)

    async def scenario():
        service, srv_task = await _start_service(app=FakeApp())
        try:
            status, headers, audio = await _request_raw(
                service, "POST", "/api/v1/audio/speech",
                json.dumps({"text": "hola"}).encode(),
            )
            assert status == 200, headers
            assert headers.get("content-type") == "audio/wav"
            assert audio == b"RIFF....WAVE"
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_speech_ogg_ok(monkeypatch, telegram_config):
    """OGG response: WAV synthesized then to_ogg converts to OGG/OPUS bytes."""

    def fake_synthesize(**_kwargs):
        return b"RIFF"

    def fake_to_ogg(_wav):
        return b"OggS..."

    monkeypatch.setattr("grafeno.telegram.tts.synthesize", fake_synthesize)
    monkeypatch.setattr("grafeno.telegram.tts.to_ogg", fake_to_ogg)

    async def scenario():
        service, srv_task = await _start_service(app=FakeApp())
        try:
            status, headers, audio = await _request_raw(
                service, "POST", "/api/v1/audio/speech",
                json.dumps({"text": "hola", "format": "ogg"}).encode(),
            )
            assert status == 200
            assert headers.get("content-type") == "audio/ogg"
            assert audio == b"OggS..."
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_speech_ogg_unavailable(monkeypatch, telegram_config):
    """to_ogg returning None yields 503 (no ffmpeg / conversion failure)."""

    def fake_synthesize(**_kwargs):
        return b"RIFF"

    def fake_to_ogg(_wav):
        return None

    monkeypatch.setattr("grafeno.telegram.tts.synthesize", fake_synthesize)
    monkeypatch.setattr("grafeno.telegram.tts.to_ogg", fake_to_ogg)

    async def scenario():
        service, srv_task = await _start_service(app=FakeApp())
        try:
            status, payload = await _request(
                service, "POST", "/api/v1/audio/speech",
                json.dumps({"text": "hola", "format": "ogg"}).encode(),
            )
            assert status == 503
            assert "ogg conversion unavailable" in payload["error"]
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_speech_not_configured(monkeypatch):
    """No TTS key configured yields 503 (STT key does not satisfy TTS)."""
    from grafeno import config as config_module

    cfg = Config()
    cfg.telegram.stt_key = ""
    cfg.telegram.tts_key = ""
    save_config(cfg)

    async def scenario():
        service, srv_task = await _start_service(app=FakeApp())
        try:
            status, payload = await _request(
                service, "POST", "/api/v1/audio/speech",
                json.dumps({"text": "hola"}).encode(),
            )
            assert status == 503
            assert payload["error"] == "tts not configured"
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_speech_validation():
    """text is required (400); unknown format is rejected (400)."""

    async def scenario():
        service, srv_task = await _start_service(app=FakeApp())
        try:
            status, payload = await _request(
                service, "POST", "/api/v1/audio/speech",
                json.dumps({"text": ""}).encode(),
            )
            assert status == 400
            assert payload["error"] == "text is required"

            status, payload = await _request(
                service, "POST", "/api/v1/audio/speech",
                json.dumps({"text": "x", "format": "mp3"}).encode(),
            )
            assert status == 400
            assert payload["error"] == "format must be wav or ogg"
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_speech_provider_error(monkeypatch, telegram_config):
    """Provider failure reported via on_error becomes 502 with that reason."""

    def fake_synthesize(**kwargs):
        on_error = kwargs.get("on_error")
        if on_error is not None:
            on_error("HTTP 500: boom")
        return None

    monkeypatch.setattr("grafeno.telegram.tts.synthesize", fake_synthesize)

    async def scenario():
        service, srv_task = await _start_service(app=FakeApp())
        try:
            status, payload = await _request(
                service, "POST", "/api/v1/audio/speech",
                json.dumps({"text": "hola"}).encode(),
            )
            assert status == 502
            assert "tts provider error" in payload["error"]
        finally:
            _stop(service, srv_task)

    _run(scenario())


# ---------------------------------------------------------------------- #
# POST /api/v1/tasks with audio attachments
# ---------------------------------------------------------------------- #
def test_create_task_with_audio_transcribed(tmp_path, monkeypatch, telegram_config):
    """Audio attachments are saved AND transcribed; description gains a section."""

    def fake_transcribe(**_kwargs):
        return "crea una tarea de prueba"

    monkeypatch.setattr("grafeno.telegram.stt.transcribe", fake_transcribe)

    async def scenario():
        from grafeno import models as models_module
        from grafeno import paths

        service, srv_task = await _start_service(app=FakeApp())
        try:
            body = {
                "name": "Audio transcribed",
                "workdir": str(tmp_path),
                "attachments": [
                    {
                        "name": "voice.ogg",
                        "data": base64.b64encode(b"AUDIO").decode(),
                    },
                ],
            }
            status, payload = await _request(
                service, "POST", "/api/v1/tasks", json.dumps(body).encode(),
            )
            assert status == 201, payload
            assert payload["transcriptions"] == [
                {"name": "voice.ogg", "text": "crea una tarea de prueba"},
            ]
            stored = models_module.load(payload["task"]["id"])
            assert "Transcription of audio attachment 'voice.ogg'" in stored.description
            assert "crea una tarea de prueba" in stored.description
            media_files = list(paths.media_dir(payload["task"]["id"]).iterdir())
            assert any(f.suffix == ".ogg" for f in media_files)
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_create_task_audio_transcription_best_effort(tmp_path, monkeypatch, telegram_config):
    """Transcription failure: task is created, the audio file is kept, error reported."""

    def fake_transcribe(**kwargs):
        on_error = kwargs.get("on_error")
        if on_error is not None:
            on_error("HTTP 429: rate limit")
        return None

    monkeypatch.setattr("grafeno.telegram.stt.transcribe", fake_transcribe)

    async def scenario():
        from grafeno import models as models_module
        from grafeno import paths

        service, srv_task = await _start_service(app=FakeApp())
        try:
            body = {
                "name": "Audio failed",
                "workdir": str(tmp_path),
                "attachments": [
                    {
                        "name": "voice.wav",
                        "data": base64.b64encode(b"WAVBYTES").decode(),
                    },
                ],
            }
            status, payload = await _request(
                service, "POST", "/api/v1/tasks", json.dumps(body).encode(),
            )
            assert status == 201
            assert payload["transcriptions"][0]["error"]
            assert "429" in payload["transcriptions"][0]["error"]
            stored = models_module.load(payload["task"]["id"])
            assert "Transcription of audio" not in stored.description
            media_files = list(paths.media_dir(payload["task"]["id"]).iterdir())
            assert any(f.suffix == ".wav" for f in media_files)
        finally:
            _stop(service, srv_task)

    _run(scenario())


def test_create_task_non_audio_untouched(tmp_path, monkeypatch):
    """Non-audio attachments skip STT entirely and transcriptions stay empty."""
    from grafeno import config as config_module

    cfg = Config()
    cfg.telegram.stt_key = ""
    cfg.telegram.tts_key = ""
    save_config(cfg)

    calls = {"n": 0}

    def counting_transcribe(**_kwargs):
        calls["n"] += 1
        return "should-not-be-called"

    monkeypatch.setattr("grafeno.telegram.stt.transcribe", counting_transcribe)

    async def scenario():
        service, srv_task = await _start_service(app=FakeApp())
        try:
            body = {
                "name": "No audio",
                "workdir": str(tmp_path),
                "attachments": [
                    {
                        "name": "photo.png",
                        "data": base64.b64encode(b"\x89PNG\r\n\x1a\nDATA").decode(),
                    },
                ],
            }
            status, payload = await _request(
                service, "POST", "/api/v1/tasks", json.dumps(body).encode(),
            )
            assert status == 201
            assert payload["transcriptions"] == []
            assert calls["n"] == 0
        finally:
            _stop(service, srv_task)

    _run(scenario())
