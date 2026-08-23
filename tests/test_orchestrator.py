"""Tests del orquestador con drivers falsos inyectados."""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

from grafeno import models, paths
from grafeno.config import Config
from grafeno.drivers.base import CLIDriver, RunResult, TokenUsage
from grafeno.models import Task, TaskState
from grafeno.pipeline.orchestrator import Orchestrator, PhaseError


class FakeDriver(CLIDriver):
    """Driver de prueba: devuelve resultados en cola, sin subprocesos."""

    def __init__(self, name: str, results: list[RunResult], available: bool = True):
        self.name = name
        self.display_name = name
        self.executable = name
        self._available = available
        self._results = deque(results)
        self.prompts: list[str] = []

    def is_available(self) -> bool:
        return self._available

    def build_command(self, request):
        return []

    def list_models(self):
        return []

    async def run(self, request, on_event=None, on_activity=None):
        self.prompts.append(request.prompt)
        if on_activity:
            on_activity()
        if self._results:
            return self._results.popleft()
        return RunResult(ok=True, text="ok")


def _ok(text: str) -> RunResult:
    return RunResult(ok=True, text=text)


def _make_task(tmp_path, **overrides) -> Task:
    task = Task.create("Demo", "desc", str(tmp_path), Config())
    task.planner.cli = "fake-planner"
    task.implementer.cli = "fake-impl"
    task.reviewer.cli = "fake-rev"
    task.final.cli = "fake-final"
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


def _run(coro):
    return asyncio.run(coro)


def test_automode_happy_path(tmp_path):
    task = _make_task(tmp_path)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("init"), _ok("contenido del plan")]),
        "fake-impl": FakeDriver("fake-impl", [_ok("implementado")]),
        "fake-rev": FakeDriver("fake-rev", [_ok("Todo bien.\nVERDICT: APPROVED")]),
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())

    assert task.state is TaskState.DONE
    assert task.iteration == 0
    # El plan se materializó con la cabecera de ejecutor (fallback de salida).
    plans = list(paths.plan_dir(task.id).glob("*.md"))
    assert len(plans) == 1
    content = plans[0].read_text(encoding="utf-8")
    assert "GRAFENO-EXECUTOR" in content
    assert "cli: fake-impl" in content
    assert "contenido del plan" in content


def test_automode_fix_loop_until_approved(tmp_path):
    task = _make_task(tmp_path)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", [_ok("v1"), _ok("v2 corregida")]),
        "fake-rev": FakeDriver(
            "fake-rev",
            [_ok("Faltan cosas.\nVERDICT: CHANGES_REQUESTED"), _ok("Ahora sí.\nVERDICT: APPROVED")],
        ),
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())

    assert task.state is TaskState.DONE
    assert task.iteration == 1
    # La corrección recibió el prompt de fix que referencia la revisión 01.
    assert "01-review.md" in drivers["fake-impl"].prompts[-1]


def test_automode_max_iterations_exhausted(tmp_path):
    task = _make_task(tmp_path, max_iterations=2)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", []),  # siempre ok
        "fake-rev": FakeDriver("fake-rev", []),    # sin veredicto -> CHANGES_REQUESTED
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())

    assert task.state is TaskState.FAILED
    assert task.iteration == 2


def test_phase_failure_marks_failed(tmp_path):
    task = _make_task(tmp_path)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [RunResult(ok=False, error="boom")]),
        "fake-impl": FakeDriver("fake-impl", []),
        "fake-rev": FakeDriver("fake-rev", []),
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())
    assert task.state is TaskState.FAILED


def test_missing_cli_marks_failed(tmp_path):
    task = _make_task(tmp_path)
    drivers = {"fake-planner": FakeDriver("fake-planner", [], available=False)}
    orch = Orchestrator(task, drivers=drivers)
    try:
        _run(orch.run_plan())
        raise AssertionError("debió lanzar PhaseError")
    except PhaseError as exc:
        assert "fake-planner" in str(exc)
    assert task.state is TaskState.FAILED


def test_unknown_cli_marks_failed(tmp_path):
    task = _make_task(tmp_path)
    task.planner.cli = "codex"  # previsto pero sin driver todavía
    orch = Orchestrator(task, drivers={})
    try:
        _run(orch.run_plan())
        raise AssertionError("debió lanzar PhaseError")
    except PhaseError:
        pass
    assert task.state is TaskState.FAILED


