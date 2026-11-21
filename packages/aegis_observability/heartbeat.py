"""File-based liveness for daemons that serve no HTTP.

Docker can only healthcheck what it can observe from inside the container. The API
answers ``/health``; the runner, the reconciler and the alert bridge do not listen on a
port, so a container healthcheck that probed the API port would mark a perfectly healthy
daemon unhealthy (or, worse, a stuck one healthy). A heartbeat file written by the
scheduled loop is the smallest honest signal: if the loop stops, the mtime stops moving.

The file lives in ``/tmp`` by convention and is written atomically (write then rename),
so a healthcheck never reads a half-written value.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

__all__ = ["heartbeat_is_fresh", "touch_heartbeat"]


def touch_heartbeat(path: str | os.PathLike[str]) -> None:
    """Record that the owning loop is alive; never raises.

    A read-only or full ``/tmp`` must not take down the process it is describing, so a
    failed write is silently ignored: the healthcheck then reports stale, which is the
    truthful answer.
    """
    target = Path(path)
    temporary = target.with_suffix(target.suffix + ".tmp")
    try:
        temporary.write_text(str(time.time()), encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        return


def heartbeat_is_fresh(
    path: str | os.PathLike[str], max_age_seconds: float, *, now: float | None = None
) -> bool:
    """True when ``path`` exists and was written less than ``max_age_seconds`` ago."""
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    try:
        modified = Path(path).stat().st_mtime
    except OSError:
        return False
    moment = time.time() if now is None else now
    return moment - modified <= max_age_seconds
