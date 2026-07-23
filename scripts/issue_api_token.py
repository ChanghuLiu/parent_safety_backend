#!/usr/bin/env python3
"""Issue or rotate a bearer token for one existing user."""

import argparse
import hashlib
from pathlib import Path
import secrets
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import models  # noqa: E402
from database import SessionLocal  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id", type=int)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.id == args.user_id).first()
        if user is None:
            parser.error(f"user {args.user_id} does not exist")

        token = secrets.token_urlsafe(32)
        user.api_token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        db.commit()
        print(f"user_id={user.id}")
        print(f"role={user.role}")
        print(f"api_token={token}")
        print("Store this token securely; only its hash is retained.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
