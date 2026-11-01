# Swagger UI y especificaciones de la API

GRAFENO incluye un Swagger UI autoalojado para su API REST y documentos OpenAPI y AsyncAPI localizados. La documentación está disponible mientras se ejecutan la TUI y su servidor API.

## Arrancar el servidor y abrir Swagger

Activa la sección `[api]` en los ajustes de GRAFENO y arranca la TUI con `grafeno`, o arranca la API solo para una ejecución sin cambiar la configuración guardada:

```bash
grafeno --web
```

Con el host y el puerto predeterminados, abre [http://127.0.0.1:8735/docs](http://127.0.0.1:8735/docs). Si cambiaste `host` o `port` de `[api]`, o usaste `--web-host` o `--web-port`, utiliza ese host y ese puerto.

## Idiomas y URL de las especificaciones

Usa `?lang=en` o `?lang=es` en `/docs` para elegir inglés o español. Sin `lang`, la documentación usa el idioma actual de la interfaz de GRAFENO; un valor no soportado recurre al inglés.

La misma selección se aplica a los cuatro documentos legibles por máquinas:

- [OpenAPI 3.1 en inglés](http://127.0.0.1:8735/docs/openapi.json?lang=en): `/docs/openapi.json?lang=en`
- [OpenAPI 3.1 en español](http://127.0.0.1:8735/docs/openapi.json?lang=es): `/docs/openapi.json?lang=es`
- [AsyncAPI 3.0 en inglés](http://127.0.0.1:8735/docs/asyncapi.json?lang=en): `/docs/asyncapi.json?lang=en`
- [AsyncAPI 3.0 en español](http://127.0.0.1:8735/docs/asyncapi.json?lang=es): `/docs/asyncapi.json?lang=es`

La shell de documentación, sus activos permitidos y estas especificaciones son públicos porque no contienen datos de tareas ni secretos. Las operaciones de la API conservan la política normal de autenticación.

## Autenticación

La autenticación es opcional y el acceso es abierto solo cuando no hay tokens de API configurados. Cuando existen tokens, usa una de las credenciales soportadas:

- Pulsa **Authorize** en Swagger UI e introduce el token en `BearerAuth`; Swagger enviará `Authorization: Bearer <token>`.
- Añade `?token=<token>` a una petición de la API cuando uses el método de token por query.

No incluyas nunca tokens reales en ejemplos, documentación versionada ni logs. La página `/docs` y las especificaciones no evitan la autenticación de **Try it out**, y `GET /api/v1/skill.md` sigue protegido siempre que haya tokens configurados.

## Try it out y WebSocket

Para las operaciones REST, despliega una operación, pulsa **Try it out**, completa los parámetros o el cuerpo de la petición y pulsa **Execute**. Autoriza antes si el servidor exige un token.

Swagger UI no es un cliente WebSocket: no puede hacer el upgrade de `/api/v1/ws` ni invocar métodos JSON-RPC con **Try it out**. Usa un cliente WebSocket RFC 6455 para el handshake y el documento AsyncAPI enlazado para el protocolo. AsyncAPI describe las peticiones y respuestas JSON-RPC, las suscripciones, los topics y los eventos push; no son operaciones REST POST.

## Descargar `skill.md`

`GET /api/v1/skill.md` descarga el skill Markdown empaquetado para agentes y consumidores de la API. No acepta ruta ni nombre de archivo y no tiene equivalente WebSocket. Con la autenticación satisfecha, una respuesta correcta devuelve el Markdown UTF-8, con su entrada de ejemplo de instancia rellenada con el nombre de esta instancia y el host de la petición, y estas cabeceras:

```text
Content-Type: text/markdown; charset=utf-8
Content-Disposition: attachment; filename="skill.md"
Cache-Control: no-store
X-Content-Type-Options: nosniff
```

El paquete incluye `src/grafeno/server/static/skill.md`, por lo que una descarga con autenticación correcta devuelve `200`; un `404` con `{"error":"skill.md is not available"}` indica que el recurso no está disponible en esa instalación. Este Agent Skill portable se instala como `grafeno-api/SKILL.md`. El skill exige una instancia configurada (nombre y URL, con una variable de entorno opcional para el token) antes de cualquier operación: consulta su sección "Instances". Guarda esa configuración fuera de `SKILL.md`, porque actualizar el skill reemplaza el archivo.

## Flujo para contribuidores

Los YAML ingleses son las fuentes estructurales y los catálogos españoles son obligatorios:

- `docs/api/openapi.yaml` y `docs/api/asyncapi.yaml`: fuentes en inglés.
- `docs/api/openapi.es-translations.yaml` y `docs/api/asyncapi.es-translations.yaml`: catálogos de traducción al español.
- `src/grafeno/server/static/docs/*.json`: artefactos generados en inglés y español que se sirven en runtime.

Tras cambiar cualquier fuente o catálogo, regenera y verifica los cuatro documentos JSON:

```bash
.venv/bin/python scripts/build_api_docs.py
.venv/bin/python scripts/build_api_docs.py --check
```

No edites solo los JSON generados: la siguiente build sobrescribiría el cambio y `--check` detectaría artefactos desactualizados. El contenido canónico del skill vive únicamente en `src/grafeno/server/static/skill.md`; no crees copias duplicadas.

Swagger UI está vendorizado en el paquete de GRAFENO. No necesita CDN, servicio externo de documentación ni credenciales de terceros en runtime.
