#!/usr/bin/env python3
"""
Timmy Pinger (pingT) - watch hundreds of hosts at once during a switch migration.

Start it with the ./pingT launcher; this module holds the code (scan.py imports it).

Pings every host once per round with fping,
keeps a rolling window per host and only raises an alarm after several misses:

  OK    green   < --warn lost in the window          (a single stray drop is ignored)
  WARN  yellow  >= --warn lost in the window         (repeated drops)
  LOSS  red     >= --loss lost in the window         (real packet loss)
  DOWN  red bg  >= --down consecutive misses         (host unreachable)

Hosts file (one host per line, '#' comments), any of:
  [Server VLAN 10]              section header = group for following hosts
  10.0.10.5   core-sw01         ip-or-name  [label]
  10.0.20.7,printer-2f,Office   CSV: ip,label,group
Hosts without a group are grouped by their /24.

A host that has never answered is SILENT (dim, no alarm) - it was already offline
before the migration. "back N/M" = hosts reachable now out of all hosts that
answered at some point. Hosts that answered are remembered in a baseline file
next to the log, so after a restart/crash they alarm as DOWN instead of SILENT
(--fresh starts a new baseline). Invalid entries are reported, not pinged.

Reverse DNS: at most ONE PTR query per host for its whole runtime (cached, throttled
to --dns-rate queries/s, off with --no-dns); "n" then shows FQDNs instead of labels,
falling back to the IP when a host has no PTR record.

Operator can IGNORE the DOWN hosts of a group (x, group number, same number again) -
e.g. clients whose users went home before the cut-over. They stay pinged and are watched
again after --down replies in a row; the summary lists them.

Keys:  q quit   r r reset stats (press twice)   v compact/detail view   n labels/FQDN
       s sort (detail view)   x ignore DOWN hosts of a group (x u = undo)
"""

from __future__ import annotations

import argparse
import ipaddress
import io
import json
import math
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
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
IGNORE_CONFIRM_S = 5  # the x dialog closes after this many seconds without a key


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

    def reset(self):
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


def is_back(h: Host) -> bool:
    """Counts for "back N/M": answered at some point and not DOWN now (a lossy host is back)."""
    return h.ever_up and h.state != DOWN


def problem_order(h: Host):
    """Sort key: worst state first, then most loss in the window, then inventory order."""
    return SEVERITY[h.state], -h.window_lost, h.order


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
            first = line.split(None, 1)[0]
            delim = ";" if ";" in first else ("," if "," in first else None)
            if delim:  # CSV: ip,label,group  (or ; from Excel in some locales)
                cols = [c.strip() for c in line.split(delim)]
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


def validate_targets(hosts: list[Host], dns_timeout: float = 5.0):
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
        deadline = time.time() + dns_timeout
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


# --------------------------------------------------------------------------- backends

class PingError(Exception):
    """The ping tool itself failed - the round's results can't be trusted."""


FPING_SPACING_MS = 5  # ms between two probes of one fping process
FPING_CHUNK = 64  # hosts per fping process; the processes run in parallel


class FpingBackend:
    """One fping process per chunk of hosts, chunks run in parallel."""
    name = "fping"

    def __init__(self, timeout_ms: int, spacing_ms: int, chunk: int, workers: int = 16):
        self.timeout_ms = timeout_ms
        self.spacing_ms = spacing_ms
        self.chunk = chunk
        self.bin = shutil.which("fping")
        self.pool = ThreadPoolExecutor(max_workers=workers)

    def round_time(self, n: int) -> float:
        return self.timeout_ms / 1000 + min(n, self.chunk) * self.spacing_ms / 1000

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

def ptr_lookup(ip: str) -> tuple[str, str | None]:
    """One reverse lookup -> ("ok", name) / ("none", None) = definitive / ("retry", None) = no answer."""
    try:
        return "ok", socket.gethostbyaddr(ip)[0].rstrip(".") or None
    except socket.herror as e:
        # h_errno: 1 HOST_NOT_FOUND, 4 NO_DATA = the DNS server answered "no PTR record"
        # 2 TRY_AGAIN (timeout), 3 NO_RECOVERY (SERVFAIL) = no usable answer, may work later
        return ("none", None) if e.errno in (1, 4) else ("retry", None)
    except socket.gaierror as e:
        definitive = {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)}
        return ("none", None) if e.errno in definitive else ("retry", None)
    except UnicodeError:  # a PTR record that isn't a valid name - an answer, just a useless one
        return "none", None
    except OSError:
        return "retry", None


