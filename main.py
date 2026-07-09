from datetime import datetime, timedelta
import random
import logging
import os
from pathlib import Path

try:
    import firebase_admin
    from firebase_admin import credentials, messaging
except ImportError:  # Firebase is optional until push credentials are configured.
    firebase_admin = None
    credentials = None
    messaging = None

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

import models
import schemas
from database import Base, engine, get_db


app = FastAPI(title="爸妈平安 Backend")
logger = logging.getLogger("parent_safety")
logging.basicConfig(level=logging.INFO)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    logger.warning(
        "validation failed path=%s errors=%s body=%s",
        request.url.path,
        exc.errors(),
        body.decode("utf-8", errors="replace"),
    )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_migrations()


def _ensure_sqlite_migrations():
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        columns = connection.execute(sql_text("PRAGMA table_info(users)")).fetchall()
        column_names = {column[1] for column in columns}
        if "fcm_token" not in column_names:
            connection.execute(sql_text("ALTER TABLE users ADD COLUMN fcm_token VARCHAR"))
            logger.info("migration added users.fcm_token column")

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


def _now() -> datetime:
    return datetime.now()


def _format_chinese_datetime(value: datetime | None) -> str:
    if value is None:
        return "暂无"
    time_label = value.strftime("%H:%M")
    if value.date() == _now().date():
        return f"今天 {time_label}"
    return f"{value.month}月{value.day}日 {time_label}"


def _is_offline_alert_exceeded(link: models.FamilyLink, device_status: models.DeviceStatus | None) -> bool:
    if not link.offline_alert_enabled or device_status is None or device_status.last_online_time is None:
        return False
    return _now() - device_status.last_online_time > timedelta(hours=link.offline_alert_hours)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
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
            now,
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
def register(payload: schemas.RegisterRequest, db: Session = Depends(get_db)):
    existing_user = (
        db.query(models.User)
        .filter(
            models.User.device_id == payload.device_id,
            models.User.role == payload.role,
        )
        .first()
    )
    if existing_user is not None:
        return {"user_id": existing_user.id, "role": existing_user.role}

    user = models.User(
        role=payload.role,
        name=payload.name,
        phone=payload.phone,
        device_id=payload.device_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"user_id": user.id, "role": user.role}




@app.post("/api/user/update-fcm-token", response_model=schemas.SuccessResponse)
def update_fcm_token(payload: schemas.UpdateFcmTokenRequest, db: Session = Depends(get_db)):
    user = _get_user_or_404(db, payload.user_id)
    user.fcm_token = payload.fcm_token
    db.commit()
    return {"success": True}


@app.post("/api/elder/create-bind-code", response_model=schemas.CreateBindCodeResponse)
def create_bind_code(payload: schemas.CreateBindCodeRequest, db: Session = Depends(get_db)):
    _get_user_or_404(db, payload.elder_user_id, role="elder")

    (
        db.query(models.BindCode)
        .filter(
            models.BindCode.elder_user_id == payload.elder_user_id,
            models.BindCode.used.is_(False),
        )
        .update({"used": True}, synchronize_session=False)
    )

    code = f"{random.randint(0, 999999):06d}"
    bind_code = models.BindCode(
        elder_user_id=payload.elder_user_id,
        code=code,
        expires_at=_now() + timedelta(minutes=10),
        used=False,
    )
    db.add(bind_code)
    db.commit()
    return {"code": code, "expires_in_minutes": 10}


