"""Telegram bot service: polling loop, intent dispatch and task creation.

Runs as a Textual worker inside the TUI (started by the App when the
integration is enabled). Voice notes are transcribed (STT), the text is
interpreted by an agent CLI (intents) and the bot proposes the resulting
tasks with inline Create/Cancel buttons. Confirmed tasks are created in
automode and scheduled for "now", so the App scheduler tick starts them
unattended — the same path as trigger tasks. Everything is best effort:
no failure of the bot ever breaks the TUI or the pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .. import _toml, media, models, paths
from ..config import TelegramConfig
from ..drivers import get_driver
from ..drivers.base import CLIDriver, RunRequest
from ..i18n import t, t_lang
from ..models import Task, state_label
from ..timefmt import format_duration
from . import intents, stt, tts
from .api import TelegramBotClient, TelegramError, TgMessage, Update
from .intents import Intent, TaskSpec

ORIGIN_TELEGRAM = "telegram"   # Task.origin of bot-created tasks
PROPOSAL_TTL = 1800.0          # seconds a Create/Cancel proposal stays valid
ATTACHMENT_TTL = 600.0         # seconds a buffered photo/video stays pending
MAX_ATTACHMENTS = 5            # per chat
ASK_CONTEXT_CHARS = 20000      # artifact context budget for the "ask" action
CALLBACK_PREFIX = "tg:"        # callback_data prefix: tg:c:<id> / tg:x:<id>
TYPING_REFRESH = 4.0           # chat actions last ~5s on the clients
UNAUTHORIZED_COOLDOWN = 300.0  # seconds between "not authorized" notices
LOG_MAX_BYTES = 1_000_000      # telegram.log is truncated past this size

# Ask prompt: Spanish, like the rest of the pipeline prompts (prompts.py).
_ASK_PROMPT = """Eres un asistente que responde preguntas sobre una tarea de GRAFENO
(orquestador de tareas de programación). Usa SOLO el contexto de la tarea
para responder; si la respuesta no está en el contexto, dilo claramente.
Responde en el idioma de la pregunta, de forma concisa (es un chat de
Telegram): sin Markdown complejo ni emojis.

Contexto de la tarea:
\"\"\"
{context}
\"\"\"

Pregunta del usuario:
\"\"\"
{question}
\"\"\"
"""


@dataclass
class _State:
    """Persisted bot state (telegram-state.toml)."""

    offset: int = 0                       # last processed update id + 1
    chats: dict[str, int] = field(default_factory=dict)  # task_id -> chat_id


def _load_state() -> _State:
    path = paths.telegram_state_path()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return _State()
    chats = data.get("chats", {})
    return _State(
        offset=int(data.get("offset", 0) or 0),
        chats={str(k): int(v) for k, v in chats.items()} if isinstance(chats, dict) else {},
    )


def _save_state(state: _State) -> None:
    """Best-effort persistence; a write failure never breaks the bot."""
    try:
        paths.telegram_state_path().write_text(
            _toml.dumps({"offset": state.offset, "chats": dict(state.chats)}),
            encoding="utf-8",
        )
    except OSError:
        pass


@dataclass
class PendingProposal:
    """Task proposal waiting for the user's inline-button confirmation."""

    id: str
    chat_id: int
    specs: list[TaskSpec]
    created_at: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_at > PROPOSAL_TTL


