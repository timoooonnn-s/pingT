#!/usr/bin/env python3
"""
scan.py - Timmy Pinger's scanner: find the reachable hosts in one or more IPv4 subnets and
write a host list (inventory) for pingT.

ICMP only, no root needed (uses fping, like pingT). Pass 1 probes every address,
later passes re-probe only the addresses that stayed silent - so a host that drops a
single packet is still found, without re-scanning everything. All subnets are swept
together, so all fping processes are busy even with many small subnets.

  ./scan.py 10.0.10.0/24 10.0.20.0/24 -o hosts.txt
  ./scan.py -f subnets.txt -o hosts.txt
  ./scan.py --rescan hosts.txt          # scan its ranges again, only ADD new hosts, show a diff

Subnets file: one per line, '#' comments, optional group name:
  10.0.10.0/24   Servers VLAN10
  10.0.20.0/24   Clients 2nd floor
  10.0.30.0/24=Printers
  10.0.99.0/24

--rescan never removes or changes a host that is already in the list. The ranges come
from the '# 10.0.10.0/24: ...' lines the scanner writes above each group (or from -f /
subnets on the command line, for hand-written lists). A host commented out in the list
('# 10.0.10.5 old-pc') is not added again.

Sweeping unused addresses makes the router ARP for every one of them, so the scan is
rate limited (--rate, default 400 pings/s in total). Scan only networks you are
responsible for.
"""

from __future__ import annotations

import argparse
import ipaddress
import math
import os
import re
import shutil
import time
from datetime import datetime

# pingt first: it stops with a clear message if 'rich' is missing
from pingt import (CHARSETS, INLINE_COMMENT, FpingBackend, __version__, charset_option, check_charset, fmt_dur,
                   is_ip, parse_hosts_file)

from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn
from rich.table import Table

WORKERS = 4  # parallel fping processes; --rate is split across them
PASSES = 3  # probe rounds; later rounds only re-probe the addresses that stayed silent
CHUNK_MAX = 256  # addresses per fping process at most
MAX_SUBNET = 65536  # addresses per subnet without --force (= a /16)
MAX_TOTAL = 262144  # addresses per scan without --force (= four /16)

IPV4 = r"\d{1,3}(?:\.\d{1,3}){3}"
RANGE_LINE = re.compile(rf"^#\s*({IPV4}/\d{{1,2}})\s*:")  # "# 10.0.10.0/24: 11 of 254 addresses answered"
COMMENTED_HOST = re.compile(rf"^#\s*({IPV4})(?=[\s,]|$)")  # "# 10.0.10.5 old-pc": taken out on purpose


class ScanAborted(Exception):
    """fping kept failing - the results are incomplete."""


class Subnet:
    def __init__(self, net: ipaddress.IPv4Network, group: str | None):
        self.net = net
        self.group = group or str(net)
        self.targets: list[str] = []  # filled by expand(), after the size check
        self.alive: dict[str, int] = {}  # ip -> pass that found it

    def expand(self, owner: dict[str, Subnet]) -> int:
        """Fill self.targets and owner (ip -> its subnet); addresses already owned by an
        earlier subnet are skipped. Returns how many were skipped (overlap)."""
        overlap = 0
        for ip in map(str, self.net.hosts()):  # skips network + broadcast (/31, /32: every address)
            if ip in owner:
                overlap += 1
                continue
            owner[ip] = self
            self.targets.append(ip)
        return overlap

    def __len__(self):
        return len(self.targets)


def hosts_n(n: int) -> str:
    return f"{n} host" + ("" if n == 1 else "s")


def parse_subnet_spec(spec: str) -> tuple[str, str | None]:
    """ "10.0.0.0/24=Group name", "10.0.0.0/24  Group name" or "10.0.0.0/24" -> (cidr, group or None).
    The first word decides: with "=" in it, it's the "=" format, otherwise the rest of the line
    is the group name (which may then contain "=")."""
    parts = spec.split(None, 1)
    if not parts:
        return "", None
    if "=" in parts[0]:
        cidr, _, group = spec.strip().partition("=")
    else:
        cidr, group = parts[0], (parts[1] if len(parts) > 1 else "")
    return cidr.strip(), group.strip() or None


