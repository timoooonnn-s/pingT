"""pingT without a terminal: host lists, the state machine, the session, keys, screen and summary."""
from __future__ import annotations

import io
import json
import re
import time
import unittest

from helpers import WorkdirTest, read
from rich.console import Console

from timmy_pinger import CREDITS, __version__
from timmy_pinger.baseline import Baseline
from timmy_pinger.charsets import CHARSETS
from timmy_pinger.controls import Controls
from timmy_pinger.durations import duration, short_duration
from timmy_pinger.events import EventLog
from timmy_pinger.host import DOWN, IGNORED, INVALID, LOSS, OK, SILENT, WARN, Host, Limits, back_counts
from timmy_pinger.hostlist import check_targets, default_group, is_ipv4, make_hosts, read_host_list
from timmy_pinger.keyboard import UNFINISHED_SEQUENCE, split_keys
from timmy_pinger.screen import NAME_MODES, Screen, ViewSettings, display_name
from timmy_pinger.session import Session
from timmy_pinger.summary import save_summary, write_summary


class Run:
    """A session with its controls and screen, like pingT.py builds them."""

    def __init__(self, test: WorkdirTest, targets, groups=None, fresh=True, dns=False):
        hosts = [Host(t, f"lbl{i}", groups[i] if groups else "G", i) for i, t in enumerate(targets)]
        check_targets(hosts)
        self.log = EventLog(test.path("t.log"), 8)
        test.addCleanup(self.log.close)
        self.session = Session(hosts, Limits(window=20, warn=2, loss=3, down=3), self.log,
                               Baseline(test.path("t-baseline.json")), fresh)
        self.view = ViewSettings(CHARSETS["unicode"])
        self.controls = Controls(self.session, self.view, dns_enabled=dns)
        self.screen = Screen(self.session, self.view, 1.0, 8)
        self.hosts = hosts

    def round(self, down=(), now=None):
        """One ping round: every host answers, except those in down (hosts or targets)."""
        lost = {h.ip if isinstance(h, Host) else h for h in down}
        self.session.record_round({h.ip: None if h.ip in lost else 1.0 for h in self.hosts if h.ip},
                                  now or time.time())

    def keys(self, *keys):
        for key in keys:
            self.controls.press(key)

    def screen_events(self) -> list[str]:
        return [line.plain[9:] for line in self.log.recent]  # without the time

    def log_file(self) -> str:
        return read(self.log.path)

    def render(self, width=100, height=30) -> list[str]:
        out = io.StringIO()
        Console(file=out, width=width, height=height, color_system=None).print(self.screen.render(width, height))
        return out.getvalue().splitlines()

    def summary(self) -> str:
        out = io.StringIO()
        write_summary(Console(file=out, width=140, color_system=None), self.session, self.view.charset, 1.0)
        return out.getvalue()


