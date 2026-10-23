"""pingt.py without a terminal: parsing, the state machine, keys, events, rendering, summary."""
from __future__ import annotations

import io
import json
import time
import unittest
from types import SimpleNamespace

from helpers import WorkdirTest, read

import pingt as P
from rich.console import Console


def make_args(**kw):
    args = dict(window=20, warn=2, loss=3, down=3, events=8, interval=1.0, timeout=800, fresh=True, dns=False,
                charset="unicode", log="t.log")
    args.update(kw)
    return SimpleNamespace(**args)


class MonitorTest(WorkdirTest):
    def monitor(self, targets, groups=None, **kw) -> P.Monitor:
        hosts = [P.Host(t, f"lbl{i}", groups[i] if groups else "G", i) for i, t in enumerate(targets)]
        P.validate_targets(hosts)
        kw.setdefault("log", self.path("t.log"))
        m = P.Monitor(hosts, None, make_args(**kw), self.path("t-baseline.json"))
        self.addCleanup(m.logfile.close)
        return m

    @staticmethod
    def round(m: P.Monitor, up=(), down=(), now=None):
        """One ping round like run_pings: every host gets a reply unless it is in down."""
        now = now or time.time()
        m._in_round = True
        for h in m.hosts:
            if h.addr:
                m._record(h, None if h in down or h.target in down else 1.0, now)
        m._in_round = False
        m._flush_events(now)

    def screen_events(self, m) -> list[str]:
        return [e.plain[9:] for e in m.events]  # without the time

    def render(self, m, width=100, height=30) -> list[str]:
        buf = io.StringIO()
        Console(file=buf, width=width, height=height, color_system=None).print(m.render(width, height))
        return buf.getvalue().splitlines()

    def summary(self, m) -> str:
        buf = io.StringIO()
        P.print_summary(Console(file=buf, width=140, color_system=None), m)
        return buf.getvalue()


class TestParsing(WorkdirTest):
    def test_hosts_file_formats(self):
        path = self.write("h.txt", "﻿# comment\n[Servers VLAN10]\n10.0.10.1    core-sw01   # note\n10.0.10.11\n"
                                   "switch01.corp.local   sw01\n10.0.20.7,printer-2f,Office\n10.0.20.8, p8 ,\n[]\n"
                                   "10.0.50.7\n10.0.50.8 Room #12\n10.0.50.9 host #note\n[ Spaced ]\n10.0.60.1\n")
        e = P.parse_hosts_file(path)
        self.assertEqual(e[0], ("10.0.10.1", "core-sw01", "Servers VLAN10", "h.txt:3"))
        self.assertEqual(e[1][:3], ("10.0.10.11", "10.0.10.11", "Servers VLAN10"))
        self.assertEqual(e[3][:3], ("10.0.20.7", "printer-2f", "Office"))
        self.assertEqual(e[4][:3], ("10.0.20.8", "p8", "Servers VLAN10"))  # empty csv group -> section
        self.assertIsNone(e[5][2])  # [] ends the group
        self.assertEqual(e[6][1], "Room #12")
        self.assertEqual(e[7][1], "host #note")  # "#" must be followed by a space to start a comment
        self.assertEqual(e[8][2], "Spaced")

    def test_auto_group_ipv4_only(self):
        self.assertEqual(P.auto_group("10.0.50.7"), "10.0.50.0/24")
        self.assertEqual(P.auto_group("sw.corp"), "other")
        self.assertFalse(P.is_ip("::1"))

    def test_validate_targets(self):
        hs = [P.Host(t, t, "g", i) for i, t in enumerate(
            ["10.0.0.1", "10.0.0.256", "1.2.3", "bad name!", "localhost", "doesnotexist.invalid", "::1", "fe80::1"])]
        P.validate_targets(hs)
        self.assertEqual(hs[0].addr, "10.0.0.1")
        self.assertEqual(hs[1].invalid, "not a valid IPv4 address")
        self.assertEqual(hs[2].invalid, "not a valid IPv4 address")
        self.assertEqual(hs[3].invalid, "not an IP address or hostname")
        self.assertEqual(hs[4].addr, "127.0.0.1")
        self.assertTrue(hs[5].invalid)
        self.assertEqual(hs[6].invalid, "IPv6 is not supported (IPv4 only)")
        self.assertEqual(hs[7].invalid, "IPv6 is not supported (IPv4 only)")
        self.assertTrue(all(h.state == P.INVALID for h in hs if h.invalid))

    def test_durations(self):
        self.assertEqual([P.fmt_dur(x) for x in (0, 59.9, 61, 3600, 5700)], ["0:00", "0:59", "1:01", "1:00:00", "1:35:00"])
        self.assertEqual([P.fmt_short(x) for x in (7, 252, 3599, 5700, 359999, 400000)],
                         ["0:07", "4:12", "59:59", "1h35", "99h59", "4d"])
        self.assertTrue(all(len(P.fmt_short(x)) <= 5 for x in range(0, 10 ** 7, 997)))

    def test_spark(self):
        cs = P.CHARSETS["unicode"]
        self.assertEqual("".join(P.spark(x, cs)[0] for x in (0.5, 1, 4.9, 9.9, 29.9, 99, 5000)), "▁▂▃▄▅▆▇")
        self.assertEqual((P.spark(29.9, cs)[1], P.spark(30, cs)[1]), ("green", "yellow"))
        self.assertEqual("".join(P.spark(x, P.CHARSETS["safe"])[0] for x in (0.5, 5, 50, 500)), "░▒▓█")

    def test_keys(self):
        def keys(s):
            return ["esc" if k == "\x1b" else k.lower() for k in P.KEY_SEQ.findall(s)
                    if k == "\x1b" or not k.startswith("\x1b")]
        self.assertEqual(keys("\x1b[A\x1b[B\x1bOP"), [])  # arrow and F keys
        self.assertEqual(keys("\x1bx"), [])  # Alt+x must not open the x dialog
        self.assertEqual(keys("\x1b"), ["esc"])
        self.assertEqual(keys("R1\x1b"), ["r", "1", "esc"])
        self.assertTrue(P.KEY_SEQ_OPEN.search(b"\x1b["))


