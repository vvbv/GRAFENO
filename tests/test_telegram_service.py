"""End-to-end tests of the Telegram bot service (fake client + fake CLI)."""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from pathlib import Path

from grafeno import models, paths
from grafeno.config import Config, TelegramConfig
from grafeno.models import TaskState
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
        self.edited: list[tuple[int, int, dict]] = []  # (chat_id, message_id, markup)

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

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
        self.edited.append((chat_id, message_id, reply_markup))

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
    workspaces: list[str] | None = None,
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
        workspaces=workspaces,
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


def _filter_query_id(client: FakeClient) -> str:
    """Query id from the first button of the last message's markup."""
    markup = client.sent[-1][2]
    assert markup is not None
    return markup["inline_keyboard"][0][0]["callback_data"].split(":")[2]


def _press(service, data: str) -> None:
    """Simulate an inline-button tap."""
    _run(service._handle_update(_callback_update(data)))


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
    assert models.list_all() == []  # chaining not answered yet
    assert "chained" in client.sent[-1][1]  # tg.chain.ask
    _run(service._handle_update(_callback_update(f"tg:n:{pid}")))

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
    # The callbacks were acknowledged.
    assert client.answered[-1] == "cb-1"
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

    assert models.list_all() == []  # waiting for the chaining answer
    markup = client.sent[-1][2]
    assert markup is not None  # the two chaining buttons
    pid = _proposal_id(client)
    _run(service._handle_update(_callback_update(f"tg:n:{pid}")))
    assert [task.name for task in models.list_all()] == ["Directa"]