class TelegramService:
    """Long-polling bot loop plus intent dispatch; constructed by the App."""

    def __init__(
        self,
        cfg: TelegramConfig,
        *,
        client: TelegramBotClient | None = None,
        default_workdir: str = "",
        parser_cli: str = "",
        parser_model: str = "",
        on_info: Callable[[str], None] | None = None,
    ):
        self.cfg = cfg
        self.client = client or TelegramBotClient(cfg.resolve_token())
        self.default_workdir = default_workdir
        self.parser_cli = parser_cli
        self.parser_model = parser_model
        self._on_info = on_info or (lambda message: None)
        self._state = _load_state()
        self._proposals: dict[str, PendingProposal] = {}
        self._attachments: dict[int, list[tuple[str, bytes, float]]] = {}
        self._send_locks: dict[int, asyncio.Lock] = {}
        self._bot_username = ""  # filled from getMe at startup
        self._bot_id = 0         # bot user id (reply detection in groups)
        self._unauth_notified: dict[int, float] = {}  # chat_id -> last notice
        self._chat_lang: dict[int, str] = {}  # chat_id -> language of its last message

    # ------------------------------------------------------------------ #
    # Polling loop
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Long-polling loop with backoff; runs until the worker is cancelled."""
        try:
            me = await asyncio.to_thread(self.client.get_me)
        except TelegramError as exc:
            if exc.cert_error:
                self._on_info(t("tg.ssl_error"))
            self._on_info(t("tg.auth_failed", error=exc))
            return  # bad token or no network at startup: do not spin
        if isinstance(me, dict):
            self._bot_username = str(me.get("username", ""))
            self._bot_id = int(me.get("id", 0) or 0)
        self._on_info(t("tg.started"))
        self._log(f"bot started as @{self._bot_username} (id {self._bot_id})")
        backoff = 1.0
        while True:
            try:
                updates = await asyncio.to_thread(self.client.get_updates, self._state.offset)
                backoff = 1.0
            except TelegramError as exc:
                if exc.status == 401:
                    self._on_info(t("tg.auth_failed", error=exc))
                    return
                if exc.cert_error:
                    # Certificate errors need user action (CA bundle); spinning
                    # would just repeat the failure.
                    self._on_info(t("tg.ssl_error"))
                    self._on_info(t("tg.auth_failed", error=exc))
                    return
                await asyncio.sleep(min(exc.retry_after or backoff, 30.0))
                backoff = min(backoff * 2, 30.0)
                continue
            for update in updates:
                self._state.offset = max(self._state.offset, update.update_id + 1)
                try:
                    await self._handle_update(update)
                except Exception as exc:  # noqa: BLE001 - one bad update never stops the bot
                    self._on_info(t("tg.update_error", error=exc))
            if updates:
                _save_state(self._state)

    # ------------------------------------------------------------------ #
    # Update handling
    # ------------------------------------------------------------------ #
    def _log(self, message: str) -> None:
        """Append a timestamped line to telegram.log (best effort, size-capped).

        Gives visibility into what the bot received and decided — the TUI
        notifications alone are easy to miss. The token is never logged.
        """
        try:
            path = paths.telegram_log_path()
            if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
                content = path.read_text(encoding="utf-8", errors="replace")
                path.write_text(content[-LOG_MAX_BYTES // 2:], encoding="utf-8")
            with path.open("a", encoding="utf-8") as handle:
                stamp = datetime.now().isoformat(timespec="seconds")
                handle.write(f"{stamp} {message}\n")
        except OSError:
            pass

    def _allowed(self, chat_id: int) -> bool:
        """Whitelist check; empty whitelist denies everyone."""
        return chat_id in self.cfg.chat_ids()

    def _tt(self, chat_id: int, key: str, **kwargs: Any) -> str:
        """Translate for a chat: language of its last message, else UI language."""
        lang = self._chat_lang.get(chat_id, "")
        return t_lang(lang, key, **kwargs) if lang else t(key, **kwargs)

    def _state_label(self, chat_id: int, task: Task) -> str:
        """Task state label in the chat's language."""
        lang = self._chat_lang.get(chat_id, "")
        if lang:
            return t_lang(lang, f"state.{task.state.value}")
        return state_label(task.state)

    async def _typing_loop(self, chat_id: int, action: str, stop: asyncio.Event) -> None:
        """Refresh the chat action every TYPING_REFRESH until ``stop`` is set."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=TYPING_REFRESH)
            except TimeoutError:
                pass  # timeout = still working: refresh the indicator
            if stop.is_set():
                break
            try:
                await asyncio.to_thread(self.client.send_chat_action, chat_id, action)
            except TelegramError:
                pass  # cosmetic indicator: never report

    @contextlib.asynccontextmanager
    async def _typing(self, chat_id: int, action: str = "typing"):
        """Show the "typing…" (or upload) indicator while the block runs.

        The action is sent immediately (clients display it ~5s) and then
        refreshed in the background until the block exits.
        """
        stop = asyncio.Event()
        try:
            await asyncio.to_thread(self.client.send_chat_action, chat_id, action)
        except TelegramError:
            pass
        task = asyncio.create_task(self._typing_loop(chat_id, action, stop))
        try:
            yield
        finally:
            stop.set()
            await asyncio.gather(task, return_exceptions=True)

    def _clean_text(self, text: str) -> str:
        """Strip the bot mention (@bot) that group messages carry."""
        if self._bot_username:
            text = text.replace(f"@{self._bot_username}", "")
        return text.strip()

    def _addressed_to_bot(self, message: TgMessage) -> bool:
        """Group gating: group chatter must not reach the parser CLI.

        In groups/supergroups the bot only processes commands, messages
        that mention it, replies to its own messages and voice notes (they
        cannot carry mentions: in the bot's group they are deliberate).
        This matters once privacy mode is disabled (BotFather /setprivacy),
        when every group message is delivered to the bot.
        """
        if message.chat_type == "private":
            return True
        if message.voice_file_id:
            return True
        text = (message.text or message.caption).strip()
        if text.startswith("/"):
            return True
        if self._bot_username and f"@{self._bot_username}" in text:
            return True
        return bool(message.reply_to_from_id) and message.reply_to_from_id == self._bot_id

    async def _notify_unauthorized(self, chat_id: int) -> None:
        """Reply once per cooldown with the chat id so users can authorize."""
        now = time.monotonic()
        if now - self._unauth_notified.get(chat_id, -UNAUTHORIZED_COOLDOWN) < UNAUTHORIZED_COOLDOWN:
            return
        self._unauth_notified[chat_id] = now
        await self._send(chat_id, t("tg.unauthorized", chat_id=chat_id))

    async def _handle_update(self, update: Update) -> None:
        if update.callback is not None:
            callback = update.callback
            if not self._allowed(callback.chat_id):
                try:
                    await asyncio.to_thread(
                        self.client.answer_callback_query,
                        callback.id,
                        t("tg.unauthorized", chat_id=callback.chat_id)[:190],
                    )
                except TelegramError:
                    pass
                return
            try:
                await asyncio.to_thread(self.client.answer_callback_query, callback.id)
            except TelegramError:
                pass  # best effort: the spinner may keep spinning once
            await self._handle_callback(callback.data, callback.chat_id)
        elif update.message is not None:
            await self._handle_message(update.message)

    async def _handle_message(self, message: TgMessage) -> None:
        preview = (message.text or message.caption or
                   ("voice" if message.voice_file_id else
                    "photo" if message.photo_file_id else
                    "video" if message.video_file_id else ""))
        self._log(
            f"message chat={message.chat_id} type={message.chat_type} "
            f"from={message.from_id}: {preview[:80]!r}"
        )
        text = self._clean_text(message.text)
        if text.startswith("/start") or text.startswith("/chatid"):
            # Answered even to non-whitelisted chats so users can learn their id.
            await self._send(message.chat_id, t("tg.start", chat_id=message.chat_id))
            return
        if not self._allowed(message.chat_id):
            self._log(f"chat {message.chat_id} not whitelisted: notified")
            await self._notify_unauthorized(message.chat_id)
            return
        if not self._addressed_to_bot(message):
            self._log(f"chat {message.chat_id}: group message not addressed to the bot, ignored")
            return
        if message.voice_file_id:
            await self._handle_voice(message)
            return
        if message.photo_file_id:
            await self._buffer_attachment(message.chat_id, message.photo_file_id, "photo.jpg")
        if message.video_file_id:
            await self._buffer_attachment(
                message.chat_id, message.video_file_id, message.video_name or "video.mp4"
            )
        text = text or self._clean_text(message.caption)
        if text:
            await self._parse_and_reply(message.chat_id, text)
        elif message.photo_file_id or message.video_file_id:
            await self._send(message.chat_id, self._tt(message.chat_id, "tg.attachment.pending"))

    async def _handle_voice(self, message: TgMessage) -> None:
        """Download a voice note, transcribe it and treat the text as input."""
        chat_id = message.chat_id
        async with self._typing(chat_id):
            try:
                file_path = await asyncio.to_thread(self.client.get_file_path, message.voice_file_id)
                data = await asyncio.to_thread(self.client.download_file, file_path)
            except TelegramError as exc:
                await self._send(chat_id, self._tt(chat_id, "tg.download_failed", error=exc))
                return
            key = self.cfg.resolve_stt_key()
            if not key:
                await self._send(chat_id, self._tt(chat_id, "tg.stt.not_configured"))
                return
            reasons: list[str] = []
            text = await asyncio.to_thread(
                stt.transcribe,
                url=self.cfg.stt_url,
                api_key=key,
                model=self.cfg.stt_model,
                data=data,
                filename="voice.ogg",
                on_error=reasons.append,
            )
        if not text:
            if reasons:
                self._log(f"stt failed for chat {chat_id}: {reasons[0]}")
                await self._send(chat_id, self._tt(chat_id, "tg.stt.failed_reason", error=reasons[0]))
            else:
                await self._send(chat_id, self._tt(chat_id, "tg.stt.failed"))
            return
        # The transcription notice goes out in the detected message language.
        await self._parse_and_reply(chat_id, text, heard=text)

    async def _buffer_attachment(self, chat_id: int, file_id: str, fallback_name: str) -> None:
        """Download a photo/video and buffer it for the next created task."""
        async with self._typing(chat_id):
            try:
                file_path = await asyncio.to_thread(self.client.get_file_path, file_id)
                data = await asyncio.to_thread(self.client.download_file, file_path)
            except TelegramError as exc:
                await self._send(chat_id, t("tg.download_failed", error=exc))
                return
        suffix = Path(file_path).suffix.lower()
        name = f"attachment{suffix}" if suffix else fallback_name
        entries = self._attachments.setdefault(chat_id, [])
        now = time.monotonic()
        entries[:] = [entry for entry in entries if now - entry[2] < ATTACHMENT_TTL]
        if len(entries) >= MAX_ATTACHMENTS:
            entries.pop(0)
        entries.append((name, data, now))

    def _take_attachments(self, chat_id: int) -> list[tuple[str, bytes]]:
        """Drain the pending attachments of a chat (expired ones dropped)."""
        now = time.monotonic()
        entries = self._attachments.pop(chat_id, [])
        return [(name, data) for name, data, ts in entries if now - ts < ATTACHMENT_TTL]

    # ------------------------------------------------------------------ #
    # Intent dispatch
    # ------------------------------------------------------------------ #
    def _parser_driver(self) -> CLIDriver | None:
        try:
            driver = get_driver(self.parser_cli)
        except (KeyError, NotImplementedError):
            return None
        return driver if driver.is_available() else None

    def _run_workdir(self) -> Path:
        """cwd for the parser/ask one-shot runs (must exist)."""
        candidate = Path(self.default_workdir or ".")
        return candidate if candidate.is_dir() else Path(".")

    async def _parse_and_reply(self, chat_id: int, text: str, *, heard: str | None = None) -> None:
        tasks = models.list_all()
        driver = self._parser_driver()
        if driver is None:
            if heard is not None:
                await self._send(chat_id, self._tt(chat_id, "tg.heard", text=heard))
            await self._send(chat_id, self._tt(chat_id, "tg.parser_unavailable", cli=self.parser_cli))
            return
        async with self._typing(chat_id):
            intent = await intents.parse_intent(
                driver,
                self.parser_model,
                text,
                intents.tasks_summary(tasks),
                self._run_workdir(),
                default_workdir=self.default_workdir,
            )
        if intent.lang:
            self._chat_lang[chat_id] = intent.lang  # answer in the user's language
        if heard is not None:
            await self._send(chat_id, self._tt(chat_id, "tg.heard", text=heard))
        if intent.error:
            # Infrastructure failure (timeout, crash): surface it, don't
            # hide it behind the generic help text.
            self._log(f"parser error for chat {chat_id}: {intent.error}")
            await self._send(chat_id, self._tt(chat_id, "tg.parser_error", error=intent.error))
            return
        self._log(f"chat {chat_id}: intent={intent.action} tasks={len(intent.tasks)} ref={intent.task_ref!r}")
        await self._dispatch(chat_id, intent, tasks)

    async def _dispatch(self, chat_id: int, intent: Intent, tasks: list[Task]) -> None:
        if intent.action == "create_tasks":
            await self._propose_or_create(chat_id, intent.tasks)
        elif intent.action == "list_tasks":
            await self._send(chat_id, self._format_task_list(chat_id, tasks))
        elif intent.action == "task_status":
            await self._send_status(chat_id, intent, tasks)
        elif intent.action == "send_files":
            await self._send_files(chat_id, intent, tasks)
        elif intent.action == "ask":
            await self._answer_question(chat_id, intent, tasks)
        else:  # help | unknown
            await self._send(chat_id, self._tt(chat_id, "tg.help"))

    def _format_task_list(self, chat_id: int, tasks: list[Task]) -> str:
        if not tasks:
            return self._tt(chat_id, "tg.list.empty")
        items = "\n".join(
            self._tt(
                chat_id, "tg.list.item",
                name=task.name, state=self._state_label(chat_id, task), id=task.id,
            )
            for task in tasks[:10]
        )
        return self._tt(chat_id, "tg.list.header", items=items)

    async def _send_status(self, chat_id: int, intent: Intent, tasks: list[Task]) -> None:
        task = intents.fuzzy_find_task(intent.task_ref, tasks)
        if task is None:
            await self._send(chat_id, self._tt(chat_id, "tg.task_not_found", ref=intent.task_ref))
            return
        await self._send(
            chat_id,
            self._tt(
                chat_id,
                "tg.status",
                name=task.name,
                state=self._state_label(chat_id, task),
                workdir=task.workdir,
                iteration=task.iteration,
                duration=format_duration(task.total_duration_seconds()),
            ),
        )

    @staticmethod
    def _artifact_files(task: Task) -> list[Path]:
        """Plan/review/final .md files across every cycle, in order."""
        files: list[Path] = []
        for cycle in range(1, task.cycle + 1):
            for getter in (paths.plan_dir, paths.review_dir, paths.final_dir):
                directory = getter(task.id, cycle)
                if directory.is_dir():
                    files.extend(sorted(directory.glob("*.md")))
        return files

    async def _send_files(self, chat_id: int, intent: Intent, tasks: list[Task]) -> None:
        task = intents.fuzzy_find_task(intent.task_ref, tasks)
        if task is None:
            await self._send(chat_id, self._tt(chat_id, "tg.task_not_found", ref=intent.task_ref))
            return
        files = self._artifact_files(task)
        if not files:
            await self._send(chat_id, self._tt(chat_id, "tg.files.none", name=task.name))
            return
        await self._send(chat_id, self._tt(chat_id, "tg.files.sent", count=len(files), name=task.name))
        async with self._typing(chat_id, action="upload_document"):
            for path in files[:20]:
                try:
                    await asyncio.to_thread(self.client.send_document, chat_id, path)
                except (TelegramError, OSError) as exc:
                    self._on_info(t("tg.send_failed", error=exc))

    async def _answer_question(self, chat_id: int, intent: Intent, tasks: list[Task]) -> None:
        task = intents.fuzzy_find_task(intent.task_ref, tasks)
        if task is None:
            await self._send(chat_id, self._tt(chat_id, "tg.task_not_found", ref=intent.task_ref))
            return
        driver = self._parser_driver()
        if driver is None:
            await self._send(chat_id, self._tt(chat_id, "tg.parser_unavailable", cli=self.parser_cli))
            return
        context = self._task_context(task)
        prompt = _ASK_PROMPT.format(context=context, question=intent.question)
        async with self._typing(chat_id):
            try:
                result = await driver.run(
                    RunRequest(
                        prompt=prompt,
                        model=self.parser_model,
                        workdir=self._run_workdir(),
                        title="grafeno:telegram:ask",
                    )
                )
            except Exception:  # noqa: BLE001 - the bot never propagates CLI errors
                result = None
        answer = result.text.strip() if result is not None and result.ok else ""
        await self._send(chat_id, answer or self._tt(chat_id, "tg.ask.failed"))

    def _task_context(self, task: Task) -> str:
        """Name, description, state and truncated artifacts of a task."""
        parts = [
            f"# {task.name}",
            f"Estado: {state_label(task.state)}",
            f"Directorio: {task.workdir}",
            f"Descripción:\n{task.description}",
        ]
        budget = ASK_CONTEXT_CHARS
        for path in self._artifact_files(task):
            if budget <= 0:
                break
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            chunk = content[:budget]
            budget -= len(chunk)
            parts.append(f"## {path.name}\n{chunk}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ #
    # Task creation
    # ------------------------------------------------------------------ #
    async def _propose_or_create(self, chat_id: int, specs: list[TaskSpec]) -> None:
        if not self.cfg.confirm_create:
            created, errors = self._create_tasks(chat_id, specs)
            await self._send(chat_id, self._format_created(chat_id, created, errors))
            return
        proposal = PendingProposal(id=secrets.token_hex(4), chat_id=chat_id, specs=specs)
        self._proposals[proposal.id] = proposal
        markup = {
            "inline_keyboard": [
                [
                    {"text": self._tt(chat_id, "tg.btn.create"), "callback_data": f"{CALLBACK_PREFIX}c:{proposal.id}"},
                    {"text": self._tt(chat_id, "tg.btn.cancel"), "callback_data": f"{CALLBACK_PREFIX}x:{proposal.id}"},
                ]
            ]
        }
        await self._send(chat_id, self._format_proposal(chat_id, specs), reply_markup=markup)

    def _format_proposal(self, chat_id: int, specs: list[TaskSpec]) -> str:
        items = []
        for spec in specs:
            workdir = spec.workdir.strip() or self.default_workdir or "."
            items.append(
                self._tt(
                    chat_id,
                    "tg.proposal.item",
                    name=spec.name,
                    workdir=workdir,
                    test_command=spec.test_command or "-",
                    description=spec.description or "-",
                )
            )
        return self._tt(chat_id, "tg.proposal.title", count=len(specs)) + "\n" + "\n".join(items)

    async def _handle_callback(self, data: str, chat_id: int) -> None:
        if not data.startswith(CALLBACK_PREFIX):
            return
        parts = data[len(CALLBACK_PREFIX):].split(":", 1)
        if len(parts) != 2:
            return
        action, proposal_id = parts
        proposal = self._proposals.pop(proposal_id, None)
        if proposal is None or proposal.expired or proposal.chat_id != chat_id:
            await self._send(chat_id, self._tt(chat_id, "tg.proposal.expired"))
            return
        if action == "x":
            self._take_attachments(chat_id)  # discard buffered attachments too
            await self._send(chat_id, self._tt(chat_id, "tg.proposal.cancelled"))
            return
        if action != "c":
            return
        created, errors = self._create_tasks(chat_id, proposal.specs)
        await self._send(chat_id, self._format_created(chat_id, created, errors))

    def _create_tasks(self, chat_id: int, specs: list[TaskSpec]) -> tuple[list[Task], list[str]]:
        """Create the confirmed tasks; the scheduler tick starts them.

        The first created task receives the chat's buffered attachments
        (images as ``media/`` tokens, videos as absolute-path references).
        """
        from .. import config as config_module

        cfg = config_module.load()
        known_tasks = models.list_all()  # to normalize parser-chosen workdirs
        attachments = self._take_attachments(chat_id)
        created: list[Task] = []
        errors: list[str] = []
        for spec in specs:
            workdir = intents.resolve_workdir(spec.workdir, known_tasks, self.default_workdir)
            if not Path(workdir).is_dir():
                errors.append(self._tt(chat_id, "tg.bad_workdir", workdir=workdir))
                continue
            try:
                task = models.Task.create(
                    spec.name.strip(),
                    spec.description.strip(),
                    workdir,
                    cfg,
                    automode=True,
                    confirm_plan=False,
                    test_command=spec.test_command.strip() or None,
                    scheduled_at=datetime.now().isoformat(timespec="minutes"),
                )
                task.origin = ORIGIN_TELEGRAM
                models.save(task)
            except Exception:  # noqa: BLE001 - one bad spec does not stop the rest
                errors.append(spec.name)
                continue
            self._state.chats[task.id] = chat_id
            created.append(task)
            self._log(f"task created: {task.id} ({task.name}) for chat {chat_id}")
            if attachments and len(created) == 1:
                self._attach_to_task(task, attachments)
        if created:
            _save_state(self._state)
        return created, errors

    @staticmethod
    def _attach_to_task(task: Task, attachments: list[tuple[str, bytes]]) -> None:
        """Save buffered attachments into the task's media dir and reference
        them in the description (``media/`` tokens for images, absolute paths
        for videos/other files). Best effort: failures only drop the file."""
        references: list[str] = []
        for name, data in attachments:
            saved = media.save_attachment(task.id, name, data)
            if saved is None:
                continue
            if saved.suffix.lower() in media.IMAGE_SUFFIXES:
                references.append(f"- media/{saved.name}")
            else:
                references.append(f"- {saved}")
        if references:
            task.description += (
                "\n\nAdjuntos recibidos por Telegram:\n" + "\n".join(references) + "\n"
            )
            models.save(task)

    def _format_created(self, chat_id: int, created: list[Task], errors: list[str]) -> str:
        parts: list[str] = []
        if created:
            items = "\n".join(
                self._tt(chat_id, "tg.created.item", name=task.name, workdir=task.workdir)
                for task in created
            )
            parts.append(self._tt(chat_id, "tg.created", count=len(created), items=items))
        if errors:
            parts.append(self._tt(chat_id, "tg.create.failed", names="; ".join(errors)))
        return "\n".join(parts) if parts else self._tt(chat_id, "tg.create.failed", names="?")

    # ------------------------------------------------------------------ #
    # Replies (text + optional TTS voice)
    # ------------------------------------------------------------------ #
    async def _send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        """Send a text reply; with TTS enabled, also a generated voice note."""
        lock = self._send_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            try:
                await asyncio.to_thread(
                    self.client.send_message, chat_id, text, reply_markup=reply_markup
                )
            except TelegramError as exc:
                self._on_info(t("tg.send_failed", error=exc))
                if exc.cert_error:
                    self._on_info(t("tg.ssl_error"))
                return
        if self.cfg.tts_enabled:
            await self._send_voice(chat_id, text)

    async def _send_voice(self, chat_id: int, text: str) -> None:
        """Best-effort TTS voice reply (never reports errors to the chat)."""
        key = self.cfg.resolve_tts_key()
        if not key:
            return
        audio = await asyncio.to_thread(
            tts.synthesize,
            url=self.cfg.tts_url,
            api_key=key,
            model=self.cfg.tts_model,
            voice=self.cfg.tts_voice,
            text=text,
        )
        if not audio:
            return
        try:
            await asyncio.to_thread(self.client.send_voice, chat_id, audio)
        except TelegramError as exc:
            self._on_info(t("tg.send_failed", error=exc))

    # ------------------------------------------------------------------ #
    # Notifications from the App
    # ------------------------------------------------------------------ #
    async def notify_task_finished(self, task: Task) -> None:
        """Notify the originating chat when a bot-created task finishes."""
        chat_id = self._state.chats.get(task.id)
        if chat_id is None:
            return
        await self._send(
            chat_id,
            self._tt(
                chat_id,
                "tg.finished",
                name=task.name,
                state=self._state_label(chat_id, task),
                duration=format_duration(task.total_duration_seconds()),
            ),
        )
        finals = sorted(paths.final_dir(task.id, task.cycle).glob("*.md"))
        for path in finals[:3]:  # the final report as a document, if present
            try:
                await asyncio.to_thread(self.client.send_document, chat_id, path)
            except (TelegramError, OSError) as exc:
                self._on_info(t("tg.send_failed", error=exc))
