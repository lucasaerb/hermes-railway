"""Small cgroup PID-pressure guard used before Hermes fan-out."""

from __future__ import annotations

import os
from pathlib import Path


def pid_pressure() -> tuple[int, int, int]:
    """Return current, maximum, and threshold; unreadable state sheds closed."""
    try:
        threshold = int(os.getenv("HERMES_PID_SHED_THRESHOLD", "750"))
    except ValueError:
        threshold = 750
    try:
        current = int(Path("/sys/fs/cgroup/pids.current").read_text().strip())
        raw_max = Path("/sys/fs/cgroup/pids.max").read_text().strip()
        maximum = int(raw_max) if raw_max != "max" else 2**31 - 1
    except (OSError, ValueError):
        return threshold, threshold, threshold
    return current, maximum, threshold


def effective_shed_threshold(maximum: int, configured_threshold: int) -> int:
    return min(configured_threshold, max(1, maximum * 3 // 4))


def should_shed() -> bool:
    current, maximum, threshold = pid_pressure()
    # Keep the configured 750-slot ceiling for Railway's 1000-slot cgroup,
    # while retaining 25 percent headroom if a smaller limit is configured.
    return current >= effective_shed_threshold(maximum, threshold)
