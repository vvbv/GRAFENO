"""Tests for the clipboard image module (media.py)."""

from __future__ import annotations

from pathlib import Path

from grafeno import config as config_module
from grafeno import media, models, paths


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


def test_attach_files_images_and_bin(tmp_path):
    """Images get media tokens; other suffixes get absolute paths; header appended."""
    task = models.Task.create("Adjuntos", "", str(tmp_path), config_module.load())
    saved = media.attach_files(
        task,
        [("a.png", b"PNGBYTES"), ("b.sh", b"ZIPBYTES")],
        header="Attachments received via the API:",
    )
    assert len(saved) == 2
    directory = paths.task_dir(task.id) / "media"
    assert (directory / "media-01.png").exists()
    assert "media/media-01.png" in task.description
    assert "Attachments received via the API:" in task.description
    assert ".bin" in task.description  # suffix sanitized to .bin
    assert set(p.name for p in media.list_media(task.id)) == {"media-01.png"}


def test_attach_files_empty_list_is_noop(tmp_path):
    """No attachments means no saved files and an untouched description."""
    task = models.Task.create("Vacio", "base", str(tmp_path), config_module.load())
    assert media.attach_files(task, [], header="HEADER-X") == []
    assert "HEADER-X" not in task.description
    assert task.description == "base"


def test_save_attachment_keeps_audio_suffix(tmp_path):
    """Audio attachments keep their suffix instead of falling back to .bin."""
    from grafeno import media

    task_id = "20260101-000000-audio"
    saved = media.save_attachment(task_id, "voice.ogg", b"OGG")
    assert saved is not None and saved.suffix == ".ogg"
    assert saved.read_bytes() == b"OGG"
    assert media.is_audio_name("voice.ogg")
    assert media.is_audio_name("NOTE.WAV")
    assert not media.is_audio_name("photo.png")
    assert not media.is_audio_name("evil.sh")


def test_attachment_kind_classifies_office_media_and_other():
    assert media.attachment_kind("report.docx") == "document"
    assert media.attachment_kind("data.XLSX") == "spreadsheet"
    assert media.attachment_kind("clip.mov") == "video"
    assert media.attachment_kind("a.webp") == "image"
    assert media.attachment_kind("x.sh") == "other"


def test_save_attachment_keeps_office_suffix_and_records_manifest():
    task_id = "t-media-office"
    xlsx = media.save_attachment(task_id, "presupuesto.xlsx", b"XLSX", origin="api", cycle=2)
    docx = media.save_attachment(task_id, "informe.docx", b"DOCX", origin="api", cycle=2)
    assert xlsx is not None and xlsx.name == "media-01.xlsx"
    assert docx is not None and docx.name == "media-01.docx"
    entries = {a.file: a for a in media.list_attachments(task_id)}
    sheet = entries["media-01.xlsx"]
    assert sheet.name == "presupuesto.xlsx"
    assert sheet.kind == "spreadsheet"
    assert sheet.origin == "api"
    assert sheet.cycle == 2
    assert sheet.size == len(b"XLSX")
    assert entries["media-01.docx"].kind == "document"


def test_save_attachment_rejects_oversized(monkeypatch):
    monkeypatch.setattr(media, "MAX_ATTACHMENT_BYTES", 4)
    assert media.save_attachment("t-media-big", "a.xlsx", b"12345") is None
    assert media.list_attachments("t-media-big") == []


def test_list_attachments_includes_unrecorded_files_and_skips_hidden():
    task_id = "t-media-legacy"
    directory = paths.media_dir(task_id)
    (directory / "media-01.png").write_bytes(b"P")
    (directory / ".hidden").write_bytes(b"H")
    entries = media.list_attachments(task_id)
    assert [e.file for e in entries] == ["media-01.png"]
    assert entries[0].kind == "image"
    assert entries[0].name == "media-01.png"


def test_delete_attachment_removes_file_and_entry():
    task_id = "t-media-del"
    saved = media.save_attachment(task_id, "a.xlsx", b"X")
    assert saved is not None
    assert media.delete_attachment(task_id, saved.name) is True
    assert media.list_attachments(task_id) == []
    assert not paths.attachments_manifest_path(task_id).exists()
    assert media.delete_attachment(task_id, "../task.toml") is False


def test_display_name_strips_dirs_and_control_chars():
    assert media.display_name("C:\\x\\a\nb.docx") == "ab.docx"
    assert media.display_name("") == "attachment"


def test_format_size():
    assert media.format_size(512) == "512 B"
    assert media.format_size(2048) == "2.0 KB"
    assert media.format_size(3 * 1024 * 1024) == "3.0 MB"


def test_max_attachment_is_500_mib():
    assert media.MAX_ATTACHMENT_BYTES == 500 * 1024 * 1024


def test_base64_length_matches_stdlib():
    import base64

    for n in (0, 1, 2, 3, 4, 1000):
        assert media.base64_length(n) == len(base64.b64encode(b"x" * n))


def test_save_attachment_file_copies_without_reading_whole_file(tmp_path, monkeypatch):
    import pathlib

    src = tmp_path / "big.zip"
    with open(src, "wb") as f:
        f.truncate(60 * 1024 * 1024)  # above the old 48 MiB cap

    def boom(self):
        raise AssertionError("read_bytes must not be used")

    monkeypatch.setattr(pathlib.Path, "read_bytes", boom)
    saved = media.save_attachment_file("t-media-big", "big.zip", src, origin="tui")
    assert saved is not None and saved.name == "media-01.zip"
    assert saved.stat().st_size == 60 * 1024 * 1024
    assert src.exists()
    entry = media.list_attachments("t-media-big")[0]
    assert entry.kind == "archive"
    assert entry.size == 60 * 1024 * 1024


def test_save_attachment_file_move_removes_source(tmp_path):
    src = tmp_path / "data.zip"
    src.write_bytes(b"abc")
    saved = media.save_attachment_file("t-media-move", "data.zip", src, move=True)
    assert saved is not None and saved.read_bytes() == b"abc"
    assert not src.exists()


def test_save_attachment_file_rejects_oversized(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "MAX_ATTACHMENT_BYTES", 4)
    src = tmp_path / "x.zip"
    src.write_bytes(b"12345")
    assert media.save_attachment_file("t-media-over", "x.zip", src) is None
    assert not list(paths.media_dir("t-media-over").iterdir())


def test_save_attachment_rejects_when_disk_is_full(tmp_path, monkeypatch):
    import collections

    usage = collections.namedtuple("U", "total used free")(1, 1, 0)
    monkeypatch.setattr(media.shutil, "disk_usage", lambda *_: usage)
    src = tmp_path / "x.zip"
    src.write_bytes(b"1")
    assert media.save_attachment("t-media-full", "x.zip", b"1") is None
    assert media.save_attachment_file("t-media-full", "x.zip", src) is None


def test_has_free_space_tolerates_errors(tmp_path, monkeypatch):
    def fail(*_):
        raise OSError("nope")

    monkeypatch.setattr(media.shutil, "disk_usage", fail)
    assert media.has_free_space(tmp_path, 10) is True
