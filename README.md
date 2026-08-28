# GRAFENO

Multi-CLI TUI orchestrator for programming tasks: **plan -> implementation -> review <=> fix -> final steps**, using agent CLIs already installed on your system.

- **CLIs supported today**: [OpenCode](https://opencode.ai) (`opencode`), [Kimi Code](https://moonshotai.github.io/kimi-code/) (`kimi`), [Codex CLI](https://github.com/openai/codex) (`codex`) and [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude`) — adding another is just creating a file under `src/grafeno/drivers/` and registering it.
- **Cross-platform**: Linux, macOS and Windows (Python 3.11+).

## How it works

Every task follows a pipeline with four configurable roles (CLI + model for each, and optionally a per-model effort level when the CLI exposes one — e.g. `opencode run ... --variant <nivel>`). The model selector in the configuration screen and in the per-task agents modal supports live text filtering by substring, which helps when a CLI exposes a long catalogue.

1. **Planner** — explores the project and writes one or several Markdown plans (`~/.grafeno/tasks/<task>/plan/`).
   Each plan includes a `GRAFENO-EXECUTOR` header declaring **which model and which CLI will implement it**, and the prompt requires optimizing the content for that executor (explicit steps, exact paths, concrete commands). That way, even if the planner lives in OpenCode and the executor in Kimi, the plan arrives intact via files.
   Before planning, if the project does not have an `AGENTS.md` at its root, GRAFENO generates one with the planner itself by invoking the native command of the corresponding CLI (e.g. `/init` in OpenCode) or, when the CLI does not expose one (like kimi), through an equivalent generic prompt; this is a best-effort operation: if it fails, the task continues without it and the raw output is left under `logs/agents-md.jsonl`.
2. **Implementer** — reads the plans and executes them in the project directory (optionally on a `grafeno/<task>` branch; it is decided per task in the creation form, with the global configuration value as the default).
3. **Reviewer** — verifies the acceptance criteria, writes the review under `review/NN-review.md` and issues a structured verdict (`VERDICT: APPROVED` / `VERDICT: CHANGES_REQUESTED`). If changes are requested, the implementer fixes them and the review runs again.
4. **Final steps** — once approved, a last agent closes the task: updates the affected
   documentation, performs a final cleanup and writes a report under `final/01-final.md`.
   It also has a configurable CLI and model (role `final`). You can add an extra block
   of instructions in `config.toml` (`final_prompt`) or override it per task when
   creating it; if empty, the closeout runs as usual.

**Markdown normalization**: every `.md` written by the pipeline (plan, review and final) is normalized on disk after its phase ends, and the same compaction is applied in memory when you open one in the detail view, so legacy artifacts render compactly too. The normalization collapses runs of blank lines into a single one and removes the blank lines between consecutive items of the same list (bullet, ordered or checkbox) — fenced code blocks are preserved verbatim. The on-disk rewrite only touches files the pipeline just produced; older artifacts keep their original bytes.

**Automode**: chains the whole cycle without intervention until the task is approved **and** the tests (if defined) pass, ending with the final steps, or until the maximum iterations are exhausted. With the `confirm_plan` option (global or per task), automode pauses after the plan so you can confirm before implementing.

**Cycles ("Ask for more")**: once the task is completed (or at any pause), the `m` key lets you request extensions on the same project. Each extension starts a new cycle with the same logic (plan -> optional approval -> implementation -> review), keeping the history under `plan/ciclo-NN/` and `review/ciclo-NN/`.

**Scheduler, chains and repetitive tasks**: in the new-task form you can set a start time (`Start at`, format `YYYY-MM-DD HH:MM`), chain the task to another (`Chained after task`, which fires as soon as the parent reaches `DONE`), and turn it into a repetitive task. Two repeat modes are available: `interval` (every N minutes after the previous run, with the interval reused as the next start time) and `infinite` (restart when the whole chain finishes). For repetitive runs you also pick a plan policy: `reuse` (run the same plan again), `replan` (drop the plan and let the planner start from scratch) or `reevaluate` (keep the plan but ask the planner to re-check it against the description before implementing). Unattended runs always use the full pipeline in automode and ignore `confirm_plan`; tasks paused manually (`PAUSED`) are never auto-started. The detail view shows the start time and the repeat mode so the context is visible without leaving the screen.

**Tasks list**: the task table renders chained tasks as a sub-list under their parent, with the child rows indented (`+` marks the root, two spaces per level). Besides the live clock that the global header shows on every screen (date and time with seconds, updated every second), this screen also has a scope button — `Project tasks` by default (it compares `task.workdir` against the current directory, including chained children whose parent belongs to this project) and `All tasks` when toggled with the `v` key. Running tasks show a leading `▶` so they stand out even when the list is long.

**Execution safety**: no phase starts with a single key — every action opens a modal that explains what is about to happen (agent, CLI, model, directory) and asks for confirmation. While a phase runs, an activity bar shows a spinner, per-phase timings, event counts and a CLI output watchdog.

**Token counting**: each run accumulates the consumed tokens in `task.toml`, broken down by phase and by CLI + model. The tasks list shows a "Tokens (in/out)" column with the total per task and a footer line with the global summary by CLI + model. The detail view adds a "Tokens" tab with the consolidated total plus the per-phase and per-CLI+model breakdowns; usages recorded with older versions are grouped under a "Legacy" phase.

**Run time tracking**: every pipeline phase records its elapsed seconds in `task.toml` (plan, implementation, review, fix, final steps and tests). The tasks list adds a "Time" column with the total duration per task, and the footer line shows the consolidated total time across every listed task; the detail view exposes the same total through the model's `Task.total_duration_seconds()` helper.

**Completion hooks**: you can configure a shell command that runs when pipeline stages finish. There is a global hook (configuration, `c` key) and an optional per-task hook (when creating it): the task-level hook replaces the global one, or is added to it if you enable "also run the global hook". In both cases you choose which stages it triggers on (plan, implementation, review, fix, final steps and tests, including repeats of each one). Hooks receive context via `GRAFENO_*` environment variables (task id and name, workdir, phase, result, status, iteration and cycle), run on a best-effort basis (with a 120 s timeout) and never interrupt the pipeline: their output is recorded in the task log.
If the hook is an `http(s)` URL, GRAFENO does not execute any command: it sends a GET with a message (task name, stage, result and status) inserted into the `{message}` placeholder of the URL, or, if there is no placeholder, into the `text` query parameter (e.g. Telegram `.../sendMessage?chat_id=...&text={message}`). The query string is not recorded in the logs.

**Manual state control**: from the detail screen you can force-close a task with the `d` key (it marks it as `done` without further review; it still allows launching the final steps afterwards) or discard it with `D` (terminal state `discarded` that blocks the remaining pipeline actions). Both ask for confirmation before persisting.

**Automatic editor on startup**: GRAFENO can open an editor when you launch it. The feature is disabled by default — only the TUI opens until you turn it on. Enable it in the configuration screen (`c`): tick the checkbox and pick the editor from the ones detected on your system (a mix of GUI and console editors, or any binary already on `PATH`). For console editors you can choose how to open it — new window, split pane (the editor on the left, GRAFENO on the right by default), or nothing. Ghostty, WezTerm, kitty, iTerm, Terminal.app, tmux and Alacritty are auto-detected; only those that support splits expose the split option. Per-project overrides go in `<project>/.grafeno.toml` under `[editor]` (only that section is read; missing fields inherit the global config). Pass `--noeditor` on the command line to skip the editor launch for a single run.

**GitHub issue selector**: when the project directory is a git repo with the `gh` CLI installed and authenticated access to it, the new-task form shows an optional "From GitHub issue" selector that lists the open issues of the repository (loaded in the background, never blocking the modal). Picking one fills the task name with the issue title and the description with the issue body (falling back to the title when the body is empty); typing in the name/description afterwards overwrites the prefilled values as usual. When `gh` is missing, the directory is not a repo, or the user has no access, the selector stays hidden and the form behaves exactly as before.

**References**: each task can attach named resources (a local directory or URL plus a short description) as inspiration or context for the planner, re-evaluator and implementer. There are three levels — global (`~/.grafeno/references.toml`, edited from the configuration screen `c`), project (the `[[references]]` array of `<workdir>/.grafeno.toml`) and per task (entered in the new-task form `n`). The new-task form also exposes two checkboxes to exclude the global and/or project level per task. Because the agents actually read those resources, large ones can noticeably increase token consumption; both the configuration screen and the new-task form show a reminder of this.

**Trigger tasks**: a trigger is a task template that is spawned automatically at a pipeline phase boundary (`before` or `after`) of another task. There are two levels — global (`~/.grafeno/triggers.toml`, edited from the configuration screen `c`) and project (the `[[triggers]]` array of `<workdir>/.grafeno.toml`). Each trigger declares the phases it listens to (`all` or any subset of plan, implement, review, fix, final steps, tests) and whether it fires before or after them. When it fires, a new independent GRAFENO task is created in automode and scheduled for the current minute, so the scheduler tick of the App starts it unattended: triggers never block nor interfere with the task that fired them, and tasks spawned by a trigger do not fire further triggers (no recursion).

**Remote projects over SSH**: a task can target a remote project via SSH (scp-like `user@host:/path` or `ssh://user@host[:port]/path` in the new-task form). GRAFENO mounts the remote directory locally with `sshfs` under `~/.grafeno/mounts/<slug>-<hash>/` so the agent CLIs work transparently on a plain local path; task data (`~/.grafeno/tasks/<id>/`) is mirrored to the remote host with `rsync --update` over ssh (push after every phase and after tests, pull on run start and on opening the detail), so the history lives on both sides and the run can be continued from either one. The host requires `sshfs` (to mount) and `rsync` (to mirror); without them the task is still created, but it will not run / sync and a warning is shown. When the remote host is the local machine (`localhost`, `127.0.0.1`, the own hostname), the mount is skipped and the remote path is used directly. Remote tasks only appear under the "All tasks" scope (the "Project tasks" scope compares against the current cwd).

**Usage-limit retries**: when an agent CLI reports an exhausted quota/rate limit (`429`, `rate limit`, `quota exceeded`, `usage limit`, `insufficient_quota`, `out of credits`, etc.), GRAFENO does not fail the phase. If the message carries a `retry after` / `try again in` time hint, the orchestrator waits exactly that long and retries the same phase, reusing the session when possible. When there is no time hint, it probes every 60 s, up to 30 attempts per phase, before giving up. While a phase is waiting, the tasks list and the `PhaseBar` append a `Waiting` suffix to the current state so you can tell at a glance that the pipeline is paused on quota, not stalled.

**Interface language**: the GUI can be displayed in English (default) or Spanish; it is chosen in the configuration screen (`c`) and persisted in `config.toml`. When changing it, new screens apply it immediately and the shortcuts footer updates on app restart.

## Installation

```bash
pipx install .          # or: pip install .
grafeno
```

Guided installation (verifies Python 3.11+, installs pipx if missing and leaves `grafeno` on the PATH):

```bash
./install.sh        # Linux and macOS
.\install.ps1       # Windows (PowerShell)
```

Development:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/grafeno
```

## Releases

Each release is generated automatically on push to `main` by bumping the
version (`src/grafeno/__init__.py` and `pyproject.toml`, always kept in
sync): the workflow `.github/workflows/release.yml` detects the bump,
validates that both files match, builds the package and publishes the
GitHub Release with the `vX.Y.Z` tag and attached artifacts.

## Usage

| Key | Screen | Action |
|---|---|---|
| `n` | List | New task |
| (form) | New task | The "Project directory" field autocompletes paths with a dropdown (arrows/Enter or mouse). "Start at", "Chained after task", repeat mode, plan-reuse policy and task-level references (with per-level exclusion checkboxes) are also configurable here. If the project is a repo with `gh` access, an optional "From GitHub issue" selector preloads the name and description from any open issue. |
| `c` | List | Global configuration |
| `Enter` | List | Open task |
| `v` | List | Toggle scope: project tasks only / all tasks |
| `r` | List | Reload the task list |
| `p` / `i` / `r` / `f` | Detail | Plan / Implement / Review / Fix (with confirmation) |
| `s` | Detail | Final steps (with confirmation) |
| `t` | Detail | Run tests |
| `a` | Detail | Automode |
| `m` | Detail | Ask for more (new extension cycle) |
| `e` | Detail | Change CLI and model of each task agent |
| `E` | Detail | Edit task name and description |
| `d` | Detail | Force-close (marks the task as `done`, with confirmation) |
| `D` | Detail | Discard task (terminal state `discarded`, with confirmation) |
| `R` | Detail | Restart the task from scratch (aborts the current run if any, then resets to `DRAFT` and clears `plan/`, `review/`, `final/` and the schedule, with confirmation) |
| `x` | Detail | Cancel execution |
| `Esc` | Detail/Config | Back |
| `Ctrl+T` | Global | Theme selector (the chosen palette is persisted in the config) |
| `Ctrl+Q` / `Cmd+Q` (macOS) | Global | Quit (pressing `q` in the list only shows this hint; it does not quit) |

## Data

```
~/.grafeno/
├── config.toml              # language (en/es), roles (cli+model+effort), automode, tests, git, theme (palette), final-steps prompt, global hook, editor, auto_update
├── references.toml          # global references (name + description + path/URL); edited from the configuration screen
├── triggers.toml            # global trigger tasks (name + description + phases + timing + workdir); edited from the configuration screen
└── tasks/<date>-<slug>/
    ├── task.toml            # state, iterations, sessions, workdir, branch, scheduling (scheduled_at, parent_id, repeat_mode, plan_reuse, repeat_count, last_completed_at), per-role effort level, references + use_global_references/use_project_references flags
    ├── plan/*.md            # plans with GRAFENO-EXECUTOR header
    ├── review/*.md          # reviews numbered by iteration
    ├── final/*.md           # final-step reports per cycle
    └── logs/*.jsonl         # raw output from each CLI invocation
```

The base directory can be changed with the `GRAFENO_HOME` environment variable.

## Tests

```bash
.venv/bin/python -m pytest
```

Includes unit tests (config, prompts, verdict, drivers, orchestrator with
fake drivers) and TUI smoke tests in headless mode.
