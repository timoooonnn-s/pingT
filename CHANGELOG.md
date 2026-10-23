# Changelog

What changed between versions — for everyone who already knows pingT.
How the numbers work: [DECISIONS.md](DECISIONS.md) "Version numbers".

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
