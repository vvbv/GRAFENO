"""Runtime of direct CLI sessions: one agent turn at a time per session.

``CliSessionManager`` lives on the App (``GrafenoApp.cli_sessions``) so a
running turn survives screen navigation and serves the TUI, the REST/WS API
and the web panel alike. Each turn runs the CLI in non-interactive mode with
``RunRequest.session_id`` set to the native session id, so the agent keeps
the context of the previous turns. Running state lives only in memory.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable, Coroutine

from . import clisessions, usage
from .clisessions import PREVIEW_CHARS, STATE_ACTIVE, STATE_CLOSED, CliSession
from .drivers import get_driver
from .drivers.base import CLIDriver, RunRequest, TokenUsage
from .i18n import t
from .models import cli_model_label
from .timefmt import format_duration

Spawn = Callable[[Coroutine[Any, Any, None], str], Any]    # (coroutine, group) -> handle with .cancel()
Listener = Callable[[str, CliSession, "dict | None"], None]  # (kind, session, entry): "changed" | "entry" | "deleted"


class SessionBusy(Exception):
    """A turn is already running in the session."""


class SessionClosed(Exception):
    """The session is closed: it must be reopened before sending."""


def _default_spawn(coro: Coroutine[Any, Any, None], group: str) -> Any:
    return asyncio.ensure_future(coro)


class CliSessionManager:
    """Starts, tracks and cancels the turns of direct CLI sessions."""

    def __init__(
        self,
        spawn: Spawn | None = None,
        driver_factory: Callable[[str], CLIDriver] = get_driver,
    ) -> None:
        self._spawn = spawn or _default_spawn
        self._driver_factory = driver_factory
        self._handles: dict[str, Any] = {}
        self._live: dict[str, CliSession] = {}
        self._started: set[str] = set()  # turns whose coroutine has begun executing
        self._finished: dict[str, asyncio.Event] = {}
        self._listeners: list[Listener] = []

    # ------------------------------------------------------------ listeners #
    def add_listener(self, listener: Listener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _emit(self, kind: str, session: CliSession, entry: dict | None = None) -> None:
        for listener in list(self._listeners):
            try:
                listener(kind, session, entry)
            except Exception:  # noqa: BLE001 - a broken listener never breaks a turn
                pass

    # -------------------------------------------------------------- queries #
    def is_running(self, session_id: str) -> bool:
        return session_id in self._handles

    def running_ids(self) -> set[str]:
        return set(self._handles)

    def get(self, session_id: str) -> CliSession:
        """Live copy while a turn runs, otherwise the stored session."""
        live = self._live.get(session_id)
        if live is not None:
            return live
        return clisessions.load(session_id)

    # ------------------------------------------------------------- commands #
    def create(
        self, workdir: str, cli: str, model: str = "", effort: str = "", name: str = ""
    ) -> CliSession:
        session = clisessions.create(workdir, cli, model=model, effort=effort, name=name)
        self._emit("changed", session)
        return session

    def send(self, session_id: str, prompt: str) -> CliSession:
        """Validate and start a turn (needs a running event loop)."""
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt is required")
        if self.is_running(session_id):
            raise SessionBusy(session_id)
        session = clisessions.load(session_id)
        if session.state == STATE_CLOSED:
            raise SessionClosed(session_id)
        session.turns += 1
        session.last_prompt = prompt[:PREVIEW_CHARS]
        session.last_error = ""
        clisessions.save(session)
        self._entry(session, "user", prompt)
        self._live[session.id] = session
        self._finished[session.id] = asyncio.Event()
        coro = self._run_turn(session, prompt)
        try:
            self._handles[session.id] = self._spawn(coro, f"grafeno-cli-session-{session.id}")
        except Exception:
            coro.close()  # never awaited
            self._live.pop(session.id, None)
            self._finished.pop(session.id, None)
            raise
        self._emit("changed", session)
        return session

    def cancel(self, session_id: str) -> bool:
        handle = self._handles.get(session_id)
        if handle is None:
            return False
        handle.cancel()
        if session_id not in self._started:
            # Cancelled before its first step: the coroutine body (and its
            # ``finally``) never runs, so close the turn here.
            session = self._live[session_id]
            self._entry(session, "info", t("cs.turn.cancelled", n=session.turns))
            self._finish(session, TokenUsage(), 0.0)
        return True

    async def wait(self, session_id: str) -> None:
        """Return when the running turn (if any) has finished."""
        event = self._finished.get(session_id)
        if event is not None:
            await event.wait()

    def rename(self, session_id: str, name: str) -> CliSession:
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        session = self.get(session_id)
        session.name = name
        clisessions.save(session)
        self._emit("changed", session)
        return session

    def close(self, session_id: str) -> CliSession:
        if self.is_running(session_id):
            raise SessionBusy(session_id)
        return self._set_state(session_id, STATE_CLOSED)

    def reopen(self, session_id: str) -> CliSession:
        return self._set_state(session_id, STATE_ACTIVE)

    def _set_state(self, session_id: str, state: str) -> CliSession:
        session = self.get(session_id)
        session.state = state
        clisessions.save(session)
        self._emit("changed", session)
        return session

    async def delete(self, session_id: str) -> bool:
        """Cancel a running turn, wait for it and remove the session."""
        try:
            session = clisessions.load(session_id)
        except (FileNotFoundError, OSError):
            return False
        self.cancel(session_id)
        await self.wait(session_id)
        ok = clisessions.delete(session_id)
        if ok:
            self._emit("deleted", session)
        return ok

    # ---------------------------------------------------------------- turns #
    def _entry(self, session: CliSession, kind: str, text: str) -> None:
        clisessions.append_entry(session.id, session.turns, kind, text)
        self._emit("entry", session, {"turn": session.turns, "kind": kind, "text": text})

    def _remember_native(self, session: CliSession, native: str) -> None:
        """Persist the native id as soon as the CLI reports it (survives a cancel)."""
        session.cli_session_id = native
        clisessions.save(session)

    async def _run_turn(self, session: CliSession, prompt: str) -> None:
        self._started.add(session.id)
        turn = session.turns
        started = time.monotonic()
        tokens = TokenUsage()
        try:
            self._entry(session, "info", t(
                "cs.turn.start", n=turn,
                agent=cli_model_label(session.cli, session.model) if session.model else session.cli,
            ))
            driver = self._driver_factory(session.cli)
            if not driver.is_available():
                session.last_error = t("cs.cli_missing", cli=driver.display_name)
                self._entry(session, "error", session.last_error)
                return
            request = RunRequest(
                prompt=prompt,
                model=session.model,
                workdir=Path(session.workdir),
                session_id=session.cli_session_id or None,
                log_path=clisessions.turn_log_path(session.id, turn),
                title=f"grafeno-session:{session.id}",
                effort=session.effort,
            )
            result = await driver.run(
                request,
                on_event=lambda event: self._entry(session, event.kind.value, event.text),
                on_session=lambda native: self._remember_native(session, native),
            )
            tokens = result.tokens
            if result.session_id:
                session.cli_session_id = result.session_id
            if result.ok:
                self._entry(session, "info", t(
                    "cs.turn.done", n=turn, duration=format_duration(time.monotonic() - started)
                ))
            else:
                session.last_error = result.error or "?"
                self._entry(session, "error", t("cs.turn.failed", n=turn, error=session.last_error))
                if result.usage_wait is not None:
                    self._entry(session, "info", t("cs.turn.usage"))
        except asyncio.CancelledError:
            self._entry(session, "info", t("cs.turn.cancelled", n=turn))
            raise
        except Exception as exc:  # noqa: BLE001 - a turn never breaks the App
            session.last_error = str(exc)
            self._entry(session, "error", t("cs.turn.failed", n=turn, error=exc))
        finally:
            self._finish(session, tokens, time.monotonic() - started)

    def _finish(self, session: CliSession, tokens: TokenUsage, elapsed: float) -> None:
        seconds = int(round(elapsed))
        session.tokens_in += tokens.input
        session.tokens_out += tokens.output
        session.seconds += seconds
        usage.record_session(
            session.workdir, session.cli, session.model, tokens.input, tokens.output, seconds
        )
        try:
            clisessions.save(session)
        except OSError:
            pass  # the session folder vanished (deleted while running)
        self._handles.pop(session.id, None)
        self._started.discard(session.id)
        self._live.pop(session.id, None)
        event = self._finished.pop(session.id, None)
        if event is not None:
            event.set()
        self._emit("changed", session)
