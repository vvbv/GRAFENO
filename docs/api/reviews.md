# Assisted review of pending git changes

## Why

Review the uncommitted changes of a project (including nested repositories)
file by file, discuss them with an agent, ask for changes and commit explicitly
the files the user accepts. Nothing is ever committed automatically: the task
pipeline does not commit and the only call that creates commits is
`POST /api/v1/reviews/{id}/commit`. Grafoide is the intended client.

## Concepts

- Review session: persisted in `~/.grafeno/reviews/<id>/` (`review.toml` and `commits.jsonl`). The git state is never stored: every response recomputes it.
- Repo id: posix path of a repository relative to the project root; `.` is the root (or the enclosing) repository.
- File identity: always the pair `repo` + `path`, where `path` is relative to THAT repository. Each entry also carries `key` (opaque stable hash), `project_path` and `abs_path`. Every file operation accepts `{"repo","path"}` or `{"key"}`.
- Decisions: `pending`, `accepted`, `changes_requested`, `skipped`, with an optional note.
- Cursor: the file the client is looking at; `navigate` moves it (`first`, `next`, `prev`, `goto`, `current`) and never wraps around (`end: true`, also when `first` with `skip_decided` finds nothing open). When the file under the cursor disappears (for example it was just committed) the entry that took its place is returned by `current` and by the next `next`, so no file is skipped. `decision` with `advance` moves on from the file just decided.
- `outdated`: the file changed after its decision was recorded (compared through `fingerprint`); it counts as pending again.
- Agent session: one direct CLI session per review (visible in `GET /api/v1/sessions`), created on the first `discuss` or `request-changes` and reused on later turns. Poll `transcript` for the answer.
- Repository entries also carry `upstream`, `ahead`, `behind` and `upstream_gone`; branch operations are documented in [project-git.md](project-git.md).

## Nested repositories

Repositories nested at any depth below the project root are discovered (any depth,
up to 200 repositories and 20000 scanned folders; `.git`, `node_modules`, `.venv`, `dist` and
similar folders are skipped). A change in `modules/modulo1/algo.py` is reported
as `repo: "modules/modulo1"`, `path: "algo.py"`; the root repository does not
list it and committing it records the commit in the nested repository only.
Submodules (`.git` file) are repositories of kind `submodule`; the pointer
change shows up in the parent only when the submodule commit moved. Nested
repositories inside ignored folders are listed in the `hidden` field of their
parent repository. When the project folder
is inside a larger repository, that repository is returned as kind `outer`
limited to the project folder.

## Safety model

