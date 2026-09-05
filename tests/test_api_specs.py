"""Coverage tests for the hand-maintained API specs under docs/api/."""

from __future__ import annotations

from pathlib import Path

import yaml

from grafeno.server import ws
from grafeno.server.rest import _ROUTES

ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "docs" / "api" / "openapi.yaml"
ASYNCAPI_PATH = ROOT / "docs" / "api" / "asyncapi.yaml"


def _load(path: Path) -> dict:
    assert path.exists(), f"missing spec: {path}"
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict), f"spec is not a mapping: {path}"
    return data


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


def test_openapi_has_both_auth_alternatives():
    spec = _load(OPENAPI_PATH)
    security = spec.get("security") or []
    flattened = {key for item in security for key in item}
    assert flattened == {"BearerAuth", "QueryToken"}


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
