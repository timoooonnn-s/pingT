"""The summary when pingT stops - on screen and as a file: the migration record."""

from __future__ import annotations

import io
import time
from datetime import datetime

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import CREDITS, __version__
from .charsets import Charset
from .durations import duration
from .host import DOWN, IGNORED, SILENT, Host, all_back, back_counts
from .session import HostStats, Session, current_longest_outage

# Everything that comes from the host list or from DNS goes through escape(): a label like
# "[old] sw2" must not be read as rich markup.


def clock(timestamp: float) -> str:
    return f"{datetime.fromtimestamp(timestamp):%H:%M:%S}"


def write_summary(console: Console, session: Session, charset: Charset, interval: float):
    now = time.time()

    def make_table(**options) -> Table:
        return Table(header_style="bold", box=charset.table, **options)

    def host_line(h: Host, color: str, rest: str):
        console.print(f"  [{color}]{escape(f'{h.target:<16}')}[/{color}] {escape(f'{h.dns_name or h.label:<30}')} {rest}")

    back, answered = back_counts(session.hosts)
    since_reset = f" (stats since the reset at {clock(session.stats_since)})" if session.resets else ""
    console.print(f"\n[bold]Summary[/bold] (pingT {__version__}) - {duration(now - session.started)}, "
                  f"{session.rounds} rounds, [{'green' if all_back(back, answered) else 'bold red'}]"
                  f"back {back}/{answered}[/] hosts that answered at some point{since_reset}")

    groups = make_table()
    for column in ("group", "hosts", "back", "not back", "silent", "ignored", "with loss", "outages", "loss %"):
        groups.add_column(column, justify="left" if column == "group" else "right")
    for group, members in session.groups.items():
        group_back, group_answered = back_counts(members)
        answered_hosts = [h for h in members if h.answered_once]
        sent = sum(h.sent for h in answered_hosts)
        lost = sum(h.lost for h in answered_hosts)
        with_loss = sum(1 for h in answered_hosts if h.lost)
        groups.add_row(escape(group), str(len(members)), str(group_back), str(group_answered - group_back),
                       str(sum(1 for h in members if h.state == SILENT)),
                       str(sum(1 for h in members if h.state == IGNORED)), str(with_loss),
                       str(sum(h.outages for h in answered_hosts)), f"{100.0 * lost / sent if sent else 0:.2f}",
                       style="bold red" if group_back < group_answered else
                       ("yellow" if with_loss else "green" if group_answered else "dim"))
    console.print(groups)

    problem = session.ping_problem(interval)
    if problem:
        console.print(f"[bold white on magenta] {escape(problem)} [/] - the last states may be outdated")
    if session.all_down_periods:
        periods = ", ".join(f"{clock(since)} for {duration((until or now) - since)}"
                            for since, until in session.all_down_periods)
        console.print(f"[bold white on magenta] ALL watched hosts were DOWN at once "
                      f"({len(session.all_down_periods)}x) [/] {periods} - probably pingT's own network, "
                      f"not the migration")

    write_timeline(console, session, make_table, now)

    not_back = [h for h in session.hosts if h.answered_once and not h.is_back]
    if not_back:
        console.print(f"[bold red]NOT BACK ({len(not_back)}):[/bold red] answered earlier, not answering now")
        for h in not_back:
            note = "  (known from baseline, no reply in this run)" if h.known_from_baseline and h.sent == h.lost else ""
            host_line(h, "red", f"[dim]{escape(h.group)}[/dim]  {h.state}{note}")
    invalid = [h for h in session.hosts if h.invalid]
    if invalid:
        console.print(f"[bold magenta]INVALID entries, not pinged ({len(invalid)}):[/bold magenta]")
        for h in invalid:
            host_line(h, "magenta", f"[dim]{escape(h.source)}[/dim]  {escape(h.invalid)}")
    ignored = [h for h in session.hosts if h.ignore_history]
    if ignored:
        still = sum(1 for h in ignored if h.ignored_since is not None)
        console.print(f"[bold yellow]IGNORED by operator ({len(ignored)}):[/bold yellow] "
                      f"{still} still ignored (no reply since), {len(ignored) - still} came back (watched again)")
        for h in sorted(ignored, key=lambda h: (h.ignored_since is None, h.order)):
            when = ", ".join(f"ignored {datetime.fromtimestamp(at):%H:%M}"
                             + (f" back {datetime.fromtimestamp(back_at):%H:%M}" if back_at else "")
                             for at, back_at in h.ignore_history)
            status = "still ignored" if h.ignored_since is not None else f"back, {h.state}"
            host_line(h, "yellow", f"[dim]{escape(h.group)}[/dim]  {status}  [dim]{when}[/dim]")
    silent = [h for h in session.hosts if not h.answered_once and not h.invalid and h.ignored_since is None]
    if silent:
        console.print(f"[dim]Silent the whole time ({len(silent)}): "
                      + escape(", ".join(h.who for h in silent)) + "[/dim]")

    # outages without lost pings: DOWN at a reset, back before it missed another ping
    with_loss = [HostStats(h, h.sent, h.lost, h.outages, current_longest_outage(h, now), h.state)
                 for h in session.hosts if h.answered_once and (h.lost or h.outages)]
    title = "Hosts with loss" + (" since the last reset" if session.resets else "")
    if with_loss:
        console.print(loss_table(make_table, with_loss, f"{title} ({len(with_loss)})"))
    else:
        since = " since the last reset" if session.resets else ""
        console.print(f"[green]No packet loss on any responding host{since}.[/green]")
    for reset in reversed(session.resets):
        span = f"{clock(reset.stats_since)} to {clock(reset.at)}"
        if reset.hosts:
            console.print(loss_table(make_table, reset.hosts, f"Before the reset at {clock(reset.at)} (r r): "
                                                         f"hosts with loss {span} ({len(reset.hosts)})"))
        else:
            console.print(f"[dim]Before the reset at {clock(reset.at)} (r r): no packet loss {span}.[/dim]")


