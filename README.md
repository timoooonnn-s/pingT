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
cd pingT        # wherever you put it
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

Using **PuTTY** and seeing boxes instead of symbols? Start with `--charset safe`
(see [troubleshooting](#putty-shows-boxes-or-garbage)).

---

## What you see

```
 pingT   back 265/268  ok 262 warn 2 loss 1 down 3 silent 12 names:label ...
Servers VLAN10  56/56  ●●●●●●●●●● ●●●●●●●●○● ●●●●●●●●○● ●●●●●●●●●●
Clients 2F      73/76  ●X●●●●●●●● ●●●●●●●●●● ●○●●●●○●●● ●●●●●●●●XX
─ problems (6) ──────────────────────────────────────────────────────
✖ clients-01 10.0.61.1        down 4:12 ××××××××××
✖ clients-75 10.0.61.75       down 4:12 ××××××××××
✖ clients-76 10.0.61.76       down 4:12 ××××××××××
● core-sw01 10.0.10.1     15% ▂▂×▂▂▂×▂▂▂▂▂×▂▂▂▂▂▂▂
● esx-03 10.0.10.13       10% ▃▃▃▃▃▃▃▃×▃▃▃▃▃▃×▃▃▃▃
● clients-10 10.0.150.1   10% ▃▃×▃▃▃▃▃▃▃▃▃×▃▃▃▃▃▃▃
─ events ────────────────────────────────────────────────────────────
14:02:11 [Clients 2F] 3 hosts DOWN
14:02:14 core-sw01 (10.0.10.1) LOSS  3/20 lost in window
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

- **Problems:** every host that is not fine. Down hosts show **how long they have been
  down** (`down 4:12`), the others their loss and their recent pings
  (`▁▂▃` = reply, higher = slower, `×` = lost).
- **Events:** what happened when. When several hosts of a group go down (or come back) at
  the same moment, that is **one line** for the group — `[Clients 2F] 3 hosts DOWN`, later
  `[Clients 2F] all 3 back after 7:29`. The log file has every single host.

**Keys**

| Key | Does |
|---|---|
| `q` | quit (summary is printed and saved) |
| `v` | switch view: groups ↔ every host as its own box |
| `n` | names: your **label** → **DNS** name → **IP** (and around again) |
| `r` `r` | reset the loss counters (press twice) |
| `x` | ignore the down hosts of a group (see [clients that went home](#clients-that-went-home)) |
| `g` | characters: unicode → safe → ascii, for PuTTY (see [troubleshooting](#putty-shows-boxes-or-garbage)) |
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
- Purple **`ALL DOWN - check pingT's own network`** at the top: every host is down at the
  same time. That is almost always pingT's own connection, not the migration — check the
  machine pingT runs on first.

**After the cut-over**

- Wait until `back` is green again (e.g. `back 268/268`). The events tell you per group
  when it was complete again (`[Clients 2F] all 70 back after 7:29`).
- Still red? The problems list shows exactly which hosts are missing and for how long.
  Press `n` to see their DNS names or IPs.
- Hosts that are back but still losing pings stay yellow/red in the problems list —
  that is real packet loss on the new setup.

**When you're done:** press `q`. The summary file lists every host that did **not**
come back, every outage and all packet loss — your migration record.

### Clients that went home

When a client network is part of the migration, users switch off their PCs while you
wait for the cut-over. Those hosts turn into red `X` and hide the real damage. Take
them out, one group at a time:

1. Press **`x`**. Every group with down hosts gets a number next to its line (up to 10 groups).
2. Press the group's **number**. The top line asks, e.g.
   `ignore 23 DOWN hosts in "Clients 2F" (down 4:12 to 1:35:00)? press 2 again to confirm`
3. Press the **same number again**. `Esc`, or 5 seconds without a key, cancels.

Only the hosts that are **down right now** are ignored, and only in that group. They get a
grey `-`, raise no alarm and don't count in `back`. The top line shows `ignored N`.

- **They are still pinged.** A host that answers again (3 replies in a row, `--down`)
  is watched again by itself — the event says `BACK after being ignored …`.
- **Undo:** `x` then `u` undoes the last ignore. Hosts that came back in the meantime
  are left alone. After `r` `r` (reset) there is no undo any more.
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

pingT remembers (in its baseline file) which hosts were online before, so hosts that
are still down show up as down instead of being ignored as "never answered". When that
file is older than 24 hours — then you probably wanted `--fresh` — a yellow warning stays
in the top line until you press a key.

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

Now add your names (labels) to `hosts.txt` if you like, and run pingT with it on migration day.

**Scanning again later** (e.g. a few days before the migration) without losing anything:

```bash
./scan.py --rescan hosts.txt
```

This scans the subnets of `hosts.txt` again (they are written in the file) and **only adds**
the devices that are new — at the end of their group, marked `# new in rescan <date>`. It
never removes or changes a device that is already in the list: your names, comments and
devices that are switched off right now all stay. It prints what changed:

```
rescan of hosts.txt: 2 new, 120 known answered, 3 known without reply (kept)
+ new, added to hosts.txt (marked '# new in rescan'):
  + 10.0.20.155        Clients 2nd floor
- no reply this time, kept unchanged:
  - 10.0.20.11         pc-accounting   Clients 2nd floor
```

The old file is saved as `hosts.txt.bak`.

- **Don't want a device any more?** Comment its line out (`# 10.0.20.11 pc-old`). A rescan
  won't add it again.
- **A host list you wrote by hand** has no subnets in it yet. Give them once:
  `./scan.py --rescan hosts.txt -f subnets.txt` — they are then written into the file.
- A new scan **won't overwrite** an existing host list (you'd lose your names). Use
  `--rescan`, or `--overwrite` if you really want a fresh list.

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
| `--charset NAME` | `unicode` | characters to draw with: `unicode`, `safe`, `ascii` (see [PuTTY](#putty-shows-boxes-or-garbage)); the default comes from `PINGT_CHARSET` if set |
| `--charset-test` | | print all three character sets and exit |

The thresholds must fit together: `1 <= --warn <= --loss <= -w`.

## Scanner options

```
./scan.py [SUBNET ...] [-f FILE] [-o OUT] [options]
./scan.py --rescan FILE [SUBNET ...] [-f FILE] [options]
```

| Option | Default | Meaning |
|---|---|---|
| `-f FILE` | | subnet list; can be given more than once. Subnets can also be listed directly: `./scan.py 10.0.10.0/24=Servers` |
| `-o FILE` | `inventory.txt` | host list to write. An existing file is kept as `FILE.bak`. With `--rescan`: the rescanned file itself |
| `--rescan FILE` | | scan the subnets of this host list again and only **add** new devices; prints a diff (see [the scanner](#the-scanner-optional)) |
| `--overwrite` | | let a new scan replace an existing host list (its names are lost) |
| `-t MS` | `500` | how long to wait for a reply |
| `--rate N` | `400` | max pings per second in total |
| `--force` | | allow subnets bigger than `/16` or more than 262,144 addresses in one scan |
| `--charset NAME` | `unicode` | as for pingT |

- **Rounds:** rounds 2 and 3 only re-try the addresses that didn't answer. The network and
  broadcast address of a subnet are skipped.
- **Speed:** all subnets are scanned together. Worst case (nothing answers): one `/24`
  ~4 s, three `/24` ~8 s. For big scans `--rate` sets the pace: 2,500 addresses ~8 s per round.
- **Rate:** scanning unused addresses makes the router look up (ARP) every one of them —
  that is what can stress a network, not the pings. Raise `--rate` only if your routers cope.
- **Overlapping subnets:** each address is scanned once and belongs to the first subnet
  that contains it. Subnets with the same group name end up in one `[group]`.
- **Ctrl-C** (or repeated fping errors) stops the scan and writes what was found to
  `FILE.partial` — your host list is **not** changed.
- **The output folder** is checked before the scan starts, so a typo in `-o` can't cost you a scan.
- **`--rescan`:** the subnets are the `# 10.0.10.0/24: …` lines above each group (the scanner
  writes them). Subnets given with `-f` or on the command line are used instead, and written
  into the file for the next rescan. Devices outside these subnets, and DNS names, are left
  alone. A device commented out (`# 10.0.10.5 …`) is not added again.
- Subnet list: `CIDR  Group name` or `CIDR=Group name`, `#` starts a comment. Without a
  name, the subnet itself is the group name. Example: [subnets.example.txt](subnets.example.txt)
- **IPv4 only** (see [DECISIONS.md](DECISIONS.md)).

## States and alarms

pingT looks at the last 20 pings of each host (`-w`). The exact limits behind the colors
(grey and purple: see [what you see](#what-you-see)):

| State | When (defaults) |
|---|---|
| OK (green) | 0–1 of the last 20 pings lost — a single lost ping is ignored |
| WARN (yellow) | 2 lost (`--warn`) |
| LOSS (red) | 3 or more lost (`--loss`) |
| DOWN (red `X`) | 3 lost **in a row** (`--down`) |

- When a down host comes back, its outage is removed from the loss count, so it turns
  green right away. The outage is still in the event log and the summary.
- A host counts as **back** when it's not down — a host that loses some pings is back.
- **ALL DOWN:** every watched host (answered before, not ignored) is down at the same time,
  and there are at least 5 of them. Shown in the top line, the log and the summary.
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
| `names:label` / `names:dns` / `names:ip` | which names are shown (`n` switches) |
| `dns N left` | DNS name lookups still running |
| `dns no answer, N left (probing 1/min)` | the DNS server doesn't answer (see [DNS names](#dns-names)) |
| `fping 1:02:13 #3712 1.1s` | runtime, number of ping rounds, duration of the last round |
| purple `PING ERROR …` / `STALE …` | pinging itself has a problem — the screen may be out of date (see [troubleshooting](#troubleshooting)) |
| purple `ALL DOWN …` | every host is down at once — check pingT's own network |
| yellow `baseline is … old …` | the baseline is older than 24 h; any key hides it |

When the window is narrow, the less important parts are hidden first.

**Group line:** `Clients 2F  73/76` = 73 of the 76 hosts that answered at some point
are back. Dots come in blocks of 10 in the order of your host list.

**Too many groups for the window:** the problems list and the events always keep some rows.
The groups that don't fit are left out and summed up in one line, e.g.
`… 12 more groups (143 hosts): back 120/130, 10 down`. Their down hosts are still in the
problems list. With a big enough window there is no such line.

**Problems list:** worst first. Down hosts show how long they have been down
(`down 4:12`, `down 1h35` after an hour), the others their loss in % of the last 20 pings
and their recent pings.

**Box view (`v`):** every host as its own box with name, loss in % (for a down host: how
long it is down) and its recent pings. Pressing `n` changes the names but keeps every host
in the same place.

**Names (`n`):** `label` = the name from your host list (or the IP if there is none),
`dns` = the DNS name (the IP if there is none), `ip` = the IP — also for entries written as
DNS names.

**Recent pings:**

| `▁` | `▂` | `▃` | `▄` | `▅` | `▆` | `▇` | `×` | `·` |
|---|---|---|---|---|---|---|---|---|
| <1 ms | <2 ms | <5 ms | <10 ms | <30 ms | <100 ms | slower | lost | no ping yet |

Green below 30 ms, yellow above. With `--charset safe` it is `░` <2 ms, `▒` <10 ms,
`▓` <100 ms, `█` slower; with `--charset ascii` `_ - = + * # @` (same steps as the table)
and `x` for lost.

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

- `#` starts a comment at the start of a line, or when it has a space before **and** after
  it (` # note`). `Room #12` and `host #note` are names.
- An entry without a group goes into its `/24` (`10.0.50.0/24`); a DNS name without a group
  goes into the group `other`.
- The same address twice: the first line counts, the address is pinged once.
- Lines with typos or unknown names are shown as invalid and skipped — pingT still starts.
  IPv6 addresses are invalid too (IPv4 only, see [DECISIONS.md](DECISIONS.md)).
- DNS names are looked up **once** at start (IPv4 only); pingT then pings the IP. If a name
  gets a new IP during the run, restart pingT. A name without an answer within 5 s is invalid.
- Example: [hosts.example.txt](hosts.example.txt). To just try pingT out, list a few
  IPs directly: `./pingT 1.1.1.1 8.8.8.8`

## Files pingT writes

Next to the event log — with the default `--log`, in the folder you start pingT from:

| File | Content |
|---|---|
| `pingT-events.log` | every event with date and time — added to on every run |
| `pingT-events-summary-<date>-<time>.txt` | the summary, saved every time pingT stops (named with the time it stopped) |
| `pingT-events-baseline.json` | which hosts were online (for [restarts](#if-pingt-was-closed-or-crashed)) |

With `--log other.log` the files are named `other…` instead of `pingT-events…`.

The summary is also saved on Ctrl-C, `kill`, or when the SSH session drops.

## DNS names

`n` switches to the DNS name (reverse lookup) of each host — or its IP if there is no DNS
name. One more `n` shows the IPs, the next one your labels again.

- **One answer per host, for the whole run.** The answer is kept; nothing is asked
  again. All hosts are looked up at once (32 queries in flight), so names show up
  within seconds; the log gets a `dns done` line with how long it took.
- A host that gets no answer (timeout) is asked again a few seconds later — 5 tries in
  total; after that pingT gives up on it and shows its IP.
- If no host at all gets an answer — not even one that answered before — the DNS
  server itself counts as down (e.g. because it's behind the switch you're migrating).
  pingT then sends only **one lookup per minute** until it answers again — those don't
  count toward the 5.
  The top line shows `dns no answer … (probing 1/min)`.
- Hosts written as DNS names in the host list are not looked up again: their DNS name is
  the one you wrote.
- `--no-dns` turns it off; `n` then switches between labels and IPs.

## How often hosts are pinged

- Every round pings every host once. A new round starts every second (`-i`).
- Up to 64 hosts share one fping process; up to 16 processes run in parallel (1,024
  hosts). 255 hosts are all pinged within about 0.3 s.
- If all hosts answer, each host gets one ping per second. If some are down, pingT
  waits for their timeout (800 ms), so a round takes ~1.1 s.
- A host is therefore marked down about 3–3.5 s after it stops answering.
- Load: about 1 ping per second per host.

## Troubleshooting

### PuTTY shows boxes or garbage

PuTTY's fonts lack some of pingT's symbols (`▁▂▃`, `✖`, the heavy table lines), and
PuTTY doesn't take them from another font. Three ways out:

1. **Use the safe characters:** `./pingT -f hosts.txt --charset safe`. They are in every
   Windows console font (Courier New, Lucida Console, Consolas). To make it the default for
   you, put `export PINGT_CHARSET=safe` into your `~/.bashrc` on the server. Try it
   while pingT runs: `g` switches unicode → safe → ascii.
2. **Garbage like `â—`** means PuTTY's character set isn't UTF-8: set *Window → Translation →
   Remote character set* to **UTF-8** — or use `--charset ascii`, which works with any
   setting.
3. **Use Windows Terminal instead of PuTTY:** Windows 10/11 have `ssh` built in
   (`ssh user@server` in Windows Terminal). It finds missing symbols in other fonts, so
   the full unicode look works.

`./pingT --charset-test` prints all three sets — use the first line where every character
looks right.

### Other problems

| Problem | What to do |
|---|---|
| Purple **`PING ERROR`** at the top | fping can't run, e.g. missing permissions. Check with `fping 127.0.0.1`. Hosts are **not** marked down meanwhile. |
| `PING ERROR … 64 of 255 hosts not pinged this round` | One of several fping processes failed. All other hosts are pinged normally; the affected hosts keep their last state until it works again. |
| Purple **`STALE`** at the top | No ping round finished for a while — the screen is out of date. |
| Purple **`ALL DOWN`** at the top | Every host is down at once — usually pingT's own network (cable, uplink, VPN). Check the machine pingT runs on. |
| Switches/routers show some lost pings | They limit how many pings they answer. Try `-i 2`, or higher `--warn`/`--loss`. |
| Many hosts grey (silent) | They haven't answered since pingT started: switched off, or blocking ping. Start pingT earlier. |
| After a restart everything is grey | You started with `--fresh`, or from another folder, or with another `--log` (the baseline file is next to the event log). |
| `invalid` hosts at the top | Typos or unknown names in the host list; shown with file and line number. |
| `note: with N hosts a round can take ~Xs …` at start | Harmless: with more than ~1,000 hosts (or a long `-t`), a round takes longer than 1 s when many hosts are down. |
| `… the Python package 'rich' is missing …` | You started it without your Python environment. Activate it (the one with `rich` installed) and start again. |
| `Permission denied` when starting `./pingT` | The file lost its "executable" flag while copying: `chmod +x pingT scan.py`. |
| Scanner: `… more than a /16` | Protection against typos (`/8` would be 16 million addresses). Split the range or add `--force`. |
| Scanner: `hosts.txt exists - use --rescan …` | A new scan would replace your list and its names. Use `--rescan hosts.txt`, or `--overwrite`. |

## For the maintainer

**The server needs:** Linux (macOS works too), Python 3.9 or newer, `fping`, and the Python
package `rich` ([requirements.txt](requirements.txt)). Nothing needs root.

**Version:** `./pingT --version` (scan.py has the same number). It lives in `__version__` in
`pingt.py`; what changed in each version is in [CHANGELOG.md](CHANGELOG.md).

**Design decisions** — why things are the way they are (IPv4 only, a rescan only adds, …):
[DECISIONS.md](DECISIONS.md). Add an entry there when you decide something new.

**Tests:** from the pingT folder, with the Python that has `rich`:

```bash
python3 -m unittest discover -s tests
```

They take about a minute and need no network: a fake `fping` (`tests/fakebin/fping`) answers
instead. Some tests run pingT in a pseudo terminal, so they need Linux or macOS.

**Files**

| File | Purpose |
|---|---|
| `pingT` | start script for the pinger |
| `pingt.py` | the pinger's code |
| `scan.py` | the scanner |
| `requirements.txt` | the Python package pingT needs (`rich`) |
| `hosts.example.txt` / `subnets.example.txt` | example host and subnet lists |
| `DECISIONS.md` / `CHANGELOG.md` | design decisions / what changed in each version |
| `tests/` | the tests and the fake `fping` |
| `.gitignore` | keeps logs, summaries, baselines, `.bak`/`.partial` files and users' own `hosts.txt` / `subnets.txt` / `inventory.txt` out of git |
