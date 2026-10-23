"""pingT end to end: started through pingT.py in a pseudo terminal, with the fake fping."""
from __future__ import annotations

import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest

from helpers import REPO, Term, WorkdirTest, fake_env, read, set_state, wait_for

PINGT = os.path.join(REPO, "pingT.py")
HOSTS = ("[Servers]\n10.9.0.1 srv1\n10.9.0.2 srv2\nlocalhost lo-by-name\n"
         "[Clients 2F]\n" + "".join(f"10.9.1.{i} pc{i}\n" for i in range(1, 7)) +
         "[Printers]\n10.9.2.1 prn1\n10.9.2.300 typo\n10.9.0.1 duplicate\n")  # 10 valid, 1 invalid, 1 duplicate


class TerminalTest(WorkdirTest):
    def start(self, *args, rows=30, cols=120, **env) -> Term:
        if "--log" not in args:  # never into the repo's logs/ folder
            args = (*args, "--log", self.path("pingT-events.log"))
        t = Term([sys.executable, PINGT, *args], self.dir, fake_env(self.dir, **env), rows, cols)
        self.addCleanup(t.close)
        return t

    def log(self, name="pingT-events.log") -> str:
        return read(self.path(name))

    def quit(self, t: Term) -> str:
        m = t.mark()
        t.send("q", 1)
        self.assertEqual(t.wait_exit(), 0)
        return t.text(m)


class TestSession(TerminalTest):
    def test_migration_session(self):
        self.write("hosts.txt", HOSTS)
        t = self.start("-f", "hosts.txt", "--fresh")
        self.assertTrue(wait_for(lambda: "back 10/10" in t.frame(), 12), t.frame()[-800:])
        self.assertIn("WARNING hosts.txt:14: '10.9.2.300' not a valid IPv4 address", t.text())
        self.assertIn("invalid 1", t.frame())

        # a cut-over: 4 clients go down at once -> one grouped event line on screen
        set_state(self.dir, **{f"10.9.1.{i}": "down" for i in range(1, 5)})
        self.assertTrue(wait_for(lambda: "[Clients 2F] 4 hosts DOWN" in t.frame(), 8), t.frame()[-1500:])
        self.assertEqual(self.log().count("DOWN  (3 missed in a row)"), 4)
        self.assertTrue(wait_for(lambda: "back 6/10" in t.frame() and "down 4" in t.frame()))
        self.assertRegex(t.frame(), r"pc1 10\.9\.1\.1 +down 0:0\d")

        # x: pick the group, confirm
        m = t.mark()
        t.send("x")
        self.assertIn("1 Clients 2F", t.text(m))
        t.send("\x1b[A")  # an arrow key does nothing
        t.send("1")
        self.assertIn('ignore 4 DOWN hosts in "Clients 2F"', t.text(m))
        t.send("1")
        self.assertIn("IGNORED 4 DOWN hosts in [Clients 2F]", self.log())
        self.assertTrue(wait_for(lambda: "ignored 4" in t.frame() and "back 6/6" in t.frame()), t.frame()[-600:])

        # an ignored host answering 3 times is watched again; undo brings back the other 3
        set_state(self.dir, **{f"10.9.1.{i}": "down" for i in range(2, 5)})
        self.assertTrue(wait_for(lambda: "pc1 (10.9.1.1) BACK after being ignored" in self.log(), 8))
        t.send("x")
        t.send("u")
        self.assertIn("UNDO ignore: 3 hosts in [Clients 2F]", self.log())
        self.assertTrue(wait_for(lambda: "down 3" in t.frame()))

        # questions: Esc, timeout, r r
        m = t.mark()
        t.send("r")
        t.send("\x1b")
        self.assertIn("cancelled", t.text(m))
        m = t.mark()
        t.send("r", 6)
        self.assertIn("cancelled (no key for 5s)", t.text(m))
        t.send("r", 0.3)
        t.send("r")
        self.assertTrue(wait_for(lambda: "stats reset" in self.log()))
        self.assertTrue(wait_for(lambda: "down 3" in t.frame()))

        # views, names, characters
        t.send("v", 1.5)
        self.assertNotIn("problems (", t.frame())
        t.send("v")
        for mode in ("dns", "ip", "label"):
            t.send("n", 1.2)
            self.assertIn(f"names:{mode}", t.frame())
        t.send("g", 1.2)
        self.assertIn("characters: safe", t.frame())
        t.send("g", 1.2)
        self.assertTrue(wait_for(lambda: "- problems (" in t.frame()), t.frame()[-800:])  # ascii separator
        t.send("g")

        # pinging fails completely: PING ERROR, hosts are not marked down
        set_state(self.dir, _exit=4)
        self.assertTrue(wait_for(lambda: "PING ERROR" in t.frame(), 6))
        self.assertEqual(self.log().count("PING ERROR"), 1)
        set_state(self.dir)
        self.assertTrue(wait_for(lambda: "pinging works again" in self.log(), 6))
        self.assertTrue(wait_for(lambda: "back 10/10" in t.frame(), 8))
        self.assertTrue(wait_for(lambda: "[Clients 2F] all 3 back after" in self.log(), 4), self.log()[-800:])

        # quit: summary on screen and in a file, baseline written
        set_state(self.dir, **{"10.9.1.6": "down"})
        wait_for(lambda: "pc6 (10.9.1.6) DOWN" in self.log(), 8)
        out = self.quit(t)
        self.assertIn("NOT BACK (1)", out)
        self.assertIn("INVALID entries, not pinged (1)", out)
        self.assertIn("IGNORED by operator", out)
        self.assertEqual(len(glob.glob(self.path("pingT-events-summary-*.txt"))), 1)
        with open(self.path("pingT-events-baseline.json")) as fh:
            self.assertEqual(len(json.load(fh)["seen"]), 10)

        # restart without --fresh: a host that was online alarms; SSH drop (SIGHUP) still saves the summary
        set_state(self.dir, **{"10.9.0.2": "down"})
        t = self.start("-f", "hosts.txt")
        self.assertTrue(wait_for(lambda: "baseline loaded: 10 hosts" in self.log(), 8))
        self.assertTrue(wait_for(lambda: "srv2 (10.9.0.2) DOWN" in self.log().split("baseline loaded")[-1], 10))
        os.close(t.fd)
        self.assertIsNotNone(t.wait_exit())
        self.assertEqual(len(glob.glob(self.path("pingT-events-summary-*.txt"))), 2)


