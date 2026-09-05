"""Tests of the Telegram intent parser (fake CLI drivers)."""

from __future__ import annotations

import asyncio
import json
from collections import deque

from grafeno import models
from grafeno.config import Config
from grafeno.drivers.base import CLIDriver, RunResult
from grafeno.telegram import intents
from grafeno.telegram.intents import Intent, TaskSpec


class FakeDriver(CLIDriver):
    """Driver double: returns queued RunResults, records prompts."""

    def __init__(self, results: list[RunResult], available: bool = True):
        self.name = "fake"
        self.display_name = "fake"
        self.executable = "fake"
        self._available = available
        self._results = deque(results)
        self.prompts: list[str] = []

    def is_available(self) -> bool:
        return self._available

    def build_command(self, request):
        return []

    def models_command(self):
        return []

    def parse_models(self, output):
        return []

    async def run(self, request, on_event=None, on_activity=None):
        self.prompts.append(request.prompt)
        if self._results:
            return self._results.popleft()
        return RunResult(ok=True, text="ok")


def _ok(text: str) -> RunResult:
    return RunResult(ok=True, text=text)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------- #
# TaskSpec / parse_intent_payload
# ---------------------------------------------------------------------- #
def test_taskspec_roundtrip():
    spec = TaskSpec(name="n", description="d", workdir="/w", test_command="pytest")
    assert TaskSpec.from_dict(spec.to_dict()) == spec


def test_parse_payload_create_multiple_tasks():
    payload = json.dumps({
        "action": "create_tasks",
        "tasks": [
            {"name": "t1", "description": "d1"},
            {"name": "t2", "description": "d2", "workdir":"/w", "test_command": "make test"},
        ],
    })
    intent = intents.parse_intent_payload(payload)
    assert intent.action == "create_tasks"
    assert [s.name for s in intent.tasks] == ["t1", "t2"]
    assert intent.tasks[1].workdir == "/w"
    assert intent.tasks[1].test_command == "make test"


def test_parse_payload_extracts_json_from_prose():
    text = 'Claro, aquí va:\n```json\n{"action": "list_tasks"}\n```\nListo.'
    intent = intents.parse_intent_payload(text)
    assert intent.action == "list_tasks"


def test_parse_payload_without_json_is_unknown():
    assert intents.parse_intent_payload("no json at all").action == "unknown"
    assert intents.parse_intent_payload("{broken json").action == "unknown"


def test_parse_payload_unknown_action_normalized():
    assert intents.parse_intent_payload('{"action": "borra_todo"}').action == "unknown"


def test_parse_payload_create_without_valid_tasks_is_unknown():
    payload = '{"action": "create_tasks", "tasks": [{"description": "sin nombre"}]}'
    assert intents.parse_intent_payload(payload).action == "unknown"


def test_parse_payload_query_without_ref_becomes_help():
    payload = '{"action": "task_status"}'
    assert intents.parse_intent_payload(payload).action == "help"


def test_parse_payload_ask_keeps_question_and_ref():
    payload = json.dumps({
        "action": "ask", "task_ref": "grafeno", "question": "¿por qué falló?",
    })
    intent = intents.parse_intent_payload(payload)
    assert intent.action == "ask"
    assert intent.task_ref == "grafeno"
    assert intent.question == "¿por qué falló?"


def test_parse_payload_lang():
    """The parser reports the user's language; unknown ones are dropped."""
    intent = intents.parse_intent_payload('{"action": "help", "lang": "es"}')
    assert intent.lang == "es"
    intent = intents.parse_intent_payload('{"action": "help", "lang": "EN"}')
    assert intent.lang == "en"
    intent = intents.parse_intent_payload('{"action": "help", "lang": "fr"}')
    assert intent.lang == ""  # no catalog for it: caller falls back
    intent = intents.parse_intent_payload('{"action": "help"}')
    assert intent.lang == ""


