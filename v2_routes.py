import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
import v2_models
import v2_schemas as schemas
from database import get_db
from v2_auth import get_v2_current_user
from v2_notifications import normalize_locale, queue_circle_notifications
from v2_time import CheckInState, as_utc, checkin_result, evaluate_schedule, localize_utc, next_scheduled_window, utc_now


router = APIRouter(prefix="/api/v2", tags=["parent-check-in-v2"])


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _active_membership(db: Session, user_id: int, circle_id: int, roles: tuple[str, ...] | None = None) -> v2_models.FamilyMembership:
    query = db.query(v2_models.FamilyMembership).filter(
        v2_models.FamilyMembership.user_id == user_id,
        v2_models.FamilyMembership.family_circle_id == circle_id,
        v2_models.FamilyMembership.membership_status == "active",
    )
    if roles:
        query = query.filter(v2_models.FamilyMembership.role.in_(roles))
    membership = query.first()
    if membership is None:
        raise HTTPException(status_code=403, detail="not authorized for this family circle")
    return membership


def _circle(db: Session, circle_id: int) -> v2_models.FamilyCircle:
    circle = db.get(v2_models.FamilyCircle, circle_id)
    if circle is None or circle.status.upper() != "ACTIVE":
        raise HTTPException(status_code=404, detail="family circle not found")
    return circle


def _circle_any(db: Session, circle_id: int) -> v2_models.FamilyCircle:
    circle = db.get(v2_models.FamilyCircle, circle_id)
    if circle is None or circle.status.upper() == "DELETED":
        raise HTTPException(status_code=404, detail="family circle not found")
    return circle


def _parent_for_user(db: Session, user_id: int, circle_id: int | None = None) -> v2_models.ParentProfile:
    query = db.query(v2_models.ParentProfile).filter(
        v2_models.ParentProfile.user_id == user_id,
        v2_models.ParentProfile.active.is_(True),
    )
    if circle_id is not None:
        query = query.filter(v2_models.ParentProfile.family_circle_id == circle_id)
    profile = query.first()
    if profile is None:
        raise HTTPException(status_code=403, detail="authenticated user is not an active parent")
    return profile


def _circle_response(db: Session, circle: v2_models.FamilyCircle) -> dict:
    count = db.query(v2_models.FamilyMembership).filter(
        v2_models.FamilyMembership.family_circle_id == circle.id,
        v2_models.FamilyMembership.membership_status == "active",
    ).count()
    organizer = db.get(models.User, circle.organizer_user_id)
    return {"id": circle.id, "organizer_user_id": circle.organizer_user_id, "status": circle.status, "member_count": count, "organizer_name": organizer.name if organizer else ""}


@router.post("/register", response_model=schemas.V2RegisterResponse)
def register_v2(payload: schemas.V2RegisterRequest, db: Session = Depends(get_db)):
    role = "parent" if payload.role == "PARENT" else "family_member"
    existing = db.query(models.User).filter(models.User.device_id == payload.device_id, models.User.role == role).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="device is already registered for this role")
    raw_token = secrets.token_urlsafe(32)
    user = models.User(
        role=role,
        name=payload.name,
        phone=payload.phone,
        device_id=payload.device_id,
        api_token_hash=_hash(raw_token),
        locale_tag=normalize_locale(payload.locale_tag),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"user_id": user.id, "role": payload.role, "api_token": raw_token, "locale_tag": user.locale_tag}


@router.get("/users/me", response_model=schemas.V2CurrentUserResponse)
def current_user_v2(current_user: models.User = Depends(get_v2_current_user)):
    return {
        "user_id": current_user.id,
        "role": "PARENT" if current_user.role == "parent" else "FAMILY_MEMBER",
        "name": current_user.name,
        "locale_tag": normalize_locale(current_user.locale_tag),
    }


