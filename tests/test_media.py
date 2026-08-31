"""Tests for the clipboard image module (media.py)."""

from __future__ import annotations

from pathlib import Path

from grafeno import media, paths


def test_next_media_name_first_in_empty_dir(tmp_path):
    assert media.next_media_name(tmp_path) == "media-01.png"


def test_next_media_name_skips_existing(tmp_path):
    (tmp_path / "media-01.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "media-02.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    assert media.next_media_name(tmp_path) == "media-03.png"


def test_save_image_writes_and_list_media_returns_it(tmp_path):
    # Ensure GRAFENO_HOME isolation: just call save_image with any id.
    task_id = "t1"
    data = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    path = media.save_image(task_id, data)
    assert path is not None
    assert path.exists()
    assert path.read_bytes() == data
    listed = media.list_media(task_id)
    assert listed == [path]
    # Sorted output.
    other = media.save_image(task_id, data)
    assert other is not None
    assert media.list_media(task_id) == sorted([path, other])


def test_list_media_unknown_task_returns_empty(tmp_path):
    assert media.list_media("no-such-task") == []


def test_save_pending_writes_all_pairs(tmp_path):
    task_id = "t-pending"
    data1 = b"\x89PNG\r\n\x1a\n" + b"a" * 8
    data2 = b"\x89PNG\r\n\x1a\n" + b"b" * 8
    written = media.save_pending(task_id, [("media-01.png", data1), ("media-02.png", data2)])
    assert [p.name for p in written] == ["media-01.png", "media-02.png"]
    listed = media.list_media(task_id)
    assert [p.name for p in listed] == ["media-01.png", "media-02.png"]
    assert listed[0].read_bytes() == data1
    assert listed[1].read_bytes() == data2


def test_read_clipboard_image_returns_none_without_command(monkeypatch):
    monkeypatch.setattr(media, "_clipboard_command", lambda: None)
    assert media.read_clipboard_image() is None


def test_read_clipboard_image_returns_png_bytes(monkeypatch):
    png_bytes = b"\x89PNG\r\n\x1a\nrest"
    monkeypatch.setattr(media, "_clipboard_command", lambda: ["fake"])
    monkeypatch.setattr(
        media.subprocess,
        "run",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": png_bytes})(),
    )
    assert media.read_clipboard_image() == png_bytes


def test_read_clipboard_image_returns_none_when_no_png_header(monkeypatch):
    monkeypatch.setattr(media, "_clipboard_command", lambda: ["fake"])
    monkeypatch.setattr(
        media.subprocess,
        "run",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": b"not a png"})(),
    )
    assert media.read_clipboard_image() is None


def test_read_clipboard_image_returns_none_on_subprocess_error(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(media, "_clipboard_command", lambda: ["fake"])

    def boom(*_a, **_kw):
        raise sp.TimeoutExpired(cmd=["fake"], timeout=5)

    monkeypatch.setattr(media.subprocess, "run", boom)
    assert media.read_clipboard_image() is None


def test_open_media_returns_false_without_executable(monkeypatch):
    monkeypatch.setattr(media.shutil, "which", lambda _name: None)
    assert media.open_media(Path("/tmp/x.png")) is False


def test_open_media_invokes_popen(monkeypatch):
    captured: dict[str, object] = {}

    class FakePopen:
        def __init__(self, argv, **_kw):
            captured["argv"] = argv

    monkeypatch.setattr(media.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media.subprocess, "Popen", FakePopen)
    assert media.open_media(Path("/tmp/x.png")) is True
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[0] in {"open", "xdg-open"}
    assert argv[-1] == "/tmp/x.png"


def test_inline_preview_supported_returns_bool(monkeypatch):
    # Unsupported terminal -> False even with the package available.
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("KITTY_WINDOW_ID", raising=False)
    monkeypatch.setattr(media.importlib.util, "find_spec", lambda _name: object())
    assert media.inline_preview_supported() is False

    # Supported terminal without the package -> False.
    monkeypatch.setenv("TERM_PROGRAM", "WezTerm")
    monkeypatch.delenv("KITTY_WINDOW_ID", raising=False)
    monkeypatch.setattr(media.importlib.util, "find_spec", lambda _name: None)
    assert media.inline_preview_supported() is False

    # Supported terminal with the package -> True.
    monkeypatch.setattr(media.importlib.util, "find_spec", lambda _name: object())
    assert media.inline_preview_supported() is True


def test_paths_media_dir_creates_directory(tmp_path):
    """paths.media_dir creates the directory on first call."""
    task_id = "t-media-paths"
    target = paths.media_dir(task_id)
    assert target.exists()
    assert target.is_dir()
    assert target.name == "media"


def test_list_media_includes_jpg_and_jpeg(tmp_path):
    """list_media accepts jpg/jpeg besides png (Telegram photos are JPEG)."""
    task_id = "t-media-jpg"
    media.save_image(task_id, b"\x89PNG\r\n\x1a\nDATA")  # media-01.png
    media.save_attachment(task_id, "photo.jpg", b"JPEG")      # media-01.jpg
    media.save_attachment(task_id, "photo2.jpeg", b"JPEG2")   # media-01.jpeg
    media.save_attachment(task_id, "clip.mp4", b"MP4")        # not an image
    names = [p.name for p in media.list_media(task_id)]
    assert "media-01.png" in names
    assert "media-01.jpg" in names
    assert "media-01.jpeg" in names
    assert all(not n.endswith(".mp4") for n in names)


def test_save_attachment_keeps_extension_and_avoids_collisions(tmp_path):
    """save_attachment keeps a safe extension and picks free media-NN names."""
    task_id = "t-media-attach"
    first = media.save_attachment(task_id, "photo.jpg", b"J1")
    second = media.save_attachment(task_id, "photo.jpg", b"J2")
    weird = media.save_attachment(task_id, "evil.sh", b"X")
    assert first is not None and first.name == "media-01.jpg"
    assert second is not None and second.name == "media-02.jpg"
    assert weird is not None and weird.suffix == ".bin"
    assert first.read_bytes() == b"J1"


def test_save_attachment_unknown_task_still_works(tmp_path):
    """save_attachment creates the media dir on demand (task may be fresh)."""
    saved = media.save_attachment("t-media-fresh", "v.mp4", b"MP4")
    assert saved is not None
    assert saved.suffix == ".mp4"
