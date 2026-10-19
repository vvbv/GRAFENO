# Project documents: README.md, AGENTS.md and GRAFENO.md

## Why

A voice assistant (Grafoide) needs project context on demand. Opening a CLI
session runs an agent and spends tokens; these read-only endpoints return the
project documents directly, without reading arbitrary files.

## Security model

- Only projects listed by `GET /api/v1/projects` are accepted, never a free path.
- Fixed whitelist of three names: `README.md`, `AGENTS.md`, `GRAFENO.md`.
- Only the first level of the project folder is searched (case-insensitive match).
- Symlinks resolving outside the project folder are `blocked` (index) or 403 (content).
- Same token authentication as the rest of the API.
- Remote projects and remote session mode are not supported (409).

## Endpoints

`GET /api/v1/projects/docs` (WS `projects.docs`): index, no content.

| Parameter | Description |
| --- | --- |
| `workdir` | Absolute project folder; takes precedence over `project` |
| `project` | Folder name, exact and case-insensitive |

`GET /api/v1/projects/doc` (WS `projects.doc`): one page of one document.

| Parameter | Description |
| --- | --- |
| `workdir`, `project` | As above |
| `name` | `README.md`, `AGENTS.md` or `GRAFENO.md` (case-insensitive, `.md` optional) |
| `offset` | Character offset, default 0 |
| `max_chars` | Page size, 1..200000, default 4000 |

| Status | Cause |
| --- | --- |
| 400 | Invalid parameter, name outside the whitelist, no workdir/project |
| 403 | Document is a symlink leaving the project folder |
| 404 | `project not found`, `project folder not found`, `document not found: NAME`, `document not readable: NAME` |
| 409 | Ambiguous project, remote project, remote session mode |

## Context budget

- At most 1 MiB is read per file (`clipped: true` when larger).
- Text is normalized with `normalize_markdown`; offsets and counts are characters.
- `approx_tokens` is chars / 4.
- The default page is 4000 characters, cut at a line break when possible.
- `outline[].offset` of the index can be passed as `offset` to jump to a section.

## Recipes

```bash
curl -s 'http://127.0.0.1:8735/api/v1/projects/docs?project=GRAFENO'
curl -s 'http://127.0.0.1:8735/api/v1/projects/docs?workdir=%2Fhome%2Fdev%2FGRAFENO'
curl -s 'http://127.0.0.1:8735/api/v1/projects/doc?project=GRAFENO&name=readme&max_chars=2000'
# next page: use next_offset from the previous response while has_more is true
curl -s 'http://127.0.0.1:8735/api/v1/projects/doc?project=GRAFENO&name=readme&offset=2000&max_chars=2000'
```

WebSocket equivalent:

```json
{"id":1,"method":"projects.docs","params":{"project":"GRAFENO"}}
{"id":2,"method":"projects.doc","params":{"project":"GRAFENO","name":"AGENTS.md","offset":0,"max_chars":4000}}
```

## Compatibility

Available since 1.83.0. Older clients can check `GET /api/v1/status` for
`version >= 1.83.0`, or treat a 404 `{"error": "not found"}` (missing route,
different from `project not found`) as unsupported.

## Notes for Grafoide

Work needed in `../Grafoide` (not part of this task): no tool reads project
documents today; the only path is `ask_cli_session`, which runs an agent and
consumes tokens.

- `api/Models.kt`: DTOs `ProjectDocsResponse`, `ProjectDocItem`, `ProjectDocOutlineEntry`, `ProjectDocResponse`, `ProjectRef` with defaults.
- `api/GrafenoClient.kt`: `projectDocs(workdir?, project?)`, `projectDoc(workdir?, project?, name, offset, maxChars)` and `supportsProjectDocs()` (version >= 1.83.0, like `supportsCliSessions()`).
- `voice/AssistantTools.kt`: tools `list_project_docs` and `read_project_doc` (name enum, offset, max_chars). `MAX_OUTPUT` is 6000 characters including the JSON envelope: ask for `max_chars` around 3500-4000 and hand `has_more`/`next_offset` to the model instead of truncating. Resolve the spoken project as `SessionQueries.kt` does and pass the exact `workdir` to avoid 409. Treat 404 `document not found` as "the file does not exist".
- `voice/Prompts.kt`: use these tools when the user asks for a README/AGENTS/GRAFENO.md or needs general project context (purpose, stack, conventions); read the index first and only the relevant sections through `outline`; never assume the files exist; prefer them over `ask_cli_session` for general project questions.
- JVM tests in `app/src/test` for DTO parsing and paging.