def test_parser_prompt_asks_for_language(tmp_path):
    prompt = intents.build_parser_prompt("hola", "", "/tmp")
    assert '"lang"' in prompt


def test_parse_payload_tasks_capped():
    payload = json.dumps({
        "action": "create_tasks",
        "tasks": [{"name": f"t{n}"} for n in range(30)],
    })
    intent = intents.parse_intent_payload(payload)
    assert len(intent.tasks) == intents.MAX_TASKS_PER_INTENT


# ---------------------------------------------------------------------- #
# parse_intent (CLI one-shot)
# ---------------------------------------------------------------------- #
def test_parse_intent_ok(tmp_path):
    driver = FakeDriver([_ok('{"action": "list_tasks"}')])
    intent = _run(intents.parse_intent(driver, "modelo", "lista", "- t1 | a | b", tmp_path))
    assert intent.action == "list_tasks"
    # The prompt carries the user text and the task summary.
    assert "lista" in driver.prompts[0]
    assert "- t1 | a | b" in driver.prompts[0]


def test_parse_intent_cli_failure_carries_error(tmp_path):
    """A failing CLI surfaces the error (distinct from a plain 'unknown')."""
    driver = FakeDriver([RunResult(ok=False, error="boom exit 1")])
    intent = _run(intents.parse_intent(driver, "", "texto", "", tmp_path))
    assert intent.action == "unknown"
    assert "boom" in intent.error


def test_parse_intent_cli_exception_carries_error(tmp_path):
    class BoomDriver(FakeDriver):
        async def run(self, request, on_event=None, on_activity=None):
            raise RuntimeError("crash")

    intent = _run(intents.parse_intent(BoomDriver([]), "", "texto", "", tmp_path))
    assert intent.action == "unknown"
    assert "crash" in intent.error


def test_parse_intent_timeout_carries_error(tmp_path):
    """A hung parser CLI is cancelled after the timeout and reported."""
    class SlowDriver(FakeDriver):
        async def run(self, request, on_event=None, on_activity=None):
            await asyncio.sleep(30)
            return RunResult(ok=True, text="{}")

    intent = _run(intents.parse_intent(SlowDriver([]), "", "texto", "", tmp_path, timeout=0.2))
    assert intent.action == "unknown"
    assert "timeout" in intent.error


def test_parse_intent_bad_json_is_unknown_without_error(tmp_path):
    driver = FakeDriver([_ok("no json here")])
    intent = _run(intents.parse_intent(driver, "", "texto", "", tmp_path))
    assert intent.action == "unknown"
    assert intent.error == ""


# ---------------------------------------------------------------------- #
# fuzzy_find_task / tasks_summary
# ---------------------------------------------------------------------- #
def _task(name: str) -> object:
    return models.Task.create(name, "desc", ".", Config())


def test_fuzzy_find_by_id_prefix_and_name():
    tasks = [_task("Arreglar login"), _task("Mejorar logs")]
    assert intents.fuzzy_find_task(tasks[0].id, tasks) is tasks[0]
    prefix = tasks[1].id[:18]
    assert intents.fuzzy_find_task(prefix, tasks) is tasks[1]
    assert intents.fuzzy_find_task("arreglar login", tasks) is tasks[0]
    assert intents.fuzzy_find_task("logs", tasks) is tasks[1]
    assert intents.fuzzy_find_task("inexistente", tasks) is None
    assert intents.fuzzy_find_task("", tasks) is None


def test_tasks_summary_format():
    tasks = [_task("Demo")]
    summary = intents.tasks_summary(tasks)
    assert tasks[0].id in summary
    assert "Demo" in summary


# ---------------------------------------------------------------------- #
# project-aware routing (workdir context)
# ---------------------------------------------------------------------- #
def test_tasks_summary_includes_workdir(tmp_path):
    task = models.Task.create("Demo", "d", str(tmp_path), Config())
    summary = intents.tasks_summary([task])
    assert str(tmp_path) in summary
    assert summary.count("|") >= 3  # id | name | state | workdir


