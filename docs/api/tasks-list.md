# Task list: filters, ordering and pagination

## Why

`GET /api/v1/tasks` without parameters returns EVERY task (hundreds, about 750 bytes each). Clients with output budgets (voice assistants) must filter on the server instead of truncating locally.

## Parameters

All optional; without them the behavior is unchanged.

| Parameter | Values | Effect |
| --- | --- | --- |
| `state` | one state or a comma separated list (`done,discarded`) | filters by state; unknown -> 400 |
| `workdir` | absolute project path | exact match after `expanduser` + `normpath` (no trailing slash) |
| `project` | folder name (`GRAFENO`) | case-insensitive match with the last segment of `workdir` (`GRAFENO` does not match `GRAFENO-CLOUD`) |
| `sort` | `tree` (default), `created`, `updated`, `completed` | `tree` = chain order; the rest = flat list sorted by that field |
| `order` | `desc` (default), `asc` | only for `sort` other than `tree` |
| `limit` | integer 1..1000 | page size; absent = all |
| `offset` | integer >= 0 (default 0) | offset within the filtered, sorted result |

Response fields added to `tasks` and `done_hidden_ids`:
- `total`: tasks after filtering and before paging.
- `offset`: offset applied.
- `limit`: limit applied or `null`.
- `has_more`: `offset + len(tasks) < total`.
- `completed_at` (per task): `last_completed_at`, else `updated_at` for DONE tasks, else empty. With `sort=completed`, tasks without it go last.

## Recipes

```bash
# Optional when tokens are configured: -H "Authorization: Bearer $TOKEN"

# Last 3 completed tasks of a project
curl -s 'http://127.0.0.1:8735/api/v1/tasks?state=done&project=GRAFENO&sort=completed&order=desc&limit=3'

# Same by exact folder
curl -s 'http://127.0.0.1:8735/api/v1/tasks?state=done&workdir=%2Fhome%2Fdev%2FDocuments%2FGithub%2FGRAFENO&sort=completed&limit=3'

# Pending work of a project
curl -s 'http://127.0.0.1:8735/api/v1/tasks?project=GRAFENO&state=draft,first_step,planning,planned,implementing,implemented,reviewing,fixing,finalizing,failed,paused'
```

Paging: request `limit=50&offset=0`, then `offset += len(tasks)` while `has_more` is true.

WebSocket equivalent:

```json
{"id":1,"method":"tasks.list","params":{"state":"done","project":"GRAFENO","sort":"completed","limit":3}}
```

## Compatibility

Servers older than 1.82.0 ignore the new query parameters and do not return `total`. Clients detect support by the presence of `total` (or by `GET /api/v1/status` `version >= 1.82.0`) and otherwise filter locally by `workdir`/`state` and sort by `updated_at` DESC (an approximation of completion order).

## Notes for Grafoide

- The truncation came from the client: `MAX_TASKS = 40` and the 6000-char tool output cap in `AssistantTools.kt`.
- Its `list_tasks` tool should expose `project`/`workdir`, `sort` and `limit` and pass them to the server.
- It should report `total`/`has_more` to the model instead of a partial list.