def test_approved_but_failing_tests_requests_changes(tmp_path):
    task = _make_task(tmp_path, test_command="exit 1", max_iterations=1)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", [_ok("v1"), _ok("v2")]),
        "fake-rev": FakeDriver("fake-rev", [_ok("bien\nVERDICT: APPROVED"), _ok("bien\nVERDICT: APPROVED")]),
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())
    # Aprobado pero tests fallando -> corrección -> agota iteraciones -> FAILED
    assert task.state is TaskState.FAILED
    assert task.iteration == 1


def test_session_ids_are_reused(tmp_path):
    task = _make_task(tmp_path)
    impl = FakeDriver(
        "fake-impl",
        [
            RunResult(ok=True, text="v1", session_id="ses-impl"),
            RunResult(ok=True, text="v2", session_id="ses-impl"),
        ],
    )
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": impl,
        "fake-rev": FakeDriver(
            "fake-rev",
            [_ok("no\nVERDICT: CHANGES_REQUESTED"), _ok("sí\nVERDICT: APPROVED")],
        ),
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())
    assert task.sessions.get("implementer") == "ses-impl"


def test_automode_split_plan_and_continue(tmp_path):
    """Automode con punto de confirmación: plan -> (pausa) -> continuar."""
    task = _make_task(tmp_path)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", [_ok("v1")]),
        "fake-rev": FakeDriver("fake-rev", [_ok("bien\nVERDICT: APPROVED")]),
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)

    _run(orch.run_automode_plan())
    assert task.state is TaskState.PLANNED
    assert list(paths.plan_dir(task.id).glob("*.md"))
    # Aún no se ha implementado nada.
    assert drivers["fake-impl"].prompts == []

    _run(orch.run_automode_continue())
    assert task.state is TaskState.DONE
    assert len(drivers["fake-impl"].prompts) == 1


def test_automode_continue_requires_plan(tmp_path):
    task = _make_task(tmp_path)
    infos: list[str] = []
    orch = Orchestrator(task, drivers={}, on_info=infos.append)
    _run(orch.run_automode_continue())
    assert task.state is TaskState.DRAFT
    assert any("plan" in message.lower() for message in infos)


