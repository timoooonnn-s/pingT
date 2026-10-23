"""What the keys do. q v n g act at once; r and x ask first, and only their answer, Esc or
5 seconds without a key close the question - a stray key or arrow key does nothing."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .charsets import CHARSETS, ENV_VARIABLE
from .durations import duration
from .host import DOWN, Host
from .screen import NAME_MODES, ViewSettings
from .session import Session

QUESTION_TIMEOUT_S = 5
MAX_NUMBERED_GROUPS = 10  # keys 1..9 and 0


@dataclass
class Question:
    kind: str  # "reset" (r r), "pick group" (x, then a number), "confirm group" (the same number again)
    until: float
    group_keys: dict = field(default_factory=dict)  # number key -> group
    hosts: list = field(default_factory=list)  # confirm group: the hosts that will be ignored


class Controls:
    def __init__(self, session: Session, view: ViewSettings, dns_enabled: bool):
        self.session = session
        self.view = view
        self.dns_enabled = dns_enabled
        self.question: Question | None = None

    def press(self, key: str) -> bool:
        """One key. Returns True to quit."""
        now = time.time()
        with self.session.lock:
            self.view.sticky = ""  # any key hides a sticky warning
            if self.question:
                self._answer(key, now)
                return False
            self.view.clear_notice()
            if key == "q":
                return True
            if key == "r":
                self._ask(Question("reset", now + QUESTION_TIMEOUT_S), "press r again to RESET all stats, Esc cancels")
            elif key == "x":
                self._open_ignore(now)
            elif key == "v":
                self.view.layout = "boxes" if self.view.layout == "groups" else "groups"
            elif key == "n":
                modes = [mode for mode in NAME_MODES if mode != "dns" or self.dns_enabled]
                self.view.names = modes[(modes.index(self.view.names) + 1) % len(modes)]
                if not self.dns_enabled:  # the top line shows the mode; only say why DNS is skipped
                    self.view.notify("names: label / IP - DNS names are off (--no-dns)", 3)
            elif key == "g":
                names = list(CHARSETS)
                charset = CHARSETS[names[(names.index(self.view.charset.name) + 1) % len(names)]]
                self.view.charset = charset
                self.view.notify(f"characters: {charset.name} - keep it with --charset {charset.name} "
                                 f"or {ENV_VARIABLE}={charset.name}", 5)
        return False

    def close_expired_question(self):
        with self.session.lock:
            if self.question and time.time() > self.question.until:
                self._close(f"cancelled (no key for {QUESTION_TIMEOUT_S}s)")

    def _ask(self, question: Question, text: str):
        self.question = question
        self.view.group_numbers = {group: key for key, group in question.group_keys.items()}
        self.view.notice, self.view.notice_until = text, question.until

    def _close(self, notice: str = ""):
        self.question = None
        self.view.group_numbers = {}
        if notice:
            self.view.notify(notice, 3)
        else:
            self.view.clear_notice()

    def _answer(self, key: str, now: float):
        question = self.question
        if key == "esc":
            self._close("cancelled")
        elif question.kind == "reset" and key == "r":
            self._close("stats reset")
            self.session.reset_stats(now)
        elif question.kind == "pick group" and key == "u" and self.session.last_ignore:
            self._close()
            group, restored = self.session.undo_last_ignore()
            self.view.notify(f'undone: {restored} hosts in "{group}" are watched again', 5)
        elif question.kind == "pick group" and key in question.group_keys:
            self._ask_to_confirm(key, question.group_keys[key], now)
        elif question.kind == "confirm group" and key in question.group_keys:
            group = question.group_keys[key]
            hosts = [h for h in question.hosts if h.state == DOWN]  # not the ones that came back meanwhile
            self._close()
            if hosts:
                self.session.ignore(group, hosts, now)
                self.view.notify(f'ignored {len(hosts)} hosts in "{group}" - x u undoes it', 5)
            else:
                self.view.notify(f'no DOWN hosts left in "{group}" - nothing ignored', 3)

    # -- x: ignore the DOWN hosts of a group (clients that went home)
    def _open_ignore(self, now: float):
        groups = self.session.groups_with_down_hosts()
        last = self.session.last_ignore
        undo = f"u = undo last ignore ({len(last.snapshots)} in {last.group})" if last else ""
        if not groups and not undo:
            self.view.notify("no DOWN hosts to ignore", 3)
            return
        keys = {str((i + 1) % 10): group for i, group in enumerate(groups[:MAX_NUMBERED_GROUPS])}
        self.view.layout = "groups"  # the numbers are shown next to the group lines
        if groups:
            text = "IGNORE the DOWN hosts of a group: press its number"
            if len(groups) > MAX_NUMBERED_GROUPS:
                text += f" (first {MAX_NUMBERED_GROUPS} of {len(groups)} groups)"
            text += (", " + undo if undo else "") + ", Esc cancels"
        else:
            text = f"no DOWN hosts to ignore - {undo}, Esc cancels"
        self._ask(Question("pick group", now + QUESTION_TIMEOUT_S, keys), text)

    def _ask_to_confirm(self, key: str, group: str, now: float):
        hosts = self.session.down_hosts(group)
        if not hosts:
            self._close(f'no DOWN hosts left in "{group}"')
            return
        self._ask(Question("confirm group", now + QUESTION_TIMEOUT_S, {key: group}, hosts),
                  f'ignore {len(hosts)} DOWN hosts in "{group}"{down_times(hosts, now)}? '
                  f'press {key} again to confirm, Esc cancels')


def down_times(hosts: list[Host], now: float) -> str:
    """ " (down 4:12 to 1:35:00)" - for checking that these really are the clients that went home."""
    downs = sorted(now - h.missing_since for h in hosts if h.missing_since)
    if not downs:
        return ""
    return f" (down {duration(downs[0])}" + (f" to {duration(downs[-1])})" if len(downs) > 1 else ")")