class TestStates(MonitorTest):
    def test_state_machine(self):
        m = self.monitor(["10.0.0.1"])
        a = m.hosts[0]
        for ok in [0, 0, 0, 0]:
            self.round(m, down=[] if ok else [a])
        self.assertEqual(a.state, P.NEVER)
        self.round(m)
        self.assertEqual((a.state, a.lost, a.sent), (P.OK, 0, 1))  # misses before the first reply are not loss
        for downs, state in [([1], P.OK), ([0, 1], P.WARN), ([0, 1], P.LOSS), ([1, 1], P.DOWN)]:
            for d in downs:
                self.round(m, down=[a] if d else [])
            self.assertEqual(a.state, state)
        self.round(m)
        # the outage (here 5 misses in a row, incl. the one before it) is taken out of the window
        self.assertEqual((a.state, a.window_lost), (P.WARN, 2))

    def test_reset_keeps_down_and_drops_undo(self):
        m = self.monitor(["10.0.0.1", "10.0.0.2"])
        self.round(m)
        for _ in range(3):
            self.round(m, down=["10.0.0.1"])
        m.key("x"), m.key("1"), m.key("1")
        self.assertIsNotNone(m.last_ignore)
        m.key("r"), m.key("r")
        self.assertIsNone(m.last_ignore)
        m.key("x")
        self.assertIn("no DOWN hosts to ignore", m.notice[0])

    def test_ignore_undo_back(self):
        m = self.monitor(["10.0.1.1", "10.0.1.2", "10.0.1.3", "10.0.2.1"], groups=["G1", "G1", "G1", "G2"])
        h1, h2, h3, h4 = m.hosts
        self.round(m)
        for _ in range(3):
            self.round(m, down=[h1, h2])
        m.key("x")
        self.assertEqual((m.ask, m.ignore_keys), ("pick", {"1": "G1"}))
        m.key("\x1b[A"), m.key("5")
        self.assertEqual(m.ask, "pick")
        m.key("1")
        self.assertEqual((m.ask, len(m.ignore_preview)), ("confirm", 2))
        m.key("1")
        self.assertEqual((h1.state, h2.state, P.back_counts(m.hosts)), (P.IGNORED, P.IGNORED, (2, 2)))
        m._save_baseline()
        with open(m.baseline_path) as fh:
            self.assertEqual(set(json.load(fh)["seen"]), {"10.0.1.3", "10.0.2.1"})
        for _ in range(3):
            self.round(m, down=[h2])
        self.assertEqual(h1.state, P.OK)
        self.assertIn("BACK after being ignored", read_log(m))
        m.key("x"), m.key("u")
        self.assertEqual((h2.state, h2.ignore_log, len(h1.ignore_log)), (P.DOWN, [], 1))

    def test_questions(self):
        m = self.monitor(["10.0.0.1"])
        m.key("r"), m.key("esc")
        self.assertEqual((m.ask, m.notice[0]), ("", "cancelled"))
        m.key("r"), m.key("x")
        self.assertEqual(m.ask, "reset")
        m.ask_until = 0
        m.expire()
        self.assertIn("cancelled (no key", m.notice[0])
        self.assertTrue(m.key("q"))

    def test_baseline_restore_and_corrupt_files(self):
        m = self.monitor(["10.0.1.1", "10.0.1.3"])
        self.round(m, down=["10.0.1.1"])
        m._save_baseline()
        m = self.monitor(["10.0.1.1", "10.0.1.3"], fresh=False)
        self.assertEqual([h.ever_up for h in m.hosts], [False, True])
        for _ in range(3):
            self.round(m, down=["10.0.1.3"])
        self.assertEqual(m.hosts[1].state, P.DOWN)
        for bad in ("[]", '"x"', '{"saved": 5}', '{"seen": 5, "saved": "2026-01-01T00:00:00"}', "not json"):
            with open(self.path("t-baseline.json"), "w") as fh:
                fh.write(bad)
            self.monitor(["10.0.1.1"], fresh=False)  # must not raise
            self.assertIn("unreadable", read_log(m))

    def test_old_baseline_sticky_warning(self):
        with open(self.path("t-baseline.json"), "w") as fh:
            json.dump({"saved": "2026-01-01T00:00:00", "seen": ["10.0.0.1"]}, fh)
        m = self.monitor(["10.0.0.1"], fresh=False)
        self.assertIn("restart with --fresh", self.render(m)[0])
        m.key("v")
        self.assertNotIn("restart with --fresh", self.render(m)[0])


