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
- SSH runs in `BatchMode`: keys must be loaded and the host must be in `known_hosts`.
- Credentials in the URL are redacted in responses and in the log.
- If git fails, the folder created by that call is removed; an existing folder is never touched.
- Timeouts: `git init` 30 s, `git clone` 600 s.
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
| 400 | `invalid project name: ...`, `unsupported url: ...`, `mode must be one of: init, clone, plain`, `url is only allowed with mode clone`, `url is required for mode clone`, `workspace is required: several working directories are configured`, `workspace is not a configured working directory`, `<field> must be a string` |
| 409 | `project already exists: NAME`, `no working directory configured`, remote session mode |
| 422 | `git clone failed: ...` |
| 500 | `git init failed: ...`, `cannot create folder: ...` |
| 503 | `git is not installed` |

## Examples

```bash
curl -s -XPOST http://127.0.0.1:8735/api/v1/projects -d '{"name":"demo"}'
# {"project": {"workdir": "/home/dev/code/demo", "count": 0, "sessions": 0, "name": "demo", "workspace": "/home/dev/code"}, "mode": "init", "git": true, "url": ""}
curl -s -XPOST http://127.0.0.1:8735/api/v1/projects -d '{"url":"https://github.com/octocat/Hello-World.git"}'
# {"project": {"name": "Hello-World", ...}, "mode": "clone", "git": true, "url": "https://github.com/octocat/Hello-World.git"}
curl -s -XPOST http://127.0.0.1:8735/api/v1/projects -d '{"name":"notes","mode":"plain"}'
# {"project": {"name": "notes", ...}, "mode": "plain", "git": false, "url": ""}
```
