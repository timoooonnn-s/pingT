#!/usr/bin/env python3
"""
Timmy Pinger (pingT) - watch hundreds of hosts at once during a switch migration.

Pings every host once per round with fping and shows all of them on one screen; only
several lost pings raise an alarm. Start it with the ./pingT launcher (scan.py imports
this module too). How it works and all options: README.md.

Keys:  q quit   r r reset stats   v view   n labels/DNS names   x ignore DOWN hosts of a group
       Esc cancels a question (r r, x)
"""

from __future__ import annotations

import argparse
import ipaddress
import io
import json
import math
import os
import queue
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import termios
import threading
import time
import tty
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.table import Table
    from rich.text import Text
except ModuleNotFoundError as e:
    if (e.name or "").split(".")[0] != "rich":
        raise  # some other module is missing: show the real error
    # a clear hint instead of a traceback - also covers scan.py, which imports this module first
    sys.exit(f"{os.path.basename(sys.argv[0])}: the Python package 'rich' is missing in the Python that runs it:\n"
             f"  {sys.executable}\n"
             "Activate your Python environment (the one with 'rich' installed) and start it again.")

# --------------------------------------------------------------------------- hosts

UNKNOWN, NEVER, OK, WARN, LOSS, DOWN, INVALID, IGNORED = \
    "UNKN", "SILENT", "OK", "WARN", "LOSS", "DOWN", "INVALID", "IGNORED"
SEVERITY = {DOWN: 0, LOSS: 1, WARN: 2, INVALID: 3, UNKNOWN: 4, OK: 5, NEVER: 6, IGNORED: 7}
ASK_TIMEOUT_S = 5  # a question (r r, the x dialog) closes after this many seconds without an answer


@dataclass
class Host:
    target: str  # as written in the inventory (IP or hostname)
    label: str
    group: str
    order: int
    history: deque = field(default_factory=deque)  # recent rtts / None = lost; sized by Monitor
    sent: int = 0
    lost: int = 0
    consec_miss: int = 0
    miss_streak_start: float | None = None
    state: str = UNKNOWN
    outages: int = 0
    longest_outage: float = 0.0
    ever_up: bool = False  # baseline: has this host ever answered? (survives reset)
    from_baseline: bool = False  # ever_up restored from the baseline file of an earlier run
    where: str = ""  # "file:line" of the entry, for messages
    fqdn: str | None = None  # from reverse DNS, None = no PTR record (or not looked up yet)
    invalid: str | None = None  # reason if the entry can't be pinged
    addr: str = ""  # IP that is actually pinged: the target itself, or a hostname resolved once at start
    ignored_at: float | None = None  # set while the operator ignores this host (x key)
    consec_ok: int = 0  # replies in a row while ignored - enough of them and it's watched again
    ignore_log: list = field(default_factory=list)  # [ignored at, back at or None] per ignore

    @property
    def window_lost(self) -> int:
        return sum(1 for r in self.history if r is None)

    @property
    def window_loss_pct(self) -> float:
        return 100.0 * self.window_lost / len(self.history) if self.history else 0.0

    @property
    def who(self) -> str:
        """"label (IP)", or only the IP when it has no label."""
        return f"{self.label} ({self.target})" if self.label != self.target else self.target

    def reset(self, keep_outage: bool = False):
        """Clears the stats. keep_outage (r r): a host that is DOWN right now stays DOWN with its
        outage running since it started - it must not count as back, or alarm a second time."""
        down = keep_outage and self.state == DOWN
        streak = self.consec_miss, self.miss_streak_start
        self.history.clear()
        self.sent = self.lost = self.consec_miss = 0
        self.miss_streak_start = None
        self.state = UNKNOWN
        self.outages = 0
        self.longest_outage = 0.0
        self.consec_ok = 0
        if self.invalid:
            self.state = INVALID
        elif self.ignored_at is not None:
            self.state = IGNORED
        elif down:
            self.state, self.outages = DOWN, 1
            self.consec_miss, self.miss_streak_start = streak


def is_back(h: Host) -> bool:
    """Counts for "back N/M": answered at some point and not DOWN now (a lossy host is back)."""
    return h.ever_up and h.state != DOWN


def back_counts(hosts: list[Host]) -> tuple[int, int]:
    """(hosts reachable now = not DOWN, hosts that ever answered)"""
    return sum(1 for h in hosts if is_back(h)), sum(1 for h in hosts if h.ever_up)


def all_back(up: int, seen: int) -> bool:
    """Green "back": every host that answered at some point is back (0/0 is not green)."""
    return up == seen and seen > 0


def auto_group(target: str) -> str:
    try:
        ip = ipaddress.ip_address(target)
    except ValueError:
        return "other"
    prefix = 24 if ip.version == 4 else 64
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))


INLINE_COMMENT = re.compile(r"\s+#(\s.*)?$")  # " # note" ends a line, "Raum #12" doesn't


def parse_hosts_file(path: str) -> list[tuple[str, str, str | None, str]]:
    """Returns (target, label, group-or-None, "file:line") tuples."""
    out = []
    section = None
    with open(path, encoding="utf-8-sig") as fh:  # -sig: tolerate a BOM from Excel exports
        for n, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = INLINE_COMMENT.sub("", line).strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip() or None
                continue
            if "," in line.split(None, 1)[0]:  # CSV: ip,label,group
                cols = [c.strip() for c in line.split(",")]
                target = cols[0]
                label = cols[1] if len(cols) > 1 and cols[1] else target
                group = cols[2] if len(cols) > 2 and cols[2] else section
            else:
                parts = line.split(None, 1)
                target = parts[0]
                label = parts[1].strip() if len(parts) > 1 else target
                group = section
            out.append((target, label, group, f"{os.path.basename(path)}:{n}"))
    return out


HOSTNAME_RE = re.compile(r"^(?=.{1,253}\.?$)[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?"
                         r"(\.[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?)*\.?$")


def validate_targets(hosts: list[Host]):
    """Sets h.addr (the IP to ping), or h.invalid (and state INVALID) for entries that can't be pinged.

    Hostnames are resolved exactly once, here. fping then only gets IPs, so it doesn't
    query DNS again on every round.
    """
    to_resolve = []
    for h in hosts:
        if is_ip(h.target):
            h.addr = h.target
            continue
        if re.fullmatch(r"[\d.]+", h.target):
            h.invalid = "not a valid IPv4 address"
        elif not HOSTNAME_RE.match(h.target):
            h.invalid = "not an IP address or hostname"
        else:
            to_resolve.append(h)
    if to_resolve:
        pool = ThreadPoolExecutor(max_workers=min(32, len(to_resolve)))
        futures = [(h, pool.submit(socket.getaddrinfo, h.target, None)) for h in to_resolve]
        deadline = time.time() + 5.0  # for all names together
        for h, fut in futures:
            try:
                infos = fut.result(timeout=max(0.0, deadline - time.time()))
            except Exception:
                h.invalid = "hostname does not resolve"
                continue
            # prefer IPv4 (what these networks mostly use), otherwise the first address
            addrs = [i[4][0] for i in infos if i[0] == socket.AF_INET] or [i[4][0] for i in infos]
            if addrs:
                h.addr = addrs[0]
            else:
                h.invalid = "hostname does not resolve"
        pool.shutdown(wait=False, cancel_futures=True)
    for h in hosts:
        if h.invalid:
            h.state = INVALID


