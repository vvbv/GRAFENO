# Project git: pending indicator and branch operations

## Why

The projects list of a client (Grafoide) needs an indicator of pending
(uncommitted) changes, and the review screen needs to push, pull and switch
branches without leaving the app. These endpoints give both, for projects with
several Git repositories nested at any depth.

## Pending changes indicator

`GET /api/v1/projects/changes` (WS `projects.changes`) returns one summary per
project: `git`, `dirty`, number of `repos`, `dirty_repos` (repo ids), `files`,
`conflicts`, `ahead`/`behind` sums, `operations` (merge/rebase in progress) and
`errors` per repo.

- Discovery has no depth limit. It is bounded by `MAX_REPOS` (200) and `MAX_SCAN_DIRS` (20000 folders); when a budget is hit `incomplete` is true.
- Nested repositories inside ignored folders (`node_modules`, `build`, ...) are counted in `hidden`.
- Summaries are cached 15 s per project; `refresh=true` bypasses the cache. Push, pull, checkout, abort and review commits invalidate it.
- Remote (SSH) projects and missing folders answer `git: false`; sshfs is never mounted. In remote session mode the answer is 200 with `session: true` and no projects.

## Branch operations

All operations take `workdir` or `project` plus `repo` (id from `GET /api/v1/projects/repos`, `.` by default).

- `GET /api/v1/projects/branches` lists local branches with upstream, ahead/behind and last commit.
- `POST /api/v1/projects/push` pushes the current branch to its upstream; a branch without upstream, with a missing one or with a differently named one needs `set_upstream` (and `remote` when several remotes exist without `origin`).
- `POST /api/v1/projects/pull` pulls the current branch with `mode` `ff-only` (default), `merge` or `rebase`.
- `POST /api/v1/projects/checkout` switches to an existing local branch with `git switch --no-guess`; branches are never created.
- `POST /api/v1/projects/abort` aborts the merge, rebase, cherry-pick or revert in progress.

Nothing is forced (no `--force`, `reset`, stash or `--no-verify`). Mutations hold the same per-repository lock as review commits. Network commands time out after 300 s, local ones after 120 s, run without a terminal and never prompt for credentials.

A 4xx/5xx response means the current branch did not move (a pull still updates remote-tracking refs, and after a `timeout` a merge or rebase may be left in progress: re-read the repository status). A 200 is read through `result.outcome`.

## Conflicts

A pull with `ff-only` that cannot fast-forward fails with `diverged`; retry with `merge` or `rebase`. A merge or rebase that stops on conflicts answers 200 with `result.outcome: "conflict"`, the conflicted paths in `result.conflicts` and the operation in `result.in_progress`. The files also show up in the review as `conflicted`. Leave the state with `abort`.

## Endpoints

| Method | Route | Parameters | WS |
| --- | --- | --- | --- |
| GET | `/api/v1/projects/changes` | `workdir?`, `project?`, `refresh?` | `projects.changes` |
| GET | `/api/v1/projects/branches` | `workdir`/`project`, `repo?` | `projects.branches` |
| POST | `/api/v1/projects/push` | `workdir`/`project`, `repo?`, `set_upstream?`, `remote?` | `projects.push` |
| POST | `/api/v1/projects/pull` | `workdir`/`project`, `repo?`, `mode?` | `projects.pull` |
| POST | `/api/v1/projects/checkout` | `workdir`/`project`, `repo?`, `branch` | `projects.checkout` |
| POST | `/api/v1/projects/abort` | `workdir`/`project`, `repo?` | `projects.abort` |

```bash
B=http://127.0.0.1:8735/api/v1
curl -s "$B/projects/changes?refresh=true"
curl -s "$B/projects/branches?workdir=/abs/proj&repo=."
curl -s -X POST $B/projects/pull -H 'Content-Type: application/json' \
  -d '{"workdir":"/abs/proj","mode":"merge"}'
curl -s -X POST $B/projects/checkout -H 'Content-Type: application/json' \
  -d '{"workdir":"/abs/proj","branch":"feature"}'
```

## Error codes

Errors are `{"error": "<message>", "code": "<code>"}` (WS: `error.message`, `error.code`, `error.status`).

| Code | HTTP | Meaning |
| --- | --- | --- |
| `invalid_branch` | 400 | Empty or invalid branch name |
| `invalid_mode` | 400 | Unknown pull mode |
| `branch_not_found` | 404 | The local branch does not exist |
| `detached` | 409 | HEAD is detached (push/pull) |
| `no_commits` | 409 | The branch has no commits (push) |
| `no_upstream` | 409 | No upstream (pull; push without `set_upstream`) |
| `upstream_gone` | 409 | The configured upstream no longer exists |
| `no_remote` | 409 | No remotes or unknown remote |
| `ambiguous_remote` | 409 | Several remotes without `origin`: pass `remote` |
| `local_changes` | 409 | Local changes the operation would overwrite |
| `diverged` | 409 | Diverged branches with `ff-only` |
| `push_rejected` | 409 | Push rejected (pull first, or a remote hook refused it) |
| `upstream_mismatch` | 409 | The upstream has a different name than the branch (e.g. created from `origin/main`); push with `set_upstream` to publish it under its own name |
| `no_operation` | 409 | `abort` without an operation in progress |
| `auth_failed` | 502 | The remote rejected the credentials |
| `remote_unreachable` | 502 | Network or remote host unreachable |

Also reused: `operation_in_progress`, `conflicted`, `not_a_repo`, `remote` (409), `git_failed` (500), `git_missing` (503), `timeout` (504).

Push always targets `branch.<name>.remote`; `pushRemote` and `remote.pushDefault` are not honored.
