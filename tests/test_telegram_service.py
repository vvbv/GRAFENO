"""End-to-end tests of the Telegram bot service (fake client + fake CLI)."""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from pathlib import Path

from grafeno import models, paths
from grafeno.config import Config, TelegramConfig
from grafeno.drivers.base import CLIDriver, RunResult
from grafeno.telegram import service as service_module
from grafeno.telegram.api import TelegramError, TgMessage, Update
from grafeno.telegram.intents import TaskSpec
from grafeno.telegram.service import (
    ORIGIN_TELEGRAM,
    PendingProposal,
    TelegramService,
    _load_state,
)


class FakeClient:
    """Bot API double: records outgoing calls, serves queued files/updates."""

    def __init__(self):
        self.sent: list[tuple[int, str, dict | None]] = []
        self.documents: list[tuple[int, str]] = []
        self.voices: list[tuple[int, bytes]] = []
        self.answered: list[str] = []
        self.answer_texts: list[str] = []
        self.chat_actions: list[tuple[int, str]] = []
        self.files: dict[str, tuple[str, bytes]] = {}  # file_id -> (path, data)

    def get_me(self):
        return {"id": 1, "username": "grafeno_bot"}

    def get_updates(self, offset, *, timeout=30.0):
        return []

    def send_message(self, chat_id, text, *, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))

    def send_document(self, chat_id, path, *, caption=""):
        self.documents.append((chat_id, Path(path).name))

    def send_voice(self, chat_id, data, *, filename="voice.wav"):
        self.voices.append((chat_id, data))

    def answer_callback_query(self, callback_id, text=""):
        self.answered.append(callback_id)
        self.answer_texts.append(text)

    def send_chat_action(self, chat_id, action="typing"):
        self.chat_actions.append((chat_id, action))

    def get_file_path(self, file_id):
        return self.files[file_id][0]

    def download_file(self, file_path):
        for path, data in self.files.values():
            if path == file_path:
                return data
        raise TelegramError("not found", status=404)


class FakeDriver(CLIDriver):
    """Driver double: returns queued RunResults, records prompts."""

    def __init__(self, results: list[RunResult]):
        self.name = "fake"
        self.display_name = "fake"
        self.executable = "fake"
        self._results = deque(results)
        self.prompts: list[str] = []

    def is_available(self):
        return True

    def build_command(self, request):
        return []

    def models_command(self):
        return []

    def parse_models(self, output):
        return []

    async def run(self, request, on_event=None, on_activity=None):
        self.prompts.append(request.prompt)
        return self._results.popleft() if self._results else RunResult(ok=True, text="{}")


def _json_result(payload: dict) -> RunResult:
    return RunResult(ok=True, text=json.dumps(payload))


def _run(coro):
    return asyncio.run(coro)


def _make_service(
    tmp_path,
    monkeypatch,
    driver: FakeDriver,
    *,
    cfg: TelegramConfig | None = None,
) -> tuple[TelegramService, FakeClient]:
    """Service wired to doubles: FakeClient, FakeDriver as parser CLI."""
    client = FakeClient()
    tg = cfg or TelegramConfig(
        enabled=True,
        bot_token="TOKEN",
        allowed_chat_ids="555",
        stt_key="STT-KEY",
        default_workdir=str(tmp_path),
    )
    monkeypatch.setattr(service_module, "get_driver", lambda name: driver)
    service = TelegramService(
        tg,
        client=client,
        default_workdir=tg.default_workdir,
        parser_cli="fake",
        parser_model="",
    )
    return service, client


def _msg(**kwargs) -> TgMessage:
    defaults = {"message_id": 1, "chat_id": 555, "from_id": 42}
    return TgMessage(**(defaults | kwargs))


def _callback_update(data: str, update_id: int = 9) -> Update:
    from grafeno.telegram.api import CallbackQuery

    return Update(
        update_id=update_id,
        callback=CallbackQuery(id="cb-1", chat_id=555, message_id=1, data=data),
    )


