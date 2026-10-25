"""Tests of the project file explorer API (listing, metadata, text pages, raw bytes)."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from urllib.parse import quote

import pytest

from grafeno import models
from grafeno.config import ApiConfig, Config
from grafeno.server import projectfiles, ws
from grafeno.server.actions import ApiError
from grafeno.server.service import ServerService


class _Service:
    app = None


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project known through a task, with text, binary, hidden and .git entries."""
    monkeypatch.chdir(tmp_path)
    root = (tmp_path / "code" / "Demo").resolve()
    (root / "src" / "pkg").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")
    (root / "main.py").write_text("print('a')\nprint('b')\nprint('c')\n", encoding="utf-8")
    (root / "notes.txt").write_bytes(b"one\r\ntwo\r\n")
    (root / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4)
    (root / "doc.pdf").write_bytes(b"%PDF-1.4\n" + b"\0" * 100)
    (root / "page.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    (root / "src" / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (root / "data.bin").write_bytes(bytes(range(256)))
    (root / "ñandú.txt").write_text("hola\n", encoding="utf-8")
    models.save(models.Task.create("T", "d", str(root), Config()))
    return root


def _raises(status, fn, *args, **kwargs) -> ApiError:
    with pytest.raises(ApiError) as info:
        fn(*args, **kwargs)
    assert info.value.status == status
    return info.value


def test_list_root(project):
    result = projectfiles.list_dir(_Service(), project="demo")
    names = [e["name"] for e in result["entries"]]
    assert names[0] == "src"
    files = names[1:]
    assert files == sorted(files, key=lambda n: (n.casefold(), n))
    assert ".env" not in names and ".git" not in names
    assert result["total"] == len(names) and result["has_more"] is False
    assert result["parent"] is None and result["path"] == ""


def test_list_hidden(project):
    result = projectfiles.list_dir(_Service(), project="demo", hidden=True)
    by_name = {e["name"]: e for e in result["entries"]}
    assert by_name[".env"]["hidden"] is True
    assert ".git" not in by_name


def test_list_subdir_and_parent(project):
    result = projectfiles.list_dir(_Service(), project="demo", path="src/pkg")
    assert result["parent"] == "src"
    assert result["entries"][0]["path"] == "src/pkg/mod.py"
    for variant in ("src/", "./src"):
        assert projectfiles.list_dir(_Service(), project="demo", path=variant)["path"] == "src"


def test_list_pagination(project):
    result = projectfiles.list_dir(_Service(), project="demo", limit=2, offset=0)
    assert len(result["entries"]) == 2 and result["has_more"] is True
    _raises(400, projectfiles.list_dir, _Service(), project="demo", limit=0)
    _raises(400, projectfiles.list_dir, _Service(), project="demo", limit=projectfiles.LIST_MAX_LIMIT + 1)
    _raises(400, projectfiles.list_dir, _Service(), project="demo", offset=-1)


def test_list_truncated(project, monkeypatch):
    monkeypatch.setattr(projectfiles, "SCAN_MAX_ENTRIES", 3)
    assert projectfiles.list_dir(_Service(), project="demo")["truncated"] is True


def test_entry_fields(project):
    by_name = {e["name"]: e for e in projectfiles.list_dir(_Service(), project="demo")["entries"]}
    png = by_name["img.png"]
    assert png["type"] == "file" and png["size"] == (project / "img.png").stat().st_size
    assert png["mime"] == "image/png" and png["preview"] == "image" and png["kind"] == "image"
    assert by_name["main.py"]["preview"] == "text"
    assert by_name["data.bin"]["preview"] == "unknown"
    assert by_name["doc.pdf"]["preview"] == "pdf"


def test_path_traversal(project):
    for bad in ("..", "../x", "src/../..", "/etc", "\0"):
        _raises(400, projectfiles.list_dir, _Service(), project="demo", path=bad)
    for blocked in (".git", ".git/config", ".GIT", ".Git/config", "src/.GIT"):
        _raises(403, projectfiles.list_dir, _Service(), project="demo", path=blocked)
    _raises(404, projectfiles.list_dir, _Service(), project="demo", path="missing")


def test_blocked_name_is_case_insensitive(project):
    (project / "src" / ".Git").mkdir()
    names = [e["name"] for e in projectfiles.list_dir(_Service(), project="demo", path="src", hidden=True)["entries"]]
    assert ".Git" not in names
    assert projectfiles.looks_like_text(b"") is True


def test_symlink_escape(project, tmp_path):
    os.symlink(tmp_path, project / "out")
    os.symlink(project / ".git", project / "g")
    os.symlink(project / "src", project / "inside")
    by_name = {e["name"]: e for e in projectfiles.list_dir(_Service(), project="demo")["entries"]}
    assert by_name["out"]["type"] == "blocked"
    assert by_name["g"]["type"] == "blocked"
    assert by_name["inside"]["type"] == "dir" and by_name["inside"]["symlink"] is True
    _raises(403, projectfiles.file_info, _Service(), project="demo", path="out")
    _raises(403, projectfiles.file_info, _Service(), project="demo", path="g")
    assert projectfiles.list_dir(_Service(), project="demo", path="inside/pkg")["entries"]


def test_broken_symlink(project):
    os.symlink(project / "nope", project / "dead")
    by_name = {e["name"]: e for e in projectfiles.list_dir(_Service(), project="demo")["entries"]}
    assert by_name["dead"]["type"] == "broken"


def test_not_a_directory_and_not_a_file(project):
    _raises(400, projectfiles.list_dir, _Service(), project="demo", path="main.py")
    _raises(400, projectfiles.read_text, _Service(), project="demo", path="src")


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_permission_denied(project):
    target = project / "main.py"
    target.chmod(0)
    try:
        _raises(403, projectfiles.file_info, _Service(), project="demo", path="main.py")
        by_name = {e["name"]: e for e in projectfiles.list_dir(_Service(), project="demo")["entries"]}
        assert by_name["main.py"]["readable"] is False
    finally:
        target.chmod(0o644)


def test_file_info_text_and_binary(project):
    info = projectfiles.file_info(_Service(), project="demo", path="main.py")
    assert info["text"] is True and info["mime"] == "text/plain; charset=utf-8"
    assert info["urls"]["text"] is not None and "path=main.py" in info["urls"]["raw"]
    binary = projectfiles.file_info(_Service(), project="demo", path="data.bin")
    assert binary["text"] is False and binary["mime"] == "application/octet-stream"
    assert binary["urls"]["text"] is None
    assert projectfiles.file_info(_Service(), project="demo", path="LICENSE")["text"] is True
    assert projectfiles.file_info(_Service(), project="demo", path="src")["type"] == "dir"


def test_unsafe_types_never_served_as_active_content(project):
    text_type = "text/plain; charset=utf-8"
    assert projectfiles.file_info(_Service(), project="demo", path="page.html")["mime"] == text_type
    assert projectfiles.mime_for("a.svg", True) == text_type
    assert projectfiles.mime_for("a.js", True) == text_type


def test_read_text_paging(project):
    first = projectfiles.read_text(_Service(), project="demo", path="main.py", max_lines=2)
    assert first["content"] == "print('a')\nprint('b')"
    assert first["next_line"] == 3 and first["has_more"] is True
    last = projectfiles.read_text(_Service(), project="demo", path="main.py", line=3)
    assert last["content"] == "print('c')" and last["has_more"] is False and last["next_line"] is None
    _raises(400, projectfiles.read_text, _Service(), project="demo", path="main.py", line=5)
    _raises(400, projectfiles.read_text, _Service(), project="demo", path="main.py", max_lines=0)


def test_read_text_crlf_and_empty(project):
    crlf = projectfiles.read_text(_Service(), project="demo", path="notes.txt")
    assert crlf["newline"] == "crlf" and crlf["content"] == "one\ntwo"
    (project / "empty.txt").write_text("")
    empty = projectfiles.read_text(_Service(), project="demo", path="empty.txt")
    assert empty["total_lines"] == 0 and empty["content"] == "" and empty["has_more"] is False


def test_read_text_binary_rejected(project):
    err = _raises(415, projectfiles.read_text, _Service(), project="demo", path="img.png")
    assert err.code == "binary_file"


def test_read_text_clipped(project, monkeypatch):
    monkeypatch.setattr(projectfiles, "TEXT_MAX_BYTES", 10)
    (project / "long.txt").write_text("a" * 40, encoding="utf-8")
    result = projectfiles.read_text(_Service(), project="demo", path="long.txt")
    assert result["clipped"] is True and result["decode_errors"] is False
    (project / "multi.txt").write_text("aaaaaaaaañ", encoding="utf-8")  # n-tilde straddles byte 10
    result = projectfiles.read_text(_Service(), project="demo", path="multi.txt")
    assert result["clipped"] is True and result["decode_errors"] is False


def test_read_text_invalid_utf8(project):
    (project / "latin.txt").write_bytes("caf\xe9\n".encode("latin-1"))
    result = projectfiles.read_text(_Service(), project="demo", path="latin.txt")
    assert result["decode_errors"] is True and "�" in result["content"]


@pytest.mark.parametrize(
    "header,expected",
    [
        ("", None), ("bytes=0-9", (0, 9)), ("bytes=90-", (90, 99)), ("bytes=-10", (90, 99)),
        ("bytes=-500", (0, 99)), ("bytes=50-1000", (50, 99)), ("bytes=9-0", None),
        ("bytes=0-1,5-6", None), ("items=0-1", None), ("Bytes=0-9", (0, 9)),
        ("bytes=0-" + "9" * 5000, None),
    ],
)
def test_parse_range(header, expected):
    assert projectfiles.parse_range(header, 100) == expected


@pytest.mark.parametrize("header,size", [("bytes=100-", 100), ("bytes=-0", 100), ("bytes=0-1", 0)])
def test_parse_range_unsatisfiable(header, size):
    with pytest.raises(projectfiles.RangeNotSatisfiable):
        projectfiles.parse_range(header, size)


def test_content_disposition():
    value = projectfiles.content_disposition("ñandú.txt", False)
    assert value.startswith("inline;")
    assert 'filename="_and_.txt"' in value
    assert "filename*=UTF-8''%C3%B1and%C3%BA.txt" in value
    value.encode("ascii")
    assert projectfiles.content_disposition("a.txt", True).startswith("attachment;")


def test_raw_response_headers(project):
    f = projectfiles.open_file(_Service(), project="demo", path="img.png")
    full = projectfiles.raw_response(f)
    assert full.status == 200
    for key in ("Accept-Ranges", "ETag", "Last-Modified"):
        assert key in full.headers
    assert full.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in full.headers["Content-Security-Policy"]
    part = projectfiles.raw_response(f, range_header="bytes=0-9")
    assert part.status == 206 and part.file_offset == 0 and part.file_length == 10
    assert part.headers["Content-Range"] == f"bytes 0-9/{f.size}"
    stale = projectfiles.raw_response(f, range_header="bytes=0-9", if_range='"other"')
    assert stale.status == 200
    bad = projectfiles.raw_response(f, range_header=f"bytes={f.size}-")
    assert bad.status == 416 and bad.headers["Content-Range"] == f"bytes */{f.size}"


def test_remote_session_and_unknown_project(project, monkeypatch):
    err = _raises(404, projectfiles.list_dir, _Service(), project="nope")
    assert "project not found" in err.message
    monkeypatch.setattr("grafeno.remotesession.active", lambda: True)
    err = _raises(409, projectfiles.list_dir, _Service(), project="Demo")
    assert "project files" in err.message


def test_ws_handlers(project, monkeypatch):
    def call(method, params):
        return asyncio.run(ws.METHODS[method](_Service(), params, None))

    assert call("projects.files", {"project": "Demo"})["entries"]
    assert call("projects.file", {"project": "Demo", "path": "main.py"})["text"] is True
    page = call("projects.file_text", {"project": "Demo", "path": "main.py", "max_lines": "1"})
    assert page["content"] == "print('a')"
    raw = call("projects.file_raw", {"project": "Demo", "path": "img.png"})
    assert base64.b64decode(raw["data"]) == (project / "img.png").read_bytes()
    with pytest.raises(ApiError) as info:
        call("projects.files", {"project": "Demo", "limit": "x"})
    assert info.value.status == 400
    monkeypatch.setattr(ws, "WS_MEDIA_MAX_BYTES", 10)
    with pytest.raises(ApiError) as info:
        call("projects.file_raw", {"project": "Demo", "path": "img.png"})
    assert info.value.status == 413


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


async def _raw(
    service: ServerService, path: str, headers: str = "", token: bool = True
) -> tuple[int, dict[str, str], bytes, str]:
    auth = "Authorization: Bearer t1\r\n" if token else ""
    request = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{auth}{headers}\r\n".encode("ascii")
    reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
    try:
        writer.write(request)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
        lines = head.decode("iso-8859-1").split("\r\n")
        resp_headers = {}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            if key:
                resp_headers[key.strip().lower()] = value.strip()
        length = int(resp_headers.get("content-length", "0"))
        body = await asyncio.wait_for(reader.readexactly(length), timeout=5.0) if length else b""
        return int(lines[0].split(" ", 2)[1]), resp_headers, body, lines[0]
    finally:
        writer.close()


def test_rest_end_to_end(project):
    data = (project / "img.png").read_bytes()
    size = len(data)

    async def scenario():
        service, task = await _start_service()
        try:
            base = "/api/v1/projects/"
            status, _, body, _ = await _raw(service, base + "files?project=Demo")
            assert status == 200 and json.loads(body)["entries"]
            status, headers, body, _ = await _raw(service, base + "file/raw?project=Demo&path=img.png")
            assert status == 200 and body == data
            assert headers["content-type"] == "image/png" and int(headers["content-length"]) == size
            status, headers, body, _ = await _raw(
                service, base + "file/raw?project=Demo&path=img.png", "Range: bytes=8-15\r\n"
            )
            assert status == 206 and body == data[8:16]
            assert headers["content-range"] == f"bytes 8-15/{size}"
            status, _, _, _ = await _raw(
                service, base + "file/raw?project=Demo&path=img.png", "Range: bytes=99999-\r\n"
            )
            assert status == 416
            name = quote("ñandú.txt")
            status, headers, _, _ = await _raw(
                service, base + f"file/raw?project=Demo&path={name}&download=1"
            )
            assert status == 200 and headers["content-disposition"].startswith("attachment;")
            status, _, body, _ = await _raw(service, base + "file/text?project=Demo&path=img.png")
            assert status == 415 and json.loads(body)["code"] == "binary_file"
            status, _, _, _ = await _raw(service, base + "files?project=Demo&path=..")
            assert status == 400
            status, _, _, line = await _raw(service, base + "files?project=Demo&path=.git")
            assert status == 403 and line.endswith("Forbidden")
            status, _, _, _ = await _raw(service, base + "files?project=Demo", token=False)
            assert status == 401
        finally:
            _stop(service, task)

    asyncio.run(scenario())
