# GRAFENO

Orquestador TUI multi-CLI para tareas de programación: **plan → implementación → revisión ⇄ corrección**, usando CLIs de agentes ya instalados en tu sistema.

- **CLIs soportados hoy**: [OpenCode](https://opencode.ai) (`opencode`) y [Kimi Code](https://moonshotai.github.io/kimi-code/) (`kimi`).
- **Arquitectura preparada para**: Codex CLI (`codex`) y Claude Code (`claude`) — añadir uno es crear un archivo en `src/grafeno/drivers/` y registrarlo.
- **Multiplataforma**: Linux, macOS y Windows (Python 3.11+).

## Cómo funciona

Cada tarea sigue un pipeline con tres roles configurables (CLI + modelo para cada uno):

1. **Planificador** — explora el proyecto y escribe uno o varios planes en Markdown (`~/.grafeno/tasks/<tarea>/plan/`).
   Cada plan incluye una cabecera `GRAFENO-EXECUTOR` que declara **qué modelo y qué CLI lo implementará**, y el prompt exige optimizar el contenido para ese ejecutor (pasos explícitos, rutas exactas, comandos concretos). Así, aunque el planificador viva en OpenCode y el ejecutor en Kimi, el plan llega íntegro por archivos.
2. **Implementador** — lee los planes y los ejecuta en el directorio del proyecto (opcionalmente en una rama `grafeno/<tarea>`).
3. **Revisor** — verifica los criterios de aceptación, escribe la revisión en `review/NN-review.md` y emite un veredicto estructurado (`VERDICT: APPROVED` / `VERDICT: CHANGES_REQUESTED`). Si pide cambios, el implementador corrige y se vuelve a revisar.

**Automode**: encadena todo el ciclo sin intervención hasta que la tarea queda aprobada **y** los tests (si se definieron) pasan, o hasta agotar las iteraciones máximas.

## Instalación

```bash
pipx install .          # o: pip install .
grafeno
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
| `c` | Lista | Configuración global |
| `Enter` | Lista | Abrir tarea |
| `p` / `i` / `r` / `f` | Detalle | Plan / Implementar / Revisar / Corregir |
| `t` | Detalle | Ejecutar tests |
| `a` | Detalle | Automode |
| `x` | Detalle | Cancelar ejecución |
| `Esc` | Detalle/Config | Volver |

## Datos

```
~/.grafeno/
├── config.toml              # roles (cli+modelo), automode, tests, git
└── tasks/<fecha>-<slug>/
    ├── task.toml            # estado, iteraciones, sesiones, workdir, rama
    ├── plan/*.md            # planes con cabecera GRAFENO-EXECUTOR
    ├── review/*.md          # revisiones numeradas por iteración
    └── logs/*.jsonl         # salida cruda de cada invocación de CLI
```

El directorio base puede cambiarse con la variable de entorno `GRAFENO_HOME`.

## Tests

```bash
.venv/bin/python -m pytest
```

Incluye tests unitarios (config, prompts, veredicto, drivers, orquestador con
drivers falsos) y smoke tests de la TUI en modo headless.