class TestEvents(MonitorTest):
    def test_grouped_events(self):
        m = self.monitor([f"10.0.1.{i}" for i in range(1, 6)] + ["10.0.2.1"], groups=["C"] * 5 + ["S"])
        self.round(m)
        t0 = time.time()
        for i in range(3):
            self.round(m, down=m.hosts[:4] + [m.hosts[5]], now=t0 + i)
        ev = self.screen_events(m)
        self.assertIn("[C] 4 hosts DOWN", ev)
        self.assertTrue(any("lbl5 (10.0.2.1) DOWN" in e for e in ev), ev)  # one host: its own line
        self.assertEqual(read_log(m).count(" DOWN  (3 missed"), 5)  # the log still has every host
        self.round(m, down=m.hosts[2:4], now=t0 + 30)
        self.assertTrue(any(e.startswith("[C] 2 hosts UP (longest outage") and "2 still DOWN" in e
                            for e in self.screen_events(m)), self.screen_events(m))
        self.round(m, now=t0 + 60)
        self.assertTrue(any(e.startswith("[C] all 4 back after") for e in self.screen_events(m)),
                        self.screen_events(m))

    def test_all_down(self):
        m = self.monitor([f"10.0.1.{i}" for i in range(1, 7)], groups=["A", "A", "A", "B", "B", "B"])
        self.round(m)
        for _ in range(3):
            self.round(m, down=m.hosts)
        self.assertTrue(m.all_down())
        self.assertIn("ALL DOWN - check pingT's own network", self.render(m, width=200)[0])
        self.assertTrue(any("ALL 6 watched hosts are DOWN" in e for e in self.screen_events(m)))
        self.round(m, down=m.hosts[1:])
        self.assertFalse(m.all_down())
        self.assertIn("ALL DOWN over after", read_log(m))
        self.assertIn("ALL watched hosts were DOWN at once (1x)", self.summary(m))

    def test_all_down_needs_five_hosts(self):
        m = self.monitor([f"10.0.1.{i}" for i in range(1, 5)])
        self.round(m)
        for _ in range(3):
            self.round(m, down=m.hosts)
        self.assertFalse(m.all_down())


