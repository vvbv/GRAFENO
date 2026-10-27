# AGENTS.md — GRAFENO

Orquestador TUI multi-CLI para tareas de programación: pipeline
**plan -> implementación -> revisión <=> corrección -> pasos finales** usando
CLIs de agentes instalados en el sistema (OpenCode, Kimi, Codex, Claude Code, Cursor y MiniMax Code).

## Stack

- Python >= 3.11, única dependencia de runtime: `textual` (TUI).
- Empaquetado con setuptools, layout `src/` (`pyproject.toml`).
- Tests: `pytest` (extra `dev`).
- Sin formateador/linter configurado: seguir el estilo existente.

## Estructura

```
src/grafeno/
├── app.py                  # App Textual y entry point (comando `grafeno`); además lleva el tick del planificador (arranque desatendido de tareas programadas, encadenadas y repetitivas); intercepta el comando CLI `grafeno update` antes del argparse; `--version`/`-v` imprime la versión; `--web` (+ `--web-host`/`--web-port`/`--no-auth`, que lo implican) activa el panel web solo para esa ejecución (`--no-auth`: sin autenticación); arranca el servidor API (worker `api-server`) si `cfg.api.enabled` o con `--web`; worker `models-check` al arrancar: avisa si los modelos de la config/perfiles ya no existen en su CLI (los de la tarea se muestran en una banda roja del detalle)
├── config.py               # Config global (~/.grafeno/config.toml): roles CLI+modelo+esfuerzo (incluido `first` para el primer paso), automode, auto_update, self_update (auto-actualización de GRAFENO), workspaces raíz (lista de carpetas), paleta (tema), idioma de la GUI (`language`) y de los prompts internos (`prompt_language`, vacío = el de la GUI), prompt de primer paso (first step, opcional) y de pasos finales, sección [telegram] (TelegramConfig), sección [api] (ApiConfig: enabled/host/port/tokens + env GRAFENO_API_TOKEN), sección [web] (WebConfig: `instance_name`/`theme`/`favicon`, temas en `WEB_THEMES`)
├── models.py               # Dataclasses de dominio (Task, etc.) con to_dict/from_dict; incluye `failed_phase` (fase del pipeline que fallo, para reanudar)
├── paths.py                # Rutas de datos; base sobreescribible con GRAFENO_HOME; incluye `api_log_path()`, `uploads_dir()` (spool de subidas binarias de la API) y `web_dir()` (favicon propio del panel)
├── i18n.py                 # Traducciones en/es; función t("clave", **kwargs); además el idioma de los prompts internos (set_prompt_language/prompt_language, vacío = el de la GUI) y prompt_template() para elegir la variante de una plantilla por idioma
├── live_log.py             # Persistencia del log en vivo (Text -> logs/live.jsonl, carga al crear el runtime; best-effort)
├── mdnorm.py               # Normalización de Markdown: colapsa saltos de línea y compacta listas sueltas en los .md de cada etapa; `markdown_outline` da el esquema de encabezados con offsets
├── media.py                # Imágenes del portapapeles: lectura (wl-paste/xclip/pngpaste/osascript), guardado en media/ de la tarea, listado y apertura con el visor del SO; preview inline opcional vía textual-image; adjuntos de referencia de cualquier tipo (Word, Excel, PDF, imágenes, vídeo, otros): grupos de sufijos (`ATTACHMENT_KINDS`, `SAFE_SUFFIXES`, el resto `.bin`), tope `MAX_ATTACHMENT_BYTES` (500 MiB), `save_attachment_file` (copia/mueve desde disco sin cargarlo en memoria, chequeo de espacio libre `has_free_space`), `base64_length` y `LARGE_ATTACHMENT_BYTES` (50 MiB: los prompts avisan de leerlo por partes), manifiesto `attachments.toml` en la carpeta de la tarea (dataclass `Attachment`: nombre original, tipo, tamaño, origen, ciclo) con `list_attachments`/`delete_attachment`
├── modelcheck.py           # Verificación de modelos retirados: compara los pares cli+modelo configurados (config general + parser de Telegram, perfiles y tareas) con la lista que reporta cada CLI; lógica pura (collect_*/used_clis/find_missing/format_issues)
├── tokenfmt.py             # Formateo compacto de conteos de tokens (1.2k, 3.4M)
├── timefmt.py              # Formateo de duraciones (42s, 3m 05s, 1h 02m 03s); `format_countdown` (distancia gruesa: 45m, 6h 05m, 2d 03h)
├── ratelimit.py            # Detección de usage agotado en CLIs: patrones de error, pista de espera (retry-after, duración relativa u hora absoluta de reseteo con zona horaria) y constantes de sondeo/reintento (PROBE_SECONDS, MAX_ATTEMPTS, PASSIVE_WAIT_SECONDS); además fallos transitorios (red, 5xx del proveedor, apagado del servicio del CLI: looks_like_transient_failure, TRANSIENT_MAX_ATTEMPTS, TRANSIENT_WAITS); y salidas no clasificadas con código distinto de 0 (CRASH_MAX_FAILURES, CRASH_WAITS, crash_wait)
├── scheduler.py            # Lógica pura: programación horaria, encadenamiento padre/hija y repetición de tareas; además `next_start` (próximo arranque efectivo: `scheduled_at` o `last_completed_at` + intervalo), `schedule_info`/`format_schedule` (estado derivado de solo lectura future/overdue/waiting_parent y texto con fecha completa, hoy/mañana, offset UTC y cuenta atrás)
├── usage.py                # Ledger de uso con fecha (~/.grafeno/usage.toml, [[record]] append-only): registro de tokens/tiempo desde el orquestador, backfill único desde las tareas existentes (fechado por updated_at), períodos (día/semana/mes) y agregación para la pantalla de reportes
├── updater.py              # Auto-actualización best-effort de los CLIs de agentes (comando nativo de cada uno) al arrancar la TUI si auto_update está activado en el config
├── selfupdate.py           # Auto-actualización de GRAFENO desde las releases de GitHub: chequeo de versión (API releases/latest) con motivo del fallo y fallback a la redirección web de releases/latest; instala el wheel de la release si existe (si no, el tag git), comparación semver y comando pipx/pip; comando CLI `grafeno update`
├── workspaces.py           # Workspaces raíz (directorios de trabajo): lectura del nivel proyecto (.grafeno.toml), effective_entries()/resolve() global+proyecto, roots() (estado de cada entrada: ruta resuelta y si existe), projects()/discover() (subcarpetas de primer nivel con su raíz, orden sin distinguir mayúsculas) y normalize_entries()/entry_key() (limpieza y dedupe de la lista guardada)
├── projectsetup.py         # Creación de proyectos en los workspaces: validación de nombre/URL/workspace, modos init (git init, defecto)/clone (git clone http(s)/ssh/scp)/plain (sin Git), git asíncrono sin shell ni prompts (GIT_ALLOW_PROTOCOL), sin terminal de control (sesión propia en POSIX, grupo de procesos sin consola en Windows), timeout/cancelación que matan el árbol de git con espera acotada (código timeout -> 504), ConnectTimeout/low speed por defecto, redacción de credenciales y limpieza si git falla (quita atributos de solo lectura); ProjectError(code, message)
├── _toml.py                # Serializador TOML propio (escritura; lectura con tomllib)
├── editor.py               # Detección de terminal/editores y apertura del editor al arrancar (config [editor] global + .grafeno.toml por proyecto)
├── gh.py                   # Integración con GitHub CLI: detección de disponibilidad (repo + gh + acceso) y listado de issues abiertos (best-effort, nunca lanza)
├── references.py           # Modelo `Reference` y niveles global/proyecto/tarea con `resolve()`
├── profiles.py             # Perfiles de procesamiento con nombre: Profile (5 RoleConfig), ~/.grafeno/profiles.toml, find() por nombre y helper roles_summary() (formato compacto cli/model compartido por Profile.summary, /api/v1/options y la línea de ayuda del selector del panel web)
├── consoles.py             # Consolas del proyecto: ConsoleSpec (nombre/comando/color), paleta CONSOLE_COLORS y persistencia por proyecto bajo ~/.grafeno/consoles/<slug>-<hash8>.toml (migra el [[consoles]] legacy del .grafeno.toml)
├── clisessions.py          # Sesiones CLI directas (conversaciones con Claude Code u OpenCode en la carpeta de un proyecto, sin tarea): `CliSession`, `SESSION_CLIS` (único punto para ampliar los CLIs), almacén en `~/.grafeno/cli-sessions/<id>/` (`session.toml`, `transcript.jsonl` best effort, `logs/turn-NNN.jsonl`), ids validados contra path traversal y transcript incremental con `seq` global
├── clisession_runtime.py   # `CliSessionManager` (vive en `GrafenoApp.cli_sessions`): un turno a la vez por sesión, turnos como workers de la App, listeners (`changed`/`entry`/`deleted`), estado running solo en memoria; usa `CLIDriver.run(on_session=...)` para persistir pronto el id nativo
├── gitrepos.py             # Núcleo git multirrepositorio para la revisión asistida: descubrimiento recursivo de repos anidados a cualquier profundidad (`discover`/`discover_all`, presupuesto MAX_SCAN_DIRS), upstream/ahead/behind, status porcelain v2 con repo propietario inequívoco, diff por archivo y commit explícito `git commit --only` con rutas literales (stage ajeno preservado, lock por repo); `GitReviewError(code, message)`
├── gitsync.py              # Operaciones de rama por repo (push/pull de la rama actual, switch a rama local existente, abort de merge/rebase) y resumen de cambios pendientes por proyecto con caché (pending_summary/invalidate); push/pull sin TTY vía projectsetup._run_process
├── reviews.py              # Sesiones de revisión asistida (`~/.grafeno/reviews/<id>/`: `review.toml`, `commits.jsonl`): decisiones por archivo, cursor y navegación, agente vía `CliSessionManager`, mensaje de commit one-shot y `commit_files` (único punto que comitea); el estado git se recalcula en cada `snapshot`
├── remote.py               # Proyectos remotos por SSH: parseo del spec, montaje sshfs bajo ~/.grafeno/mounts/ y espejo de datos de la tarea con rsync (best-effort), sondeo del SO destino (detect_os)
├── remotesession.py        # Modo sesión remota (`grafeno [user@]host[:port]`): bootstrap (sondeo de $HOME remoto, mkdir ~/.grafeno, montaje sshfs), activate() exporta GRAFENO_HOME al montaje; spec_for_task/describe_target para el fallback de sesión
├── triggers.py             # Tareas trigger: modelo, niveles global/proyecto, fire() y spawn() best-effort
├── telegram/               # Bot de Telegram (stdlib, sin dependencias nuevas): voz/texto -> tareas, consultas y respuestas con voz
│   ├── api.py              #   Cliente Bot API con urllib (long polling, multipart a mano, troceo 4096 con pacing CHUNK_DELAY entre chunks y reintento ante 429; el reply_markup viaja siempre en el último chunk); transporte inyectable; el token nunca se loguea
│   ├── stt.py              #   Transcripción vía endpoint OpenAI-compatible (Groq whisper-large-v3-turbo por defecto), best-effort
│   ├── tts.py              #   Voz generada vía endpoint OpenAI-compatible (Groq orpheus, voz masculina `troy` por defecto), opt-in; el WAV del proveedor se convierte a OGG/OPUS con ffmpeg externo (best effort; sin ffmpeg se envía como sendAudio); el texto se trocea en segmentos de menos de 200 caracteres (límite de Groq Orpheus) y los WAV se unen (`split_speech`/`merge_wavs`); los problemas (proveedor, clave, ffmpeg ausente) se registran en telegram.log y se avisan una vez por chat y en la TUI (`_tts_notice`), y al arrancar el bot se avisa si falta ffmpeg
│   ├── intents.py          #   Interpretación del mensaje con un CLI de agente (prompt one-shot -> JSON): crear/listar tareas/listar proyectos (directorios del scope global)/tareas de un proyecto/estado/archivos/preguntar
│   └── service.py          #   Bucle de polling (worker de la App), whitelist de chats, propuestas con botones inline, creación origin="telegram", notificación de fin
├── server/                 # Servidor remoto REST + WS sobre stdlib (asyncio)
│   ├── service.py          #   Ciclo de vida: bind, eventos push por sondeo (signature) y registro de conexiones WS; logs a ~/.grafeno/api.log (cap 1 MiB) sin el token
│   ├── httpcore.py         #   HTTP/1.1 parseo/serialización (límites 32 KiB head/8 MiB body; `body_policy`: rutas de subida autorizadas antes de leer el cuerpo, JSON+base64 hasta el base64 de un archivo de 500 MiB + 1 MiB y cuerpo binario de `POST .../attachments` volcado a `~/.grafeno/uploads/` por bloques hasta 500 MiB; timeout por inactividad de 60 s; 413 si excede, 507 sin espacio, `Expect: 100-continue`; `Response.file` sirve archivos en streaming (con `file_offset`/`file_length` para respuestas parciales 206), keep-alive)
│   ├── auth.py             #   Bearer/query token; denegar todo cuando no hay tokens
│   ├── rest.py             #   Router con placeholders genéricos `{nombre}` (`{task_id}`, `{session_id}`) precompilados (regex fullmatch); dispatch a actions.py
│   ├── actions.py          #   Operaciones compartidas REST/WS (read y write); ApiError(status, message) -> Response
│   ├── clisessions.py      #   Operaciones REST/WS de las sesiones CLI directas (`session_summary`, crear/enviar/cancelar/renombrar/cerrar/reabrir/eliminar, transcript incremental)
│   ├── web.py              #   Panel web (`grafeno --web`): prepare() (copia en memoria de ApiConfig, loopback por defecto, token efímero si se expone sin tokens), URLs del panel y render de la página con las cadenas i18n inyectadas y los placeholders de tema (`data-theme`), título y versión del favicon
│   ├── projectdocs.py      #   Documentos de proyecto de solo lectura (README.md, AGENTS.md, GRAFENO.md): lista blanca, proyecto resuelto contra list_projects, symlinks fuera del proyecto bloqueados, tope 1 MiB, paginación por caracteres sobre el texto normalizado y esquema de encabezados (mdnorm.markdown_outline)
│   ├── projectfiles.py     #   Explorador de archivos de proyecto de solo lectura: listado paginado (ocultos opcionales, tope SCAN_MAX_ENTRIES), metadatos (mime seguro, preview, texto/binario, urls), páginas de texto por líneas (tope 2 MiB) y bytes en crudo con Range (206/416); rutas relativas validadas, `..`/absolutas rechazadas, `.git` bloqueado, symlinks fuera del proyecto bloqueados; reutiliza projectdocs.resolve_project
│   ├── projectcreate.py    #   POST /api/v1/projects y WS projects.create: mapea ProjectError.code a status (400/409/422/500/503/504); propaga `code` en el error y registra petición/fallo/éxito con duración en `api.log`; 409 en modo sesión remota
│   ├── branding.py         #   Marca del panel web: nombre de instancia, tema (`WEB_THEMES`) y favicon propio (`~/.grafeno/web/favicon.<ext>`, PNG/ICO/GIF/JPEG/WebP sin SVG, máx. 256 KiB, firma validada); `boot_info()` para la página y `favicon_response()` para `/favicon.ico` (204 sin favicon)
│   ├── clock.py            #   Reloj del servidor para GET /api/v1/time y el método WS time: payload server_time() (ISO 8601 con offset, UTC con Z, epoch, fecha/hora/día locales, nombre IANA best effort vía TZ, /etc/localtime o /etc/timezone, vacío si no cuadra con el offset)
│   ├── reviews.py          #   Operaciones REST/WS de la revisión asistida y mapa `code` -> status (`GitReviewError`); `ApiError` lleva `code` opcional
│   ├── projectgit.py       #   Operaciones REST/WS de git por proyecto: indicador de pendientes (projects/changes), ramas, push, pull, checkout y abort; mapa code -> status
│   ├── settings.py         #   Operaciones REST/WS de catálogo, reportes y ajustes: opciones del formulario, modelos+variantes (caché 10 min), autocompletado de directorios, issues de gh, reporte de uso y ajustes globales (secretos de solo escritura)
│   ├── static/             #   Panel web (paquete de datos en pyproject): index.html (shell + JSON de arranque no ejecutable), app.css, app.js (SPA vanilla sin dependencias ni CDN: lista, detalle, nueva tarea, reportes, ajustes)
│   └── ws.py               #   RFC 6455: handshake (accept key), frames enmascarados de cliente, comandos JSON-RPC {"id","method","params"} y eventos {"event":...,...} con suscripción por topic
├── drivers/                # Abstracción de CLIs de agentes
│   ├── base.py             #   CLIDriver: ciclo de subproceso asyncio, eventos JSONL (`run` acepta el callback `on_session` con el id nativo de sesión en cuanto aparece); expone variantes de esfuerzo por modelo (variants_command/parse_variants/list_variants_async)
│   ├── opencode.py, kimi.py, codex.py, claude.py, cursor.py, minimax.py#   Dialectos concretos (opencode detecta la versión mayor con `--version`: en 2.x sin `--dir`/`--variant`, el esfuerzo va como sufijo `#variante` del modelo, el directorio se pasa vía `PWD` con `run_env()` y las variantes salen de `opencode api model.list`; minimax = MiniMax Code, ejecutable `mcode`; el esfuerzo se mapea a la variante de thinking como sufijo `#variante` del modelo)
│   └── __init__.py         #   Registro: get_driver(), available_clis(), fetch_all_models(), fetch_all_variants()
├── pipeline/
│   ├── orchestrator.py     # Orquestador de fases (first opcional/plan/implementar/revisar/final, automode, ciclos); `_mark_failed` registra la fase fallida, `_review_fix_loop` es el bucle compartido, `run_automode_resume` reanuda desde la fase fallida reaprovechando artefactos (resetea el presupuesto de iteracion solo si estaba agotado) y `run_continue` hace lo mismo desde estados transitorios huérfanos (tarea interrumpida por TUI matada, apagón o crash del CLI) usando el mapa `INTERRUPTED_PHASE`
│   ├── hooks.py            # Hooks de completado por etapa (comando shell o webhook URL; global + por tarea, mejor esfuerzo)
│   ├── prompts.py          # Prompts por fase (first, plan, ...) en en/es (dicts por idioma, renderizados en i18n.prompt_language()), cabecera GRAFENO-EXECUTOR e instrucciones finales personalizables
│   ├── verdict.py          # Parseo del veredicto del revisor (VERDICT: APPROVED / CHANGES_REQUESTED)
│   └── gitops.py           # Rama opcional grafeno/<tarea>; diff base (base_commit) y generacion de changes.md del reporte final
└── tui/
    ├── runtime.py          # TaskRuntime: ejecución en segundo plano por tarea (workers Textual); notifica a la App cuando una ejecución termina en DONE (gancho de encadenamiento/repetición)
    ├── console_pty.py      #   Proceso shell sobre PTY (POSIX): start/read/write/interrupt/close, sin shell=True; lectura no bloqueante; eco del kernel desactivado (la pantalla ecoa localmente)
    ├── dirpicker.py        # Autocompletado de rutas (formulario de nueva tarea y directorios de trabajo); placeholder y suggest_empty opcionales; include_files sugiere también archivos
    ├── rolesform.py        # Formulario reutilizable CLI+modelo por rol; incluye filtro de texto sobre el selector de modelos; parametrizable (lista de roles, opción CLI heredada, CLI de respaldo y lista restringida de CLIs `clis`; lo usan también el intérprete de Telegram y el formulario de sesiones CLI)
    ├── profilesform.py     # Editor reutilizable de perfiles de procesamiento (tabla + alta/edición vía ProfileEditScreen)
    ├── attachform.py       # Editor reutilizable de adjuntos (rutas locales con autocompletado de archivos; copia a media/ al crear la tarea o al pedir más)
    ├── refform.py          # Editor reutilizable de referencias (tabla + añadir/borrar)
    ├── trigform.py         # Editor reutilizable de triggers globales (tabla + añadir/borrar)
    ├── workspacesform.py   # Editor reutilizable de directorios de trabajo (tabla con nº de proyectos o "no encontrada" + DirectoryPicker con autocompletado; Enter o Añadir agrega, Eliminar seleccionada o Supr quita; normaliza y deduplica)
    ├── widgets.py          # Widgets comunes (cabecera GrafenoHeader con reloj fecha/hora (refresco alineado al cambio de minuto, sin segundos), LocationBar con la ruta actual y la de la tarea + distintivo SSH, barra de fases, helpers Markdown, MediaTextArea que guarda imágenes pegadas e inserta tokens media/media-NN.png)
    ├── screens/clisessions.py#   Pantalla de sesiones CLI (`CliSessionsScreen`: lista + conversación en vivo; `NewCliSessionScreen`): se abre desde Proyectos (botón "CLI sessions" o `s`) o desde la lista de tareas (`s`, todas las sesiones)
    └── screens/            # tasks (lista con filtros combinables: ámbito proyecto/todas con `v`, estado todas/sin completadas/solo completadas en ciclo con `h`, y fecha de creación con input YYYY-MM-DD + botón Hoy/Todas las fechas (toggle: aplica el día actual o quita el filtro activo), tecla `d`), detail (detalle+acciones: `u` reanuda una tarea FAILED reusando artefactos, `c` continúa una tarea fallida o interrumpida (estado transitorio huérfano) desde la fase donde paró vía `run_continue`, `R` reinicia desde cero borrandolos), config (ConfigScreen(section="workspaces") abre desplazada a los directorios de trabajo), roles, profileedit (modal nombre + RolesForm, clon de roles.py), consoles (tabs de shells del proyecto), reports (reporte de uso: tokens/modelos/tiempo/proyectos por día, semana, mes o rango; atajo `i` desde la lista), projects (pantalla Proyectos: subcarpetas de los directorios de trabajo con nº de tareas y filtro de texto; tecla `p` o botón Proyectos desde la lista; Enter/clic abre el modal ProjectActionsScreen -> "Nueva tarea en este proyecto", `n` atajo directo, `w` abre la configuración en los directorios de trabajo; se cierra con dismiss(carpeta) y la lista abre NewTaskScreen(workdir) con la ruta predefinida)
