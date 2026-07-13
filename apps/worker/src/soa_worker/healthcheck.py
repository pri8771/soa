"""Worker container liveness probe (REL-001).

``python -m soa_worker.healthcheck`` for the image ``HEALTHCHECK``. The
worker has no HTTP surface, so liveness is a heartbeat FILE: the run
loop rewrites ``liveness_file`` every beat (``worker._touch_liveness_
file``), and this probe fails when that file is missing or older than
``heartbeat_interval_seconds * liveness_staleness_factor`` — i.e. the
loop has stalled or died.

Exit codes: 0 healthy (or liveness disabled — no file configured), 1
stale/missing/unreadable. Docker treats any non-zero as unhealthy.
"""

import sys
import time
from pathlib import Path

from soa_worker.settings import WorkerSettings, load_settings


def check(settings: WorkerSettings, *, now: float | None = None) -> tuple[bool, str]:
    """Return (healthy, human message). ``now`` is injectable for tests."""
    if not settings.liveness_file:
        return True, "liveness file not configured; probe disabled"
    path = Path(settings.liveness_file)
    if not path.exists():
        return False, f"liveness file {path} does not exist (worker not started?)"
    current = now if now is not None else time.time()
    max_age = settings.heartbeat_interval_seconds * settings.liveness_staleness_factor
    try:
        age = current - path.stat().st_mtime
    except OSError as error:
        return False, f"cannot stat liveness file {path}: {error}"
    if age > max_age:
        return False, f"liveness file is {age:.1f}s old (max {max_age:.1f}s) — worker stalled"
    return True, f"worker alive ({age:.1f}s since last heartbeat)"


def main() -> int:
    healthy, message = check(load_settings())
    print(message)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
