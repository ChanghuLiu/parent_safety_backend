"""Create and verify a consistent SQLite backup for a controlled deployment.

This script is intentionally explicit: it never overwrites an existing
backup, never writes beside the live database by default, and uses SQLite's
online backup API instead of copying a live database file.
"""

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--metadata", type=Path)
    args = parser.parse_args()

    source = args.database.resolve()
    output = args.output.resolve()
    if not source.is_file():
        raise SystemExit(f"database does not exist: {source}")
    if source == output:
        raise SystemExit("backup output must differ from the live database")
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing backup: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db:
        with sqlite3.connect(output) as backup_db:
            source_db.backup(backup_db)

    with sqlite3.connect(output) as verify_db:
        integrity = verify_db.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise SystemExit(f"backup integrity check failed: {integrity}")

    record = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source),
        "source_size": source.stat().st_size,
        "backup_path": str(output),
        "backup_size": output.stat().st_size,
        "backup_sha256": sha256(output),
        "integrity_check": integrity,
        "app_env": os.getenv("APP_ENV", ""),
    }
    metadata = (args.metadata or output.with_suffix(output.suffix + ".json")).resolve()
    if metadata.exists():
        raise SystemExit(f"refusing to overwrite metadata: {metadata}")
    metadata.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
