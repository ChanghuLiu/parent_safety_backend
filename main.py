import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
import hashlib
import logging
import os
from pathlib import Path
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import firebase_admin
    from firebase_admin import credentials, messaging
except ImportError:  # Firebase is optional until push credentials are configured.
    firebase_admin = None
    credentials = None
    messaging = None

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
import schemas
from database import Base, SessionLocal, engine, get_db


logger = logging.getLogger("parent_safety")
logging.basicConfig(level=logging.INFO)

APP_ENV = os.getenv("APP_ENV", "production").strip().lower()
VALID_APP_ENVS = {"development", "dev", "testing", "test", "production", "prod"}
if APP_ENV not in VALID_APP_ENVS:
    raise RuntimeError(
        f"Invalid APP_ENV={APP_ENV!r}; expected one of {sorted(VALID_APP_ENVS)}"
    )
IS_PRODUCTION = APP_ENV in {"production", "prod"}

PRODUCTION_HOSTS = [
    "parent-safety-api.duckdns.org",
    "95.41.57.202",
]
PRODUCTION_ORIGINS = [
    "https://parent-safety-api.duckdns.org",
    "http://95.41.57.202",
    "https://95.41.57.202",
]
OFFLINE_ALERT_INTERVAL_SECONDS = int(
    os.getenv("OFFLINE_ALERT_INTERVAL_SECONDS", "900")
)
if OFFLINE_ALERT_INTERVAL_SECONDS < 60:
    raise RuntimeError("OFFLINE_ALERT_INTERVAL_SECONDS must be at least 60")

APP_TIMEZONE_NAME = os.getenv("APP_TIMEZONE", "UTC")
try:
    APP_TIMEZONE = ZoneInfo(APP_TIMEZONE_NAME)
except ZoneInfoNotFoundError as exc:
    raise RuntimeError(f"Invalid APP_TIMEZONE={APP_TIMEZONE_NAME!r}") from exc

