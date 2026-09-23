#!/usr/bin/env python3
"""Issue a one-time organizer recovery credential from a trusted server shell.

This is deliberately not an HTTP endpoint.  It must be run by an authorized
operator on the backend host after independently verifying the account owner.
It never prints or returns a bearer token.
"""

import argparse
import hashlib
import secrets
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import models  # noqa: E402
import v2_models  # noqa: E402,F401
from database import SessionLocal  # noqa: E402
from v2_routes import _hash  # noqa: E402
from v2_time import utc_now  # noqa: E402


def bootstrap_recovery(db, user_id: int) -> str:
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None or user.role != "family_member":
        raise ValueError("user is not an organizer-capable family member")
    membership = db.query(v2_models.FamilyMembership).filter(
        v2_models.FamilyMembership.user_id == user.id,
        v2_models.FamilyMembership.role == "ORGANIZER",
        v2_models.FamilyMembership.membership_status == "active",
    ).first()
    if membership is None:
        raise ValueError("user has no active organizer membership")
    recovery_code = secrets.token_urlsafe(32)
    user.organizer_recovery_verifier = _hash(recovery_code)
    user.organizer_recovery_created_at = utc_now()
    user.organizer_recovery_used_at = None
    user.organizer_recovery_failed_attempts = 0
    user.organizer_recovery_locked_until = None
    user.organizer_recovery_one_time = True
    db.add(models.OrganizerRecoveryAuditEvent(user_id=user.id, action="bootstrap_issued", source="trusted_operator"))
    db.commit()
    return recovery_code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--confirm", action="store_true", help="confirm trusted operator authorization")
    args = parser.parse_args()
    if not args.confirm:
        parser.error("refusing to issue recovery credential without --confirm")
    with SessionLocal() as db:
        try:
            recovery_code = bootstrap_recovery(db, args.user_id)
        except ValueError as exc:
            parser.error(str(exc))
    print(f"user_id={args.user_id}")
    print("recovery_code=" + recovery_code)
    print("Store this one-time recovery code securely; it is not shown again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
