"""scanT with the fake fping: new scans, --rescan (only adds, diff), limits and errors."""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest

from helpers import REPO, Term, WorkdirTest, fake_env, read, set_state

from timmy_pinger import __version__
from timmy_pinger.scanner import parse_subnet

SCAN = os.path.join(REPO, "scanT.py")


class ScanTest(WorkdirTest):
    def scan(self, *args, env=None):
        r = subprocess.run([sys.executable, SCAN, *args], cwd=self.dir, capture_output=True, text=True,
                           env=fake_env(self.dir, COLUMNS="160", **(env or {})), timeout=60)
        return r.returncode, r.stdout + r.stderr

    def alive(self, *ips):
        set_state(self.dir, _default="down", **{ip: "up" for ip in ips})


class TestParsing(unittest.TestCase):
    def test_subnet_spec(self):
        self.assertEqual(parse_subnet("10.0.0.0/24=Lab"), ("10.0.0.0/24", "Lab"))
        self.assertEqual(parse_subnet("10.0.0.0/24   Clients 2nd floor"), ("10.0.0.0/24", "Clients 2nd floor"))
        self.assertEqual(parse_subnet("10.0.0.0/24  Lab=Test"), ("10.0.0.0/24", "Lab=Test"))
        self.assertEqual(parse_subnet("10.0.0.0/24"), ("10.0.0.0/24", None))


class TestScan(ScanTest):
    def test_new_scan(self):
        self.write("subnets.txt", "# lan\n10.5.0.0/29   Servers  # comment\n10.5.1.0/30=Printers\n10.5.2.0/30\n")
        self.alive("10.5.0.1", "10.5.0.3", "10.5.1.2")
        code, out = self.scan("-f", "subnets.txt", "-o", "hosts.txt")
        self.assertEqual(code, 0, out)
        self.assertIn("3 hosts in", out)
        text = read(self.path("hosts.txt"))
        self.assertIn("# 10.5.0.0/29: 2 of 6 addresses answered\n[Servers]\n10.5.0.1\n10.5.0.3\n", text)
        self.assertIn("# 10.5.2.0/30: 0 of 2 addresses answered\n[10.5.2.0/30]\n", text)

    def test_existing_file_is_not_overwritten(self):
        self.write("hosts.txt", "10.5.0.1 my-label\n")
        code, out = self.scan("10.5.0.0/30", "-o", "hosts.txt")
        self.assertEqual(code, 2)
        self.assertIn("use --rescan hosts.txt", out)
        code, out = self.scan("10.5.0.0/30", "-o", "hosts.txt", "--overwrite")
        self.assertEqual(code, 0, out)
        self.assertEqual(read(self.path("hosts.txt.bak")), "10.5.0.1 my-label\n")

    def test_rescan_only_adds(self):
        self.alive("10.5.0.1", "10.5.0.2", "10.5.0.3")
        self.scan("10.5.0.0/29=Servers", "10.5.1.0/30=Printers", "-o", "hosts.txt")
        # the engineer edits the list: labels, a host commented out, a comment, a hand-added host
        text = read(self.path("hosts.txt"))
        text = (text.replace("10.5.0.1\n", "10.5.0.1    core-sw   # keep this comment\n")
                    .replace("10.5.0.2\n", "# 10.5.0.2  old-pc taken out on purpose\n")
                    .replace("10.5.0.3\n", "10.5.0.3,esx-01\n"))
        self.write("hosts.txt", text)
        self.alive("10.5.0.1", "10.5.0.2", "10.5.0.5", "10.5.1.1")  # .3 is off now, .5 and printer .1 are new
        code, out = self.scan("--rescan", "hosts.txt")
        self.assertEqual(code, 0, out)
        new = read(self.path("hosts.txt"))
        head = text[: text.index("10.5.0.3,esx-01\n") + len("10.5.0.3,esx-01\n")]
        self.assertTrue(new.startswith(head), new)  # everything before the insert point is unchanged
        self.assertIn("10.5.0.1    core-sw   # keep this comment\n", new)
        self.assertIn("10.5.0.3,esx-01\n10.5.0.5         # new in rescan", new)  # end of its group
        self.assertIn("[Printers]\n10.5.1.1         # new in rescan", new)
        self.assertEqual(new.count("10.5.0.2"), 1)  # commented out: not added again
        self.assertEqual(read(self.path("hosts.txt.bak")), text)
        self.assertIn("2 new, 1 known answered, 1 known without reply (kept)", out)
        self.assertIn("+ 10.5.0.5", out)
        self.assertIn("- 10.5.0.3", out)
        self.assertIn("commented out in hosts.txt but answering, not added: 10.5.0.2", out)

        os.remove(self.path("hosts.txt.bak"))
        code, out = self.scan("--rescan", "hosts.txt")  # nothing new: the file is not rewritten
        self.assertIn("no new hosts - hosts.txt not changed", out)
        self.assertFalse(os.path.exists(self.path("hosts.txt.bak")))

    def test_rescan_hand_written_list(self):
        self.write("hosts.txt", "[Lab]\n10.5.3.1 lab-sw\n")
        code, out = self.scan("--rescan", "hosts.txt")
        self.assertEqual(code, 2)
        self.assertIn("has no scanned subnets", out)
        self.alive("10.5.3.1", "10.5.3.2")
        code, out = self.scan("--rescan", "hosts.txt", "10.5.3.0/30=Lab")
        self.assertEqual(code, 0, out)
        new = read(self.path("hosts.txt"))
        self.assertTrue(new.startswith("# 10.5.3.0/30: subnet for scanT --rescan"), new)  # remembered
        self.assertIn("[Lab]\n10.5.3.1 lab-sw\n10.5.3.2", new)
        code, out = self.scan("--rescan", "hosts.txt")  # now without the range on the command line
        self.assertEqual(code, 0, out)
        self.assertIn("no new hosts", out)

    def test_rescan_to_other_file(self):
        self.alive("10.5.0.1")
        self.scan("10.5.0.0/30=A", "-o", "hosts.txt")
        self.alive("10.5.0.1", "10.5.0.2")
        code, out = self.scan("--rescan", "hosts.txt", "-o", "hosts-new.txt")
        self.assertEqual(code, 0, out)
        self.assertNotIn("10.5.0.2", read(self.path("hosts.txt")))
        self.assertIn("10.5.0.2", read(self.path("hosts-new.txt")))


