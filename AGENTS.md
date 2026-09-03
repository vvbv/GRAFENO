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
├── app.py                  # App Textual y entry point (comando `grafeno`); además lleva el tick del planificador (arranque desatendido de tareas programadas, encadenadas y repetitivas); intercepta el comando CLI `grafeno update` antes del argparse
├── config.py               # Config global (~/.grafeno/config.toml): roles CLI+modelo+esfuerzo, automode, auto_update, self_update (auto-actualización de GRAFENO), workspaces raíz (lista de carpetas), paleta (tema), prompt de pasos finales, sección [telegram] (TelegramConfig)
├── models.py               # Dataclasses de dominio (Task, etc.) con to_dict/from_dict
├── paths.py                # Rutas de datos; base sobreescribible con GRAFENO_HOME
├── i18n.py                 # Traducciones en/es; función t("clave", **kwargs)
├── live_log.py             # Persistencia del log en vivo (Text -> logs/live.jsonl, carga al crear el runtime; best-effort)
├── mdnorm.py               # Normalización de Markdown: colapsa saltos de línea y compacta listas sueltas en los .md de cada etapa
├── media.py                # Imágenes del portapapeles: lectura (wl-paste/xclip/pngpaste/osascript), guardado en media/ de la tarea, listado y apertura con el visor del SO; preview inline opcional vía textual-image
├── tokenfmt.py             # Formateo compacto de conteos de tokens (1.2k, 3.4M)
├── timefmt.py              # Formateo de duraciones (42s, 3m 05s, 1h 02m 03s)
├── ratelimit.py            # Detección de usage agotado en CLIs: patrones de error, pista de espera (retry-after, duración relativa u hora absoluta de reseteo con zona horaria) y constantes de sondeo/reintento
├── scheduler.py            # Lógica pura: programación horaria, encadenamiento padre/hija y repetición de tareas
├── updater.py              # Auto-actualización best-effort de los CLIs de agentes (comando nativo de cada uno) al arrancar la TUI si auto_update está activado en el config
├── selfupdate.py           # Auto-actualización de GRAFENO desde las releases de GitHub: chequeo de versión (API releases/latest), comparación semver y comando pipx/pip; comando CLI `grafeno update`
├── workspaces.py           # Workspaces raíz: lectura del nivel proyecto (.grafeno.toml), resolve() global+proyecto y discover() de proyectos sin tareas (subcarpetas de primer nivel)
├── _toml.py                # Serializador TOML propio (escritura; lectura con tomllib)
├── editor.py               # Detección de terminal/editores y apertura del editor al arrancar (config [editor] global + .grafeno.toml por proyecto)
├── gh.py                   # Integración con GitHub CLI: detección de disponibilidad (repo + gh + acceso) y listado de issues abiertos (best-effort, nunca lanza)
├── references.py           # Modelo `Reference` y niveles global/proyecto/tarea con `resolve()`
├── consoles.py             # Consolas del proyecto: ConsoleSpec (nombre/comando/color), paleta CONSOLE_COLORS y persistencia por proyecto bajo ~/.grafeno/consoles/<slug>-<hash8>.toml (migra el [[consoles]] legacy del .grafeno.toml)
├── remote.py               # Proyectos remotos por SSH: parseo del spec, montaje sshfs bajo ~/.grafeno/mounts/ y espejo de datos de la tarea con rsync (best-effort), sondeo del SO destino (detect_os)
├── remotesession.py        # Modo sesión remota (`grafeno [user@]host[:port]`): bootstrap (sondeo de $HOME remoto, mkdir ~/.grafeno, montaje sshfs), activate() exporta GRAFENO_HOME al montaje; spec_for_task/describe_target para el fallback de sesión
├── triggers.py             # Tareas trigger: modelo, niveles global/proyecto, fire() y spawn() best-effort
├── telegram/               # Bot de Telegram (stdlib, sin dependencias nuevas): voz/texto -> tareas, consultas y respuestas con voz
│   ├── api.py              #   Cliente Bot API con urllib (long polling, multipart a mano, troceo 4096); transporte inyectable; el token nunca se loguea
│   ├── stt.py              #   Transcripción vía endpoint OpenAI-compatible (Groq whisper-large-v3-turbo por defecto), best-effort
│   ├── tts.py              #   Voz generada vía endpoint OpenAI-compatible (Groq orpheus, voz masculina `troy` por defecto), opt-in; el WAV del proveedor se convierte a OGG/OPUS con ffmpeg externo (best effort; sin ffmpeg se envía como sendAudio) y los fallos se registran en telegram.log
│   ├── intents.py          #   Interpretación del mensaje con un CLI de agente (prompt one-shot -> JSON): crear/listar tareas/listar proyectos (directorios del scope global)/tareas de un proyecto/estado/archivos/preguntar
│   └── service.py          #   Bucle de polling (worker de la App), whitelist de chats, propuestas con botones inline, creación origin="telegram", notificación de fin
├── drivers/                # Abstracción de CLIs de agentes
│   ├── base.py             #   CLIDriver: ciclo de subproceso asyncio, eventos JSONL; expone variantes de esfuerzo por modelo (variants_command/parse_variants/list_variants_async)
│   ├── opencode.py, kimi.py, codex.py, claude.py#   Dialectos concretos
│   └── __init__.py         #   Registro: get_driver(), available_clis(), fetch_all_models(), fetch_all_variants()
├── pipeline/
│   ├── orchestrator.py     # Orquestador de fases (plan/implementar/revisar/final, automode, ciclos)
│   ├── hooks.py            # Hooks de completado por etapa (comando shell o webhook URL; global + por tarea, mejor esfuerzo)
│   ├── prompts.py          # Prompts por fase, cabecera GRAFENO-EXECUTOR e instrucciones finales personalizables
│   ├── verdict.py          # Parseo del veredicto del revisor (VERDICT: APPROVED / CHANGES_REQUESTED)
│   └── gitops.py           # Rama opcional grafeno/<tarea>; diff base (base_commit) y generacion de changes.md del reporte final
└── tui/
    ├── runtime.py          # TaskRuntime: ejecución en segundo plano por tarea (workers Textual); notifica a la App cuando una ejecución termina en DONE (gancho de encadenamiento/repetición)
    ├── console_pty.py      #   Proceso shell sobre PTY (POSIX): start/read/write/interrupt/close, sin shell=True; lectura no bloqueante; eco del kernel desactivado (la pantalla ecoa localmente)
    ├── dirpicker.py        # Autocompletado de rutas en el formulario
    ├── rolesform.py        # Formulario reutilizable CLI+modelo por rol; incluye filtro de texto sobre el selector de modelos
    ├── refform.py          # Editor reutilizable de referencias (tabla + añadir/borrar)
    ├── trigform.py         # Editor reutilizable de triggers globales (tabla + añadir/borrar)
    ├── widgets.py          # Widgets comunes (cabecera GrafenoHeader con reloj fecha/hora, LocationBar con la ruta actual y la de la tarea + distintivo SSH, barra de fases, helpers Markdown, MediaTextArea que guarda imágenes pegadas e inserta tokens media/media-NN.png)
    └── screens/            # tasks (lista), detail (detalle+acciones), config, roles, consoles (tabs de shells del proyecto)