def resolve_names(mon: "Monitor", rate: float, max_tries: int = 5, pause_s: float = 60.0):
    """Reverse DNS for every host: one answered query per host, ever.

    An answer ("name" or "no PTR record") is final and never asked again. Only when the
    DNS server gives no usable answer (timeout, SERVFAIL) is that host tried again later,
    at most max_tries times. After 3 unanswered queries in a row the DNS server counts as
    unreachable: then only one probe per pause_s goes out until it answers again.
    """
    todo: deque[tuple[Host, int]] = deque()
    with mon.lock:
        for h in mon.hosts:
            if h.invalid or not is_ip(h.target):
                h.fqdn = None if h.invalid else h.target  # already a name: no query needed
            else:
                todo.append((h, 0))
        mon.dns_pending = len(todo)
    unanswered_in_row = 0
    while todo and not mon.stop.is_set():
        h, tries = todo.popleft()
        t0 = time.time()
        status, name = ptr_lookup(h.addr)
        tries += 1
        if status == "retry" and tries < max_tries:
            todo.append((h, tries))  # try the others first, this one again later
            unanswered_in_row += 1
        else:
            unanswered_in_row = 0 if status != "retry" else unanswered_in_row
            with mon.lock:
                h.fqdn = name
                mon.dns_pending -= 1
        if unanswered_in_row >= 3:  # DNS server not answering: one probe per pause, not a flood
            with mon.lock:
                mon.dns_paused = True
            mon.stop.wait(pause_s)
            continue
        if mon.dns_paused and status != "retry":
            with mon.lock:
                mon.dns_paused = False
        mon.stop.wait(max(0.0, 1.0 / rate - (time.time() - t0)))  # spread the queries out
    with mon.lock:
        mon.dns_paused = False


def is_ip(target: str) -> bool:
    try:
        ipaddress.ip_address(target)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------- monitor