# --------------------------------------------------------------------------- fping

class PingError(Exception):
    """The ping tool itself failed - the round's results can't be trusted."""


class FpingBackend:
    """One fping process per chunk of hosts, up to `workers` of them run in parallel."""

    def __init__(self, timeout_ms: int, spacing_ms: int = 5, chunk: int = 64, workers: int = 16):
        self.timeout_ms = timeout_ms
        self.spacing_ms = spacing_ms  # ms between two probes of one fping process
        self.chunk = chunk  # hosts per fping process
        self.workers = workers
        self.bin = shutil.which("fping")
        self.pool = ThreadPoolExecutor(max_workers=workers)

    def round_time(self, n: int) -> float:
        """Worst case for n hosts (none answers): the chunks run in waves of `workers` processes."""
        waves = max(1, math.ceil(n / (self.chunk * self.workers)))
        return waves * (self.timeout_ms / 1000 + min(n, self.chunk) * self.spacing_ms / 1000)

    def _run(self, targets: list[str]) -> dict[str, float | None]:
        # -C1 -q: one probe per host, per-host summary "host : 1.23" or "host : -"
        cmd = [self.bin, "-C1", "-q", "-r0", "-B1",
               f"-t{self.timeout_ms}", f"-i{self.spacing_ms}", *targets]
        limit = self.round_time(len(targets)) + 5
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=limit)
        except subprocess.TimeoutExpired:
            raise PingError(f"fping did not finish within {limit:.0f}s ({len(targets)} hosts)") from None
        except OSError as e:
            raise PingError(f"cannot run fping: {e}") from None
        results: dict[str, float | None] = {t: None for t in targets}
        parsed = 0
        for line in (proc.stdout + proc.stderr).splitlines():
            host, sep, value = line.partition(" : ")
            host = host.strip()
            if sep and host in results:
                parsed += 1
                try:
                    results[host] = float(value.strip())
                except ValueError:
                    results[host] = None
        # exit 0 = all alive, 1 = some unreachable, 2 = some names unresolvable; 3/4 = fping error
        if proc.returncode >= 3 or parsed == 0:
            msg = [l for l in proc.stderr.strip().splitlines() if " : " not in l]
            raise PingError(f"fping failed (exit {proc.returncode}): "
                            f"{msg[-1] if msg else 'no results for any host'}")
        return results

    def round(self, targets: list[str]) -> tuple[dict[str, float | None], list[str]]:
        """Returns (results, errors). A failing chunk only loses its own hosts, not the round."""
        chunks = [targets[i:i + self.chunk] for i in range(0, len(targets), self.chunk)]
        futures = [self.pool.submit(self._run, c) for c in chunks]
        results: dict[str, float | None] = {}
        errors: list[str] = []
        for fut in futures:
            try:
                results.update(fut.result())
            except PingError as e:
                errors.append(str(e))
        return results, errors


# --------------------------------------------------------------------------- reverse dns

DNS_WORKERS = 32  # reverse DNS queries in flight at once
DNS_RETRY_S = 2  # between two rounds of retries
DNS_PAUSE_S = 60  # while the DNS server doesn't answer: one query per this many seconds
DNS_MAX_TRIES = 5  # queries without a usable answer before a host is given up


def ptr_lookup(ip: str) -> tuple[bool, str | None]:
    """One reverse lookup -> (answered, name). "No PTR record" is an answer: (True, None).
    (False, None) = no usable answer (timeout) - may work later.

    getnameinfo, not gethostbyaddr: on macOS Python runs gethostbyaddr behind a global
    lock, so parallel lookups would still go out one at a time."""
    try:
        return True, socket.getnameinfo((ip, 0), socket.NI_NAMEREQD)[0].rstrip(".") or None
    except socket.gaierror as e:
        # EAI_NONAME / EAI_NODATA = the DNS server answered "no PTR record"
        # EAI_AGAIN (timeout), EAI_FAIL (SERVFAIL) = no usable answer
        return e.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)), None
    except UnicodeError:  # a PTR record that isn't a valid name - an answer, just a useless one
        return True, None
    except OSError:
        return False, None


def resolve_names(mon: "Monitor"):
    """Reverse DNS for every host, until it gets an answer - then never again.

    All open hosts are asked at once (DNS_WORKERS in flight), names show up as they come
    in. Hosts without a usable answer are asked again in the next round; after
    DNS_MAX_TRIES they are given up (shown by their IP). When a whole round gets no answer
    and a host that did answer before (the canary) doesn't either, the DNS server counts
    as unreachable (e.g. behind the migrated switch): then only one query per DNS_PAUSE_S
    goes out until it answers again, and those rounds use up no tries.
    """
    todo: list[Host] = []
    with mon.lock:
        for h in mon.hosts:
            if not h.invalid and is_ip(h.target):
                todo.append(h)
            elif not h.invalid:
                h.fqdn = h.target  # already a name: no query needed
        mon.dns_pending = len(todo)
    if not todo:
        return
    jobs: queue.SimpleQueue[Host] = queue.SimpleQueue()
    results: queue.SimpleQueue[tuple[Host, bool, str | None]] = queue.SimpleQueue()

    def worker():
        while True:
            h = jobs.get()
            results.put((h, *ptr_lookup(h.addr)))

    for _ in range(min(DNS_WORKERS, len(todo))):  # daemon threads: a hanging lookup never blocks quitting
        threading.Thread(target=worker, daemon=True).start()

    t_start = time.time()
    tries: Counter[int] = Counter()  # by host order
    canary: str | None = None  # an IP whose lookup was answered: tells "server down" from "this host fails"
    named = given_up = 0
    while todo and not mon.stop.is_set():
        if mon.dns_paused:  # one probe; the full round only once the server answers again
            if not ptr_lookup(canary or todo[0].addr)[0]:
                mon.stop.wait(DNS_PAUSE_S)
                continue
            with mon.lock:
                mon.dns_paused = False
        for h in todo:
            jobs.put(h)
        failed: list[Host] = []
        for _ in todo:
            h, answered, name = results.get()
            if not answered:
                failed.append(h)
                continue
            canary = canary or h.addr
            named += name is not None
            with mon.lock:
                h.fqdn = name
                mon.dns_pending -= 1
        server_down = len(failed) == len(todo) and (canary is None or not ptr_lookup(canary)[0])
        if not server_down:
            for h in failed:
                tries[h.order] += 1
            given_up += sum(tries[h.order] >= DNS_MAX_TRIES for h in failed)
            failed = [h for h in failed if tries[h.order] < DNS_MAX_TRIES]  # given up: stays None = IP
        todo = failed
        with mon.lock:
            mon.dns_pending = len(todo)
            mon.dns_paused = server_down
            if not todo:
                mon.event(None, f"dns done in {fmt_dur(time.time() - t_start)}: {named} names, "
                                f"{given_up} without an answer", "dim")
        if todo:
            mon.stop.wait(DNS_PAUSE_S if server_down else DNS_RETRY_S)