- Only projects listed by `GET /api/v1/projects` or the folder of a local task are accepted; remote projects and remote session mode answer 409.
- Client paths are validated and always matched against the current `git status`; arbitrary files are never read.
- Commits use `git commit --only` with literal pathspecs (`GIT_LITERAL_PATHSPECS=1`) and a message on stdin: only the listed paths are recorded and the staging area of other paths is preserved. No `-a`, `add -A`, `--no-verify` or author override; hooks run.
- Rejected: conflicted files, partially staged files (unless `include_unstaged`), stale fingerprints, paths that are not pending, repositories with a merge, rebase, cherry-pick or revert in progress.
- Commits of the same repository are serialized with a lock; all repository groups are validated before the first commit is made.
- The commit message suggestion (`commit-message`) is a one-shot agent run and never commits.

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/v1/projects/repos` | Repositories of a project (`workdir` or `project`) |
| GET | `/api/v1/reviews` | List reviews (`workdir?`, `state?`) |
| POST | `/api/v1/reviews` | Start or resume (`workdir`/`project`/`task_id`, `name?`, `cli?`, `model?`, `effort?`, `resume?`) |
| GET | `/api/v1/reviews/{id}` | Detail: repos, files, counts, current |
| DELETE | `/api/v1/reviews/{id}` | Delete (the agent session is kept) |
| POST | `/api/v1/reviews/{id}/close` | Close |
| POST | `/api/v1/reviews/{id}/reopen` | Reopen |
| GET | `/api/v1/reviews/{id}/files` | Files (`repo?`, `decision?`) |
| GET | `/api/v1/reviews/{id}/diff` | Diff of a file (`repo`+`path` or `key`, `max_bytes?`) |
| POST | `/api/v1/reviews/{id}/navigate` | Move the cursor |
| POST | `/api/v1/reviews/{id}/decision` | Record a decision (`advance?`) |
| POST | `/api/v1/reviews/{id}/discuss` | Ask the agent (asynchronous turn) |
| POST | `/api/v1/reviews/{id}/request-changes` | Ask the agent to apply changes to one file |
| GET | `/api/v1/reviews/{id}/transcript` | Agent conversation (`after?`, `limit?`) |
| POST | `/api/v1/reviews/{id}/cancel` | Cancel the running agent turn |
| POST | `/api/v1/reviews/{id}/commit-message` | Suggest a commit message (never commits) |
| POST | `/api/v1/reviews/{id}/commit` | Commit explicit files |
| GET | `/api/v1/reviews/{id}/commits` | History of commits made from the review |

WebSocket methods: `projects.repos`, `reviews.list`, `reviews.start`,
`reviews.get`, `reviews.delete`, `reviews.close`, `reviews.reopen`,
`reviews.files`, `reviews.diff`, `reviews.navigate`, `reviews.decide`,
`reviews.discuss`, `reviews.request_changes`, `reviews.transcript`,
`reviews.cancel`, `reviews.commit_message`, `reviews.commit`,
`reviews.commits` (params = query + body + `review_id`). Subscribe to the
`reviews` topic for `review.changed` and `review.deleted` events.

Typical walkthrough:

```bash
B=http://127.0.0.1:8735/api/v1
curl -s -X POST $B/reviews -H 'Content-Type: application/json' -d '{"workdir":"/path/to/project"}'
curl -s -X POST $B/reviews/RID/navigate -H 'Content-Type: application/json' -d '{"action":"first"}'
curl -s "$B/reviews/RID/diff?repo=modules/modulo1&path=algo.py"
curl -s -X POST $B/reviews/RID/decision -H 'Content-Type: application/json' \
  -d '{"repo":"modules/modulo1","path":"algo.py","decision":"accepted","advance":true}'
curl -s -X POST $B/reviews/RID/commit-message -H 'Content-Type: application/json' \
  -d '{"files":[{"repo":"modules/modulo1","path":"algo.py"}]}'
curl -s -X POST $B/reviews/RID/commit -H 'Content-Type: application/json' \
  -d '{"files":[{"repo":"modules/modulo1","path":"algo.py"}],"message":"feat: add algo"}'
```

## Error codes

Errors are `{"error": "<message>", "code": "<code>"}`; `code` only appears on
errors that have one (WebSocket: `error.code`).

| Code | Status | Meaning |
| --- | --- | --- |
| `invalid_path` | 400 | Empty, absolute, `..` or option-like path |
| `empty_message` | 400 | Commit message is empty |
| `multi_repo` | 400 | Files of several repositories without `allow_multi_repo` |
| `not_pending` | 409 | The file is not a pending change |
| `conflicted` | 409 | The file has merge conflicts |
| `not_committable` | 409 | The change cannot be committed (e.g. dirty submodule content) |
| `partially_staged` | 409 | Staged and unstaged changes; pass `include_unstaged` to commit the working tree content |
| `stale` | 409 | The file changed since `fingerprint` was read |
| `operation_in_progress` | 409 | Merge, rebase, cherry-pick or revert in progress |
| `not_a_repo` | 409 | Repository not found in the project |
| `busy` | 409 | The agent is already running a turn |
| `closed` | 409 | The review is closed |
| `remote` | 409 | Remote session mode |
| `commit_failed` | 422 | Git or a hook rejected the commit; untracked files are left untracked |
| `git_failed` | 500 | Unexpected git failure |
| `agent_failed` | 502 | The commit message agent failed |
| `timeout` | 504 | Git or the agent timed out |
| `git_missing` | 503 | git is not installed |
