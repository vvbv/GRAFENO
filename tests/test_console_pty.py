"""Tests of the PTY-backed console process (POSIX only)."""

from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="PTY requires POSIX")

from grafeno.tui.console_pty import ConsoleProcess


def _read_until(proc: ConsoleProcess, needle: bytes, timeout: float = 5.0) -> bytes:
    """Accumulate non-blocking reads until ``needle`` appears or times out."""
    data = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and needle not in data:
        chunk = proc.read()
        if chunk:
            data += chunk
        else:
            time.sleep(0.05)
    return data


def test_custom_command_echoes_input(tmp_path):
    """`cat` on a pty echoes back what we write (input reaches the process)."""
    proc = ConsoleProcess("cat", str(tmp_path))
    proc.start()
    try:
        assert proc.fd is not None
        assert proc.running
        proc.write("hola-grafeno\n")
        assert b"hola-grafeno" in _read_until(proc, b"hola-grafeno")
    finally:
        proc.close()
    assert not proc.running


def test_default_shell_runs_and_exits_cleanly(tmp_path):
    """The default shell accepts a command and its exit code is visible."""
    proc = ConsoleProcess("", str(tmp_path))
    proc.start()
    try:
        proc.write("exit 7\n")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.05)
        assert proc.poll() == 7
    finally:
        proc.close()


def test_close_is_idempotent_and_read_is_safe(tmp_path):
    """close() twice does not raise; read() after close returns b""."""
    proc = ConsoleProcess("cat", str(tmp_path))
    proc.start()
    proc.close()
    proc.close()
    assert proc.read() == b""
    assert proc.fd is None


def test_start_with_bad_command_raises(tmp_path):
    """A non-existent command raises instead of hanging."""
    proc = ConsoleProcess("comando-que-no-existe-grafeno", str(tmp_path))
    with pytest.raises(OSError):
        proc.start()
