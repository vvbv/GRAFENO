# GRAFENO

Orquestador TUI multi-CLI para tareas de programación: **plan → implementación → revisión ⇄ corrección → pasos finales**, usando CLIs de agentes ya instalados en tu sistema.

- **CLIs soportados hoy**: [OpenCode](https://opencode.ai) (`opencode`) y [Kimi Code](https://moonshotai.github.io/kimi-code/) (`kimi`).
- **Arquitectura preparada para**: Codex CLI (`codex`) y Claude Code (`claude`) — añadir uno es crear un archivo en `src/grafeno/drivers/` y registrarlo.
- **Multiplataforma**: Linux, macOS y Windows (Python 3.11+).

## Cómo funciona

Cada tarea sigue un pipeline con cuatro roles configurables (CLI + modelo para cada uno):

1. **Planificador** — explora el proyecto y escribe uno o varios planes en Markdown (`~/.grafeno/tasks/<tarea>/plan/`).
   Cada plan incluye una cabecera `GRAFENO-EXECUTOR` que declara **qué modelo y qué CLI lo implementará**, y el prompt exige optimizar el contenido para ese ejecutor (pasos explícitos, rutas exactas, comandos concretos). Así, aunque el planificador viva en OpenCode y el ejecutor en Kimi, el plan llega íntegro por archivos.
   Antes de planificar, si el proyecto no tiene un `AGENTS.md` en su raíz, GRAFENO lo genera con el propio planificador invocando el comando nativo del CLI correspondiente (p.ej. `/init` en OpenCode) o, si el CLI no expone uno (como kimi), mediante un prompt genérico equivalente; es una operación de mejor esfuerzo: si falla, la tarea continúa sin él y la salida cruda queda en `logs/agents-md.jsonl`.
2. **Implementador** — lee los planes y los ejecuta en el directorio del proyecto (opcionalmente en una rama `grafeno/<tarea>`; se decide por tarea en el formulario de creación, con el valor global de la configuración como defecto).
3. **Revisor** — verifica los criterios de aceptación, escribe la revisión en `review/NN-review.md` y emite un veredicto estructurado (`VERDICT: APPROVED` / `VERDICT: CHANGES_REQUESTED`). Si pide cambios, el implementador corrige y se vuelve a revisar.
4. **Pasos finales** — tras la aprobación, un último agente cierra la tarea: actualiza la
   documentación afectada, hace limpieza final y escribe un informe en `final/01-final.md`.
   También tiene CLI y modelo configurables (rol `final`). Puedes añadirle un bloque de
   instrucciones extra en `config.toml` (`final_prompt`) o sobrescribirlo por tarea al
   crearla; si está vacío el cierre se ejecuta como siempre.

**Automode**: encadena todo el ciclo sin intervención hasta que la tarea queda aprobada **y** los tests (si se definieron) pasan, y termina con los pasos finales, o hasta agotar las iteraciones máximas. Con la opción `confirm_plan` (global o por tarea), el automode se pausa tras el plan para que confirmes antes de implementar.

**Ciclos («Pedir más»)**: una vez completada la tarea (o en cualquier pausa), la tecla `m` permite pedir ampliaciones sobre el mismo proyecto. Cada ampliación arranca un ciclo nuevo con la misma lógica (plan → aprobación opcional → implementación → revisión), conservando el historial en `plan/ciclo-NN/` y `review/ciclo-NN/`.

**Seguridad de ejecución**: ninguna fase arranca con una sola tecla — cada acción abre un modal que explica qué va a ocurrir (agente, CLI, modelo, directorio) y pide confirmación. Mientras una fase corre, una barra de actividad muestra spinner, tiempos por fase, nº de eventos y watchdog de salida del CLI.

**Conteo de tokens**: cada ejecución acumula los tokens consumidos en `task.toml`, desglosados por modelo. La lista de tareas muestra una columna «Tokens (in/out)» con el total por tarea, una línea inferior agrega el resumen global por modelo, y el detalle incluye los totales en la barra de actividad.

**Idioma de la interfaz**: la GUI puede mostrarse en inglés (defecto) o español; se elige en la pantalla de configuración (`c`) y se persiste en `config.toml`. Al cambiarlo, las pantallas nuevas lo aplican de inmediato y el pie de atajos se actualiza al reiniciar la app.

## Instalación

```bash
pipx install .          # o: pip install .
grafeno
```

Instalación guiada (verifica Python 3.11+, instala pipx si falta y deja `grafeno` en el PATH):

```bash
./scripts/install.sh      # Linux y macOS
scripts\install.ps1       # Windows (PowerShell)
```

Desarrollo:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/grafeno
```

## Uso

| Tecla | Pantalla | Acción |
|---|---|---|
| `n` | Lista | Nueva tarea |
| (formulario) | Nueva tarea | El campo «Directorio del proyecto» autocompleta rutas con un desplegable (flechas/Enter o ratón) |
| `c` | Lista | Configuración global |
| `Enter` | Lista | Abrir tarea |
| `p` / `i` / `r` / `f` | Detalle | Planificar / Implementar / Revisar / Corregir (con confirmación) |
| `s` | Detalle | Pasos finales (con confirmación) |
| `t` | Detalle | Ejecutar tests |
| `a` | Detalle | Automode |
| `m` | Detalle | Pedir más (nuevo ciclo de ampliación) |
| `e` | Detalle | Cambiar CLI y modelo de cada agente de la tarea |
| `x` | Detalle | Cancelar ejecución |
| `Esc` | Detalle/Config | Volver |
| `Ctrl+Q` | Global | Salir |

## Datos

```
~/.grafeno/
├── config.toml              # idioma (en/es), roles (cli+modelo), automode, tests, git, prompt de pasos finales
└── tasks/<fecha>-<slug>/
    ├── task.toml            # estado, iteraciones, sesiones, workdir, rama
    ├── plan/*.md            # planes con cabecera GRAFENO-EXECUTOR
    ├── review/*.md          # revisiones numeradas por iteración
    ├── final/*.md           # informes de pasos finales por ciclo
    └── logs/*.jsonl         # salida cruda de cada invocación de CLI
```

El directorio base puede cambiarse con la variable de entorno `GRAFENO_HOME`.

## Tests

```bash
.venv/bin/python -m pytest
```

Incluye tests unitarios (config, prompts, veredicto, drivers, orquestador con
drivers falsos) y smoke tests de la TUI en modo headless.
