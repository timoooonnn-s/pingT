#!/usr/bin/env python3
"""
scan.py - Timmy Pinger's scanner: find the reachable hosts in one or more subnets and
write an inventory for pingT.

ICMP only, no root needed (uses fping, like pingT). Pass 1 probes every address,
later passes re-probe only the addresses that stayed silent - so a host that drops a
single packet is still found, without re-scanning everything. All subnets are swept
together, so all fping processes are busy even with many small subnets.

  ./scan.py 10.0.10.0/24 10.0.20.0/24 -o inventory.txt
  ./scan.py -f subnets.txt -o inventory.txt --then-ping
  ./scan.py -f subnets.txt --merge inventory.txt -o inventory.txt

Subnets file: one per line, '#' comments, optional group name:
  10.0.10.0/24   Servers VLAN10
  10.0.20.0/24   Clients EG
  10.0.30.0/24

Sweeping unused addresses makes the router ARP for every one of them, so the scan is
rate limited (--rate, default 400 pings/s in total). Scan only networks you are
responsible for.
"""

from __future__ import annotations

import argparse
import ipaddress
import math
import os
import shutil
import sys
import time
from datetime import datetime

# pingt first: it stops with a clear message if 'rich' is missing
from pingt import INLINE_COMMENT, FpingBackend, auto_group, fmt_dur, is_ip, parse_hosts_file

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn
from rich.table import Table

WORKERS = 4  # parallel fping processes; --rate is split across them
MAX_SUBNET = 65536  # addresses per subnet without --force (= a /16)
MAX_TOTAL = 262144  # addresses per scan without --force (= four /16)


class ScanAborted(Exception):
    """fping kept failing - the results are incomplete."""


class Subnet:
    def __init__(self, net: ipaddress.IPv4Network | ipaddress.IPv6Network, group: str | None):
        self.net = net
        self.group = group or str(net)
        self.targets: list[str] = []  # filled by expand(), after the size check
        self.alive: dict[str, float] = {}  # ip -> best rtt seen
        self.first_pass: dict[str, int] = {}  # ip -> pass that first found it

    def expand(self, all_addresses: bool, taken: set[str]) -> int:
        """Fill self.targets; addresses already owned by an earlier subnet are skipped.
        Returns how many were skipped (overlap)."""
        if self.net.prefixlen >= self.net.max_prefixlen - 1 or all_addresses:
            addresses = iter(self.net)  # /31, /32 (or --all-addresses): every address
        else:
            addresses = self.net.hosts()  # skips network + broadcast
        overlap = 0
        for ip in addresses:
            ip = str(ip)
            if ip in taken:
                overlap += 1
                continue
            taken.add(ip)
            self.targets.append(ip)
        return overlap

    def __len__(self):
        return len(self.targets)


def parse_subnets_file(path: str) -> list[tuple[str, str | None]]:
    out = []
    with open(path, encoding="utf-8-sig") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = INLINE_COMMENT.sub("", line).strip()
            cidr, _, group = (c.strip() for c in line.partition("="))
            if not group:  # "10.0.0.0/24  Group name" (whitespace separated)
                parts = cidr.split(None, 1)
                cidr, group = parts[0], (parts[1].strip() if len(parts) > 1 else "")
            out.append((cidr, group or None))
    return out


