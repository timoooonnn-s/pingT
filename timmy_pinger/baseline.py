"""The baseline file: which hosts answered at some point.

It survives a crash or restart: started again without --fresh, pingT knows which hosts were
online, so a host that is still down shows as DOWN instead of "never answered"."""

from __future__ import annotations

import json
import os
from datetime import datetime

from .host import Host


class BaselineError(Exception):
    """The file exists but can't be used."""


class Baseline:
    def __init__(self, path: str):
        self.path = path
        self.answered: set[str] = set()  # targets, as written in the host list

    def load(self) -> datetime | None:
        """Reads the file; returns when it was saved, or None when there is none."""
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
            answered = set(data.get("seen", []))
            saved = datetime.fromisoformat(data["saved"])
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise BaselineError(str(e)) from None
        self.answered = answered
        return saved

    def save(self, hosts: list[Host]):
        """Adds the hosts that answered and writes the file - atomically (temp file + rename),
        so a crash in the middle can't leave a broken file. Raises OSError."""
        self.answered |= {h.target for h in hosts if h.answered_once}
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump({"saved": datetime.now().isoformat(timespec="seconds"), "seen": sorted(self.answered)},
                      fh, indent=0)
        os.replace(temp, self.path)

    def forget(self, host: Host):
        """An ignored host must not come back as DOWN after a restart."""
        self.answered.discard(host.target)
