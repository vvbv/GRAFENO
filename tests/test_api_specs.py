"""Coverage, localization and freshness tests for the hand-maintained API specs."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

from grafeno.server import ws
from grafeno.server.rest import _ROUTES

ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "docs" / "api" / "openapi.yaml"
ASYNCAPI_PATH = ROOT / "docs" / "api" / "asyncapi.yaml"
GENERATED_DIR = ROOT / "src" / "grafeno" / "server" / "static" / "docs"
TRANSLATABLE = {"title", "summary", "description"}
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def _load(path: Path) -> dict:
    assert path.exists(), f"missing spec: {path}"
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict), f"spec is not a mapping: {path}"
    return data


def _load_builder():
    path = ROOT / "scripts" / "build_api_docs.py"
    module_spec = importlib.util.spec_from_file_location("build_api_docs", path)
    assert module_spec and module_spec.loader
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    return module


def _generated(family: str, language: str) -> dict:
    path = GENERATED_DIR / f"{family}.{language}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _walk(node: object, path: tuple[object, ...] = ()):
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, path + (index,))


def _resolve_pointer(document: dict, reference: str) -> object:
    assert reference.startswith("#/")
    current: object = document
    for raw_segment in reference[2:].split("/"):
        segment = raw_segment.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(segment)]
        else:
            assert isinstance(current, dict) and segment in current, reference
            current = current[segment]
    return current


def _url_without_language(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "lang"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _assert_localized_structure(english: object, spanish: object, path: tuple[object, ...] = ()) -> None:
    assert type(english) is type(spanish), path
    if isinstance(english, dict):
        assert english.keys() == spanish.keys(), path
        for key in english:
            _assert_localized_structure(english[key], spanish[key], path + (key,))
        return
    if isinstance(english, list):
        assert len(english) == len(spanish), path
        for index, (left, right) in enumerate(zip(english, spanish)):
            _assert_localized_structure(left, right, path + (index,))
        return
    if path and path[-1] in TRANSLATABLE:
        assert isinstance(spanish, str) and spanish.strip(), path
        return
    if len(path) >= 2 and path[-2:] == ("externalDocs", "url"):
        assert _url_without_language(english) == _url_without_language(spanish), path
        return
    assert english == spanish, path


def test_openapi_parse_and_version():
    spec = _load(OPENAPI_PATH)
    assert str(spec.get("openapi")).startswith("3.")
    assert spec["info"]["title"]
    assert spec["info"]["version"]


def test_openapi_covers_every_rest_route():
    spec = _load(OPENAPI_PATH)
    paths = spec.get("paths") or {}
    missing = []
    for route in _ROUTES:
        method = route.method.lower()
        # The stored regex matches literal template strings because
        # `{task_id}` placeholders contain no slashes.
        matched = [p for p in paths if route.pattern.fullmatch(p)]
        if not matched or method not in paths[matched[0]]:
            missing.append((route.method, route.pattern.pattern))
    assert not missing, f"REST routes not documented: {missing}"


def test_openapi_auth_is_optional():
    spec = _load(OPENAPI_PATH)
    security = spec.get("security") or []
    flattened = {key for item in security for key in item}
    assert flattened == {"BearerAuth", "QueryToken"}
    assert any(item == {} for item in security), "missing anonymous (no-auth) entry"


def test_asyncapi_parse_and_version():
    spec = _load(ASYNCAPI_PATH)
    assert str(spec.get("asyncapi")).startswith("3.")
    assert spec["info"]["title"]
    assert spec["info"]["version"]


def test_asyncapi_covers_every_ws_method():
    spec = _load(ASYNCAPI_PATH)
    enum = spec["components"]["schemas"]["MethodEnum"]["enum"]
    missing = sorted(set(ws.METHODS) - set(enum))
    assert not missing, f"WS methods not documented: {missing}"


def test_asyncapi_documents_task_changed_event():
    spec = _load(ASYNCAPI_PATH)
    messages = spec["components"]["messages"]
    event = messages["TaskChanged"]["payload"]["properties"]["event"]
    assert event.get("const") == "task.changed"


def test_generated_specs_are_packaged_and_declare_versions() -> None:
    root = resources.files("grafeno.server").joinpath("static/docs")
    for family, field, version in (
        ("openapi", "openapi", "3.1"),
        ("asyncapi", "asyncapi", "3.0"),
    ):
        for language in ("en", "es"):
            name = f"{family}.{language}.json"
            packaged = json.loads(root.joinpath(name).read_text(encoding="utf-8"))
            disk = _generated(family, language)
            assert packaged == disk
            assert str(disk[field]).startswith(version)
            assert disk["info"]["title"]
            assert disk["info"]["version"]


def test_generated_specs_are_fresh() -> None:
    subprocess.run(
        [sys.executable, "scripts/build_api_docs.py", "--check"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_freshness_checker_detects_modified_output(tmp_path) -> None:
    builder = _load_builder()
    documents = builder.build_documents(ROOT)
    temporary = {tmp_path / path.name: document for path, document in documents.items()}
    builder.write_documents(temporary)
    modified_path = tmp_path / "openapi.es.json"
    modified = json.loads(modified_path.read_text(encoding="utf-8"))
    modified["info"]["title"] = "Stale output"
    modified_path.write_text(json.dumps(modified), encoding="utf-8")
    assert modified_path in builder.check_documents(temporary)


def test_english_and_spanish_specs_differ_only_in_translations() -> None:
    for family in ("openapi", "asyncapi"):
        english = _generated(family, "en")
        spanish = _generated(family, "es")
        _assert_localized_structure(english, spanish)
        for language, document in (("en", english), ("es", spanish)):
            for path, node in _walk(document):
                if len(path) >= 2 and path[-2:] == ("externalDocs", "url"):
                    query = dict(parse_qsl(urlsplit(node).query))
                    assert query.get("lang") == language, (family, path)


def test_spanish_localization_covers_nested_spec_sections() -> None:
    english = _generated("openapi", "en")
    spanish = _generated("openapi", "es")
    status_en = english["paths"]["/api/v1/status"]["get"]
    status_es = spanish["paths"]["/api/v1/status"]["get"]
    assert status_es["summary"] == "Metadatos del servidor (versión, tiempo de actividad, pid y ruta WS)."
    assert status_es["summary"] != status_en["summary"]
    assert status_es["responses"]["200"]["description"] == "Metadatos del servidor."
    assert status_es["responses"]["200"]["description"] != status_en["responses"]["200"]["description"]

    parameter_en = english["paths"]["/api/v1/ws"]["get"]["parameters"][0]
    parameter_es = spanish["paths"]["/api/v1/ws"]["get"]["parameters"][0]
    assert parameter_es["description"] == "Debe ser `websocket` (sin distinguir mayúsculas de minúsculas)."
    assert parameter_es["description"] != parameter_en["description"]

    property_en = english["components"]["schemas"]["CliSessionSummary"]["properties"]["cli"]
    property_es = spanish["components"]["schemas"]["CliSessionSummary"]["properties"]["cli"]
    assert property_es["description"] == (
        "CLI de agente de la sesión (uno de opencode, kimi, codex, claude, cursor, minimax). "
        "Se conserva tal como está guardado aunque ese CLI ya no esté instalado."
    )
    assert property_es["description"] != property_en["description"]

    tag_en = next(tag for tag in english["tags"] if tag["name"] == "documentation")
    tag_es = next(tag for tag in spanish["tags"] if tag["name"] == "documentation")
    assert tag_es["description"] == "Recursos de documentación empaquetados para consumidores de la API y agentes."
    assert tag_es["description"] != tag_en["description"]

    for path, node in _walk(spanish):
        if path and path[-1] in TRANSLATABLE and isinstance(node, str):
            assert node.strip(), path


def test_internal_references_resolve_in_both_languages() -> None:
    for family in ("openapi", "asyncapi"):
        for language in ("en", "es"):
            document = _generated(family, language)
            for path, node in _walk(document):
                if path and path[-1] == "$ref" and isinstance(node, str) and node.startswith("#/"):
                    _resolve_pointer(document, node)


def test_openapi_operation_contracts() -> None:
    document = _generated("openapi", "en")
    operation_ids = set()
    for path, path_item in document["paths"].items():
        shared_parameters = path_item.get("parameters", [])
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            operation_id = operation.get("operationId")
            assert operation_id and operation_id not in operation_ids, (path, method)
            operation_ids.add(operation_id)
            assert operation.get("summary"), (path, method)
            assert operation.get("tags"), (path, method)
            assert operation.get("responses"), (path, method)
            parameters = shared_parameters + operation.get("parameters", [])
            resolved = [
                _resolve_pointer(document, item["$ref"]) if "$ref" in item else item
                for item in parameters
            ]
            for placeholder in re.findall(r"\{([^}]+)\}", path):
                matches = [
                    item for item in resolved
                    if item.get("in") == "path" and item.get("name") == placeholder
                ]
                assert matches and all(item.get("required") is True for item in matches), (path, placeholder)

    skill = document["paths"]["/api/v1/skill.md"]["get"]
    assert skill["operationId"] == "getApiSkill"
    assert "text/markdown" in skill["responses"]["200"]["content"]
    assert "401" in skill["responses"] and "404" in skill["responses"]

    websocket_path = document["paths"]["/api/v1/ws"]
    assert set(websocket_path) == {"get"}
    websocket = websocket_path["get"]
    assert websocket["operationId"] == "openWebSocket"
    assert {"101", "400", "401"}.issubset(websocket["responses"])
    assert websocket["externalDocs"]["url"].startswith("/docs/asyncapi.json")
    header_names = {item["name"] for item in websocket["parameters"]}
    assert {"Upgrade", "Connection", "Sec-WebSocket-Key", "Sec-WebSocket-Version"} == header_names
    assert all(item.get("required") is True for item in websocket["parameters"])
    assert document["servers"][0]["url"] == "/"