def parse_subnets_file(path: str) -> list[tuple[str, str | None]]:
    out = []
    with open(path, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if line and not line.startswith("#"):
                out.append(parse_subnet_spec(INLINE_COMMENT.sub("", line)))
    return out


def sweep(owner: dict[str, Subnet], backend: FpingBackend, console: Console, bar: bool) -> None:
    """Probes every address of all subnets, then re-probes only the silent ones."""
    failed_in_row = found = 0
    progress_bar = BarColumn() if bar else TextColumn("{task.percentage:>3.0f}%")
    with Progress(TextColumn("[bold]{task.fields[name]}"), progress_bar,
                  TextColumn("{task.completed}/{task.total} addr"),
                  TextColumn("[green]{task.fields[found]} found"),
                  TimeRemainingColumn(), console=console) as progress:
        for p in range(1, PASSES + 1):
            todo = [ip for ip, sub in owner.items() if ip not in sub.alive]
            if not todo:
                break
            # spread each pass over all fping processes, even when it's only a few addresses
            backend.chunk = max(1, min(CHUNK_MAX, math.ceil(len(todo) / WORKERS)))
            step = backend.chunk * WORKERS
            task = progress.add_task("", name=f"pass {p}/{PASSES}", total=len(todo), found=found)
            for i in range(0, len(todo), step):
                batch = todo[i:i + step]
                results, errors = backend.round(batch)
                if errors:
                    more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
                    progress.console.print(f"[bold red]scan error:[/bold red] {escape(errors[0])}{more}")
                    failed_in_row = failed_in_row + 1 if not results else 0
                    if failed_in_row >= 3:
                        raise ScanAborted(f"fping keeps failing: {errors[0]}")
                else:
                    failed_in_row = 0
                for ip, rtt in results.items():
                    if rtt is not None:
                        owner[ip].alive[ip] = p
                        found += 1
                progress.update(task, advance=len(batch), found=found)


def ip_sort_key(target: str):
    return (0, ipaddress.IPv4Address(target)) if is_ip(target) else (1, target)


def build_inventory(subnets: list[Subnet], owner: dict[str, Subnet]) -> str:
    """A new host list: one [group] per group, the subnets above it as '# CIDR: ...' lines
    (--rescan reads them back), one IP per line."""
    now = datetime.now()
    lines = [f"# generated by scan.py {__version__} on {now:%Y-%m-%d %H:%M}, ICMP sweep, {PASSES} passes",
             f"# {sum(len(s.alive) for s in subnets)} hosts answered out of {len(owner)} scanned addresses", ""]
    groups: dict[str, list[Subnet]] = {}  # several subnets can share a group
    for sub in subnets:
        groups.setdefault(sub.group, []).append(sub)
    for group, subs in groups.items():
        lines += [f"# {s.net}: {len(s.alive)} of {len(s)} addresses answered" for s in subs]
        lines.append(f"[{group}]")
        lines += sorted((ip for s in subs for ip in s.alive), key=ip_sort_key)
        lines.append("")
    return "\n".join(lines)


def read_inventory(path: str) -> tuple[list[str], list[tuple[str, str | None]], dict[str, tuple], set[str]]:
    """For --rescan: (the file's lines, its ranges as (cidr, group), known hosts {target: (label, group)},
    IPs of commented-out host lines). A '# CIDR: ...' line belongs to the [group] below it."""
    with open(path, encoding="utf-8-sig") as fh:
        lines = fh.read().splitlines()
    ranges: list[tuple[str, str | None]] = []
    pending: list[str] = []
    commented: set[str] = set()
    section = None
    for raw in lines:
        line = raw.strip()
        m = RANGE_LINE.match(line)
        if m:
            pending.append(m.group(1))
            continue
        if line.startswith("#"):
            m = COMMENTED_HOST.match(line)
            if m:
                commented.add(m.group(1))
            continue
        body = INLINE_COMMENT.sub("", line).strip()
        if body.startswith("[") and body.endswith("]"):
            section = body[1:-1].strip() or None
        elif not body:
            continue
        ranges += [(cidr, section) for cidr in pending]  # above a [group], or above host lines of the current one
        pending = []
    ranges += [(cidr, section) for cidr in pending]
    known: dict[str, tuple] = {}
    for target, label, group, _ in parse_hosts_file(path):
        known.setdefault(target, (label if label != target else "", group))
    return lines, ranges, known, commented - set(known)


def section_at(lines: list[str], group: str) -> int | None:
    """Index of the '[group]' line, or None."""
    for i, raw in enumerate(lines):
        body = INLINE_COMMENT.sub("", raw.strip()).strip()
        if body.startswith("[") and body.endswith("]") and body[1:-1].strip() == group:
            return i
    return None


def add_to_inventory(lines: list[str], new: dict[str, list[str]], new_ranges: dict[str, list[str]]) -> list[str]:
    """The file's lines with the new hosts added at the end of their [group] (a group that isn't
    there yet is added at the end). Nothing else is changed. new_ranges: '# CIDR:' lines to add
    above a group, so the next --rescan finds ranges that came from -f."""
    out = list(lines)
    today = f"{datetime.now():%Y-%m-%d}"
    for group in dict.fromkeys([*new, *new_ranges]):
        host_lines = [f"{ip:<16} # new in rescan {today}" for ip in sorted(new.get(group, []), key=ip_sort_key)]
        range_lines = [f"# {cidr}: range for scan.py --rescan (added {today})" for cidr in new_ranges.get(group, [])]
        hdr = section_at(out, group)
        if hdr is None:
            if out and out[-1].strip():
                out.append("")
            out += [*range_lines, f"[{group}]", *host_lines, ""]
            continue
        end = next((i for i in range(hdr + 1, len(out))
                    if INLINE_COMMENT.sub("", out[i].strip()).strip().startswith("[")), len(out))
        last = max((i for i in range(hdr, end) if out[i].strip() and not out[i].strip().startswith("#")),
                   default=hdr)  # after the group's last host line, before the next group's comments
        out[last + 1:last + 1] = host_lines
        out[hdr:hdr] = range_lines
    return out


def check_writable(path: str) -> str | None:
    """Why path can't be written, or None - checked before the scan, so no scan is lost."""
    folder = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(folder):
        return f"folder {folder} does not exist"
    if not os.access(folder, os.W_OK):
        return f"no permission to write in {folder}"
    if os.path.exists(path) and not os.access(path, os.W_OK):
        return f"no permission to write {path}"
    return None


def write_file(path: str, text: str) -> None:
    """Atomic write (temp file + rename); an existing file is kept as <path>.bak first."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(prog="scan.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"scan.py {__version__} (Timmy Pinger)")
    ap.add_argument("subnets", nargs="*", help="CIDRs to scan, optionally CIDR=Group name")
    ap.add_argument("-f", "--file", action="append", default=[], help="subnets file (repeatable)")
    ap.add_argument("-o", "--out", help="host list to write (default inventory.txt, with --rescan the "
                                        "rescanned file itself; the previous version is kept as .bak)")
    ap.add_argument("--rescan", metavar="FILE", help="scan the ranges of this host list again and only ADD the "
                                                     "new hosts; prints a diff")
    ap.add_argument("--overwrite", action="store_true", help="allow a new scan to replace an existing host list "
                                                             "(its labels are lost; use --rescan to keep them)")
    ap.add_argument("-t", "--timeout", type=int, default=500, help="reply timeout in ms (default 500)")
    ap.add_argument("--rate", type=int, default=400, help="max pings per second in total (default 400)")
    ap.add_argument("--force", action="store_true",
                    help=f"allow subnets larger than /16 ({MAX_SUBNET} addresses) and scans larger "
                         f"than {MAX_TOTAL} addresses")
    env = charset_option(ap)
    args = ap.parse_args()
    check_charset(ap, args, env)
    cs = CHARSETS[args.charset]

    console = Console()
    if not shutil.which("fping"):
        ap.error("fping not found in PATH (apt install fping)")
    for ok, msg in [(50 <= args.timeout <= 10000, "--timeout must be 50..10000 ms"),
                    (args.rate >= 10, "--rate must be >= 10 pings/s")]:
        if not ok:
            ap.error(msg)

    old_lines: list[str] = []
    known: dict[str, tuple] = {}
    commented: set[str] = set()
    file_ranges: list[tuple[str, str | None]] = []
    if args.rescan:
        try:
            old_lines, file_ranges, known, commented = read_inventory(args.rescan)
        except OSError as e:
            ap.error(f"cannot read --rescan host list: {e}")

    entries = []
    for path in args.file:
        try:
            entries += parse_subnets_file(path)
        except OSError as e:
            ap.error(f"cannot read subnets file: {e}")
    entries += [parse_subnet_spec(spec) for spec in args.subnets]
    if args.rescan and not entries:
        entries = file_ranges
        if not entries:
            ap.error(f"{args.rescan} has no scanned ranges (no '# 10.0.10.0/24: ...' lines above its groups) - "
                     f"give them with -f subnets.txt or on the command line")
    if not entries:
        ap.error("no subnets given (e.g. 10.0.10.0/24, or -f subnets.txt)")

    # parse and size-check everything BEFORE expanding any address list
    subnets, seen = [], set()
    for cidr, group in entries:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError as e:
            ap.error(f"invalid subnet '{cidr}': {e}")
        if net.version != 4:
            ap.error(f"{net}: IPv6 is not supported (IPv4 only, see DECISIONS.md)")
        if net.num_addresses > MAX_SUBNET and not args.force:
            ap.error(f"{net} has {net.num_addresses:,} addresses - more than a /16. "
                     f"Split it into smaller subnets, or use --force if you really mean it.")
        if net in seen:
            console.print(f"[yellow]note:[/yellow] {net} listed twice, scanning it once")
            continue
        seen.add(net)
        subnets.append(Subnet(net, group))
    size = sum(s.net.num_addresses for s in subnets)
    if size > MAX_TOTAL and not args.force:
        ap.error(f"{size:,} addresses in total - more than {MAX_TOTAL:,}. Scan fewer subnets per run, "
                 f"or use --force.")

    out = args.out or args.rescan or "inventory.txt"
    if not args.rescan and os.path.exists(out) and not args.overwrite:
        ap.error(f"{out} exists - use --rescan {out} to add new hosts to it, or --overwrite to replace it "
                 f"(the old one is kept as {out}.bak)")
    for path in (out, out + ".partial"):
        problem = check_writable(path)
        if problem:
            ap.error(f"cannot write the result: {problem}")

    owner: dict[str, Subnet] = {}  # ip -> the subnet it is scanned for
    for sub in subnets:
        overlap = sub.expand(owner)
        if overlap:
            console.print(f"[yellow]note:[/yellow] {sub.net} overlaps an earlier subnet - {overlap} addresses "
                          f"stay in the earlier group")

    total = len(owner)
    spacing = max(1, round(1000 * WORKERS / args.rate))  # ms between probes per fping process
    backend = FpingBackend(args.timeout, spacing, CHUNK_MAX, workers=WORKERS)
    batches = math.ceil(total / (CHUNK_MAX * WORKERS)) if total else 0
    eta = total / args.rate + args.timeout / 1000 * batches
    console.print(f"[bold]{'rescanning ' + escape(args.rescan) + ':' if args.rescan else 'scanning'}[/bold] "
                  f"{len(subnets)} subnets, {total:,} addresses, "
                  f"{PASSES} passes, ~{args.rate} pings/s (first pass ~{fmt_dur(eta)})")

    t0 = time.time()
    interrupted = None
    try:
        sweep(owner, backend, console, bar=cs.name == "unicode")
    except KeyboardInterrupt:
        interrupted = "interrupted"
    except ScanAborted as e:
        interrupted = str(e)

    alive = {ip for s in subnets for ip in s.alive}
    if args.rescan:
        new: dict[str, list[str]] = {}
        for ip in sorted(alive - set(known) - commented, key=ip_sort_key):
            new.setdefault(owner[ip].group, []).append(ip)
        listed = {r[0] for r in file_ranges}
        new_ranges: dict[str, list[str]] = {}
        for s in subnets:
            if str(s.net) not in listed:  # came from -f: remember it in the file for the next --rescan
                new_ranges.setdefault(s.group, []).append(str(s.net))
        changed = bool(new or new_ranges)
        text = "\n".join(add_to_inventory(old_lines, new, new_ranges)) + "\n"
    else:
        changed = True
        text = build_inventory(subnets, owner)
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

    table = Table(header_style="bold", box=cs.box)
    for col in ("subnet", "group", "scanned", "found", "found only in pass 2+"):
        table.add_column(col, justify="left" if col in ("subnet", "group") else "right")
    for sub in subnets:
        late = sum(1 for found_in in sub.alive.values() if found_in > 1)
        table.add_row(str(sub.net), escape(sub.group), str(len(sub)), str(len(sub.alive)), str(late),
                      style="green" if sub.alive else "yellow")
    console.print(table)
    if not args.rescan:
        console.print(f"{hosts_n(len(alive))} in {fmt_dur(time.time() - t0)} -> [bold]{escape(out)}[/bold]")
    else:
        print_diff(console, args.rescan, out, owner, alive, known, commented, new, changed)

    if interrupted:
        raise SystemExit(130 if interrupted == "interrupted" else 1)


def print_diff(console: Console, name: str, out: str, owner: dict[str, Subnet], alive: set[str],
               known: dict[str, tuple], commented: set[str], new: dict[str, list[str]], changed: bool):
    """What the rescan found compared to the host list."""
    new_ips = [ip for ips in new.values() for ip in ips]
    in_ranges = [t for t in known if t in owner]
    answered = [t for t in in_ranges if t in alive]
    missing = [t for t in in_ranges if t not in alive]
    not_scanned = len(known) - len(in_ranges)
    revived = sorted(commented & alive, key=ip_sort_key)

    def row(mark: str, target: str, style: str):
        label, group = known.get(target, ("", None))
        group = group or owner[target].group
        console.print(f"  [{style}]{mark} {escape(f'{target:<16}')}[/{style}] {escape(label)}  [dim]{escape(group)}[/dim]")

    console.print(f"\n[bold]rescan of {escape(name)}:[/bold] [green]{len(new_ips)} new[/green], "
                  f"{len(answered)} known answered, "
                  f"[yellow]{len(missing)} known without reply (kept)[/yellow]"
                  + (f", {not_scanned} outside the ranges (not scanned)" if not_scanned else ""))
    if new_ips:
        console.print(f"[green]+ new, added to {escape(out)} (marked '# new in rescan'):[/green]")
        for ip in new_ips:
            row("+", ip, "green")
    if missing:
        console.print("[yellow]- no reply this time, kept unchanged:[/yellow]")
        for t in missing:
            row("-", t, "yellow")
    if revived:
        console.print(f"[cyan]# commented out in {escape(name)} but answering, not added:[/cyan] "
                      + ", ".join(revived))
    if not changed:
        console.print(f"[dim]no new hosts - {escape(out)} not changed[/dim]")


if __name__ == "__main__":
    main()