@router.post("/family-circles", response_model=schemas.CircleResponse)
def create_circle(payload: schemas.CircleCreateRequest, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    if current_user.role != "family_member":
        raise HTTPException(status_code=403, detail="only family members can organize a family circle")
    existing = db.query(v2_models.FamilyMembership).filter(
        v2_models.FamilyMembership.user_id == current_user.id,
        v2_models.FamilyMembership.role == "ORGANIZER",
        v2_models.FamilyMembership.membership_status == "active",
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="organizer already has an active family circle")
    circle = v2_models.FamilyCircle(organizer_user_id=current_user.id, status="PENDING_PURCHASE")
    db.add(circle)
    db.flush()
    db.add(v2_models.FamilyMembership(
        family_circle_id=circle.id,
        user_id=current_user.id,
        role="ORGANIZER",
        relationship="organizer",
        membership_status="active",
    ))
    db.commit()
    db.refresh(circle)
    return _circle_response(db, circle)


@router.get("/family-circles", response_model=list[schemas.CircleResponse])
def list_my_circles(current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    memberships = db.query(v2_models.FamilyMembership).filter(
        v2_models.FamilyMembership.user_id == current_user.id,
        v2_models.FamilyMembership.membership_status == "active",
    ).all()
    circles = []
    for membership in memberships:
        circle = db.get(v2_models.FamilyCircle, membership.family_circle_id)
        if circle is not None and circle.status.upper() != "DELETED":
            circles.append(_circle_response(db, circle))
    return circles


@router.get("/family-circles/{circle_id}", response_model=schemas.CircleResponse)
def get_circle(circle_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    circle = _circle_any(db, circle_id)
    _active_membership(db, current_user.id, circle_id)
    return _circle_response(db, circle)


@router.get("/family-circles/{circle_id}/members", response_model=list[schemas.MemberResponse])
def list_members(circle_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    _circle(db, circle_id)
    _active_membership(db, current_user.id, circle_id)
    members = db.query(v2_models.FamilyMembership).filter(v2_models.FamilyMembership.family_circle_id == circle_id).order_by(v2_models.FamilyMembership.id.asc()).all()
    return [
        {
            "membership_id": member.id,
            "user_id": member.user_id,
            "display_name": member.user.name if member.user else "",
            "role": member.role,
            "relationship": member.relationship,
            "phone": member.user.phone if member.user else None,
            "membership_status": member.membership_status,
        }
        for member in members
    ]


@router.post("/family-circles/{circle_id}/invitations", response_model=schemas.InvitationResponse)
def create_invitation(circle_id: int, payload: schemas.InvitationCreateRequest, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    circle = _circle(db, circle_id)
    _active_membership(db, current_user.id, circle.id, ("ORGANIZER",))
    token = secrets.token_urlsafe(24)
    invitation = v2_models.FamilyInvitation(
        token_hash=_hash(token),
        family_circle_id=circle.id,
        invited_role=payload.role,
        invited_relationship=payload.relationship,
        invited_by_user_id=current_user.id,
        expires_at=utc_now() + timedelta(hours=payload.expires_in_hours),
        status="pending",
    )
    db.add(invitation)
    db.commit()
    db.refresh(invitation)
    return {"invitation_id": invitation.id, "token": token, "role": payload.role, "expires_at": invitation.expires_at}


@router.post("/invitations/{token}/accept", response_model=schemas.InvitationAcceptResponse)
def accept_invitation(token: str, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    invitation = db.query(v2_models.FamilyInvitation).filter(v2_models.FamilyInvitation.token_hash == _hash(token)).first()
    if invitation is None or invitation.status != "pending" or invitation.expires_at < utc_now():
        raise HTTPException(status_code=400, detail="invalid or expired invitation")
    if invitation.invited_role == "PARENT" and db.query(v2_models.ParentProfile).filter(
        v2_models.ParentProfile.family_circle_id == invitation.family_circle_id,
        v2_models.ParentProfile.active.is_(True),
    ).count() >= 2:
        raise HTTPException(status_code=409, detail="family circle already has the maximum of two parents")
    if db.query(v2_models.FamilyMembership).filter(
        v2_models.FamilyMembership.family_circle_id == invitation.family_circle_id,
        v2_models.FamilyMembership.user_id == current_user.id,
        v2_models.FamilyMembership.membership_status == "active",
    ).first() is not None:
        raise HTTPException(status_code=409, detail="user is already a member of this circle")
    membership = v2_models.FamilyMembership(
        family_circle_id=invitation.family_circle_id,
        user_id=current_user.id,
        role=invitation.invited_role,
        relationship=invitation.invited_relationship,
        membership_status="active",
    )
    db.add(membership)
    db.flush()
    parent_profile = None
    if invitation.invited_role == "PARENT":
        parent_profile = v2_models.ParentProfile(
            user_id=current_user.id,
            family_circle_id=invitation.family_circle_id,
            display_name=current_user.name,
            timezone="UTC",
            active=True,
        )
        db.add(parent_profile)
    invitation.status = "accepted"
    invitation.accepted_by_user_id = current_user.id
    invitation.accepted_at = utc_now()
    db.commit()
    db.refresh(membership)
    return {"membership_id": membership.id, "family_circle_id": invitation.family_circle_id, "role": invitation.invited_role, "parent_profile_id": parent_profile.id if parent_profile else None}


@router.delete("/family-circles/{circle_id}/members/{membership_id}", response_model=schemas.ActionResponse)
def remove_member(circle_id: int, membership_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    circle = _circle(db, circle_id)
    _active_membership(db, current_user.id, circle.id, ("ORGANIZER",))
    membership = db.query(v2_models.FamilyMembership).filter(
        v2_models.FamilyMembership.id == membership_id,
        v2_models.FamilyMembership.family_circle_id == circle.id,
        v2_models.FamilyMembership.membership_status == "active",
    ).first()
    if membership is None:
        raise HTTPException(status_code=404, detail="active membership not found")
    if membership.role == "ORGANIZER":
        raise HTTPException(status_code=400, detail="transfer organizer or delete the circle explicitly")
    membership.membership_status = "removed"
    for profile in db.query(v2_models.ParentProfile).filter(v2_models.ParentProfile.user_id == membership.user_id, v2_models.ParentProfile.family_circle_id == circle.id).all():
        profile.active = False
    db.commit()
    return {"success": True, "status": "removed"}


@router.post("/family-circles/{circle_id}/leave", response_model=schemas.ActionResponse)
def leave_circle(circle_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    membership = _active_membership(db, current_user.id, circle_id)
    if membership.role == "ORGANIZER":
        raise HTTPException(status_code=400, detail="organizer must transfer ownership or explicitly delete the circle")
    membership.membership_status = "left"
    for profile in db.query(v2_models.ParentProfile).filter(v2_models.ParentProfile.user_id == current_user.id, v2_models.ParentProfile.family_circle_id == circle_id).all():
        profile.active = False
    db.commit()
    return {"success": True, "status": "left"}


@router.delete("/family-circles/{circle_id}", response_model=schemas.ActionResponse)
def delete_circle(circle_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    circle = _circle_any(db, circle_id)
    _active_membership(db, current_user.id, circle_id, ("ORGANIZER",))
    circle.status = "deleted"
    for membership in db.query(v2_models.FamilyMembership).filter(v2_models.FamilyMembership.family_circle_id == circle_id).all():
        membership.membership_status = "removed"
    for profile in db.query(v2_models.ParentProfile).filter(v2_models.ParentProfile.family_circle_id == circle_id).all():
        profile.active = False
    db.commit()
    return {"success": True, "status": "deleted"}


@router.delete("/users/me", response_model=schemas.ActionResponse)
def delete_my_v2_account(current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    organizer_circle = db.query(v2_models.FamilyMembership).filter(
        v2_models.FamilyMembership.user_id == current_user.id,
        v2_models.FamilyMembership.role == "ORGANIZER",
        v2_models.FamilyMembership.membership_status == "active",
    ).first()
    if organizer_circle is not None:
        raise HTTPException(status_code=409, detail="delete or transfer organizer circles before deleting the account")
    profiles = db.query(v2_models.ParentProfile).filter(v2_models.ParentProfile.user_id == current_user.id).all()
    profile_ids = [profile.id for profile in profiles]
    if profile_ids:
        db.query(v2_models.CheckInEvent).filter(v2_models.CheckInEvent.parent_profile_id.in_(profile_ids)).delete(synchronize_session=False)
        db.query(v2_models.V2HelpRequest).filter(v2_models.V2HelpRequest.parent_profile_id.in_(profile_ids)).delete(synchronize_session=False)
        db.query(v2_models.ParentProfile).filter(v2_models.ParentProfile.id.in_(profile_ids)).delete(synchronize_session=False)
    db.query(v2_models.FamilyMembership).filter(v2_models.FamilyMembership.user_id == current_user.id).update({"membership_status": "deleted"}, synchronize_session=False)
    db.query(models.DevicePushToken).filter(models.DevicePushToken.user_id == current_user.id).delete(synchronize_session=False)
    db.query(models.DeviceStatus).filter(models.DeviceStatus.user_id == current_user.id).delete(synchronize_session=False)
    current_user.invalidated_at = utc_now()
    current_user.api_token_hash = None
    current_user.name = "Deleted user"
    current_user.phone = ""
    current_user.device_id = f"deleted-{current_user.id}"
    current_user.locale_tag = None
    db.commit()
    return {"success": True, "status": "deleted"}


@router.delete("/parents/me/data", response_model=schemas.ActionResponse)
def delete_my_parent_data(current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    """Delete a parent's check-in/help data without deleting the account or membership."""
    profile = _parent_for_user(db, current_user.id)
    db.query(v2_models.CheckInEvent).filter(v2_models.CheckInEvent.parent_profile_id == profile.id).delete(synchronize_session=False)
    db.query(v2_models.V2HelpRequest).filter(v2_models.V2HelpRequest.parent_profile_id == profile.id).delete(synchronize_session=False)
    db.query(v2_models.CheckInSchedule).filter(v2_models.CheckInSchedule.parent_profile_id == profile.id).delete(synchronize_session=False)
    db.commit()
    return {"success": True, "status": "parent_data_deleted"}


@router.put("/parents/me/schedule", response_model=schemas.ScheduleResponse)
def set_parent_schedule(payload: schemas.ScheduleRequest, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    profile = _parent_for_user(db, current_user.id)
    membership = _active_membership(db, current_user.id, profile.family_circle_id, ("PARENT",))
    del membership
    schedule = db.query(v2_models.CheckInSchedule).filter(v2_models.CheckInSchedule.parent_profile_id == profile.id, v2_models.CheckInSchedule.enabled.is_(True)).first()
    if schedule is None:
        schedule = v2_models.CheckInSchedule(parent_profile_id=profile.id, **payload.model_dump())
        db.add(schedule)
    else:
        for key, value in payload.model_dump().items():
            setattr(schedule, key, value)
    profile.timezone = payload.timezone
    db.commit()
    db.refresh(schedule)
    return schedule


@router.put("/family-circles/{circle_id}/parents/{parent_id}/schedule", response_model=schemas.ScheduleResponse)
def organizer_set_parent_schedule(circle_id: int, parent_id: int, payload: schemas.ScheduleRequest, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    _circle(db, circle_id)
    _active_membership(db, current_user.id, circle_id, ("ORGANIZER",))
    profile = db.query(v2_models.ParentProfile).filter(v2_models.ParentProfile.id == parent_id, v2_models.ParentProfile.family_circle_id == circle_id, v2_models.ParentProfile.active.is_(True)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="parent not found")
    schedule = db.query(v2_models.CheckInSchedule).filter(v2_models.CheckInSchedule.parent_profile_id == profile.id, v2_models.CheckInSchedule.enabled.is_(True)).first()
    if schedule is None:
        schedule = v2_models.CheckInSchedule(parent_profile_id=profile.id, **payload.model_dump())
        db.add(schedule)
    else:
        for key, value in payload.model_dump().items():
            setattr(schedule, key, value)
    profile.timezone = payload.timezone
    db.commit()
    db.refresh(schedule)
    return schedule


@router.post("/parents/me/check-ins", response_model=schemas.CheckInResponse)
def parent_checkin(payload: schemas.CheckInRequest, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    profile = _parent_for_user(db, current_user.id)
    _active_membership(db, current_user.id, profile.family_circle_id, ("PARENT",))
    now = utc_now()
    if payload.idempotency_key:
        existing = db.query(v2_models.CheckInEvent).filter(v2_models.CheckInEvent.idempotency_key == payload.idempotency_key).first()
        if existing is not None:
            return {"event_id": existing.id, "status": existing.status, "occurred_at_utc": existing.occurred_at, "local_date": existing.local_date.isoformat(), "duplicate": True}
    schedule = None
    if payload.schedule_id is not None:
        schedule = db.query(v2_models.CheckInSchedule).filter(v2_models.CheckInSchedule.id == payload.schedule_id, v2_models.CheckInSchedule.parent_profile_id == profile.id).first()
        if schedule is None:
            raise HTTPException(status_code=404, detail="schedule not found")
    else:
        schedule = db.query(v2_models.CheckInSchedule).filter(v2_models.CheckInSchedule.parent_profile_id == profile.id, v2_models.CheckInSchedule.enabled.is_(True)).first()
    if schedule is not None:
        evaluation = evaluate_schedule(now, schedule.local_time, schedule.timezone, schedule.grace_period_minutes, schedule.days_of_week)
        scheduled_for = evaluation.scheduled_for_utc
        local_date = evaluation.local_date
        status = checkin_result(now, scheduled_for).value
    else:
        local_now = localize_utc(now, profile.timezone)
        scheduled_for = now
        local_date = local_now.date()
        status = CheckInState.CHECKED_IN.value
    event = None
    if schedule is not None:
        event = db.query(v2_models.CheckInEvent).filter(
            v2_models.CheckInEvent.parent_profile_id == profile.id,
            v2_models.CheckInEvent.schedule_id == schedule.id,
            v2_models.CheckInEvent.scheduled_for_utc == scheduled_for,
        ).first()
    if event is not None and event.occurred_at is not None:
        return {"event_id": event.id, "status": event.status, "occurred_at_utc": event.occurred_at, "local_date": event.local_date.isoformat(), "duplicate": True}
    if event is None:
        event = v2_models.CheckInEvent(
            parent_profile_id=profile.id,
            schedule_id=schedule.id if schedule else None,
            scheduled_for_utc=scheduled_for,
            occurred_at=now,
            local_date=local_date,
            event_type="checkin",
            status=status,
            battery_level=payload.battery_level,
            idempotency_key=payload.idempotency_key,
        )
        db.add(event)
    else:
        event.occurred_at = now
        event.event_type = "checkin"
        event.status = status
        event.battery_level = payload.battery_level
        event.idempotency_key = payload.idempotency_key
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        if payload.idempotency_key:
            existing = db.query(v2_models.CheckInEvent).filter(v2_models.CheckInEvent.idempotency_key == payload.idempotency_key).first()
            if existing is not None:
                return {"event_id": existing.id, "status": existing.status, "occurred_at_utc": existing.occurred_at, "local_date": existing.local_date.isoformat(), "duplicate": True}
        raise HTTPException(status_code=409, detail="check-in window already recorded") from exc
    queued = queue_circle_notifications(db, profile.family_circle_id, f"checkin:{event.id}", "checkin_confirmed", profile.display_name, exclude_user_id=current_user.id)
    db.commit()
    db.refresh(event)
    return {"event_id": event.id, "status": event.status, "occurred_at_utc": event.occurred_at, "local_date": event.local_date.isoformat(), "duplicate": False, "notification_recipients": queued}


@router.get("/family-circles/{circle_id}/parents", response_model=list[schemas.ParentStatusResponse])
def list_parents(circle_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    _circle(db, circle_id)
    _active_membership(db, current_user.id, circle_id)
    profiles = db.query(v2_models.ParentProfile).filter(v2_models.ParentProfile.family_circle_id == circle_id, v2_models.ParentProfile.active.is_(True)).all()
    return [_parent_status(profile, db, utc_now()) for profile in profiles]


def _parent_status(profile: v2_models.ParentProfile, db: Session, now: datetime) -> dict:
    schedule = db.query(v2_models.CheckInSchedule).filter(v2_models.CheckInSchedule.parent_profile_id == profile.id, v2_models.CheckInSchedule.enabled.is_(True)).first()
    latest = db.query(v2_models.CheckInEvent).filter(v2_models.CheckInEvent.parent_profile_id == profile.id, v2_models.CheckInEvent.event_type == "checkin", v2_models.CheckInEvent.occurred_at.is_not(None)).order_by(v2_models.CheckInEvent.occurred_at.desc()).first()
    if schedule is not None:
        evaluation = evaluate_schedule(now, schedule.local_time, schedule.timezone, schedule.grace_period_minutes, schedule.days_of_week)
        current_window = db.query(v2_models.CheckInEvent).filter(
            v2_models.CheckInEvent.parent_profile_id == profile.id,
            v2_models.CheckInEvent.schedule_id == schedule.id,
            v2_models.CheckInEvent.scheduled_for_utc == evaluation.scheduled_for_utc,
        ).first()
        state = current_window.status if current_window and current_window.occurred_at else evaluation.state.value
        _, next_scheduled = next_scheduled_window(now, schedule.local_time, schedule.timezone, schedule.days_of_week)
        next_local = localize_utc(next_scheduled, schedule.timezone).isoformat()
    else:
        local_today = localize_utc(now, profile.timezone).date()
        state = CheckInState.CHECKED_IN.value if latest and localize_utc(latest.occurred_at, profile.timezone).date() == local_today else CheckInState.UPCOMING.value
        next_local = None
    active_help = db.query(v2_models.V2HelpRequest).filter(
        v2_models.V2HelpRequest.parent_profile_id == profile.id,
        v2_models.V2HelpRequest.status.in_(["created", "acknowledged"]),
    ).first()
    if active_help is not None:
        state = CheckInState.HELP_REQUESTED.value
    device = db.query(models.DeviceStatus).filter(models.DeviceStatus.user_id == profile.user_id).first()
    return {
        "parent_profile_id": profile.id,
        "parent_user_id": profile.user_id,
        "display_name": profile.display_name,
        "phone": profile.user.phone if profile.user else None,
        "timezone": profile.timezone,
        "state": state,
        "last_checkin_utc": latest.occurred_at if latest else None,
        "last_checkin_local": localize_utc(latest.occurred_at, profile.timezone).isoformat() if latest else None,
        "next_checkin_local": next_local,
        "battery_level": device.battery_level if device else (latest.battery_level if latest else None),
        "last_online_utc": device.last_online_time if device else None,
    }


def _event_action(event_id: int, circle_id: int, action: str, current_user: models.User, db: Session):
    _circle(db, circle_id)
    _active_membership(db, current_user.id, circle_id, ("ORGANIZER", "FAMILY_MEMBER"))
    event = db.query(v2_models.CheckInEvent).join(v2_models.ParentProfile).filter(
        v2_models.CheckInEvent.id == event_id,
        v2_models.ParentProfile.family_circle_id == circle_id,
    ).first()
    if event is None:
        raise HTTPException(status_code=404, detail="check-in event not found")
    event.escalation_state = action
    db.commit()
    return {"success": True, "status": action}


@router.post("/family-circles/{circle_id}/check-in-events/{event_id}/acknowledge", response_model=schemas.ActionResponse)
def acknowledge_checkin_event(circle_id: int, event_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    return _event_action(event_id, circle_id, "ACKNOWLEDGED", current_user, db)


@router.post("/family-circles/{circle_id}/check-in-events/{event_id}/resolve", response_model=schemas.ActionResponse)
def resolve_checkin_event(circle_id: int, event_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    return _event_action(event_id, circle_id, "RESOLVED", current_user, db)


@router.get("/family-circles/{circle_id}/parents/{parent_id}/status", response_model=schemas.ParentStatusResponse)
def parent_status(circle_id: int, parent_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    _circle(db, circle_id)
    _active_membership(db, current_user.id, circle_id)
    profile = db.query(v2_models.ParentProfile).filter(v2_models.ParentProfile.id == parent_id, v2_models.ParentProfile.family_circle_id == circle_id, v2_models.ParentProfile.active.is_(True)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="parent not found")
    return _parent_status(profile, db, utc_now())


@router.get("/family-circles/{circle_id}/parents/{parent_id}/history", response_model=list[schemas.HistoryItemResponse])
def parent_history(circle_id: int, parent_id: int, days: int = Query(default=7, ge=1, le=30), current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    _circle(db, circle_id)
    _active_membership(db, current_user.id, circle_id)
    profile = db.query(v2_models.ParentProfile).filter(v2_models.ParentProfile.id == parent_id, v2_models.ParentProfile.family_circle_id == circle_id, v2_models.ParentProfile.active.is_(True)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="parent not found")
    local_today = localize_utc(utc_now(), profile.timezone).date()
    events = db.query(v2_models.CheckInEvent).filter(v2_models.CheckInEvent.parent_profile_id == profile.id, v2_models.CheckInEvent.local_date >= local_today - timedelta(days=days - 1)).order_by(v2_models.CheckInEvent.local_date.desc(), v2_models.CheckInEvent.scheduled_for_utc.desc()).limit(31).all()
    help_requests = db.query(v2_models.V2HelpRequest).filter(v2_models.V2HelpRequest.parent_profile_id == profile.id, v2_models.V2HelpRequest.created_at >= utc_now() - timedelta(days=days)).order_by(v2_models.V2HelpRequest.created_at.desc()).limit(31).all()
    rows = [*events, *help_requests]
    rows.sort(key=lambda row: getattr(row, "occurred_at", None) or getattr(row, "created_at", None), reverse=True)
    return [
        {
            "event_id": row.id,
            "local_date": row.local_date.isoformat() if hasattr(row, "local_date") else localize_utc(row.created_at, profile.timezone).date().isoformat(),
            "occurred_at_utc": row.occurred_at if hasattr(row, "occurred_at") else row.created_at,
            "status": row.status if hasattr(row, "status") else CheckInState.HELP_REQUESTED.value,
            "battery_level": row.battery_level if hasattr(row, "battery_level") else None,
        }
        for row in rows[:31]
    ]


@router.post("/family-circles/{circle_id}/parents/{parent_id}/reminders", response_model=schemas.ActionResponse)
def send_parent_reminder(circle_id: int, parent_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    _circle(db, circle_id)
    _active_membership(db, current_user.id, circle_id, ("ORGANIZER", "FAMILY_MEMBER"))
    profile = db.query(v2_models.ParentProfile).filter(v2_models.ParentProfile.id == parent_id, v2_models.ParentProfile.family_circle_id == circle_id, v2_models.ParentProfile.active.is_(True)).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="parent not found")
    queued = queue_circle_notifications(db, circle_id, f"reminder:{profile.id}:{current_user.id}:{utc_now().replace(second=0, microsecond=0).isoformat()}", "reminder", profile.display_name, exclude_user_id=profile.user_id)
    db.commit()
    return {"success": True, "status": f"queued:{queued}"}


@router.post("/parents/me/help-requests", response_model=schemas.HelpRequestResponse)
def create_help_request(payload: schemas.HelpRequestCreate, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    profile = _parent_for_user(db, current_user.id)
    _active_membership(db, current_user.id, profile.family_circle_id, ("PARENT",))
    request = v2_models.V2HelpRequest(family_circle_id=profile.family_circle_id, parent_profile_id=profile.id, request_type=payload.request_type, message=payload.message, status="created")
    db.add(request)
    db.flush()
    queue_circle_notifications(db, profile.family_circle_id, f"help:{request.id}", "help_requested", profile.display_name, exclude_user_id=current_user.id)
    db.commit()
    db.refresh(request)
    return request


def _help_action(request_id: int, action: str, current_user: models.User, db: Session):
    request = db.get(v2_models.V2HelpRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="help request not found")
    membership = _active_membership(db, current_user.id, request.family_circle_id, ("ORGANIZER", "FAMILY_MEMBER"))
    if action == "acknowledge" and request.status == "created":
        request.status = "acknowledged"
        request.acknowledged_at = utc_now()
        request.acknowledged_by_membership_id = membership.id
    elif action == "resolve" and request.status in {"created", "acknowledged"}:
        request.status = "resolved"
        request.resolved_at = utc_now()
    db.commit()
    return {"success": True, "status": request.status}


@router.post("/help-requests/{request_id}/acknowledge", response_model=schemas.ActionResponse)
def acknowledge_help(request_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    return _help_action(request_id, "acknowledge", current_user, db)


@router.post("/help-requests/{request_id}/resolve", response_model=schemas.ActionResponse)
def resolve_help(request_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    return _help_action(request_id, "resolve", current_user, db)


@router.post("/devices/me/heartbeat", response_model=schemas.ActionResponse)
def heartbeat(payload: schemas.DeviceStatusRequest, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    current_user.locale_tag = normalize_locale(payload.locale_tag)
    status = db.query(models.DeviceStatus).filter(models.DeviceStatus.user_id == current_user.id).first()
    if status is None:
        status = models.DeviceStatus(user_id=current_user.id)
        db.add(status)
    status.platform = payload.platform
    status.app_version = payload.app_version
    status.device_uuid = payload.device_id
    status.battery_level = payload.battery_level
    status.last_online_time = utc_now()
    db.commit()
    return {"success": True}


@router.post("/devices/me/push-token", response_model=schemas.ActionResponse)
def register_push_token(payload: schemas.PushTokenRequest, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    current_user.locale_tag = normalize_locale(current_user.locale_tag)
    provider = "android_huawei" if payload.push_provider == "hms" else payload.push_provider
    if provider == "fcm":
        current_user.fcm_token = payload.push_token
        current_user.fcm_token_invalidated_at = None
    token = db.query(models.DevicePushToken).filter(models.DevicePushToken.user_id == current_user.id, models.DevicePushToken.push_provider == provider).first()
    if token is None:
        token = models.DevicePushToken(user_id=current_user.id, push_provider=provider, push_token=payload.push_token, platform=payload.platform, push_token_updated_at=utc_now())
        db.add(token)
    else:
        token.push_token = payload.push_token
        token.platform = payload.platform
        token.push_token_updated_at = utc_now()
        token.push_token_invalidated_at = None
    db.commit()
    return {"success": True}


@router.post("/family-circles/{circle_id}/escalation-rules", response_model=schemas.ActionResponse)
def create_escalation_rule(circle_id: int, payload: schemas.EscalationRuleRequest, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    _circle(db, circle_id)
    _active_membership(db, current_user.id, circle_id, ("ORGANIZER",))
    member = db.query(v2_models.FamilyMembership).filter(v2_models.FamilyMembership.id == payload.family_member_id, v2_models.FamilyMembership.family_circle_id == circle_id, v2_models.FamilyMembership.role.in_(["ORGANIZER", "FAMILY_MEMBER"]), v2_models.FamilyMembership.membership_status == "active").first()
    if member is None:
        raise HTTPException(status_code=404, detail="family member not found")
    rule = v2_models.EscalationRule(family_circle_id=circle_id, family_member_id=member.id, priority=payload.priority, delay_minutes=payload.delay_minutes, enabled=payload.enabled)
    db.add(rule)
    db.commit()
    return {"success": True, "status": "created"}