tests/                      # pytest; conftest aísla GRAFENO_HOME e idioma por test
docs/screenshot.png         # captura de la lista de tareas usada en el README
install.sh, install.ps1     # instaladores de usuario (Linux/macOS y Windows), vía pipx; la ausencia de CLIs de agente es siempre un warning (nunca un error)
```

Los datos en runtime viven en `~/.grafeno/` (`tasks/<fecha>-<slug>/` con
`task.toml`, `first/`, `plan/`, `review/`, `final/`, `media/`, `attachments.toml`, `logs/live.jsonl`, `logs/*.jsonl`; `cli-sessions/<id>/` con `session.toml`, `transcript.jsonl` y `logs/turn-NNN.jsonl`; `reviews/<id>/` con `review.toml` y `commits.jsonl`;
además `config.toml`, `references.toml`, `triggers.toml`, `profiles.toml` (con backup rotativo `profiles.toml.bak` best-effort en cada guardado), `consoles/`, `web/`,
`mounts/`, `telegram-state.toml`, `api.log`); no
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
- **Prompts internos (en/es)**: todo texto que se envía a un CLI de agente
  (prompts de fase en `pipeline/prompts.py`, generación de AGENTS.md en
  `drivers/base.py`, parser y preguntas del bot en `telegram/intents.py` y
  `telegram/service.py`) es una plantilla por idioma `{"es": ..., "en": ...}`
  con las dos variantes juntas, formateada UNA sola vez con `str.format`
  (las llaves de los datos de la tarea quedan literales) y elegida con
  `i18n.prompt_template()`. El idioma es `Config.prompt_language` (selector
  en la sección Idioma de los ajustes; vacío = el de la GUI) y se aplica en
  caliente vía `i18n.set_prompt_language` al arrancar y al guardar. Al
  añadir o cambiar un prompt, actualiza ambos idiomas:
  `tests/test_prompts.py` exige los mismos placeholders en cada variante.
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
  `TokenUsage` acumulado cuando el CLI emite eventos de uso. Todo spawn
  (modelos, variantes, update, run) pasa por
  `resolve_command()`/`resolve_executable()` (ruta absoluta resuelta una vez
  con `shutil.which`): en Windows los CLIs npm son shims `.cmd` y el nombre
  pelado no se puede spawnear (CreateProcess solo implica `.exe`), lo que
  vaciaba las listas de modelos con un `OSError` silenciado. Los drivers cuyo
  CLI lee el prompt por stdin (opencode `run`, claude `-p`, codex `exec`:
  `stdin_prompt() -> True`) lo omiten de argv y la base lo escribe en stdin:
  en Windows cmd.exe corta los argumentos citados en el primer salto de
  línea, y los prompts multi-línea llegaban truncados al CLI (kimi no
  documenta stdin y sigue usando `-p` por argv). El nivel de
  trabajo del modelo viaja en `RunRequest.effort`;; los CLIs sin soporte
  exponen `variants_command() -> []` y `parse_variants -> {}` (defecto de
  la base) e ignoran el campo en `build_command`. Cada driver puede
  exponer además `update_command()` (defecto `[]` en la base = CLI sin
  comando nativo de auto-actualización, p.ej. `claude update`,
  `opencode upgrade`, `kimi update`) que `updater.update_all()` ejecuta
  en segundo plano al arrancar la TUI si `Config.auto_update` está
  activado. `RunResult.usage_wait` propaga al orquestador la pista de
  espera cuando se detecta uso agotado en el run (reintenta la fase con
  la espera indicada o sondea cada `PROBE_SECONDS` hasta `MAX_ATTEMPTS`
  intentos rapidos; agotados, entra en modo de espera pasiva reintentando
  cada `PASSIVE_WAIT_SECONDS` —15 minutos— de forma indefinida: la tarea
  nunca falla por usage agotado; durante la espera la TUI muestra el
  sufijo i18n `state.waiting` en la lista de tareas y en `PhaseBar`).
  Si el fallo no es de usage pero parece transitorio (ECONNRESET, socket
  cerrado, error 5xx/`Unexpected server error` del proveedor, `overloaded`,
  `Session interrupted: shutdown` del servicio de fondo de opencode 2.x),
  `CLIDriver.detect_transient` marca `RunResult.transient` y el orquestador
  reintenta la fase hasta `TRANSIENT_MAX_ATTEMPTS` veces (esperas
  crecientes `TRANSIENT_WAITS`, mismo sufijo `state.waiting`) retomando la
  sesión y anteponiendo al prompt original el aviso de reanudación
  (`prompts.resumed_prompt`, nunca apilado); si persiste, la fase falla como
  siempre (`failed_phase` registrada, reanudable con `c`/`u`).
  Si el CLI termina con un código distinto de 0 sin causa reconocible (ni
  usage ni transitorio, p. ej. el `OpenCode CLI exited with code 1` pelado
  al pasar de revisión a corrección o al revés), el orquestador lo trata como
  crash (`_is_crash`: exige `returncode` distinto de 0 y de `None`; un fallo
  al lanzar el ejecutable no se reintenta) y relanza la fase hasta
  `CRASH_MAX_FAILURES` (3) fallos consecutivos con esperas `CRASH_WAITS`
  (10 s, 30 s) y el aviso `prompts.resumed_prompt`; el último intento
  descarta la sesión guardada y arranca una nueva (mensaje
  `orch.crash.retry_fresh`). Al tercer fallo la fase falla como siempre
  (`orch.crash.exhausted`). La rama de errores de cada driver
  normaliza su texto con `format_error_message(payload, ...)` de
  `base.py` (nunca `str(payload)`: los campos dict del CLI se traducen a
  `name: message (ref)` y el fallback es `json.dumps`, nunca el repr de
  Python).
- **Auto-actualización de GRAFENO**: `selfupdate.py` consulta la última
  release de GitHub al arrancar la TUI (siempre, worker en segundo
  plano, fallos silenciosos). Con `Config.self_update` activado se
  auto-actualiza y notifica; desactivado, solo muestra `(v X.Y.Z
  available)` en naranja junto a la versión de la cabecera
  (`GrafenoHeader.format_title` + reactive `App.available_update`). El
  instalador se elige según cómo está instalado GRAFENO: pipx solo si el
  intérprete vive en un venv de pipx (`installed_via_pipx`, nuance Windows);
  si no, `sys.executable -m pip install --upgrade --force-reinstall`. Tras
  el comando verifica la versión instalada con
  `importlib.metadata.version("grafeno")` (si no coincide, `ok=False` con
  `supd.verify_failed`; sin metadata se confía en el código de salida). El
  comando `grafeno update` (interceptado en `main()` antes del argparse)
  actualiza manualmente y devuelve código de salida 0/1. Antes de instalar
  imprime la versión destino (`supd.updating`) y durante la instalación dibuja
  una barra de progreso ASCII con porcentaje en una sola línea, solo con los
  porcentajes que el instalador ya emite: no se inyectan opciones de progreso
  específicas de `pip` (`--progress-bar=on`, ni vía `--pip-args` con pipx)
  porque `pipx` puede delegar la instalación en `uv`, que las rechaza
  (`unexpected argument '--progress-bar' found`); no reintroducirlas.
  `check_latest_release` devuelve `ReleaseInfo(version, wheel_url, error)`:
  si falla la API (rate limit de 60/h sin autenticar) cae a la redirección
  web de `releases/latest`; se instala el wheel de la release o el tag git si
  no hay asset; el detalle de fallo del instalador se elige con
  `summarize_output` (líneas `ERROR:`/`did not run successfully`/`Fatal error`
  antes que las `note:`) y aparece el mensaje `supd.ahead` cuando la versión
  local va por delante de la última release.
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
- **Servidor API (REST + WebSocket)**: integración opcional (sección `[api]`
  del config + sección en la pantalla de ajustes). El servidor corre como
  worker de la App mientras la TUI está abierta: bind sobre `host:port`
  (por defecto `127.0.0.1:8735`) con HTTP/1.1 parseado a mano sobre asyncio
  streams (stdlib, cero dependencias nuevas; misma política que el bot de
  Telegram). Limites (`httpcore.body_policy`): 32 KiB de cabecera y 8 MiB de cuerpo; las
  rutas de subida se autorizan antes de leer el cuerpo, el JSON+base64 llega
  hasta el base64 de un archivo de 500 MiB + 1 MiB y el cuerpo binario de
  `POST .../attachments` se vuelca por bloques a `~/.grafeno/uploads/` hasta
  500 MiB (timeout por inactividad de 60 s; 413 si excede, 507 sin espacio,
  `Expect: 100-continue`); `GET .../media` se sirve en streaming y
  `tasks.media` por WS se limita a 16 MiB (`ws.WS_MEDIA_MAX_BYTES`);
  la ruta REST de media añade `nosniff` y nunca sirve HTML/SVG; sin chunked;
  keep-alive HTTP/1.1. Autenticación opcional por token: `Authorization: Bearer
  <token>` o `?token=` solo cuando hay tokens configurados; el conjunto vacío
  (defecto) NO exige API key (acceso abierto). La API key es credencial dedicada de
  este servidor, independiente de la del bot de Telegram (integración
  separada que sigue funcionando como canal alternativo). Tokens también por
  env `GRAFENO_API_TOKEN` (prioridad al env). El router se compila al importar
  con `re.fullmatch` y soporta placeholders genéricos `{nombre}` (`{task_id}`, `{session_id}`). Acciones REST
  bajo `/api/v1`: status, time (hora actual del servidor con zona; guía en `docs/api/server-time.md`; método WS `time`), sessions (sesiones CLI directas, ver su viñeta), tasks (`GET` acepta filtros `state` (lista separada por comas), `workdir`, `project`, orden `sort` (tree/created/updated/completed) + `order` y paginación `limit`/`offset` con `total`/`has_more` en la respuesta; cada item lleva `completed_at` (de `last_completed_at`, que se sella al llegar a DONE); guía en `docs/api/tasks-list.md`; GET/POST con todas las opciones del
  formulario de la TUI y `attachments?` opcional base64
  máx. 10: los adjuntos de audio por nombre de archivo (`AUDIO_SUFFIXES`:
  ogg/oga/opus/mp3/wav/m4a/flac) se transcriben además con el proveedor STT
  configurado y el texto se añade a la descripción, devolviendo en la
  respuesta un array `transcriptions` por adjunto con `{name, text}` o
  `{name, error}`; `/{id}/start|resume|restart|reset|continue|approve-plan|
  run|edit|roles|pause|extend|discard|mark-done`, `/{id}/logs`, `/{id}/artifacts?kind=
  first|plan|review|final&cycle=`, `/{id}/artifact?kind=&path=`,
  `/{id}/media?name=`, `/{id}/attachments` (GET lista con metadatos, POST JSON
  base64 o cuerpo binario con `?name=` y `?cycle=` opcional, sin arrancar ciclo; DELETE `?file=`,
  409 si corre; WS `tasks.attachments`/`tasks.attach`/`tasks.detach`), projects, options, models, fs/dirs, issues,
  `POST /projects` (crear proyecto; guía en `docs/api/project-create.md`; `GET /projects` devuelve además `sessions` por item y `create` con las opciones de creación), `GET /projects/changes` (indicador de cambios pendientes por proyecto, recursivo en repos anidados, caché 15 s), `GET /projects/branches` y `POST /projects/push|pull|checkout|abort` (guía en `docs/api/project-git.md`), projects/docs y projects/doc (documentos README.md/AGENTS.md/GRAFENO.md de un proyecto; guía en `docs/api/project-docs.md`; métodos WS `projects.docs`/`projects.doc`; 409 en proyectos remotos y modo sesión remota), `projects/files`, `projects/file`, `projects/file/text` y `projects/file/raw` (explorador de archivos de un proyecto para Grafoide; guía en `docs/api/project-files.md`; métodos WS `projects.files`/`projects.file`/`projects.file_text`/`projects.file_raw` (este con tope de 16 MiB); 409 en proyectos remotos y modo sesión remota), `GET /projects/repos` y `/api/v1/reviews...` (revisión asistida multirrepositorio; guía en `docs/api/reviews.md`; topic WS `reviews` con `review.changed`/`review.deleted`; los errores pueden llevar un campo `code` opcional), reports, settings (GET/POST) y `/audio/speech` (síntesis
  TTS bajo demanda con el proveedor configurado: WAV por defecto u OGG/OPUS
  cuando `format=ogg` y hay `ffmpeg` instalado en el host). WebSocket en `/api/v1/ws` con
  handshake RFC 6455 (cliente enmascarado obligatorio; servidor sin
  máscara), comandos JSON-RPC `{"id","method","params"}` que devuelven
  `{"id","result"|"error"}` y eventos de suscripción `{"event":"task.changed",
  "task":{...}}` publicados al topic `tasks`. La entrega de eventos se hace
  por sondeo del store (`tasks_signature`, cada 2 s): cada conexión
  suscrita recibe el `task.changed` cuando cambia `state`; el snapshot se
  actualiza aunque no haya conexiones (para no spamear a un cliente que
  se conecte luego con el backlog). Las operaciones de escritura
  (`create/start/resume/restart/pause/extend/discard/mark-done`) usan
  exactamente las mismas ramas y corutinas de la TUI
  (`TaskRuntime.start`, `run_automode*`, `reset_to_draft`,
  `start_new_cycle`, `cancel`, `state = DONE/DISCARDED`) y, cuando el
  servicio no tiene `app` (tests de solo lectura), devuelven `503`. Cambios
  de la sección `[api]` aplican al siguiente arranque de la TUI (el worker
  se crea en `on_mount`, no en caliente). El log de actividad va a
  `~/.grafeno/api.log` (cap 1 MiB, rotación in-place) y nunca contiene el
  token. Las specs se mantienen a mano en `docs/api/` (OpenAPI 3.1 para
  REST, AsyncAPI 3.0 para la WebSocket) y `tests/test_api_specs.py`
  garantiza que cada ruta REST y cada método WS del código estén
  documentados.
- **Panel web**: `grafeno --web` arranca el servidor API para esa
  ejecución (sin tocar `config.toml`: `server/web.prepare` devuelve una
  copia de `ApiConfig` que la App usa en memoria) y sirve en `/` un panel
  de una sola página (`server/static/`: `index.html` + `app.css` + `app.js`,
  sin dependencias ni CDN) con la misma navegación que la TUI (la lista
  ocupa toda la pantalla y al abrir una tarea su detalle la reemplaza a
  pantalla completa; Volver/Escape regresan conservando filtros y scroll;
  en las pestañas de artefactos la lista de archivos y el archivo abierto
  se desplazan por separado; la columna de tareas ocupa por defecto el 60%
  del viewport (`#task-table th.name` con `width` y `min-width` a `60vw`:
  solo con `width` el layout automático de la tabla la encoge; si el resto
  no cabe, la tabla hace scroll horizontal) y se redimensiona arrastrando el
  borde de su cabecera (`.col-resizer`, también con las flechas); el ancho
  elegido se guarda en px en `localStorage` (`grafeno.taskColWidth`) y el
  doble clic sobre el borde vuelve al 60%)
  y las mismas opciones (lista con
  filtros y cadenas, detalle con todas las acciones del pipeline, pestañas
  de artefactos/medios (visor a pantalla completa con navegación anterior/siguiente por flechas de la GUI y del teclado)/log/tokens, editor de agentes, formulario completo
  de nueva tarea (con la carpeta predefinida vía `#/new/<carpeta codificada>`),
  pestaña Proyectos (`#/projects`) (botón "Nuevo proyecto": modal con modo solo carpeta/git init/clonar que llama a `POST /api/v1/projects` usando las opciones `create` de `GET /api/v1/projects`; tras crear recarga y abre las acciones del proyecto), reportes y ajustes globales
  (`#/settings/workspaces` abre desplazado a los directorios de trabajo);
  las consolas siguen siendo
  solo de la TUI; los adjuntos de hasta 25 MiB (cupo de 48 MiB) van en
  línea en el JSON —los audios conservan la transcripción— y los mayores se
  suben en binario con progreso (`uploadRaw`, XHR) tras crear la tarea o antes
  de `/extend` con `cycle` = siguiente; la pestaña Media no descarga archivos
  de más de 64 MiB ni no previsualizables hasta pedirlo) que usa exclusivamente la API REST/WS. Las reglas de qué
  acción aplica en cada estado viven en el servidor
  (`actions.available_actions`, mismas que el detalle de la TUI) y viajan
  en el detalle de la tarea. Por defecto
  escucha solo en `127.0.0.1`; `--web-host 0.0.0.0` lo abre a la red y
  `--web-port` cambia el puerto (ambos implican `--web`; puerto por defecto
  el de `[api]`). Si se expone fuera de loopback sin tokens configurados se
  genera un token efímero (`secrets.token_urlsafe`) que solo vive en
  memoria, viaja en la URL anunciada por notificación de la TUI y nunca se
  escribe a disco ni al log. `--no-auth` (implica `--web`) desactiva la
  autenticación de esa ejecución: `ApiConfig.auth_disabled` (campo solo de
  runtime, fuera de `to_dict`) hace que `resolve_tokens()` devuelva vacío
  aunque haya tokens en el fichero o en `GRAFENO_API_TOKEN`, no se genera
  token efímero y la TUI avisa (en rojo si además se expone fuera de
  loopback). La página (`/`, `/index.html`), sus assets
  (`/assets/app.css`, `/assets/app.js`, lista blanca `web.ASSETS`) y
  `/favicon.ico` se sirven sin autenticación (no contienen datos); todo lo
  demás pasa por `auth.check`. La página lleva CSP estricta (`script-src
  'self'`, sin scripts inline) y el DOM se construye siempre con
  `textContent` (Markdown incluido: renderizador propio que crea nodos, sin
  `innerHTML`). Los textos salen del catálogo de `i18n.py`: se inyectan como
  JSON todas las claves con los prefijos de `web.UI_PREFIXES` (reutiliza
  los textos de la TUI: `nt.*`, `det.*`, `cfg.*`, `reports.*`...) más las
  propias `web.*`; al añadir texto al panel usa `tr("clave")` con una clave
  existente o nueva en ambos idiomas (`tests/test_api_web.py` exige que
  toda clave usada en `app.js` exista en en/es). Acciones del servidor para
  el panel (también en REST/WS): `run` (fase concreta o automode
  respetando `confirm_plan`), `continue`, `approve-plan`, `reset`
  (cancela y espera al worker, igual que `R`), `edit` (nombre,
  descripción y re-encadenado validado con `scheduler.rechain_error`),
  `roles`, `artifact` y `media`; `discard`/`restart` esperan a que el
  worker cancelado termine para que su PAUSED no pise el estado final.
  La tarjeta 'Panel web' de los ajustes (`#/settings/web`) define el nombre
  de la instancia (distintivo en la cabecera y título de la pestaña), el
  tema (`auto` = claro/oscuro del sistema, o `night`/`day`/`blue`/`pink`/
  `white`/`dark`/`gray`: juegos de las mismas variables CSS en `app.css` vía
  `<html data-theme>`, renderizado en servidor para evitar parpadeo) y un
  favicon propio subido en base64 por `POST /api/v1/settings`
  (`web.favicon`/`web.favicon_clear`); se guarda en `[web]` del config (por
  instancia) y `GET /api/v1/status` expone `instance_name`. La pantalla de
  ajustes de la TUI conserva `theme` y `[web]` al guardar.
- **Telegram**: integración opcional de un bot (sección `[telegram]` del
  config + sección en la pantalla de ajustes). El bot corre como worker de
  la App mientras la TUI está abierta (long polling con stdlib urllib:
  cero dependencias nuevas). Acepta texto y notas de voz (transcritas vía
  STT OpenAI-compatible, Groq por defecto), interpreta la intención con el
  CLI del rol planner (o `parser_cli`/`parser_model`/`parser_effort` propios, resueltos con `TelegramConfig.parser_role`: CLI vacío = CLI del planner y su modelo salvo modelo propio; CLI explícito sin modelo = modelo por defecto de ese CLI; el esfuerzo nunca se hereda) mediante un
  prompt one-shot que exige JSON estricto, propone la(s) tarea(s)
  conservando TODO el detalle sustantivo del mensaje (prohibido resumir de
  forma agresiva o condensar requisitos, especialmente con audios largos
  transcritos) y las crea tras la confirmación con botones inline (automode,
  `scheduled_at`= ahora, `origin="telegram"`: el tick del planificador las
  arranca desatendidas, igual que los triggers).   Antes de crearlas, el bot
  pregunta siempre el encadenamiento con dos botones inline ("ninguna" =
  paralela, "a la última del proyecto" = `parent_id` a la última hoja no
  terminal del bosque de encadenamiento del proyecto según
  `_chain_ends` de `telegram/service.py`: una tarea del propio proyecto,
  sin hijos en el almacén y sin estado terminal); si esa cabeza no es
  única (2+ cadenas paralelas en el mismo proyecto), el bot pregunta
  antes con botones inline (callback `tg:m:<id>:<idx>`, índice `-1` =
  paralela; la cola FIFO avanza con cada respuesta). Cuando un mismo
  mensaje crea varias tareas y el usuario elige encadenar, el lote se
  encadena de forma secuencial: la primera tras la última de la cadena
  de su proyecto (elegida en la desambiguación o por defecto si es única)
  y cada una de las siguientes tras la anterior del lote del mismo
  proyecto. `chain_parent` es un `dict[workdir, parent_id]` que `_create_tasks`
  consulta para enrutar cada spec del lote al `parent_id` correspondiente
  (`""` = paralela por desambiguación del usuario o por falta de
  candidata); sin candidata o con posición inválida según
  `scheduler.rechain_error`, la tarea se crea paralela y se avisa con
  `tg.chain.no_parent`. La pregunta es obligatoria también con
  `confirm_create` desactivado. El contexto del parser incluye las
  tareas existentes con su directorio (columna extra en
  `intents.tasks_summary`), de modo que las tareas nuevas se enrutan al
  proyecto correspondiente (`workdir` exacto del listado, vía
  `intents.resolve_workdir`; vacío = directorio por defecto como fallback) y
  las referencias a tareas se resuelven de forma inequívoca. Fotos/vídeos
  adjuntos se guardan en `media/` de la primera tarea creada; los
  vídeos/documentos de más de 20 MB (límite de `getFile` de la Bot API) se
  rechazan antes de descargarlos con `tg.attachment.too_large`. También
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
  Groq por defecto, voz masculina `troy`) son opt-in (`tts_enabled`); el texto
  se trocea en segmentos de menos de 200 caracteres y los problemas de voz se
  avisan una vez por chat y en la TUI. Groq exige que un admin acepte los
  términos del modelo Orpheus en su consola (si no, HTTP 400 que el bot
  reporta en el chat).
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
  El parser CLI tiene timeout configurable (`parser_timeout`, base 300 s, 30-3600) ampliado según la longitud del mensaje (`intents.parser_timeout_for`, +1 s cada 40 caracteres, tope 3600 s); el timeout no se reintenta y se avisa con `tg.parser_timeout`; la acción `ask` usa el mismo timeout. El intérprete se configura en los ajustes de la TUI (fila `RolesForm` parametrizada `#tg-parser-form`) y del panel web (`rolesEditor` con `opts.roles`); `GrafenoApp.apply_telegram_parser` lo aplica en caliente al guardar (TUI y `POST /api/v1/settings`), mientras que el resto de `[telegram]` sigue aplicando al reiniciar. Sus fallos se contestan en el chat
  en vez de quedar en silencio. Si un envío con teclado inline falla, el
  servicio lo reintenta con un mensaje corto de respaldo (`tg.buttons_fallback`):
  una propuesta nunca queda sin botones de confirmación. El parser devuelve además el idioma del
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
- **Perfiles de procesamiento**: conjuntos con nombre de asignación
  CLI+modelo+esfuerzo por rol (`profiles.py`, `~/.grafeno/profiles.toml`,
  claves aplanadas `planner_cli`... porque `_toml` solo serializa escalares
  en arrays de tablas). Se eligen al crear la tarea (selector en el
  formulario, pregunta inline en el bot de Telegram tras la confirmación —
  solo si hay perfiles definidos — y campo `profile` opcional en la API) y
  la tarea guarda el nombre (`Task.profile`) y una foto de los roles: el
  pipeline no cambia, el perfil solo elige la fuente del copiado en
  `Task.create`. Borrar un perfil no afecta a tareas ya creadas.
- **Workspaces (directorios de trabajo)**: carpetas raíz configurables
  (global `Config.workspaces` en `~/.grafeno/config.toml` + `workspaces` en
  `.grafeno.toml` de proyecto; `workspaces.effective_entries`/`resolve` las
  combinan). No restringen nada: solo definen el catálogo de proyectos (cada
  subcarpeta de primer nivel no oculta es un proyecto: `workspaces.projects`
  /`discover`). Se editan como lista: en la TUI con `WorkspacesForm`
  (sección de Configuración) y en el panel web con `listEditor` (columna
  `type: "dir"` con el autocompletado compartido `dirSuggestions` sobre
  `/api/v1/fs/dirs`); la lista se guarda normalizada
  (`workspaces.normalize_entries`: sin vacíos, sin separador final, sin
  duplicados), también vía `POST /api/v1/settings`. La pantalla Proyectos
  de la TUI (`tui/screens/projects.py`, tecla `p`) y la pestaña Proyectos
  del panel web (`#/projects`) listan esas carpetas con su nº de tareas y
  desde un proyecto se crea una tarea con el directorio predefinido
  (`NewTaskScreen(workdir)` / `#/new/<carpeta codificada>`).
  `GET /api/v1/projects` fusiona los workdirs por ruta normalizada, devuelve por item `workdir`, `count`, `sessions`, `name` y
  `workspace` (raíz donde se encontró; vacío para workdirs que solo vienen
  de tareas) más `workspaces` (raíces con `entry`/`path`/`exists`) y
  `session`; en modo sesión remota no se descubre nada (las raíces viven en
  el host remoto). El listado de proyectos del bot de Telegram
  (`list_projects`) combina las carpetas descubiertas con los proyectos con
  tareas (count 0 y marca i18n `tg.projects.item_empty`), y la creación de
  tareas las resuelve como `workdir` vía
  `intents.resolve_workdir`/`resolve_project_dir` con `extra_dirs`. Los proyectos
  también se crean vía API (`projectsetup.create_project`). Sin
  duplicados (dedup por ruta resuelta; los specs remotos `user@host:...`
  nunca se fusionan con rutas locales).
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
  (padre e hijas del padre fuera de DONE/DISCARDED) y que no haya ciclos. La UI (lista y detalle de TUI y panel web, y `schedule` en la API) muestra el arranque pendiente solo para tareas DRAFT que no corren: fecha y hora completas con offset UTC del host, hoy/mañana/ayer por fecha de calendario, cuenta atrás, vencida o en espera de la tarea padre; en cualquier otro estado o tras `reset_to_draft` no se muestra fecha (el `scheduled_at` crudo se conserva en la API por compatibilidad).
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
  corrección ni pasos finales, igual que las referencias; revisión y
  corrección reciben solo un puntero). `list_media` acepta
  png/jpg/jpeg/gif/webp (las fotos de Telegram llegan como JPEG) y
  `save_attachment` guarda adjuntos arbitrarios (imagen o video) con el
  patrón `media-NN<ext>` (tope por archivo `MAX_ATTACHMENT_BYTES` = 500 MiB;
  `save_attachment_file` copia/mueve desde disco sin cargarlos en memoria); el conjunto de sufijos reconocidos se publica
  como `IMAGE_SUFFIXES` (png/jpg/jpeg/gif/webp) y `AUDIO_SUFFIXES`
  (ogg/oga/opus/mp3/wav/m4a/flac); lo que no está en `SAFE_SUFFIXES` cae a `.bin`. El helper
  `is_audio_name(filename)` decide si un adjunto entra en la rama de
  transcripción de audio. `media.attach_files(task, attachments, header)`
  es el helper compartido que guarda los adjuntos y los referencia en la
  descripción (token `media/...` para imágenes, ruta absoluta para el resto):
  lo usan el bot de Telegram y el servidor API, que acepta `attachments`
  (base64, máx. 10) en la creación de tareas que se guardan igual.
  Los adjuntos de cualquier tipo (Word, Excel, PDF, vídeo...) se guardan en
  `media/` como `media-NN<ext>` con su nombre original en `attachments.toml`;
  se adjuntan desde el formulario de nueva tarea y "pedir más" de la TUI
  (`AttachmentsForm`), el panel web (creación, pedir más y subida en la
  pestaña Media), la API y Telegram (fotos, vídeos y documentos). Los prompts
  de plan, reevaluación e implementación reciben la sección "Archivos adjuntos
  de referencia" (nombre, tipo, tamaño, ruta absoluta y pista de lectura por
  tipo); revisión y corrección solo un puntero de una línea al directorio y
  los pasos finales nada.
- **changes.md y summary.md**: al terminar la fase final, el orquestador escribe
  `final/<ciclo>/changes.md` con todos los cambios aportados por la tarea
  (comiteados y sin comitear): commits, `git status`, diff completo contra
  `Task.base_commit` (HEAD registrado al arrancar la implementacion) y el
  contenido de los archivos nuevos sin seguimiento. Es best effort: sin
  repo git no se genera y nunca rompe el pipeline; gitops es solo lectura.
  Además, el agente de pasos finales escribe `final/<ciclo>/summary.md`:
  un resumen muy conciso de lo que se pidió, lo que se hizo y, en tareas
  sobre bugs o revisiones, qué lo estaba causando y cuál fue la solución;
  si el agente no lo genera, el orquestador escribe un fallback mínimo
  (best effort, con cabeceras en español como changes.md).
- **Revisión asistida (multirrepositorio)**: revisa los cambios Git pendientes
  (sin commit) de un proyecto, con repos anidados, y comitea archivos de forma
  explícita (`reviews.py`, `gitrepos.py`, `server/reviews.py`). Nada se comitea
  automáticamente: el pipeline de tareas no comitea y solo
  `POST /reviews/{id}/commit` (`reviews.commit_files`) crea commits. Cada archivo
  se identifica por `repo` (id del repo: ruta posix relativa a la raíz, `.` =
  raíz) + `path` (relativo a ESE repo); un archivo de `modules/modulo1` se
  comitea en su propio repo, nunca en el padre. El commit usa
  `git commit --only` con rutas literales (`GIT_LITERAL_PATHSPECS`), preserva el
  stage de otras rutas, respeta hooks y autor del sistema y prevalida todos los
  repos antes del primer commit. Se rechazan conflictos, archivos parcialmente
  en stage (salvo `include_unstaged`), huella (`fingerprint`) obsoleta y repos
  con merge/rebase en curso. Discutir y pedir cambios son turnos de una sesión
  CLI directa enlazada a la revisión (`CliSessionManager`, visible en
  `/sessions`); el mensaje de commit asistido es un one-shot que no comitea.
  Deshabilitado en modo sesión remota y para proyectos SSH.
- **Sesiones CLI directas**: conversaciones con Claude Code u OpenCode abiertas
  en la carpeta de un proyecto, SIN crear una tarea del pipeline (`clisessions.py`
  + `clisession_runtime.py`). No usan PTY: cada turno ejecuta el CLI en modo no
  interactivo (`CLIDriver.run` con `RunRequest.session_id` = id nativo del CLI,
  `--resume`/`--session`), de modo que el contexto se conserva entre turnos;
  `on_session` persiste pronto el id nativo (sobrevive a una cancelación). Un
  turno a la vez por sesión (varias sesiones, también del mismo proyecto, en
  paralelo); el estado running vive solo en memoria (en disco solo
  `active`/`closed`). Se gestionan en la TUI (Proyectos -> "CLI sessions" o `s`;
  `s` en la lista de tareas abre todas), en el panel web
  (`#/sessions[/<carpeta>[/<id>]]`, polling del transcript cada 1.5 s); el composer tiene un checkbox "Enviar con Enter" (Shift+Enter = salto de línea; preferencia en `localStorage` `grafeno.csEnterSends`; desactivado = botón o Ctrl+Enter) y por API:
  rutas `/api/v1/sessions...`, métodos WS `sessions.*` y topic `sessions` con los
  eventos `session.changed`/`session.deleted` (las entradas del transcript se
  consultan con `after`). `SESSION_CLIS` es el único punto para añadir CLIs.
  Quedan deshabilitadas en modo sesión remota y para proyectos SSH. El uso se
  registra en `usage.toml` con fase `session` y `task_id` vacío (suma tokens y
  tiempo al proyecto sin contar como tarea). Las herramientas se ejecutan
  autoaprobadas, igual que en el pipeline.
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
  (`run_test`). `conftest.py` deja offline la comprobación de release
  (`selfupdate.default_opener` y `_latest_from_redirect`): cada `GrafenoApp`
  montada la lanza y agotaría el rate limit de la API de GitHub (60/h).
- **Commits**: Conventional Commits en inglés por defecto (los prompts de
  los agentes lo imponen salvo indicación contraria en AGENTS.md o la tarea)
  (`feat:`, `fix(drivers):`, `test:`, `docs:`), mensaje corto en minúsculas
  que describe el cambio funcional, a veces con detalle entre paréntesis
  (p.ej. `test: ciclos, paralelismo, confirmaciones, config y prompts (56 tests)`).
- **Sin emojis** en código, docs ni UI.

## Versionado y releases

- La versión se declara en UN ÚNICO sitio (fuente única de verdad):
  `src/grafeno/__init__.py` (`__version__`). Todo cambio de versión
  modifica SOLO ese archivo. `pyproject.toml` NO lleva versión estática:
  declara `dynamic = ["version"]` en `[project]` y la deriva de
  `__init__.py` en build con `[tool.setuptools.dynamic]`
  (`version = {attr = "grafeno.__version__"}`), de modo que el paquete
  siempre se construye con la versión correcta. Nunca añadas una línea
  `version = "..."` a `pyproject.toml`.
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
  `src/grafeno/__init__.py` o `pyproject.toml` (también admite
  `workflow_dispatch` para relanzarlo manualmente, p.ej. para
  reintentar/backfills) y detecta si la versión actual de `__init__.py`
  ya tiene su etiqueta `vX.Y.Z` en `origin` mediante
  `git ls-remote --exit-code --tags origin "refs/tags/v${NEW_VERSION}"`
  (exit 0 = ya publicada, exit 2 = hay que publicar). Esta detección por
  etiqueta remota es idempotente y robusta frente a pushes multi-commit
  y a re-ejecuciones manuales: el orden interno de los commits dentro de
  un push ya no afecta al release. El paso de build, la verificación de
  que `dist/grafeno-X.Y.Z.tar.gz` y `dist/grafeno-X.Y.Z-py3-none-any.whl`
  llevan exactamente esa versión (derivada de `__init__.py` vía
  `tool.setuptools.dynamic`) y la creación del GitHub Release con tag
  `vX.Y.Z` solo se ejecutan cuando la etiqueta no existe. Un bloque
  `concurrency: group: release, cancel-in-progress: false` encola pushes
  solapados sin perder el segundo run (la detección por etiqueta lo
  deja en `changed=false` si el primero ya publicó). No hace falta
  crear tags a mano.
