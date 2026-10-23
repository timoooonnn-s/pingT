# Decisions

Why pingT and the scanner work the way they do. One entry per decision: what was decided,
why, and what follows from it. Add new entries at the end; if a decision is reversed, add a
new entry that says so instead of deleting the old one.

---

## 1. IPv4 only — 2026-10-23

**Decision:** pingT and scan.py only handle IPv4.

**Why:** the networks pingT is used for are IPv4. IPv6 support added code paths (other group
sizes, sorting mixed address types) that nobody used, and one of them crashed the scanner.

**Consequences:**
- An IPv6 address in a host list is shown as invalid (`?`, "IPv6 is not supported") and not pinged.
- A DNS name in a host list uses only its IPv4 address; a name with no IPv4 address is invalid.
- `scan.py` refuses IPv6 subnets with an error.

## 2. No "checkpoint"; reset and ignore stay manual — 2026-10-23

**Decision:** there is no key that freezes "the hosts that must come back". `r r` (reset)
and `x` (ignore the DOWN hosts of a group) stay as they are.

**Why:** which hosts matter is the engineer's call. pingT gives an overview of whole subnets;
in a big migration a few missing clients don't matter much, and in a small one the engineer
has the overview anyway — especially since scanning and pinging happen at different times.

## 3. No sound alerts — 2026-10-23

**Decision:** no bell or other sound, not even optional.

**Why:** not wanted.

## 4. `--then-ping` removed from scan.py — 2026-10-23

**Decision:** scan.py no longer starts pingT after a scan.

**Why:** scanning and pinging happen at different times: the scan during working hours (when
the clients are on), pingT on migration day. Starting pingT right after a scan skips the
warm-up time pingT needs.

## 5. A rescan only adds — 2026-10-23

**Decision:** `scan.py --rescan hosts.txt` scans the ranges of an existing host list again and
**only adds** the hosts that are new. It never removes or changes a host that is already in
the list, and prints a diff. `--merge` and `--drop-missing` are gone.

**Why:** a host that doesn't answer in a second scan is usually switched off, not gone.
Dropping it would remove it from the next migration's watch list without anyone noticing.

**Consequences:**
- The ranges come from the `# 10.0.10.0/24: …` lines the scanner writes above each group.
  For a hand-written list they are given once with `-f` / on the command line, and then
  written into the file, so the next rescan finds them.
- The file is otherwise left as it is: labels, comments, order and the comma format stay.
  New hosts go to the end of their group, marked `# new in rescan <date>`.
- To take a host out on purpose, comment it out (`# 10.0.10.5 old-pc`). A rescan doesn't add a
  commented-out host again; the diff mentions it if it answers.

## 6. A new scan doesn't overwrite an existing host list — 2026-10-23

**Decision:** `scan.py … -o hosts.txt` stops with an error if `hosts.txt` exists, and points
to `--rescan`. `--overwrite` replaces it anyway (the old file is kept as `.bak`).

**Why:** a new scan writes bare IPs; replacing a list would silently lose its labels and the
hosts that are switched off right now.

## 7. Three character sets; unicode stays the default — 2026-10-23

**Decision:** `--charset unicode|safe|ascii` (or `PINGT_CHARSET`, or the `g` key while
running). `unicode` stays the default.

**Why:** PuTTY doesn't fall back to another font for characters its font lacks. `safe` uses
only characters every Windows console font has (WGL4: Courier New, Lucida Console, Consolas);
`ascii` works even when PuTTY's character set isn't UTF-8. The full unicode look is the best
one where it works, so it stays the default.

## 8. Grouped events: the screen is condensed, the log stays complete — 2026-10-23

**Decision:** when several hosts of a group have the same event in one ping round (DOWN, UP,
LOSS, …), the screen shows one line (`[Clients 2F] 70 hosts DOWN`). When a group has no DOWN
hosts left after an outage of 2 or more hosts, one line says so (`[Clients 2F] all 70 back
after 7:29`). The log file still gets one line per host, plus the group lines.

**Why:** in a cut-over 70 hosts go down in the same second; one line per host made the
8-line event panel useless exactly when it matters.

## 9. "ALL DOWN" only when every watched host is down — 2026-10-23

**Decision:** pingT warns `ALL DOWN - check pingT's own network` only when **every** watched
host (answered before, not ignored) is DOWN at the same time, and only with at least 5
watched hosts.

**Why:** that pattern almost always means pingT's own connection is gone, not the migration.
A share like 90% would catch more such cases, but would also fire on a real large outage.

## 10. Too many groups for the screen: cut them, keep the problems list — 2026-10-23

