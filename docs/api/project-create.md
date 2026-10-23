# Project creation in the working directories

## Why

A voice assistant (Grafoide) creates projects by voice and then opens tasks or
CLI sessions in them. Projects are first-level folders of the configured
working directories (workspaces).

## Modes

| Mode | Behavior |
| --- | --- |
| `init` | Folder + `git init`. Default when there is no `url`. |
| `clone` | `git clone` of `url`. Default when `url` is given; `name` is optional (derived from the repository). |
| `plain` | "Create without Git": only the folder. |

## Listing

`GET /api/v1/projects` (WS `projects.list`) lists every non-hidden first-level
folder of the working directories even when it has no tasks or sessions
(`count` and `sessions` are 0). Items are merged by normalized path, so a
workdir with a trailing slash does not duplicate its folder. The root of the
response carries `create` with `available`, `git`, `modes`, `default_mode`,
`url_schemes`, `name_max_length` and `workspaces`.

## Security model

- The project is only created inside a configured, existing working directory, never in a free path.
- Names: at most 100 characters; no `/ \ : * ? " < > |`, no control characters, no leading `.` or `-`, no trailing `.` or space, no Windows device names.
- URLs: `https://`, `http://`, `ssh://` and scp-like `user@host:path`; `file://`, `git://`, `ext::`, local paths, spaces and a leading `-` are rejected.
- git runs without a shell, with `--` before the URL, `GIT_TERMINAL_PROMPT=0` and `GIT_ALLOW_PROTOCOL=http:https:ssh`.
- SSH runs in `BatchMode` with `ConnectTimeout=30`: keys must be loaded and the host must be in `known_hosts`. HTTP aborts transfers below 1000 B/s for 60 s (`GIT_HTTP_LOW_SPEED_LIMIT`/`GIT_HTTP_LOW_SPEED_TIME`, respected if the user defines them).
- git runs without a controlling terminal (own session on Linux/macOS, own process group without console window on Windows): ssh or a credential helper can never wait for an invisible prompt; a custom GIT_SSH_COMMAND that would prompt fails fast instead.
- git never prompts: `GIT_ASKPASS` is forced empty (this also overrides `core.askPass` and `SSH_ASKPASS`). Non-interactive credential helpers (store, libsecret) keep working.
- Credentials in the URL are redacted in responses and in the log.
- If git fails, the folder created by that call is removed; an existing folder is never touched.
- Windows: the cleanup also removes the read-only files git leaves in .git, so a failed clone can be retried with the same name.
- Timeouts: git init 30 s, git clone 600 s. On timeout or cancellation the whole git process tree (ssh, git-remote-https) is killed and the call returns within a few seconds (504, code timeout).
- Cloning `https://user:token@host/...` makes git store that URL, token included, as `origin` in `.git/config` of the cloned project; prefer credential helpers or SSH keys.
- Not available in remote session mode (409).

## Endpoint

`POST /api/v1/projects` (WS `projects.create`).

| Parameter | Description |
| --- | --- |
| `name` | Folder name; optional with `url` |
| `url` | Clone URL |
| `mode` | `init`, `clone` or `plain` |
| `workspace` | Working directory (configured entry or resolved path); required only when several are configured |

| Status | Cause |
| --- | --- |
| 201 | Created: `{project, mode, git, url}` |
| 400 | `invalid project name: ...`, `unsupported url: ...`, `mode must be one of: init, clone, plain`, `url is only allowed with mode clone`, `url is required for mode clone`, `workspace is required: several working directories are configured`, `workspace is not a configured working directory`, `<field> must be a string`, `invalid json`; URL messages: `url is required`, `url is too long`, `invalid url`, `invalid url host`, `url has no repository path`, `url must not contain spaces or control characters`, `unsupported url transport`; name messages: `name is required` and `invalid project name: <reason>` (includes names too long in UTF-8 bytes) |
| 409 | `project already exists: NAME`, `no working directory configured`, remote session mode |
| 422 | `git clone failed: ...` |
| 500 | `git init failed: ...`, `cannot create folder: ...` |
| 503 | `git is not installed` |
| 504 | `git init timed out after 30 s`, `git clone timed out after 600 s` |

Validation and git errors carry a stable `code` (the type errors `<field> must be a string`, `invalid json` and the remote session mode 409 do not): invalid_name, invalid_url, invalid_mode, invalid_workspace, no_workspace, exists, git_missing, clone_failed, init_failed, io_error, timeout (WS: `error.code`).

## Examples

```bash
curl -s -XPOST http://127.0.0.1:8735/api/v1/projects -d '{"name":"demo"}'
# {"project": {"workdir": "/home/dev/code/demo", "count": 0, "sessions": 0, "name": "demo", "workspace": "/home/dev/code"}, "mode": "init", "git": true, "url": ""}
curl -s -XPOST http://127.0.0.1:8735/api/v1/projects -d '{"url":"https://github.com/octocat/Hello-World.git"}'
# {"project": {"name": "Hello-World", ...}, "mode": "clone", "git": true, "url": "https://github.com/octocat/Hello-World.git"}
curl -s -XPOST http://127.0.0.1:8735/api/v1/projects -d '{"name":"notes","mode":"plain"}'
# {"project": {"name": "notes", ...}, "mode": "plain", "git": false, "url": ""}
```
