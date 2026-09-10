"""One logical notification pipeline for FCM/HMS recipients.

The durable delivery ledger makes event emission idempotent. Provider adapters
can consume queued rows without changing event/state logic; no provider
credentials or network delivery are required by the local V2 test phase.
"""

from datetime import datetime

from sqlalchemy.orm import Session

import models
import v2_models


SUPPORTED_LOCALES = {"en", "pt", "pt-BR", "es", "ar", "zh-CN", "zh-TW", "fr", "de", "hi", "id", "it", "ja", "ko", "ru", "tr"}


def normalize_locale(value: str | None) -> str:
    candidate = (value or "en").replace("_", "-")
    if candidate in SUPPORTED_LOCALES:
        return candidate
    language = candidate.split("-", 1)[0]
    return language if language in SUPPORTED_LOCALES else "en"


def localized_message(event_type: str, locale: str, parent_name: str) -> tuple[str, str]:
    locale = normalize_locale(locale)
    messages = {
        "checkin_confirmed": {
            "en": ("Check-in confirmed", f"{parent_name} checked in."),
            "pt": ("Check-in confirmado", f"{parent_name} confirmou que está bem."),
            "pt-BR": ("Check-in confirmado", f"{parent_name} confirmou que está bem."),
            "es": ("Check-in confirmado", f"{parent_name} confirmó que está bien."),
            "ar": ("تم تأكيد الاطمئنان", f"أكد {parent_name} أنه بخير."),
            "zh-CN": ("已确认平安", f"{parent_name} 已完成报平安。"),
            "zh-TW": ("已確認平安", f"{parent_name} 已完成報平安。"),
        },
        "missed_checkin": {
            "en": ("Check-in missed", f"{parent_name} has not checked in yet."),
            "pt": ("Check-in não realizado", f"{parent_name} ainda não confirmou que está bem."),
            "pt-BR": ("Check-in não realizado", f"{parent_name} ainda não confirmou que está bem."),
            "es": ("Check-in no realizado", f"{parent_name} aún no ha confirmado que está bien."),
            "ar": ("لم يتم تسجيل الاطمئنان", f"لم يؤكد {parent_name} أنه بخير بعد."),
            "zh-CN": ("尚未报平安", f"{parent_name} 还没有完成报平安。"),
            "zh-TW": ("尚未報平安", f"{parent_name} 還沒有完成報平安。"),
        },
        "help_requested": {
            "en": ("Help requested", f"{parent_name} is asking for help."),
            "pt": ("Ajuda solicitada", f"{parent_name} está pedindo ajuda."),
            "pt-BR": ("Ajuda solicitada", f"{parent_name} está pedindo ajuda."),
            "es": ("Ayuda solicitada", f"{parent_name} está pidiendo ayuda."),
            "ar": ("تم طلب المساعدة", f"يطلب {parent_name} المساعدة."),
            "zh-CN": ("请求帮助", f"{parent_name} 正在请求帮助。"),
            "zh-TW": ("請求協助", f"{parent_name} 正在請求協助。"),
        },
        "reminder": {
            "en": ("Check-in reminder", f"Please remind {parent_name} to check in."),
            "pt": ("Lembrete de check-in", f"Lembre {parent_name} de confirmar que está bem."),
            "pt-BR": ("Lembrete de check-in", f"Lembre {parent_name} de confirmar que está bem."),
            "es": ("Recordatorio de check-in", f"Recuerda a {parent_name} confirmar que está bien."),
            "ar": ("تذكير بالاطمئنان", f"يرجى تذكير {parent_name} بتسجيل الاطمئنان."),
            "zh-CN": ("报平安提醒", f"请提醒 {parent_name} 完成报平安。"),
            "zh-TW": ("報平安提醒", f"請提醒 {parent_name} 完成報平安。"),
        },
    }
    return messages.get(event_type, messages["reminder"]).get(locale, messages[event_type if event_type in messages else "reminder"]["en"])


def queue_circle_notifications(db: Session, circle_id: int, event_key: str, event_type: str, parent_name: str, exclude_user_id: int | None = None) -> int:
    memberships = (
        db.query(v2_models.FamilyMembership)
        .filter(v2_models.FamilyMembership.family_circle_id == circle_id, v2_models.FamilyMembership.membership_status == "active")
        .all()
    )
    queued = 0
    for membership in memberships:
        user = db.get(models.User, membership.user_id)
        if user is None or user.id == exclude_user_id or membership.role == "PARENT":
            continue
        locale = normalize_locale(user.locale_tag)
        title, body = localized_message(event_type, locale, parent_name)
        token = (
            db.query(models.DevicePushToken)
            .filter(models.DevicePushToken.user_id == user.id, models.DevicePushToken.push_token_invalidated_at.is_(None))
            .order_by(models.DevicePushToken.push_token_updated_at.desc())
            .first()
        )
        delivery = v2_models.V2NotificationDelivery(
            event_key=event_key,
            recipient_user_id=user.id,
            locale_tag=locale,
            provider=token.push_provider if token else None,
            title=title,
            body=body,
            status="queued" if token else "no_device_token",
            attempts=0,
        )
        db.add(delivery)
        queued += 1
    return queued


def queue_recipient_notification(db: Session, event_key: str, recipient_user_id: int, event_type: str, parent_name: str) -> bool:
    user = db.get(models.User, recipient_user_id)
    if user is None:
        return False
    locale = normalize_locale(user.locale_tag)
    title, body = localized_message(event_type, locale, parent_name)
    token = (
        db.query(models.DevicePushToken)
        .filter(models.DevicePushToken.user_id == user.id, models.DevicePushToken.push_token_invalidated_at.is_(None))
        .order_by(models.DevicePushToken.push_token_updated_at.desc())
        .first()
    )
    if db.query(v2_models.V2NotificationDelivery).filter(v2_models.V2NotificationDelivery.event_key == event_key, v2_models.V2NotificationDelivery.recipient_user_id == user.id).first() is not None:
        return False
    db.add(v2_models.V2NotificationDelivery(
        event_key=event_key,
        recipient_user_id=user.id,
        locale_tag=locale,
        provider=token.push_provider if token else None,
        title=title,
        body=body,
        status="queued" if token else "no_device_token",
        attempts=0,
    ))
    return True


def dispatch_queued_notifications(db: Session) -> int:
    """Drain queued rows through the existing FCM/HMS provider adapters.

    Imported lazily to avoid a module cycle during FastAPI startup. The V2
    state machine remains provider-independent and rows stay auditable when
    credentials or a provider are unavailable.
    """
    from main import _send_push_notification

    delivered = 0
    rows = db.query(v2_models.V2NotificationDelivery).filter(v2_models.V2NotificationDelivery.status == "queued").order_by(v2_models.V2NotificationDelivery.id.asc()).limit(100).all()
    for delivery in rows:
        user = db.get(models.User, delivery.recipient_user_id)
        if user is None:
            delivery.status = "invalid_recipient"
            continue
        delivery.attempts += 1
        event_type = delivery.event_key.split(":", 1)[0]
        if _send_push_notification(db, user, delivery.title, delivery.body, event_type=event_type):
            delivery.status = "delivered"
            delivery.delivered_at = datetime.utcnow()
            delivered += 1
        else:
            delivery.status = "retryable_failure"
    db.commit()
    return delivered
