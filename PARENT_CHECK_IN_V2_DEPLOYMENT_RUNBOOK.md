# Parent Check-In V2 controlled production deployment

This is a runbook only. It has not been executed against production.

## Preflight

Confirm the reviewed backend commit, the Android contract, a maintenance
window, and an operator with access to `/etc/parent-safety/backend.env`.
Never place credentials or purchase tokens in this document.

```bash
sudo systemctl is-active parent-safety-api.service
sudo systemctl show parent-safety-api.service -p WorkingDirectory -p ExecStart
test "$(git -C /home/ubuntu/parent_safety_backend rev-parse HEAD)" = "<reviewed-sha>"
```

Record the current production SHA:

```bash
git -C /home/ubuntu/parent_safety_backend rev-parse HEAD
```

## Consistent backup

Quiesce writes before the backup. Do not use `cp` on a live SQLite file.

```bash
sudo systemctl stop parent-safety-v2-worker.service 2>/dev/null || true
sudo systemctl stop parent-safety-api.service
sudo install -d -m 0750 -o ubuntu -g ubuntu /var/backups/parent-safety
APP_ENV=production /home/ubuntu/parent_safety_backend/venv/bin/python \
  /home/ubuntu/parent_safety_backend/scripts/backup_sqlite_for_deploy.py \
  --database /home/ubuntu/parent_safety_backend/parent_safety.db \
  --output /var/backups/parent-safety/parent_safety-<timestamp>.db
```

The tool records UTC timestamp, source/backup sizes, SHA-256, and
`PRAGMA integrity_check=ok`. Keep the JSON metadata with the backup and
verify the recorded hash before continuing.

## Migration and startup

```bash
cd /home/ubuntu/parent_safety_backend
source venv/bin/activate
APP_ENV=production alembic upgrade head
sudo systemctl start parent-safety-api.service
curl --fail https://parent-safety-api.duckdns.org/api/health
```

Run an authenticated legacy smoke test and an authenticated V2 smoke test
using disposable operator fixtures. Confirm legacy `/api/*` behavior before
starting the V2 worker.

## Start exactly one V2 worker

Install the reviewed unit and tmpfiles entry only during an approved change
window. Do not start more than one instance or add a second scheduler to the
web service.

```bash
sudo systemd-tmpfiles --create deploy/parent-safety-v2-worker.tmpfiles
sudo systemctl daemon-reload
sudo systemctl enable --now parent-safety-v2-worker.service
sudo systemctl status parent-safety-v2-worker.service --no-pager
journalctl -u parent-safety-v2-worker.service -n 100 --no-pager
```

The `flock` lock and durable unique event/delivery keys make an accidental
second start fail safely and make retries idempotent.

## Rollback

Trigger rollback for migration failure, legacy regression, V2 startup failure,
worker failure, authentication regression, or unexpected 5xx responses.

```bash
sudo systemctl stop parent-safety-v2-worker.service
sudo systemctl stop parent-safety-api.service
git -C /home/ubuntu/parent_safety_backend fetch origin
git -C /home/ubuntu/parent_safety_backend checkout <previous-production-sha>
```

If V2 wrote to the database, restore the verified pre-deploy SQLite backup
after the service is stopped. Run `PRAGMA integrity_check`, start the legacy
web service, and repeat legacy health/smoke checks. Do not rely on Alembic
downgrade after V2 data has been written because the V2 downgrade drops V2
tables.

## Backup/restore rehearsal

The rehearsal must use a disposable production-like database and prove:

`legacy database → consistent backup → V2 migration/writes → restore backup →
legacy schema/data and legacy routes verified`

Production execution is not implied by a successful rehearsal.
