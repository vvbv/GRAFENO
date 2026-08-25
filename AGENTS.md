# AGENTS.md — GRAFENO

Orquestador TUI multi-CLI para tareas de programación: pipeline
**plan -> implementación -> revisión <=> corrección -> pasos finales** usando
CLIs de agentes instalados en el sistema (OpenCode, Kimi, Codex y Claude Code).

## Stack

- Python >= 3.11, única dependencia de runtime: `textual` (TUI).
- Empaquetado con setuptools, layout `src/` (`pyproject.toml`).
- Tests: `pytest` (extra `dev`).
- Sin formateador/linter configurado: seguir el estilo existente.

## Estructura

```
src/grafeno/
├── app.py                  # App Textual y entry point (comando `grafeno`); además lleva el tick del planificador (arranque desatendido de tareas programadas, encadenadas y repetitivas)
├── config.py               # Config global (~/.grafeno/config.toml): roles CLI+modelo+esfuerzo, automode, auto_update, paleta (tema), prompt de pasos finales
├── models.py               # Dataclasses de dominio (Task, etc.) con to_dict/from_dict
├── paths.py                # Rutas de datos; base sobreescribible con GRAFENO_HOME
├── i18n.py                 # Traducciones en/es; función t("clave", **kwargs)
├── live_log.py             # Persistencia del log en vivo (Text -> logs/live.jsonl, carga al crear el runtime; best-effort)
├── mdnorm.py               # Normalización de Markdown: colapsa saltos de línea y compacta listas sueltas en los .md de cada etapa
├── tokenfmt.py             # Formateo compacto de conteos de tokens (1.2k, 3.4M)
├── timefmt.py              # Formateo de duraciones (42s, 3m 05s, 1h 02m 03s)
├── ratelimit.py            # Detección de usage agotado en CLIs: patrones de error, pista de espera (retry-after) y constantes de sondeo/reintento
├── scheduler.py            # Lógica pura: programación horaria, encadenamiento padre/hija y repetición de tareas
├── updater.py              # Auto-actualización best-effort de los CLIs de agentes (comando nativo de cada uno) al arrancar la TUI si auto_update está activado en el config
├── _toml.py                # Serializador TOML propio (escritura; lectura con tomllib)
├── editor.py               # Detección de terminal/editores y apertura del editor al arrancar (config [editor] global + .grafeno.toml por proyecto)
├── gh.py                   # Integración con GitHub CLI: detección de disponibilidad (repo + gh + acceso) y listado de issues abiertos (best-effort, nunca lanza)
├── references.py           # Modelo `Reference` y niveles global/proyecto/tarea con `resolve()`
├── triggers.py             # Tareas trigger: modelo, niveles global/proyecto, fire() y spawn() best-effort
├── drivers/                # Abstracción de CLIs de agentes
│   ├── base.py             #   CLIDriver: ciclo de subproceso asyncio, eventos JSONL; expone variantes de esfuerzo por modelo (variants_command/parse_variants/list_variants_async)
│   ├── opencode.py, kimi.py, codex.py, claude.py#   Dialectos concretos
│   └── __init__.py         #   Registro: get_driver(), available_clis(), fetch_all_models(), fetch_all_variants()
├── pipeline/
│   ├── orchestrator.py     # Orquestador de fases (plan/implementar/revisar/final, automode, ciclos)
│   ├── hooks.py            # Hooks de completado por etapa (comando shell o webhook URL; global + por tarea, mejor esfuerzo)
│   ├── prompts.py          # Prompts por fase, cabecera GRAFENO-EXECUTOR e instrucciones finales personalizables
│   ├── verdict.py          # Parseo del veredicto del revisor (VERDICT: APPROVED / CHANGES_REQUESTED)
│   └── gitops.py           # Rama opcional grafeno/<tarea>
└── tui/
    ├── runtime.py          # TaskRuntime: ejecución en segundo plano por tarea (workers Textual); notifica a la App cuando una ejecución termina en DONE (gancho de encadenamiento/repetición)
    ├── dirpicker.py        # Autocompletado de rutas en el formulario
    ├── rolesform.py        # Formulario reutilizable CLI+modelo por rol; incluye filtro de texto sobre el selector de modelos
    ├── refform.py          # Editor reutilizable de referencias (tabla + añadir/borrar)
    ├── trigform.py         # Editor reutilizable de triggers globales (tabla + añadir/borrar)
    ├── widgets.py          # Widgets comunes (cabecera GrafenoHeader con reloj fecha/hora, barra de fases, helpers Markdown)
    └── screens/            # tasks (lista), detail (detalle+acciones), config, roles
tests/                      # pytest; conftest aísla GRAFENO_HOME e idioma por test
install.sh, install.ps1     # instaladores de usuario (Linux/macOS y Windows), vía pipx; la ausencia de CLIs de agente es siempre un warning (nunca un error)
```

