"""Shared helpers with no project imports: time rendering, geo, process lock.

All DB timestamps are stored as naive UTC strings (SQLite's datetime('now')
is UTC, not Moscow time) — comparisons in code stay in UTC via
datetime.utcnow(); conversion to MSK happens only at display time, through
format_msk() below, so there is exactly one place that can get the offset
wrong.
"""

from __future__ import annotations

import fcntl
import math
import os
from datetime import datetime, timedelta
from typing import IO

from constants import MSK_OFFSET_HOURS

_ISO_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f")


def parse_utc(value: str) -> datetime:
    """Parse a naive UTC timestamp string as stored by SQLite/our own code."""
    for fmt in _ISO_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    # Fall back to fromisoformat for anything with a "Z"/offset already.
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def format_msk(iso_utc: str, fmt: str = "%d.%m %H:%M") -> str:
    """Render a stored UTC timestamp string in Moscow time (fixed UTC+3)."""
    dt_utc = parse_utc(iso_utc)
    return (dt_utc + timedelta(hours=MSK_OFFSET_HOURS)).strftime(fmt)


def utcnow_str() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def next_msk_morning(now_utc: datetime, hour: int = 9) -> datetime:
    """Next occurrence of `hour`:00 Moscow time, returned as a UTC datetime."""
    now_msk = now_utc + timedelta(hours=MSK_OFFSET_HOURS)
    candidate_msk = now_msk.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate_msk <= now_msk:
        candidate_msk += timedelta(days=1)
    return candidate_msk - timedelta(hours=MSK_OFFSET_HOURS)


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class SingletonLockError(RuntimeError):
    pass


def acquire_singleton_lock(path: str) -> IO[str]:
    """Take an exclusive, non-blocking flock on `path`.

    Guards against systemd and a manual `python main.py` (e.g. from ssh)
    both running a poller against the same DB at once — duplicate
    notifications and races on sending. The lock is tied to the returned
    file descriptor and is released automatically by the OS whenever the
    process exits, however it exits, so there is no stale-pidfile cleanup
    to worry about. Caller must keep the returned handle alive for the
    life of the process.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        fh.close()
        raise SingletonLockError(
            f"Another instance is already running (lock: {path})"
        ) from exc
    fh.write(str(os.getpid()))
    fh.flush()
    return fh
