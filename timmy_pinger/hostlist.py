"""Host lists: reading them and checking that every entry can be pinged. IPv4 only (DECISIONS.md)."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass

from . import dns
from .host import INVALID, Host

INLINE_COMMENT = re.compile(r"\s+#(\s.*)?$")  # " # note" ends a line, "Room #12" and "host #note" don't
HOSTNAME = re.compile(r"^(?=.{1,253}\.?$)[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?"
                      r"(\.[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9])?)*\.?$")


@dataclass(frozen=True)
class Entry:
    """One host line of a host list."""
    target: str
    label: str  # the target itself when the line has no name
    group: str | None  # None: no [group] above it and none in the comma format
    source: str  # "hosts.txt:12"


def read_host_list(path: str) -> list[Entry]:
    """Lines: "IP [name]", "ip,name,group", "[group]" starts a group, "[]" ends it, "#" comments.
    The full format: README.md "Host list format"."""
    entries = []
    group = None
    with open(path, encoding="utf-8-sig") as fh:  # -sig: tolerate the BOM of Excel exports
        for number, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = INLINE_COMMENT.sub("", line).strip()
            if line.startswith("[") and line.endswith("]"):
                group = line[1:-1].strip() or None
                continue
            if "," in line.split(None, 1)[0]:  # comma format: ip,name,group
                cols = [c.strip() for c in line.split(",")]
                target = cols[0]
                label = cols[1] if len(cols) > 1 and cols[1] else target
                entry_group = cols[2] if len(cols) > 2 and cols[2] else group
            else:
                target, *name = line.split(None, 1)  # spaces or tabs
                label = name[0].strip() if name else target
                entry_group = group
            entries.append(Entry(target, label, entry_group, f"{os.path.basename(path)}:{number}"))
    return entries


def is_ipv4(target: str) -> bool:
    try:
        ipaddress.IPv4Address(target)
        return True
    except ValueError:
        return False


def is_ipv6(target: str) -> bool:
    try:
        return ipaddress.ip_address(target).version == 6
    except ValueError:
        return False


def default_group(target: str) -> str:
    """The group of an entry without one: its /24, or "other" for a DNS name."""
    if not is_ipv4(target):
        return "other"
    return str(ipaddress.IPv4Network(f"{target}/24", strict=False))


def make_hosts(entries: list[Entry]) -> list[Host]:
    """One Host per target; when a target is listed twice, the first entry counts."""
    hosts: list[Host] = []
    seen = set()
    for e in entries:
        if e.target not in seen:
            seen.add(e.target)
            hosts.append(Host(e.target, e.label, e.group or default_group(e.target), len(hosts), source=e.source))
    return hosts


def check_targets(hosts: list[Host]):
    """Sets host.ip, or host.invalid (and the INVALID state) for entries that can't be pinged.
    DNS names are resolved here, once: fping only ever gets IPs."""
    names = []
    for h in hosts:
        if is_ipv4(h.target):
            h.ip = h.target
        elif is_ipv6(h.target):
            h.invalid = "IPv6 is not supported (IPv4 only)"
        elif re.fullmatch(r"[\d.]+", h.target):
            h.invalid = "not a valid IPv4 address"
        elif not HOSTNAME.match(h.target):
            h.invalid = "not an IP address or hostname"
        else:
            names.append(h)
    dns.resolve_hostnames(names)
    for h in hosts:
        if h.invalid:
            h.state = INVALID
