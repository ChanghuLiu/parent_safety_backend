# Parent Safety Backend

FastAPI backend for the Parent Safety app. It stores users, elder-child bindings,
daily check-ins, device heartbeat status, help requests, and optional Firebase
Cloud Messaging push notifications.

## Tech Stack

- Python 3.12
- FastAPI
- SQLAlchemy
- SQLite
- Firebase Admin SDK, optional for push notifications

## Install On A New Computer

1. Clone the repository.

```bash
git clone https://github.com/ChanghuLiu/parent_safety_backend.git
cd parent_safety_backend
git checkout develop
```

2. Create and activate a virtual environment.

```bash
python3 -m venv venv
source venv/bin/activate
```

On Windows PowerShell:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

3. Install dependencies.

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

4. Configure Firebase credentials if push notifications are needed.

The Firebase service account JSON is intentionally not committed to git. Put the
file on the new computer and choose one of these options:

```bash
export FIREBASE_CREDENTIALS_PATH=/absolute/path/to/firebase-service-account.json
```

Or place a file named `firebase-service-account.json` next to `main.py`.

If Firebase credentials are not configured, the API still runs, but FCM sends are
skipped and logged.

5. Start the API server.

Production is the default environment. It accepts requests only when the HTTP
`Host` is `parent-safety-api.duckdns.org` or `95.41.57.202`.

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

For local development, explicitly enable development hosts:

```bash
APP_ENV=development uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Set the timezone used for check-in dates and quiet hours when needed:

```bash
APP_TIMEZONE=America/Toronto APP_ENV=development uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

When running locally, open:

- Health check: `http://localhost:8000/api/health`
- API docs: `http://localhost:8000/docs`

## Database

The app uses local SQLite:

```text
parent_safety.db
```

The database file is created automatically on startup. Tables are created by
`Base.metadata.create_all(bind=engine)`, and small SQLite migrations are applied
from `main.py`.

Database files are ignored by git. To start fresh on a test machine, stop the
server and remove the local DB file:

```bash
rm -f parent_safety.db
```

Then restart the server.

## Main API Endpoints

- `GET /api/health`
- `POST /api/register`
- `POST /api/user/update-fcm-token`
- `POST /api/user/unbind`
- `POST /api/user/delete-data`
- `POST /api/elder/create-bind-code`
- `POST /api/child/bind-elder`
- `POST /api/elder/checkin`
- `POST /api/elder/heartbeat`
- `POST /api/elder/help-request`
- `POST /api/child/update-alert-settings`
- `POST /api/child/update-offline-alert-settings`
- `GET /api/child/elder-status/{child_user_id}`

Use `http://localhost:8000/docs` for request and response schemas.

## Authentication

`POST /api/register` returns an `api_token` when it creates a user. Store that
token in the device's secure storage. Every other user endpoint requires:

```http
Authorization: Bearer <api_token>
```

Calling `/api/register` again for an existing device also requires its token and
does not provide token recovery. Users created before token authentication must
have a token issued by an administrator after the upgraded server has started:

```bash
python3 scripts/issue_api_token.py USER_ID
```

The command displays the token once. Transfer it to the corresponding device
securely; the database stores only its SHA-256 hash.

### Unbind and account deletion

Remove one family link by its stable identifier:

```http
POST /api/user/unbind
Authorization: Bearer <api_token>
Content-Type: application/json

{"family_link_id":123}
```

Both participants may remove the link. The binding response and child status
records include `family_link_id`.

Delete the authenticated account and its owned data:

```http
POST /api/user/delete-data
Authorization: Bearer <api_token>
```

No request body is required or used. Both operations are idempotent. Account-deletion
retries are recognized using only a one-way token hash tombstone; the deleted
token cannot authorize any other endpoint.

### Help-request push messages

Help requests use high-priority Android data-only FCM messages. The payload
contains the string fields `event_type`, `child_user_id`, `elder_user_id`,
`title`, and `body`; `event_type` is always `help_request`.

## Offline Alert Worker

The application checks for offline alerts every 15 minutes. Override the interval
(minimum 60 seconds) with `OFFLINE_ALERT_INTERVAL_SECONDS`. Run a single Uvicorn
worker so only one in-process scheduler sends alerts.

## Quick Smoke Test

With the development server running:

```bash
curl http://localhost:8000/api/health
```

Expected response:

```json
{"status":"ok"}
```

Install and run the security regression tests with:

```bash
pip install -r requirements-dev.txt
pytest
```

## Notes

- Do not commit `parent_safety.db`, `.env`, virtual environments, or Firebase
  service account JSON files.
- The current git development branch is `develop`.
- Production accepts the API hostname `parent-safety-api.duckdns.org` and IP
  `95.41.57.202`; other HTTP `Host` values are rejected.
- A production reverse proxy must preserve the original host header (for Nginx,
  use `proxy_set_header Host $host;`).
- For another device on the same network, use the computer's LAN IP instead of
  `localhost`. Add that LAN IP to `allowed_hosts` temporarily for local testing.
