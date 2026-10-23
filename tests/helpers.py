"""Shared test helpers: a fake-fping environment and a tiny pty driver that runs pingT / scan.py
in a real terminal, sends keys and reads the screen."""
from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import sys
import tempfile
import termios
import threading
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKEBIN = os.path.join(REPO, "tests", "fakebin")
sys.path.insert(0, REPO)

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Z0-9]|\x1b[=>78]|\x1b\][^\x07]*\x07")


def fake_env(workdir: str, **extra) -> dict:
    """Environment with the fake fping first in PATH; its state file is workdir/state.json."""
    env = dict(os.environ)
    env.update(PATH=FAKEBIN + os.pathsep + env["PATH"], FAKE_STATE=os.path.join(workdir, "state.json"),
               TERM="xterm-256color")
    env.pop("PINGT_CHARSET", None)
    env.update(extra)
    return env


def set_state(workdir: str, **state):
    with open(os.path.join(workdir, "state.json"), "w") as fh:
        json.dump(state, fh)


def wait_for(cond, timeout: float = 10, step: float = 0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


class WorkdirTest(unittest.TestCase):
    """Every test gets its own empty folder (removed afterwards) with the fake fping state in it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pingT-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        set_state(self.dir)

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def write(self, name: str, text: str) -> str:
        with open(self.path(name), "w", encoding="utf-8") as fh:
            fh.write(text)
        return self.path(name)


class Term:
    """A program in a pseudo terminal of rows x cols."""

    def __init__(self, argv: list[str], cwd: str, env: dict, rows: int = 30, cols: int = 120):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # child
            os.chdir(cwd)
            os.execvpe(argv[0], argv, env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.buf = b""
        self.lock = threading.Lock()
        self.code = None
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while True:
            try:
                if select.select([self.fd], [], [], 0.2)[0]:
                    data = os.read(self.fd, 65536)
                    if not data:
                        return
                    with self.lock:
                        self.buf += data
            except OSError:
                return

    def send(self, keys: str, pause: float = 0.7):
        os.write(self.fd, keys.encode())
        time.sleep(pause)

    def mark(self) -> int:
        with self.lock:
            return len(self.buf)

    def text(self, since: int = 0) -> str:
        with self.lock:
            raw = self.buf[since:]
        return ANSI.sub("", raw.decode(errors="replace"))

    def frame(self) -> str:
        """The last complete screen (from the second-last header on)."""
        txt = self.text(max(0, self.mark() - 30000))
        i = txt.rfind(" pingT ")
        j = txt.rfind(" pingT ", 0, i) if i > 0 else -1
        return txt[j:] if j >= 0 else txt

    def wait_exit(self, timeout: float = 20):
        end = time.time() + timeout
        while time.time() < end:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                time.sleep(0.3)  # let the reader catch the last output
                self.code = os.waitstatus_to_exitcode(status)
                return self.code
            time.sleep(0.1)
        return None

    def close(self):
        """Kills the program if it still runs."""
        if self.code is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
            except (OSError, ChildProcessError):
                pass
        try:
            os.close(self.fd)
        except OSError:
            pass