class TestErrors(ScanTest):
    def test_argument_errors(self):
        for args, msg in [
            ([], "no subnets given"),
            (["10.0.0.0/15"], "more than a /16"),
            (["10.0.0.0/16", "10.1.0.0/16", "10.2.0.0/16", "10.3.0.0/16", "10.4.0.0/16"], "more than 262,144"),
            (["banana/24"], "invalid subnet"),
            (["::1/128"], "IPv6 is not supported"),
            (["10.0.0.1", "-t", "10"], "--timeout must be"),
            (["10.0.0.1", "--rate", "5"], "--rate must be"),
            (["10.0.0.1", "-f", "nope.txt"], "cannot read subnet list"),
            (["--rescan", "nope.txt"], "cannot read --rescan host list"),
            (["10.0.0.1", "-o", "/nonexistent/x.txt"], "folder /nonexistent does not exist"),
            (["10.0.0.1", "--then-ping"], "unrecognized arguments: --then-ping"),
            (["10.0.0.1", "--merge", "x"], "unrecognized arguments"),
        ]:
            code, out = self.scan(*args)
            self.assertEqual(code, 2, (args, out))
            self.assertIn(msg, out, args)

    def test_overlap_and_duplicates(self):
        code, out = self.scan("10.5.0.0/30=A", "10.5.0.0/30=B", "10.5.0.0/29=C", "-o", "o.txt")
        self.assertIn("10.5.0.0/30 listed twice", out)
        self.assertIn("2 addresses stay in the earlier group", out)

    def test_fping_keeps_failing(self):
        self.write("hosts.txt", "10.6.0.1 keep-me\n")
        set_state(self.dir, _exit=4)
        code, out = self.scan("10.6.0.0/22", "-o", "new.txt")
        self.assertEqual(code, 1, out)
        self.assertIn("fping keeps failing", out)
        self.assertTrue(os.path.exists(self.path("new.txt.partial")))
        self.assertFalse(os.path.exists(self.path("new.txt")))

    def test_ctrl_c_writes_partial(self):
        self.write("hosts.txt", "10.6.0.1 keep-me\n")
        t = Term([sys.executable, SCAN, "--rescan", "hosts.txt", "10.6.0.0/22"], self.dir,
                 fake_env(self.dir, FAKE_SLEEP="3"))
        self.addCleanup(t.close)
        time.sleep(2)
        t.send("\x03", 0)
        self.assertEqual(t.wait_exit(), 130)
        self.assertIn("interrupted - results are incomplete", t.text())
        self.assertEqual(read(self.path("hosts.txt")), "10.6.0.1 keep-me\n")
        self.assertTrue(os.path.exists(self.path("hosts.txt.partial")))

    def test_version(self):
        code, out = self.scan("--version")
        self.assertEqual((code, out.strip()), (0, f"scanT {__version__} (by Timmy)"))
        r = subprocess.run([sys.executable, os.path.join(REPO, "pingT.py"), "--version"], capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), f"pingT {__version__} (by Timmy)")
        self.alive("10.5.0.1")
        self.scan("10.5.0.0/30", "-o", "o.txt")
        self.assertIn(f"# generated by scanT {__version__} on", read(self.path("o.txt")))

    def test_ascii_output(self):
        self.alive("10.5.0.1")
        code, out = self.scan("10.5.0.0/30", "-o", "o.txt", "--charset", "ascii")
        self.assertEqual(code, 0, out)
        self.assertTrue(out.isascii(), {c for c in out if not c.isascii()})


if __name__ == "__main__":
    unittest.main()