def write_timeline(console: Console, session: Session, make_table, now: float):
    """Every outage of 2 or more hosts of a group: when it started, how long until the group
    was complete again."""
    outages = [o for o in session.outage_timeline if len(o.hosts) >= 2]
    if not outages:
        return
    timeline = make_table(title=f"Group outages ({len(outages)})")
    for column in ("from", "group", "hosts down", "until", "took", "result"):
        timeline.add_column(column, justify="right" if column in ("hosts down", "took") else "left")
    for o in outages:
        members = session.groups[o.group]
        if o.until is None:
            still_down = sum(1 for h in members if h.order in o.hosts and h.state == DOWN)
            until, took, result, style = "-", duration(now - o.since), f"still {still_down} DOWN", "bold red"
        else:
            back = len(o.hosts) - o.ignored
            result = f"all {back} back" if not o.ignored else f"{back} back, {o.ignored} ignored"
            until, took, style = clock(o.until), duration(o.until - o.since), "green" if not o.ignored else "yellow"
        timeline.add_row(clock(o.since), escape(o.group), str(len(o.hosts)), until, took, result, style=style)
    console.print(timeline)


def loss_table(make_table, stats: list[HostStats], title: str) -> Table:
    stats = sorted(stats, key=lambda s: (-s.outages, -s.lost, s.host.order))
    with_dns = any(s.host.dns_name and s.host.dns_name != s.host.label for s in stats)
    columns = ["host", *(["dns name"] if with_dns else []), "target", "group", "state", "sent", "lost", "loss %",
               "outages", "longest"]
    numbers = ("sent", "lost", "loss %", "outages", "longest")
    result = make_table(title=title)
    for column in columns:
        result.add_column(column, justify="right" if column in numbers else "left")
    for s in stats:
        h = s.host
        pct = 100.0 * s.lost / s.sent if s.sent else 0.0
        result.add_row(escape(h.label), *([escape(h.dns_name or "-")] if with_dns else []), escape(h.target),
                       escape(h.group), s.state, str(s.sent), str(s.lost), f"{pct:.1f}", str(s.outages),
                       duration(s.longest_outage) if s.outages else "-",
                       style="red" if s.outages or s.state == DOWN else "yellow")
    return result


def save_summary(session: Session, path: str, charset: Charset, interval: float) -> str | None:
    """The summary as a plain text file. Returns an error message, or None."""
    buffer = io.StringIO()
    try:
        write_summary(Console(file=buffer, width=140, color_system=None, force_terminal=False), session, charset,
                      interval)
    except Exception as e:  # never lose the file to a rendering problem: keep what was written
        buffer.write(f"\n(summary incomplete: {type(e).__name__}: {e})\n")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"Timmy Pinger {__version__} ({CREDITS}) - summary, written {datetime.now():%Y-%m-%d %H:%M:%S}\n")
            fh.write(buffer.getvalue())
    except OSError as e:
        return str(e)
    return None