def _proposal_id(client: FakeClient) -> str:
    """Extract the proposal id from the markup of the last message sent."""
    markup = client.sent[-1][2]
    assert markup is not None
    return markup["inline_keyboard"][0][0]["callback_data"].split(":")[-1]


# ---------------------------------------------------------------------- #
# Voice note -> proposal -> confirm -> task created
# ---------------------------------------------------------------------- #
def test_voice_note_creates_task_after_confirmation(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({
        "action": "create_tasks",
        "tasks": [{"name": "Saludar", "description": "añade un saludo"}],
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    client.files["vf"] = ("voice/file_1.oga", b"AUDIO")
    monkeypatch.setattr(
        service_module.stt, "transcribe",
        lambda **kwargs: "crea una tarea que salude",
    )

    _run(service._handle_message(_msg(voice_file_id="vf")))

    # 1) transcription notice, 2) proposal with inline buttons.
    assert len(client.sent) == 2
    assert "crea una tarea que salude" in client.sent[0][1]
    proposal_text, markup = client.sent[1][1], client.sent[1][2]
    assert "Saludar" in proposal_text
    assert markup is not None

    pid = _proposal_id(client)
    _run(service._handle_update(_callback_update(f"tg:c:{pid}")))

    tasks = models.list_all()
    assert len(tasks) == 1
    task = tasks[0]
    assert task.name == "Saludar"
    assert task.description == "añade un saludo"
    assert task.origin == ORIGIN_TELEGRAM
    assert task.automode is True
    assert task.confirm_plan is False
    assert task.scheduled_at  # the scheduler tick will start it
    assert task.workdir == str(tmp_path)
    # The chat mapping is persisted for the finish notification.
    assert _load_state().chats[task.id] == 555
    # The callback was acknowledged.
    assert client.answered == ["cb-1"]
    # The user got the confirmation message.
    assert "Saludar" in client.sent[-1][1]


def test_create_without_confirmation_when_disabled(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({
        "action": "create_tasks",
        "tasks": [{"name": "Directa", "description": "d"}],
    })])
    cfg = TelegramConfig(
        enabled=True, bot_token="T", allowed_chat_ids="555",
        confirm_create=False, default_workdir=str(tmp_path),
    )
    service, client = _make_service(tmp_path, monkeypatch, driver, cfg=cfg)

    _run(service._parse_and_reply(555, "crea una tarea directa"))

    assert [task.name for task in models.list_all()] == ["Directa"]
    assert client.sent[-1][2] is None  # no inline buttons


def test_multiple_tasks_created(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({
        "action": "create_tasks",
        "tasks": [{"name": "A"}, {"name": "B"}],
    })])
    cfg = TelegramConfig(
        enabled=True, bot_token="T", allowed_chat_ids="555",
        confirm_create=False, default_workdir=str(tmp_path),
    )
    service, _ = _make_service(tmp_path, monkeypatch, driver, cfg=cfg)

    _run(service._parse_and_reply(555, "dos tareas"))

    assert sorted(task.name for task in models.list_all()) == ["A", "B"]


def test_create_with_bad_workdir_reports_error(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({
        "action": "create_tasks",
        "tasks": [{"name": "X", "workdir": "/no/existe/esto"}],
    })])
    cfg = TelegramConfig(
        enabled=True, bot_token="T", allowed_chat_ids="555",
        confirm_create=False, default_workdir=str(tmp_path),
    )
    service, client = _make_service(tmp_path, monkeypatch, driver, cfg=cfg)

    _run(service._parse_and_reply(555, "crea"))

    assert models.list_all() == []
    assert "/no/existe/esto" in client.sent[-1][1]


def test_create_task_routes_to_existing_project_workdir(tmp_path, monkeypatch):
    """The parser-chosen workdir is normalized to the existing task's directory."""
    existing = models.Task.create("Api", "d", str(tmp_path), Config())
    models.save(existing)
    driver = FakeDriver([])
    cfg = TelegramConfig(
        enabled=True, bot_token="T", allowed_chat_ids="555",
        confirm_create=False, default_workdir="/default",
    )
    service, _ = _make_service(tmp_path, monkeypatch, driver, cfg=cfg)

    created, errors = service._create_tasks(555, [
        TaskSpec(name="Nueva", description="d", workdir=str(tmp_path).upper()),
    ])

    assert errors == []
    assert len(created) == 1
    assert created[0].workdir == str(tmp_path)