def sweep(subnets: list[Subnet], backend: FpingBackend, passes: int, chunk_max: int,
          console: Console) -> None:
    """Probes every address of all subnets, then re-probes only the silent ones."""
    owner = {ip: sub for sub in subnets for ip in sub.targets}
    failed_in_row = 0

    def found() -> int:
        return sum(len(s.alive) for s in subnets)

    with Progress(TextColumn("[bold]{task.fields[name]}"), BarColumn(),
                  TextColumn("{task.completed}/{task.total} addr"),
                  TextColumn("[green]{task.fields[found]} found"),
                  TimeRemainingColumn(), console=console) as progress:
        for p in range(1, passes + 1):
            todo = [ip for ip, sub in owner.items() if ip not in sub.alive]
            if not todo:
                break
            # spread each pass over all fping processes, even when it's only a few addresses
            backend.chunk = max(1, min(chunk_max, math.ceil(len(todo) / WORKERS)))
            step = backend.chunk * WORKERS
            task = progress.add_task("", name=f"pass {p}/{passes}", total=len(todo), found=found())
            for i in range(0, len(todo), step):
                batch = todo[i:i + step]
                results, errors = backend.round(batch)
                if errors:
                    more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
                    progress.console.print(f"[bold red]scan error:[/bold red] {errors[0]}{more}")
                    failed_in_row = failed_in_row + 1 if not results else 0
                    if failed_in_row >= 3:
                        raise ScanAborted(f"fping keeps failing: {errors[0]}")
                else:
                    failed_in_row = 0
                for ip, rtt in results.items():
                    if rtt is not None:  # answered: keep the best rtt seen
                        sub = owner[ip]
                        previous = sub.alive.get(ip)
                        sub.alive[ip] = rtt if previous is None else min(rtt, previous)
                        sub.first_pass.setdefault(ip, p)
                progress.update(task, advance=len(batch), found=found())


def ip_sort_key(target: str):
    return (0, ipaddress.ip_address(target)) if is_ip(target) else (1, target)


def build_inventory(subnets: list[Subnet], passes: int, old: dict[str, tuple[str, str | None]],
                    keep_missing: bool, merge_name: str) -> tuple[str, list[str], list[str]]:
    """pingT inventory text: one [group] section per subnet, one IP per line.

    Hosts from the --merge inventory are never lost silently:
    - inside a scanned subnet but no reply: kept, marked "# no reply in scan <date>"
      (left out with keep_missing=False, i.e. --drop-missing)
    - outside the scanned subnets (or hostnames): not scanned, so kept unchanged in their old group
    Returns (text, kept without reply, kept because not scanned).
    """
    today = f"{datetime.now():%Y-%m-%d}"
    alive_all = {ip for s in subnets for ip in s.alive}
    no_reply: list[str] = []
    not_scanned: list[str] = []
    sections: dict[str, list[tuple[str, bool]]] = {s.group: [] for s in subnets}  # group -> (target, no reply)
    for sub in subnets:
        sections[sub.group] += [(ip, False) for ip in sub.alive]
    for target, (_, old_group) in old.items():
        if target in alive_all:
            continue
        sub = next((s for s in subnets if is_ip(target) and ipaddress.ip_address(target) in s.net), None)
        if sub:  # scanned, didn't answer
            if keep_missing:
                sections[sub.group].append((target, True))
                no_reply.append(target)
        else:  # not part of this scan: keep it as it was
            sections.setdefault(old_group or auto_group(target), []).append((target, False))
            not_scanned.append(target)

    summary = [f"{len(no_reply)} kept from {merge_name} without reply" if no_reply else "",
               f"{len(not_scanned)} kept from {merge_name} (not scanned)" if not_scanned else ""]
    lines = [f"# generated by scan.py on {datetime.now():%Y-%m-%d %H:%M}, ICMP sweep, {passes} passes",
             f"# {len(alive_all)} hosts answered out of {sum(len(s) for s in subnets)} scanned addresses"
             + "".join(f", {x}" for x in summary if x), ""]
    by_group = {s.group: s for s in subnets}
    for group, entries in sections.items():
        sub = by_group.get(group)
        if sub:
            lines.append(f"# {sub.net}: {len(sub.alive)} of {len(sub)} addresses answered")
        else:
            lines.append(f"# not in the scanned subnets, kept unchanged from {merge_name}")
        lines.append(f"[{group}]")
        for target, silent in sorted(entries, key=lambda e: ip_sort_key(e[0])):
            label = old.get(target, ("", None))[0]  # keep the label from --merge, if any
            line = f"{target:<16} {label}".rstrip()
            if silent:
                line += f"  # no reply in scan {today}"
            lines.append(line)
        lines.append("")
    return "\n".join(lines), no_reply, not_scanned