allowed_hosts = list(PRODUCTION_HOSTS)
allowed_origins = list(PRODUCTION_ORIGINS)
if not IS_PRODUCTION:
    allowed_hosts.extend(["localhost", "127.0.0.1", "testserver"])
    allowed_origins.extend(
        [
            "http://localhost",
            "http://localhost:8000",
            "http://127.0.0.1",
            "http://127.0.0.1:8000",
        ]
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    global offline_alert_task
    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_migrations()
    offline_alert_task = asyncio.create_task(_offline_alert_loop())
    try:
        yield
    finally:
        offline_alert_task.cancel()
        with suppress(asyncio.CancelledError):
            await offline_alert_task
        offline_alert_task = None


app = FastAPI(title="爸妈平安 Backend", lifespan=lifespan)
bearer_scheme = HTTPBearer(auto_error=False)
offline_alert_task: asyncio.Task | None = None

# Reject requests sent to an unapproved Host before they reach an API route.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    safe_errors = [
        {
            "loc": list(error.get("loc", ())),
            "type": error.get("type"),
            "msg": error.get("msg"),
        }
        for error in exc.errors()
    ]
    logger.warning(
        "validation failed path=%s errors=%s",
        request.url.path,
        safe_errors,
    )
    return JSONResponse(
        status_code=422,
        content={"detail": safe_errors},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _ensure_sqlite_migrations():
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        columns = connection.execute(sql_text("PRAGMA table_info(users)")).fetchall()
        column_names = {column[1] for column in columns}
        if "fcm_token" not in column_names:
            connection.execute(sql_text("ALTER TABLE users ADD COLUMN fcm_token VARCHAR"))
            logger.info("migration added users.fcm_token column")
        if "api_token_hash" not in column_names:
            connection.execute(sql_text("ALTER TABLE users ADD COLUMN api_token_hash VARCHAR(64)"))
            logger.info("migration added users.api_token_hash column")
        connection.execute(
            sql_text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_api_token_hash "
                "ON users(api_token_hash) WHERE api_token_hash IS NOT NULL"
            )
        )
        connection.execute(
            sql_text(
                """
                CREATE TRIGGER IF NOT EXISTS prevent_duplicate_user
                BEFORE INSERT ON users
                WHEN EXISTS (
                    SELECT 1 FROM users
                    WHERE device_id = NEW.device_id AND role = NEW.role
                )
                BEGIN
                    SELECT RAISE(ABORT, 'duplicate user device and role');
                END
                """
            )
        )
        connection.execute(
            sql_text(
                """
                CREATE TRIGGER IF NOT EXISTS prevent_duplicate_active_bind_code
                BEFORE INSERT ON bind_codes
                WHEN EXISTS (
                    SELECT 1 FROM bind_codes
                    WHERE code = NEW.code AND used = 0
                )
                BEGIN
                    SELECT RAISE(ABORT, 'duplicate active bind code');
                END
                """
            )
        )

        family_columns = connection.execute(sql_text("PRAGMA table_info(family_links)")).fetchall()
        family_column_names = {column[1] for column in family_columns}
        if "offline_alert_enabled" not in family_column_names:
            connection.execute(sql_text("ALTER TABLE family_links ADD COLUMN offline_alert_enabled BOOLEAN DEFAULT 1 NOT NULL"))
            logger.info("migration added family_links.offline_alert_enabled column")
        if "offline_alert_hours" not in family_column_names:
            connection.execute(sql_text("ALTER TABLE family_links ADD COLUMN offline_alert_hours INTEGER DEFAULT 6 NOT NULL"))
            logger.info("migration added family_links.offline_alert_hours column")
        if "last_offline_alert_sent_at" not in family_column_names:
            connection.execute(sql_text("ALTER TABLE family_links ADD COLUMN last_offline_alert_sent_at VARCHAR"))
            logger.info("migration added family_links.last_offline_alert_sent_at column")
        if "offline_quiet_hours_enabled" not in family_column_names:
            connection.execute(sql_text("ALTER TABLE family_links ADD COLUMN offline_quiet_hours_enabled BOOLEAN DEFAULT 1 NOT NULL"))
            logger.info("migration added family_links.offline_quiet_hours_enabled column")
        if "offline_quiet_start_time" not in family_column_names:
            connection.execute(sql_text("ALTER TABLE family_links ADD COLUMN offline_quiet_start_time TEXT DEFAULT '22:00' NOT NULL"))
            logger.info("migration added family_links.offline_quiet_start_time column")
        if "offline_quiet_end_time" not in family_column_names:
            connection.execute(sql_text("ALTER TABLE family_links ADD COLUMN offline_quiet_end_time TEXT DEFAULT '07:00' NOT NULL"))
            logger.info("migration added family_links.offline_quiet_end_time column")
        if "offline_pause_until" not in family_column_names:
            connection.execute(sql_text("ALTER TABLE family_links ADD COLUMN offline_pause_until TEXT"))
            logger.info("migration added family_links.offline_pause_until column")
        connection.execute(
            sql_text(
                """
                CREATE TRIGGER IF NOT EXISTS prevent_duplicate_family_link
                BEFORE INSERT ON family_links
                WHEN EXISTS (
                    SELECT 1 FROM family_links
                    WHERE elder_user_id = NEW.elder_user_id
                      AND child_user_id = NEW.child_user_id
                )
                BEGIN
                    SELECT RAISE(ABORT, 'duplicate family link');
                END
                """
            )
        )


@app.get("/")
def root():
    return {"message": "Parent Safety Backend is running"}




def _get_firebase_app():
    if firebase_admin is None:
        logger.error("FCM send skipped: firebase-admin is not installed")
        return None
    try:
        if firebase_admin._apps:
            return firebase_admin.get_app()

        credentials_path = os.getenv("FIREBASE_CREDENTIALS_PATH")
        if not credentials_path:
            default_credentials_path = Path(__file__).with_name("firebase-service-account.json")
            if default_credentials_path.exists():
                credentials_path = str(default_credentials_path)
            else:
                admin_sdk_files = sorted(Path(__file__).parent.glob("*firebase-adminsdk*.json"))
                if len(admin_sdk_files) == 1:
                    credentials_path = str(admin_sdk_files[0])

        if credentials_path:
            logger.info("initializing Firebase Admin with credentials file %s", credentials_path)
            return firebase_admin.initialize_app(credentials.Certificate(credentials_path))

        logger.error(
            "FCM initialization skipped: set FIREBASE_CREDENTIALS_PATH or place firebase-service-account.json next to main.py"
        )
        return None
    except Exception as exc:
        logger.error("FCM initialization failed: %s", exc, exc_info=True)
        return None