class TestScreen(MonitorTest):
    def test_down_for_in_problems_list(self):
        m = self.monitor(["10.0.0.1", "10.0.0.2"])
        self.round(m)
        t0 = time.time() - 252
        for i in range(3):
            self.round(m, down=["10.0.0.1"], now=t0 + i)
        screen = "\n".join(self.render(m))
        self.assertRegex(screen, r"✖ lbl0 10\.0\.0\.1 +down 4:1\d")

    def test_name_modes(self):
        m = self.monitor(["10.0.0.1", "localhost"])
        m.args.dns = True
        m.hosts[0].fqdn = "sw1.corp"
        self.assertEqual([m._name(m.hosts[0], False, mode) for mode in P.NAME_MODES], ["lbl0", "sw1.corp", "10.0.0.1"])
        self.assertEqual(m._name(m.hosts[1], True, "ip"), "127.0.0.1")  # a hostname entry shows its IP
        for expect in ("dns", "ip", "label"):
            m.key("n")
            self.assertEqual(m.names, expect)
        m.args.dns = False
        m.key("n")
        self.assertEqual(m.names, "ip")  # --no-dns: label <-> ip
        m.key("n")
        self.assertEqual(m.names, "label")

    def test_silent_host_shows_no_percent(self):
        m = self.monitor(["10.0.0.1", "10.0.0.2"])
        for _ in range(4):
            self.round(m, down=["10.0.0.2"])
        m.view = "detail"
        line = next(row for row in self.render(m) if "lbl1" in row)
        self.assertNotIn("100%", line)

    def test_render_sizes_fit(self):
        m = self.monitor([f"10.1.{i // 250}.{i % 250 + 1}" for i in range(600)])
        self.round(m)
        for _ in range(3):
            self.round(m, down=m.hosts[::7])
        for w, h in ((20, 10), (40, 15), (80, 24), (250, 60)):
            for view in ("compact", "detail"):
                m.view = view
                rows = self.render(m, w, h)
                self.assertLessEqual(len(rows), h, (view, w, h))
                self.assertTrue(all(len(r) <= w for r in rows), (view, w, h))

    def test_many_groups_are_cut(self):
        m = self.monitor([f"10.2.{i // 10}.{i % 10 + 1}" for i in range(300)], groups=[f"grp{i // 10:02d}" for i in range(300)])
        self.round(m)
        for _ in range(3):
            self.round(m, down=[m.hosts[295]])
        rows = self.render(m, 80, 24)
        self.assertLessEqual(len(rows), 24)
        more = [r for r in rows if "more groups" in r]
        self.assertEqual(len(more), 1, rows)
        self.assertIn("1 down", more[0])
        self.assertTrue(any("problems (1)" in r for r in rows), rows)
        self.assertTrue(any("lbl295" in r for r in rows), rows)
        self.assertFalse(any("more groups" in r for r in self.render(m, 200, 60)))  # fits: no line

    def test_charsets(self):
        m = self.monitor([f"10.3.0.{i}" for i in range(1, 30)])
        self.round(m)
        for _ in range(3):
            self.round(m, down=m.hosts[:3])
        m.hosts[4].label = "a-very-long-label-that-gets-cut-off-somewhere"
        for name in ("safe", "ascii"):
            m.key("g")
            self.assertEqual(m.cs.name, name)
        for view in ("compact", "detail"):
            m.view = view
            text = "\n".join(self.render(m, 60, 20)) + self.summary(m)
            self.assertTrue(text.isascii(), {ch for ch in text if not ch.isascii()})
        m.key("g")
        self.assertEqual(m.cs.name, "unicode")


class TestSummary(MonitorTest):
    def test_brackets_in_names(self):
        m = self.monitor(["10.0.3.1", "10.0.3.2", "10.0.3.3"], groups=["G", "G", "Clients [old]"])
        m.hosts[0].label = "uplink [/24 core]"
        m.hosts[1].label = "sw [bold]x"
        self.round(m)
        for _ in range(3):
            self.round(m, down=m.hosts)
        out = self.summary(m)
        for text in ("uplink [/24 core]", "sw [bold]x", "Clients [old]"):
            self.assertIn(text, out)
        self.assertIsNone(P.save_summary(m, self.path("s.txt")))
        self.assertIn("uplink [/24 core]", read(self.path("s.txt")))


def read_log(m) -> str:
    m.logfile.flush()
    with open(m.args.log, encoding="utf-8") as fh:
        return fh.read()


if __name__ == "__main__":
    unittest.main()
