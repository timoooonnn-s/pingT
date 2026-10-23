# Changelog

What changed between versions — for everyone who already knows pingT.
How the numbers work: [DECISIONS.md](DECISIONS.md) "Version numbers".

## 3.0 — 2026-10-23

**Changed — check your habits and scripts**

- **The commands are now `python3 pingT.py` and `python3 scanT.py`** (before: `./pingT` and
  `./scan.py`). The old names are gone.
- **Setup:** one Python 3.11 environment in your home folder (`~/.venv`), activated before
  every start — README "Setup (once)".
- **pingT writes into the `logs/` folder next to `pingT.py`** (event log, summaries,
  baseline), no matter which folder you start it from. Move or delete them there after a
  migration. `--log` still puts them somewhere else.
- `--charset-test` is gone: press `g` while pingT runs to try the character sets.
- `r` `r` no longer deletes anything from the summary: what the counters held before the
  reset gets its own table ("Before the reset at …").

**New**

- The summary has a **timeline of the group outages**: when 2 or more hosts of a group went
  down, when the group was complete again and how long it took — or that it is still down.
- `--help` of both tools is grouped, with examples (and pingT's keys).
- The README has two explicit parts: quick start and everyday use, and the reference.
- Credits: `pingT 3.0 - by Timmy` at the right end of the top line (when there is room), in
  `--help`, `--version` and the summary file.

**Fixed**

- Restarting pingT from another folder lost the baseline (it was next to the log in the
  current folder) - now it is always in `logs/`.
- After `r` `r` the runtime in the top line started again at 0:00; it now counts from the
  start, and the summary says since when the counters run.
- Leaving pingT after the SSH session dropped could end with a traceback when resetting the
  terminal (the summary was saved anyway).

**Inside**

- The code is rebuilt as the package `timmy_pinger/` (one module per job, see README "For
  the maintainer"); `pingT.py` and `scanT.py` only start it. Everything else behaves as in 2.0.

## 2.0 — 2026-10-23

The first numbered version. Earlier copies had no version number (1.x).

**Changed — check your habits and scripts**

- `scan.py --merge` and `--drop-missing` are gone. Use **`./scan.py --rescan hosts.txt`**:
  it scans the subnets written in the file again, only **adds** new devices and prints a
  diff. It never removes or changes a device already in the list.
- `scan.py --then-ping` is gone: scan during working hours, run pingT on migration day.
- A new scan **won't overwrite** an existing host list any more. Use `--rescan`, or
  `--overwrite` to replace it.
- **IPv4 only.** IPv6 addresses in a host list are shown as invalid; the scanner refuses IPv6
  subnets.
- `n` now cycles through **label → DNS name → IP** (before: label ↔ DNS name).
- `r` `r` (reset) removes the undo of the last ignore (`x u`).

**New**

- **`ALL DOWN - check pingT's own network`** when every watched host is down at once.
- **Grouped events:** one line per group when several of its hosts go down or come back at
  the same time (`[Clients 2F] 70 hosts DOWN`, `[Clients 2F] all 70 back after 7:29`). The
  log file still has every host.
- The problems list shows **how long** a down host has been down (`down 4:12`).
- **PuTTY:** `--charset safe` / `ascii`, `PINGT_CHARSET`, the `g` key and `--charset-test`.
- When the groups don't fit on the screen, the problems list and events stay visible; the
  groups left out are summed up in one line.
- The "baseline older than 24 h" warning stays in the top line until a key is pressed.
- `--version` for pingT and scan.py; the version is also in the log, the summary and the
  host lists the scanner writes.
- Tests (`tests/`) and [DECISIONS.md](DECISIONS.md).

**Fixed**

- The summary was not saved when a label, group or DNS name contained `[` … `]`.
- The scanner lost its results when the output file couldn't be written; the folder is now
  checked before the scan.
- A broken baseline file stopped pingT at start.
- With many groups, the problems list could disappear from the screen.
- Group names with `=` in a subnet list (`10.0.0.0/24  Lab=Test`) were rejected.
- Hosts that never answered showed `100%` loss in the box view.
- Alt+key counted as `Esc` followed by the key.