def test_second_cycle_uses_cycle_dirs(tmp_path):
    """'Pedir más': un ciclo nuevo planifica en plan/ciclo-02 y conserva el 1."""
    task = _make_task(tmp_path)
    planner = FakeDriver("fake-planner", [_ok("plan ciclo 1"), _ok("plan ciclo 2")])
    drivers = {
        "fake-planner": planner,
        "fake-impl": FakeDriver("fake-impl", [_ok("impl 1"), _ok("impl 2")]),
        "fake-rev": FakeDriver(
            "fake-rev", [_ok("ok\nVERDICT: APPROVED"), _ok("ok\nVERDICT: APPROVED")]
        ),
        "fake-final": FakeDriver("fake-final", [_ok("cierre"), _ok("cierre 2")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())
    assert task.state is TaskState.DONE
    assert list(paths.plan_dir(task.id, 1).glob("*.md"))

    task.start_new_cycle("añade más cosas")
    models.save(task)
    assert task.cycle == 2
    assert task.iteration == 0
    assert task.state is TaskState.DRAFT
    assert task.current_extension == "añade más cosas"

    orch2 = Orchestrator(task, drivers=drivers)
    _run(orch2.run_automode())
    assert task.state is TaskState.DONE

    cycle2_plans = list(paths.plan_dir(task.id, 2).glob("*.md"))
    assert cycle2_plans and "ciclo-02" in str(cycle2_plans[0])
    assert list(paths.review_dir(task.id, 2).glob("*.md"))
    # El prompt del ciclo 2 incluye la petición de ampliación.
    assert "añade más cosas" in planner.prompts[-1]
    assert "Ampliación" in planner.prompts[-1]
    # El ciclo 1 sigue intacto.
    assert list(paths.plan_dir(task.id, 1).glob("*.md"))
    cycle2_final = list(paths.final_dir(task.id, 2).glob("*.md"))
    assert cycle2_final and "ciclo-02" in str(cycle2_final[0])


def test_agents_md_se_genera_antes_del_plan(tmp_path):
    """Sin AGENTS.md en el proyecto, el planner lo genera antes de planificar."""
    task = _make_task(tmp_path)
    planner = FakeDriver("fake-planner", [_ok("init hecho"), _ok("plan")])
    drivers = {
        "fake-planner": planner,
        "fake-impl": FakeDriver("fake-impl", []),
        "fake-rev": FakeDriver("fake-rev", []),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_plan())

    assert task.state is TaskState.PLANNED
    assert len(planner.prompts) == 2
    assert "AGENTS.md" in planner.prompts[0]   # primero el init
    assert "PLANIFICADOR" in planner.prompts[1]  # después el plan


def test_agents_md_se_omite_si_ya_existe(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Agentes\n", encoding="utf-8")
    task = _make_task(tmp_path)
    planner = FakeDriver("fake-planner", [_ok("plan")])
    drivers = {"fake-planner": planner}
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_plan())

    assert task.state is TaskState.PLANNED
    assert len(planner.prompts) == 1  # solo el prompt de plan


def test_agents_md_fallo_no_falla_la_tarea(tmp_path):
    """Si la generación de AGENTS.md falla, el plan sigue adelante."""
    task = _make_task(tmp_path)
    planner = FakeDriver(
        "fake-planner", [RunResult(ok=False, error="boom"), _ok("plan")]
    )
    drivers = {"fake-planner": planner}
    infos: list[str] = []
    orch = Orchestrator(task, drivers=drivers, on_info=infos.append)
    _run(orch.run_plan())

    assert task.state is TaskState.PLANNED
    assert any("AGENTS.md" in message for message in infos)


def test_durations_are_recorded_and_persisted(tmp_path):
    task = _make_task(tmp_path)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", [_ok("v1"), _ok("v2")]),
        "fake-rev": FakeDriver(
            "fake-rev",
            [_ok("no\nVERDICT: CHANGES_REQUESTED"), _ok("sí\nVERDICT: APPROVED")],
        ),
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    activities: list[str] = []
    orch = Orchestrator(task, drivers=drivers, on_activity=lambda phase: activities.append(phase))
    _run(orch.run_automode())

    assert task.state is TaskState.DONE
    # Cada fase por la que pasó tiene duración registrada.
    for phase in ("plan", "implement", "review", "fix", "final"):
        assert phase in task.durations
        assert task.durations[phase] >= 0
    # El callback de actividad se propaga.
    assert activities
    # Persisten en task.toml.
    from grafeno import models

    reloaded = models.load(task.id)
    assert reloaded.durations == task.durations


def test_automode_runs_final_steps_after_approval(tmp_path):
    """Tras la aprobación, el automode ejecuta los pasos finales y escribe el informe."""
    task = _make_task(tmp_path)
    final = FakeDriver("fake-final", [_ok("informe de cierre")])
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", [_ok("v1")]),
        "fake-rev": FakeDriver("fake-rev", [_ok("bien\nVERDICT: APPROVED")]),
        "fake-final": final,
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())

    assert task.state is TaskState.DONE
    assert len(final.prompts) == 1
    # El prompt de cierre apunta al directorio final del ciclo.
    assert str(paths.final_dir(task.id)) in final.prompts[0]
    # Respaldo: la salida se materializó como 01-final.md.
    report = paths.final_dir(task.id) / "01-final.md"
    assert report.exists()
    assert "informe de cierre" in report.read_text(encoding="utf-8")
    assert "final" in task.durations


def test_final_steps_failure_marks_failed(tmp_path):
    task = _make_task(tmp_path)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", [_ok("v1")]),
        "fake-rev": FakeDriver("fake-rev", [_ok("bien\nVERDICT: APPROVED")]),
        "fake-final": FakeDriver("fake-final", [RunResult(ok=False, error="boom")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())
    assert task.state is TaskState.FAILED


def test_tokens_recorded_per_model(tmp_path):
    task = _make_task(tmp_path)
    task.implementer.model = "prov/Impl-Model"
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", [
            RunResult(ok=True, text="impl", tokens=TokenUsage(input=1000, output=200)),
        ]),
        "fake-rev": FakeDriver("fake-rev", [_ok("VERDICT: APPROVED")]),
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())

    assert task.state is TaskState.DONE
    by_model = task.tokens_by_model()
    assert by_model["prov/Impl-Model"] == (1000, 200)
    # Persistido: se recarga de task.toml sin pérdida.
    assert models.load(task.id).tokens == task.tokens
