"""scanT: find the reachable hosts in IPv4 subnets and write a host list for pingT.

ICMP only, no root needed (fping, like pingT). Pass 1 probes every address, later passes
only the ones that stayed silent - a host that drops one packet is still found, without
scanning everything again. All subnets are swept together, so every fping process is busy
even with many small subnets.
"""

from __future__ import annotations

import argparse
import ipaddress
import math
import os
import shutil
import time

from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn
from rich.table import Table

from . import CREDITS, __version__
from .charsets import add_charset_option, charset_from_args
from .durations import duration
from .fping import Fping
from .hostlist import INLINE_COMMENT
from .inventory import HostListFile, cannot_write, ip_order, new_host_list, write_file

PROCESSES = 4  # fping processes in parallel; --rate is split between them
PASSES = 3
CHUNK_MAX = 256  # addresses per fping process at most
MAX_SUBNET = 65536  # addresses in one subnet without --force (a /16)
MAX_TOTAL = 262144  # addresses in one scan without --force (four /16)

DESCRIPTION = f"""\
scanT {__version__} {CREDITS} - Timmy Pinger's scanner: finds the hosts that answer ping in
IPv4 subnets and writes a host list for pingT. ICMP only, no root needed. Every address gets
up to 3 tries, so a host that misses one ping is still found.
"""

EPILOG = """\
examples:
  python3 scanT.py -f subnets.txt -o hosts.txt     a new host list from a subnet list
  python3 scanT.py 10.0.10.0/24=Servers -o hosts.txt
  python3 scanT.py --rescan hosts.txt              scan its subnets again: only ADDS new hosts,
                                                   prints what changed
  python3 scanT.py --rescan hosts.txt -f subnets.txt   the same for a hand-written host list

subnet list: one subnet per line, a group name after it, '#' comments:
  10.0.10.0/24   Servers VLAN10
  10.0.20.0/24   Clients 2nd floor
  10.0.30.0/24=Printers

--rescan never removes or changes a host that is already in the list; new hosts go to the
end of their group, marked '# new in rescan'. A host you commented out ('# 10.0.10.5 old-pc')
is not added again.

Scanning unused addresses makes the router ARP for every one of them - that is why the scan
is rate limited (--rate). Scan only networks you are responsible for.
"""


class ScanAborted(Exception):
    """fping keeps failing - the results are incomplete."""


class Subnet:
    def __init__(self, network: ipaddress.IPv4Network, group: str | None):
        self.network = network
        self.group = group or str(network)
        self.addresses: list[str] = []  # filled by claim_addresses, after the size checks
        self.answered: dict[str, int] = {}  # ip -> the pass that found it

    def claim_addresses(self, owners: dict[str, Subnet]) -> int:
        """Takes the subnet's addresses that no earlier subnet has; returns how many were taken already."""
        overlap = 0
        for ip in map(str, self.network.hosts()):  # without network and broadcast address (/31, /32: all)
            if ip in owners:
                overlap += 1
                continue
            owners[ip] = self
            self.addresses.append(ip)
        return overlap


def parse_subnet(spec: str) -> tuple[str, str | None]:
    """ "10.0.0.0/24=Group name", "10.0.0.0/24  Group name" or "10.0.0.0/24" -> (cidr, group or None).
    The first word decides: with "=" in it, it's the "=" form, otherwise the rest of the line is
    the group name (which may then contain "=")."""
    words = spec.split(None, 1)
    if not words:
        return "", None
    if "=" in words[0]:
        cidr, _, group = spec.strip().partition("=")
    else:
        cidr, group = words[0], (words[1] if len(words) > 1 else "")
    return cidr.strip(), group.strip() or None


