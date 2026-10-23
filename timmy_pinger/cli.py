"""pingT's command line: python3 pingT.py -f hosts.txt"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sys
import threading
import time
from datetime import datetime

from rich.console import Console
from rich.live import Live

from . import CREDITS, __version__, dns
from .baseline import Baseline
from .charsets import add_charset_option, charset_from_args
from .controls import Controls
from .events import EventLog
from .fping import Fping
from .host import Limits
from .hostlist import Entry, check_targets, make_hosts, read_host_list
from .keyboard import Keyboard
from .screen import Screen, ViewSettings
from .session import Session
from .summary import save_summary, write_summary

TOOL_FOLDER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FOLDER = os.path.join(TOOL_FOLDER, "logs")  # event log, summaries, baseline (README "Files pingT writes")
OLD_BASELINE_S = 24 * 3600

DESCRIPTION = f"""\
pingT {__version__} {CREDITS} - watch hundreds of hosts at once during a switch migration.

Pings every host once a second (fping) and shows all of them on one screen: which hosts
went down, and whether all of them came back. Only several lost pings raise an alarm.
IPv4 only. The whole story: README.md.
"""

EPILOG = """\
examples:
  python3 pingT.py -f hosts.txt --fresh    start a new migration session
  python3 pingT.py -f hosts.txt            restart during a migration (keeps the baseline)
  python3 pingT.py 1.1.1.1 8.8.8.8         just try it out
  python3 pingT.py -f hosts.txt --charset safe     for PuTTY (or: export PINGT_CHARSET=safe)

keys while it runs:
  q      quit (the summary is printed and saved)
  v      groups <-> every host as its own box
  n      names: your label -> DNS name -> IP
  r r    reset the loss counters (press twice; the summary keeps what they held)
  x      ignore the DOWN hosts of a group (x, its number, the number again); x u undoes it
  g      characters: unicode -> safe -> ascii (for PuTTY)
  Esc    cancel a question (r, x)