def csv_text(subnets: list[Subnet], passes: int) -> str:
    rows = ["ip,subnet,group,found_in_pass,total_passes,best_rtt_ms"]
    for sub in subnets:
        for ip in sorted(sub.alive, key=ip_sort_key):
            rows.append(f"{ip},{sub.net},{sub.group},{sub.first_pass.get(ip, 0)},{passes},{sub.alive[ip]:.2f}")
    return "\n".join(rows) + "\n"


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
    ap.add_argument("subnets", nargs="*", help="CIDRs to scan, optionally CIDR=Group name")
    ap.add_argument("-f", "--file", action="append", default=[], help="subnets file (repeatable)")
    ap.add_argument("-o", "--out", default="inventory.txt",
                    help="inventory to write (default inventory.txt; the previous version is kept as .bak)")
    ap.add_argument("--csv", help="also write per-host results (ip, subnet, group, pass that found it, best rtt)")
    ap.add_argument("-p", "--passes", type=int, default=3, help="probe rounds (default 3)")
    ap.add_argument("-t", "--timeout", type=int, default=500, help="reply timeout in ms (default 500)")
    ap.add_argument("--rate", type=int, default=400, help="max pings per second in total (default 400)")
    ap.add_argument("--chunk", type=int, default=256, help="max addresses per fping process (default 256)")
    ap.add_argument("--all-addresses", action="store_true",
                    help="also probe the network and broadcast address of each subnet")
    ap.add_argument("--merge", help="existing inventory: keep its labels, and keep its hosts that did not "
                                    "answer (marked with a comment)")
    ap.add_argument("--drop-missing", action="store_true",
                    help="with --merge: leave out hosts that did not answer instead of keeping them")
    ap.add_argument("--force", action="store_true",
                    help=f"allow subnets larger than /16 ({MAX_SUBNET} addresses) and scans larger "
                         f"than {MAX_TOTAL} addresses")
    ap.add_argument("--then-ping", action="store_true", help="start pingT with the new inventory (--fresh)")
    args = ap.parse_args()

    console = Console()
    if not shutil.which("fping"):
        ap.error("fping not found in PATH (apt install fping)")
    for ok, msg in [(args.passes >= 1, "--passes must be >= 1"),
                    (50 <= args.timeout <= 10000, "--timeout must be 50..10000 ms"),
                    (args.rate >= 10, "--rate must be >= 10 pings/s"),
                    (args.chunk >= 1, "--chunk must be >= 1"),
                    (not args.drop_missing or args.merge, "--drop-missing only makes sense with --merge")]:
        if not ok:
            ap.error(msg)

    entries = []
    for path in args.file:
        try:
            entries += parse_subnets_file(path)
        except OSError as e:
            ap.error(f"cannot read subnets file: {e}")
    for spec in args.subnets:
        cidr, _, group = spec.partition("=")
        entries.append((cidr.strip(), group.strip() or None))
    if not entries:
        ap.error("no subnets given (e.g. 10.0.10.0/24, or -f subnets.txt)")

    # parse and size-check everything BEFORE expanding any address list
    subnets, seen = [], set()
    for cidr, group in entries:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError as e:
            ap.error(f"invalid subnet '{cidr}': {e}")
        if net.num_addresses > MAX_SUBNET and not args.force:
            ap.error(f"{net} has {net.num_addresses:,} addresses - more than a /16. "
                     f"Split it into smaller subnets, or use --force if you really mean it.")
        if net in seen:
            console.print(f"[yellow]note:[/yellow] {net} listed twice, scanning it once")
            continue
        seen.add(net)
        subnets.append(Subnet(net, group))
    total = sum(s.net.num_addresses for s in subnets)
    if total > MAX_TOTAL and not args.force:
        ap.error(f"{total:,} addresses in total - more than {MAX_TOTAL:,}. Scan fewer subnets per run, "
                 f"or use --force.")
    taken: set[str] = set()
    for sub in subnets:
        overlap = sub.expand(args.all_addresses, taken)
        if overlap:
            console.print(f"[yellow]note:[/yellow] {sub.net} overlaps an earlier subnet - {overlap} addresses "
                          f"stay in the earlier group")

    old: dict[str, tuple[str, str | None]] = {}
    if args.merge:
        try:
            for target, label, group, _ in parse_hosts_file(args.merge):
                old.setdefault(target, (label if label != target else "", group))
        except OSError as e:
            ap.error(f"cannot read --merge inventory: {e}")

    total = sum(len(s) for s in subnets)
    spacing = max(1, round(1000 * WORKERS / args.rate))  # ms between probes per fping process
    backend = FpingBackend(args.timeout, spacing, args.chunk, workers=WORKERS)
    batches = math.ceil(total / (args.chunk * WORKERS)) if total else 0
    eta = total / args.rate + args.timeout / 1000 * batches
    console.print(f"[bold]scanning[/bold] {len(subnets)} subnets, {total:,} addresses, "
                  f"{args.passes} passes, ~{args.rate} pings/s (first pass ~{fmt_dur(eta)})")

    t0 = time.time()
    interrupted = None
    try:
        sweep(subnets, backend, args.passes, args.chunk, console)
    except KeyboardInterrupt:
        interrupted = "interrupted"
    except ScanAborted as e:
        interrupted = str(e)

    text, no_reply, not_scanned = build_inventory(subnets, args.passes, old, keep_missing=not args.drop_missing,
                                 merge_name=os.path.basename(args.merge) if args.merge else "")
    out, csv_out = args.out, args.csv
    if interrupted:  # incomplete: never replace a good inventory with it
        out, csv_out = args.out + ".partial", (args.csv + ".partial" if args.csv else None)
        console.print(f"[yellow]{interrupted} - results are incomplete, written to {out} "
                      f"({args.out} was not touched)[/yellow]")
    write_file(out, text)
    if csv_out:
        write_file(csv_out, csv_text(subnets, args.passes))

    table = Table(header_style="bold")
    for col in ("subnet", "group", "scanned", "found", "found only in pass 2+"):
        table.add_column(col, justify="left" if col in ("subnet", "group") else "right")
    for sub in subnets:
        late = sum(1 for first in sub.first_pass.values() if first > 1)
        table.add_row(str(sub.net), sub.group, str(len(sub)), str(len(sub.alive)), str(late),
                      style="green" if sub.alive else "yellow")
    console.print(table)
    found = sum(len(s.alive) for s in subnets)
    console.print(f"{found} hosts in {fmt_dur(time.time() - t0)} -> [bold]{out}[/bold]"
                  + (f", {csv_out}" if csv_out else ""))

    def listing(targets: list[str]) -> str:
        return "  " + ", ".join(targets[:30]) + (" …" if len(targets) > 30 else "")

    if old:
        alive_all = {ip for s in subnets for ip in s.alive}
        silent = [t for t in old if t not in alive_all and t not in not_scanned]
        if silent and no_reply:
            console.print(f"[yellow]{len(silent)} hosts from {args.merge} did not answer[/yellow] - kept, "
                          f"marked '# no reply in scan' (--drop-missing leaves them out):")
            console.print(listing(silent))
        elif silent:
            console.print(f"[yellow]{len(silent)} hosts from {args.merge} did not answer[/yellow] "
                          f"- left out because of --drop-missing:")
            console.print(listing(silent))
        if not_scanned:
            console.print(f"[dim]{len(not_scanned)} hosts from {args.merge} are outside the scanned subnets "
                          f"- kept unchanged:[/dim]")
            console.print(listing(not_scanned))

    if interrupted:
        raise SystemExit(130 if interrupted == "interrupted" else 1)
    if args.then_ping:
        launcher = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pingT")
        console.print(f"[dim]starting {launcher} -f {out} --fresh[/dim]")
        # execv: pingT replaces this process, so Ctrl-C / kill reach pingT directly
        os.execv(sys.executable, [sys.executable, launcher, "-f", out, "--fresh"])


if __name__ == "__main__":
    main()
