"""Re-run a Timmy Pinger script with the project's .venv Python (where rich is installed).

Imported first by pingT and scan.py, before anything that needs third-party packages,
so both can be started directly (./pingT, ./scan.py). Standard library only.
"""
import os
import sys


def use_project_venv(script: str) -> None:
    """If a .venv exists next to `script` and we're not running in it, exec into it (once)."""
    here = os.path.dirname(os.path.realpath(script))
    venv = os.path.join(here, ".venv")
    python = os.path.join(venv, "bin", "python")
    if (os.path.exists(python) and not os.environ.get("PINGT_IN_VENV")
            and os.path.realpath(sys.prefix) != os.path.realpath(venv)):
        os.environ["PINGT_IN_VENV"] = "1"  # the env var prevents exec loops
        os.execv(python, [python, os.path.realpath(script), *sys.argv[1:]])
