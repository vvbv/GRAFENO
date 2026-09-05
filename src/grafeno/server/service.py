"""REST + WebSocket server lifecycle.

Mirrors :class:`telegram.service.TelegramService`: it is constructed by
``GrafenoApp`` and run as a Textual worker. The service does NOT block
on bind errors — failures are logged and notified through the App so
the TUI keeps running even when the port is taken.

WebSocket clients connected to the service receive a stream of
``task.changed`` events polled from the on-disk task store (no App
hooks involved); the poll loop is started by :meth:`run` and cancelled
on :meth:`stop`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .. import __version__, models, paths
from ..config import ApiConfig
from ..i18n import t
from ..models import TaskState, tasks_signature
from .httpcore import HttpServer
from .ws import WsConnection, dispatch_upgrade

POLL_SECONDS = 2.0
LOG_MAX_BYTES = 1_000_000  # api.log truncated past this size


class ServerService:
    """Bind, accept and tear down the API server."""

    def __init__(self, config: ApiConfig, app: object | None = None) -> None:
        self.config = config
        self.app = app
        self.server: Optional[asyncio.AbstractServer] = None
        self.port: int = config.port
        self.http: Optional[HttpServer] = None
        self.version = __version__
        self.pid = os.getpid()
        self.start_monotonic: float = 0.0
        self._connections: list[WsConnection] = []
        self._connection_topics: dict[int, set[str]] = {}
        self._last_states: dict[str, TaskState] = {}
        self._events_task: Optional[asyncio.Task] = None
        self._logger = logging.getLogger("grafeno.api")
        self._log_path: Path = paths.api_log_path()
        self._log_startup_token_error()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Bind the server and serve until cancelled."""
        self.http = HttpServer(self._handle_request)
        try:
            self.server, self.port = await self.http.serve(self.config.host, self.config.port)
        except OSError as exc:
            self._log(f"bind failed: {exc}")
            self._notify(t("api.failed", error=exc))
            return
        self.start_monotonic = time.monotonic()
        self._log(f"listening on {self.config.host}:{self.port}")
        self._notify(t("api.started", host=self.config.host, port=self.port))
        self._events_task = asyncio.create_task(self._events_loop())
        try:
            async with self.server:
                await self.server.serve_forever()
        except asyncio.CancelledError:
            self._log("server cancelled")
            raise
        finally:
            await self._shutdown()

    def stop(self) -> None:
        """Request graceful shutdown. Idempotent and non-blocking."""
        if self.http is not None:
            self.http.close()
        if self._events_task is not None and not self._events_task.done():
            self._events_task.cancel()

    async def _shutdown(self) -> None:
        if self._events_task is not None and not self._events_task.done():
            self._events_task.cancel()
            try:
                await self._events_task
            except (asyncio.CancelledError, Exception):
                pass
        for conn in list(self._connections):
            await conn.send_close()
        self._connections.clear()
        self._connection_topics.clear()
        self._log("server stopped")
        self._notify(t("api.stopped"))

    # ------------------------------------------------------------------ #
    # Request dispatch
    # ------------------------------------------------------------------ #
    async def _handle_request(self, request):
        from .auth import AuthError, check

        try:
            check(self.config, request.headers, request.query)
        except AuthError as exc:
            self._log(f"request denied: {exc}")
            from .httpcore import Response

            return Response.json(401, {"error": "unauthorized"}), None
        try:
            return await dispatch_upgrade(self, request)
        except Exception as exc:  # noqa: BLE001
            self._log(f"unhandled dispatch error: {exc}")
            from .httpcore import Response

            return Response.json(500, {"error": "internal error"}), None

    # ------------------------------------------------------------------ #
    # WebSocket connections
    # ------------------------------------------------------------------ #
    def add_connection(self, conn: WsConnection) -> None:
        self._connections.append(conn)
        self._connection_topics[id(conn)] = set()

    def remove_connection(self, conn: WsConnection) -> None:
        if conn in self._connections:
            self._connections.remove(conn)
        self._connection_topics.pop(id(conn), None)

    def set_topics(self, conn: WsConnection, topics: set[str]) -> None:
        self._connection_topics[id(conn)] = set(topics)

    def topics_of(self, conn: WsConnection) -> set[str]:
        return self._connection_topics.get(id(conn), set())

    async def _events_loop(self) -> None:
        """Poll the task store and broadcast ``task.changed`` events."""
        from .actions import task_summary

        try:
            while True:
                try:
                    await asyncio.sleep(POLL_SECONDS)
                except asyncio.CancelledError:
                    return
                try:
                    sig = tasks_signature()
                except Exception as exc:  # noqa: BLE001
                    self._log(f"signature poll failed: {exc}")
                    continue
                if not self._last_states:
                    self._last_states = {task.id: task.state for task in models.list_all()}
                    continue
                current = {task.id: task.state for task in models.list_all()}
                changed_ids = [
                    task_id
                    for task_id, state in current.items()
                    if self._last_states.get(task_id) is not state
                ]
                if not changed_ids:
                    self._last_states = current
                    continue
                self._last_states = current
                if not self._connections:
                    continue
                changed = [task for task in models.list_all() if task.id in changed_ids]
                for task in changed:
                    await self._broadcast_event("task.changed", {"event": "task.changed", "task": task_summary(task)})
        except asyncio.CancelledError:
            return

    async def _broadcast_event(self, topic: str, payload: dict) -> None:
        for conn in list(self._connections):
            if topic not in self._connection_topics.get(id(conn), set()):
                continue
            try:
                await conn.send_json(payload)
            except Exception:  # noqa: BLE001
                self.remove_connection(conn)

    # ------------------------------------------------------------------ #
    # Logging / notification
    # ------------------------------------------------------------------ #
    def _log(self, message: str) -> None:
        """Best-effort append to ``api.log``; never logs tokens."""
        try:
            if self._log_path.exists() and self._log_path.stat().st_size > LOG_MAX_BYTES:
                content = self._log_path.read_text(encoding="utf-8", errors="replace")
                self._log_path.write_text(content[-LOG_MAX_BYTES // 2:], encoding="utf-8")
            with self._log_path.open("a", encoding="utf-8") as handle:
                stamp = datetime.now().isoformat(timespec="seconds")
                handle.write(f"{stamp} {message}\n")
        except OSError:
            pass

    def _log_startup_token_error(self) -> None:
        if not self.config.resolve_tokens():
            self._log("warning: no tokens configured (deny all)")

    def _notify(self, message: str) -> None:
        """Surface a one-line message to the App (if available)."""
        if self.app is None:
            return
        notify = getattr(self.app, "notify", None)
        if callable(notify):
            try:
                notify(message)
            except Exception:
                pass
