"""Build deterministic English and Spanish JSON API specifications."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

TRANSLATABLE_KEYS = frozenset({"title", "summary", "description"})
FAMILIES = ("openapi", "asyncapi")
LANGUAGES = ("en", "es")


class CatalogError(ValueError):
    """Report an invalid or outdated translation catalog."""


def format_path(path: tuple[object, ...]) -> str:
    """Format a recursive document path for diagnostics."""
    result = "$"
    for part in path:
        result += f"[{part}]" if isinstance(part, int) else f".{part}"
    return result


def excerpt(text: str, limit: int = 120) -> str:
    """Return a compact source excerpt for diagnostics."""
    compact = " ".join(text.split())
    if len(compact) > limit:
        return compact[: limit - 3] + "..."
    return compact


def iter_translatable(
    node: object, path: tuple[object, ...] = ()
) -> Iterator[tuple[tuple[object, ...], str]]:
    """Yield paths and exact strings stored in human-text specification keys."""
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = path + (key,)
            if key in TRANSLATABLE_KEYS and isinstance(value, str):
                yield child_path, value
            else:
                yield from iter_translatable(value, child_path)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from iter_translatable(value, path + (index,))


def load_translation_catalog(path: Path) -> dict[str, str]:
    """Load and strictly validate a flat source-to-translation catalog."""
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, list):
        raise CatalogError(f"{path}: catalog must be a list of mappings")

    catalog: dict[str, str] = {}
    for index, entry in enumerate(data):
        location = f"{path}: entry {index + 1}"
        if not isinstance(entry, dict) or set(entry) != {"source", "translation"}:
            raise CatalogError(
                f"{location}: each mapping must contain exactly source and translation"
            )
        source = entry["source"]
        translation = entry["translation"]
        if not isinstance(source, str) or not source.strip():
            raise CatalogError(f"{location}: source must be a non-empty string")
        if not isinstance(translation, str) or not translation.strip():
            raise CatalogError(f"{location}: translation must be a non-empty string")
        if source in catalog:
            raise CatalogError(f"{location}: duplicate source: {excerpt(source)!r}")
        catalog[source] = translation
    return catalog


def localize(
    node: object, catalog: dict[str, str], path: tuple[object, ...] = ()
) -> object:
    """Copy a specification while replacing only translatable string values."""
    if isinstance(node, dict):
        localized: dict[object, object] = {}
        for key, value in node.items():
            child_path = path + (key,)
            if key in TRANSLATABLE_KEYS and isinstance(value, str):
                try:
                    localized[key] = catalog[value]
                except KeyError as exc:
                    raise CatalogError(
                        f"Missing Spanish translation at {format_path(child_path)}: "
                        f"{excerpt(value)!r}"
                    ) from exc
            else:
                localized[key] = localize(value, catalog, child_path)
        return localized
    if isinstance(node, list):
        return [localize(value, catalog, path + (index,)) for index, value in enumerate(node)]
    return copy.deepcopy(node)


def localize_external_docs(node: object, language: str) -> None:
    """Add the selected language to internal documentation links in place."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "externalDocs" and isinstance(value, dict):
                url = value.get("url")
                if isinstance(url, str) and url.startswith("/docs"):
                    parts = urlsplit(url)
                    query = [
                        (name, item)
                        for name, item in parse_qsl(parts.query, keep_blank_values=True)
                        if name != "lang"
                    ]
                    query.append(("lang", language))
                    value["url"] = urlunsplit(
                        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
                    )
            else:
                localize_external_docs(value, language)
    elif isinstance(node, list):
        for value in node:
            localize_external_docs(value, language)


def validate_catalog_coverage(
    family: str,
    source: object,
    catalog: dict[str, str],
    catalog_path: Path,
) -> None:
    """Reject both missing source texts and stale catalog entries."""
    occurrences: dict[str, list[tuple[object, ...]]] = {}
    for path, text in iter_translatable(source):
        occurrences.setdefault(text, []).append(path)

    problems: list[str] = []
    for text, paths in occurrences.items():
        if text not in catalog:
            locations = ", ".join(format_path(path) for path in paths[:3])
            if len(paths) > 3:
                locations += f" (and {len(paths) - 3} more)"
            problems.append(f"missing at {locations}: {excerpt(text)!r}")
    for text in catalog:
        if text not in occurrences:
            problems.append(f"stale entry in {catalog_path}: {excerpt(text)!r}")
    if problems:
        details = "\n".join(f"  - {problem}" for problem in problems)
        raise CatalogError(f"{family} Spanish catalog is out of date:\n{details}")


def build_documents(root: Path) -> dict[Path, dict]:
    """Build all localized documents from English sources and Spanish catalogs."""
    documents: dict[Path, dict] = {}
    for family in FAMILIES:
        source_path = root / "docs" / "api" / f"{family}.yaml"
        catalog_path = root / "docs" / "api" / f"{family}.es-translations.yaml"
        with source_path.open(encoding="utf-8") as handle:
            source = yaml.safe_load(handle)
        if not isinstance(source, dict):
            raise CatalogError(f"{source_path}: specification must be a mapping")
        catalog = load_translation_catalog(catalog_path)
        validate_catalog_coverage(family, source, catalog, catalog_path)

        for language in LANGUAGES:
            document = copy.deepcopy(source) if language == "en" else localize(source, catalog)
            localize_external_docs(document, language)
            output_path = (
                root
                / "src"
                / "grafeno"
                / "server"
                / "static"
                / "docs"
                / f"{family}.{language}.json"
            )
            documents[output_path] = document
    return documents


def serialize_document(document: dict) -> str:
    """Serialize a document deterministically as readable UTF-8 JSON."""
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def write_documents(documents: dict[Path, dict]) -> None:
    """Write generated JSON documents, creating their output directory."""
    for path, document in documents.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(serialize_document(document), encoding="utf-8")


def check_documents(documents: dict[Path, dict]) -> list[Path]:
    """Return generated files that are absent or semantically out of date."""
    stale: list[Path] = []
    for path, expected in documents.items():
        try:
            with path.open(encoding="utf-8") as handle:
                actual = json.load(handle)
        except (OSError, json.JSONDecodeError):
            stale.append(path)
            continue
        if actual != expected:
            stale.append(path)
    return stale


def main() -> int:
    """Generate the JSON files or check them without modifying the workspace."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate catalogs and generated JSON without writing files",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        documents = build_documents(root)
    except (CatalogError, OSError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.check:
        stale = check_documents(documents)
        if stale:
            for path in stale:
                print(f"out of date: {path.relative_to(root)}", file=sys.stderr)
            return 1
        print("API documentation catalogs and generated JSON are up to date.")
        return 0

    write_documents(documents)
    for path in documents:
        print(f"generated: {path.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