@app.post("/api/child/bind-elder", response_model=schemas.BindElderResponse)
def bind_elder(payload: schemas.BindElderRequest, db: Session = Depends(get_db)):
    logger.info("bind_elder request child_user_id=%s code=%s relationship=%s name=%s phone=%s", payload.child_user_id, payload.code, payload.elder_relationship, payload.elder_name, payload.elder_phone)
    _get_user_or_404(db, payload.child_user_id, role="child")

    bind_code = (
        db.query(models.BindCode)
        .filter(models.BindCode.code == payload.code)
        .order_by(models.BindCode.created_at.desc())
        .first()
    )
    if bind_code is None:
        logger.warning("bind_elder failed: code not found code=%s child_user_id=%s", payload.code, payload.child_user_id)
        raise HTTPException(status_code=404, detail="code not found")
    logger.info("bind_elder matched code id=%s elder_user_id=%s used=%s expires_at=%s now=%s", bind_code.id, bind_code.elder_user_id, bind_code.used, bind_code.expires_at, _now())
    if bind_code.used:
        logger.warning("bind_elder failed: code already used code=%s", payload.code)
        raise HTTPException(status_code=400, detail="code has already been used")
    if bind_code.expires_at < _now():
        logger.warning("bind_elder failed: code expired code=%s expires_at=%s now=%s", payload.code, bind_code.expires_at, _now())
        raise HTTPException(status_code=400, detail="code has expired")

    family_link = models.FamilyLink(
        elder_user_id=bind_code.elder_user_id,
        child_user_id=payload.child_user_id,
        elder_relationship=payload.elder_relationship,
        elder_name=payload.elder_name,
        elder_phone=payload.elder_phone,
    )
    bind_code.used = True
    db.add(family_link)
    db.commit()
    logger.info("bind_elder success child_user_id=%s elder_user_id=%s family_link_id=%s", payload.child_user_id, bind_code.elder_user_id, family_link.id)

    return {
        "success": True,
        "elder_user_id": bind_code.elder_user_id,
        "elder_relationship": payload.elder_relationship,
        "elder_name": payload.elder_name,
    }


@app.post("/api/elder/checkin", response_model=schemas.ElderCheckinResponse)
def elder_checkin(payload: schemas.ElderCheckinRequest, db: Session = Depends(get_db)):
    _get_user_or_404(db, payload.elder_user_id, role="elder")

    now = _now()
    checkin_time = now.strftime("%H:%M")
    checkin = models.DailyCheckin(
        elder_user_id=payload.elder_user_id,
        checkin_date=now.date(),
        checkin_time=checkin_time,
        battery_level=payload.battery_level,
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
        title = f"{link.elder_relationship} {link.elder_name} 已确认平安"
        body = f"今天 {checkin_time} 已确认：我很好"
        if _send_fcm_notification(child.fcm_token, title, body):
            logger.info("checkin FCM send success child_user_id=%s", link.child_user_id)
        else:
            logger.error("checkin FCM send error child_user_id=%s", link.child_user_id)

    logger.info(
        "checkin elder_user_id=%s resolved_help_requests=%s",
        payload.elder_user_id,
        len(resolved_help_requests),
    )
    return {"success": True, "message": "今日已确认平安", "checkin_time": checkin_time}




@app.post("/api/elder/heartbeat", response_model=schemas.SuccessResponse)
def elder_heartbeat(payload: schemas.ElderHeartbeatRequest, db: Session = Depends(get_db)):
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
def create_help_request(payload: schemas.HelpRequestCreate, db: Session = Depends(get_db)):
    _get_user_or_404(db, payload.elder_user_id, role="elder")

    message = payload.message or {
        "call_back": "请给我回电话",
        "not_feeling_well": "我身体不舒服",
        "errand": "我需要买东西/办事",
        "other": "其他帮助",
    }.get(payload.type, "其他帮助")
    logger.info("help_request elder_user_id=%s type=%s message=%s", payload.elder_user_id, payload.type, message)

    help_request = models.HelpRequest(
        elder_user_id=payload.elder_user_id,
        type=payload.type,
        message=message,
        status="pending",
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
        if _send_fcm_notification(child.fcm_token, title, message):
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
def update_alert_settings(payload: schemas.UpdateAlertSettingsRequest, db: Session = Depends(get_db)):
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
def update_offline_alert_settings(payload: schemas.UpdateOfflineAlertSettingsRequest, db: Session = Depends(get_db)):
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
    link.offline_quiet_start_time = payload.quiet_start_time
    link.offline_quiet_end_time = payload.quiet_end_time
    link.offline_pause_until = payload.offline_pause_until
    db.commit()
    return {"success": True}


@app.get(
    "/api/child/elder-status/{child_user_id}",
    response_model=list[schemas.ElderStatusResponse],
)
def get_elder_status(child_user_id: int, db: Session = Depends(get_db)):
    _get_user_or_404(db, child_user_id, role="child")

    today = _now().date()
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
                "elder_user_id": link.elder_user_id,
                "elder_relationship": link.elder_relationship,
                "elder_name": link.elder_name,
                "elder_phone": link.elder_phone,
                "today_status": "normal" if today_checkin else "need_confirm",
                "today_checkin_time": today_checkin.checkin_time if today_checkin else None,
                "battery_level": device_status.battery_level if device_status else None,
                "last_online_time": (
                    device_status.last_online_time.strftime("%H:%M")
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
