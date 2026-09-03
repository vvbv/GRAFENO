"""PTY-backed shell process for the consoles screen (POSIX only).

Line-oriented console: the widget reads raw bytes from the master fd and
writes user input back. Full-screen curses programs (vim, htop, less) are
not supported; the pty line discipline provides echo and Ctrl+C handling.
"""

from __future__ import annotations

import os
import shlex
import struct
import subprocess

from ..consoles import default_shell

# fcntl/pty/termios are POSIX-only. Keep them conditional so the module stays
# importable on Windows (the consoles screen gates spawning behind
# ``consoles.supported()``, which is False there).
if os.name == "posix":
    import fcntl
    import pty
    import termios

# Default terminal size advertised to the spawned process (rows, cols).
_ROWS, _COLS = 40, 120


class ConsoleProcess:
    """Shell (or custom command) attached to a pseudo-terminal."""

    def __init__(self, command: str, workdir: str):
        self._command = command
        self._workdir = workdir
        self._fd: int | None = None       # master side of the pty
        self._proc: subprocess.Popen | None = None

    @property
    def fd(self) -> int | None:
        """Master fd to watch for readability (None before start)."""
        return self._fd

    @property
    def running(self) -> bool:
        """True while the child process is alive."""
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        """Spawn the process on a fresh pty. Raises OSError on failure."""
        if os.name != "posix":
            raise OSError("PTY consoles require a POSIX platform")
        argv = shlex.split(self._command) if self._command.strip() else [default_shell()]
        env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
        master, slave = pty.openpty()
        # Disable kernel echo: the consoles screen echoes submitted lines
        # itself (``ConsolesScreen._local_echo``). With kernel echo on, a
        # line written while the tty is still in canonical mode is echoed
        # once by the kernel and again by the shell's line editor, showing
        # a duplicated prefix (e.g. "lls"). Programs reading stdin in
        # canonical mode (e.g. cat) will not echo their input anymore; the
        # local echo keeps submitted lines visible regardless.
        # ``tcgetattr`` returns ``[iflag, oflag, cflag, lflag, ...]``; index
        # ``3`` is ``lflag``.
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        # Advertise a sane window size so tools format their output.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", _ROWS, _COLS, 0, 0))
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=self._workdir,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        except Exception:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)          # the parent only keeps the master side
        os.set_blocking(master, False)
        self._fd = master

    def read(self, size: int = 65536) -> bytes:
        """Non-blocking read; b"" when nothing is pending or on EOF."""
        if self._fd is None:
            return b""
        try:
            return os.read(self._fd, size)
        except BlockingIOError:
            return b""
        except OSError:
            return b""  # EIO on Linux when the slave side closed (process exited)

    def write(self, data: str) -> None:
        """Send user input to the process (best effort)."""
        if self._fd is None or not self.running:
            return
        try:
            os.write(self._fd, data.encode("utf-8", errors="replace"))
        except OSError:
            pass

    def interrupt(self) -> None:
        """Send Ctrl+C through the pty line discipline (SIGINT)."""
        self.write("\x03")

    def poll(self) -> int | None:
        """Exit code of the child, or None while it runs / before start."""
        return None if self._proc is None else self._proc.poll()

    def close(self) -> None:
        """Terminate the process and close the master fd (idempotent)."""
        proc, self._proc = self._proc, None
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        elif proc is not None:
            proc.wait()  # already dead: reap the zombie
