"""What pingT shows: the top line, the group lines, the problems list, the box view and the
recent events. It only reads the session (under its lock); the keys change ViewSettings."""

from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass, field

from rich.console import Group
from rich.table import Table
from rich.text import Text

from . import CREDITS, __version__
from .charsets import Charset
from .durations import duration, short_duration
from .host import (DOWN, IGNORED, INVALID, LOSS, OK, SEVERITY, SILENT, UNKNOWN, WARN, Host, all_back,
                   back_counts)
from .session import Session

NAME_MODES = ("label", "dns", "ip")  # what "n" cycles through

# state -> (style of the marker / count, style of the name)
STATE_STYLES = {
    UNKNOWN: ("dim", "dim"),
    SILENT: ("dim", "dim"),
    IGNORED: ("dim", "dim"),
    INVALID: ("bold magenta", "magenta"),
    OK: ("green", ""),
    WARN: ("yellow", "yellow"),
    LOSS: ("bold red", "bold red"),
    DOWN: ("bold white on red", "bold white on red"),
}
DOT_STYLES = {UNKNOWN: "dim", SILENT: "dim", IGNORED: "dim", INVALID: "bold magenta",
              OK: "green", WARN: "yellow", LOSS: "bold red", DOWN: "bold white on red"}

# host box layouts, most detailed first: (name width, recent pings shown, loss % shown)
BOX_LAYOUTS = [(18, 20, True), (16, 15, True), (14, 10, True), (12, 8, True), (12, 5, True),
               (10, 0, True), (8, 0, True), (6, 0, True), (6, 0, False), (4, 0, False)]
DOWN_FOR_WIDTH = 10  # "down 4:12 " - shown instead of the oldest recent pings of a DOWN host


@dataclass
class ViewSettings:
    charset: Charset
    layout: str = "groups"  # "groups", or "boxes": every host as a box (v)
    names: str = "label"  # one of NAME_MODES (n)
    notice: str = ""  # a short message in the top line
    notice_until: float = 0.0
    sticky: str = ""  # a warning in the top line until the next key
    group_numbers: dict = field(default_factory=dict)  # group -> number key, while x waits for one

    def notify(self, text: str, seconds: float):
        self.notice, self.notice_until = text, time.time() + seconds

    def clear_notice(self):
        self.notice, self.notice_until = "", 0.0


def display_name(h: Host, mode: str, with_ip: bool = False) -> str:
    """The host's name in a name mode; with_ip adds the IP when the name isn't the IP itself."""
    ip = h.ip or h.target
    if mode == "ip":
        return ip
    name = (h.dns_name or ip) if mode == "dns" else h.label
    return f"{name} {ip}" if with_ip and name != ip else name


def box_width(name_width: int, pings_shown: int, with_loss: bool) -> int:
    """Characters of one host box: marker + name + loss % + recent pings."""
    return 2 + name_width + (5 if with_loss else 0) + (1 + pings_shown if pings_shown else 0)


