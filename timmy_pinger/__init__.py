"""Timmy Pinger: pingT (watch hosts during a switch migration) and scanT (find them).

Start them with python3 pingT.py and python3 scanT.py. How they work: README.md. Why they work that way: DECISIONS.md.
"""

import os
import sys

__version__ = "3.0"  # pingT and scanT share it; how to bump it: DECISIONS.md "Version numbers"
CREDITS = "by Timmy"

# A clear hint instead of a traceback when rich is missing. It's here, in the package's
# first import, so it covers every module and both tools.
try:
    import rich  # noqa: F401
except ModuleNotFoundError:
    sys.exit(f"{os.path.basename(sys.argv[0])}: the Python package 'rich' is missing in the Python that runs it:\n"
             f"  {sys.executable}\n"
             "Activate the Python environment first - usually:  source ~/.venv/bin/activate\n"
             "(README.md \"Setup\")")
