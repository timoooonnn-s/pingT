"""The event log: recent events for the screen, every event in the log file."""

from __future__ import annotations

from collections import deque
from datetime import datetime

from rich.text import Text

from .durations import duration
from .host import DOWN, Host, HostEvent

# kind -> (text after the count, style), for several hosts of a group with that event in one round
GROUPED_TEXT = {
    "DOWN": ("hosts DOWN", "bold white on red"),
    "UP": ("hosts UP", "bold green"),
    "LOSS": ("hosts with LOSS", "bold red"),
    "CLEAR": ("hosts: loss cleared", "green"),
    "FIRST": ("hosts answered for the first time", "cyan"),
    "BACK": ("ignored hosts are BACK - watched again", "bold green"),
}


class EventLog:
    def __init__(self, path: str, screen_lines: int):
        self.path = path
        self.recent: deque[Text] = deque(maxlen=screen_lines)  # for the event panel
        self._file = open(path, "a", encoding="utf-8")  # open for the whole run, closed by close()

    def write(self, text: str, style: str, host: Host | None = None, group: str | None = None,
              screen: bool = True, file: bool = True):
        """One event. host: about that host; group: about a whole group."""
        now = datetime.now()
        who = host.who + " " if host else ""
        if screen:
            self.recent.append(Text.assemble((f"{now:%H:%M:%S} ", "dim"), (f"[{group}] " if group else "", "bold"),
                                             (who, "bold"), (text, style)))
        if file:
            group = host.group if host else group
            self._file.write(f"{now.isoformat(timespec='seconds')} {f'[{group}] ' if group else ''}{who}{text}\n")
            self._file.flush()

    def close(self):
        self._file.close()


class RoundEvents:
    """The host events of one ping round. When several hosts of a group have the same kind of
    event, the screen gets one line for the group; the file always gets every host
    (DECISIONS.md "Grouped events")."""

    def __init__(self):
        self._by_kind_and_group: dict[tuple[str, str], list[tuple[Host, HostEvent]]] = {}

    def add(self, host: Host, event: HostEvent):
        self._by_kind_and_group.setdefault((event.kind, host.group), []).append((host, event))

    def write(self, log: EventLog, groups: dict[str, list[Host]]):
        for (kind, group), items in self._by_kind_and_group.items():
            if len(items) == 1:
                host, event = items[0]
                log.write(event.text, event.style, host=host)
                continue
            text, style = GROUPED_TEXT[kind]
            text = f"{len(items)} {text}"
            if kind == "UP":
                still_down = sum(1 for h in groups[group] if h.state == DOWN)
                text += f" (longest outage {duration(max(e.outage for _, e in items))})"
                text += f", {still_down} still DOWN" if still_down else ""
            log.write(text, style, group=group)
            for host, event in items:
                log.write(event.text, event.style, host=host, screen=False)
