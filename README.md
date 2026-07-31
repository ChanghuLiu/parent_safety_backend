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

The Phase 3 migration adds nullable `platform`, `app_version`, and `device_uuid`
columns to `device_status`. It also installs an idempotent trigger that prevents
new duplicate `(elder_user_id, checkin_date)` rows. Existing check-in history is
not deleted or rewritten; fresh databases additionally use a unique index for
that pair.

The HarmonyOS Push Phase 1 migration adds the `device_push_tokens` table. It
does not alter or backfill `users.fcm_token`, so existing Android FCM records,
users, device status, family links, alerts, and history remain unchanged. The
new table keeps one token per registered user/device and provider, and prevents
the same provider token from being assigned to two devices. Rolling back the
application code is safe because older code ignores the new table. Dropping the
table as a separate rollback step removes only provider-neutral registrations;
it does not restore them, and should be done only after taking a backup.

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
- `GET /api/elder/current-status`
- `POST /api/elder/help-request`
- `POST /api/child/help-alerts/{alert_id}/acknowledge`
- `POST /api/child/help-alerts/{alert_id}/resolve`
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

### Device status and parent self-status

The existing heartbeat route remains compatible with the Android request:

```json
{"elder_user_id": 12, "battery_level": 75}
```

It also accepts the optional `platform`, `app_version`, `role`, `device_uuid`,
and `last_online` fields. `role`, when supplied, must be `elder`; the registered
role remains authoritative. The response remains the Android-compatible
`{"success":true}` and never includes API credentials or push tokens.

An elder/parent device can retrieve its own current status without supplying a
user ID:

```http
GET /api/elder/current-status
Authorization: Bearer <elder_api_token>
```

The response includes safety/check-in status, battery and last-online values,
active-help status, and all current `family_link_id` values needed for unbinding.

### Push-token registration

HarmonyOS, Android Google, and Android Huawei devices use the existing
authenticated token route:

```http
POST /api/user/update-fcm-token
Authorization: Bearer <api_token>
Content-Type: application/json
```

The HarmonyOS request is:

```json
{
  "platform": "harmonyos",
  "push_provider": "harmonyos",
  "push_token": "<token returned by pushService.getToken()>",
  "device_id": "<device_id used at /api/register>",
  "app_version": "2.1.0"
}
```

`app_version` is optional. `push_token` must contain at least one non-whitespace
character and at most 4096 characters. Tokens are opaque; no Firebase token
format rules are applied to HarmonyOS.

Successful response:

```json
{
  "success": true,
  "registered": true,
  "platform": "harmonyos",
  "push_provider": "harmonyos",
  "token_updated_at": "2026-07-30T18:42:10.123456",
  "token_changed": true
}
```

The timestamp is UTC. Re-uploading the same active token returns
`token_changed: false` with the original update timestamp. Uploading a changed
token replaces the prior token for that device/provider and updates the
timestamp. Uploading an invalidated token again reactivates it.

The only accepted platform/provider combinations are:

- `android_google` + `fcm`
- `android_huawei` + `huawei_android`
- `harmonyos` + `harmonyos`

Existing Android clients may continue sending `user_id`, `platform`, and
`fcm_token`; the backend derives the corresponding provider. Google tokens
continue to be mirrored to `users.fcm_token` for existing FCM delivery.
Android Huawei and HarmonyOS tokens are stored only in `device_push_tokens`.
HarmonyOS notification delivery setup is documented in
`HARMONYOS_PUSH_SERVER_SETUP.md`.

Error behavior:

- `400`: unsupported platform/provider combination.
- `401`: missing or invalid bearer token.
- `403`: the authenticated token does not own the supplied user/device.
- `404`: the supplied `device_id` is not registered.
- `409`: that provider token is already assigned to another device, or a
  concurrent registration conflicts.
- `422`: missing identity/token, empty or overlong token, or another malformed
  request field.

Responses and logs never include the full token. Registration logs contain the
database user/device record ID, platform, provider, change flag, token length,
and only a masked suffix. `PUSH_DELIVERY_ENABLED=false` disables outbound
delivery only and does not block registration.

### Help alerts

`POST /api/elder/help-request` retains its existing response fields and adds a
stable `alert_id`, `alert_type`, `created_at`, `status`, elder user ID, and parent
device ID. A linked child can update an alert without deleting it:

```http
POST /api/child/help-alerts/42/acknowledge
Authorization: Bearer <child_api_token>
Content-Type: application/json

{"child_user_id":34}
```

Use the corresponding `/resolve` route to clear the active alert. Both actions
are idempotent, require a current family link, and return the updated alert
record.

### Daily safety confirmation

`POST /api/elder/checkin` stores one new record per elder per application-local
date. A repeated request updates and returns the same record instead of returning
an error or adding another row. Only the first submission sends the check-in push
notification.

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

Push delivery is enabled by default. To exercise alert calculation locally
without calling Firebase or another external push provider:

```bash
APP_ENV=development PUSH_DELIVERY_ENABLED=false uvicorn main:app --reload
```

HarmonyOS devices are not routed through FCM. Outbound HarmonyOS notifications
use the dedicated Push Kit V3 provider when both delivery flags and a service
account are configured; see `HARMONYOS_PUSH_SERVER_SETUP.md`. `android_google`
uses FCM; `android_huawei` retains its existing reserved/skipped delivery
behavior.

To inspect obvious invalid/test FCM values in the local development database,
run the safe dry-run first. Output includes record IDs, token length, and a
one-way hash prefix, never the token itself:

```bash
APP_ENV=development python3 scripts/clean_invalid_push_tokens.py
APP_ENV=development python3 scripts/clean_invalid_push_tokens.py --apply
```

The apply form clears only matched `users.fcm_token` fields and records
`fcm_token_invalidated_at`; it does not delete users or related records and
refuses to run outside development.

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
