"""Run one idempotent Parent Check-In V2 scheduler pass.

Use this from an explicitly configured development/staging cron or durable
worker. It is intentionally not started by the legacy FastAPI lifespan, so
starting the old service cannot silently create schedule events in an
unreviewed database.
"""

import os
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import SessionLocal  # noqa: E402
from v2_time import utc_now  # noqa: E402
from v2_notifications import dispatch_queued_notifications  # noqa: E402
from v2_worker import process_checkin_windows, process_escalations  # noqa: E402


if __name__ == "__main__":
    if os.getenv("APP_ENV", "production").strip().lower() not in {"development", "dev", "testing", "test", "staging"}:
        raise SystemExit("refusing implicit V2 scheduler run outside an explicitly non-production environment")
    db = SessionLocal()
    try:
        now = utc_now()
        windows = process_checkin_windows(db, now)
        escalations = process_escalations(db, now)
        delivered = dispatch_queued_notifications(db)
        print(f"v2 scheduler pass complete windows_changed={windows} escalations_queued={escalations} notifications_delivered={delivered}")
    finally:
        db.close()