def _send_fcm_notification(token: str, title: str, body: str) -> bool:
    app = _get_firebase_app()
    if app is None:
        return False
    try:
        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            token=token,
        )
        messaging.send(message, app=app)
        return True
    except Exception as exc:
        logger.error("FCM send failed: %s", exc, exc_info=True)
        return False


def _send_fcm_data_notification(token: str, data: dict[str, str]) -> bool:
    app = _get_firebase_app()
    if app is None:
        return False
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in data.items()):
        logger.error("FCM data send rejected: all keys and values must be strings")
        return False
    try:
        message = messaging.Message(
            data=data,
            token=token,
            android=messaging.AndroidConfig(priority="high"),
        )
        messaging.send(message, app=app)
        return True
    except Exception as exc:
        logger.error("FCM data send failed: %s", exc, exc_info=True)
        return False


def _now() -> datetime:
    """Return a naive UTC datetime for consistent SQLite storage."""
    return datetime.now(ZoneInfo("UTC")).replace(tzinfo=None)


def _local_now() -> datetime:
    return datetime.now(APP_TIMEZONE)


def _as_local(value: datetime) -> datetime:
    return value.replace(tzinfo=ZoneInfo("UTC")).astimezone(APP_TIMEZONE)


def _normalize_input_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=APP_TIMEZONE)
    return (
        value.astimezone(ZoneInfo("UTC"))
        .replace(tzinfo=None)
        .isoformat(timespec="seconds")
    )


def _hash_api_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_api_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, _hash_api_token(token)


def _user_from_credentials(
    db: Session,
    credentials: HTTPAuthorizationCredentials | None,
) -> models.User | None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        return None
    return (
        db.query(models.User)
        .filter(models.User.api_token_hash == _hash_api_token(credentials.credentials))
        .first()
    )


