# Swagger UI and API specifications

GRAFENO includes a self-hosted Swagger UI for its REST API and localized OpenAPI and AsyncAPI documents. The documentation is available while the TUI and its API server are running.

## Start the server and open Swagger

Enable the `[api]` section in the GRAFENO settings and start the TUI with `grafeno`, or start the API for one run without changing the saved configuration:

```bash
grafeno --web
```

With the default host and port, open [http://127.0.0.1:8735/docs](http://127.0.0.1:8735/docs). If you changed `[api] host` or `port`, or used `--web-host` or `--web-port`, use that host and port instead.

## Languages and specification URLs

Use `?lang=en` or `?lang=es` on `/docs` to select English or Spanish. Without `lang`, the documentation uses the current GRAFENO interface language; an unsupported value falls back to English.

The same selection applies to the four machine-readable documents:

- [OpenAPI 3.1 in English](http://127.0.0.1:8735/docs/openapi.json?lang=en): `/docs/openapi.json?lang=en`
- [OpenAPI 3.1 in Spanish](http://127.0.0.1:8735/docs/openapi.json?lang=es): `/docs/openapi.json?lang=es`
- [AsyncAPI 3.0 in English](http://127.0.0.1:8735/docs/asyncapi.json?lang=en): `/docs/asyncapi.json?lang=en`
- [AsyncAPI 3.0 in Spanish](http://127.0.0.1:8735/docs/asyncapi.json?lang=es): `/docs/asyncapi.json?lang=es`

The documentation shell, its whitelisted assets and these specifications are public because they contain no task data or secrets. API operations keep the normal authentication policy.

## Authentication

Authentication is optional and is open only when no API tokens are configured. When tokens exist, use one of the supported credentials:

- Click **Authorize** in Swagger UI and enter the token in `BearerAuth`; Swagger then sends `Authorization: Bearer <token>`.
- Add `?token=<token>` to an API request when using the query-token method.

Never put real tokens in examples, committed documentation or logs. The `/docs` page and specifications do not bypass authentication for **Try it out**, and `GET /api/v1/skill.md` remains protected whenever tokens are configured.

## Try it out and WebSocket

For REST operations, expand an operation, click **Try it out**, complete the parameters or request body and click **Execute**. Authorize first if the server requires a token.

Swagger UI is not a WebSocket client: it cannot perform the `/api/v1/ws` upgrade or invoke JSON-RPC methods with **Try it out**. Use an RFC 6455 WebSocket client for the handshake and the linked AsyncAPI document for the protocol. AsyncAPI describes the JSON-RPC requests and responses, subscriptions, topics and push events; these are not REST POST operations.

## Download `skill.md`

`GET /api/v1/skill.md` downloads the packaged Markdown skill for agents and API consumers. It accepts no file path or name and has no WebSocket equivalent. With authentication satisfied, a successful response returns the exact UTF-8 Markdown with these headers:

```text
Content-Type: text/markdown; charset=utf-8
Content-Disposition: attachment; filename="skill.md"
Cache-Control: no-store
X-Content-Type-Options: nosniff
```

The package includes `src/grafeno/server/static/skill.md`, so a correctly authenticated download returns `200`; a `404` with `{"error":"skill.md is not available"}` means the resource is unavailable in that installation. This portable Agent Skill is installed as `grafeno-api/SKILL.md`.

## Contributor workflow

The English YAML files are the structural sources and the Spanish catalogs are mandatory:

- `docs/api/openapi.yaml` and `docs/api/asyncapi.yaml`: English sources.
- `docs/api/openapi.es-translations.yaml` and `docs/api/asyncapi.es-translations.yaml`: Spanish translation catalogs.
- `src/grafeno/server/static/docs/*.json`: generated English and Spanish artifacts served at runtime.

After changing any source or catalog, regenerate and verify all four JSON documents:

```bash
.venv/bin/python scripts/build_api_docs.py
.venv/bin/python scripts/build_api_docs.py --check
```

Do not edit only the generated JSON files: the next build would overwrite the change and `--check` would detect stale artifacts. The canonical skill content lives only in `src/grafeno/server/static/skill.md`; do not create duplicate copies.

Swagger UI is vendorized in the GRAFENO package. It needs no CDN, external documentation service or third-party credentials at runtime.
