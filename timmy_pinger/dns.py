"""DNS: host list names -> IPs once at start, and the reverse lookups behind the "n" key."""

from __future__ import annotations

import queue
import socket
import threading
import time
from collections import Counter
from typing import TYPE_CHECKING

from .durations import duration

if TYPE_CHECKING:
    from .host import Host
    from .session import Session

WORKERS = 32  # lookups in flight at once
RESOLVE_TIMEOUT_S = 5  # per name at start, counted from when its own lookup begins
RETRY_S = 2  # between two rounds of reverse-lookup retries
SERVER_DOWN_PAUSE_S = 60  # while the DNS server doesn't answer: one probe per this many seconds
MAX_TRIES = 5  # reverse lookups without a usable answer before a host is given up


def start_workers(func, count: int) -> tuple[queue.SimpleQueue, queue.SimpleQueue]:
    """count threads: an item put into jobs comes out of results as (item, func(item)); a None
    ends one thread. Daemon threads, so a hanging lookup never blocks quitting."""
    jobs: queue.SimpleQueue = queue.SimpleQueue()
    results: queue.SimpleQueue = queue.SimpleQueue()

    def worker():
        while (item := jobs.get()) is not None:
            results.put((item, func(item)))

    for _ in range(count):
        threading.Thread(target=worker, daemon=True).start()
    return jobs, results


def resolve_hostnames(hosts: list[Host]):
    """Sets host.ip to the IPv4 address of host.target, or host.invalid. Each name gets
    RESOLVE_TIMEOUT_S from when its own lookup starts, so on a slow DNS server a long list
    doesn't time out the names still waiting in line."""
    if not hosts:
        return
    started: dict[int, float] = {}  # host order -> when its lookup began

    def lookup(h: Host) -> list:
        started[h.order] = time.time()
        try:
            return socket.getaddrinfo(h.target, None, socket.AF_INET)
        except OSError:
            return []

    count = min(WORKERS, len(hosts))
    jobs, results = start_workers(lookup, count)
    for h in [*hosts, *[None] * count]:  # the Nones end the workers once every name is done
        jobs.put(h)
    waiting = {h.order: h for h in hosts}
    while waiting:
        try:
            h, answers = results.get(timeout=0.2)
            if waiting.pop(h.order, None):  # else: it answered after its timeout
                if answers:
                    h.ip = answers[0][4][0]
                else:
                    h.invalid = "hostname has no IPv4 address"
        except queue.Empty:
            pass
        now = time.time()
        for order in [o for o in waiting if now - started.get(o, now) > RESOLVE_TIMEOUT_S]:
            waiting.pop(order).invalid = f"no DNS answer within {RESOLVE_TIMEOUT_S}s"


def reverse_lookup(ip: str) -> tuple[bool, str | None]:
    """(answered, name). "No PTR record" is an answer: (True, None). (False, None) = no usable
    answer (timeout), may work later.

    getnameinfo, not gethostbyaddr: on macOS Python runs gethostbyaddr behind a global lock,
    so parallel lookups would still go out one at a time."""
    try:
        return True, socket.getnameinfo((ip, 0), socket.NI_NAMEREQD)[0].rstrip(".") or None
    except socket.gaierror as e:
        # EAI_NONAME / EAI_NODATA: the server answered "no PTR record"
        # EAI_AGAIN (timeout), EAI_FAIL (SERVFAIL): no usable answer
        return e.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)), None
    except UnicodeError:  # a PTR record that isn't a valid name: an answer, just a useless one
        return True, None
    except OSError:
        return False, None


def look_up_dns_names(session: Session):
    """The reverse lookups, in their own thread: every host is asked until it gets an answer,
    then never again.

    All open hosts are asked at once (WORKERS in flight), names show up as they come in.
    Hosts without a usable answer are asked again in the next round and given up after
    MAX_TRIES (they show their IP). When a whole round gets no answer and a host that did
    answer before (the canary) doesn't either, the DNS server counts as unreachable - e.g.
    behind the switch being migrated. Then only one probe per SERVER_DOWN_PAUSE_S goes out
    until it answers again, and those rounds use up no tries.
    """
    todo: list[Host] = []
    with session.lock:
        for h in session.hosts:
            if h.invalid:
                continue
            if h.target == h.ip:
                todo.append(h)
            else:
                h.dns_name = h.target  # written as a DNS name in the host list: no lookup needed
        session.dns_pending = len(todo)
    if not todo:
        return
    count = min(WORKERS, len(todo))
    jobs, results = start_workers(lambda h: reverse_lookup(h.ip), count)
    started = time.time()
    tries: Counter[int] = Counter()  # by host order
    canary: str | None = None  # an IP that got an answer: tells "server down" from "this host fails"
    named = without_name = given_up = 0
    while todo and not session.stop.is_set():
        if session.dns_server_down:  # one probe first; the full round only once the server answers
            if not reverse_lookup(canary or todo[0].ip)[0]:
                session.stop.wait(SERVER_DOWN_PAUSE_S)
                continue
            with session.lock:
                session.dns_server_down = False
        for h in todo:
            jobs.put(h)
        failed: list[Host] = []
        for _ in todo:
            h, (answered, name) = results.get()
            if not answered:
                failed.append(h)
                continue
            canary = canary or h.ip
            named += name is not None
            without_name += name is None
            with session.lock:
                h.dns_name = name
                session.dns_pending -= 1
        server_down = len(failed) == len(todo) and (canary is None or not reverse_lookup(canary)[0])
        if not server_down:
            for h in failed:
                tries[h.order] += 1
            given_up += sum(tries[h.order] >= MAX_TRIES for h in failed)
            failed = [h for h in failed if tries[h.order] < MAX_TRIES]
        todo = failed
        with session.lock:
            session.dns_pending = len(todo)
            session.dns_server_down = server_down
            if not todo:
                session.log.write(f"dns done in {duration(time.time() - started)}: {named} names, "
                                  f"{without_name} without a DNS name, {given_up} gave no answer", "dim")
        if todo:
            session.stop.wait(SERVER_DOWN_PAUSE_S if server_down else RETRY_S)
    for _ in range(count):  # done, or quitting: end the workers
        jobs.put(None)
