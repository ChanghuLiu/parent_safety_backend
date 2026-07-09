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

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Open:

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
- `POST /api/elder/create-bind-code`
- `POST /api/child/bind-elder`
- `POST /api/elder/checkin`
- `POST /api/elder/heartbeat`
- `POST /api/elder/help-request`
- `POST /api/child/update-alert-settings`
- `POST /api/child/update-offline-alert-settings`
- `GET /api/child/elder-status/{child_user_id}`

Use `http://localhost:8000/docs` for request and response schemas.

## Quick Smoke Test

```bash
curl http://localhost:8000/api/health
```

Expected response:

```json
{"status":"ok"}
```

## Notes

- Do not commit `parent_safety.db`, `.env`, virtual environments, or Firebase
  service account JSON files.
- The current git development branch is `develop`.
- For another device on the same network, use the computer's LAN IP instead of
  `localhost`, for example `http://192.168.1.10:8000`.