def read_subnet_list(path: str) -> list[tuple[str, str | None]]:
    subnets = []
    with open(path, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if line and not line.startswith("#"):
                subnets.append(parse_subnet(INLINE_COMMENT.sub("", line)))
    return subnets


def sweep(owners: dict[str, Subnet], fping: Fping, console: Console, progress_bar: bool):
    """Probes every address, then twice more the ones that stayed silent."""
    failed_in_a_row = found = 0
    with Progress(TextColumn("[bold]{task.fields[name]}"),
                  BarColumn() if progress_bar else TextColumn("{task.percentage:>3.0f}%"),
                  TextColumn("{task.completed}/{task.total} addr"), TextColumn("[green]{task.fields[found]} found"),
                  TimeRemainingColumn(), console=console) as progress:
        for scan_pass in range(1, PASSES + 1):
            todo = [ip for ip, subnet in owners.items() if ip not in subnet.answered]
            if not todo:
                break
            # spread each pass over all fping processes, even when it's only a few addresses
            fping.chunk_size = max(1, min(CHUNK_MAX, math.ceil(len(todo) / PROCESSES)))
            batch_size = fping.chunk_size * PROCESSES
            task = progress.add_task("", name=f"pass {scan_pass}/{PASSES}", total=len(todo), found=found)
            for start in range(0, len(todo), batch_size):
                batch = todo[start:start + batch_size]
                results, errors = fping.ping(batch)
                if errors:
                    more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
                    progress.console.print(f"[bold red]scan error:[/bold red] {escape(errors[0])}{more}")
                    failed_in_a_row = failed_in_a_row + 1 if not results else 0
                    if failed_in_a_row >= 3:
                        raise ScanAborted(f"fping keeps failing: {errors[0]}")
                else:
                    failed_in_a_row = 0
                for ip, rtt in results.items():
                    if rtt is not None:
                        owners[ip].answered[ip] = scan_pass
                        found += 1
                progress.update(task, advance=len(batch), found=found)


def parse_args() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(prog="python3 scanT.py", description=DESCRIPTION, epilog=EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"scanT {__version__} ({CREDITS})")

    what = parser.add_argument_group("what to scan")
    what.add_argument("subnets", nargs="*", metavar="SUBNET", help="subnets to scan, e.g. 10.0.10.0/24 or "
                                                                   "10.0.10.0/24=Servers (with a group name)")
    what.add_argument("-f", "--file", action="append", default=[], metavar="FILE",
                      help="subnet list; can be given more than once")
    what.add_argument("--rescan", metavar="HOSTLIST", help="scan the subnets of this host list again and only ADD "
                                                           "the new hosts; prints what changed")

    result = parser.add_argument_group("the result")
    result.add_argument("-o", "--out", metavar="FILE", help="host list to write (default inventory.txt; with "
                                                            "--rescan the rescanned host list itself). The previous "
                                                            "version is kept as FILE.bak")
    result.add_argument("--overwrite", action="store_true", help="let a new scan replace an existing host list - "
                                                                 "its names are lost (--rescan keeps them)")
    add_charset_option(result)

    pace = parser.add_argument_group("pace and limits")
    pace.add_argument("-t", "--timeout", type=int, default=500, metavar="MS",
                      help="how long to wait for a reply, 50..10000 ms (default 500)")
    pace.add_argument("--rate", type=int, default=400, metavar="PINGS",
                      help="max pings per second in total (default 400)")
    pace.add_argument("--force", action="store_true", help=f"allow subnets bigger than a /16 and more than "
                                                           f"{MAX_TOTAL:,} addresses in one scan")
    args = parser.parse_args()
    if not shutil.which("fping"):
        parser.error("fping not found in PATH (apt install fping)")
    for ok, message in [(50 <= args.timeout <= 10000, "--timeout must be 50..10000 ms"),
                        (args.rate >= 10, "--rate must be >= 10 pings/s")]:
        if not ok:
            parser.error(message)
    return parser, args


def main():
    parser, args = parse_args()
    charset = charset_from_args(parser, args)
    console = Console()

    host_list = None
    if args.rescan:
        try:
            host_list = HostListFile(args.rescan)
        except OSError as e:
            parser.error(f"cannot read --rescan host list: {e}")

    specs: list[tuple[str, str | None]] = []
    for path in args.file:
        try:
            specs += read_subnet_list(path)
        except OSError as e:
            parser.error(f"cannot read subnet list: {e}")
    specs += [parse_subnet(spec) for spec in args.subnets]
    if host_list and not specs:
        specs = host_list.subnets
        if not specs:
            parser.error(f"{args.rescan} has no scanned subnets (no '# 10.0.10.0/24: ...' lines above its groups) - "
                         f"give them with -f subnets.txt or on the command line")
    if not specs:
        parser.error("no subnets given (e.g. 10.0.10.0/24, or -f subnets.txt)")

    # every subnet is parsed and size-checked BEFORE any address list is built
    subnets: list[Subnet] = []
    seen = set()
    for cidr, group in specs:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError as e:
            parser.error(f"invalid subnet '{cidr}': {e}")
        if network.version != 4:
            parser.error(f"{network}: IPv6 is not supported (IPv4 only, see DECISIONS.md)")
        if network.num_addresses > MAX_SUBNET and not args.force:
            parser.error(f"{network} has {network.num_addresses:,} addresses - more than a /16. "
                         f"Split it into smaller subnets, or use --force if you really mean it.")
        if network in seen:
            console.print(f"[yellow]note:[/yellow] {network} listed twice, scanning it once")
            continue
        seen.add(network)
        subnets.append(Subnet(network, group))
    total = sum(s.network.num_addresses for s in subnets)
    if total > MAX_TOTAL and not args.force:
        parser.error(f"{total:,} addresses in total - more than {MAX_TOTAL:,}. Scan fewer subnets per run, "
                     f"or use --force.")

    out = args.out or args.rescan or "inventory.txt"
    if not args.rescan and os.path.exists(out) and not args.overwrite:
        parser.error(f"{out} exists - use --rescan {out} to add new hosts to it, or --overwrite to replace it "
                     f"(the old one is kept as {out}.bak)")
    for path in (out, out + ".partial"):
        if problem := cannot_write(path):
            parser.error(f"cannot write the result: {problem}")

    owners: dict[str, Subnet] = {}  # ip -> the subnet it is scanned for
    for subnet in subnets:
        if overlap := subnet.claim_addresses(owners):
            console.print(f"[yellow]note:[/yellow] {subnet.network} overlaps an earlier subnet - {overlap} addresses "
                          f"stay in the earlier group")

    spacing = max(1, round(1000 * PROCESSES / args.rate))  # ms between two probes of one fping process
    fping = Fping(args.timeout, spacing, CHUNK_MAX, processes=PROCESSES)
    batches = math.ceil(len(owners) / (CHUNK_MAX * PROCESSES)) if owners else 0
    first_pass = len(owners) / args.rate + args.timeout / 1000 * batches
    what = f"rescanning {escape(args.rescan)}:" if args.rescan else "scanning"
    console.print(f"[bold]{what}[/bold] {len(subnets)} subnets, {len(owners):,} addresses, {PASSES} passes, "
                  f"~{args.rate} pings/s (first pass ~{duration(first_pass)})")

    started = time.time()
    interrupted = None
    try:
        sweep(owners, fping, console, progress_bar=charset.name == "unicode")
    except KeyboardInterrupt:
        interrupted = "interrupted"
    except ScanAborted as e:
        interrupted = str(e)

    answered = {ip for s in subnets for ip in s.answered}
    new_hosts: dict[str, list[str]] = {}
    if host_list:
        for ip in sorted(answered - set(host_list.known) - host_list.commented_out, key=ip_order):
            new_hosts.setdefault(owners[ip].group, []).append(ip)
        listed = {subnet for subnet, _ in host_list.subnets}
        new_subnets: dict[str, list[str]] = {}
        for s in subnets:
            if str(s.network) not in listed:  # from -f: remember it in the file for the next rescan
                new_subnets.setdefault(s.group, []).append(str(s.network))
        changed = bool(new_hosts or new_subnets)
        text = host_list.with_new_hosts(new_hosts, new_subnets)
    else:
        changed = True
        groups: dict[str, list[tuple[str, int, int]]] = {}
        hosts: dict[str, list[str]] = {}
        for s in subnets:
            groups.setdefault(s.group, []).append((str(s.network), len(s.answered), len(s.addresses)))
            hosts.setdefault(s.group, []).extend(s.answered)
        text = new_host_list(groups, hosts, len(owners), PASSES, __version__)
    if interrupted:  # incomplete: never replace a good host list with it
        console.print(f"[yellow]{escape(interrupted)} - results are incomplete, written to {escape(out)}.partial "
                      f"({escape(out)} was not touched)[/yellow]")
        out += ".partial"
    if changed or interrupted:
        try:
            write_file(out, text)
        except OSError as e:  # checked before the scan, so rare: don't lose the result
            console.print(f"[bold red]cannot write {escape(out)}: {escape(str(e))}[/bold red] - the result:")
            print(text)
            raise SystemExit(1) from None

    table = Table(header_style="bold", box=charset.table)
    for column in ("subnet", "group", "scanned", "found", "found only in pass 2+"):
        table.add_column(column, justify="left" if column in ("subnet", "group") else "right")
    for s in subnets:
        late = sum(1 for found_in in s.answered.values() if found_in > 1)
        table.add_row(str(s.network), escape(s.group), str(len(s.addresses)), str(len(s.answered)), str(late),
                      style="green" if s.answered else "yellow")
    console.print(table)
    if host_list:
        print_rescan_diff(console, host_list, args.rescan, out, owners, answered, new_hosts, changed)
    else:
        count = f"{len(answered)} host" + ("" if len(answered) == 1 else "s")
        console.print(f"{count} in {duration(time.time() - started)} -> [bold]{escape(out)}[/bold]")
    if interrupted:
        raise SystemExit(130 if interrupted == "interrupted" else 1)


def print_rescan_diff(console: Console, host_list: HostListFile, name: str, out: str, owners: dict[str, Subnet],
                      answered: set[str], new_hosts: dict[str, list[str]], changed: bool):
    """What the rescan found, compared to the host list."""
    new = [ip for ips in new_hosts.values() for ip in ips]
    scanned_known = [target for target in host_list.known if target in owners]
    no_reply = [target for target in scanned_known if target not in answered]
    not_scanned = len(host_list.known) - len(scanned_known)
    answering_again = sorted(host_list.commented_out & answered, key=ip_order)

    def row(mark: str, target: str, style: str):
        label, group = host_list.known.get(target, ("", None))
        group = group or owners[target].group
        console.print(f"  [{style}]{mark} {escape(f'{target:<16}')}[/{style}] {escape(label)}  [dim]{escape(group)}[/dim]")

    console.print(f"\n[bold]rescan of {escape(name)}:[/bold] [green]{len(new)} new[/green], "
                  f"{len(scanned_known) - len(no_reply)} known answered, "
                  f"[yellow]{len(no_reply)} known without reply (kept)[/yellow]"
                  + (f", {not_scanned} outside the subnets (not scanned)" if not_scanned else ""))
    if new:
        console.print(f"[green]+ new, added to {escape(out)} (marked '# new in rescan'):[/green]")
        for ip in new:
            row("+", ip, "green")
    if no_reply:
        console.print("[yellow]- no reply this time, kept unchanged:[/yellow]")
        for target in no_reply:
            row("-", target, "yellow")
    if answering_again:
        console.print(f"[cyan]# commented out in {escape(name)} but answering, not added:[/cyan] "
                      + ", ".join(answering_again))
    if not changed:
        console.print(f"[dim]no new hosts - {escape(out)} not changed[/dim]")