class TestHostList(WorkdirTest):
    def test_formats(self):
        path = self.write("h.txt", "﻿# comment\n[Servers VLAN10]\n10.0.10.1    core-sw01   # note\n10.0.10.11\n"
                                   "switch01.corp.local   sw01\n10.0.20.7,printer-2f,Office\n10.0.20.8, p8 ,\n[]\n"
                                   "10.0.50.7\n10.0.50.8 Room #12\n10.0.50.9 host #note\n[ Spaced ]\n10.0.60.1\t tabbed\n")
        e = read_host_list(path)
        self.assertEqual((e[0].target, e[0].label, e[0].group, e[0].source),
                         ("10.0.10.1", "core-sw01", "Servers VLAN10", "h.txt:3"))
        self.assertEqual((e[1].label, e[1].group), ("10.0.10.11", "Servers VLAN10"))
        self.assertEqual((e[3].label, e[3].group), ("printer-2f", "Office"))
        self.assertEqual((e[4].label, e[4].group), ("p8", "Servers VLAN10"))  # empty comma group -> [group]
        self.assertIsNone(e[5].group)  # [] ends the group
        self.assertEqual(e[6].label, "Room #12")
        self.assertEqual(e[7].label, "host #note")  # "#" needs a space after it to start a comment
        self.assertEqual((e[8].target, e[8].label, e[8].group), ("10.0.60.1", "tabbed", "Spaced"))

    def test_make_hosts(self):
        hosts = make_hosts(read_host_list(self.write("h.txt", "10.0.0.1 a\n10.0.0.1 b\nsw.corp\n")))
        self.assertEqual([(h.target, h.label, h.group) for h in hosts],
                         [("10.0.0.1", "a", "10.0.0.0/24"), ("sw.corp", "sw.corp", "other")])

    def test_ipv4_only(self):
        self.assertEqual(default_group("10.0.50.7"), "10.0.50.0/24")
        self.assertFalse(is_ipv4("::1"))

    def test_check_targets(self):
        hosts = [Host(t, t, "g", i) for i, t in enumerate(
            ["10.0.0.1", "10.0.0.256", "1.2.3", "bad name!", "localhost", "doesnotexist.invalid", "::1", "fe80::1"])]
        check_targets(hosts)
        self.assertEqual(hosts[0].ip, "10.0.0.1")
        self.assertEqual([h.invalid for h in hosts[1:4]],
                         ["not a valid IPv4 address", "not a valid IPv4 address", "not an IP address or hostname"])
        self.assertEqual(hosts[4].ip, "127.0.0.1")
        self.assertTrue(hosts[5].invalid)
        self.assertEqual({hosts[6].invalid, hosts[7].invalid}, {"IPv6 is not supported (IPv4 only)"})
        self.assertTrue(all(h.state == INVALID for h in hosts if h.invalid))


class TestSmallParts(unittest.TestCase):
    def test_durations(self):
        self.assertEqual([duration(x) for x in (0, 59.9, 61, 3600, 5700)], ["0:00", "0:59", "1:01", "1:00:00", "1:35:00"])
        self.assertEqual([short_duration(x) for x in (7, 252, 3599, 5700, 359999, 400000)],
                         ["0:07", "4:12", "59:59", "1h35", "99h59", "4d"])
        self.assertTrue(all(len(short_duration(x)) <= 5 for x in range(0, 10 ** 7, 997)))

    def test_latency_characters(self):
        self.assertEqual("".join(CHARSETS["unicode"].latency_char(x) for x in (0.5, 1, 4.9, 9.9, 29.9, 99, 5000)),
                         "▁▂▃▄▅▆▇")
        self.assertEqual("".join(CHARSETS["safe"].latency_char(x) for x in (0.5, 5, 50, 500)), "░▒▓█")

    def test_keys(self):
        self.assertEqual(split_keys("\x1b[A\x1b[B\x1bOP"), [])  # arrow and F keys
        self.assertEqual(split_keys("\x1bx"), [])  # Alt+x must not open the x dialog
        self.assertEqual(split_keys("\x1b"), ["esc"])
        self.assertEqual(split_keys("R1\x1b"), ["r", "1", "esc"])
        self.assertTrue(UNFINISHED_SEQUENCE.search(b"\x1b["))


