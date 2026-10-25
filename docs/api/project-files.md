# Project files: browse, read and download

## Why

Grafoide browses a project from the phone: folders, code and text files, image
previews and PDFs. Reading files through a CLI session would spend tokens; these
read-only endpoints return the data directly.

## Security model

- Only projects listed by `GET /api/v1/projects` are accepted, never a free path.
- `path` is a POSIX path relative to the project root. Absolute paths and `..` are rejected (400).
- `.git` is never listed or served, at any depth (403).
- `.git` is matched case-insensitively (`.GIT`, `.Git`).
- Dotfiles are hidden from listings unless `hidden=1`; `hidden` only affects listings, so a dotfile such as `.env` can still be read through `file`, `file/text` and `file/raw` when its path is known. They may contain secrets such as `.env`: the API is as protected as its token configuration, so configure tokens when exposing it beyond loopback.
- Symlinks leaving the project folder or pointing into `.git` are `blocked` in listings and 403 on access.
- FIFOs, sockets and devices are listed as `other` and never served.
- Permission problems are 403.
- Remote projects and remote session mode answer 409.
- The API is read-only: there are no write routes.
- Raw responses never use `text/html`, `image/svg+xml` or JavaScript types (they are served as `text/plain`) and carry `X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox`.

## Endpoints

Every endpoint accepts `workdir` (absolute project folder, takes precedence) or `project` (folder name, exact and case-insensitive), and `path` (relative, empty = root).

`GET /api/v1/projects/files` (WS `projects.files`): one directory, folders first.

| Parameter | Description |
| --- | --- |
| `hidden` | Include dotfiles (`1`, `true`, `yes`, `on`), default off |
| `limit` | Page size, 1..2000, default 500 |
| `offset` | Entries to skip, default 0 |

`GET /api/v1/projects/file` (WS `projects.file`): metadata of one file or folder (size, dates, `etag`, `mime`, `kind`, `preview`, `text`, `urls`).

`GET /api/v1/projects/file/text` (WS `projects.file_text`): one page of lines of a text file.

| Parameter | Description |
| --- | --- |
| `line` | First line, 1-based, default 1 |
| `max_lines` | Page size, 1..5000, default 400 |

`GET /api/v1/projects/file/raw` (WS `projects.file_raw`): raw bytes.

| Parameter | Description |
| --- | --- |
| `download` | Send `Content-Disposition: attachment` instead of `inline` |
| `Range` header | A single byte range, answered with 206 |
| `If-Range` header | ETag or Last-Modified; if stale the whole file is sent |

Fields of `entries[]`:

| Field | Description |
| --- | --- |
| `name`, `path` | Entry name and path relative to the project root |
| `type` | `dir`, `file`, `other`, `broken` (dangling symlink) or `blocked` (symlink leaving the project or pointing into `.git`) |
| `size`, `modified` | Bytes and ISO timestamp (null when not applicable) |
| `hidden`, `symlink`, `readable` | Flags |
| `mime`, `kind` | Safe MIME type and attachment kind |
| `preview` | `image`, `pdf`, `audio`, `video`, `text`, `download` or `unknown`; in listings it is a hint from the suffix, `unknown` means "ask `projects/file`" |

## Errors

| Status | Cause |
| --- | --- |
| 400 | Invalid parameter, `..` or absolute path, not a directory / not a regular file |
| 401 | Missing or invalid token |
| 403 | Outside the project, `.git`, permission denied |
| 404 | Project or path not found |
| 409 | Ambiguous project, remote project, remote session mode |
| 413 | WS `projects.file_raw` only: file above 16 MiB |
| 415 | Binary file on the text endpoint (`code: "binary_file"`) |
| 416 | Range not satisfiable (`Content-Range: bytes */size`) |

## Large files

- A directory scan stops at 10 000 entries (`truncated`); pages hold 500 entries by default (max 2000).
- The text endpoint decodes at most 2 MiB (`clipped`); when `clipped` is true, `total_lines` counts only the lines of the decoded 2 MiB window and the rest of the file is available only through the raw endpoint and pages by lines (default 400, max 5000; a page stops past 256 000 characters). It reports `decode_errors` and `newline`.
- The raw endpoint streams any size with constant server memory, supports single `Range` requests (206) for resumable downloads and seeking, ignores multi-range requests (full 200) and answers 416 with `Content-Range: bytes */size`.
- `If-Range` with a stale `ETag` returns the whole file.
- WS `projects.file_raw` returns base64 and is limited to 16 MiB.

## Recipes

```bash
B=http://127.0.0.1:8735/api/v1
H="Authorization: Bearer $TOKEN"
curl -s -H "$H" "$B/projects/files?project=GRAFENO&path=src/grafeno"
curl -s -H "$H" "$B/projects/file?project=GRAFENO&path=pyproject.toml"
curl -s -H "$H" "$B/projects/file/text?project=GRAFENO&path=src/grafeno/__init__.py&max_lines=5"
curl -s -H "$H" -o shot.png "$B/projects/file/raw?project=GRAFENO&path=docs/screenshot.png"
curl -s -H "$H" -H 'Range: bytes=0-99' "$B/projects/file/raw?project=GRAFENO&path=docs/screenshot.png"
```

```json
{"id":1,"method":"projects.files","params":{"project":"GRAFENO","path":"src"}}
{"id":2,"method":"projects.file_text","params":{"project":"GRAFENO","path":"src/grafeno/__init__.py","line":1,"max_lines":200}}
```

## Compatibility

Available since 1.89.0. Clients check `GET /api/v1/status` `version >= 1.89.0` or treat `404 {"error": "not found"}` (missing route) as unsupported, which differs from `path not found`.

## Notes for Grafoide

Work for the sibling repo `../Grafoide`, not part of this server change.

- `api/Models.kt`: DTOs `ProjectFilesResponse`, `ProjectFileEntry`, `ProjectFileInfo`, `ProjectFileUrls` and `ProjectFileText`, with a default for every field (forward compatible).
- `api/GrafenoClient.kt`: `projectFiles(workdir?, project?, path, hidden, limit, offset)`, `projectFile(...)`, `projectFileText(..., line, maxLines)` and `downloadProjectFile(..., target, onProgress)`. The download reuses the `downloadMedia` pattern: stream to a `.part` file and rename on completion; resume with `Range: bytes=<partial size>-` and `If-Range: <etag>`.
- `supportsProjectFiles()`: gate by server version >= 1.89, like `supportsProjectDocs()`.
- Previews: load images (`preview == "image"`) through the authenticated client (Bearer header). Download PDFs and other documents to the app cache and open them with `ACTION_VIEW` through a `FileProvider` URI, using the `mime` field. Avoid handing a URL with `?token=` to external apps.
- Text viewer: page with `next_line` while `has_more`; when `clipped` offer download instead of more pages; show a binary placeholder on 415.
- Always pass the exact `workdir` returned by `/api/v1/projects` to avoid 409 on ambiguous names.
