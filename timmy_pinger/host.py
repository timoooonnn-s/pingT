"""One watched host and what a reply or a lost ping does to it (the state machine)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .durations import duration

# The states. The values are what the log and the summary print.
UNKNOWN = "UNKN"  # no ping yet
SILENT = "SILENT"  # never answered since the start: was off before the migration, no alarm
OK = "OK"
WARN = "WARN"
LOSS = "LOSS"
DOWN = "DOWN"
INVALID = "INVALID"  # the host list entry can't be pinged
IGNORED = "IGNORED"  # taken out by the operator (x key)

SEVERITY = {DOWN: 0, LOSS: 1, WARN: 2, INVALID: 3, UNKNOWN: 4, OK: 5, SILENT: 6, IGNORED: 7}  # worst first


@dataclass(frozen=True)
class Limits:
    window: int  # how many recent pings count for WARN / LOSS
    warn: int  # lost pings in the window until WARN
    loss: int  # lost pings in the window until LOSS
    down: int  # lost pings in a row until DOWN; also replies in a row until an ignored host is watched again


@dataclass(frozen=True)
class HostEvent:
    kind: str  # DOWN, UP, LOSS, CLEAR, FIRST, BACK - several of a kind in one round are grouped per group
    text: str
    style: str
    outage: float = 0.0  # UP: how long the host was down


@dataclass
class Host:
    target: str  # as written in the host list: an IP or a DNS name
    label: str  # the name from the host list, or the target itself
    group: str
    order: int  # position in the host list
    source: str = ""  # "hosts.txt:12", for messages
    ip: str = ""  # what is pinged: the target, or the IP its DNS name had at start
    invalid: str | None = None  # why the entry can't be pinged
    dns_name: str | None = None  # from the reverse lookup; None = none (yet)

    recent: deque = field(default_factory=deque)  # recent round trip times in ms, None = lost
    sent: int = 0
    lost: int = 0
    misses_in_row: int = 0
    missing_since: float | None = None  # first lost ping of the current run of misses
    state: str = UNKNOWN
    outages: int = 0
    longest_outage: float = 0.0
    answered_once: bool = False  # counts for "back N/M"; kept by a reset
    known_from_baseline: bool = False  # answered_once comes from an earlier run

    ignored_since: float | None = None
    replies_while_ignored: int = 0
    ignore_history: list = field(default_factory=list)  # [ignored at, back at or None] per ignore

    @property
    def recent_lost(self) -> int:
        return sum(1 for rtt in self.recent if rtt is None)

    @property
    def recent_loss_pct(self) -> float:
        return 100.0 * self.recent_lost / len(self.recent) if self.recent else 0.0

    @property
    def who(self) -> str:
        """"label (IP)", or only the IP when it has no label."""
        return f"{self.label} ({self.target})" if self.label != self.target else self.target

    @property
    def is_back(self) -> bool:
        """Counts as back in "back N/M": answered at some point and not DOWN now (a lossy host is back)."""
        return self.answered_once and self.state != DOWN

    def record_ping(self, rtt: float | None, now: float, limits: Limits) -> list[HostEvent]:
        """One ping result (rtt in ms, None = lost). Returns what is worth an event."""
        events: list[HostEvent] = []
        if self.ignored_since is not None:  # only watch for it coming back, count nothing
            self.replies_while_ignored = self.replies_while_ignored + 1 if rtt is not None else 0
            if self.replies_while_ignored < limits.down:
                return events
            self.ignore_history[-1][1] = now
            events.append(HostEvent("BACK", f"BACK after being ignored for {duration(now - self.ignored_since)}"
                                            f" - watched again", "bold green"))
            self.ignored_since, self.replies_while_ignored = None, 0
            self.answered_once = True
            self.state = UNKNOWN  # this reply is recorded below as the first one of a watched host

        if rtt is not None and not self.answered_once:
            self.answered_once = True
            if self.state == SILENT:
                events.append(HostEvent("FIRST", f"FIRST REPLY (silent for {self.misses_in_row} pings)", "cyan"))
            # the misses before its first reply are not loss: it was simply off
            self.recent.clear()
            self.sent -= self.lost
            self.lost = self.misses_in_row = 0
            self.missing_since = None

        self.sent += 1
        misses, missing_since = self.misses_in_row, self.missing_since
        if rtt is not None and self.state == DOWN:
            # The outage is counted separately (event, outages, longest), so it leaves the
            # loss window: afterwards the window shows real packet loss only.
            for _ in range(min(misses, len(self.recent))):
                self.recent.pop()
        self.recent.append(rtt)
        if rtt is None:
            self.lost += 1
            if self.misses_in_row == 0:
                self.missing_since = now
            self.misses_in_row += 1
        else:
            self.misses_in_row = 0
            self.missing_since = None

        old, new = self.state, self._evaluate(limits)
        if old == new:
            return events
        self.state = new
        if new == DOWN:
            self.outages += 1
            events.append(HostEvent("DOWN", f"DOWN  ({self.misses_in_row} missed in a row)", "bold white on red"))
        elif old == DOWN:
            outage = now - missing_since if missing_since else 0.0
            self.longest_outage = max(self.longest_outage, outage)
            events.append(HostEvent("UP", f"UP    after {duration(outage)} ({misses} lost)", "bold green", outage))
        elif new == LOSS:
            events.append(HostEvent("LOSS", f"LOSS  {self.recent_lost}/{len(self.recent)} lost in window", "bold red"))
        elif old == LOSS and new in (OK, WARN):
            events.append(HostEvent("CLEAR", f"CLEAR loss back to {self.recent_lost}/{len(self.recent)}", "green"))
        return events

    def _evaluate(self, limits: Limits) -> str:
        if self.ignored_since is not None:
            return IGNORED
        if not self.recent:
            return UNKNOWN
        if not self.answered_once:
            return SILENT
        if self.misses_in_row >= limits.down:
            return DOWN
        lost = self.recent_lost
        if lost >= limits.loss:
            return LOSS
        if lost >= limits.warn:
            return WARN
        return OK

    def reset_stats(self, keep_outage: bool = False):
        """Clears the counters. keep_outage (r r): a host that is DOWN right now stays DOWN, its
        outage still counted from when it started - it must not count as back or alarm again."""
        still_down = keep_outage and self.state == DOWN
        misses, missing_since = self.misses_in_row, self.missing_since
        self.recent.clear()
        self.sent = self.lost = self.misses_in_row = self.replies_while_ignored = 0
        self.missing_since = None
        self.outages, self.longest_outage = 0, 0.0
        if self.invalid:
            self.state = INVALID
        elif self.ignored_since is not None:
            self.state = IGNORED
        elif still_down:
            self.state, self.outages = DOWN, 1
            self.misses_in_row, self.missing_since = misses, missing_since
        else:
            self.state = UNKNOWN

    def ignore(self, now: float) -> dict:
        """Takes the host out of the alarms (x key). Returns what undo_ignore needs to put it back."""
        snapshot = {name: getattr(self, name) for name in _IGNORE_UNDOES} | {"recent": self.recent.copy()}
        self.reset_stats()  # the outage (user went home) is not migration damage; the event log keeps it
        self.answered_once = self.known_from_baseline = False
        self.ignored_since, self.state = now, IGNORED
        self.ignore_history.append([now, None])
        return snapshot

    def undo_ignore(self, snapshot: dict):
        for name, value in snapshot.items():
            setattr(self, name, value)
        self.ignored_since, self.replies_while_ignored = None, 0
        self.ignore_history.pop()


# what ignore() changes and undo_ignore() restores (recent is copied separately)
_IGNORE_UNDOES = ("sent", "lost", "misses_in_row", "missing_since", "state", "outages", "longest_outage",
                  "answered_once", "known_from_baseline")


def back_counts(hosts: list[Host]) -> tuple[int, int]:
    """(back now, answered at some point) - the two numbers of "back N/M"."""
    return sum(1 for h in hosts if h.is_back), sum(1 for h in hosts if h.answered_once)


def all_back(back: int, answered: int) -> bool:
    """"back N/M" is green: every host that answered at some point is back (0/0 is not green)."""
    return back == answered and answered > 0