Los datos en runtime viven en `~/.grafeno/` (`tasks/<fecha>-<slug>/` con
`task.toml`, `plan/`, `review/`, `final/`, `logs/live.jsonl`, `logs/*.jsonl`); no
en el repo.

## Compilar / ejecutar / tests

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/grafeno              # ejecutar la TUI
.venv/bin/python -m pytest     # tests (testpaths = tests)
```

Instalación de usuario: `pipx install .` o `./install.sh` / `install.ps1`.

## Convenciones

- **Idioma del código**: docstrings y comentarios en INGLÉS; mensajes de
  usuario vía `t("clave")` en `i18n.py` (añadir la clave en ambos idiomas);
  identificadores en inglés. Todo texto de UI pasa por `t("clave")` y se
  traduce en el catálogo de `i18n.py`.
- **Estilo**: `from __future__ import annotations` en cada módulo; docstring de
  módulo en la primera línea; dataclasses con `to_dict`/`from_dict` para
  persistencia; type hints modernos (`str | None`); async/await con
  subprocesos asyncio para los CLIs (sin `shell=True`); comentarios en línea
  breves tras el código cuando aclaran un valor (alineados a la derecha).
- **Drivers**: añadir un CLI = nuevo archivo en `drivers/` que herede de
  `CLIDriver` (implementar `build_command`, `models_command`, `parse_models`,
  `decode_event`, `extract_usage`) y registrarlo en `drivers/__init__.py`.
  La lectura de streams usa `read_lines` (chunked), nunca el reader de líneas
  de asyncio (bug de 64 KiB). `decode_line` devuelve además del evento el
  `TokenUsage` acumulado cuando el CLI emite eventos de uso. El nivel de
  trabajo del modelo viaja en `RunRequest.effort`; los CLIs sin soporte
  exponen `variants_command() -> []` y `parse_variants -> {}` (defecto de
  la base) e ignoran el campo en `build_command`. Cada driver puede
  exponer además `update_command()` (defecto `[]` en la base = CLI sin
  comando nativo de auto-actualización, p.ej. `claude update`,
  `opencode upgrade`, `kimi update`) que `updater.update_all()` ejecuta
  en segundo plano al arrancar la TUI si `Config.auto_update` está
  activado. `RunResult.usage_wait` propaga al orquestador la pista de
  espera cuando se detecta uso agotado en el run (reintenta la fase con
  la espera indicada o sondea cada `PROBE_SECONDS` hasta `MAX_ATTEMPTS`;
  durante la espera la TUI muestra el sufijo i18n `state.waiting` en la
  lista de tareas y en `PhaseBar`).
- **Editor**: la apertura automática usa `editor.py` (mejor esfuerzo,
  nunca bloquea la TUI). Config global en `[editor]`, sobreescritura
  por proyecto en `<proyecto>/.grafeno.toml`; el flag `--noeditor`
   la desactiva. Por defecto desactivado: sin editor configurado solo
   se abre la TUI.
- **GitHub (gh)**: el formulario de nueva tarea muestra un selector
  opcional de issues abiertos cuando el directorio es un repositorio con
  `gh` instalado y acceso autenticado (`gh.py`); al elegir un issue se
  rellenan nombre y descripción de la tarea. La carga se hace en segundo
  plano y nunca bloquea ni rompe el formulario.
- **Referencias**: tres niveles (global `~/.grafeno/references.toml`,
  proyecto `.grafeno.toml` `[[references]]`, tarea). Cada tarea puede
  excluir el nivel global y/o proyecto con sus flags; ``references.resolve``
  combina los tres niveles en orden y los inyecta en los prompts de plan,
  reevaluación e implementación (nunca en revisión, corrección ni pasos
  finales, para acotar el consumo de tokens).
- **Tareas trigger**: dos niveles (global `~/.grafeno/triggers.toml`,
  proyecto `.grafeno.toml` `[[triggers]]`). Cada trigger define fases
  (`all` o lista de `HOOK_STAGES`) y momento (`before`/`after`); al
  dispararse crea una tarea independiente (automode, `scheduled_at`=ahora,
  `origin="trigger"`) que arranca con el tick del planificador: nunca
  bloquean ni rompen el pipeline, y las tareas con `origin="trigger"` no
  disparan más triggers (sin recursión).
- **Planificador**: las tareas pueden tener `scheduled_at`, `parent_id`
  y modo de repetición (`interval`/`infinite`) con política de plan
  (`reuse`/`replan`/`reevaluate`). El arranque desatendido usa siempre el
  pipeline completo (automode) e ignora `confirm_plan`; las pausas
  manuales (PAUSED) nunca se auto-arrancan. La reencadenación (cambio de
  `parent_id` desde la pantalla de edición, sin tocar el estado) valida en
  `scheduler.rechain_error` que la posición no tenga tareas completadas
  (padre e hijas del padre fuera de DONE/DISCARDED) y que no haya ciclos.
- **Log en vivo**: cada entrada formateada del log de la pestaña Log se
  persiste en `logs/live.jsonl` (`live_log.py`) y se restaura al crear el
  `TaskRuntime`, de modo que sobrevive al cierre de la app. El fichero
  guarda todo el historial; en memoria solo se cargan las últimas
  `_MAX_LOG_ENTRIES` entradas. `reset_to_draft` lo borra.
- **Tests**: un archivo `test_<modulo>.py` por módulo; fixtures autouse en
  `conftest.py` ya aíslan `GRAFENO_HOME` y fijan idioma inglés; drivers falsos
  para el orquestador; smoke tests TUI con el modo headless de Textual
  (`run_test`).
- **Commits**: Conventional Commits en inglés por defecto (los prompts de
  los agentes lo imponen salvo indicación contraria en AGENTS.md o la tarea)
  (`feat:`, `fix(drivers):`, `test:`, `docs:`), mensaje corto en minúsculas
  que describe el cambio funcional, a veces con detalle entre paréntesis
  (p.ej. `test: ciclos, paralelismo, confirmaciones, config y prompts (56 tests)`).
- **Sin emojis** en código, docs ni UI.

## Versionado y releases

- La versión se declara en DOS sitios que deben quedar SIEMPRE
  sincronizados: `pyproject.toml` (`[project].version`) y
  `src/grafeno/__init__.py` (`__version__`). Cualquier cambio de versión
  actualiza ambos en el mismo commit.
- Toda modificación del proyecto (feature, fix, refactor, docs relevantes)
  incrementa la versión siguiendo semver:
  - **patch** (X.Y.Z+1): correcciones y cambios menores sin cambio de
    comportamiento visible.
  - **minor** (X.Y+1.0): funcionalidades nuevas retrocompatibles.
  - **major** (X+1.0.0): cambios que rompen compatibilidad.
- El commit que sube la versión sigue el formato
  `chore(release): bump a X.Y.Z`.
- Releases: se generan automáticamente al hacer push a `main` con el bump
  de versión (commit `chore(release): bump a X.Y.Z`). El workflow
  `.github/workflows/release.yml` se dispara cuando cambian
  `src/grafeno/__init__.py` o `pyproject.toml`, detecta si la versión se
  incrementó respecto al commit anterior, valida que ambos archivos
  coinciden, construye el paquete y crea el GitHub Release con el tag
  `vX.Y.Z`. Si la versión no cambió, no publica nada. No hace falta crear
  tags a mano.