# ---------------------------------------------------------------------- #
# Whitelist and /start
# ---------------------------------------------------------------------- #
def test_unauthorized_chat_gets_hint_with_cooldown(tmp_path, monkeypatch):
    """Unauthorized chats get their chat id once per cooldown (not silence)."""
    driver = FakeDriver([])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._handle_message(_msg(chat_id=999, text="crea algo")))
    assert len(client.sent) == 1
    assert "999" in client.sent[0][1]

    _run(service._handle_message(_msg(chat_id=999, text="otra vez")))
    assert len(client.sent) == 1  # cooldown: no repeated notice
    assert driver.prompts == []  # the parser CLI is never invoked


def test_unauthorized_callback_answers_with_hint(tmp_path, monkeypatch):
    driver = FakeDriver([])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    update = _callback_update("tg:c:abcd")
    update.callback.chat_id = 999

    _run(service._handle_update(update))

    assert client.answered == ["cb-1"]
    assert "999" in client.answer_texts[0]


def test_typing_indicator_while_parsing(tmp_path, monkeypatch):
    """The bot shows 'typing…' while the parser CLI thinks."""
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "lista"))

    assert (555, "typing") in client.chat_actions


def test_typing_indicator_on_voice(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    client.files["vf"] = ("voice/file_1.oga", b"AUDIO")
    monkeypatch.setattr(service_module.stt, "transcribe", lambda **kwargs: "lista")

    _run(service._handle_message(_msg(voice_file_id="vf")))

    assert (555, "typing") in client.chat_actions


def test_bot_mention_is_stripped(tmp_path, monkeypatch):
    """Group messages mention the bot: '@bot texto' is parsed as 'texto'."""
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    service._bot_username = "GrafenoCLI_bot"

    _run(service._handle_message(_msg(text="@GrafenoCLI_bot lista mis tareas")))

    assert "@GrafenoCLI_bot" not in driver.prompts[0]
    assert "lista mis tareas" in driver.prompts[0]


# ---------------------------------------------------------------------- #
# Group gating (privacy mode off => the bot must ignore group chatter)
# ---------------------------------------------------------------------- #
def _group_msg(**kwargs) -> TgMessage:
    defaults = {"message_id": 1, "chat_id": 555, "from_id": 42, "chat_type": "supergroup"}
    return TgMessage(**(defaults | kwargs))


def test_group_chatter_is_ignored(tmp_path, monkeypatch):
    driver = FakeDriver([])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    service._bot_username = "GrafenoCLI_bot"
    service._bot_id = 1

    _run(service._handle_message(_group_msg(text="conversación entre humanos")))

    assert driver.prompts == []  # the parser CLI never runs for chatter
    assert client.sent == []


def test_group_mention_is_processed(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    service._bot_username = "GrafenoCLI_bot"
    service._bot_id = 1

    _run(service._handle_message(_group_msg(text="@GrafenoCLI_bot lista")))

    assert len(driver.prompts) == 1


def test_group_reply_to_bot_is_processed(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    service._bot_username = "GrafenoCLI_bot"
    service._bot_id = 1

    _run(service._handle_message(_group_msg(text="y eso qué?", reply_to_from_id=1)))

    assert len(driver.prompts) == 1


def test_group_reply_to_other_user_is_ignored(tmp_path, monkeypatch):
    driver = FakeDriver([])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    service._bot_username = "GrafenoCLI_bot"
    service._bot_id = 1

    _run(service._handle_message(_group_msg(text="respuesta a otra persona", reply_to_from_id=999)))

    assert driver.prompts == []


def test_group_command_is_processed(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({"action": "help"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    service._bot_username = "GrafenoCLI_bot"

    _run(service._handle_message(_group_msg(text="/help@GrafenoCLI_bot")))

    assert len(driver.prompts) == 1


def test_group_voice_note_is_processed(tmp_path, monkeypatch):
    """Voice notes cannot carry mentions: in the bot's group they are always
    addressed to it (they are deliberate dictation)."""
    driver = FakeDriver([_json_result({"action": "list_tasks", "lang": "es"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    service._bot_username = "GrafenoCLI_bot"
    service._bot_id = 1
    client.files["vf"] = ("voice/file_1.oga", b"AUDIO")
    monkeypatch.setattr(service_module.stt, "transcribe", lambda **kwargs: "lista mis tareas")

    _run(service._handle_message(_group_msg(voice_file_id="vf")))

    assert len(driver.prompts) == 1
    assert any("escuchado" in text for _, text, _ in client.sent)  # tg.heard in Spanish


# ---------------------------------------------------------------------- #
# Per-chat language
# ---------------------------------------------------------------------- #
def test_reply_follows_message_language_spanish(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({"action": "help", "lang": "es"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "ayuda por favor"))

    assert "Convierto tus mensajes" in client.sent[-1][1]  # tg.help in Spanish


def test_reply_follows_message_language_english(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({"action": "help", "lang": "en"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "help me"))

    assert "I turn your messages" in client.sent[-1][1]  # tg.help in English


def test_reply_language_persists_for_notifications(tmp_path, monkeypatch):
    """The finish notification uses the language of the chat's last message."""
    driver = FakeDriver([_json_result({
        "action": "create_tasks", "lang": "es",
        "tasks": [{"name": "Algo"}],
    })])
    cfg = TelegramConfig(
        enabled=True, bot_token="T", allowed_chat_ids="555",
        confirm_create=False, default_workdir=str(tmp_path),
    )
    service, client = _make_service(tmp_path, monkeypatch, driver, cfg=cfg)
    _run(service._parse_and_reply(555, "crea algo"))
    task = models.list_all()[0]

    _run(service.notify_task_finished(task))

    assert "Tarea terminada" in client.sent[-1][1]  # tg.finished in Spanish


def test_invalid_lang_falls_back_to_ui_language(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({"action": "help", "lang": "fr"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "aidez-moi"))

    assert "I turn your messages" in client.sent[-1][1]  # UI language (en)


# ---------------------------------------------------------------------- #
# Parser failure surfacing + bot log
# ---------------------------------------------------------------------- #
def test_parser_error_is_reported_to_the_chat(tmp_path, monkeypatch):
    driver = FakeDriver([RunResult(ok=False, error="model exploded")])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "crea algo"))

    assert "model exploded" in client.sent[-1][1]


def test_bot_log_records_received_and_decisions(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._handle_message(_msg(text="lista")))

    log = paths.telegram_log_path().read_text(encoding="utf-8")
    assert "chat=555" in log
    assert "intent=list_tasks" in log


def test_start_answers_with_chat_id_even_unauthorized(tmp_path, monkeypatch):
    driver = FakeDriver([])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._handle_message(_msg(chat_id=999, text="/start")))

    assert len(client.sent) == 1
    assert client.sent[0][0] == 999
    assert "999" in client.sent[0][1]


# ---------------------------------------------------------------------- #
# Proposals: cancel / expired
# ---------------------------------------------------------------------- #
def _make_proposal(service: TelegramService, client: FakeClient, tmp_path, monkeypatch):
    _run(service._parse_and_reply(555, "crea algo"))
    return _proposal_id(client)


def test_cancel_proposal_creates_nothing(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({
        "action": "create_tasks", "tasks": [{"name": "Nope"}],
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    pid = _make_proposal(service, client, tmp_path, monkeypatch)

    _run(service._handle_update(_callback_update(f"tg:x:{pid}")))

    assert models.list_all() == []
    assert pid not in service._proposals


def test_expired_proposal_is_rejected(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({
        "action": "create_tasks", "tasks": [{"name": "Vieja"}],
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    pid = _make_proposal(service, client, tmp_path, monkeypatch)
    service._proposals[pid].created_at -= service_module.PROPOSAL_TTL + 10

    _run(service._handle_update(_callback_update(f"tg:c:{pid}")))

    assert models.list_all() == []
    assert "expir" in client.sent[-1][1].lower()


def test_callback_from_other_chat_is_rejected(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({
        "action": "create_tasks", "tasks": [{"name": "Ajena"}],
    })])
    cfg = TelegramConfig(
        enabled=True, bot_token="T", allowed_chat_ids="555, 777",
        default_workdir=str(tmp_path),
    )
    service, client = _make_service(tmp_path, monkeypatch, driver, cfg=cfg)
    pid = _make_proposal(service, client, tmp_path, monkeypatch)

    _run(service._handle_update(_callback_update(f"tg:c:{pid}")))
    # Now replay the same id from another allowed chat: already consumed.
    update = _callback_update(f"tg:c:{pid}")
    update.callback.chat_id = 777
    _run(service._handle_update(update))

    assert len(models.list_all()) == 1  # only the first confirmation created it


# ---------------------------------------------------------------------- #
# Queries: list / status / files / ask
# ---------------------------------------------------------------------- #
def _existing_task() -> object:
    task = models.Task.create("Arreglar login", "desc", ".", Config())
    models.save(task)
    return task


def test_list_tasks(tmp_path, monkeypatch):
    task = _existing_task()
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "lista mis tareas"))

    assert "Arreglar login" in client.sent[-1][1]
    assert task.id in client.sent[-1][1]


def test_task_status(tmp_path, monkeypatch):
    task = _existing_task()
    driver = FakeDriver([_json_result({"action": "task_status", "task_ref": "login"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "estado de la tarea del login"))

    assert "Arreglar login" in client.sent[-1][1]
    assert "Borrador" in client.sent[-1][1] or "Draft" in client.sent[-1][1]


def test_task_not_found(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({"action": "task_status", "task_ref": "zzz"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "estado de zzz"))

    assert "zzz" in client.sent[-1][1]


def test_send_files_sends_md_documents(tmp_path, monkeypatch):
    task = _existing_task()
    (paths.plan_dir(task.id) / "01-plan.md").write_text("# Plan", encoding="utf-8")
    (paths.review_dir(task.id) / "01-review.md").write_text("# Review", encoding="utf-8")
    driver = FakeDriver([_json_result({"action": "send_files", "task_ref": "login"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "envíame los archivos de login"))

    names = [name for _, name in client.documents]
    assert "01-plan.md" in names
    assert "01-review.md" in names


def test_send_files_without_artifacts(tmp_path, monkeypatch):
    _existing_task()
    driver = FakeDriver([_json_result({"action": "send_files", "task_ref": "login"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "archivos de login"))

    assert client.documents == []
    assert client.sent  # "no .md artifacts yet" message


def test_ask_answers_with_task_context(tmp_path, monkeypatch):
    task = _existing_task()
    (paths.plan_dir(task.id) / "01-plan.md").write_text("# Plan secreto", encoding="utf-8")
    driver = FakeDriver([
        _json_result({"action": "ask", "task_ref": "login", "question": "¿qué planea?"}),
        RunResult(ok=True, text="Planifica arreglar el login."),
    ])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "¿qué planea la tarea del login?"))

    assert client.sent[-1][1] == "Planifica arreglar el login."
    # The ask prompt carried the task artifacts as context.
    assert "Plan secreto" in driver.prompts[-1]


def test_unknown_intent_sends_help(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({"action": "unknown"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "asdf"))

    assert "tasks" in client.sent[-1][1]


def test_parser_unavailable(tmp_path, monkeypatch):
    driver = FakeDriver([])
    driver.is_available = lambda: False  # type: ignore[assignment]
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "crea algo"))

    assert "fake" in client.sent[-1][1]


# ---------------------------------------------------------------------- #
# Attachments (photo / video)
# ---------------------------------------------------------------------- #
def test_photo_and_video_attached_to_created_task(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({
        "action": "create_tasks", "tasks": [{"name": "Con adjuntos"}],
    })])
    cfg = TelegramConfig(
        enabled=True, bot_token="T", allowed_chat_ids="555",
        confirm_create=False, default_workdir=str(tmp_path),
    )
    service, client = _make_service(tmp_path, monkeypatch, driver, cfg=cfg)
    client.files["pf"] = ("photos/file_1.jpg", b"JPEG-DATA")
    client.files["vf2"] = ("videos/file_2.mp4", b"MP4-DATA")

    _run(service._handle_message(_msg(photo_file_id="pf", video_file_id="vf2")))
    assert "eceived" in client.sent[-1][1]  # attachment pending notice

    _run(service._parse_and_reply(555, "crea la tarea"))

    task = models.list_all()[0]
    media_dir = paths.task_dir(task.id) / "media"
    saved = sorted(p.name for p in media_dir.iterdir())
    assert "media-01.jpg" in saved
    assert "media-01.mp4" in saved
    assert "media/media-01.jpg" in task.description
    assert str(media_dir / "media-01.mp4") in task.description
    # Images are picked up by the prompt media section (jpg included).
    assert any(p.suffix == ".jpg" for p in service_module.media.list_media(task.id))


def test_attachment_cancel_discards_buffer(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({
        "action": "create_tasks", "tasks": [{"name": "X"}],
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    client.files["pf"] = ("photos/file_1.jpg", b"JPEG-DATA")
    _run(service._handle_message(_msg(photo_file_id="pf")))
    _run(service._parse_and_reply(555, "crea"))
    pid = _proposal_id(client)

    _run(service._handle_update(_callback_update(f"tg:x:{pid}")))

    assert service._attachments.get(555, []) == []


# ---------------------------------------------------------------------- #
# STT / TTS paths
# ---------------------------------------------------------------------- #
def test_voice_without_stt_key(tmp_path, monkeypatch):
    monkeypatch.delenv("GRAFENO_TELEGRAM_STT_KEY", raising=False)
    driver = FakeDriver([])
    cfg = TelegramConfig(
        enabled=True, bot_token="T", allowed_chat_ids="555",
        stt_key="", default_workdir=str(tmp_path),  # no STT key configured
    )
    service, client = _make_service(tmp_path, monkeypatch, driver, cfg=cfg)
    client.files["vf"] = ("voice/file_1.oga", b"AUDIO")

    _run(service._handle_message(_msg(voice_file_id="vf")))

    assert "speech-to-text" in client.sent[-1][1]
    assert driver.prompts == []


def test_voice_stt_failure(tmp_path, monkeypatch):
    driver = FakeDriver([])
    cfg = TelegramConfig(
        enabled=True, bot_token="T", allowed_chat_ids="555",
        stt_key="KEY", default_workdir=str(tmp_path),
    )
    service, client = _make_service(tmp_path, monkeypatch, driver, cfg=cfg)
    client.files["vf"] = ("voice/file_1.oga", b"AUDIO")
    monkeypatch.setattr(service_module.stt, "transcribe", lambda **kwargs: None)

    _run(service._handle_message(_msg(voice_file_id="vf")))

    assert client.sent  # friendly failure message
    assert driver.prompts == []


def test_voice_stt_failure_reports_reason(tmp_path, monkeypatch):
    """An STT provider error (e.g. 401 invalid key) is told to the user."""
    driver = FakeDriver([])
    cfg = TelegramConfig(
        enabled=True, bot_token="T", allowed_chat_ids="555",
        stt_key="KEY", default_workdir=str(tmp_path),
    )
    service, client = _make_service(tmp_path, monkeypatch, driver, cfg=cfg)
    client.files["vf"] = ("voice/file_1.oga", b"AUDIO")

    def _failing(**kwargs):
        on_error = kwargs.get("on_error")
        if on_error:
            on_error("HTTP 401: Invalid API Key")
        return None

    monkeypatch.setattr(service_module.stt, "transcribe", _failing)

    _run(service._handle_message(_msg(voice_file_id="vf")))

    assert "HTTP 401" in client.sent[-1][1]
    # …and the reason lands in the bot log for post-mortem diagnosis.
    log = paths.telegram_log_path().read_text(encoding="utf-8")
    assert "stt failed" in log and "HTTP 401" in log


def test_tts_voice_reply_when_enabled(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    cfg = TelegramConfig(
        enabled=True, bot_token="T", allowed_chat_ids="555",
        tts_enabled=True, tts_key="KEY", default_workdir=str(tmp_path),
    )
    service, client = _make_service(tmp_path, monkeypatch, driver, cfg=cfg)
    monkeypatch.setattr(service_module.tts, "synthesize", lambda **kwargs: b"WAV")

    _run(service._parse_and_reply(555, "lista"))

    assert client.voices == [(555, b"WAV")]


def test_tts_disabled_by_default(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    monkeypatch.setattr(service_module.tts, "synthesize", lambda **kwargs: b"WAV")

    _run(service._parse_and_reply(555, "lista"))

    assert client.voices == []


# ---------------------------------------------------------------------- #
# Finish notification
# ---------------------------------------------------------------------- #
def test_notify_task_finished_sends_message_and_final(tmp_path, monkeypatch):
    driver = FakeDriver([])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    task = _existing_task()
    (paths.final_dir(task.id) / "01-final.md").write_text("# Fin", encoding="utf-8")
    service._state.chats[task.id] = 555

    _run(service.notify_task_finished(task))

    assert "Arreglar login" in client.sent[-1][1]
    assert ("555", "01-final.md") in [(str(c), n) for c, n in client.documents]


def test_notify_task_finished_unmapped_task_is_silent(tmp_path, monkeypatch):
    driver = FakeDriver([])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    task = _existing_task()

    _run(service.notify_task_finished(task))

    assert client.sent == []


# ---------------------------------------------------------------------- #
# Polling loop
# ---------------------------------------------------------------------- #
def test_run_stops_cleanly_on_auth_error(tmp_path, monkeypatch):
    class FailingClient(FakeClient):
        def get_me(self):
            raise TelegramError("HTTP 401: Unauthorized", status=401)

    driver = FakeDriver([])
    service, _ = _make_service(tmp_path, monkeypatch, driver)
    service.client = FailingClient()
    infos: list[str] = []
    service._on_info = infos.append

    _run(service.run())

    assert infos and "401" in infos[0]


def test_run_cert_error_shows_fix_hint(tmp_path, monkeypatch):
    """A CERTIFICATE_VERIFY_FAILED at startup surfaces the actionable hint."""
    class CertClient(FakeClient):
        def get_me(self):
            raise TelegramError(
                "network error: [SSL: CERTIFICATE_VERIFY_FAILED] certificate "
                "verify failed: unable to get local issuer certificate",
                status=0,
                cert_error=True,
            )

    driver = FakeDriver([])
    service, _ = _make_service(tmp_path, monkeypatch, driver)
    service.client = CertClient()
    infos: list[str] = []
    service._on_info = infos.append

    _run(service.run())

    assert any("certifi" in message for message in infos)
    assert any("CERTIFICATE_VERIFY_FAILED" in message for message in infos)


def test_run_processes_updates_and_persists_offset(tmp_path, monkeypatch):
    class LoopClient(FakeClient):
        """First poll serves a batch; later polls block until released."""

        def __init__(self, batch):
            super().__init__()
            self._batch = batch
            self._served = False
            self._release = threading.Event()

        def get_updates(self, offset, *, timeout=30.0):
            if not self._served:
                self._served = True
                return self._batch
            self._release.wait(5)
            return []

    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    service, _ = _make_service(tmp_path, monkeypatch, driver)
    batch = [
        Update(update_id=77, message=_msg(text="lista")),
        Update(update_id=78, message=_msg(chat_id=999, text="intruso")),
    ]
    client = LoopClient(batch)
    service.client = client

    async def scenario():
        run_task = asyncio.create_task(service.run())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if client.sent and _load_state().offset >= 79:
                break
        client._release.set()
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    _run(scenario())

    # The whitelisted chat got its answer; the intruder only the auth notice.
    texts = {chat: text for chat, text, _ in client.sent}
    assert "999" in texts[999]
    assert "999" not in texts[555]
    assert len(driver.prompts) == 1  # parser only ran for the allowed chat
    assert _load_state().offset == 79