class Monitor:
    def __init__(self, hosts: list[Host], backend, args):
        self.hosts = hosts
        for h in hosts:
            h.history = deque(maxlen=args.window)
        self.by_addr: dict[str, list[Host]] = {}
        for h in hosts:
            if h.addr:
                self.by_addr.setdefault(h.addr, []).append(h)
        self.groups: dict[str, list[Host]] = {}
        for h in hosts:
            self.groups.setdefault(h.group, []).append(h)
        self.backend = backend
        self.args = args
        self.lock = threading.Lock()
        self.events: deque[Text] = deque(maxlen=500)
        self.started = time.time()
        self.rounds = 0
        self.last_round_s = 0.0
        self.view = "compact"  # "v" toggles detail
        self.sort_problems = False  # "s" toggles (detail view)
        self.show_fqdn = False  # "n" toggles
        self.dns_pending = 0  # hosts still waiting for a reverse-DNS answer
        self.dns_paused = False  # DNS server not answering, only probing once a minute
        self.stop = threading.Event()
        self.ping_errors = 0  # consecutive failed rounds
        self.last_error = ""
        self._last_error_event = 0.0  # when a PING ERROR was last written to the log
        self.last_round_end = 0.0
        self.notice = ("", 0.0)  # (text, expires) shown in the header
        self.ignore_step = ""  # x dialog: "" closed, "pick" a group number, "confirm" by pressing it again
        self.ignore_until = 0.0  # the dialog closes by itself at this time
        self.ignore_keys: dict[str, str] = {}  # number key -> group, shown next to the group lines
        self.ignore_preview: list[Host] = []  # the hosts the confirm step will ignore
        self.last_ignore: tuple[str, list[tuple[Host, dict]]] | None = None  # (group, snapshots) for undo
        self.baseline_path = args.baseline
        self.baseline_seen: set[str] = set()
        self.baseline_dirty = False
        self.logfile = open(args.log, "a", encoding="utf-8") if args.log else None
        self.event(None, f"started: {len(hosts)} hosts in {len(self.groups)} groups via {backend.name}, "
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
        ts = datetime.now().strftime("%H:%M:%S")
        if host is None:
            who = ""
        elif host.label != host.target:
            who = f"{host.label} ({host.target}) "
        else:
            who = f"{host.target} "
        self.events.append(Text.assemble((ts + " ", "dim"), (who, "bold"), (msg, style)))
        grp = f"[{host.group}] " if host else ""
        line = f"{datetime.now().isoformat(timespec='seconds')} {grp}{who}{msg}"
        if self.logfile:
            self.logfile.write(line + "\n")
            self.logfile.flush()
        if self.args.headless:
            print(line, flush=True)

    def state_counts(self) -> dict[str, int]:
        counts = {st: 0 for st in SEVERITY}
        for h in self.hosts:
            counts[h.state] += 1
        return counts

    def status_line(self) -> str:
        counts = self.state_counts()
        up, seen = self.back_counts()
        not_back = [h.label for h in self.hosts if h.ever_up and not is_back(h)]
        line = (f"{datetime.now().isoformat(timespec='seconds')} STATUS back {up}/{seen}  "
                f"down {counts[DOWN]}  loss {counts[LOSS]}  warn {counts[WARN]}  silent {counts[NEVER]}  "
                f"invalid {counts[INVALID]}  round #{self.rounds} {self.last_round_s:.1f}s")
        problem = self.ping_problem()
        if problem:
            line += f"  !! {problem}"
        if not_back:
            line += "  not back: " + ", ".join(not_back[:15]) + (" …" if len(not_back) > 15 else "")
        return line

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
                results, errors = self.backend.round(targets) if targets else ({}, [])
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

    # -- ignore: the operator takes the DOWN hosts of a group out of the alarms (clients that went home)
    def ignore_key(self, key: str | None) -> bool:
        """The x dialog: x -> group number -> the same number again. True = the key was used here."""
        now = time.time()
        with self.lock:
            if self.ignore_step and now > self.ignore_until:
                self._ignore_close("ignore cancelled (no key for 5s)")
            if not key:
                return False
            if not self.ignore_step:
                if key != "x":
                    return False
                self._ignore_open(now)
            elif self.ignore_step == "pick" and key == "u" and self.last_ignore:
                self._ignore_close("")
                self._undo_ignore()
            elif self.ignore_step == "pick" and key in self.ignore_keys:
                self._ignore_preview(key, now)
            elif self.ignore_step == "confirm" and key in self.ignore_keys:
                group, hosts = self.ignore_keys[key], [h for h in self.ignore_preview if h.state == DOWN]
                self._ignore_close("")
                if hosts:
                    self._ignore(group, hosts, now)
                    self.notice = (f'ignored {len(hosts)} hosts in "{group}" - x u undoes it', now + 5)
                else:
                    self.notice = (f'no DOWN hosts left in "{group}" - nothing ignored', now + 3)
            else:  # any other key cancels, and does nothing else
                self._ignore_close("ignore cancelled")
            return True

    def _ignore_open(self, now: float):
        groups = [g for g, members in self.groups.items() if any(h.state == DOWN for h in members)]
        undo = (f"u = undo last ignore ({len(self.last_ignore[1])} in {self.last_ignore[0]})"
                if self.last_ignore else "")
        if not groups and not undo:
            self.notice = ("no DOWN hosts to ignore", now + 3)
            return
        self.ignore_keys = {str((i + 1) % 10): g for i, g in enumerate(groups[:10])}  # 1..9, 0
        self.ignore_step, self.ignore_until = "pick", now + IGNORE_CONFIRM_S
        self.view = "compact"  # the numbers are shown next to the group lines
        if groups:
            text = "IGNORE the DOWN hosts of a group: press its number"
            if len(groups) > 10:
                text += f" (first 10 of {len(groups)} groups)"
            text += (", " + undo if undo else "") + ", any other key cancels"
        else:
            text = f"no DOWN hosts to ignore - {undo}, any other key cancels"
        self.notice = (text, self.ignore_until)

    def _ignore_preview(self, key: str, now: float):
        group = self.ignore_keys[key]
        hosts = [h for h in self.groups[group] if h.state == DOWN]
        if not hosts:
            self._ignore_close(f'no DOWN hosts left in "{group}"')
            return
        downs = sorted(now - h.miss_streak_start for h in hosts if h.miss_streak_start)
        span = ""
        if downs:
            span = f" (down {fmt_dur(downs[0])}" + (f" to {fmt_dur(downs[-1])})" if len(downs) > 1 else ")")
        self.ignore_step, self.ignore_until = "confirm", now + IGNORE_CONFIRM_S
        self.ignore_keys, self.ignore_preview = {key: group}, hosts
        self.notice = (f'ignore {len(hosts)} DOWN hosts in "{group}"{span}? press {key} again to confirm',
                       self.ignore_until)

    def _ignore_close(self, notice: str):
        self.ignore_step, self.ignore_keys, self.ignore_preview = "", {}, []
        self.notice = (notice, time.time() + 3) if notice else ("", 0.0)

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

    def reset(self):
        with self.lock:
            for h in self.hosts:
                h.reset()
            self.started = time.time()
            self.rounds = 0
            self.event(None, "stats reset", "cyan")

    # -- rendering
    def render(self, console: Console):
        with self.lock:
            return self._render(console.size.width, console.size.height)

    def _render(self, width: int, height: int):
        header = self._header(width)
        if self.view == "compact":
            body, used = self._compact(width, height - 1)
        else:
            body, used = self._detail(width, height - 1 - min(self.args.events, 3))
        parts = [header, *body]
        self._append_events(parts, width, height - 1 - used)
        return Group(*parts)

    def back_counts(self) -> tuple[int, int]:
        """(hosts reachable now = not DOWN, hosts that ever answered)"""
        return sum(1 for h in self.hosts if is_back(h)), sum(1 for h in self.hosts if h.ever_up)

    def _header(self, width: int = 200) -> Text:
        """Status line; the least important parts are dropped first when it doesn't fit."""
        counts = self.state_counts()
        up, seen = self.back_counts()
        problem = self.ping_problem()
        notice, expires = self.notice
        # (priority, text, style) - higher priority is dropped first, 0 never
        segs: list[tuple[int, str, str]] = [(0, " pingT ", "bold black on cyan")]
        if problem:
            segs.append((0, f" {problem} ", "bold white on magenta"))
        if notice and time.time() < expires:
            segs.append((0, f" {notice} ", "bold black on yellow"))
        segs += [
            (0, f" back {up}/{seen} ", "bold black on green" if up == seen and seen else "bold white on red"),
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
        segs.append((3, f"{self.backend.name} {fmt_dur(time.time() - self.started)} "
                        f"#{self.rounds} {self.last_round_s:.1f}s", "dim"))
        segs.append((4, "[q]uit [r]eset [v]iew [n]ames [x]ignore" + (" [s]ort" if self.view == "detail" else ""), "dim"))

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
            seen = sum(1 for h in members if h.ever_up)
            up = sum(1 for h in members if is_back(h))
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
                             "green" if up == seen else STATE_STYLES[worst][0])
                else:
                    t.append(" " * prefix_w)
                for i, h in enumerate(members[start:start + per_line]):
                    if i and i % 10 == 0:
                        t.append(" ")
                    t.append(*GLYPHS[h.state])
                lines.append(t)

        problems = sorted((h for h in self.hosts if h.state in (DOWN, LOSS, WARN, INVALID)),
                          key=problem_order)
        free = height - len(lines)
        # problems get the rows they need at full detail; events get what's left (min 2 lines)
        if problems:
            name_w, hist_w, _ = CELL_LAYOUTS[0]
            full_cols = max(1, (width + 1) // (2 + name_w + 16 + 5 + 1 + hist_w + 1))
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

    # detail view: every host as a cell, auto-fit
    def _detail(self, width: int, height: int) -> tuple[list, int]:
        hosts = self.hosts
        if self.sort_problems:
            hosts = sorted(hosts, key=problem_order)
        grid, rows = self._cells(hosts, width, max(1, height), with_target=False)
        return [grid], rows

    def _cells(self, hosts: list[Host], width: int, max_rows: int, with_target: bool):
        """Grid of host cells using the most detailed layout that fits in max_rows."""
        # Size from BOTH name variants, so toggling label/FQDN keeps every host in the same
        # cell. While PTR answers are still arriving, reserve the maximum width instead
        # (not while DNS is unreachable - that can last, so size from the names we have).
        if self.dns_pending and not self.dns_paused:
            name_len = 10_000
        else:
            name_len = max(max(len(self._name(h, with_target, fqdn=False)),
                               len(self._name(h, with_target, fqdn=True))) for h in hosts)
        for name_w, hist_w, show_pct in CELL_LAYOUTS:
            name_w = min(name_w + (16 if with_target else 0), max(4, name_len))
            cell_w = 2 + name_w + (5 if show_pct else 0) + (1 + hist_w if hist_w else 0)
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
    for limit, ch in SPARK:
        if rtt < limit:
            return ch, ("green" if rtt < 30 else "yellow")
    return "▇", "yellow"


def fmt_dur(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# --------------------------------------------------------------------------- keyboard

class Keyboard:
    """Single-key input without Enter (POSIX terminals only)."""

    def __init__(self):
        self.enabled = sys.stdin.isatty()
        self.old = None

    def __enter__(self):
        if self.enabled:
            import termios
            import tty
            self.old = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        return self

    def __exit__(self, *exc):
        if self.old is not None:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.old)

    def get(self, timeout: float) -> str | None:
        if not self.enabled:
            time.sleep(timeout)
            return None
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return os.read(sys.stdin.fileno(), 1).decode(errors="ignore") if ready else None


# --------------------------------------------------------------------------- main

def print_summary(console: Console, mon: Monitor):
    """After exit: every host that lost anything, worst first, plus per-group totals."""
    up, seen = mon.back_counts()
    console.print(f"\n[bold]Summary[/bold] - {fmt_dur(time.time() - mon.started)}, {mon.rounds} rounds, "
                  f"[{'green' if up == seen else 'bold red'}]back {up}/{seen}[/] hosts that answered at some point")
    gt = Table(header_style="bold")
    for col in ("group", "hosts", "back", "not back", "silent", "ignored", "with loss", "outages", "loss %"):
        gt.add_column(col, justify="left" if col == "group" else "right")
    for gname, members in mon.groups.items():
        seen_m = [h for h in members if h.ever_up]
        back = sum(1 for h in seen_m if is_back(h))
        sent = sum(h.sent for h in seen_m)
        lost = sum(h.lost for h in seen_m)
        affected = sum(1 for h in seen_m if h.lost)
        outages = sum(h.outages for h in seen_m)
        gt.add_row(gname, str(len(members)), str(back), str(len(seen_m) - back),
                   str(sum(1 for h in members if h.state == NEVER)),
                   str(sum(1 for h in members if h.state == IGNORED)), str(affected), str(outages),
                   f"{100.0 * lost / sent if sent else 0:.2f}",
                   style="bold red" if back < len(seen_m) else ("yellow" if affected else "green"))
    console.print(gt)

    problem = mon.ping_problem()
    if problem:
        console.print(f"[bold white on magenta] {problem} [/] - the last states may be outdated")
    not_back = [h for h in mon.hosts if h.ever_up and not is_back(h)]
    if not_back:
        console.print(f"[bold red]NOT BACK ({len(not_back)}):[/bold red] answered earlier, not answering now")
        for h in not_back:
            note = "  (known from baseline, no reply in this run)" if h.from_baseline and h.sent == h.lost else ""
            name = h.fqdn or h.label
            console.print(f"  [red]{h.target:<16}[/red] {name:<30} [dim]{h.group}[/dim]  {h.state}{note}")
    invalid = [h for h in mon.hosts if h.invalid]
    if invalid:
        console.print(f"[bold magenta]INVALID entries, not pinged ({len(invalid)}):[/bold magenta]")
        for h in invalid:
            console.print(f"  [magenta]{h.target:<16}[/magenta] {h.label:<24} [dim]{h.where}[/dim]  {h.invalid}")
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
            name = h.fqdn or h.label
            console.print(f"  [yellow]{h.target:<16}[/yellow] {name:<30} [dim]{h.group}[/dim]  {status}  [dim]{when}[/dim]")
    silent = [h for h in mon.hosts if not h.ever_up and not h.invalid and h.ignored_at is None]
    if silent:
        console.print(f"[dim]Silent the whole time ({len(silent)}): "
                      + ", ".join(h.label if h.label == h.target else f"{h.label} ({h.target})" for h in silent)
                      + "[/dim]")

    bad = sorted((h for h in mon.hosts if h.ever_up and h.lost), key=lambda h: (-h.outages, -h.lost, h.order))
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
    ap.add_argument("--events", type=int, default=8, help="max event log lines (default 8, 0=off)")
    ap.add_argument("--log", default="pingT-events.log", help="append events to this file (default pingT-events.log)")
    ap.add_argument("--no-log", dest="log", action="store_const", const=None, help="don't write an event log")
    ap.add_argument("--headless", action="store_true",
                    help="no TUI: print events + a status line to stdout (for nohup / background)")
    ap.add_argument("--status", type=float, default=30, help="headless: seconds between status lines (default 30)")
    ap.add_argument("--baseline", help="file remembering which hosts ever answered "
                                       "(default: next to the log, <log>-baseline.json)")
    ap.add_argument("--fresh", action="store_true", help="ignore the saved baseline and start a new one")
    ap.add_argument("--no-dns", dest="dns", action="store_false",
                    help="no reverse DNS lookups at all")
    ap.add_argument("--dns-rate", type=float, default=20.0,
                    help="max reverse DNS queries per second (default 10, one query per host total)")
    args = ap.parse_args()

    checks = [
        (args.window >= 1, "-w/--window must be >= 1"),
        (1 <= args.warn <= args.loss <= args.window, "need 1 <= --warn <= --loss <= --window"),
        (args.down >= 1, "--down must be >= 1"),
        (args.interval >= 0.2, "-i/--interval must be >= 0.2 s"),
        (50 <= args.timeout <= 10000, "-t/--timeout must be 50..10000 ms"),
        (args.events >= 0, "--events must be >= 0"),
        (args.status >= 1, "--status must be >= 1 s"),
        (args.dns_rate > 0, "--dns-rate must be > 0"),
    ]
    for ok, msg in checks:
        if not ok:
            ap.error(msg)
    if not shutil.which("fping"):
        ap.error("fping not found in PATH - install it first (apt install fping)")
    out_dir = os.path.dirname(os.path.abspath(args.log)) if args.log else os.getcwd()
    stem = os.path.splitext(os.path.basename(args.log))[0] if args.log else "pingT"
    if not args.baseline:
        args.baseline = os.path.join(out_dir, f"{stem}-baseline.json")
    summary_path = os.path.join(out_dir, f"{stem}-summary-{datetime.now():%Y%m%d-%H%M%S}.txt")

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

    backend = FpingBackend(args.timeout, FPING_SPACING_MS, FPING_CHUNK)
    needed = backend.round_time(len(hosts) - len(invalid))  # worst case: every host times out
    if needed > 1.5 * args.interval:
        print(f"note: with many hosts down a round can take ~{needed:.1f}s > interval {args.interval}s; "
              f"rounds then run back-to-back", file=sys.stderr)
    if invalid and not args.headless and sys.stdin.isatty():
        time.sleep(3)  # let the warnings be read before the dashboard takes over the screen

    def on_signal(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_signal)  # kill -> still save the summary
    if signal.getsignal(signal.SIGHUP) != signal.SIG_IGN:  # keep nohup's "ignore hangup"
        signal.signal(signal.SIGHUP, on_signal)  # SSH session dropped -> still save the summary

    console = Console()
    try:
        mon = Monitor(hosts, backend, args)
    except OSError as e:
        ap.error(f"cannot open log file: {e}")
    ping_thread = threading.Thread(target=mon.run_pings, daemon=True)
    ping_thread.start()
    if args.dns:
        threading.Thread(target=resolve_names, args=(mon, args.dns_rate), daemon=True).start()

    try:
        if args.headless:
            while True:
                time.sleep(args.status)
                with mon.lock:
                    line = mon.status_line()
                    print(line, flush=True)
                    if mon.logfile:
                        mon.logfile.write(line + "\n")
                        mon.logfile.flush()
        reset_armed_until = 0.0
        with Keyboard() as kb, Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                live.update(mon.render(console), refresh=True)
                key = (kb.get(0.5) or "").lower()
                if mon.ignore_key(key):  # the x dialog (also closes it after its timeout)
                    reset_armed_until = 0.0
                    continue
                if not key:
                    continue
                if key == "r" and time.time() < reset_armed_until:
                    mon.reset()
                    reset_armed_until = 0.0
                    mon.notice = ("stats reset", time.time() + 3)
                    continue
                reset_armed_until = 0.0  # any other key cancels a pending reset
                mon.notice = ("", 0.0)
                if key == "q":
                    break
                if key == "r":
                    reset_armed_until = time.time() + 3
                    mon.notice = ("press r again within 3s to RESET all stats", reset_armed_until)
                if key == "v":
                    mon.view = "detail" if mon.view == "compact" else "compact"
                if key == "n":
                    if args.dns or mon.show_fqdn:
                        mon.show_fqdn = not mon.show_fqdn
                    else:
                        mon.notice = ("reverse DNS is off (--no-dns)", time.time() + 3)
                if key == "s":
                    mon.sort_problems = not mon.sort_problems
    except (KeyboardInterrupt, OSError):  # OSError: terminal went away
        pass
    finally:
        mon.stop.set()
        ping_thread.join(timeout=backend.round_time(len(hosts)) + 5)
        with mon.lock:
            mon.event(None, "stopped", "cyan")
            err = save_summary(mon, summary_path)  # file first - the terminal may be gone
            try:
                print_summary(console, mon)
                if args.log:
                    console.print(f"[dim]events logged to {os.path.abspath(args.log)}[/dim]")
                console.print(f"[dim]summary saved to {summary_path}[/dim]" if not err
                              else f"[red]could not save summary: {err}[/red]")
            except OSError:
                pass


if __name__ == "__main__":
    main()