def is_ip(target: str) -> bool:
    try:
        ipaddress.ip_address(target)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- monitor

class Monitor:
    def __init__(self, hosts: list[Host], backend, args, baseline_path: str):
        self.hosts = hosts
        self.by_addr: dict[str, list[Host]] = {}  # IP -> hosts pinged through it (valid hosts only)
        self.groups: dict[str, list[Host]] = {}
        for h in hosts:
            h.history = deque(maxlen=args.window)
            self.groups.setdefault(h.group, []).append(h)
            if h.addr:
                self.by_addr.setdefault(h.addr, []).append(h)
        self.backend = backend
        self.args = args
        self.lock = threading.Lock()
        self.events: deque[Text] = deque(maxlen=args.events)
        self.started = time.time()
        self.rounds = 0
        self.last_round_s = 0.0
        self.view = "compact"  # "v" toggles detail
        self.show_fqdn = False  # "n" toggles
        self.dns_pending = 0  # hosts still waiting for a reverse-DNS answer
        self.dns_paused = False  # DNS server not answering, only probing once a minute
        self.stop = threading.Event()
        self.ping_errors = 0  # consecutive failed rounds
        self.last_error = ""
        self._last_error_event = 0.0  # when a PING ERROR was last written to the log
        self.last_round_end = 0.0
        self.notice = ("", 0.0)  # (text, expires) shown in the header
        # an open question: "" none, "reset" (r r), x dialog: "pick" a group number, "confirm" by pressing it again
        self.ask = ""
        self.ask_until = 0.0  # the question closes by itself at this time
        self.ignore_keys: dict[str, str] = {}  # number key -> group, shown next to the group lines
        self.ignore_preview: list[Host] = []  # the hosts the confirm step will ignore
        self.last_ignore: tuple[str, list[tuple[Host, dict]]] | None = None  # (group, snapshots) for undo
        self.baseline_path = baseline_path
        self.baseline_seen: set[str] = set()
        self.baseline_dirty = False
        self.logfile = open(args.log, "a", encoding="utf-8")
        self.event(None, f"started: {len(hosts)} hosts in {len(self.groups)} groups via fping, "
                         f"window={args.window} warn>={args.warn} loss>={args.loss} down>={args.down}", "cyan")
        for h in hosts:
            if h.invalid:
                self.event(h, f"INVALID entry ({h.where}): {h.invalid} - not pinged", "bold magenta")
        self._load_baseline(fresh=args.fresh)

    # -- baseline: which hosts answered at some point (survives restarts/crashes)
    def _load_baseline(self, fresh: bool):
        if fresh or not os.path.exists(self.baseline_path):
            if fresh:
                self.event(None, f"--fresh: ignoring previous baseline {self.baseline_path}", "cyan")
            return
        try:
            with open(self.baseline_path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.baseline_seen = set(data.get("seen", []))
            saved = datetime.fromisoformat(data["saved"])
        except (OSError, ValueError, KeyError, TypeError) as e:
            self.event(None, f"baseline {self.baseline_path} unreadable ({e}) - starting a new one", "yellow")
            self.baseline_seen = set()
            return
        restored = 0
        for h in self.hosts:
            if h.target in self.baseline_seen and not h.invalid:
                h.ever_up = h.from_baseline = True
                restored += 1
        age = (datetime.now() - saved).total_seconds()
        self.event(None, f"baseline loaded: {restored} hosts answered in an earlier run "
                         f"(saved {saved:%Y-%m-%d %H:%M}, {fmt_dur(age)} ago) - they alarm if unreachable",
                   "cyan")
        if age > 24 * 3600:
            self.event(None, "baseline is older than 24h - start with --fresh if this is a new migration",
                       "bold yellow")

    def _save_baseline(self):
        """Atomic write (temp file + rename), so a crash mid-write can't corrupt it."""
        self.baseline_seen |= {h.target for h in self.hosts if h.ever_up}
        tmp = self.baseline_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"saved": datetime.now().isoformat(timespec="seconds"),
                           "seen": sorted(self.baseline_seen)}, fh, indent=0)
            os.replace(tmp, self.baseline_path)
            self.baseline_dirty = False
        except OSError as e:
            self.event(None, f"cannot save baseline {self.baseline_path}: {e}", "yellow")

    # -- events
    def event(self, host: Host | None, msg: str, style: str):
        now = datetime.now()
        who = host.who + " " if host else ""
        self.events.append(Text.assemble((f"{now:%H:%M:%S} ", "dim"), (who, "bold"), (msg, style)))
        grp = f"[{host.group}] " if host else ""
        self.logfile.write(f"{now.isoformat(timespec='seconds')} {grp}{who}{msg}\n")
        self.logfile.flush()

    # -- state machine
    def _evaluate(self, h: Host) -> str:
        if h.ignored_at is not None:
            return IGNORED
        if not h.history:
            return UNKNOWN
        if not h.ever_up:  # never answered: offline before we started, not an alarm
            return NEVER
        if h.consec_miss >= self.args.down:
            return DOWN
        lost = h.window_lost
        if lost >= self.args.loss:
            return LOSS
        if lost >= self.args.warn:
            return WARN
        return OK

    def _record(self, h: Host, rtt: float | None, now: float):
        if h.ignored_at is not None:  # ignored: nothing is counted, only watch for it coming back
            h.consec_ok = h.consec_ok + 1 if rtt is not None else 0
            if h.consec_ok < self.args.down:
                return
            h.ignore_log[-1][1] = now
            self.event(h, f"BACK after being ignored for {fmt_dur(now - h.ignored_at)} - watched again",
                       "bold green")
            h.ignored_at, h.consec_ok = None, 0
            h.ever_up = self.baseline_dirty = True
            h.state = UNKNOWN  # this reply is recorded below as the first one of a watched host
        if rtt is not None and not h.ever_up:
            h.ever_up = True
            self.baseline_dirty = True
            if h.state == NEVER:
                self.event(h, f"FIRST REPLY (silent for {h.consec_miss} pings)", "cyan")
            # misses from before its first reply are not loss
            h.history.clear()
            h.sent -= h.lost
            h.lost = h.consec_miss = 0
            h.miss_streak_start = None
        h.sent += 1
        streak, streak_start = h.consec_miss, h.miss_streak_start
        if rtt is not None and h.state == DOWN:
            # recovering: the outage is tracked separately (event, outages, duration),
            # so drop it from the loss window - afterwards the window shows real loss only
            for _ in range(min(streak, len(h.history))):
                h.history.pop()
        h.history.append(rtt)
        if rtt is None:
            h.lost += 1
            if h.consec_miss == 0:
                h.miss_streak_start = now
            h.consec_miss += 1
        else:
            h.consec_miss = 0
            h.miss_streak_start = None

        old, new = h.state, self._evaluate(h)
        if old == new:
            return
        h.state = new
        if new == DOWN:
            h.outages += 1
            self.event(h, f"DOWN  ({h.consec_miss} missed in a row)", "bold white on red")
        elif old == DOWN:
            dur = now - streak_start if streak_start else 0.0
            h.longest_outage = max(h.longest_outage, dur)
            self.event(h, f"UP    after {fmt_dur(dur)} ({streak} lost)", "bold green")
        elif new == LOSS:
            self.event(h, f"LOSS  {h.window_lost}/{len(h.history)} lost in window", "bold red")
        elif old == LOSS and new in (OK, WARN):
            self.event(h, f"CLEAR loss back to {h.window_lost}/{len(h.history)}", "green")

    def run_pings(self):
        targets = list(self.by_addr)  # unique IPs of all valid hosts
        while not self.stop.is_set():
            t0 = time.time()
            try:
                results, errors = self.backend.round(targets)
                if errors and not results:
                    raise PingError(errors[0])  # nothing worked: the whole round is lost
                now = time.time()
                with self.lock:
                    if errors:  # partly failed: record what worked, report what didn't
                        self._ping_error(f"{len(targets) - len(results)} of {len(targets)} hosts not pinged "
                                         f"this round: {errors[0]}", t0)
                    elif self.ping_errors:
                        self.event(None, f"pinging works again after {self.ping_errors} rounds with errors",
                                   "green")
                        self.ping_errors = 0
                    for addr, rtt in results.items():
                        for h in self.by_addr.get(addr, ()):
                            self._record(h, rtt, now)
                    self.rounds += 1
                    self.last_round_s = now - t0
                    self.last_round_end = now
                    if self.baseline_dirty:
                        self._save_baseline()
            except Exception as e:  # never let the ping thread die: skip the round, show it, retry
                with self.lock:
                    self._ping_error("round skipped: " + (str(e) if isinstance(e, PingError)
                                                          else f"{type(e).__name__}: {e}"), t0)
            # after an error wait at least 0.2 s, so a failing fping can't spin in a tight loop
            delay = self.args.interval - (time.time() - t0)
            self.stop.wait(max(0.2 if self.ping_errors else 0.0, delay))

    def _ping_error(self, msg: str, t0: float):
        """Count a round with errors; log the first one and then at most once a minute."""
        self.ping_errors += 1
        self.last_error = msg
        if self.ping_errors == 1 or t0 - self._last_error_event >= 60:
            self.event(None, f"PING ERROR ({self.ping_errors}x): {msg}", "bold white on magenta")
            self._last_error_event = t0

    def ping_problem(self) -> str:
        """Why the shown data can't be trusted right now, or ''."""
        if self.ping_errors:
            return f"PING ERROR x{self.ping_errors}: {self.last_error}"
        if self.last_round_end:
            age = time.time() - self.last_round_end
            if age > max(5.0, 3 * max(self.args.interval, self.last_round_s)):
                return f"STALE: no completed round for {fmt_dur(age)}"
        return ""

    # -- keys: q v n act at once; r and x ask a question first (Esc or ASK_TIMEOUT_S cancels it)
    def key(self, key: str) -> bool:
        """Handles one key. True = quit."""
        now = time.time()
        with self.lock:
            if self.ask:
                self._answer(key, now)
                return False
            self.notice = ("", 0.0)
            if key == "q":
                return True
            if key == "r":
                self._ask("reset", "press r again to RESET all stats, Esc cancels", now)
            elif key == "x":
                self._ignore_open(now)
            elif key == "v":
                self.view = "detail" if self.view == "compact" else "compact"
            elif key == "n":
                if self.args.dns or self.show_fqdn:
                    self.show_fqdn = not self.show_fqdn
                else:
                    self.notice = ("reverse DNS is off (--no-dns)", now + 3)
        return False

    def expire(self):
        """Closes a question nobody answered."""
        with self.lock:
            if self.ask and time.time() > self.ask_until:
                self._close(f"cancelled (no key for {ASK_TIMEOUT_S}s)")

    def _ask(self, step: str, text: str, now: float):
        self.ask, self.ask_until = step, now + ASK_TIMEOUT_S
        self.notice = (text, self.ask_until)

    def _close(self, notice: str = ""):
        self.ask, self.ignore_keys, self.ignore_preview = "", {}, []
        self.notice = (notice, time.time() + 3) if notice else ("", 0.0)

    def _answer(self, key: str, now: float):
        """A key while a question is open: its answer, Esc = cancel, anything else is ignored
        (a stray key or arrow key must not answer or cancel it)."""
        if key == "esc":
            self._close("cancelled")
        elif self.ask == "reset" and key == "r":
            self._close("stats reset")
            self._reset()
        elif self.ask == "pick" and key == "u" and self.last_ignore:
            self._close()
            self._undo_ignore()
        elif self.ask == "pick" and key in self.ignore_keys:
            self._ignore_preview(key, now)
        elif self.ask == "confirm" and key in self.ignore_keys:
            group, hosts = self.ignore_keys[key], [h for h in self.ignore_preview if h.state == DOWN]
            self._close()
            if hosts:
                self._ignore(group, hosts, now)
                self.notice = (f'ignored {len(hosts)} hosts in "{group}" - x u undoes it', now + 5)
            else:
                self.notice = (f'no DOWN hosts left in "{group}" - nothing ignored', now + 3)

    def _reset(self):
        for h in self.hosts:
            h.reset(keep_outage=True)
        self.started = time.time()
        self.rounds = 0
        self.event(None, "stats reset", "cyan")

    # -- ignore: the operator takes the DOWN hosts of a group out of the alarms (clients that went home)
    def _ignore_open(self, now: float):
        groups = [g for g, members in self.groups.items() if any(h.state == DOWN for h in members)]
        undo = (f"u = undo last ignore ({len(self.last_ignore[1])} in {self.last_ignore[0]})"
                if self.last_ignore else "")
        if not groups and not undo:
            self.notice = ("no DOWN hosts to ignore", now + 3)
            return
        self.ignore_keys = {str((i + 1) % 10): g for i, g in enumerate(groups[:10])}  # 1..9, 0
        self.view = "compact"  # the numbers are shown next to the group lines
        if groups:
            text = "IGNORE the DOWN hosts of a group: press its number"
            if len(groups) > 10:
                text += f" (first 10 of {len(groups)} groups)"
            text += (", " + undo if undo else "") + ", Esc cancels"
        else:
            text = f"no DOWN hosts to ignore - {undo}, Esc cancels"
        self._ask("pick", text, now)

    def _ignore_preview(self, key: str, now: float):
        group = self.ignore_keys[key]
        hosts = [h for h in self.groups[group] if h.state == DOWN]
        if not hosts:
            self._close(f'no DOWN hosts left in "{group}"')
            return
        downs = sorted(now - h.miss_streak_start for h in hosts if h.miss_streak_start)
        span = ""
        if downs:
            span = f" (down {fmt_dur(downs[0])}" + (f" to {fmt_dur(downs[-1])})" if len(downs) > 1 else ")")
        self.ignore_keys, self.ignore_preview = {key: group}, hosts
        self._ask("confirm", f'ignore {len(hosts)} DOWN hosts in "{group}"{span}? '
                             f'press {key} again to confirm, Esc cancels', now)

    def _ignore(self, group: str, hosts: list[Host], now: float):
        """No alarm, not in "back N/M", no stats - but still pinged, see _record."""
        snapshots = []
        for h in hosts:
            snapshots.append((h, {k: getattr(h, k) for k in UNDO_FIELDS} | {"history": h.history.copy()}))
            h.reset()  # the outage (user went home) is not migration damage; the event log keeps it
            h.ever_up = h.from_baseline = False
            h.ignored_at, h.consec_ok, h.state = now, 0, IGNORED
            h.ignore_log.append([now, None])
            self.baseline_seen.discard(h.target)  # a restart must not bring it back as DOWN
        self.baseline_dirty = True
        self.last_ignore = (group, snapshots)
        self.event(None, f"IGNORED {len(hosts)} DOWN hosts in [{group}] (still pinged, watched again after "
                         f"{self.args.down} replies in a row): " + ", ".join(h.label for h in hosts),
                   "bold yellow")

    def _undo_ignore(self):
        group, snapshots = self.last_ignore
        self.last_ignore = None
        restored = 0
        for h, snap in snapshots:
            if h.ignored_at is None:  # came back in the meantime - it's watched already
                continue
            for k, v in snap.items():
                setattr(h, k, v)
            h.ignored_at, h.consec_ok = None, 0
            h.ignore_log.pop()
            restored += 1
        self.baseline_dirty = True
        self.event(None, f"UNDO ignore: {restored} hosts in [{group}] are watched again", "cyan")
        self.notice = (f'undone: {restored} hosts in "{group}" are watched again', time.time() + 5)

    # -- rendering
    def render(self, width: int, height: int):
        with self.lock:
            if self.view == "compact":
                body, used = self._compact(width, height - 1)
            else:  # every host as a cell, events get at most 3 lines
                grid, used = self._cells(self.hosts, width, max(1, height - 1 - min(self.args.events, 3)),
                                         with_target=False)
                body = [grid]
            parts = [self._header(width), *body]
            self._append_events(parts, width, height - 1 - used)
            return Group(*parts)

    def _header(self, width: int) -> Text:
        """Top line; the least important parts are dropped first when it doesn't fit."""
        counts = Counter(h.state for h in self.hosts)
        up, seen = back_counts(self.hosts)
        problem = self.ping_problem()
        notice, expires = self.notice
        # (priority, text, style) - higher priority is dropped first, 0 never
        segs: list[tuple[int, str, str]] = [(0, " pingT ", "bold black on cyan")]
        if problem:
            segs.append((0, f" {problem} ", "bold white on magenta"))
        if notice and time.time() < expires:
            segs.append((0, f" {notice} ", "bold black on yellow"))
        segs += [
            (0, f" back {up}/{seen} ", "bold black on green" if all_back(up, seen) else "bold white on red"),
            (1, f"ok {counts[OK]}", "green"),
            (1, f"warn {counts[WARN]}", "yellow"),
            (1, f"loss {counts[LOSS]}", "red"),
            (0, f"down {counts[DOWN]}", "bold white on red" if counts[DOWN] else "red"),
            (2, f"silent {counts[NEVER]}", "dim"),
        ]
        if counts[IGNORED]:
            segs.append((1, f"ignored {counts[IGNORED]}", "yellow"))
        if counts[INVALID]:
            segs.append((1, f"invalid {counts[INVALID]}", "bold magenta"))
        segs.append((2, f"names:{'FQDN' if self.show_fqdn else 'label'}",
                     "bold cyan" if self.show_fqdn else "dim"))
        if self.dns_pending:
            segs.append((3, f"dns no answer, {self.dns_pending} left (probing 1/min)" if self.dns_paused
                            else f"dns {self.dns_pending} left", "yellow" if self.dns_paused else "dim"))
        segs.append((3, f"fping {fmt_dur(time.time() - self.started)} "
                        f"#{self.rounds} {self.last_round_s:.1f}s", "dim"))
        segs.append((4, "[q]uit [r]eset [v]iew [n]ames [x]ignore", "dim"))

        keep = segs
        while sum(len(t) + 1 for _, t, _ in keep) > width:
            worst = max(pri for pri, _, _ in keep)
            if worst == 0:
                break
            keep = [seg for seg in keep if seg[0] != worst]
        out = Text(no_wrap=True, overflow="ellipsis")
        for i, (_, text, style) in enumerate(keep):
            if i:
                out.append(" ")
            out.append(text, style)
        return out

    def _append_events(self, parts: list, width: int, lines: int):
        """Event panel: separator + most recent events, limited to --events and free lines."""
        lines = min(lines, self.args.events)
        if lines <= 0:
            return
        if lines > 1:
            parts.append(Text("─ events " + "─" * max(0, width - 9), style="dim"))
            lines -= 1
        for e in list(self.events)[-lines:]:
            e = e.copy()
            e.truncate(width, overflow="ellipsis")
            parts.append(e)

    # compact view: one glyph per host per group line + problem list
    def _compact(self, width: int, height: int) -> tuple[list, int]:
        name_w = min(22, max(len(g) for g in self.groups))
        marks = {g: k for k, g in self.ignore_keys.items()}  # x dialog: number key per group
        mark_w = 2 if marks else 0
        prefix_w = mark_w + name_w + 1 + 8  # "name 123/456 "
        per_line = max(10, ((width - prefix_w + 1) // 11) * 10)  # blocks of 10 glyphs + space
        lines: list[Text] = []
        for gname, members in self.groups.items():
            # up / hosts that ever answered (silent-from-the-start hosts don't count)
            up, seen = back_counts(members)
            worst = min((h.state for h in members), key=SEVERITY.get)
            label = gname if len(gname) <= name_w else gname[: name_w - 1] + "…"
            for start in range(0, len(members), per_line):
                t = Text(no_wrap=True, overflow="crop")
                if start == 0:
                    if mark_w:
                        t.append(marks.get(gname, " "), "bold black on yellow" if gname in marks else "")
                        t.append(" ")
                    t.append(label.ljust(name_w) + " ", STATE_STYLES[worst][1] or "bold")
                    t.append(f"{up:>3}/{seen:<3} ",
                             "green" if all_back(up, seen) else STATE_STYLES[worst][0])
                else:
                    t.append(" " * prefix_w)
                for i, h in enumerate(members[start:start + per_line]):
                    if i and i % 10 == 0:
                        t.append(" ")
                    t.append(*GLYPHS[h.state])
                lines.append(t)

        problems = sorted((h for h in self.hosts if h.state in (DOWN, LOSS, WARN, INVALID)),
                          key=lambda h: (SEVERITY[h.state], -h.window_lost, h.order))  # worst first
        free = height - len(lines)
        # problems get the rows they need at full detail; events get what's left (min 2 lines)
        if problems:
            name_w, hist_w, show_pct = CELL_LAYOUTS[0]
            full_cols = max(1, (width + 1) // (cell_width(name_w + 16, hist_w, show_pct) + 1))
            wanted = math.ceil(len(problems) / full_cols)
            reserve = min(self.args.events, max(2, free - 1 - wanted))
        else:
            reserve = min(self.args.events, free - 1)
        prob_lines = max(0, free - reserve - 1)  # -1 for the separator
        out: list = list(lines)
        used = len(lines)
        if prob_lines > 0:
            title = f"─ problems ({len(problems)}) " if problems else "─ no problems "
            out.append(Text(title + "─" * max(0, width - len(title)),
                            style="bold red" if problems else "dim green"))
            used += 1
            if problems:
                grid, rows = self._cells(problems, width, prob_lines, with_target=True)
                out.append(grid)
                used += rows
        return out, used

    def _cells(self, hosts: list[Host], width: int, max_rows: int, with_target: bool):
        """Grid of host cells using the most detailed layout that fits in max_rows."""
        # size from BOTH name variants, so toggling label/FQDN keeps every host in the same cell
        name_len = max(max(len(self._name(h, with_target, fqdn=False)),
                           len(self._name(h, with_target, fqdn=True))) for h in hosts)
        for name_w, hist_w, show_pct in CELL_LAYOUTS:
            name_w = min(name_w + (16 if with_target else 0), max(4, name_len))
            cell_w = cell_width(name_w, hist_w, show_pct)
            cols = max(1, (width + 1) // (cell_w + 1))
            if cols * max_rows >= len(hosts):
                break
        rows = min(max_rows, math.ceil(len(hosts) / cols))
        capacity = rows * cols
        shown = hosts if len(hosts) <= capacity else hosts[: capacity - 1]
        cells = [self._cell(h, name_w, hist_w, show_pct, with_target) for h in shown]
        if len(shown) < len(hosts):
            cells.append(Text(f"… +{len(hosts) - len(shown)} more", style="bold red"))
        grid = Table.grid(padding=(0, 1))
        for _ in range(cols):
            grid.add_column(width=cell_w, no_wrap=True)
        for r in range(rows):  # column-major: reads top-to-bottom like the hosts file
            grid.add_row(*[cells[c * rows + r] if c * rows + r < len(cells) else Text("")
                           for c in range(cols)])
        return grid, rows

    def _name(self, h: Host, with_target: bool, fqdn: bool | None = None) -> str:
        """Display name; fqdn=None means the current mode (the 'n' toggle)."""
        if self.show_fqdn if fqdn is None else fqdn:
            name = h.fqdn or h.target  # no PTR record (or not looked up yet) -> IP
            return f"{name} {h.target}" if with_target and name != h.target else name
        if with_target and h.label != h.target:
            return f"{h.label} {h.target}"
        return h.label

    def _cell(self, h: Host, name_w: int, hist_w: int, show_pct: bool, with_target: bool) -> Text:
        dot_style, name_style = STATE_STYLES[h.state]
        t = Text(no_wrap=True, overflow="crop")
        t.append({DOWN: "✖ ", INVALID: "? ", IGNORED: "- "}.get(h.state, "● "), dot_style)
        name = self._name(h, with_target)
        name = name if len(name) <= name_w else name[: name_w - 1] + "…"
        t.append(name.ljust(name_w), name_style)
        if h.state == INVALID:
            t.append(f" invalid: {h.invalid}", "magenta")
            return t
        if show_pct:
            pct = h.window_loss_pct
            t.append(f"{pct:4.0f}%" if h.history else "   -%",
                     "dim" if pct == 0 else ("yellow" if h.state != DOWN else "bold red"))
        if hist_w:
            t.append(" ")
            recent = list(h.history)[-hist_w:]
            t.append("·" * (hist_w - len(recent)), "dim")
            for rtt in recent:
                t.append(*(("×", "bold red") if rtt is None else spark(rtt)))
        return t


# what an ignore changes on a host - restored by undo (history is copied separately)
UNDO_FIELDS = ("sent", "lost", "consec_miss", "miss_streak_start", "state", "outages", "longest_outage",
               "ever_up", "from_baseline")


def cell_width(name_w: int, hist_w: int, show_pct: bool) -> int:
    """Characters of one host cell: dot + name + loss % + recent pings."""
    return 2 + name_w + (5 if show_pct else 0) + (1 + hist_w if hist_w else 0)


# (name width, history width, show loss %) from most to least detailed
CELL_LAYOUTS = [(18, 20, True), (16, 15, True), (14, 10, True), (12, 8, True), (12, 5, True),
                (10, 0, True), (8, 0, True), (6, 0, True), (6, 0, False), (4, 0, False)]

STATE_STYLES = {
    UNKNOWN: ("dim", "dim"),
    NEVER: ("dim", "dim"),
    IGNORED: ("dim", "dim"),
    INVALID: ("bold magenta", "magenta"),
    OK: ("green", ""),
    WARN: ("yellow", "yellow"),
    LOSS: ("bold red", "bold red"),
    DOWN: ("bold white on red", "bold white on red"),
}

GLYPHS = {
    UNKNOWN: ("·", "dim"),
    NEVER: ("○", "dim"),
    IGNORED: ("-", "dim"),
    INVALID: ("?", "bold magenta"),
    OK: ("●", "green"),
    WARN: ("●", "yellow"),
    LOSS: ("●", "bold red"),
    DOWN: ("X", "bold white on red"),
}

SPARK = [(1, "▁"), (2, "▂"), (5, "▃"), (10, "▄"), (30, "▅"), (100, "▆"), (math.inf, "▇")]


def spark(rtt: float) -> tuple[str, str]:
    return next(ch for limit, ch in SPARK if rtt < limit), ("green" if rtt < 30 else "yellow")


def fmt_dur(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- keyboard

# one key: an escape sequence (arrow keys, F-keys: ESC [ ... or ESC O x), a lone ESC, or one character
KEY_SEQ = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|O.)?|.", re.S)
KEY_SEQ_OPEN = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*|O)?\Z")  # input ends inside an escape sequence


class Keyboard:
    """Single-key input without Enter (POSIX terminals only)."""

    def __init__(self):
        self.enabled = sys.stdin.isatty()
        self.old = None

    def __enter__(self):
        if self.enabled:
            self.old = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        return self

    def __exit__(self, *exc):
        if self.old is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.old)

    def keys(self, timeout: float) -> list[str]:
        """The keys pressed within timeout, lower case, Esc as "esc". Escape sequences
        (arrow keys, F-keys) are dropped, so they can't answer or cancel anything."""
        if not self.enabled:
            time.sleep(timeout)
            return []
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            return []
        data = os.read(sys.stdin.fileno(), 64)
        if not data:  # stdin closed: stop reading, or select would return at once forever
            self.enabled = False
        # a slow link can split an arrow key: wait briefly for the rest, or its ESC counts as Esc
        while KEY_SEQ_OPEN.search(data) and select.select([sys.stdin], [], [], 0.1)[0]:
            more = os.read(sys.stdin.fileno(), 64)
            if not more:
                break
            data += more
        data = data.decode(errors="ignore")
        return ["esc" if k == "\x1b" else k.lower() for k in KEY_SEQ.findall(data)
                if k == "\x1b" or not k.startswith("\x1b")]


# --------------------------------------------------------------------------- main

def print_summary(console: Console, mon: Monitor):
    """After exit: every host that lost anything, worst first, plus per-group totals."""
    def host_line(h: Host, color: str, rest: str):
        console.print(f"  [{color}]{h.target:<16}[/{color}] {h.fqdn or h.label:<30} {rest}")

    up, seen = back_counts(mon.hosts)
    console.print(f"\n[bold]Summary[/bold] - {fmt_dur(time.time() - mon.started)}, {mon.rounds} rounds, "
                  f"[{'green' if all_back(up, seen) else 'bold red'}]back {up}/{seen}[/] "
                  f"hosts that answered at some point")
    gt = Table(header_style="bold")
    for col in ("group", "hosts", "back", "not back", "silent", "ignored", "with loss", "outages", "loss %"):
        gt.add_column(col, justify="left" if col == "group" else "right")
    for gname, members in mon.groups.items():
        back, seen_n = back_counts(members)
        seen_m = [h for h in members if h.ever_up]
        sent = sum(h.sent for h in seen_m)
        lost = sum(h.lost for h in seen_m)
        affected = sum(1 for h in seen_m if h.lost)
        outages = sum(h.outages for h in seen_m)
        gt.add_row(gname, str(len(members)), str(back), str(seen_n - back),
                   str(sum(1 for h in members if h.state == NEVER)),
                   str(sum(1 for h in members if h.state == IGNORED)), str(affected), str(outages),
                   f"{100.0 * lost / sent if sent else 0:.2f}",
                   style="bold red" if back < seen_n else ("yellow" if affected else
                                                           "green" if seen_n else "dim"))
    console.print(gt)

    problem = mon.ping_problem()
    if problem:
        console.print(f"[bold white on magenta] {problem} [/] - the last states may be outdated")
    not_back = [h for h in mon.hosts if h.ever_up and not is_back(h)]
    if not_back:
        console.print(f"[bold red]NOT BACK ({len(not_back)}):[/bold red] answered earlier, not answering now")
        for h in not_back:
            note = "  (known from baseline, no reply in this run)" if h.from_baseline and h.sent == h.lost else ""
            host_line(h, "red", f"[dim]{h.group}[/dim]  {h.state}{note}")
    invalid = [h for h in mon.hosts if h.invalid]
    if invalid:
        console.print(f"[bold magenta]INVALID entries, not pinged ({len(invalid)}):[/bold magenta]")
        for h in invalid:
            host_line(h, "magenta", f"[dim]{h.where}[/dim]  {h.invalid}")
    ignored = [h for h in mon.hosts if h.ignore_log]
    if ignored:
        still = sum(1 for h in ignored if h.ignored_at is not None)
        console.print(f"[bold yellow]IGNORED by operator ({len(ignored)}):[/bold yellow] "
                      f"{still} still ignored (no reply since), {len(ignored) - still} came back (watched again)")
        for h in sorted(ignored, key=lambda h: (h.ignored_at is None, h.order)):
            when = ", ".join(f"ignored {datetime.fromtimestamp(at):%H:%M}"
                             + (f" back {datetime.fromtimestamp(back):%H:%M}" if back else "")
                             for at, back in h.ignore_log)
            status = "still ignored" if h.ignored_at is not None else f"back, {h.state}"
            host_line(h, "yellow", f"[dim]{h.group}[/dim]  {status}  [dim]{when}[/dim]")
    silent = [h for h in mon.hosts if not h.ever_up and not h.invalid and h.ignored_at is None]
    if silent:
        console.print(f"[dim]Silent the whole time ({len(silent)}): "
                      + ", ".join(h.who for h in silent) + "[/dim]")

    # h.outages without h.lost: DOWN at an r r reset, back before it missed another ping
    bad = sorted((h for h in mon.hosts if h.ever_up and (h.lost or h.outages)),
                 key=lambda h: (-h.outages, -h.lost, h.order))
    if not bad:
        console.print("[green]No packet loss on any responding host.[/green]")
        return
    with_fqdn = any(h.fqdn and h.fqdn != h.label for h in bad)
    cols = ["host"] + (["fqdn"] if with_fqdn else []) + \
           ["target", "group", "state", "sent", "lost", "loss %", "outages", "longest"]
    table = Table(title=f"Hosts with loss ({len(bad)})", header_style="bold")
    for col in cols:
        table.add_column(col, justify="right" if col in ("sent", "lost", "loss %", "outages", "longest") else "left")
    now = time.time()
    for h in bad:
        longest = h.longest_outage
        if h.state == DOWN and h.miss_streak_start:  # still down: count the ongoing outage
            longest = max(longest, now - h.miss_streak_start)
        pct = 100.0 * h.lost / h.sent if h.sent else 0.0
        row = [h.label] + ([h.fqdn or "-"] if with_fqdn else []) + \
              [h.target, h.group, h.state, str(h.sent), str(h.lost), f"{pct:.1f}",
               str(h.outages), fmt_dur(longest) if h.outages else "-"]
        table.add_row(*row, style="red" if h.outages or h.state == DOWN else "yellow")
    console.print(table)


def save_summary(mon: Monitor, path: str) -> str | None:
    """Plain-text copy of the summary; returns an error message or None."""
    buf = io.StringIO()
    print_summary(Console(file=buf, width=140, color_system=None, force_terminal=False), mon)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"Timmy Pinger summary, written {datetime.now():%Y-%m-%d %H:%M:%S}\n")
            fh.write(buf.getvalue())
    except OSError as e:
        return str(e)
    return None


def main():
    ap = argparse.ArgumentParser(prog="pingT", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="*", help="hosts/IPs to ping (in addition to -f)")
    ap.add_argument("-f", "--file", action="append", default=[], help="hosts file (repeatable)")
    ap.add_argument("-i", "--interval", type=float, default=1.0, help="seconds between rounds (default 1.0)")
    ap.add_argument("-t", "--timeout", type=int, default=800, help="reply timeout in ms (default 800)")
    ap.add_argument("-w", "--window", type=int, default=20, help="rolling window size in pings (default 20)")
    ap.add_argument("--warn", type=int, default=2, help="lost in window -> WARN (default 2)")
    ap.add_argument("--loss", type=int, default=3, help="lost in window -> LOSS (default 3)")
    ap.add_argument("--down", type=int, default=3, help="consecutive misses -> DOWN (default 3)")
    ap.add_argument("--events", type=int, default=8, help="event lines on screen (default 8, 0=off)")
    ap.add_argument("--log", default="pingT-events.log",
                    help="append events to this file (default pingT-events.log); "
                         "the summary and baseline files are named after it")
    ap.add_argument("--fresh", action="store_true", help="ignore the saved baseline and start a new one")
    ap.add_argument("--no-dns", dest="dns", action="store_false",
                    help="no reverse DNS lookups at all")
    args = ap.parse_args()

    checks = [
        (args.window >= 1, "-w/--window must be >= 1"),
        (1 <= args.warn <= args.loss <= args.window, "need 1 <= --warn <= --loss <= --window"),
        (args.down >= 1, "--down must be >= 1"),
        (args.interval >= 0.2, "-i/--interval must be >= 0.2 s"),
        (50 <= args.timeout <= 10000, "-t/--timeout must be 50..10000 ms"),
        (args.events >= 0, "--events must be >= 0"),
    ]
    for ok, msg in checks:
        if not ok:
            ap.error(msg)
    if not shutil.which("fping"):
        ap.error("fping not found in PATH - install it first (apt install fping)")
    stem = os.path.splitext(os.path.abspath(args.log))[0]  # pingT-events -> -baseline.json, -summary-...

    entries = []
    for path in args.file:
        try:
            entries += parse_hosts_file(path)
        except OSError as e:
            ap.error(f"cannot read hosts file: {e}")
    entries += [(t, t, None, "cmdline") for t in args.targets]
    seen, hosts = set(), []
    for target, label, group, where in entries:
        if target not in seen:  # duplicates: first entry wins, the address is pinged once
            seen.add(target)
            hosts.append(Host(target, label, group or auto_group(target), len(hosts), where=where))
    if not hosts:
        ap.error("no hosts given (use -f hosts.txt and/or list targets)")

    validate_targets(hosts)
    invalid = [h for h in hosts if h.invalid]
    for h in invalid:
        print(f"WARNING {h.where}: '{h.target}' {h.invalid} - not pinged", file=sys.stderr)
    if len(invalid) == len(hosts):
        ap.error("no valid hosts to ping")

    backend = FpingBackend(args.timeout)
    needed = backend.round_time(len(hosts) - len(invalid))  # worst case: every host times out
    if needed > 1.5 * args.interval:
        print(f"note: with many hosts down a round can take ~{needed:.1f}s > interval {args.interval}s; "
              f"rounds then run back-to-back", file=sys.stderr)
    if invalid and sys.stdin.isatty():
        time.sleep(3)  # let the warnings be read before the dashboard takes over the screen

    def on_signal(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_signal)  # kill -> still save the summary
    signal.signal(signal.SIGHUP, on_signal)  # SSH session dropped -> still save the summary

    console = Console()
    try:
        mon = Monitor(hosts, backend, args, f"{stem}-baseline.json")
    except OSError as e:
        ap.error(f"cannot open log file: {e}")
    ping_thread = threading.Thread(target=mon.run_pings, daemon=True)
    ping_thread.start()
    if args.dns:
        threading.Thread(target=resolve_names, args=(mon,), daemon=True).start()

    try:
        with Keyboard() as kb, Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                live.update(mon.render(*console.size), refresh=True)
                keys = kb.keys(0.5)
                mon.expire()  # first, so a late key can't answer a question that timed out
                if any(mon.key(k) for k in keys):  # True = q
                    break
    except (KeyboardInterrupt, OSError):  # OSError: terminal went away
        pass
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):  # a 2nd signal must not cut the summary short
            signal.signal(sig, signal.SIG_IGN)
        mon.stop.set()
        ping_thread.join(timeout=backend.round_time(len(hosts)) + 5)
        summary_path = f"{stem}-summary-{datetime.now():%Y%m%d-%H%M%S}.txt"  # named when it stops
        with mon.lock:
            mon.event(None, "stopped", "cyan")
            err = save_summary(mon, summary_path)  # file first - the terminal may be gone
            try:
                print_summary(console, mon)
                console.print(f"[dim]events logged to {os.path.abspath(args.log)}[/dim]")
                console.print(f"[dim]summary saved to {summary_path}[/dim]" if not err
                              else f"[red]could not save summary: {err}[/red]")
            except OSError:
                pass


if __name__ == "__main__":
    main()