**Decision:** when the group lines don't fit, the problems list and the events keep their
rows. The groups that don't fit are left out and summed up in one line
(`… 12 more groups (143 hosts): back 120/130, 10 down`). When everything fits, there is no
such line.

**Why:** before, many groups pushed the problems list off the screen, so a DOWN host could
be invisible.

## 11. `r r` drops the undo of the last ignore — 2026-10-23

**Decision:** after a reset, `x u` is no longer offered.

**Why:** the undo restores the hosts' stats from before the ignore — after a reset those are
stats from before the reset. The ignored hosts still come back by themselves when they answer.

## 12. Tests with the standard library only — 2026-10-23

**Decision:** the tests in `tests/` use `unittest` and a fake `fping` script; they need nothing
but the Python that runs pingT (with `rich`).

**Why:** the servers pingT runs on shouldn't need extra packages, and every engineer should
be able to run the tests with one command (README "For the maintainer").

## 13. Version numbers — 2026-10-23

**Decision:** pingT and scan.py share one version number, `__version__` in `pingt.py`
(`./pingT --version`, `./scan.py --version`). It is also written into the event log, the
summary and the host lists the scanner writes. The first numbered version is **2.0**:
copies from before had no number and count as 1.x.

**Why:** the tool is copied around the team; without a number nobody can tell which version
runs on which server.

**How to bump:** `MAJOR.MINOR`. MINOR for fixes and new features; MAJOR when something
people rely on changes in a way that breaks it (an option removed, a file format changed).
Every bump gets an entry in [CHANGELOG.md](CHANGELOG.md).

## 14. The code is a package of modules, rebuilt, not rewritten — 2026-10-23

**Decision:** the code lives in `timmy_pinger/`, one module per job (README "For the
maintainer"). It was rebuilt from the 2.0 code: same algorithms, new structure and names.

**Why:** in 2.0 one class did five jobs (state machine, events, baseline, key dialogs, the whole
screen), and the scanner imported all of the pinger for six helpers. The algorithms themselves
were tested in real migrations, so they were kept, not rewritten.

**Rules that keep it that way:**
- `host.py` knows nothing about screens or files: `record_ping()` returns events, the session
  writes them.
- `screen.py` only reads the session; the keys change `ViewSettings`.
- Three threads share a session; everything that touches it holds `session.lock`.
- No abstract base classes, interfaces with one implementation or config objects - plain
  functions and small classes until something really needs more.

## 15. The commands are `pingT.py` and `scanT.py` — clean break — 2026-10-23

**Decision:** `./pingT` became `./pingT.py`, `./scan.py` became `./scanT.py`. No forwarders under
the old names. Version 3.0, because people type these commands from memory (decision 13).

**Consequences:** the package can't be called `pingt/`: on macOS (and Windows) file names
ignore case, so it would be the same name as `pingT.py`. Hence `timmy_pinger/`.

## 16. Logs, summaries and the baseline go into `logs/` — 2026-10-23

**Decision:** by default pingT writes into `logs/` next to `pingT.py`, not into the folder it is
started from. Only `logs/README.md` is in git.

**Why:** one place to move off the box or delete after a migration. It also ends a trap: started
from another folder, pingT used to miss its baseline and showed down hosts as "never answered".

## 17. `r r` keeps the record — 2026-10-23

**Decision:** a reset still starts the counters fresh (on screen nothing changes), but the
summary keeps what they held before, as its own table per reset. The group outage timeline is
never reset.

**Why:** the summary is the migration record; one key press must not quietly remove part of it.

## 18. `--charset-test` removed — 2026-10-23

**Decision:** the `g` key is the way to try the character sets (decision 7 otherwise stands).

**Why:** two ways to do one thing; `g` shows the real screen, which is the better test.

## 19. Credits — 2026-10-23

**Decision:** `pingT <version> - by Timmy` in the top line (its least important part, so only
shown when there is room), in `--help`, `--version` and the first line of the summary file.

## 20. How pingT is set up and started — 2026-10-23

**Decision:** every engineer has one Python 3.11 environment in the home folder (`~/.venv`,
with `requirements.txt` installed) and activates it before starting the tools. The README and
`--help` write every command as `python3 pingT.py …` / `python3 scanT.py …`, not `./pingT.py`.
The code comes from the company's internal repository (`git clone`, then `git pull` for updates);
fping is already installed on the server.

**Why:** files copied from Windows (WinSCP & co.) often lose the "executable" flag, so
`./pingT.py` fails with "Permission denied" - `python3 pingT.py` always works. Writing `python3`
also makes visible which Python runs, which is where a missing `rich` comes from.

**Open:** the address of the internal repository - the README's setup has a placeholder
`<INTERNAL-REPO-URL>` until it is known.
