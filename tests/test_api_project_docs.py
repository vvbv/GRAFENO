"""Tests of the project documents API (README.md, AGENTS.md, GRAFENO.md)."""

from __future__ import annotations

import asyncio
import os

import pytest

from grafeno import config as config_module
from grafeno import models
from grafeno.config import ApiConfig, Config
from grafeno.mdnorm import normalize_markdown
from grafeno.server import projectdocs, ws
from grafeno.server.actions import ApiError
from grafeno.server.service import ServerService


class _Service:
    app = None


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project known through a task, with README.md and AGENTS.md (no GRAFENO.md)."""
    monkeypatch.chdir(tmp_path)
    root = (tmp_path / "code" / "Demo").resolve()
    root.mkdir(parents=True)
    (root / "README.md").write_text("# Demo\n\nIntro line.\n\n## Usage\n\nRun it.\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("# Agents\n\n- a\n\n- b\n", encoding="utf-8")
    models.save(models.Task.create("T", "d", str(root), Config()))
    return root


def _raises(status, fn, *args, **kwargs) -> ApiError:
    with pytest.raises(ApiError) as info:
        fn(*args, **kwargs)
    assert info.value.status == status
    return info.value


def test_index(project):
    result = projectdocs.list_docs(_Service(), project="demo")
    docs = result["docs"]
    assert [d["name"] for d in docs] == list(projectdocs.DOC_NAMES)
    for doc in docs[:2]:
        assert doc["exists"] is True and doc["status"] == "ok"
        for key in ("chars", "approx_tokens", "size", "modified", "outline"):
            assert key in doc
        assert "content" not in doc
    assert docs[2] == {"name": "GRAFENO.md", "exists": False, "status": "missing"}
    assert result["available"] == ["README.md", "AGENTS.md"]


def test_resolve_by_workdir(project):
    by_project = projectdocs.list_docs(_Service(), project="demo")
    assert projectdocs.list_docs(_Service(), workdir=str(project)) == by_project
    assert projectdocs.list_docs(_Service(), workdir=str(project) + "/") == by_project


def test_project_name_exact_case_insensitive(project):
    assert projectdocs.list_docs(_Service(), project="DEMO")["project"]["name"] == "Demo"
    err = _raises(404, projectdocs.list_docs, _Service(), project="Dem")
    assert "project not found" in err.message


def test_requires_workdir_or_project(project):
    _raises(400, projectdocs.list_docs, _Service())


def test_folder_outside_catalogue(project, tmp_path):
    err = _raises(404, projectdocs.list_docs, _Service(), workdir=str(tmp_path))
    assert "project not found" in err.message


def test_workspace_only_project(project, tmp_path):
    solo = tmp_path / "ws" / "Solo"
    solo.mkdir(parents=True)
    (solo / "README.md").write_text("# Solo\n", encoding="utf-8")
    cfg = config_module.load()
    cfg.workspaces = [str(tmp_path / "ws")]
    config_module.save(cfg)
    assert projectdocs.list_docs(_Service(), project="Solo")["available"] == ["README.md"]


def test_ambiguous_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for side in ("a", "b"):
        root = tmp_path / side / "Demo"
        root.mkdir(parents=True)
        models.save(models.Task.create(f"T-{side}", "d", str(root), Config()))
    err = _raises(409, projectdocs.list_docs, _Service(), project="Demo")
    assert str(tmp_path / "a" / "Demo") in err.message
    assert str(tmp_path / "b" / "Demo") in err.message


def test_remote_only_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    models.save(models.Task.create("R", "d", "/srv/Remote", Config(), remote="u@h:/srv/Remote"))
    err = _raises(409, projectdocs.list_docs, _Service(), project="Remote")
    assert "remote projects are not supported" in err.message


def test_remote_session_mode(project, monkeypatch):
    monkeypatch.setattr(projectdocs.remotesession, "active", lambda: True)
    _raises(409, projectdocs.list_docs, _Service(), project="Demo")


def test_missing_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    models.save(models.Task.create("T", "d", str(tmp_path / "gone"), Config()))
    err = _raises(404, projectdocs.list_docs, _Service(), project="gone")
    assert "project folder not found" in err.message


def test_canonical_name():
    for raw, expected in (
        ("readme", "README.md"), ("README.MD", "README.md"),
        ("Agents.md", "AGENTS.md"), ("grafeno", "GRAFENO.md"),
    ):
        assert projectdocs.canonical_name(raw) == expected
    for raw in ("../README.md", "setup.py", ""):
        _raises(400, projectdocs.canonical_name, raw)


def test_read_full(project):
    result = projectdocs.read_doc(_Service(), project="Demo", name="README.md")
    expected = normalize_markdown((project / "README.md").read_text(encoding="utf-8"))
    assert result["content"] == expected
    assert result["has_more"] is False
    assert result["next_offset"] is None
    assert result["offset"] == 0


def test_paging(project):
    body = "".join(f"line {i:03d} lorem ipsum\n" for i in range(400))
    (project / "README.md").write_text(body, encoding="utf-8")
    expected = normalize_markdown(body)
    pages, offset = [], 0
    while True:
        page = projectdocs.read_doc(
            _Service(), project="Demo", name="readme", offset=offset, max_chars=1000
        )
        pages.append(page["content"])
        assert len(page["content"]) <= 1000
        if not page["has_more"]:
            break
        assert page["content"].endswith("\n")
        offset = page["next_offset"]
    assert "".join(pages) == expected
    assert len(pages) > 1


def test_offset_and_size_bounds(project):
    total = projectdocs.read_doc(_Service(), project="Demo", name="readme")["total_chars"]
    end = projectdocs.read_doc(_Service(), project="Demo", name="readme", offset=total)
    assert end["content"] == "" and end["has_more"] is False
    _raises(400, projectdocs.read_doc, _Service(), project="Demo", name="readme", offset=total + 1)
    _raises(400, projectdocs.read_doc, _Service(), project="Demo", name="readme", offset=-1)
    _raises(400, projectdocs.read_doc, _Service(), project="Demo", name="readme", max_chars=0)
    _raises(
        400, projectdocs.read_doc, _Service(), project="Demo", name="readme",
        max_chars=projectdocs.DOC_MAX_CHARS + 1,
    )


def test_missing_document(project):
    err = _raises(404, projectdocs.read_doc, _Service(), project="Demo", name="GRAFENO.md")
    assert "document not found" in err.message


def test_case_insensitive_file(project):
    (project / "README.md").rename(project / "Readme.md")
    if (project / "README.md").exists():
        pytest.skip("case-insensitive filesystem")
    docs = projectdocs.list_docs(_Service(), project="Demo")["docs"]
    assert docs[0]["status"] == "ok" and docs[0]["file"] == "Readme.md"
    assert projectdocs.read_doc(_Service(), project="Demo", name="readme")["content"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlink_escaping_project(project, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("secret\n", encoding="utf-8")
    os.symlink(outside, project / "GRAFENO.md")
    doc = projectdocs.list_docs(_Service(), project="Demo")["docs"][2]
    assert doc["status"] == "blocked" and doc["exists"] is False
    _raises(403, projectdocs.read_doc, _Service(), project="Demo", name="GRAFENO.md")


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlink_inside_project(project):
    (project / "docs").mkdir()
    (project / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    os.symlink(project / "docs" / "guide.md", project / "GRAFENO.md")
    assert projectdocs.list_docs(_Service(), project="Demo")["docs"][2]["status"] == "ok"


def test_clipped_by_size(project, monkeypatch):
    monkeypatch.setattr(projectdocs, "DOC_MAX_BYTES", 100)
    (project / "README.md").write_text("x" * 250 + "\n" + "y" * 250 + "\n", encoding="utf-8")
    result = projectdocs.read_doc(_Service(), project="Demo", name="readme")
    assert result["clipped"] is True
    assert result["total_chars"] <= 101  # normalization appends a final newline


def test_invalid_utf8_and_bom(project):
    (project / "README.md").write_bytes(b"# T\n\xff\xfe ok\n")
    assert projectdocs.read_doc(_Service(), project="Demo", name="readme")["content"]
    (project / "README.md").write_text("\ufeff# T\n", encoding="utf-8")
    content = projectdocs.read_doc(_Service(), project="Demo", name="readme")["content"]
    assert content == "# T\n"


def test_outline_offset_reads_section(project):
    docs = projectdocs.list_docs(_Service(), project="Demo")["docs"]
    entry = next(e for e in docs[0]["outline"] if e["title"] == "Usage")
    page = projectdocs.read_doc(
        _Service(), project="Demo", name="readme", offset=entry["offset"]
    )
    assert page["content"].startswith("## Usage")


def test_ws_handlers(project):
    index = asyncio.run(ws.METHODS["projects.docs"](_Service(), {"project": "Demo"}, None))
    assert len(index["docs"]) == 3
    page = asyncio.run(ws.METHODS["projects.doc"](
        _Service(), {"project": "Demo", "name": "readme", "offset": "0", "max_chars": 5}, None
    ))
    assert len(page["content"]) <= 5
    with pytest.raises(ApiError) as info:
        asyncio.run(ws.METHODS["projects.doc"](
            _Service(), {"project": "Demo", "name": "readme", "max_chars": "x"}, None
        ))
    assert info.value.status == 400


async def _start_service() -> tuple[ServerService, asyncio.Task]:
    service = ServerService(ApiConfig(enabled=True, host="127.0.0.1", port=0, tokens="t1"), app=None)
    task = asyncio.create_task(service.run())
    for _ in range(200):
        if service.server is not None:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("server did not bind in time")
    return service, task


def _stop(service: ServerService, task: asyncio.Task) -> None:
    if service.http is not None:
        service.http.close()
    if not task.done():
        task.cancel()


async def _get(service: ServerService, path: str, token: bool = True) -> tuple[int, dict]:
    import json

    auth = "Authorization: Bearer t1\r\n" if token else ""
    raw = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{auth}\r\n".encode("ascii")
    reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
    try:
        writer.write(raw)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
        length = 0
        for line in lines[1:]:
            key, _, value = line.partition(":")
            if key.strip().lower() == "content-length":
                length = int(value.strip())
        body = await asyncio.wait_for(reader.readexactly(length), timeout=5.0) if length else b""
        return status, json.loads(body) if body else {}
    finally:
        writer.close()


def test_rest_end_to_end(project):
    async def scenario():
        service, task = await _start_service()
        try:
            status, payload = await _get(service, "/api/v1/projects/docs?project=Demo")
            assert status == 200 and len(payload["docs"]) == 3
            status, payload = await _get(
                service, "/api/v1/projects/doc?project=Demo&name=readme&max_chars=5"
            )
            assert status == 200 and payload["has_more"] is True
            status, payload = await _get(service, "/api/v1/projects/doc?project=Demo&name=GRAFENO.md")
            assert status == 404 and payload == {"error": "document not found: GRAFENO.md"}
            status, _ = await _get(service, "/api/v1/projects/doc?project=Demo&name=x&offset=abc")
            assert status == 400
            status, _ = await _get(service, "/api/v1/projects/docs?project=Demo", token=False)
            assert status == 401
        finally:
            _stop(service, task)

    asyncio.run(scenario())