class Screen:
    def __init__(self, session: Session, view: ViewSettings, interval: float, event_lines: int):
        self.session = session
        self.view = view
        self.interval = interval
        self.event_lines = event_lines

    def render(self, width: int, height: int) -> Group:
        with self.session.lock:
            if self.view.layout == "groups":
                body, used = self._group_view(width, height - 1)
            else:  # every host as a box; at least 3 lines stay for the events
                grid, used = self._boxes(self.session.hosts, width,
                                         max(1, height - 1 - min(self.event_lines, 3)), with_ip=False)
                body = [grid]
            parts = [self._top_line(width), *body]
            parts += self._events(width, height - 1 - used)
            return Group(*parts)

    @property
    def cs(self) -> Charset:
        return self.view.charset

    # -- top line
    def _top_line(self, width: int) -> Text:
        """The less important parts (higher number) are left out first when the line is too long."""
        session = self.session
        counts = Counter(h.state for h in session.hosts)
        back, answered = back_counts(session.hosts)
        problem = session.ping_problem(self.interval)
        parts: list[tuple[int, str, str]] = [(0, " pingT ", "bold black on cyan")]
        if problem:
            parts.append((0, f" {problem} ", "bold white on magenta"))
        if session.is_all_down:
            parts.append((0, " ALL DOWN - check pingT's own network ", "bold white on magenta"))
        if self.view.sticky:
            parts.append((0, f" {self.view.sticky} ", "bold black on yellow"))
        if self.view.notice and time.time() < self.view.notice_until:
            parts.append((0, f" {self.view.notice} ", "bold black on yellow"))
        parts += [
            (0, f" back {back}/{answered} ", "bold black on green" if all_back(back, answered) else "bold white on red"),
            (1, f"ok {counts[OK]}", "green"),
            (1, f"warn {counts[WARN]}", "yellow"),
            (1, f"loss {counts[LOSS]}", "red"),
            (0, f"down {counts[DOWN]}", "bold white on red" if counts[DOWN] else "red"),
            (2, f"silent {counts[SILENT]}", "dim"),
        ]
        if counts[IGNORED]:
            parts.append((1, f"ignored {counts[IGNORED]}", "yellow"))
        if counts[INVALID]:
            parts.append((1, f"invalid {counts[INVALID]}", "bold magenta"))
        parts.append((2, f"names:{self.view.names}", "dim" if self.view.names == "label" else "bold cyan"))
        if session.dns_pending:
            parts.append((3, f"dns no answer, {session.dns_pending} left (probing 1/min)", "yellow")
                         if session.dns_server_down else (3, f"dns {session.dns_pending} left", "dim"))
        parts.append((3, f"fping {duration(time.time() - session.started)} #{session.rounds} "
                         f"{session.last_round_s:.1f}s", "dim"))
        parts.append((4, "[q]uit [r]eset [v]iew [n]ames [x]ignore [g]lyphs", "dim"))
        parts.append((5, f"pingT {__version__} - {CREDITS}", "dim"))

        while sum(len(text) + 1 for _, text, _ in parts) > width:
            least_important = max(priority for priority, _, _ in parts)
            if least_important == 0:
                break
            parts = [part for part in parts if part[0] != least_important]
        line = Text(no_wrap=True, overflow=self.cs.overflow)
        for i, (_, text, style) in enumerate(parts):
            line.append((" " if i else "") + text, style)
        return line

    # -- group view: one line per group, one dot per host, then the problems list
    def _group_view(self, width: int, height: int) -> tuple[list, int]:
        session = self.session
        numbers = self.view.group_numbers
        name_width = min(22, max(len(g) for g in session.groups))
        number_width = 2 if numbers else 0
        prefix_width = number_width + name_width + 1 + 8  # "Clients 2F 123/456 "
        dots_per_line = max(10, ((width - prefix_width + 1) // 11) * 10)  # blocks of 10 dots + a space
        blocks: list[tuple[str, list[Text]]] = []  # (group, its lines)
        for group, members in session.groups.items():
            back, answered = back_counts(members)
            worst = min((h.state for h in members), key=SEVERITY.get)
            lines = []
            for start in range(0, len(members), dots_per_line):
                line = Text(no_wrap=True, overflow="crop")
                if start == 0:
                    if number_width:
                        line.append(numbers.get(group, " ") + " ", "bold black on yellow" if group in numbers else "")
                    line.append(self._cut(group, name_width).ljust(name_width) + " ", STATE_STYLES[worst][1] or "bold")
                    line.append(f"{back:>3}/{answered:<3} ",
                                "green" if all_back(back, answered) else STATE_STYLES[worst][0])
                else:
                    line.append(" " * prefix_width)
                for i, h in enumerate(members[start:start + dots_per_line]):
                    line.append((" " if i and i % 10 == 0 else "") + self.cs.dots[h.state], DOT_STYLES[h.state])
                lines.append(line)
            blocks.append((group, lines))

        problems = sorted((h for h in session.hosts if h.state in (DOWN, LOSS, WARN, INVALID)),
                          key=lambda h: (SEVERITY[h.state], -h.recent_lost, h.order))
        rows_wanted = 0
        if problems:
            name_width, pings, with_loss = BOX_LAYOUTS[0]
            columns = max(1, (width + 1) // (box_width(name_width + 16, pings, with_loss) + 1))
            rows_wanted = math.ceil(len(problems) / columns)

        # The group lines must not push the problems and events off the screen: those keep some
        # rows, and the groups that don't fit are summed up in one line (DECISIONS.md).
        kept_rows = min(self.event_lines, 2) + (1 + min(rows_wanted, max(3, height // 3)) if problems else 0)
        rows_for_groups = max(1, height - kept_rows)
        lines = [line for _, group_lines in blocks for line in group_lines]
        if len(lines) > rows_for_groups:
            lines = self._cut_groups(blocks, rows_for_groups)

        free = height - len(lines)
        if problems:  # problems get the rows they need, the events what's left (at least 2)
            event_rows = min(self.event_lines, max(2, free - 1 - rows_wanted))
        else:
            event_rows = min(self.event_lines, free - 1)
        problem_rows = max(0, free - event_rows - 1)  # -1 for the separator
        out: list = list(lines)
        used = len(lines)
        if problem_rows > 0:
            out.append(self._separator(f"problems ({len(problems)})" if problems else "no problems", width,
                                       "bold red" if problems else "dim green"))
            used += 1
            if problems:
                grid, rows = self._boxes(problems, width, problem_rows, with_ip=True)
                out.append(grid)
                used += rows
        return out, used

    def _cut_groups(self, blocks: list[tuple[str, list[Text]]], rows: int) -> list[Text]:
        """Whole groups in host list order, then one line for the rest. While x waits for a
        number, the numbered groups come first, so their numbers can be seen."""
        numbers = self.view.group_numbers
        if numbers:
            blocks = sorted(blocks, key=lambda block: block[0] not in numbers)
        lines: list[Text] = []
        left_out: list[str] = []
        for group, group_lines in blocks:
            if not left_out and len(lines) + len(group_lines) <= rows - 1:
                lines += group_lines
            elif not left_out and not lines and rows > 1:  # a single group bigger than the screen: its start
                lines = group_lines[: rows - 1]
            else:
                left_out.append(group)
        hosts = [h for group in left_out for h in self.session.groups[group]]
        back, answered = back_counts(hosts)
        counts = Counter(h.state for h in hosts)
        states = ", ".join(f"{n} {name}" for n, name in
                           ((counts[DOWN], "down"), (counts[LOSS], "loss"), (counts[WARN], "warn")) if n)
        worst = min((h.state for h in hosts), key=SEVERITY.get) if hosts else OK
        text = f"{self.cs.ellipsis} {len(left_out)} more groups ({len(hosts)} hosts): back {back}/{answered}"
        lines.append(Text(text + (f", {states}" if states else ""), no_wrap=True, overflow="crop",
                          style="green" if all_back(back, answered) and not states else STATE_STYLES[worst][0]))
        return lines

    # -- host boxes: the problems list and the box view
    def _boxes(self, hosts: list[Host], width: int, max_rows: int, with_ip: bool) -> tuple[Table, int]:
        """A grid of host boxes in the most detailed layout that fits into max_rows."""
        # sized for every name mode, so "n" keeps each host in the same place
        longest_name = max(len(display_name(h, mode, with_ip)) for h in hosts for mode in NAME_MODES)
        for layout_name_width, pings, with_loss in BOX_LAYOUTS:
            name_width = min(layout_name_width + (16 if with_ip else 0), max(4, longest_name))
            cell_width = box_width(name_width, pings, with_loss)
            columns = max(1, (width + 1) // (cell_width + 1))
            if columns * max_rows >= len(hosts):
                break
        rows = min(max_rows, math.ceil(len(hosts) / columns))
        capacity = rows * columns
        shown = hosts if len(hosts) <= capacity else hosts[: capacity - 1]
        now = time.time()
        cells = [self._box(h, name_width, pings, with_loss, with_ip, now) for h in shown]
        if len(shown) < len(hosts):
            cells.append(Text(f"{self.cs.ellipsis} +{len(hosts) - len(shown)} more", style="bold red"))
        grid = Table.grid(padding=(0, 1))
        for _ in range(columns):
            grid.add_column(width=cell_width, no_wrap=True)
        for row in range(rows):  # column by column: reads top to bottom, like the host list
            grid.add_row(*[cells[col * rows + row] if col * rows + row < len(cells) else Text("")
                           for col in range(columns)])
        return grid, rows

    def _box(self, h: Host, name_width: int, pings: int, with_loss: bool, with_ip: bool, now: float) -> Text:
        cs = self.cs
        marker_style, name_style = STATE_STYLES[h.state]
        box = Text(no_wrap=True, overflow="crop")
        box.append(cs.markers.get(h.state, cs.markers[OK]) + " ", marker_style)
        box.append(self._cut(display_name(h, self.view.names, with_ip), name_width).ljust(name_width), name_style)
        if h.state == INVALID:
            box.append(f" invalid: {h.invalid}", "magenta")
            return box
        down_for = now - h.missing_since if h.state == DOWN and h.missing_since else None
        show_down_for = down_for is not None and pings >= DOWN_FOR_WIDTH
        if with_loss:
            if down_for is not None:  # the loss % of a DOWN host says nothing: how long it is down instead
                box.append(" " * 5 if show_down_for else f"{short_duration(down_for):>5}", "bold red")
            elif h.state in (SILENT, UNKNOWN, IGNORED) or not h.recent:  # nothing to measure
                box.append("    -", "dim")
            else:
                pct = h.recent_loss_pct
                box.append(f"{pct:4.0f}%", "dim" if pct == 0 else "yellow")
        if pings:
            box.append(" ")
            if show_down_for:
                box.append(f"down {short_duration(down_for):<5}".ljust(DOWN_FOR_WIDTH + (pings > DOWN_FOR_WIDTH)),
                           "bold red")
                pings = max(0, pings - DOWN_FOR_WIDTH - 1)
            recent = list(h.recent)[-pings:] if pings else []
            box.append(cs.no_ping_yet * (pings - len(recent)), "dim")
            for rtt in recent:
                if rtt is None:
                    box.append(cs.lost, "bold red")
                else:
                    box.append(cs.latency_char(rtt), "green" if rtt < 30 else "yellow")
        return box

    # -- events and helpers
    def _events(self, width: int, rows: int) -> list[Text]:
        """Separator + the most recent events, as many as --events and the free rows allow."""
        rows = min(rows, self.event_lines)
        if rows <= 0:
            return []
        lines = []
        if rows > 1:
            lines.append(self._separator("events", width, "dim"))
            rows -= 1
        for event in list(self.session.log.recent)[-rows:]:
            line = event.copy()
            line.truncate(width, overflow=self.cs.overflow)
            lines.append(line)
        return lines

    def _separator(self, title: str, width: int, style: str) -> Text:
        """"─ title ─────" across the screen."""
        return Text(f"{self.cs.line} {title} " + self.cs.line * max(0, width - len(title) - 3), style=style)

    def _cut(self, text: str, width: int) -> str:
        ellipsis = self.cs.ellipsis
        return text if len(text) <= width else text[: max(0, width - len(ellipsis))] + ellipsis