class TestStateMachine(WorkdirTest):
    def test_states(self):
        run = Run(self, ["10.0.0.1"])
        host = run.hosts[0]
        for _ in range(4):
            run.round(down=[host])
        self.assertEqual(host.state, SILENT)
        run.round()
        self.assertEqual((host.state, host.lost, host.sent), (OK, 0, 1))  # misses before the first reply are no loss
        for misses, state in [([1], OK), ([0, 1], WARN), ([0, 1], LOSS), ([1, 1], DOWN)]:
            for miss in misses:
                run.round(down=[host] if miss else [])
            self.assertEqual(host.state, state)
        run.round()
        # the outage (5 misses in a row, with the one before it) leaves the window
        self.assertEqual((host.state, host.recent_lost), (WARN, 2))

    def test_reset_keeps_down_hosts_and_drops_undo(self):
        run = Run(self, ["10.0.0.1", "10.0.0.2"])
        run.round()
        for _ in range(3):
            run.round(down=["10.0.0.1"])
        run.keys("x", "1", "1")
        self.assertIsNotNone(run.session.last_ignore)
        run.round()
        for _ in range(3):
            run.round(down=["10.0.0.2"])
        run.keys("r", "r")
        self.assertIsNone(run.session.last_ignore)
        self.assertEqual(run.hosts[1].state, DOWN)  # still down after the reset
        run.keys("x")
        self.assertEqual(run.controls.question.kind, "pick group")
        self.assertNotIn("undo", run.view.notice)

    def test_ignore_undo_and_coming_back(self):
        run = Run(self, ["10.0.1.1", "10.0.1.2", "10.0.1.3", "10.0.2.1"], groups=["G1", "G1", "G1", "G2"])
        h1, h2, _, _ = run.hosts
        run.round()
        for _ in range(3):
            run.round(down=[h1, h2])
        run.keys("x")
        self.assertEqual(run.view.group_numbers, {"G1": "1"})
        run.keys("5")  # not a group number: nothing happens
        self.assertEqual(run.controls.question.kind, "pick group")
        run.keys("1")
        self.assertEqual((run.controls.question.kind, len(run.controls.question.hosts)), ("confirm group", 2))
        run.keys("1")
        self.assertEqual((h1.state, h2.state, back_counts(run.hosts)), (IGNORED, IGNORED, (2, 2)))
        run.session.baseline.save(run.hosts)
        with open(run.session.baseline.path) as fh:
            self.assertEqual(set(json.load(fh)["seen"]), {"10.0.1.3", "10.0.2.1"})
        for _ in range(3):
            run.round(down=[h2])
        self.assertEqual(h1.state, OK)
        self.assertIn("BACK after being ignored", run.log_file())
        run.keys("x", "u")
        self.assertEqual((h2.state, h2.ignore_history, len(h1.ignore_history)), (DOWN, [], 1))

    def test_questions(self):
        run = Run(self, ["10.0.0.1"])
        run.keys("r", "esc")
        self.assertEqual((run.controls.question, run.view.notice), (None, "cancelled"))
        run.keys("r", "x")
        self.assertEqual(run.controls.question.kind, "reset")
        run.controls.question.until = 0
        run.controls.close_expired_question()
        self.assertIn("cancelled (no key", run.view.notice)
        self.assertTrue(run.controls.press("q"))

    def test_baseline(self):
        run = Run(self, ["10.0.1.1", "10.0.1.3"])
        run.round(down=["10.0.1.1"])
        run = Run(self, ["10.0.1.1", "10.0.1.3"], fresh=False)
        self.assertEqual([h.answered_once for h in run.hosts], [False, True])
        for _ in range(3):
            run.round(down=["10.0.1.3"])
        self.assertEqual(run.hosts[1].state, DOWN)  # answered in the earlier run: alarms
        for broken in ("[]", '"x"', '{"saved": 5}', '{"seen": 5, "saved": "2026-01-01T00:00:00"}', "not json"):
            self.write("t-baseline.json", broken)
            run = Run(self, ["10.0.1.1"], fresh=False)  # must not raise
            self.assertIn("unreadable", run.log_file())


