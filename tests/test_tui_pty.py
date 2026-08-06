from __future__ import annotations

import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
import unittest
from contextlib import suppress
from pathlib import Path


def _read_until(descriptor: int, needle: bytes, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    output = b""
    while needle not in output:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
            raise AssertionError(f"timed out waiting for {needle!r}; received {output!r}")
        chunk = os.read(descriptor, 65536)
        if not chunk:
            raise AssertionError(f"terminal closed while waiting for {needle!r}")
        output += chunk
    return output


def _wait_while_draining(
    process: subprocess.Popen[bytes],
    descriptor: int,
    timeout: float,
) -> int:
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        if select.select([descriptor], [], [], min(remaining, 0.05))[0]:
            with suppress(OSError):
                os.read(descriptor, 65536)
    return process.returncode


class TuiPseudoTerminalTests(unittest.TestCase):
    def test_escape_cancels_search_without_ncurses_one_second_delay(self) -> None:
        """Measure the real curses input path, including Escape disambiguation."""

        project = Path(__file__).parents[1]
        master, slave = pty.openpty()
        control_read, control_write = os.pipe()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 16, 100, 0, 0))
        environment = os.environ.copy()
        python_path = [str(project), str(project / "tests")]
        if existing := environment.get("PYTHONPATH"):
            python_path.append(existing)
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join(python_path),
                "TERM": "xterm-256color",
                "KISESH_TEST_CONTROL_FD": str(control_write),
            }
        )
        program = """
import os
from kisesh.tui import SessionManager
from render_fixture import StaticService

control = int(os.environ["KISESH_TEST_CONTROL_FD"])

class ProbeManager(SessionManager):
    def _prompt(self, *args, **kwargs):
        os.write(control, b"prompt-started\\n")
        result = super()._prompt(*args, **kwargs)
        os.write(control, b"prompt-finished\\n")
        return result

    def _handle_key(self, screen, key):
        result = super()._handle_key(screen, key)
        return 0 if key == "/" else result

raise SystemExit(ProbeManager(StaticService()).run())
"""
        process = subprocess.Popen(
            [sys.executable, "-c", program],
            cwd=project,
            env=environment,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            pass_fds=(control_write,),
        )
        os.close(slave)
        os.close(control_write)
        try:
            _read_until(master, b"KiSesh", 2.0)
            os.write(master, b"/")
            _read_until(control_read, b"prompt-started", 1.0)

            started = time.monotonic()
            os.write(master, b"x\x1b")
            _read_until(control_read, b"prompt-finished", 0.5)
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.35, f"Escape cancellation took {elapsed:.3f}s")
            self.assertEqual(_wait_while_draining(process, master, 2.0), 0)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            os.close(master)
            os.close(control_read)


if __name__ == "__main__":
    unittest.main()