def test_tasks_summary_remote_shows_ssh_spec(tmp_path):
    task = models.Task.create("Remota", "d", "/home/u/proj", Config())
    task.remote = "user@host:/home/u/proj"
    assert "user@host:/home/u/proj" in intents.tasks_summary([task])


def test_parser_prompt_describes_workdir_routing(tmp_path):
    prompt = intents.build_parser_prompt("hola", "", "/tmp")
    assert "id | nombre | estado | directorio" in prompt
    assert "directorio por defecto" in prompt
    assert "EXACTAMENTE el\n  directorio" in prompt or "EXACTAMENTE el directorio" in prompt


def test_resolve_workdir_empty_falls_back_to_default():
    assert intents.resolve_workdir("", [], "/default") == "/default"
    assert intents.resolve_workdir("  ", [], "") == "."


def test_resolve_workdir_matches_existing_task(tmp_path):
    task = models.Task.create("Demo", "d", str(tmp_path), Config())
    # Case differences still resolve to the canonical task workdir.
    assert intents.resolve_workdir(str(tmp_path).upper(), [task]) == str(tmp_path)


def test_resolve_workdir_passthrough_unknown_path():
    assert intents.resolve_workdir("/otra/ruta", [], "/d") == "/otra/ruta"


# ---------------------------------------------------------------------- #
# project listing (global scope directories)
# ---------------------------------------------------------------------- #
def test_parse_payload_list_projects():
    assert intents.parse_intent_payload('{"action": "list_projects"}').action == "list_projects"


def test_project_dirs_groups_counts_and_order(tmp_path):
    a = _task("A")
    b = _task("B")
    c = models.Task.create("C", "d", str(tmp_path), Config())
    # Two tasks in "." and one in tmp_path; first-seen order is kept.
    assert intents.project_dirs([a, b, c]) == [(".", 2), (str(tmp_path), 1)]


def test_project_dirs_remote_uses_ssh_spec():
    task = _task("Remota")
    task.remote = "user@host:/home/u/proj"
    assert intents.project_dirs([task]) == [("user@host:/home/u/proj", 1)]


def test_projects_summary_format():
    summary = intents.projects_summary([_task("A"), _task("B")])
    assert summary == "- . | 2"


def test_parser_prompt_includes_projects_section_and_action(tmp_path):
    prompt = intents.build_parser_prompt("hola", "", "/tmp", projects="- /x | 2")
    assert "- /x | 2" in prompt
    assert "list_projects" in prompt


# ---------------------------------------------------------------------- #
# tasks of one project (list_project_tasks)
# ---------------------------------------------------------------------- #
def test_parse_payload_list_project_tasks():
    intent = intents.parse_intent_payload(
        '{"action": "list_project_tasks", "project_ref": "/x/proj"}'
    )
    assert intent.action == "list_project_tasks"
    assert intent.project_ref == "/x/proj"


def test_parse_payload_list_project_tasks_without_ref_is_help():
    assert intents.parse_intent_payload('{"action": "list_project_tasks"}').action == "help"


def test_parser_prompt_mentions_list_project_tasks(tmp_path):
    prompt = intents.build_parser_prompt("hola", "", "/tmp")
    assert "list_project_tasks" in prompt
    assert "project_ref" in prompt


def test_resolve_project_dir_exact_and_case_insensitive(tmp_path):
    task = models.Task.create("A", "d", str(tmp_path), Config())
    assert intents.resolve_project_dir(str(tmp_path), [task]) == str(tmp_path)
    assert intents.resolve_project_dir(str(tmp_path).upper(), [task]) == str(tmp_path)


def test_resolve_project_dir_basename_fragment(tmp_path):
    proj = tmp_path / "grafeno"
    proj.mkdir()
    task = models.Task.create("A", "d", str(proj), Config())
    assert intents.resolve_project_dir("grafeno", [task]) == str(proj)


