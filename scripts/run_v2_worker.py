"""Run the explicit, single-instance Parent Check-In V2 worker.

Production execution is allowed only through an explicit deployment setting
(``V2_WORKER_EXPLICIT=true``), so the legacy web service cannot accidentally
become a second V2 scheduler.
"""

import os
import signal
import sys
import time
from pathlib import Path
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import SessionLocal  # noqa: E402
from v2_notifications import dispatch_queued_notifications  # noqa: E402
from v2_time import utc_now  # noqa: E402
from v2_worker import process_checkin_windows, process_escalations  # noqa: E402


def main() -> int:
    environment = os.getenv("APP_ENV", "production").strip().lower()
    if environment in {"production", "prod"} and os.getenv("V2_WORKER_EXPLICIT", "").lower() != "true":
        raise SystemExit("refusing production V2 worker without V2_WORKER_EXPLICIT=true")
    if environment not in {"development", "dev", "testing", "test", "staging", "production", "prod"}:
        raise SystemExit(f"invalid APP_ENV={environment!r}")
    interval = int(os.getenv("V2_WORKER_INTERVAL_SECONDS", "60"))
    if interval < 60:
        raise SystemExit("V2_WORKER_INTERVAL_SECONDS must be at least 60")

    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.is_set():
        with SessionLocal() as db:
            process_checkin_windows(db, utc_now())
            process_escalations(db, utc_now())
            dispatch_queued_notifications(db)
        stop.wait(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