tests/                      # pytest; conftest aísla GRAFENO_HOME e idioma por test
docs/screenshot.png         # captura de la lista de tareas usada en el README
install.sh, install.ps1     # instaladores de usuario (Linux/macOS y Windows), vía pipx; la ausencia de CLIs de agente es siempre un warning (nunca un error)
```

Los datos en runtime viven en `~/.grafeno/` (`tasks/<fecha>-<slug>/` con
`task.toml`, `plan/`, `review/`, `final/`, `media/`, `logs/live.jsonl`, `logs/*.jsonl`;
además `config.toml`, `references.toml`, `triggers.toml`, `consoles/`,
`mounts/`, `telegram-state.toml`); no
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
- **Auto-actualización de GRAFENO**: `selfupdate.py` consulta la última
  release de GitHub al arrancar la TUI (siempre, worker en segundo
  plano, fallos silenciosos). Con `Config.self_update` activado se
  auto-actualiza (`pipx install --force git+...@vX.Y.Z`, fallback a pip
  del intérprete) y notifica; desactivado, solo muestra `(v X.Y.Z
  available)` en naranja junto a la versión de la cabecera
  (`GrafenoHeader.format_title` + reactive `App.available_update`). El
  comando `grafeno update` (interceptado en `main()` antes del argparse)
  actualiza manualmente y devuelve código de salida 0/1.
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
- **Telegram**: integración opcional de un bot (sección `[telegram]` del
  config + sección en la pantalla de ajustes). El bot corre como worker de
  la App mientras la TUI está abierta (long polling con stdlib urllib:
  cero dependencias nuevas). Acepta texto y notas de voz (transcritas vía
  STT OpenAI-compatible, Groq por defecto), interpreta la intención con el
  CLI del rol planner (o `parser_cli`/`parser_model` propios) mediante un
  prompt one-shot que exige JSON estricto, propone la(s) tarea(s) y las
  crea tras la confirmación con botones inline (automode, `scheduled_at`=
  ahora, `origin="telegram"`: el tick del planificador las arranca
  desatendidas, igual que los triggers).   Antes de crearlas, el bot
  pregunta siempre el encadenamiento con dos botones inline ("ninguna" =
  paralela, "a la última del proyecto" = `parent_id` a la última tarea en
  progreso del proyecto según `IN_PROGRESS_STATES` de
  `telegram/service.py`; sin candidata o con posición inválida según
  `scheduler.rechain_error`, se crea paralela y se avisa). Cuando un mismo
  mensaje crea varias tareas y el usuario elige encadenar, el lote se
  encadena de forma secuencial: la primera tras la última en progreso de
  su proyecto (o paralela con aviso si no hay candidata) y cada una de las
  siguientes tras la anterior del lote del mismo proyecto. La pregunta es
  obligatoria también con `confirm_create` desactivado. El contexto del
  parser incluye las tareas existentes con su directorio (columna extra en
  `intents.tasks_summary`), de modo que las tareas nuevas se enrutan al
  proyecto correspondiente (`workdir` exacto del listado, vía
  `intents.resolve_workdir`; vacío = directorio por defecto como fallback) y
  las referencias a tareas se resuelven de forma inequívoca. Fotos/vídeos
  adjuntos se guardan en `media/` de la primera tarea creada. También
  responde consultas:
  listado de proyectos (directorios distintos con tareas del scope global,
  con su nº de tareas; acción `list_projects` del parser),
  listado de tareas de UN proyecto con su estado (acción
  `list_project_tasks` del parser; `project_ref` se resuelve con
  `intents.resolve_project_dir`: directorio exacto o fragmento único del
  nombre/directorio, ambiguo o desconocido -> aviso `tg.project_not_found`).
  Las consultas de listado de tareas (`list_tasks` y `list_project_tasks`)
  preguntan primero el ámbito de estados con botones inline
  (`PendingListQuery`: todas / todas menos completadas (excluye DONE) /
  selector multiselección de estados con toggle que edita el markup vía
  `api.edit_message_reply_markup`, callbacks `tg:fa/fn/fp/ft/fd`),
  resumen/estado de tareas, envío de los .md de plan/revisión/final como
  documentos y preguntas concretas sobre una tarea (one-shot con los
  artefactos como contexto). Las respuestas de voz (TTS OpenAI-compatible,
  Groq por defecto, voz masculina `troy`) son opt-in (`tts_enabled`).
  Seguridad: whitelist de chat ids (`allowed_chat_ids`; vacío = denegar
  todos, `/start` responde con el chat id para autorizarse), el token
  puede venir de `GRAFENO_TELEGRAM_TOKEN` (tiene prioridad sobre el
  fichero) y nunca se loguea.   Los chats no autorizados reciben un aviso
  con su chat id (cooldown de 5 min; en callbacks, como toast). En grupos
  el bot solo recibe comandos o menciones (privacy mode de BotFather):
  la mención `@bot` se limpia del texto antes de interpretarlo. Mientras
  procesa (STT, interpretación, consultas, envío de archivos) muestra el
  indicador "typing…"/"upload_document" (sendChatAction, refresco cada 4s).
  Con privacy mode desactivado, el gating propio filtra el tráfico de
  grupo: solo se procesan comandos, menciones, respuestas al bot y notas
  de voz (no pueden llevar mención: en el grupo del bot son deliberadas);
  la opción `group_all` del config desactiva ese filtro y todo mensaje de
  un grupo whitelisted llega al parser sin necesidad de mención.
  El parser CLI tiene timeout (120s) y sus fallos se contestan en el chat
  en vez de quedar en silencio. El parser devuelve además el idioma del
  mensaje (`lang`) y el bot contesta en ese idioma (`i18n.t_lang`, por
  chat, sin tocar el idioma global de la TUI). Las claves STT/TTS/token
  se sanealan al resolverlas (toleran prefijos pegados como
  `"groq gsk_..."` o `"Bearer ..."`). Las peticiones HTTP llevan un
  User-Agent de producto (`grafeno/...`): el UA por defecto de urllib es
  bloqueado por Cloudflare en algunos proveedores (Groq: 403/1010), y los
  fallos de STT reportan el motivo al chat (con la clave enmascarada).
  Actividad del bot en `~/.grafeno/telegram.log`
  (recibidos, decisiones, errores; acotado a 1 MB; nunca el token).
  Estado en `~/.grafeno/telegram-state.toml`
  (offset de updates + mapeo task_id→chat_id para la notificación de fin,
  enviada desde `App.task_finished`). El contexto TLS honra
  `GRAFENO_SSL_CA_BUNDLE`/`REQUESTS_CA_BUNDLE` y usa certifi si está
  instalado; por política, los errores de certificado se ignoran: un
  `CERTIFICATE_VERIFY_FAILED` reintenta la petición sin verificación
  (`default_opener` en `telegram/api.py`), de modo que el bot funciona en
  intérpretes sin certificados raíz o tras proxies que interceptan TLS.
- **Referencias**: tres niveles (global `~/.grafeno/references.toml`,
  proyecto `.grafeno.toml` `[[references]]`, tarea). Cada tarea puede
  excluir el nivel global y/o proyecto con sus flags; ``references.resolve``
  combina los tres niveles en orden y los inyecta en los prompts de plan,
  reevaluación e implementación (nunca en revisión, corrección ni pasos
  finales, para acotar el consumo de tokens).
- **Workspaces**: carpetas raíz configurables (global `Config.workspaces`
  en `~/.grafeno/config.toml` + `workspaces` en `.grafeno.toml` de
  proyecto; `workspaces.resolve` las combina). `workspaces.discover`
  lista las subcarpetas de primer nivel como proyectos aunque no tengan
  tareas: el listado de proyectos del bot de Telegram (`list_projects`)
  las combina con los proyectos con tareas (count 0 y marca i18n
  `tg.projects.item_empty`), y la creación de tareas las resuelve como
  `workdir` vía `intents.resolve_workdir`/`resolve_project_dir` con
  `extra_dirs`. Sin duplicados (dedup por ruta resuelta; los specs
  remotos `user@host:...` nunca se fusionan con rutas locales).
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
- **Remoto (SSH/sshfs)**: una tarea puede apuntar a un proyecto remoto
  (`Task.remote`, spec canónico `user@host:/ruta`; `workdir` guarda entonces la
  ruta EN el host remoto). Todo acceso al proyecto usa
  `remote.effective_workdir(task.remote, task.workdir)`: montaje sshfs bajo
  `~/.grafeno/mounts/<slug>-<hash>/` (transparente para los CLIs), o la ruta
  directa cuando el host es la propia máquina (`is_self`). Los datos de la
  tarea se espejan con rsync (`push` tras cada fase y tras los tests; `pull`
  al arrancar la ejecución y al abrir el detalle), siempre mejor esfuerzo: la
  sincronización nunca rompe el pipeline. En el listado, las remotas muestran
  el spec SSH en la columna de directorio y solo aparecen en el scope
  "All tasks". El SO del destino se sondea una vez por tarea
  (remote.detect_os), se persiste en Task.remote_os, se muestra en el detalle
  y se inyecta en los prompts del pipeline (seccion "Entorno remoto") para que
  el plan y las pruebas lo tengan en cuenta.

  Además, `grafeno [user@]host[:port]` (flags `--remote-key`,
  `--remote-password`, `--remote-port`; password también por
  GRAFENO_REMOTE_PASSWORD o prompt) arranca el modo sesión remota: el
  `~/.grafeno` del usuario remoto se monta por sshfs y se exporta como
  GRAFENO_HOME, de modo que toda la TUI (config, tareas, referencias,
  triggers, logs) opera sobre el remoto. Las tareas de sesión guardan
  `workdir` como ruta remota con `task.remote` vacío: el remoto lo aporta la
  sesión vía `remotesession.spec_for_task` y el fallback de
  `remote.effective_workdir`; los montajes de sesión viven fuera de
  GRAFENO_HOME (`<grafeno local>/sessions/<slug>-<hash>/`) y la
  autenticación ssh/sshfs/rsync se centraliza en `remote.py`
  (`_ssh_options`/`_with_auth`, identity o sshpass, nunca persistida). Los
  tests de las tareas de sesión se ejecutan por ssh en el host remoto.
- **Log en vivo**: cada entrada formateada del log de la pestaña Log se
  persiste en `logs/live.jsonl` (`live_log.py`) y se restaura al crear el
  `TaskRuntime`, de modo que sobrevive al cierre de la app. El fichero
  guarda todo el historial; en memoria solo se cargan las últimas
  `_MAX_LOG_ENTRIES` entradas. `reset_to_draft` lo borra.
- **Media**: las imágenes pegadas en la descripción o en "ask for more" se
  guardan en `media/` de la tarea (token `media/media-NN.png` en el texto),
  se listan en la pestaña Media del detalle (ruta absoluta siempre visible;
  preview inline solo con `textual-image` instalado y terminal compatible,
  si no se abren con el visor del SO) y sus rutas absolutas se inyectan en
  los prompts de plan, reevaluación e implementación (nunca en revisión,
  corrección ni pasos finales, igual que las referencias). `list_media`
  acepta png/jpg/jpeg (las fotos de Telegram llegan como JPEG) y
  `save_attachment` guarda adjuntos arbitrarios (imagen o video) con el
  patrón `media-NN<ext>`.
- **changes.md**: al terminar la fase final, el orquestador escribe
  `final/<ciclo>/changes.md` con todos los cambios aportados por la tarea
  (comiteados y sin comitear): commits, `git status`, diff completo contra
  `Task.base_commit` (HEAD registrado al arrancar la implementacion) y el
  contenido de los archivos nuevos sin seguimiento. Es best effort: sin
  repo git no se genera y nunca rompe el pipeline; gitops es solo lectura.
- **Consolas por proyecto**: la lista de tareas (botón/tecla `k`) y el
  detalle de cada tarea (`k`) abren la pantalla de consolas del proyecto
  (en remotas, sobre el montaje sshfs vía `remote.effective_workdir`).
  Cada consola es un `ConsoleSpec` (nombre, comando —vacío = shell del
  usuario— y color) persistido bajo `~/.grafeno/consoles/<slug>-<hash8>.toml`
  (con migración automática del antiguo `[[consoles]]` del proyecto);
  el color tiñe el fondo del tab y el marco del área. Los
  procesos son shells sobre PTY (`tui/console_pty.py`, solo POSIX: en
  Windows se muestra un aviso), orientados a líneas: los programas a
  pantalla completa (alternate screen) se detectan por sus secuencias de
  escape, muestran un aviso y su salida se descarta hasta que salen de
  ese modo; el botón Terminal abre una terminal externa en el directorio
  del proyecto (reutilizando la detección de editor.py), con
  decodificación ANSI vía `rich.ansi.AnsiDecoder`
  y lectura con `loop.add_reader` sobre el fd maestro. Solo se persisten
  las definiciones: los procesos nacen y mueren con la pantalla. Los
  botones de la pantalla (tabs y acciones) usan el modo compacto de
  Textual (una línea, sin borde). El PTY se crea con el eco del kernel
  desactivado y la pantalla ecoa localmente cada línea enviada (junto al
  prompt pendiente), evitando el eco duplicado del shell.
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
