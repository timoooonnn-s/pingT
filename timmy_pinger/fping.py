"""Pinging through fping: one probe per host and round, many fping processes in parallel."""

from __future__ import annotations

import math
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor


class PingError(Exception):
    """fping itself failed - the results of that call can't be trusted."""


class Fping:
    """Splits the targets into chunks, one fping process per chunk, up to `processes` at once."""

    def __init__(self, timeout_ms: int, spacing_ms: int = 5, chunk_size: int = 64, processes: int = 16):
        self.timeout_ms = timeout_ms
        self.spacing_ms = spacing_ms  # between two probes of one fping process
        self.chunk_size = chunk_size  # targets per fping process
        self.processes = processes
        self.binary = shutil.which("fping")
        self.pool = ThreadPoolExecutor(max_workers=processes)

    def worst_round_time(self, targets: int) -> float:
        """Seconds for one round when no target answers: the chunks run in waves of `processes`."""
        waves = max(1, math.ceil(targets / (self.chunk_size * self.processes)))
        return waves * (self.timeout_ms / 1000 + min(targets, self.chunk_size) * self.spacing_ms / 1000)

    def ping(self, targets: list[str]) -> tuple[dict[str, float | None], list[str]]:
        """One probe per target -> ({ip: round trip in ms or None}, errors). A failing fping
        process only loses its own chunk: those targets are missing from the results."""
        chunks = [targets[i:i + self.chunk_size] for i in range(0, len(targets), self.chunk_size)]
        futures = [self.pool.submit(self._run, chunk) for chunk in chunks]
        results: dict[str, float | None] = {}
        errors: list[str] = []
        for future in futures:
            try:
                results.update(future.result())
            except PingError as e:
                errors.append(str(e))
        return results, errors

    def _run(self, targets: list[str]) -> dict[str, float | None]:
        # -C1 -q: one probe per target, then one summary line per target: "ip : 1.23" or "ip : -"
        cmd = [self.binary, "-C1", "-q", "-r0", "-B1", f"-t{self.timeout_ms}", f"-i{self.spacing_ms}", *targets]
        limit = self.worst_round_time(len(targets)) + 5
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=limit)
        except subprocess.TimeoutExpired:
            raise PingError(f"fping did not finish within {limit:.0f}s ({len(targets)} hosts)") from None
        except OSError as e:
            raise PingError(f"cannot run fping: {e}") from None
        results: dict[str, float | None] = dict.fromkeys(targets)
        parsed = 0
        for line in (proc.stdout + proc.stderr).splitlines():
            target, separator, value = line.partition(" : ")
            target = target.strip()
            if separator and target in results:
                parsed += 1
                try:
                    results[target] = float(value.strip())
                except ValueError:  # "-": no reply
                    pass
        # exit 0: all answered, 1: some didn't, 2: some names didn't resolve; 3 and 4: fping failed
        if proc.returncode >= 3 or parsed == 0:
            messages = [line for line in proc.stderr.strip().splitlines() if " : " not in line]
            raise PingError(f"fping failed (exit {proc.returncode}): "
                            f"{messages[-1] if messages else 'no results for any host'}")
        return results
