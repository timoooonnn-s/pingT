"""The characters pingT and scanT draw with. PuTTY's fonts lack some of the unicode ones,
and PuTTY doesn't take missing characters from another font (README "PuTTY shows boxes")."""

from __future__ import annotations

import argparse
import math
import os
from dataclasses import dataclass

from rich import box

from .host import DOWN, IGNORED, INVALID, LOSS, OK, SILENT, UNKNOWN, WARN


@dataclass(frozen=True)
class Charset:
    name: str
    dots: dict  # state -> the one character per host in the group lines
    markers: dict  # state -> first character of a host box (problems list, box view); default: markers[OK]
    lost: str  # a lost ping in the recent-pings bar
    no_ping_yet: str
    latency: tuple  # (round trip below this many ms, character), fastest first
    line: str  # separator lines
    ellipsis: str  # marks cut-off text
    table: box.Box  # summary and scan tables
    overflow: str  # how rich cuts text that doesn't fit ("ellipsis" draws "…", not ASCII)

    def latency_char(self, rtt: float) -> str:
        return next(char for limit, char in self.latency if rtt < limit)


_DOTS = {UNKNOWN: "·", SILENT: "○", IGNORED: "-", INVALID: "?", OK: "●", WARN: "●", LOSS: "●", DOWN: "X"}

CHARSETS = {cs.name: cs for cs in (
    Charset("unicode", _DOTS, {DOWN: "✖", INVALID: "?", IGNORED: "-", OK: "●"}, "×", "·",
            ((1, "▁"), (2, "▂"), (5, "▃"), (10, "▄"), (30, "▅"), (100, "▆"), (math.inf, "▇")),
            "─", "…", box.HEAVY_HEAD, "ellipsis"),
    # only characters every Windows console font has (WGL4: Courier New, Lucida Console, Consolas)
    Charset("safe", _DOTS, {DOWN: "X", INVALID: "?", IGNORED: "-", OK: "●"}, "×", "·",
            ((2, "░"), (10, "▒"), (100, "▓"), (math.inf, "█")),
            "─", "…", box.SQUARE, "ellipsis"),
    # 7-bit ASCII: works even when the terminal's character set is wrong
    Charset("ascii", {UNKNOWN: ".", SILENT: ".", IGNORED: "-", INVALID: "?", OK: "o", WARN: "o", LOSS: "o", DOWN: "X"},
            {DOWN: "X", INVALID: "?", IGNORED: "-", OK: "o"}, "x", ".",
            ((1, "_"), (2, "-"), (5, "="), (10, "+"), (30, "*"), (100, "#"), (math.inf, "@")),
            "-", "...", box.ASCII, "crop"),
)}

ENV_VARIABLE = "PINGT_CHARSET"


def add_charset_option(parser):
    """--charset (on a parser or an argument group); its default comes from $PINGT_CHARSET,
    checked by charset_from_args."""
    parser.add_argument("--charset", choices=list(CHARSETS), default=None, metavar="NAME",
                        help=f"characters to draw with: unicode (default), safe (every Windows font - "
                             f"for PuTTY), ascii (any character set); set your default with "
                             f"export {ENV_VARIABLE}=safe")


def charset_from_args(parser: argparse.ArgumentParser, args) -> Charset:
    if args.charset:
        return CHARSETS[args.charset]
    name = os.environ.get(ENV_VARIABLE, "").strip().lower() or "unicode"
    if name not in CHARSETS:
        parser.error(f"{ENV_VARIABLE}={name!r} is unknown - use one of: {', '.join(CHARSETS)}")
    return CHARSETS[name]
