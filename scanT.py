#!/usr/bin/env python3
"""Timmy Pinger's scanner: python3 scanT.py -f subnets.txt -o hosts.txt  (--help for all options)"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from timmy_pinger.scanner import main  # noqa: E402 - needs the path above

main()