files: the event log, the summaries and the baseline go into logs/ next to pingT.py
(or next to --log). Host list format: README.md "Host list format".
"""


def parse_args() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(prog="python3 pingT.py", description=DESCRIPTION, epilog=EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"pingT {__version__} ({CREDITS})")

    hosts = parser.add_argument_group("what to ping")
    hosts.add_argument("targets", nargs="*", metavar="IP", help="IPs or DNS names to ping, in addition to -f")
    hosts.add_argument("-f", "--file", action="append", default=[], metavar="FILE",
                       help="host list; can be given more than once")
    hosts.add_argument("--fresh", action="store_true",
                       help="start a new session: forget which hosts were online in an earlier run. "
                            "Leave it out only to restart during a migration")

    alarms = parser.add_argument_group("when a host counts as WARN / LOSS / DOWN")
    alarms.add_argument("-w", "--window", type=int, default=20, metavar="PINGS",
                        help="how many recent pings count for WARN and LOSS (default 20)")
    alarms.add_argument("--warn", type=int, default=2, metavar="N",
                        help="lost pings in the window until yellow WARN (default 2)")
    alarms.add_argument("--loss", type=int, default=3, metavar="N",
                        help="lost pings in the window until red LOSS (default 3)")
    alarms.add_argument("--down", type=int, default=3, metavar="N",
                        help="lost pings IN A ROW until DOWN (default 3)")

    pinging = parser.add_argument_group("pinging")
    pinging.add_argument("-i", "--interval", type=float, default=1.0, metavar="SECONDS",
                         help="seconds between two pings to the same host (default 1.0, min 0.2)")
    pinging.add_argument("-t", "--timeout", type=int, default=800, metavar="MS",
                         help="how long to wait for a reply, 50..10000 ms (default 800)")
    pinging.add_argument("--no-dns", dest="dns", action="store_false",
                         help="no reverse DNS lookups (the n key then switches between labels and IPs)")

    output = parser.add_argument_group("screen and files")
    output.add_argument("--events", type=int, default=8, metavar="LINES",
                        help="event lines on screen (default 8, 0 = none)")
    output.add_argument("--log", default=None, metavar="FILE",
                        help="event log (default logs/pingT-events.log next to pingT.py); "
                             "the summaries and the baseline go next to it, named after it")
    add_charset_option(output)
    args = parser.parse_args()

    for ok, message in [
        (args.window >= 1, "-w/--window must be >= 1"),
        (1 <= args.warn <= args.loss <= args.window, "need 1 <= --warn <= --loss <= --window"),
        (args.down >= 1, "--down must be >= 1"),
        (args.interval >= 0.2, "-i/--interval must be >= 0.2 s"),
        (50 <= args.timeout <= 10000, "-t/--timeout must be 50..10000 ms"),
        (args.events >= 0, "--events must be >= 0"),
    ]:
        if not ok:
            parser.error(message)
    if not shutil.which("fping"):
        parser.error("fping not found in PATH - install it first (apt install fping)")
    return parser, args


def main():
    parser, args = parse_args()
    charset = charset_from_args(parser, args)
    if args.log is None:
        os.makedirs(LOG_FOLDER, exist_ok=True)
        args.log = os.path.join(LOG_FOLDER, "pingT-events.log")
    stem = os.path.splitext(os.path.abspath(args.log))[0]  # .../pingT-events -> -baseline.json, -summary-...

    entries: list[Entry] = []
    for path in args.file:
        try:
            entries += read_host_list(path)
        except OSError as e:
            parser.error(f"cannot read host list: {e}")
    entries += [Entry(target, target, None, "command line") for target in args.targets]
    hosts = make_hosts(entries)
    if not hosts:
        parser.error("no hosts given (use -f hosts.txt and/or list targets)")
    check_targets(hosts)
    invalid = [h for h in hosts if h.invalid]
    for h in invalid:
        print(f"WARNING {h.source}: '{h.target}' {h.invalid} - not pinged", file=sys.stderr)
    if len(invalid) == len(hosts):
        parser.error("no valid hosts to ping")

    fping = Fping(args.timeout)
    pinged = len(hosts) - len(invalid)
    worst_round = fping.worst_round_time(pinged)
    if worst_round > 1.5 * args.interval:
        print(f"note: with {pinged} hosts a round can take ~{worst_round:.1f}s when many of them are down "
              f"(> interval {args.interval}s); rounds then run back-to-back", file=sys.stderr)
    if invalid and sys.stdin.isatty():
        time.sleep(3)  # time to read the warnings before the screen takes over

    def stop_on_signal(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_on_signal)  # kill: still save the summary
    signal.signal(signal.SIGHUP, stop_on_signal)  # SSH session dropped: still save the summary

    try:
        log = EventLog(args.log, args.events)
    except OSError as e:
        parser.error(f"cannot open log file: {e}")
    session = Session(hosts, Limits(args.window, args.warn, args.loss, args.down), log,
                      Baseline(f"{stem}-baseline.json"), fresh=args.fresh)
    view = ViewSettings(charset)
    if session.baseline_age is not None and session.baseline_age > OLD_BASELINE_S:
        view.sticky = f"baseline is {session.baseline_age / 3600:.0f}h old - new migration? restart with --fresh " \
                      f"(any key hides this)"
    controls = Controls(session, view, dns_enabled=args.dns)
    screen = Screen(session, view, args.interval, args.events)

    ping_thread = threading.Thread(target=session.run_pings, args=(fping, args.interval), daemon=True)
    ping_thread.start()
    if args.dns:
        threading.Thread(target=dns.look_up_dns_names, args=(session,), daemon=True).start()

    console = Console()
    try:
        with Keyboard() as keyboard, Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                live.update(screen.render(*console.size), refresh=True)
                keys = keyboard.read(0.5)
                controls.close_expired_question()  # first, so a late key can't answer a question that timed out
                if any(controls.press(key) for key in keys):
                    break
    except (KeyboardInterrupt, OSError):  # OSError: the terminal went away
        pass
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):  # a 2nd signal must not cut the summary short
            signal.signal(sig, signal.SIG_IGN)
        session.stop.set()
        ping_thread.join(timeout=fping.worst_round_time(pinged) + 5)
        summary_path = f"{stem}-summary-{datetime.now():%Y%m%d-%H%M%S}.txt"  # named after when it stopped
        with session.lock:
            log.write("stopped", "cyan")
            error = save_summary(session, summary_path, view.charset, args.interval)  # the file first: the terminal may be gone
            try:
                write_summary(console, session, view.charset, args.interval)
                console.print(f"[dim]events logged to {os.path.abspath(args.log)}[/dim]")
                console.print(f"[dim]summary saved to {summary_path}[/dim]" if not error
                              else f"[red]could not save summary: {error}[/red]")
            except OSError:
                pass
            log.close()
