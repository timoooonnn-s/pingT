# Timmy Pinger

**pingT** pings a list of hosts every second and shows all of them on one screen.
It is made for switch migrations: you see at a glance which devices went down during
the cut-over and whether **all of them came back** afterwards.

A second, optional tool — the **scanner** — finds the reachable hosts in your subnets
and writes that host list for you.

---

## Quick start

**1. Go to the Timmy Pinger folder**

```bash
cd timmy-pinger        # wherever you put it
```

**2. Write a host list** — a text file, e.g. `hosts.txt`, one IP per line:

```
[Servers]
10.0.10.1    core-switch
10.0.10.11   esx-01

[Clients 2nd floor]
10.0.20.101
10.0.20.102
```

- `[Servers]` starts a group. Groups are just for the display.
- A name after the IP is optional. Without one, the IP is shown.
- Don't want to type it? Let the [scanner](#the-scanner-optional) write it for you.

**3. Start pingT**

```bash
./pingT -f hosts.txt --fresh
```

`--fresh` starts a new session. Leave it out only when you restart pingT during a
migration (see [below](#if-pingt-was-closed-or-crashed)).

**4. Stop it with `q`.** pingT prints a summary and saves it as a file in this folder.

---

## What you see

```
 pingT   back 265/268  ok 262 warn 2 loss 1 down 3 silent 12 names:label ...
Servers VLAN10  56/56  ●●●●●●●●●● ●●●●●●●●○● ●●●●●●●●○● ●●●●●●●●●●
Clients 2F      73/76  ●X●●●●●●●● ●●●●●●●●●● ●○●●●●○●●● ●●●●●●●●●X
─ problems (4) ──────────────────────────────────────────────────────
✖ clients-01 10.0.61.1  100% ××××××××××××××××××××
● clients-10 10.0.150.1  25% ▃▃×▃▃▃×▃▃▃▃▃×▃▃▃▃▃▃▃
─ events ────────────────────────────────────────────────────────────
14:02:11 clients-01 (10.0.61.1) DOWN  (3 missed in a row)
```

- **Top line:** `back 265/268` = 265 of the 268 hosts that were online are reachable
  right now. Green when all are back, red when some are missing.
- **One line per group**, one dot per host:

  | | Meaning |
  |---|---|
  | `●` green | fine |
  | `●` yellow | lost a few pings |
  | `●` red | losing many pings |
  | `X` red | **down** (3 pings in a row lost) |
  | `○` grey | never answered since start — was already off, **ignored** |
  | `-` grey | ignored by you with `x` (see [clients that went home](#clients-that-went-home)) |
  | `?` purple | typo in the host list, not pinged |

- **Problems:** every host that is not fine, with its recent pings
  (`▁▂▃` = reply, higher = slower, `×` = lost).
- **Events:** what happened when — the same lines are written to a log file.

**Keys**

| Key | Does |
|---|---|
| `q` | quit (summary is printed and saved) |
| `v` | switch view: groups ↔ every host as its own box |
| `n` | show DNS names instead of your labels (and back) |
| `r` `r` | reset the loss counters (press twice) |
| `x` | ignore the down hosts of a group: `x`, group number, the same number again (`x` `u` = undo) |
| `Esc` | cancel a question (`r`, `x`) |

While `r` or `x` waits for its answer, other keys (arrow keys too) do nothing — only the answer,
`Esc`, or 5 seconds without a key close it.

---

## On migration day

**Before the cut-over**

1. Run pingT on a machine that is **not** connected through the switches you are
   migrating — otherwise pingT loses its own network during the cut-over.
2. Start it inside `tmux` (or `screen`), so a dropped SSH session doesn't stop it.
3. Start it **15–30 minutes before** the cut-over, with `--fresh`:
   `./pingT -f hosts.txt --fresh`
   In that time pingT learns which hosts are online. Hosts that never answer are
   greyed out and ignored — they were off before you touched anything.
4. Clients switched off while you wait? Just before the cut-over, ignore them with `x`
   (see [clients that went home](#clients-that-went-home)), so `back` is green when you start.

**During the cut-over**

- Affected groups turn into red `X`, and `back` drops (e.g. `back 190/268`).
- Short hiccups are fine: a host only counts as down after 3 lost pings in a row.

**After the cut-over**

- Wait until `back` is green again (e.g. `back 268/268`).
- Still red? The problems list shows exactly which hosts are missing.
  Press `n` to see their DNS names.
- Hosts that are back but still losing pings stay yellow/red in the problems list —
  that is real packet loss on the new setup.

**When you're done:** press `q`. The summary file lists every host that did **not**
come back, every outage and all packet loss — your migration record.

### Clients that went home

When a client network is part of the migration, users switch off their PCs while you
wait for the cut-over. Those hosts turn into red `X` and hide the real damage. Take
them out, one group at a time:

1. Press **`x`**. Every group with down hosts gets a number next to its line.
2. Press the group's **number**. The top line asks, e.g.
   `ignore 23 DOWN hosts in "Clients 2F" (down 4:12 to 1:35:00)? press 2 again to confirm`
3. Press the **same number again**. `Esc`, or 5 seconds without a key, cancels.

Only the hosts that are **down right now** are ignored, and only in that group. They get a
grey `-`, raise no alarm and don't count in `back`. The top line shows `ignored N`.

- **They are still pinged.** A host that answers again (3 replies in a row, `--down`)
  is watched again by itself — the event says `BACK after being ignored …`.
- **Undo:** `x` then `u` undoes the last ignore. Hosts that came back in the meantime
  are left alone.
- **During the cut-over** you can do the same when more people go home. Careful: a
  client broken by the migration looks just like one that was switched off. Check the
  down times in the question before you confirm.
- **The summary** lists every ignored host with the time you ignored it and whether it
  came back — your record says what was ignored, it doesn't disappear.
- Their outage before the ignore is not counted as loss in the summary; the event
  log still has it.
- After a restart (without `--fresh`) ignored hosts start as silent `○`, not as down.

### If pingT was closed or crashed

Start it again **without** `--fresh`:

```bash
./pingT -f hosts.txt
```

pingT remembers which hosts were online before, so hosts that are still down show up
as down (instead of being ignored as "never answered").

---

## The scanner (optional)

The scanner pings every address of your subnets and writes a host list with all
devices that answered. You can use that list directly with pingT.

**1. Write a subnet list**, e.g. `subnets.txt` — one subnet per line, with a group name:

```
10.0.10.0/24   Servers
10.0.20.0/24   Clients 2nd floor
10.0.30.0/24   Printers
```

**2. Scan** — best during working hours, when the clients are switched on:

```bash
./scan.py -f subnets.txt -o hosts.txt
```

Or scan and start pingT right away: `./scan.py -f subnets.txt -o hosts.txt --then-ping`

**Scanning again later** (e.g. on migration day) without losing anything:

```bash
./scan.py -f subnets.txt --merge hosts.txt -o hosts.txt
```

This keeps your names from the old list, adds new devices, and keeps devices that are
switched off right now (marked `# no reply in scan …`). The old file is saved as
`hosts.txt.bak`.

Good to know:

- It pings every address a few times (3 rounds), so a device that misses one ping
  is still found. A `/24` takes a few seconds.
- It only finds devices that answer ping. Devices with ping blocked (e.g. Windows PCs
  with firewall) can't be found — and pingT couldn't watch them anyway.
- Only scan networks you are responsible for.

---

# Reference

Everything in detail. You don't need this to use the tools.

- [pingT options](#pingt-options)
- [Scanner options](#scanner-options)
- [States and alarms](#states-and-alarms)
- [The screen in detail](#the-screen-in-detail)
- [Host list format](#host-list-format)
- [Files pingT writes](#files-pingt-writes)
- [Remembering hosts across restarts](#remembering-hosts-across-restarts)
- [DNS names](#dns-names)
- [How often hosts are pinged](#how-often-hosts-are-pinged)
- [Troubleshooting](#troubleshooting)
- [For the maintainer](#for-the-maintainer)

## pingT options

```
./pingT [IP ...] [-f FILE] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `-f FILE` | | host list; can be given more than once. IPs can also be listed directly: `./pingT 10.0.0.1 10.0.0.2` |
| `--fresh` | | start a new session: forget which hosts were online in an earlier run |
| `-i SEC` | `1.0` | seconds between two pings to the same host (min 0.2) |
| `-t MS` | `800` | how long to wait for a reply (50–10000) |
| `-w N` | `20` | how many recent pings count for the loss check |
| `--warn N` | `2` | lost pings (out of the last `-w`) until yellow |
| `--loss N` | `3` | lost pings (out of the last `-w`) until red |
| `--down N` | `3` | lost pings **in a row** until down |
| `--events N` | `8` | lines of the event list on screen (0 = hide) |
| `--log FILE` | `pingT-events.log` | event log file; the summary and baseline files are named after it |
| `--no-dns` | | don't look up DNS names |

The thresholds must fit together: `1 <= --warn <= --loss <= -w`.

## Scanner options

```
./scan.py [SUBNET ...] [-f FILE] [-o OUT] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `-f FILE` | | subnet list; can be given more than once. Subnets can also be listed directly: `./scan.py 10.0.10.0/24=Servers` |
| `-o FILE` | `inventory.txt` | host list to write. An existing file is kept as `FILE.bak` |
| `--merge FILE` | | take names from an existing host list and keep its devices (see [the scanner](#the-scanner-optional)) |
| `--drop-missing` | | with `--merge`: leave out devices that didn't answer |
| `--then-ping` | | start pingT with the new list right after the scan |
| `-t MS` | `500` | how long to wait for a reply |
| `--rate N` | `400` | max pings per second in total |
| `--force` | | allow subnets bigger than `/16` or more than 262,144 addresses in one scan |

- **Rounds:** every address is pinged in round 1; rounds 2 and 3 only re-try the addresses
  that didn't answer. The network and broadcast address of a subnet are skipped.
- **Speed:** all subnets are scanned together. Worst case (nothing answers): one `/24`
  ~4 s, three `/24` ~8 s. For big scans `--rate` sets the pace: 2,500 addresses ~8 s per round.
- **Rate:** scanning unused addresses makes the router look up (ARP) every one of them —
  that is what can stress a network, not the pings. Raise `--rate` only if your routers cope.
- **Size limit:** a subnet above `/16` is refused (a typo like `/8` would mean 16 million
  addresses). `--force` overrides it.
- **Overlapping subnets:** each address is scanned once and belongs to the first subnet
  that contains it. Subnets with the same group name end up in one `[group]`.
- **Ctrl-C** (or repeated fping errors) stops the scan and writes what was found to
  `FILE.partial` — your host list is **not** changed.
- **`--merge` in detail:** devices that answered get the name from the old list. Devices
  in the scanned subnets that didn't answer are kept and marked `# no reply in scan <date>`
  (`--drop-missing` leaves them out). Devices outside the scanned subnets, and DNS names,
  are kept unchanged in their old group.
- Subnet list: `CIDR  Group name` or `CIDR=Group name`, `#` starts a comment. Without a
  name, the subnet itself is the group name. Example: [subnets.example.txt](subnets.example.txt)

## States and alarms

pingT looks at the last 20 pings of each host (`-w`).

| State | When (defaults) |
|---|---|
| OK (green) | 0–1 of the last 20 pings lost — a single lost ping is ignored |
| WARN (yellow) | 2 lost (`--warn`) |
| LOSS (red) | 3 or more lost (`--loss`) |
| DOWN (red `X`) | 3 lost **in a row** (`--down`) |
| SILENT (grey `○`) | never answered since pingT started — ignored |
| IGNORED (grey `-`) | you ignored it with `x` — still pinged, watched again after 3 replies in a row |
| INVALID (purple `?`) | the line in the host list is not a valid IP or name |

- When a down host comes back, its outage is removed from the loss count, so it turns
  green right away. The outage is still in the event log and the summary.
- A host counts as **back** when it's not down — a host that loses some pings is back.
- `r` `r` resets the loss counters (to measure only the time after the cut-over).
  Note: it also clears finished outages from the summary; the event log still has them.
  Hosts that are down at that moment stay down, and their outage still counts from when it
  started.

## The screen in detail

**Top line**

| Part | Meaning |
|---|---|
| `back N/M` | reachable now / hosts that answered at some point |
| `ok warn loss down` | number of hosts in each state |
| `silent` | hosts that never answered |
| `ignored` | hosts you ignored with `x` (only shown if there are any) |
| `invalid` | host list lines that can't be pinged (only shown if there are any) |
| `names:label` / `names:FQDN` | which names are shown (`n` switches) |
| `dns N left` | DNS name lookups still running |
| `fping 1:02:13 #3712 1.1s` | runtime, number of ping rounds, duration of the last round |
| purple `PING ERROR …` / `STALE …` | pinging itself has a problem — the screen may be out of date (see [troubleshooting](#troubleshooting)) |

When the window is narrow, the less important parts are hidden first.

**Group line:** `Clients 2F  73/76` = 73 of the 76 hosts that answered at some point
are back. Dots come in blocks of 10 in the order of your host list.

**Box view (`v`):** every host as its own box with name, loss in % and its recent
pings. Pressing `n` changes the names but keeps every host in the same place.

**Recent pings:**

| `▁` | `▂` | `▃` | `▄` | `▅` | `▆` | `▇` | `×` | `·` |
|---|---|---|---|---|---|---|---|---|
| <1 ms | <2 ms | <5 ms | <10 ms | <30 ms | <100 ms | slower | lost | no ping yet |

Green below 30 ms, yellow above.

## Host list format

```
[Servers VLAN10]                  # a group for the lines below
10.0.10.1     core-sw01           # IP + name
10.0.10.11                        # IP only
switch01.corp.local   sw01        # a DNS name also works
10.0.20.7,printer-2f,Office       # comma format: ip,name,group
[]                                # ends the group
10.0.50.7                         # no group: grouped by its /24 -> "10.0.50.0/24"
```

- `#` starts a comment at the start of a line or after a space (`Room #12` is a name).
- The same address twice: the first line counts, the address is pinged once.
- Lines with typos or unknown names are shown as invalid and skipped — pingT still starts.
- DNS names are looked up **once** at start; pingT then pings the IP. If a name gets a
  new IP during the run, restart pingT.
- Example: [hosts.example.txt](hosts.example.txt). To just try pingT out, list a few
  IPs directly: `./pingT 1.1.1.1 8.8.8.8`

## Files pingT writes

In the folder you start pingT from:

| File | Content |
|---|---|
| `pingT-events.log` | every event with date and time — added to on every run |
| `pingT-events-summary-<date>-<time>.txt` | the summary, saved every time pingT stops (named with the time it stopped) |
| `pingT-events-baseline.json` | which hosts were online (for restarts) |

With `--log other.log` the files are named `other…` instead of `pingT-events…`.

The summary is also saved on Ctrl-C, `kill`, or when the SSH session drops.

## Remembering hosts across restarts

- A host that answers once is saved in the baseline file.
- When pingT starts, it reads that file: hosts from it count as "were online", so if
  they don't answer they show up as **down** instead of silent.
- `--fresh` ignores the old file and starts over — use it for every new migration.
  pingT also warns when the file is older than 24 hours.

## DNS names

`n` shows the DNS name (reverse lookup) instead of your label — or the IP if there
is no DNS name.

- **One answer per host, for the whole run.** The answer is kept; nothing is asked
  again. Lookups are spread out (max 10 per second): 255 hosts take ~25 s.
- A host that gets no answer (timeout) is asked again later, up to 5 times; after
  that pingT gives up on it and shows its IP.
- If 3 different hosts in a row get no answer, the DNS server itself counts as down
  (e.g. because it's behind the switch you're migrating). pingT then sends only
  **one lookup per minute** until it answers again — those don't count toward the 5.
  The top line shows `dns no answer … (probing 1/min)`.
- Hosts written as DNS names in the host list are not looked up again.
- `--no-dns` turns it off.

## How often hosts are pinged

- Every round pings every host once. A new round starts every second (`-i`).
- Up to 64 hosts share one fping process; up to 16 processes run in parallel (1,024
  hosts). 255 hosts are all pinged within about 0.3 s.
- If all hosts answer, each host gets one ping per second. If some are down, pingT
  waits for their timeout (800 ms), so a round takes ~1.1 s.
- A host is therefore marked down about 3–3.5 s after it stops answering.
- Load: about 1 ping per second per host.

## Troubleshooting

| Problem | What to do |
|---|---|
| Boxes or odd shapes instead of `▁▂▃` in **PuTTY** | PuTTY's default font lacks these symbols. Pick another font (*Window → Appearance*), set *Window → Translation → Remote character set* to **UTF-8**. |
| Garbage like `â—` instead of symbols | PuTTY's character set isn't UTF-8 (see above). |
| Purple **`PING ERROR`** at the top | fping can't run, e.g. missing permissions. Check with `fping 127.0.0.1`. Hosts are **not** marked down meanwhile. |
| `PING ERROR … 64 of 255 hosts not pinged this round` | One of several fping processes failed. All other hosts are pinged normally; the affected hosts keep their last state until it works again. |
| Purple **`STALE`** at the top | No ping round finished for a while — the screen is out of date. |
| Switches/routers show some lost pings | They limit how many pings they answer. Try `-i 2`, or higher `--warn`/`--loss`. |
| Many hosts grey (silent) | They haven't answered since pingT started: switched off, or blocking ping. Start pingT earlier. |
| After a restart everything is grey | You started with `--fresh`, or from another folder (the baseline file is in the folder you start from). |
| `invalid` hosts at the top | Typos or unknown names in the host list; shown with file and line number. |
| `dns no answer … (probing 1/min)` | The DNS server doesn't answer. Names appear once it does. |
| `note: … a round can take ~Xs` at start | Harmless: with many hosts down, rounds take a bit longer than 1 s. |
| `… the Python package 'rich' is missing …` | You started it without your Python environment. Activate it (the one with `rich` installed) and start again. |
| `Permission denied` when starting `./pingT` | The file lost its "executable" flag while copying: `chmod +x pingT scan.py`. |
| Scanner: `… more than a /16` | Protection against typos. Split the range or add `--force`. |
| Scanner: `FILE.partial` appeared | A scan was interrupted; your host list was not changed. Run the scan again. |

## For the maintainer

**The server needs:** Linux, Python 3.9 or newer, `fping`, and the Python package
`rich` ([requirements.txt](requirements.txt)). Nothing needs root.

**Files**

| File | Purpose |
|---|---|
| `pingT` | start script for the pinger |
| `pingt.py` | the pinger's code |
| `scan.py` | the scanner |
| `*.example.txt` | example host and subnet lists |
| `.gitignore` | keeps logs, summaries, backups and users' own `hosts.txt` / `subnets.txt` / `inventory.txt` out of git |