class TestEvents(WorkdirTest):
    def test_grouped_events(self):
        run = Run(self, [f"10.0.1.{i}" for i in range(1, 6)] + ["10.0.2.1"], groups=["C"] * 5 + ["S"])
        run.round()
        t0 = time.time()
        for i in range(3):
            run.round(down=run.hosts[:4] + [run.hosts[5]], now=t0 + i)
        self.assertIn("[C] 4 hosts DOWN", run.screen_events())
        self.assertTrue(any("lbl5 (10.0.2.1) DOWN" in e for e in run.screen_events()))  # one host: its own line
        self.assertEqual(run.log_file().count(" DOWN  (3 missed"), 5)  # the file still has every host
        run.round(down=run.hosts[2:4], now=t0 + 30)
        self.assertTrue(any(e.startswith("[C] 2 hosts UP (longest outage") and "2 still DOWN" in e
                            for e in run.screen_events()), run.screen_events())
        run.round(now=t0 + 60)
        self.assertTrue(any(e.startswith("[C] all 4 back after") for e in run.screen_events()), run.screen_events())

    def test_all_down(self):
        run = Run(self, [f"10.0.1.{i}" for i in range(1, 7)], groups=["A", "A", "A", "B", "B", "B"])
        run.round()
        for _ in range(3):
            run.round(down=run.hosts)
        self.assertTrue(run.session.is_all_down)
        self.assertIn("ALL DOWN - check pingT's own network", run.render(width=200)[0])
        self.assertTrue(any("ALL 6 watched hosts are DOWN" in e for e in run.screen_events()))
        run.round(down=run.hosts[1:])
        self.assertFalse(run.session.is_all_down)
        self.assertIn("ALL DOWN over after", run.log_file())
        self.assertIn("ALL watched hosts were DOWN at once (1x)", run.summary())

    def test_all_down_needs_five_hosts(self):
        run = Run(self, [f"10.0.1.{i}" for i in range(1, 5)])
        run.round()
        for _ in range(3):
            run.round(down=run.hosts)
        self.assertFalse(run.session.is_all_down)


class TestScreen(WorkdirTest):
    def test_down_for(self):
        run = Run(self, ["10.0.0.1", "10.0.0.2"])
        run.round()
        t0 = time.time() - 252
        for i in range(3):
            run.round(down=["10.0.0.1"], now=t0 + i)
        self.assertRegex("\n".join(run.render()), r"✖ lbl0 10\.0\.0\.1 +down 4:1\d")

    def test_name_modes(self):
        run = Run(self, ["10.0.0.1", "localhost"], dns=True)
        run.hosts[0].dns_name = "sw1.corp"
        self.assertEqual([display_name(run.hosts[0], mode) for mode in NAME_MODES], ["lbl0", "sw1.corp", "10.0.0.1"])
        self.assertEqual(display_name(run.hosts[1], "ip", with_ip=True), "127.0.0.1")  # a DNS name entry: its IP
        for expected in ("dns", "ip", "label"):
            run.keys("n")
            self.assertEqual(run.view.names, expected)
        run.controls.dns_enabled = False
        run.keys("n")
        self.assertEqual(run.view.names, "ip")  # --no-dns: label <-> ip
        run.keys("n")
        self.assertEqual(run.view.names, "label")

    def test_silent_host_shows_no_loss(self):
        run = Run(self, ["10.0.0.1", "10.0.0.2"])
        for _ in range(4):
            run.round(down=["10.0.0.2"])
        run.view.layout = "boxes"
        self.assertNotIn("100%", next(row for row in run.render() if "lbl1" in row))

    def test_every_size_fits(self):
        run = Run(self, [f"10.1.{i // 250}.{i % 250 + 1}" for i in range(600)])
        run.round()
        for _ in range(3):
            run.round(down=run.hosts[::7])
        for width, height in ((20, 10), (40, 15), (80, 24), (250, 60)):
            for layout in ("groups", "boxes"):
                run.view.layout = layout
                rows = run.render(width, height)
                self.assertLessEqual(len(rows), height, (layout, width, height))
                self.assertTrue(all(len(r) <= width for r in rows), (layout, width, height))

    def test_too_many_groups_are_cut(self):
        run = Run(self, [f"10.2.{i // 10}.{i % 10 + 1}" for i in range(300)], groups=[f"grp{i // 10:02d}" for i in range(300)])
        run.round()
        for _ in range(3):
            run.round(down=[run.hosts[295]])
        rows = run.render(80, 24)
        self.assertLessEqual(len(rows), 24)
        more = [r for r in rows if "more groups" in r]
        self.assertEqual(len(more), 1, rows)
        self.assertIn("1 down", more[0])
        self.assertTrue(any("problems (1)" in r for r in rows))
        self.assertTrue(any("lbl295" in r for r in rows))
        self.assertFalse(any("more groups" in r for r in run.render(200, 60)))  # everything fits: no such line

    def test_charsets(self):
        run = Run(self, [f"10.3.0.{i}" for i in range(1, 30)])
        run.round()
        for _ in range(3):
            run.round(down=run.hosts[:3])
        run.hosts[4].label = "a-very-long-label-that-gets-cut-off-somewhere"
        for name in ("safe", "ascii"):
            run.keys("g")
            self.assertEqual(run.view.charset.name, name)
        for layout in ("groups", "boxes"):
            run.view.layout = layout
            text = "\n".join(run.render(60, 20)) + run.summary()
            self.assertTrue(text.isascii(), {c for c in text if not c.isascii()})
        run.keys("g")
        self.assertEqual(run.view.charset.name, "unicode")

    def test_credits_only_when_there_is_room(self):
        run = Run(self, ["10.0.0.1"])
        self.assertIn(f"pingT {__version__} - {CREDITS}", run.render(width=200)[0])
        self.assertNotIn(CREDITS, run.render(width=100)[0])