def test_resolve_project_dir_remote_spec(tmp_path):
    task = _task("Remota")
    task.remote = "user@host:/home/u/proj"
    assert intents.resolve_project_dir("user@host:/home/u/proj", [task]) == "user@host:/home/u/proj"
    assert intents.resolve_project_dir("proj", [task]) == "user@host:/home/u/proj"


def test_resolve_project_dir_unknown_or_ambiguous(tmp_path):
    a = models.Task.create("A", "d", str(tmp_path / "alpha"), Config())
    b = models.Task.create("B", "d", str(tmp_path / "beta"), Config())
    assert intents.resolve_project_dir("zzz", [a, b]) is None
    assert intents.resolve_project_dir(tmp_path.name.lower(), [a, b]) is None  # matches both
    assert intents.resolve_project_dir("", [a, b]) is None


def test_project_tasks_filters_by_directory(tmp_path):
    mine = models.Task.create("Mia", "d", str(tmp_path), Config())
    other = _task("Ajena")  # workdir "."
    assert intents.project_tasks(str(tmp_path), [mine, other]) == [mine]


def test_project_dirs_includes_discovered_without_duplicates(tmp_path):
    """Discovered dirs are appended with count 0; dirs with tasks are not duplicated."""
    proj = tmp_path / "proj"
    proj.mkdir()
    task = models.Task.create("Con tareas", "d", str(proj), Config())
    extra = [str(proj), str(proj) + "/", str(tmp_path / "nuevo")]
    assert intents.project_dirs([task], extra) == [
        (str(proj), 1),
        (str(tmp_path / "nuevo"), 0),
    ]


def test_project_dirs_discovered_never_shadows_remote(tmp_path):
    """A remote SSH spec is never merged with a local dir of the same basename."""
    task = _task("Remota")
    task.remote = "user@host:/home/u/proj"
    local = tmp_path / "proj"
    local.mkdir()
    assert intents.project_dirs([task], [str(local)]) == [
        ("user@host:/home/u/proj", 1),
        (str(local), 0),
    ]


def test_resolve_project_dir_matches_discovered(tmp_path):
    """Discovered projects resolve by basename; ambiguous fragments return None."""
    nuevo = tmp_path / "nuevo"
    assert intents.resolve_project_dir("nuevo", [], [str(nuevo)]) == str(nuevo)
    otro = tmp_path / "nuevo-dos"
    assert intents.resolve_project_dir("nuevo", [], [str(nuevo), str(otro)]) is None


def test_resolve_workdir_matches_discovered_basename(tmp_path):
    """A discovered project name (case-insensitive) becomes its exact path."""
    nuevo = tmp_path / "nuevo"
    assert intents.resolve_workdir("Nuevo", [], "/d", [str(nuevo)]) == str(nuevo)


def test_projects_summary_marks_zero_count(tmp_path):
    """Discovered projects appear in the parser context with count 0."""
    summary = intents.projects_summary([], [str(tmp_path / "nuevo")])
    assert summary == f"- {tmp_path / 'nuevo'} | 0"


# ---------------------------------------------------------------------- #
# parser prompt: anti-summarization guidance for create_tasks
# ---------------------------------------------------------------------- #
def test_prompt_create_tasks_forbids_aggressive_summary():
    """The create_tasks guidance forbids aggressive summarization."""
    prompt = intents.build_parser_prompt("texto", "", ".")
    assert "resumir de forma agresiva" in prompt
    assert "PROHIBIDO" in prompt
    assert "sustantiva" in prompt
    assert "transcripción de audio larga" in prompt


def test_prompt_create_tasks_anti_compression_rule():
    """Long/detailed messages must not be compressed for brevity."""
    prompt = intents.build_parser_prompt("texto", "", ".")
    assert "NO comprimas" in prompt
    assert "prioriza no perder información" in prompt