def _token_hash_from_credentials(
    credentials: HTTPAuthorizationCredentials | None,
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="valid bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _hash_api_token(credentials.credentials)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    user = _user_from_credentials(db, credentials)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="valid bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def _authorize_user(current_user: models.User, user_id: int, role: str) -> None:
    if current_user.id != user_id or current_user.role != role:
        raise HTTPException(status_code=403, detail="not authorized for this user")


def _format_chinese_datetime(value: datetime | None) -> str:
    if value is None:
        return "暂无"
    local_value = _as_local(value)
    time_label = local_value.strftime("%H:%M")
    if local_value.date() == _local_now().date():
        return f"今天 {time_label}"
    return f"{local_value.month}月{local_value.day}日 {time_label}"


def _is_offline_alert_exceeded(link: models.FamilyLink, device_status: models.DeviceStatus | None) -> bool:
    if not link.offline_alert_enabled or device_status is None or device_status.last_online_time is None:
        return False
    return _now() - device_status.last_online_time > timedelta(hours=link.offline_alert_hours)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return (
                parsed.astimezone(ZoneInfo("UTC"))
                .replace(tzinfo=None)
            )
        return parsed
    except ValueError:
        logger.warning("invalid datetime value=%s", value)
        return None


def _parse_time_minutes(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%H:%M")
        return parsed.hour * 60 + parsed.minute
    except ValueError:
        logger.warning("invalid time value=%s", value)
        return None


def _is_within_quiet_hours(now: datetime, start_time: str | None, end_time: str | None) -> bool:
    start_minutes = _parse_time_minutes(start_time)
    end_minutes = _parse_time_minutes(end_time)
    if start_minutes is None or end_minutes is None:
        return False

    current_minutes = now.hour * 60 + now.minute
    if start_minutes <= end_minutes:
        return start_minutes <= current_minutes <= end_minutes
    return current_minutes >= start_minutes or current_minutes <= end_minutes


def check_offline_alerts(db: Session) -> int:
    links = db.query(models.FamilyLink).filter(models.FamilyLink.offline_alert_enabled.is_(True)).all()
    sent_count = 0
    now = _now()
    for link in links:
        device_status = (
            db.query(models.DeviceStatus)
            .filter(models.DeviceStatus.user_id == link.elder_user_id)
            .first()
        )
        if not _is_offline_alert_exceeded(link, device_status):
            continue

        pause_until = _parse_datetime(link.offline_pause_until)
        if pause_until is not None and now < pause_until:
            continue

        if link.offline_quiet_hours_enabled and _is_within_quiet_hours(
            _local_now(),
            link.offline_quiet_start_time,
            link.offline_quiet_end_time,
        ):
            continue

        last_sent_at = _parse_datetime(link.last_offline_alert_sent_at)
        if last_sent_at is not None and now - last_sent_at < timedelta(hours=12):
            continue

        child = db.query(models.User).filter(models.User.id == link.child_user_id).first()
        if child is None or not child.fcm_token:
            logger.info("offline alert skipped child_user_id=%s: no token", link.child_user_id)
            continue

        title = "手机长时间未在线"
        body = "可能是手机没电、关机或没有网络，请联系确认。"
        if _send_fcm_notification(child.fcm_token, title, body):
            link.last_offline_alert_sent_at = now.isoformat(timespec="seconds")
            sent_count += 1
        else:
            logger.error("offline alert FCM send failed child_user_id=%s elder_user_id=%s", link.child_user_id, link.elder_user_id)

    if sent_count:
        db.commit()
    return sent_count


def _run_offline_alert_check() -> None:
    db = SessionLocal()
    try:
        sent_count = check_offline_alerts(db)
        logger.info("offline alert check complete sent_count=%s", sent_count)
    except Exception:
        db.rollback()
        logger.exception("offline alert check failed")
    finally:
        db.close()


async def _offline_alert_loop() -> None:
    while True:
        await asyncio.sleep(OFFLINE_ALERT_INTERVAL_SECONDS)
        await asyncio.to_thread(_run_offline_alert_check)


def _get_user_or_404(db: Session, user_id: int, role: str | None = None) -> models.User:
    query = db.query(models.User).filter(models.User.id == user_id)
    if role is not None:
        query = query.filter(models.User.role == role)

    user = query.first()
    if user is None:
        detail = f"{role or 'user'} not found"
        raise HTTPException(status_code=404, detail=detail)
    return user


@app.get("/api/health", response_model=schemas.HealthResponse)
def health():
    return {"status": "ok"}


@app.post("/api/register", response_model=schemas.RegisterResponse)
def register(
    payload: schemas.RegisterRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
):
    existing_user = (
        db.query(models.User)
        .filter(
            models.User.device_id == payload.device_id,
            models.User.role == payload.role,
        )
        .first()
    )
    if existing_user is not None:
        if existing_user.api_token_hash is None:
            raise HTTPException(
                status_code=403,
                detail="legacy account requires administrator token enrollment",
            )

        authenticated_user = _user_from_credentials(db, credentials)
        if authenticated_user is None or authenticated_user.id != existing_user.id:
            raise HTTPException(
                status_code=401,
                detail="this device is already registered; its bearer token is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return {
            "user_id": existing_user.id,
            "role": existing_user.role,
            "api_token": credentials.credentials,
        }

    api_token, api_token_hash = _new_api_token()
    user = models.User(
        role=payload.role,
        name=payload.name,
        phone=payload.phone,
        device_id=payload.device_id,
        api_token_hash=api_token_hash,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="device registration conflicted; retry with its bearer token",
        ) from exc
    db.refresh(user)
    return {"user_id": user.id, "role": user.role, "api_token": api_token}




@app.post("/api/user/update-fcm-token", response_model=schemas.SuccessResponse)
def update_fcm_token(
    payload: schemas.UpdateFcmTokenRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.id != payload.user_id:
        raise HTTPException(status_code=403, detail="not authorized for this user")
    current_user.fcm_token = payload.fcm_token
    db.commit()
    return {"success": True}


@app.post("/api/user/unbind", response_model=schemas.SuccessResponse)
def unbind_family_link(
    payload: schemas.UnbindRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    family_link = (
        db.query(models.FamilyLink)
        .filter(models.FamilyLink.id == payload.family_link_id)
        .first()
    )
    if family_link is None:
        return {"success": True}
    if current_user.id not in {
        family_link.elder_user_id,
        family_link.child_user_id,
    }:
        raise HTTPException(status_code=403, detail="not authorized for this family link")

    db.delete(family_link)
    db.commit()
    return {"success": True}


@app.post("/api/user/delete-data", response_model=schemas.SuccessResponse)
def delete_user_data(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
):
    token_hash = _token_hash_from_credentials(credentials)
    current_user = (
        db.query(models.User)
        .filter(models.User.api_token_hash == token_hash)
        .first()
    )
    if current_user is None:
        already_deleted = (
            db.query(models.DeletedApiToken)
            .filter(models.DeletedApiToken.token_hash == token_hash)
            .first()
        )
        if already_deleted is not None:
            return {"success": True}
        raise HTTPException(
            status_code=401,
            detail="valid bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = current_user.id
    try:
        db.add(models.DeletedApiToken(token_hash=token_hash))
        (
            db.query(models.BindAttempt)
            .filter(models.BindAttempt.child_user_id == user_id)
            .delete(synchronize_session=False)
        )
        (
            db.query(models.FamilyLink)
            .filter(
                (models.FamilyLink.elder_user_id == user_id)
                | (models.FamilyLink.child_user_id == user_id)
            )
            .delete(synchronize_session=False)
        )
        (
            db.query(models.BindCode)
            .filter(models.BindCode.elder_user_id == user_id)
            .delete(synchronize_session=False)
        )
        (
            db.query(models.DailyCheckin)
            .filter(models.DailyCheckin.elder_user_id == user_id)
            .delete(synchronize_session=False)
        )
        (
            db.query(models.DeviceStatus)
            .filter(models.DeviceStatus.user_id == user_id)
            .delete(synchronize_session=False)
        )
        (
            db.query(models.HelpRequest)
            .filter(models.HelpRequest.elder_user_id == user_id)
            .delete(synchronize_session=False)
        )
        db.delete(current_user)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("account deletion failed user_id=%s", user_id)
        raise HTTPException(status_code=500, detail="account deletion failed")

    return {"success": True}


@app.post("/api/elder/create-bind-code", response_model=schemas.CreateBindCodeResponse)
def create_bind_code(
    payload: schemas.CreateBindCodeRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _authorize_user(current_user, payload.elder_user_id, "elder")
    _get_user_or_404(db, payload.elder_user_id, role="elder")

    (
        db.query(models.BindCode)
        .filter(
            models.BindCode.elder_user_id == payload.elder_user_id,
            models.BindCode.used.is_(False),
        )
        .update({"used": True}, synchronize_session=False)
    )

    code = ""
    bind_code = None
    for _ in range(20):
        candidate = f"{secrets.randbelow(1_000_000):06d}"
        try:
            with db.begin_nested():
                candidate_bind_code = models.BindCode(
                    elder_user_id=payload.elder_user_id,
                    code=candidate,
                    expires_at=_now() + timedelta(minutes=30),
                    used=False,
                )
                db.add(candidate_bind_code)
                db.flush()
            code = candidate
            bind_code = candidate_bind_code
            break
        except IntegrityError:
            logger.info(
                "bind code collision elder_user_id=%s; retrying",
                payload.elder_user_id,
            )
    if bind_code is None:
        db.rollback()
        raise HTTPException(status_code=503, detail="could not allocate bind code")
    db.commit()
    return {"code": code, "expires_in_minutes": 10}


@app.post("/api/child/bind-elder", response_model=schemas.BindElderResponse)
def bind_elder(
    payload: schemas.BindElderRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _authorize_user(current_user, payload.child_user_id, "child")
    logger.info("bind_elder request child_user_id=%s", payload.child_user_id)
    _get_user_or_404(db, payload.child_user_id, role="child")

    attempt_window_start = _now() - timedelta(minutes=15)
    (
        db.query(models.BindAttempt)
        .filter(
            models.BindAttempt.attempted_at < attempt_window_start,
        )
        .delete(synchronize_session=False)
    )
    recent_attempts = (
        db.query(models.BindAttempt)
        .filter(
            models.BindAttempt.child_user_id == payload.child_user_id,
            models.BindAttempt.attempted_at >= attempt_window_start,
        )
        .count()
    )
    if recent_attempts >= 5:
        db.commit()
        raise HTTPException(
            status_code=429,
            detail="too many bind attempts; try again later",
            headers={"Retry-After": "900"},
        )
    db.add(
        models.BindAttempt(
            child_user_id=payload.child_user_id,
            attempted_at=_now(),
        )
    )
    db.commit()

    bind_code = (
        db.query(models.BindCode)
        .filter(
            models.BindCode.code == payload.code,
            models.BindCode.used.is_(False),
            models.BindCode.expires_at >= _now(),
        )
        .order_by(models.BindCode.created_at.desc())
        .first()
    )
    if bind_code is None:
        logger.warning("bind_elder failed child_user_id=%s", payload.child_user_id)
        raise HTTPException(status_code=400, detail="invalid or expired code")

    consumed = (
        db.query(models.BindCode)
        .filter(
            models.BindCode.id == bind_code.id,
            models.BindCode.used.is_(False),
        )
        .update({"used": True}, synchronize_session=False)
    )
    if consumed != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="code was already consumed")

    family_link = models.FamilyLink(
        elder_user_id=bind_code.elder_user_id,
        child_user_id=payload.child_user_id,
        elder_relationship=payload.elder_relationship,
        elder_name=payload.elder_name,
        elder_phone=payload.elder_phone,
    )
    db.add(family_link)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="family link already exists") from exc
    logger.info("bind_elder success child_user_id=%s elder_user_id=%s family_link_id=%s", payload.child_user_id, bind_code.elder_user_id, family_link.id)

    return {
        "success": True,
        "family_link_id": family_link.id,
        "elder_user_id": bind_code.elder_user_id,
        "elder_relationship": payload.elder_relationship,
        "elder_name": payload.elder_name,
    }


@app.post("/api/elder/checkin", response_model=schemas.ElderCheckinResponse)
def elder_checkin(
    payload: schemas.ElderCheckinRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _authorize_user(current_user, payload.elder_user_id, "elder")
    _get_user_or_404(db, payload.elder_user_id, role="elder")

    now = _now()
    local_now = _local_now()
    checkin_time = local_now.strftime("%H:%M")
    checkin = models.DailyCheckin(
        elder_user_id=payload.elder_user_id,
        checkin_date=local_now.date(),
        checkin_time=checkin_time,
        battery_level=payload.battery_level,
        created_at=now,
    )
    db.add(checkin)

    device_status = (
        db.query(models.DeviceStatus)
        .filter(models.DeviceStatus.user_id == payload.elder_user_id)
        .first()
    )
    if device_status is None:
        device_status = models.DeviceStatus(user_id=payload.elder_user_id)
        db.add(device_status)

    device_status.battery_level = payload.battery_level
    device_status.last_online_time = now
    device_status.updated_at = now

    resolved_help_requests = (
        db.query(models.HelpRequest)
        .filter(
            models.HelpRequest.elder_user_id == payload.elder_user_id,
            models.HelpRequest.status == "pending",
        )
        .all()
    )
    for help_request in resolved_help_requests:
        help_request.status = "resolved"

    db.commit()

    links = (
        db.query(models.FamilyLink)
        .filter(models.FamilyLink.elder_user_id == payload.elder_user_id)
        .all()
    )
    for link in links:
        child = db.query(models.User).filter(models.User.id == link.child_user_id).first()
        if child is None or not child.fcm_token:
            continue
        data = {
            "event_type": "checkin",
            "child_user_id": str(link.child_user_id),
            "elder_user_id": str(payload.elder_user_id),
            "checkin_time": checkin_time,
        }
        if _send_fcm_data_notification(child.fcm_token, data):
            logger.info("checkin FCM send success child_user_id=%s", link.child_user_id)
        else:
            logger.error("checkin FCM send error child_user_id=%s", link.child_user_id)

    logger.info(
        "checkin elder_user_id=%s resolved_help_requests=%s",
        payload.elder_user_id,
        len(resolved_help_requests),
    )
    return {"success": True, "message": "今日已确认平安", "checkin_time": checkin_time}


@app.get(
    "/api/elder/{elder_user_id}/checkins",
    response_model=list[schemas.ElderCheckinRecordResponse],
)
def elder_checkins(
    elder_user_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _get_user_or_404(db, elder_user_id, role="elder")
    if current_user.role == "elder":
        _authorize_user(current_user, elder_user_id, "elder")
    elif current_user.role == "child":
        linked = (
            db.query(models.FamilyLink)
            .filter(
                models.FamilyLink.child_user_id == current_user.id,
                models.FamilyLink.elder_user_id == elder_user_id,
            )
            .first()
        )
        if linked is None:
            raise HTTPException(status_code=403, detail="family link required")
    else:
        raise HTTPException(status_code=403, detail="role is not authorized")

    checkins = (
        db.query(models.DailyCheckin)
        .filter(models.DailyCheckin.elder_user_id == elder_user_id)
        .order_by(models.DailyCheckin.created_at.desc(), models.DailyCheckin.id.desc())
        .limit(5)
        .all()
    )
    help_requests = (
        db.query(models.HelpRequest)
        .filter(models.HelpRequest.elder_user_id == elder_user_id)
        .order_by(models.HelpRequest.created_at.desc(), models.HelpRequest.id.desc())
        .limit(5)
        .all()
    )

    records = [
        (
            record.created_at,
            record.id,
            {
                "event_type": "checkin",
                "record_date": record.checkin_date.isoformat(),
                "record_time": record.checkin_time,
                "message": "I'm fine",
                "battery_level": record.battery_level,
            },
        )
        for record in checkins
    ]
    for record in help_requests:
        local_created_at = _as_local(record.created_at)
        records.append(
            (
                record.created_at,
                record.id,
                {
                    "event_type": "help_request",
                    "record_date": local_created_at.date().isoformat(),
                    "record_time": local_created_at.strftime("%H:%M"),
                    "message": record.message,
                    "battery_level": None,
                },
            )
        )

    records.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [record for _, _, record in records[:5]]


@app.post("/api/elder/heartbeat", response_model=schemas.SuccessResponse)
def elder_heartbeat(
    payload: schemas.ElderHeartbeatRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _authorize_user(current_user, payload.elder_user_id, "elder")
    _get_user_or_404(db, payload.elder_user_id, role="elder")
    now = _now()
    device_status = (
        db.query(models.DeviceStatus)
        .filter(models.DeviceStatus.user_id == payload.elder_user_id)
        .first()
    )
    if device_status is None:
        device_status = models.DeviceStatus(user_id=payload.elder_user_id)
        db.add(device_status)
    device_status.battery_level = payload.battery_level
    device_status.last_online_time = now
    device_status.updated_at = now
    db.commit()
    return {"success": True}


@app.post("/api/elder/help-request", response_model=schemas.HelpRequestResponse)
def create_help_request(
    payload: schemas.HelpRequestCreate,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _authorize_user(current_user, payload.elder_user_id, "elder")
    _get_user_or_404(db, payload.elder_user_id, role="elder")

    message = payload.message or {
        "call_back": "请给我回电话",
        "not_feeling_well": "我身体不舒服",
        "errand": "我需要买东西/办事",
        "other": "其他帮助",
    }.get(payload.type, "其他帮助")
    logger.info(
        "help_request elder_user_id=%s type=%s",
        payload.elder_user_id,
        payload.type,
    )

    help_request = models.HelpRequest(
        elder_user_id=payload.elder_user_id,
        type=payload.type,
        message=message,
        status="pending",
        created_at=_now(),
    )
    db.add(help_request)
    db.commit()

    links = (
        db.query(models.FamilyLink)
        .filter(models.FamilyLink.elder_user_id == payload.elder_user_id)
        .all()
    )
    linked_child_ids = [link.child_user_id for link in links]
    logger.info("help_request notify elder_user_id=%s linked_child_user_ids=%s", payload.elder_user_id, linked_child_ids)

    children_with_fcm_token = 0
    notified_children = 0
    for link in links:
        child = db.query(models.User).filter(models.User.id == link.child_user_id).first()
        has_token = bool(child and child.fcm_token)
        logger.info("help_request child_user_id=%s has_fcm_token=%s", link.child_user_id, has_token)
        if not has_token:
            continue
        children_with_fcm_token += 1
        title = f"{link.elder_relationship} {link.elder_name} 需要帮助"
        data = {
            "event_type": "help_request",
            "child_user_id": str(link.child_user_id),
            "elder_user_id": str(payload.elder_user_id),
            "title": title,
            "body": message,
        }
        if _send_fcm_data_notification(child.fcm_token, data):
            notified_children += 1
            logger.info("help_request FCM send success child_user_id=%s", link.child_user_id)
        else:
            logger.error("help_request FCM send error child_user_id=%s", link.child_user_id)

    return {
        "success": True,
        "message": "已通知孩子",
        "linked_children_count": len(links),
        "children_with_fcm_token": children_with_fcm_token,
        "notified_children": notified_children,
    }




@app.post("/api/child/update-alert-settings", response_model=schemas.SuccessResponse)
def update_alert_settings(
    payload: schemas.UpdateAlertSettingsRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _authorize_user(current_user, payload.child_user_id, "child")
    _get_user_or_404(db, payload.child_user_id, role="child")
    _get_user_or_404(db, payload.elder_user_id, role="elder")
    link = (
        db.query(models.FamilyLink)
        .filter(
            models.FamilyLink.child_user_id == payload.child_user_id,
            models.FamilyLink.elder_user_id == payload.elder_user_id,
        )
        .order_by(models.FamilyLink.created_at.desc())
        .first()
    )
    if link is None:
        raise HTTPException(status_code=404, detail="family link not found")
    link.offline_alert_enabled = payload.offline_alert_enabled
    link.offline_alert_hours = payload.offline_alert_hours
    db.commit()
    return {"success": True}


@app.post("/api/child/update-offline-alert-settings", response_model=schemas.SuccessResponse)
def update_offline_alert_settings(
    payload: schemas.UpdateOfflineAlertSettingsRequest,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _authorize_user(current_user, payload.child_user_id, "child")
    _get_user_or_404(db, payload.child_user_id, role="child")
    _get_user_or_404(db, payload.elder_user_id, role="elder")
    link = (
        db.query(models.FamilyLink)
        .filter(
            models.FamilyLink.child_user_id == payload.child_user_id,
            models.FamilyLink.elder_user_id == payload.elder_user_id,
        )
        .order_by(models.FamilyLink.created_at.desc())
        .first()
    )
    if link is None:
        raise HTTPException(status_code=404, detail="family link not found")

    link.offline_alert_enabled = payload.offline_alert_enabled
    link.offline_alert_hours = payload.offline_alert_hours
    link.offline_quiet_hours_enabled = payload.quiet_hours_enabled
    link.offline_quiet_start_time = payload.quiet_start_time.strftime("%H:%M")
    link.offline_quiet_end_time = payload.quiet_end_time.strftime("%H:%M")
    link.offline_pause_until = _normalize_input_datetime(payload.offline_pause_until)
    db.commit()
    return {"success": True}


@app.get(
    "/api/child/elder-status/{child_user_id}",
    response_model=list[schemas.ElderStatusResponse],
)
def get_elder_status(
    child_user_id: int,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _authorize_user(current_user, child_user_id, "child")
    _get_user_or_404(db, child_user_id, role="child")

    today = _local_now().date()
    links = (
        db.query(models.FamilyLink)
        .filter(models.FamilyLink.child_user_id == child_user_id)
        .order_by(models.FamilyLink.created_at.desc())
        .all()
    )

    response = []
    for link in links:
        today_checkin = (
            db.query(models.DailyCheckin)
            .filter(
                models.DailyCheckin.elder_user_id == link.elder_user_id,
                models.DailyCheckin.checkin_date == today,
            )
            .order_by(models.DailyCheckin.created_at.desc())
            .first()
        )
        device_status = (
            db.query(models.DeviceStatus)
            .filter(models.DeviceStatus.user_id == link.elder_user_id)
            .first()
        )
        pending_help_request = (
            db.query(models.HelpRequest)
            .filter(
                models.HelpRequest.elder_user_id == link.elder_user_id,
                models.HelpRequest.status == "pending",
            )
            .order_by(models.HelpRequest.created_at.desc(), models.HelpRequest.id.desc())
            .first()
        )

        response.append(
            {
                "family_link_id": link.id,
                "elder_user_id": link.elder_user_id,
                "elder_relationship": link.elder_relationship,
                "elder_name": link.elder_name,
                "elder_phone": link.elder_phone,
                "today_status": "normal" if today_checkin else "need_confirm",
                "today_checkin_time": today_checkin.checkin_time if today_checkin else None,
                "battery_level": device_status.battery_level if device_status else None,
                "last_online_time": (
                    _as_local(device_status.last_online_time).strftime("%H:%M")
                    if device_status and device_status.last_online_time
                    else None
                ),
                "pending_help_request": (
                    {
                        "type": pending_help_request.type,
                        "message": pending_help_request.message,
                        "created_at": pending_help_request.created_at,
                    }
                    if pending_help_request
                    else None
                ),
                "offline_alert_enabled": link.offline_alert_enabled,
                "offline_alert_hours": link.offline_alert_hours,
                "offline_quiet_hours_enabled": link.offline_quiet_hours_enabled,
                "offline_quiet_start_time": link.offline_quiet_start_time,
                "offline_quiet_end_time": link.offline_quiet_end_time,
                "offline_pause_until": link.offline_pause_until,
                "offline_alert_exceeded": _is_offline_alert_exceeded(link, device_status),
            }
        )

    return response