class TestSummary(WorkdirTest):
    def test_brackets_in_names(self):
        run = Run(self, ["10.0.3.1", "10.0.3.2", "10.0.3.3"], groups=["G", "G", "Clients [old]"])
        run.hosts[0].label = "uplink [/24 core]"
        run.hosts[1].label = "sw [bold]x"
        run.round()
        for _ in range(3):
            run.round(down=run.hosts)
        out = run.summary()
        for text in ("uplink [/24 core]", "sw [bold]x", "Clients [old]"):
            self.assertIn(text, out)
        self.assertIsNone(save_summary(run.session, self.path("s.txt"), run.view.charset, 1.0))
        text = read(self.path("s.txt"))
        self.assertTrue(text.startswith(f"Timmy Pinger {__version__} ({CREDITS}) - summary"))
        self.assertIn("uplink [/24 core]", text)

    def test_group_timeline(self):
        run = Run(self, [f"10.0.1.{i}" for i in range(1, 5)] + ["10.0.2.1", "10.0.2.2"], groups=["C"] * 4 + ["S"] * 2)
        run.round()
        t0 = time.time() - 600
        for i in range(3):
            run.round(down=run.hosts[:4], now=t0 + i)
        run.round(now=t0 + 449)  # all back 7:29 after the first lost ping
        for i in range(3):
            run.round(down=run.hosts[4:], now=t0 + 500 + i)
        out = run.summary()
        self.assertIn("Group outages (2)", out)
        self.assertRegex(out, r"C +│ +4 │ [\d:]+ │ +7:29 │ all 4 back")
        self.assertRegex(out, r"S +│ +2 │ - +│ .* still 2 DOWN")

    def test_reset_keeps_the_record(self):
        run = Run(self, ["10.0.0.1", "10.0.0.2"])
        run.round()
        for _ in range(3):
            run.round(down=["10.0.0.1"])
        run.round()  # back: a finished outage
        run.keys("r", "r")
        run.round()
        out = run.summary()
        self.assertIn("stats since the reset at", out)
        self.assertIn("No packet loss on any responding host since the last reset", out)
        self.assertRegex(out, r"Before the reset at [\d:]+ \(r r\): hosts with loss [\d:]+ to [\d:]+ \(1\)")
        self.assertTrue(re.search(r"lbl0 .* 10\.0\.0\.1 .* OK .* 1 │ +0:0\d", out), out)


if __name__ == "__main__":
    unittest.main()
