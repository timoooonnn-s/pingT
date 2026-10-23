"""Single keys from the terminal, without Enter (POSIX terminals only)."""

from __future__ import annotations

import os
import re
import select
import sys
import termios
import time
import tty

# One key: an escape sequence (arrow and F keys: ESC [ ... or ESC O x; Alt+key: ESC x), a lone ESC,
# or one character.
KEY = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|O.|[^\x1b])?|.", re.S)
UNFINISHED_SEQUENCE = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*|O)?\Z")


def split_keys(data: str) -> list[str]:
    """Lower-case keys, Esc as "esc". Escape sequences (arrow keys, F keys, Alt+key) are dropped,
    so they can't answer or cancel a question."""
    return ["esc" if k == "\x1b" else k.lower() for k in KEY.findall(data) if k == "\x1b" or not k.startswith("\x1b")]


class Keyboard:
    def __init__(self):
        self.enabled = sys.stdin.isatty()
        self._saved_mode = None

    def __enter__(self):
        if self.enabled:
            self._saved_mode = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        return self

    def __exit__(self, *exc):
        if self._saved_mode is not None:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._saved_mode)
            except (termios.error, OSError):  # the terminal is gone (SSH dropped)
                pass

    def read(self, timeout: float) -> list[str]:
        """The keys pressed within timeout."""
        if not self.enabled:
            time.sleep(timeout)
            return []
        if not select.select([sys.stdin], [], [], timeout)[0]:
            return []
        data = os.read(sys.stdin.fileno(), 64)
        if not data:  # stdin closed: stop reading, or select would return at once forever
            self.enabled = False
        # a slow link can split an arrow key: wait briefly for the rest, or its ESC counts as Esc
        while UNFINISHED_SEQUENCE.search(data) and select.select([sys.stdin], [], [], 0.1)[0]:
            more = os.read(sys.stdin.fileno(), 64)
            if not more:
                break
            data += more
        return split_keys(data.decode(errors="ignore"))
