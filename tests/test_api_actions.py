"""Tests of the task operations added for the web panel (server/actions.py)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from grafeno import models, paths
from grafeno.config import Config, RoleConfig
from grafeno.i18n import t
from grafeno.models import Task, TaskState
from grafeno.server import actions
from grafeno.server.actions import ApiError
from grafeno.tui.runtime import TaskRuntime


class _Orch:
    """Orchestrator stub: records the runner called; ``gate`` keeps it running."""

    calls: list[str] = []
    gate: asyncio.Event | None = None

    def __init__(self, task: Task, **_: Any) -> None:
        self.task = task

    def __getattr__(self, name: str):
        if not name.startswith("run_"):
            raise AttributeError(name)

        async def runner(*_: Any) -> bool:
            _Orch.calls.append(name)
            if _Orch.gate is not None:
                await _Orch.gate.wait()
            return True

        return runner


class FakeApp:
    def __init__(self) -> None:
        self.runtimes: dict[str, TaskRuntime] = {}
        self.available_models: dict[str, list[str]] = {}

    def runtime_for(self, task: Task) -> TaskRuntime:
        runtime = self.runtimes.get(task.id)
        if runtime is None:
            runtime = TaskRuntime(task, orchestrator_factory=_Orch)
            self.runtimes[task.id] = runtime
        elif not runtime.running:
            runtime.task = task
        return runtime

    def notify(self, *_: Any, **__: Any) -> None:
        pass

    def run_worker(self, coro, **_: Any):
        return asyncio.ensure_future(coro)


class FakeService:
    def __init__(self, app: FakeApp | None) -> None:
        self.app = app

    def _log(self, message: str) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_stub():
    _Orch.calls = []
    _Orch.gate = None
    yield


def _task(tmp_path, name: str = "Demo", **overrides) -> Task:
    task = Task.create(name, "desc", str(tmp_path), Config(), **overrides)
    models.save(task)
    return task


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------- #
# Task creation with every form option
# ---------------------------------------------------------------------- #
def test_create_task_with_every_option(tmp_path) -> None:
    service = FakeService(FakeApp())
    payload = {
        "name": "Full",
        "workdir": str(tmp_path),
        "description": "d",
        "automode": False,
        "test_command": "pytest -q",
        "create_branch": False,
        "confirm_plan": True,
        "first_prompt": "first",
        "final_prompt": "final",
        "hook_command": "echo hi",
        "hook_stages": ["plan", "tests", "bogus"],
        "hook_mode": "both",
        "scheduled_at": "2030-01-02 03:04",
        "repeat_mode": "interval",
        "repeat_interval_minutes": 15,
        "plan_reuse": "reevaluate",
        "use_global_references": False,
        "use_project_references": True,
        "references": [{"name": "docs", "path": "https://example.com", "description": "x"}, {"name": ""}],
    }
    status, result = asyncio.run(actions.create_task(service, payload))
    assert status == 201
    task = models.load(result["task"]["id"])
    assert task.automode is True  # repetitive tasks always run in automode
    assert task.test_command == "pytest -q"
    assert task.create_branch is False and task.confirm_plan is True
    assert task.first_prompt == "first" and task.final_prompt == "final"
    assert task.hook_command == "echo hi"
    assert task.hook_stages == "plan,tests"
    assert task.hook_mode == "both"
    assert task.scheduled_at == "2030-01-02T03:04"
    assert (task.repeat_mode, task.repeat_interval_minutes, task.plan_reuse) == ("interval", 15, "reevaluate")
    assert task.use_global_references is False and task.use_project_references is True
    assert [ref.name for ref in task.references] == ["docs"]


def test_create_task_defaults_from_config(tmp_path) -> None:
    status, result = asyncio.run(actions.create_task(FakeService(None), {"name": "D", "workdir": str(tmp_path)}))
    task = models.load(result["task"]["id"])
    cfg = Config()
    assert task.test_command == cfg.automode.test_command
    assert task.hook_mode == "override"
    assert task.plan_reuse == "reuse"


def test_create_task_with_profile(tmp_path) -> None:
    from grafeno import profiles

    profiles.save_global([profiles.Profile(name="fast", planner=RoleConfig("claude", "sonnet", "high"))])
    service = FakeService(FakeApp())
    status, result = asyncio.run(actions.create_task(service, {
        "name": "P", "workdir": str(tmp_path), "profile": "fast",
    }))
    assert status == 201
    task = models.load(result["task"]["id"])
    assert task.profile == "fast"
    assert task.planner.cli == "claude" and task.planner.model == "sonnet"


def test_create_task_with_unknown_profile(tmp_path) -> None:
    with pytest.raises(ApiError) as exc:
        asyncio.run(actions.create_task(FakeService(None), {
            "name": "P", "workdir": str(tmp_path), "profile": "ghost",
        }))
    assert exc.value.status == 400
    assert "unknown profile" in exc.value.message


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"name": "x", "workdir": "/definitely/missing"}, "nt.error.bad_dir"),
        ({"name": "x", "workdir": ".", "scheduled_at": "tomorrow"}, "nt.error.bad_schedule"),
        ({"name": "x", "workdir": ".", "repeat_mode": "interval", "repeat_interval_minutes": 0}, "nt.error.bad_interval"),
        ({"name": "x", "remote": "not a spec"}, "nt.error.bad_remote"),
    ],
)
def test_create_task_validation(payload, message) -> None:
    with pytest.raises(ApiError) as exc:
        asyncio.run(actions.create_task(FakeService(None), payload))
    assert exc.value.status == 400
    assert exc.value.message.split(":")[0] == t(message, path="/definitely/missing").split(":")[0]


def test_create_task_remote_spec(tmp_path) -> None:
    status, result = asyncio.run(actions.create_task(FakeService(None), {"name": "R", "remote": "me@host:/srv/app"}))
    task = models.load(result["task"]["id"])
    assert task.remote == "me@host:/srv/app"
    assert task.workdir == "/srv/app"


# ---------------------------------------------------------------------- #
# Detail extras and available actions
# ---------------------------------------------------------------------- #
def test_detail_extras(tmp_path) -> None:
    task = _task(tmp_path)
    task.state = TaskState.IMPLEMENTED
    task.tokens = {"plan|opencode|m|input": 10, "plan|opencode|m|output": 5}
    models.save(task)
    (paths.plan_dir(task.id, 1) / "plan.md").write_text("# p")
    (paths.plan_dir(task.id, 2) / "plan.md").write_text("# p2")
    (paths.media_dir(task.id) / "media-01.png").write_bytes(b"png")
    detail = actions.get_task(FakeService(FakeApp()), task.id)["task"]
    assert detail["artifacts"]["plan"] == ["ciclo-02/plan.md", "plan.md"]
    assert detail["media"] == ["media-01.png"]
    assert [item["key"] for item in detail["phase_bar"]] == ["plan", "implement", "review", "final", "done"]
    assert detail["phase_bar"][1]["status"] == "done"
    assert detail["actions"]["review"] is True
    assert detail["actions"]["final"] is False
    assert detail["actions"]["cancel"] is False
    assert detail["tokens_by_phase"][0]["input"] == 10
    assert detail["runtime"]["running"] is False


def test_artifact_file_and_traversal(tmp_path) -> None:
    task = _task(tmp_path)
    (paths.plan_dir(task.id, 2) / "plan.md").write_text("# Title\n\n\n\nbody")
    data = actions.get_artifact_file(FakeService(None), task.id, "plan", "ciclo-02/plan.md")
    assert data["content"].startswith("# Title")
    for bad in ("../task.toml", "../../x.md", "", "/etc/passwd"):
        with pytest.raises(ApiError):
            actions.get_artifact_file(FakeService(None), task.id, "plan", bad)


def test_media_bytes_and_bad_names(tmp_path) -> None:
    task = _task(tmp_path)
    (paths.media_dir(task.id) / "media-01.png").write_bytes(b"\x89PNG")
    data, mime = actions.get_media(FakeService(None), task.id, "media-01.png")
    assert (data, mime) == (b"\x89PNG", "image/png")
    for bad in ("../task.toml", ".hidden", ""):
        with pytest.raises(ApiError):
            actions.get_media(FakeService(None), task.id, bad)


# ---------------------------------------------------------------------- #
# Pipeline actions
# ---------------------------------------------------------------------- #
def test_run_phase_rules(tmp_path) -> None:
    async def scenario():
        service = FakeService(FakeApp())
        task = _task(tmp_path)
        with pytest.raises(ApiError) as exc:
            actions.run_phase(service, task.id, "implement")
        assert exc.value.message == t("api.err.need_plan")
        with pytest.raises(ApiError):
            actions.run_phase(service, task.id, "tests")  # no test command
        with pytest.raises(ApiError):
            actions.run_phase(service, task.id, "bogus")
        assert actions.run_phase(service, task.id, "plan") == {"ok": True}
        await _settle()
        assert _Orch.calls == ["run_plan"]

    asyncio.run(scenario())


def test_automode_respects_confirm_plan(tmp_path) -> None:
    async def scenario():
        app = FakeApp()
        task = _task(tmp_path, confirm_plan=True)
        actions.run_phase(FakeService(app), task.id, "automode")
        await _settle()
        assert _Orch.calls == ["run_automode_plan"]
        assert app.runtimes[task.id]._plan_then_ask is False  # reset when the run ends

    asyncio.run(scenario())


def test_running_task_blocks_actions_and_cancel_waits(tmp_path) -> None:
    async def scenario():
        _Orch.gate = asyncio.Event()  # keeps the run alive until cancelled
        app = FakeApp()
        service = FakeService(app)
        task = _task(tmp_path)
        actions.run_phase(service, task.id, "plan")
        await _settle()
        assert actions.get_task(service, task.id)["task"]["actions"]["cancel"] is True
        with pytest.raises(ApiError) as exc:
            actions.run_phase(service, task.id, "plan")
        assert exc.value.status == 409
        with pytest.raises(ApiError):
            actions.edit_task(service, task.id, {"name": "x"})
        result = await actions.discard_task(service, task.id)
        assert result["state"] == "discarded"
        # The cancelled worker saved PAUSED first; discard must win.
        assert models.load(task.id).state is TaskState.DISCARDED

    asyncio.run(scenario())


def test_continue_and_approve_plan(tmp_path) -> None:
    async def scenario():
        service = FakeService(FakeApp())
        task = _task(tmp_path)
        with pytest.raises(ApiError):
            actions.continue_task(service, task.id)  # DRAFT is not interrupted
        with pytest.raises(ApiError):
            actions.approve_plan(service, task.id)
        task.state = TaskState.IMPLEMENTING  # orphan transient state
        models.save(task)
        actions.continue_task(service, task.id)
        await _settle()
        task = models.load(task.id)
        task.state = TaskState.PLANNED
        models.save(task)
        actions.approve_plan(service, task.id)
        await _settle()
        assert _Orch.calls == ["run_continue", "run_automode_continue"]

    asyncio.run(scenario())


def test_reset_clears_artifacts(tmp_path) -> None:
    task = _task(tmp_path)
    task.state = TaskState.DONE
    models.save(task)
    (paths.plan_dir(task.id, 1) / "plan.md").write_text("# p")
    result = asyncio.run(actions.reset_task(FakeService(FakeApp()), task.id))
    assert result["state"] == "draft"
    assert not list(paths.plan_dir(task.id, 1).glob("*.md"))


def test_extend_with_attachments(tmp_path) -> None:
    async def scenario():
        task = _task(tmp_path)
        actions.extend_task(FakeService(FakeApp()), task.id, "More", attachments=[("shot.png", b"png")])
        await _settle()
        reloaded = models.load(task.id)
        assert reloaded.cycle == 2
        assert reloaded.extensions["2"].endswith("- media/media-01.png")
        assert _Orch.calls == ["run_automode"]

    asyncio.run(scenario())


# ---------------------------------------------------------------------- #
# Edit and roles
# ---------------------------------------------------------------------- #
def test_edit_task_and_rechain(tmp_path) -> None:
    service = FakeService(None)
    parent = _task(tmp_path, "Parent")
    child = _task(tmp_path, "Child")
    actions.edit_task(service, child.id, {"name": "Renamed", "description": "new", "parent_id": parent.id})
    reloaded = models.load(child.id)
    assert (reloaded.name, reloaded.description, reloaded.parent_id) == ("Renamed", "new", parent.id)
    with pytest.raises(ApiError) as exc:
        actions.edit_task(service, parent.id, {"parent_id": child.id})
    assert exc.value.message == t("et.error.parent_cycle")
    with pytest.raises(ApiError):
        actions.edit_task(service, child.id, {"name": "  "})


def test_update_roles_and_profile(tmp_path) -> None:
    from grafeno import profiles

    profiles.save_global([profiles.Profile(name="fast", planner=RoleConfig("claude", "sonnet", "high"))])
    service = FakeService(None)
    task = _task(tmp_path)
    actions.update_roles(service, task.id, {"profile": "fast"})
    reloaded = models.load(task.id)
    assert reloaded.profile == "fast"
    assert (reloaded.planner.cli, reloaded.planner.model, reloaded.planner.effort) == ("claude", "sonnet", "high")
    # Explicit roles that no longer match the profile drop its name.
    actions.update_roles(service, task.id, {"profile": "fast", "roles": {"planner": {"cli": "codex"}}})
    reloaded = models.load(task.id)
    assert reloaded.planner.cli == "codex" and reloaded.profile == ""
    with pytest.raises(ApiError):
        actions.update_roles(service, task.id, {"roles": {"planner": {"cli": "nope"}}})