def test_multiple_tasks_created(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({
        "action": "create_tasks",
        "tasks": [{"name": "A"}, {"name": "B"}],
    })])
    cfg = TelegramConfig(
        enabled=True, bot_token="T", allowed_chat_ids="555",
        confirm_create=False, default_workdir=str(tmp_path),
    )
    service, client = _make_service(tmp_path, monkeypatch, driver, cfg=cfg)

    _run(service._parse_and_reply(555, "dos tareas"))
    pid = _proposal_id(client)
    _run(service._handle_update(_callback_update(f"tg:n:{pid}")))

    assert sorted(task.name for task in models.list_all()) == ["A", "B"]
    assert all(task.parent_id == "" for task in models.list_all())


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
    pid = _proposal_id(client)
    _run(service._handle_update(_callback_update(f"tg:n:{pid}")))

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

    created, errors, _unchained = service._create_tasks(555, [
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


def test_group_all_processes_chatter(tmp_path, monkeypatch):
    """With group_all enabled, every whitelisted group message reaches the
    parser without needing a mention, reply or command."""
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    tg = TelegramConfig(
        enabled=True,
        bot_token="TOKEN",
        allowed_chat_ids="555",
        stt_key="STT-KEY",
        default_workdir=str(tmp_path),
        group_all=True,
    )
    service, client = _make_service(tmp_path, monkeypatch, driver, cfg=tg)
    service._bot_username = "GrafenoCLI_bot"
    service._bot_id = 1

    _run(service._handle_message(_group_msg(text="conversación entre humanos")))

    assert len(driver.prompts) == 1


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
    pid = _proposal_id(client)
    _run(service._handle_update(_callback_update(f"tg:n:{pid}")))
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
    _run(service._handle_update(_callback_update(f"tg:n:{pid}")))
    # Now replay the same id from another allowed chat: already consumed.
    update = _callback_update(f"tg:n:{pid}")
    update.callback.chat_id = 777
    _run(service._handle_update(update))

    assert len(models.list_all()) == 1  # only the first confirmation created it


# ---------------------------------------------------------------------- #
# Chaining question (tg:n / tg:l)
# ---------------------------------------------------------------------- #
def _in_progress_task(name: str, workdir: str, state=TaskState.IMPLEMENTING):
    """Saved task in an active pipeline state (chaining candidate)."""
    task = models.Task.create(name, "d", workdir, Config())
    task.state = state
    models.save(task)
    return task


def test_chain_last_links_to_latest_in_progress_task(tmp_path, monkeypatch):
    en_curso = _in_progress_task("En curso", str(tmp_path))
    driver = FakeDriver([_json_result({
        "action": "create_tasks",
        "tasks": [{"name": "Nueva"}],
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    _run(service._parse_and_reply(555, "crea"))
    pid = _proposal_id(client)
    _run(service._handle_update(_callback_update(f"tg:c:{pid}")))

    markup = client.sent[-1][2]
    assert markup is not None
    callbacks = [btn["callback_data"] for row in markup["inline_keyboard"] for btn in row]
    assert f"tg:n:{pid}" in callbacks
    assert f"tg:l:{pid}" in callbacks

    _run(service._handle_update(_callback_update(f"tg:l:{pid}")))

    task = models.list_all()[0]
    assert task.parent_id == en_curso.id
    assert "chained after En curso" in client.sent[-1][1]


def test_chain_last_picks_the_newest_candidate(tmp_path, monkeypatch):
    una = _in_progress_task("Una", str(tmp_path))
    dos = _in_progress_task("Dos", str(tmp_path))
    other_dir = tmp_path / "otro"
    other_dir.mkdir()
    otra = _in_progress_task("Otra", str(other_dir))
    driver = FakeDriver([_json_result({
        "action": "create_tasks",
        "tasks": [{"name": "Nueva"}],
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    _run(service._parse_and_reply(555, "crea"))
    pid = _proposal_id(client)
    _run(service._handle_update(_callback_update(f"tg:c:{pid}")))
    _run(service._handle_update(_callback_update(f"tg:l:{pid}")))

    nueva = next(t for t in models.list_all() if t.name == "Nueva")
    assert nueva.parent_id == max(una.id, dos.id)
    assert nueva.parent_id != otra.id  # never the other-project task


def test_chain_last_ignores_non_active_states(tmp_path, monkeypatch):
    done = _in_progress_task("Hecha", str(tmp_path), state=TaskState.DONE)
    draft = models.Task.create("Borrador", "d", str(tmp_path), Config())
    models.save(draft)
    driver = FakeDriver([_json_result({
        "action": "create_tasks",
        "tasks": [{"name": "Nueva"}],
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    _run(service._parse_and_reply(555, "crea"))
    pid = _proposal_id(client)
    _run(service._handle_update(_callback_update(f"tg:c:{pid}")))
    _run(service._handle_update(_callback_update(f"tg:l:{pid}")))

    task = models.list_all()[0]
    assert task.parent_id == ""
    assert "parallel task" in client.sent[-1][1]


def test_chain_last_invalid_position_falls_back(tmp_path, monkeypatch):
    padre = _in_progress_task("Padre", str(tmp_path))
    hija = models.Task.create("Hija", "d", str(tmp_path), Config())
    hija.parent_id = padre.id
    hija.state = TaskState.DONE
    models.save(hija)
    driver = FakeDriver([_json_result({
        "action": "create_tasks",
        "tasks": [{"name": "Nueva"}],
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    _run(service._parse_and_reply(555, "crea"))
    pid = _proposal_id(client)
    _run(service._handle_update(_callback_update(f"tg:c:{pid}")))
    _run(service._handle_update(_callback_update(f"tg:l:{pid}")))

    task = models.list_all()[0]
    assert task.parent_id == ""
    assert "parallel task" in client.sent[-1][1]


def test_chain_none_creates_parallel_even_with_in_progress(tmp_path, monkeypatch):
    _in_progress_task("En curso", str(tmp_path))
    driver = FakeDriver([_json_result({
        "action": "create_tasks",
        "tasks": [{"name": "Nueva"}],
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    _run(service._parse_and_reply(555, "crea"))
    pid = _proposal_id(client)
    _run(service._handle_update(_callback_update(f"tg:c:{pid}")))
    _run(service._handle_update(_callback_update(f"tg:n:{pid}")))

    task = models.list_all()[0]
    assert task.parent_id == ""
    assert "parallel task" not in client.sent[-1][1]


def test_cancel_at_chaining_step_creates_nothing(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({
        "action": "create_tasks", "tasks": [{"name": "Nope"}],
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    _run(service._parse_and_reply(555, "crea"))
    pid = _proposal_id(client)
    _run(service._handle_update(_callback_update(f"tg:c:{pid}")))

    _run(service._handle_update(_callback_update(f"tg:x:{pid}")))

    assert models.list_all() == []
    assert pid not in service._proposals


def test_chaining_question_without_confirmation_config(tmp_path, monkeypatch):
    """With confirm_create=False the chaining buttons show up directly."""
    cfg = TelegramConfig(
        enabled=True, bot_token="T", allowed_chat_ids="555",
        confirm_create=False, default_workdir=str(tmp_path),
    )
    driver = FakeDriver([_json_result({
        "action": "create_tasks", "tasks": [{"name": "Directa"}],
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver, cfg=cfg)

    _run(service._parse_and_reply(555, "crea una tarea directa"))

    assert models.list_all() == []
    assert client.sent[-1][2] is not None  # chaining buttons present
    pid = _proposal_id(client)
    _run(service._handle_update(_callback_update(f"tg:l:{pid}")))
    task = models.list_all()[0]
    assert task.parent_id == ""
    assert "parallel task" in client.sent[-1][1]


def test_chain_last_batch_chains_sequentially(tmp_path, monkeypatch):
    """A multi-task message with 'chain last' chains the batch in order."""
    en_curso = _in_progress_task("En curso", str(tmp_path))
    driver = FakeDriver([_json_result({
        "action": "create_tasks",
        "tasks": [{"name": "A"}, {"name": "B"}],
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    _run(service._parse_and_reply(555, "dos tareas"))
    pid = _proposal_id(client)
    _run(service._handle_update(_callback_update(f"tg:c:{pid}")))
    _run(service._handle_update(_callback_update(f"tg:l:{pid}")))

    by_name = {task.name: task for task in models.list_all()}
    assert by_name["A"].parent_id == en_curso.id
    assert by_name["B"].parent_id == by_name["A"].id


def test_chain_last_batch_without_candidate_chains_among_themselves(tmp_path, monkeypatch):
    """With no in-progress task, the first goes parallel and the rest chain."""
    driver = FakeDriver([_json_result({
        "action": "create_tasks",
        "tasks": [{"name": "A"}, {"name": "B"}],
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    _run(service._parse_and_reply(555, "dos tareas"))
    pid = _proposal_id(client)
    _run(service._handle_update(_callback_update(f"tg:c:{pid}")))
    _run(service._handle_update(_callback_update(f"tg:l:{pid}")))

    by_name = {task.name: task for task in models.list_all()}
    assert by_name["A"].parent_id == ""
    assert by_name["B"].parent_id == by_name["A"].id
    assert "parallel task" in client.sent[-1][1]  # notice only for A


def test_chain_last_batch_respects_each_project(tmp_path, monkeypatch):
    """Specs of different projects chain within their own project only."""
    otro = tmp_path / "otro"
    otro.mkdir()
    driver = FakeDriver([_json_result({
        "action": "create_tasks",
        "tasks": [
            {"name": "A", "workdir": str(tmp_path)},
            {"name": "B", "workdir": str(otro)},
            {"name": "C", "workdir": str(tmp_path)},
        ],
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)
    _run(service._parse_and_reply(555, "tres tareas"))
    pid = _proposal_id(client)
    _run(service._handle_update(_callback_update(f"tg:c:{pid}")))
    _run(service._handle_update(_callback_update(f"tg:l:{pid}")))

    by_name = {task.name: task for task in models.list_all()}
    assert by_name["A"].parent_id == ""
    assert by_name["B"].parent_id == ""
    assert by_name["C"].parent_id == by_name["A"].id


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

    query_id = _filter_query_id(client)
    _press(service, f"tg:fa:{query_id}")

    assert "Arreglar login" in client.sent[-1][1]
    assert task.id in client.sent[-1][1]


def test_list_projects(tmp_path, monkeypatch):
    """'list_projects' answers with the distinct task workdirs and counts."""
    models.save(models.Task.create("A", "d", str(tmp_path), Config()))
    models.save(models.Task.create("B", "d", str(tmp_path), Config()))
    other = tmp_path / "otro"
    other.mkdir()
    models.save(models.Task.create("C", "d", str(other), Config()))
    driver = FakeDriver([_json_result({"action": "list_projects"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "lista mis proyectos"))

    message = client.sent[-1][1]
    assert str(tmp_path) in message
    assert str(other) in message
    assert "2" in message  # tmp_path group has two tasks


def test_list_projects_empty(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({"action": "list_projects"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "lista mis proyectos"))

    assert "No projects" in client.sent[-1][1]  # conftest fixes English


def test_list_projects_includes_discovered(tmp_path, monkeypatch):
    """Workspace subfolders without tasks are listed and marked as empty."""
    ws = tmp_path / "ws"
    (ws / "vacio").mkdir(parents=True)
    driver = FakeDriver([_json_result({"action": "list_projects"})])
    service, client = _make_service(
        tmp_path, monkeypatch, driver, workspaces=[str(ws)]
    )

    _run(service._parse_and_reply(555, "lista mis proyectos"))

    message = client.sent[-1][1]
    assert str(ws / "vacio") in message
    assert "no tasks yet" in message  # conftest fixes English


def test_list_projects_dedupes_projects_with_tasks(tmp_path, monkeypatch):
    """A workspace subfolder that already has tasks is listed once, with its count."""
    ws = tmp_path / "ws"
    with_tasks = ws / "con-tareas"
    with_tasks.mkdir(parents=True)
    models.save(models.Task.create("A", "d", str(with_tasks), Config()))
    driver = FakeDriver([_json_result({"action": "list_projects"})])
    service, client = _make_service(
        tmp_path, monkeypatch, driver, workspaces=[str(ws)]
    )

    _run(service._parse_and_reply(555, "lista mis proyectos"))

    message = client.sent[-1][1]
    assert message.count(str(with_tasks)) == 1
    assert "1 task(s)" in message
    assert "no tasks yet" not in message


def test_parser_receives_projects_context(tmp_path, monkeypatch):
    """The parser prompt carries the projects listing for workdir routing."""
    models.save(models.Task.create("A", "d", str(tmp_path), Config()))
    driver = FakeDriver([_json_result({"action": "list_projects"})])
    service, _ = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "lista mis proyectos"))

    assert f"- {tmp_path} | 1" in driver.prompts[0]


def test_list_project_tasks(tmp_path, monkeypatch):
    """'list_project_tasks' lists only the tasks of that project, with state."""
    models.save(models.Task.create("A1", "d", str(tmp_path), Config()))
    other = tmp_path / "otro"
    other.mkdir()
    models.save(models.Task.create("B1", "d", str(other), Config()))
    driver = FakeDriver([_json_result({
        "action": "list_project_tasks", "project_ref": str(tmp_path),
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "tareas del proyecto de " + str(tmp_path)))

    query_id = _filter_query_id(client)
    _press(service, f"tg:fa:{query_id}")

    message = client.sent[-1][1]
    assert "A1" in message
    assert "B1" not in message  # the other project's task is not listed
    assert str(tmp_path) in message
    assert "Draft" in message  # state label (conftest fixes English)


def test_list_project_tasks_by_name_fragment(tmp_path, monkeypatch):
    proj = tmp_path / "grafeno"
    proj.mkdir()
    models.save(models.Task.create("A1", "d", str(proj), Config()))
    driver = FakeDriver([_json_result({
        "action": "list_project_tasks", "project_ref": "grafeno",
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "que tareas tiene grafeno"))

    query_id = _filter_query_id(client)
    _press(service, f"tg:fa:{query_id}")

    assert "A1" in client.sent[-1][1]


def test_list_project_tasks_not_found(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({
        "action": "list_project_tasks", "project_ref": "zzz",
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "tareas del proyecto zzz"))

    assert "zzz" in client.sent[-1][1]  # not-found message echoes the ref


# ---------------------------------------------------------------------- #
# State filter for task-list queries
# ---------------------------------------------------------------------- #
def test_list_tasks_asks_state_filter(tmp_path, monkeypatch):
    task = _existing_task()
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "lista mis tareas"))

    message = client.sent[-1][1]
    assert "Which tasks do you want to see?" in message
    markup = client.sent[-1][2]
    callbacks = [
        button["callback_data"]
        for row in markup["inline_keyboard"]
        for button in row
    ]
    assert sum(1 for data in callbacks if data.startswith("tg:fa:")) == 1
    assert sum(1 for data in callbacks if data.startswith("tg:fn:")) == 1
    assert sum(1 for data in callbacks if data.startswith("tg:fp:")) == 1
    assert task.name not in message  # list not sent yet


def test_filter_all_lists_every_state(tmp_path, monkeypatch):
    draft = models.Task.create("Draft one", "d", str(tmp_path), Config())
    models.save(draft)
    done = models.Task.create("Done one", "d", str(tmp_path), Config())
    done.state = TaskState.DONE
    models.save(done)
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "lista mis tareas"))

    query_id = _filter_query_id(client)
    _press(service, f"tg:fa:{query_id}")

    message = client.sent[-1][1]
    assert "Draft one" in message
    assert "Done one" in message


def test_filter_active_excludes_done(tmp_path, monkeypatch):
    models.save(models.Task.create("Draft one", "d", str(tmp_path), Config()))
    done = models.Task.create("Done one", "d", str(tmp_path), Config())
    done.state = TaskState.DONE
    models.save(done)
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "lista mis tareas"))

    query_id = _filter_query_id(client)
    _press(service, f"tg:fn:{query_id}")

    message = client.sent[-1][1]
    assert "Draft one" in message
    assert "Done one" not in message


def test_filter_picker_toggles_and_shows(tmp_path, monkeypatch):
    draft = models.Task.create("Draft one", "d", str(tmp_path), Config())
    models.save(draft)
    done = models.Task.create("Done one", "d", str(tmp_path), Config())
    done.state = TaskState.DONE
    models.save(done)
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "lista mis tareas"))

    query_id = _filter_query_id(client)
    _press(service, f"tg:fp:{query_id}")

    picker_message = client.sent[-1][1]
    assert "Tap states to toggle them" in picker_message
    picker_markup = client.sent[-1][2]
    state_buttons = [
        button
        for row in picker_markup["inline_keyboard"]
        for button in row
        if button["callback_data"].startswith("tg:ft:")
    ]
    assert len(state_buttons) == 12
    assert all(button["text"].startswith("[ ] ") for button in state_buttons)
    show_buttons = [
        button
        for row in picker_markup["inline_keyboard"]
        for button in row
        if button["callback_data"].startswith("tg:fd:")
    ]
    assert len(show_buttons) == 1

    _press(service, f"tg:ft:{query_id}:done")
    assert len(client.edited) == 1
    edited_markup = client.edited[-1][2]
    edited_texts = [
        button["text"]
        for row in edited_markup["inline_keyboard"]
        for button in row
    ]
    assert any(text == "[x] Done" for text in edited_texts)
    assert all(
        not text.startswith("[x] ") or text == "[x] Done"
        for text in edited_texts
    )

    _press(service, f"tg:fd:{query_id}")
    final_message = client.sent[-1][1]
    assert "Done one" in final_message
    assert "Draft one" not in final_message


def test_filter_toggle_second_time_unmarks(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "lista mis tareas"))

    query_id = _filter_query_id(client)
    _press(service, f"tg:fp:{query_id}")
    _press(service, f"tg:ft:{query_id}:done")
    _press(service, f"tg:ft:{query_id}:done")
    assert len(client.edited) == 2
    final_markup = client.edited[-1][2]
    done_texts = [
        button["text"]
        for row in final_markup["inline_keyboard"]
        for button in row
        if button["callback_data"].endswith(":done")
    ]
    assert done_texts == ["[ ] Done"]


def test_filter_show_without_selection_warns(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "lista mis tareas"))

    query_id = _filter_query_id(client)
    _press(service, f"tg:fp:{query_id}")
    _press(service, f"tg:fd:{query_id}")
    warn = client.sent[-1][1]
    assert "No state selected" in warn

    # Query is still alive: pressing All now answers the full list.
    _press(service, f"tg:fa:{query_id}")
    # The last message is either an empty notice or a listing; either way the
    # query has been handled (popped), so pressing fa on the same id again
    # must hit the "expired" branch.
    _press(service, f"tg:fa:{query_id}")
    assert "expired" in client.sent[-1][1].lower()


def test_filter_unknown_or_expired_query(tmp_path, monkeypatch):
    driver = FakeDriver([_json_result({"action": "list_tasks"})])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _press(service, "tg:fa:zzzz9999")

    assert "That proposal expired or was already handled." in client.sent[-1][1]


def test_list_project_tasks_filter_flow(tmp_path, monkeypatch):
    other = tmp_path / "otro"
    other.mkdir()
    models.save(models.Task.create("A1", "d", str(tmp_path), Config()))
    models.save(models.Task.create("B1", "d", str(other), Config()))
    driver = FakeDriver([_json_result({
        "action": "list_project_tasks", "project_ref": str(tmp_path),
    })])
    service, client = _make_service(tmp_path, monkeypatch, driver)

    _run(service._parse_and_reply(555, "tareas del proyecto de " + str(tmp_path)))

    query_id = _filter_query_id(client)
    _press(service, f"tg:fa:{query_id}")

    message = client.sent[-1][1]
    assert "A1" in message
    assert "B1" not in message
    assert str(tmp_path) in message


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
    pid = _proposal_id(client)
    _run(service._handle_update(_callback_update(f"tg:n:{pid}")))

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
