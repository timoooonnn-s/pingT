#!/usr/bin/env python3
"""Timmy Pinger: python3 pingT.py -f hosts.txt  (--help for all options; the code is in timmy_pinger/)"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from timmy_pinger.cli import main  # noqa: E402 - needs the path above

main()
