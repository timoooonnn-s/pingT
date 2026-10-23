"""A pingT run: the hosts, the ping loop, and what the operator does to them (reset, ignore).

Three threads use a session - the ping loop, the reverse lookups and the screen/keys - so
everything that reads or changes it holds session.lock.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from . import __version__
from .baseline import Baseline, BaselineError
from .durations import duration
from .events import EventLog, RoundEvents
from .fping import Fping, PingError
from .host import DOWN, Host, Limits

ALL_DOWN_MIN_HOSTS = 5  # the "ALL DOWN" warning needs at least this many watched hosts (DECISIONS.md)
STALE_AFTER_S = 5.0  # no finished round for this long (and 3 intervals): the screen may be out of date


@dataclass
class GroupOutage:
    """From the first DOWN host of a group until none is DOWN any more."""
    group: str
    since: float
    hosts: set = field(default_factory=set)  # orders of the hosts that were DOWN in it
    until: float | None = None
    ignored: int = 0  # of those, ignored by the operator when it ended


@dataclass
class HostStats:
    host: Host
    sent: int
    lost: int
    outages: int
    longest_outage: float
    state: str


@dataclass
class Reset:
    """What r r cleared - kept for the summary (DECISIONS.md "r r keeps the record")."""
    at: float
    stats_since: float
    hosts: list[HostStats]  # the hosts that had lost pings or outages until then


@dataclass
class Ignore:
    """The last x ignore, for x u."""
    group: str
    snapshots: list[tuple[Host, dict]]


class Session:
    def __init__(self, hosts: list[Host], limits: Limits, log: EventLog, baseline: Baseline, fresh: bool):
        self.hosts = hosts
        self.limits = limits
        self.log = log
        self.baseline = baseline
        self.groups: dict[str, list[Host]] = {}
        self.by_ip: dict[str, list[Host]] = {}  # the IP that is pinged -> its hosts
        for h in hosts:
            h.recent = deque(maxlen=limits.window)
            self.groups.setdefault(h.group, []).append(h)
            if h.ip:
                self.by_ip.setdefault(h.ip, []).append(h)

        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.started = self.stats_since = time.time()
        self.rounds = 0
        self.last_round_s = 0.0
        self.last_round_end = 0.0
        self.ping_errors = 0  # rounds with errors in a row
        self.last_error = ""
        self._error_logged_at = 0.0
        self.dns_pending = 0  # hosts still waiting for their reverse lookup
        self.dns_server_down = False
        self.last_ignore: Ignore | None = None
        self.open_outages: dict[str, GroupOutage] = {}  # group -> its outage going on now
        self.outage_timeline: list[GroupOutage] = []  # every group outage, oldest first
        self.all_down_periods: list[list] = []  # [since, until or None]
        self.resets: list[Reset] = []
        self.baseline_age: float | None = None  # seconds, when a baseline was loaded
        self._baseline_changed = False

        group_count = f"{len(self.groups)} group" + ("s" if len(self.groups) != 1 else "")
        log.write(f"started pingT {__version__}: {len(hosts)} hosts in {group_count} via fping, window={limits.window} "
                  f"warn>={limits.warn} loss>={limits.loss} down>={limits.down}", "cyan")
        for h in hosts:
            if h.invalid:
                log.write(f"INVALID entry ({h.source}): {h.invalid} - not pinged", "bold magenta", host=h)
        if fresh:
            log.write(f"--fresh: ignoring previous baseline {baseline.path}", "cyan")
        else:
            self._load_baseline()

    # -- baseline
    def _load_baseline(self):
        try:
            saved = self.baseline.load()
        except BaselineError as e:
            self.log.write(f"baseline {self.baseline.path} unreadable ({e}) - starting a new one", "yellow")
            return
        if saved is None:
            return
        restored = 0
        for h in self.hosts:
            if h.target in self.baseline.answered and not h.invalid:
                h.answered_once = h.known_from_baseline = True
                restored += 1
        self.baseline_age = (datetime.now() - saved).total_seconds()
        self.log.write(f"baseline loaded: {restored} hosts answered in an earlier run (saved {saved:%Y-%m-%d %H:%M}, "
                       f"{duration(self.baseline_age)} ago) - they alarm if unreachable", "cyan")
        if self.baseline_age > 24 * 3600:
            self.log.write("baseline is older than 24h - start with --fresh if this is a new migration", "bold yellow")

    def _save_baseline(self):
        try:
            self.baseline.save(self.hosts)
            self._baseline_changed = False
        except OSError as e:
            self.log.write(f"cannot save baseline {self.baseline.path}: {e}", "yellow")

    # -- pinging
    def run_pings(self, fping: Fping, interval: float):
        """The ping loop, in its own thread until session.stop is set. It never dies: a failed
        round is shown and skipped."""
        targets = list(self.by_ip)
        while not self.stop.is_set():
            started = time.time()
            try:
                results, errors = fping.ping(targets)
                if errors and not results:
                    raise PingError(errors[0])  # nothing worked: the whole round is lost
                now = time.time()
                with self.lock:
                    if errors:  # partly failed: record what worked, show what didn't
                        self._ping_error(f"{len(targets) - len(results)} of {len(targets)} hosts not pinged "
                                         f"this round: {errors[0]}", started)
                    elif self.ping_errors:
                        self.log.write(f"pinging works again after {self.ping_errors} rounds with errors", "green")
                        self.ping_errors = 0
                    self.record_round(results, now)
                    self.last_round_s = now - started
            except Exception as e:
                with self.lock:
                    self._ping_error("round skipped: " + (str(e) if isinstance(e, PingError)
                                                          else f"{type(e).__name__}: {e}"), started)
            # after an error wait at least 0.2 s, so a failing fping can't run in a tight loop
            wait = interval - (time.time() - started)
            self.stop.wait(max(0.2 if self.ping_errors else 0.0, wait))

    def record_round(self, results: dict[str, float | None], now: float):
        """The results of one ping round: {ip: round trip in ms, or None for lost}."""
        events = RoundEvents()
        for ip, rtt in results.items():
            for h in self.by_ip.get(ip, ()):
                answered_before = h.answered_once
                for event in h.record_ping(rtt, now, self.limits):
                    events.add(h, event)
                self._baseline_changed |= h.answered_once and not answered_before
        events.write(self.log, self.groups)
        self._follow_group_outages(now)
        self._check_all_down(now)
        self.rounds += 1
        self.last_round_end = now
        if self._baseline_changed:
            self._save_baseline()

    def _ping_error(self, message: str, round_started: float):
        """Counts a round with errors; logs the first one, then at most one per minute."""
        self.ping_errors += 1
        self.last_error = message
        if self.ping_errors == 1 or round_started - self._error_logged_at >= 60:
            self.log.write(f"PING ERROR ({self.ping_errors}x): {message}", "bold white on magenta")
            self._error_logged_at = round_started

    def ping_problem(self, interval: float) -> str:
        """Why the shown states can't be trusted right now, or ""."""
        if self.ping_errors:
            return f"PING ERROR x{self.ping_errors}: {self.last_error}"
        if self.last_round_end:
            age = time.time() - self.last_round_end
            if age > max(STALE_AFTER_S, 3 * max(interval, self.last_round_s)):
                return f"STALE: no completed round for {duration(age)}"
        return ""

    def _follow_group_outages(self, now: float):
        """Opens an outage when a group gets its first DOWN host, closes it when none is left -
        "[Clients 2F] all 70 back after 7:29" and the summary's timeline."""
        for group, members in self.groups.items():
            down = [h for h in members if h.state == DOWN]
            outage = self.open_outages.get(group)
            if down:
                if outage is None:
                    outage = GroupOutage(group, since=min(h.missing_since or now for h in down))
                    self.open_outages[group] = outage
                    self.outage_timeline.append(outage)
                outage.hosts.update(h.order for h in down)
            elif outage is not None:
                del self.open_outages[group]
                outage.until = now
                outage.ignored = sum(1 for h in members if h.order in outage.hosts and h.ignored_since is not None)
                back = len(outage.hosts) - outage.ignored
                if len(outage.hosts) >= 2 and back:  # one host: its own UP line says it all
                    took = duration(now - outage.since)
                    self.log.write(f"all {back} back after {took}" if not outage.ignored else
                                   f"no DOWN hosts left after {took}: {back} back, {outage.ignored} ignored",
                                   "bold green", group=group)

    def _check_all_down(self, now: float):
        watched = [h for h in self.hosts if h.answered_once and h.ignored_since is None]
        all_down = len(watched) >= ALL_DOWN_MIN_HOSTS and all(h.state == DOWN for h in watched)
        if all_down and not self.is_all_down:
            self.all_down_periods.append([now, None])
            self.log.write(f"ALL {len(watched)} watched hosts are DOWN at once - check pingT's own network "
                           f"connection first", "bold white on magenta")
        elif self.is_all_down and not all_down:
            self.all_down_periods[-1][1] = now
            self.log.write(f"ALL DOWN over after {duration(now - self.all_down_periods[-1][0])} - "
                           f"hosts answer again", "green")

    @property
    def is_all_down(self) -> bool:
        return bool(self.all_down_periods) and self.all_down_periods[-1][1] is None

    # -- what the operator does
    def reset_stats(self, now: float):
        """r r: the counters start fresh; what they held stays in the summary."""
        had_loss = [HostStats(h, h.sent, h.lost, h.outages, current_longest_outage(h, now), h.state)
                    for h in self.hosts if h.answered_once and (h.lost or h.outages)]
        self.resets.append(Reset(now, self.stats_since, had_loss))
        for h in self.hosts:
            h.reset_stats(keep_outage=True)
        self.stats_since = now
        self.last_ignore = None  # its undo would bring back the counters from before the reset
        self.log.write("stats reset", "cyan")

    def groups_with_down_hosts(self) -> list[str]:
        return [g for g, members in self.groups.items() if any(h.state == DOWN for h in members)]

    def down_hosts(self, group: str) -> list[Host]:
        return [h for h in self.groups[group] if h.state == DOWN]

    def ignore(self, group: str, hosts: list[Host], now: float):
        """No alarm, not in "back N/M", no stats - but still pinged: a host that answers
        `limits.down` times in a row is watched again by itself."""
        self.last_ignore = Ignore(group, [(h, h.ignore(now)) for h in hosts])
        for h in hosts:
            self.baseline.forget(h)
        self._baseline_changed = True
        self.log.write(f"IGNORED {len(hosts)} DOWN hosts in [{group}] (still pinged, watched again after "
                       f"{self.limits.down} replies in a row): " + ", ".join(h.label for h in hosts), "bold yellow")

    def undo_last_ignore(self) -> tuple[str, int]:
        """x u. Hosts that came back in the meantime are left alone. Returns (group, hosts restored)."""
        ignore, self.last_ignore = self.last_ignore, None
        restored = 0
        for h, snapshot in ignore.snapshots:
            if h.ignored_since is not None:
                h.undo_ignore(snapshot)
                restored += 1
        self._baseline_changed = True
        self.log.write(f"UNDO ignore: {restored} hosts in [{ignore.group}] are watched again", "cyan")
        return ignore.group, restored


def current_longest_outage(h: Host, now: float) -> float:
    """The longest outage, counting one that is still going on."""
    if h.state == DOWN and h.missing_since:
        return max(h.longest_outage, now - h.missing_since)
    return h.longest_outage
