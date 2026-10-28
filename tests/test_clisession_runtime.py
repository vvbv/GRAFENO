"""Tests of the direct CLI session manager (fake driver, no subprocesses)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from grafeno import clisessions, paths
from grafeno.clisession_runtime import CliSessionManager, SessionBusy, SessionClosed
from grafeno.drivers.base import CLIDriver, EventKind, RunEvent, RunRequest, RunResult, TokenUsage


class FakeSessionDriver(CLIDriver):
    name = "claude"
    display_name = "Fake Claude"
    executable = "fake"

    def __init__(self, delay: float = 0.0, ok: bool = True, available: bool = True):
        self.requests: list[RunRequest] = []
        self.delay = delay
        self.ok = ok
        self.available = available

    def is_available(self) -> bool:
        return self.available

    async def run(self, request, on_event=None, on_activity=None, on_session=None):
        self.requests.append(request)
        if on_session:
            on_session("native-1")
        if on_event:
            on_event(RunEvent(EventKind.TEXT, f"echo: {request.prompt}"))
        if self.delay:
            await asyncio.sleep(self.delay)
        return RunResult(ok=self.ok, text="", session_id="native-1",
                         error="" if self.ok else "boom", tokens=TokenUsage(10, 5))


def _manager(driver):
    return CliSessionManager(driver_factory=lambda cli: driver)


def _project(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    return folder


def test_two_turns_resume_the_native_session(tmp_path):
    project = _project(tmp_path)
    driver = FakeSessionDriver()

    async def main():
        manager = _manager(driver)
        session = manager.create(str(project), "claude")
        manager.send(session.id, "one")
        await manager.wait(session.id)
        manager.send(session.id, "two")
        await manager.wait(session.id)
        return session.id

    session_id = asyncio.run(main())
    assert driver.requests[0].session_id is None
    assert driver.requests[1].session_id == "native-1"
    assert all(r.workdir == Path(str(project.resolve())) for r in driver.requests)
    entries, _ = clisessions.load_transcript(session_id)
    assert {"user", "text", "info"} <= {e["kind"] for e in entries}
    loaded = clisessions.load(session_id)
    assert loaded.turns == 2 and loaded.tokens_in == 20 and loaded.tokens_out == 10


def test_send_while_running_raises_busy(tmp_path):
    async def main():
        manager = _manager(FakeSessionDriver(delay=5))
        session = manager.create(str(_project(tmp_path)), "claude")
        manager.send(session.id, "one")
        with pytest.raises(SessionBusy):
            manager.send(session.id, "two")
        manager.cancel(session.id)
        await manager.wait(session.id)

    asyncio.run(main())


def test_cancel_keeps_native_session_id(tmp_path):
    async def main():
        manager = _manager(FakeSessionDriver(delay=5))
        session = manager.create(str(_project(tmp_path)), "claude")
        manager.send(session.id, "one")
        await asyncio.sleep(0.05)
        assert manager.cancel(session.id)
        await manager.wait(session.id)
        return manager, session.id

    manager, session_id = asyncio.run(main())
    assert clisessions.load(session_id).cli_session_id == "native-1"
    assert "cancelled" in clisessions.load_transcript(session_id)[0][-1]["text"]
    assert not manager.is_running(session_id)


def test_closed_session_rejects_send_and_reopen_allows_it(tmp_path):
    async def main():
        manager = _manager(FakeSessionDriver())
        session = manager.create(str(_project(tmp_path)), "claude")
        manager.close(session.id)
        with pytest.raises(SessionClosed):
            manager.send(session.id, "x")
        manager.reopen(session.id)
        manager.send(session.id, "x")
        await manager.wait(session.id)

    asyncio.run(main())


def test_failed_turn_records_last_error(tmp_path):
    async def main():
        manager = _manager(FakeSessionDriver(ok=False))
        session = manager.create(str(_project(tmp_path)), "claude")
        manager.send(session.id, "x")
        await manager.wait(session.id)
        return session.id

    session_id = asyncio.run(main())
    assert "boom" in clisessions.load(session_id).last_error
    assert "error" in {e["kind"] for e in clisessions.load_transcript(session_id)[0]}


def test_missing_cli_reports_error(tmp_path):
    async def main():
        manager = _manager(FakeSessionDriver(available=False))
        session = manager.create(str(_project(tmp_path)), "claude")
        manager.send(session.id, "x")
        await manager.wait(session.id)
        return session.id

    session_id = asyncio.run(main())
    assert "Fake Claude" in clisessions.load(session_id).last_error


def test_listeners_receive_events_and_broken_listener_is_harmless(tmp_path):
    seen: list[str] = []

    def broken(kind, session, entry):
        raise RuntimeError("boom")

    async def main():
        manager = _manager(FakeSessionDriver())
        manager.add_listener(broken)
        manager.add_listener(lambda kind, session, entry: seen.append(kind))
        session = manager.create(str(_project(tmp_path)), "claude")
        manager.send(session.id, "x")
        await manager.wait(session.id)

    asyncio.run(main())
    assert "changed" in seen and "entry" in seen


def test_delete_cancels_and_removes(tmp_path):
    seen: list[str] = []

    async def main():
        manager = _manager(FakeSessionDriver(delay=5))
        manager.add_listener(lambda kind, session, entry: seen.append(kind))
        session = manager.create(str(_project(tmp_path)), "claude")
        manager.send(session.id, "x")
        await asyncio.sleep(0.05)
        ok = await manager.delete(session.id)
        return ok, session.id

    ok, session_id = asyncio.run(main())
    assert ok is True
    assert not paths.cli_session_dir(session_id).exists()
    assert "deleted" in seen


class _NoSessionDriver(FakeSessionDriver):
    async def run(self, request, on_event=None, on_activity=None, on_session=None):
        self.requests.append(request)
        return RunResult(ok=False, text="", session_id="", error="boom", tokens=TokenUsage(0, 0))


def test_initial_context_is_prepended_once(tmp_path):
    from grafeno.i18n import t

    driver = FakeSessionDriver()

    async def main():
        manager = _manager(driver)
        session = manager.create(str(_project(tmp_path)), "claude")
        clisessions.save_initial_context(session.id, "CTX\n")
        manager.send(session.id, "hola")
        await manager.wait(session.id)
        manager.send(session.id, "dos")
        await manager.wait(session.id)
        return session.id

    session_id = asyncio.run(main())
    assert driver.requests[0].prompt == "CTX\nhola"
    assert driver.requests[1].prompt == "dos"
    assert not clisessions.has_initial_context(session_id)
    entries = clisessions.load_transcript(session_id)[0]
    assert [e["text"] for e in entries if e["kind"] == "user"] == ["hola", "dos"]
    assert any(e["kind"] == "info" and e["text"] == t("cs.context.sent") for e in entries)


def test_initial_context_survives_a_failed_turn(tmp_path):
    driver = _NoSessionDriver(ok=False)

    async def main():
        manager = _manager(driver)
        session = manager.create(str(_project(tmp_path)), "claude")
        clisessions.save_initial_context(session.id, "CTX\n")
        manager.send(session.id, "hola")
        await manager.wait(session.id)
        return session.id

    assert clisessions.has_initial_context(asyncio.run(main()))
