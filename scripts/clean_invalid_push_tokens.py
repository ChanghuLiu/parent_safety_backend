#!/usr/bin/env python3
"""Inspect and optionally clear obviously invalid local push-token data."""

import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sys
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


def _invalid_reason(token: str, device_id: str, platform: str | None, device_uuid: str | None) -> str | None:
    normalized_platform = (platform or "").strip().lower().replace("-", "_")
    lowered = token.lower()
    if normalized_platform in {"harmonyos", "harmony_os"}:
        return "HarmonyOS device has an FCM token"
    if token in {device_id, device_uuid}:
        return "token duplicates an installation/device identifier"
    if _is_uuid(token):
        return "token is an installation UUID"
    if lowered.startswith(("test", "fake", "dummy", "fcm-token", "fcm_token")):
        return "token has an obvious test-token prefix"
    return None


def _masked_metadata(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return f"length={len(token)} sha256_prefix={digest}"


def main() -> int:
    app_env = os.getenv("APP_ENV", "").strip().lower()
    if app_env not in {"development", "dev"}:
        print("Refusing to run: set APP_ENV=development for this local-only utility.")
        return 2

    parser = argparse.ArgumentParser(
        description="Find invalid/test FCM tokens without exposing token values."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Clear only the listed fcm_token fields (default is dry-run).",
    )
    args = parser.parse_args()

    from sqlalchemy import inspect

    from database import SessionLocal, engine
    import models
    import main as backend

    if not inspect(engine).has_table("users"):
        print("Refusing to run: the selected database has no users table.")
        return 2
    backend._ensure_sqlite_migrations()

    db = SessionLocal()
    try:
        rows = (
            db.query(models.User, models.DeviceStatus)
            .outerjoin(
                models.DeviceStatus,
                models.DeviceStatus.user_id == models.User.id,
            )
            .filter(models.User.fcm_token.is_not(None))
            .all()
        )
        matches = []
        for user, status in rows:
            reason = _invalid_reason(
                user.fcm_token,
                user.device_id,
                status.platform if status else None,
                status.device_uuid if status else None,
            )
            if reason:
                matches.append((user, status, reason))
                print(
                    f"user_id={user.id} device_status_id={status.id if status else '-'} "
                    f"{_masked_metadata(user.fcm_token)} reason={reason}"
                )

        if not matches:
            print("No obvious invalid/test FCM tokens found.")
            return 0
        if not args.apply:
            print(f"Dry run: {len(matches)} token field(s) would be cleared.")
            return 0

        invalidated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        for user, _, _ in matches:
            user.fcm_token = None
            user.fcm_token_invalidated_at = invalidated_at
        db.commit()
        print(f"Cleared {len(matches)} invalid/test FCM token field(s); no records deleted.")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