class TestEdges(TerminalTest):
    def test_old_baseline_sigterm_custom_log(self):
        with open(self.path("mig-baseline.json"), "w") as fh:
            json.dump({"saved": "2026-01-01T00:00:00", "seen": ["10.9.0.1"]}, fh)
        t = self.start("10.9.0.1", "--log", self.path("mig.log"))
        self.assertTrue(wait_for(lambda: "restart with --fresh (any key hides this)" in t.frame(), 8), t.frame())
        time.sleep(1.5)
        self.assertIn("restart with --fresh", t.frame())  # stays
        t.send("v", 1.5)
        self.assertNotIn("restart with --fresh", t.frame())
        os.kill(t.pid, signal.SIGTERM)
        t.wait_exit()
        self.assertEqual(len(glob.glob(self.path("mig-summary-*.txt"))), 1)

    def test_brackets_in_labels(self):
        self.write("h.txt", "10.9.5.1 uplink [/24 core]\n10.9.5.2 [old] sw2\n")
        set_state(self.dir, **{"10.9.5.1": "down"})
        t = self.start("-f", "h.txt", "--fresh")
        time.sleep(3)
        out = self.quit(t)
        self.assertIn("uplink [/24 core]", out)
        summary = glob.glob(self.path("pingT-events-summary-*.txt"))
        self.assertEqual(len(summary), 1)
        self.assertIn("uplink [/24 core]", read(summary[0]))

    def test_chunk_failure_and_all_down(self):
        self.write("h.txt", "".join(f"10.8.{i // 200}.{i % 200 + 1}\n" for i in range(300)))
        set_state(self.dir, _fail_chunk_with=["10.8.0.70"])
        t = self.start("-f", "h.txt", "--fresh", "--no-dns", rows=40, cols=160)
        self.assertTrue(wait_for(lambda: "64 of 300 hosts not pinged this round" in self.log(), 8))
        self.assertTrue(wait_for(lambda: "back 236/236" in t.frame(), 6), t.frame()[-600:])
        set_state(self.dir, _default="down", _fail_chunk_with=["10.8.0.70"])
        self.assertTrue(wait_for(lambda: "ALL DOWN - check pingT's own network" in t.frame(), 8), t.frame()[-800:])
        set_state(self.dir, _fail_chunk_with=["10.8.0.70"])
        self.assertTrue(wait_for(lambda: "ALL DOWN over after" in self.log(), 8))
        self.quit(t)

    def test_charset_option_and_env(self):
        t = self.start("10.9.0.1", "10.9.0.2", "--charset", "ascii")
        self.assertTrue(wait_for(lambda: "back 2/2" in t.frame(), 8))
        self.assertTrue(t.frame().isascii())
        out = self.quit(t)
        self.assertTrue(out.isascii(), {c for c in out if not c.isascii()})
        t = self.start("10.9.0.1", PINGT_CHARSET="safe")
        self.assertTrue(wait_for(lambda: "back 1/1" in t.frame(), 8))
        self.assertIn("└", self.quit(t))  # thin (safe) table lines
        t = self.start("10.9.0.1", PINGT_CHARSET="fancy")
        t.wait_exit()
        self.assertIn("PINGT_CHARSET='fancy' is unknown", t.text())

    def test_default_log_folder(self):
        """Without --log, everything goes into logs/ next to pingT.py - checked on a copy of the tool."""
        tool = self.path("tool")
        os.makedirs(tool)
        shutil.copy(PINGT, tool)
        shutil.copytree(os.path.join(REPO, "timmy_pinger"), os.path.join(tool, "timmy_pinger"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        t = Term([sys.executable, os.path.join(tool, "pingT.py"), "10.9.0.1"], self.dir, fake_env(self.dir))
        self.addCleanup(t.close)
        self.assertTrue(wait_for(lambda: "back 1/1" in t.frame(), 8))
        self.quit(t)
        logs = sorted(os.listdir(os.path.join(tool, "logs")))
        self.assertEqual([name.split("-2")[0] for name in logs],
                         ["pingT-events-baseline.json", "pingT-events-summary", "pingT-events.log"])

    def test_removed_option(self):
        r = subprocess.run([sys.executable, PINGT, "--charset-test"], capture_output=True, text=True)
        self.assertIn("unrecognized arguments: --charset-test", r.stderr)


if __name__ == "__main__":
    unittest.main()
