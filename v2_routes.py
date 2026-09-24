import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
import v2_models
import v2_schemas as schemas
from database import get_db
from v2_auth import get_v2_current_user
from v2_notifications import normalize_locale, queue_circle_notifications, queue_recipient_notification
from v2_time import CheckInState, as_utc, checkin_result, evaluate_schedule, localize_utc, next_scheduled_window, utc_now


router = APIRouter(prefix="/api/v2", tags=["parent-check-in-v2"])
logger = logging.getLogger("parent_safety")

PUBLIC_INVITATION_CODE_ATTEMPT_LIMIT = 5
PUBLIC_INVITATION_CODE_ATTEMPT_WINDOW_MINUTES = 15
PUBLIC_INVITATION_CODE_ALLOCATION_ATTEMPTS = 20
ORGANIZER_RECOVERY_MAX_FAILURES = 5
ORGANIZER_RECOVERY_LOCK_MINUTES = 15


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _public_invitation_code() -> str:
    """Return an ASCII six-digit code using the OS-backed secrets generator."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _normalize_invitation_credential(value: str) -> tuple[str, bool]:
    stripped = value.strip()
    without_spaces = "".join(stripped.split())
    is_public_code = (
        len(without_spaces) == 6
        and all("0" <= character <= "9" for character in without_spaces)
    )
    return (without_spaces, True) if is_public_code else (stripped, False)


def _organizer_recovery_code() -> str:
    return secrets.token_urlsafe(32)


def _record_public_code_attempt(db: Session, user_id: int) -> None:
    window_start = utc_now() - timedelta(minutes=PUBLIC_INVITATION_CODE_ATTEMPT_WINDOW_MINUTES)
    db.query(v2_models.V2InvitationAttempt).filter(
        v2_models.V2InvitationAttempt.attempted_at < window_start,
    ).delete(synchronize_session=False)
    recent_attempts = db.query(v2_models.V2InvitationAttempt).filter(
        v2_models.V2InvitationAttempt.user_id == user_id,
        v2_models.V2InvitationAttempt.attempted_at >= window_start,
    ).count()
    if recent_attempts >= PUBLIC_INVITATION_CODE_ATTEMPT_LIMIT:
        db.commit()
        retry_after_seconds = PUBLIC_INVITATION_CODE_ATTEMPT_WINDOW_MINUTES * 60
        raise HTTPException(
            status_code=429,
            detail="too many invitation-code attempts; try again later",
            headers={"Retry-After": str(retry_after_seconds)},
        )
    db.add(v2_models.V2InvitationAttempt(user_id=user_id, attempted_at=utc_now()))
    db.commit()


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


def _offline_setting_response(setting: v2_models.OfflineAlertSetting | None) -> dict:
    return {
        "offline_alert_enabled": setting.offline_alert_enabled if setting else True,
        "offline_alert_hours": setting.offline_alert_hours if setting else 6,
        "quiet_hours_enabled": setting.quiet_hours_enabled if setting else True,
        "quiet_start_time": setting.quiet_start_time if setting else "22:00",
        "quiet_end_time": setting.quiet_end_time if setting else "07:00",
        "pause_until": setting.pause_until if setting else None,
    }


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


def _circle_response(db: Session, circle: v2_models.FamilyCircle, viewer_user_id: int | None = None) -> dict:
    count = db.query(v2_models.FamilyMembership).filter(
        v2_models.FamilyMembership.family_circle_id == circle.id,
        v2_models.FamilyMembership.membership_status == "active",
    ).count()
    organizer = db.get(models.User, circle.organizer_user_id)
    membership_role = None
    if viewer_user_id is not None:
        membership = db.query(v2_models.FamilyMembership).filter(
            v2_models.FamilyMembership.family_circle_id == circle.id,
            v2_models.FamilyMembership.user_id == viewer_user_id,
            v2_models.FamilyMembership.membership_status == "active",
        ).first()
        membership_role = membership.role if membership is not None else None
    return {
        "id": circle.id,
        "organizer_user_id": circle.organizer_user_id,
        "status": circle.status,
        "member_count": count,
        "organizer_name": organizer.name if organizer else "",
        "membership_role": membership_role,
    }


@router.post("/register", response_model=schemas.V2RegisterResponse)
def register_v2(payload: schemas.V2RegisterRequest, db: Session = Depends(get_db)):
    role = "parent" if payload.role == "PARENT" else "family_member"
    existing = db.query(models.User).filter(models.User.device_id == payload.device_id, models.User.role == role).first()
    if existing is not None and role == "parent":
        # A reinstall must not receive the old account's token.  Create or
        # reuse a pending identity that can only become active through an
        # invitation from the original circle organizer.
        pending = db.query(models.User).filter(
            models.User.role == "parent",
            models.User.recovery_device_id == payload.device_id,
        ).order_by(models.User.id.desc()).first()
        active_membership = None
        if pending is not None:
            active_membership = db.query(v2_models.FamilyMembership).filter(
                v2_models.FamilyMembership.user_id == pending.id,
                v2_models.FamilyMembership.membership_status == "active",
            ).first()
        if pending is None or active_membership is not None:
            pending = models.User(
                role=role,
                name=payload.name,
                phone=payload.phone,
                # Keep the protected physical-device owner unique.  This
                # placeholder is replaced only after authorized recovery.
                device_id=f"pending-parent-{uuid4().hex}",
                recovery_device_id=payload.device_id,
                api_token_hash=None,
                locale_tag=normalize_locale(payload.locale_tag),
            )
            db.add(pending)
        else:
            pending.name = payload.name
            pending.phone = payload.phone
            pending.locale_tag = normalize_locale(payload.locale_tag)
        raw_token = secrets.token_urlsafe(32)
        pending.api_token_hash = _hash(raw_token)
        db.commit()
        db.refresh(pending)
        return {"user_id": pending.id, "role": payload.role, "api_token": raw_token, "locale_tag": pending.locale_tag}
    if existing is not None:
        logger.warning(
            "register_duplicate_conflict event=register_duplicate_conflict "
            "existing_user_id=%s role=%s device_fp=%s timestamp=%s",
            existing.id,
            role,
            _hash(payload.device_id),
            utc_now().isoformat(),
        )
        raise HTTPException(status_code=409, detail="device is already registered for this role")
    pending = db.query(models.User).filter(
        models.User.role == role,
        models.User.recovery_device_id == payload.device_id,
    ).order_by(models.User.id.desc()).first()
    if pending is not None:
        active_membership = db.query(v2_models.FamilyMembership).filter(
            v2_models.FamilyMembership.user_id == pending.id,
            v2_models.FamilyMembership.membership_status == "active",
        ).first()
        if active_membership is None:
            raw_token = secrets.token_urlsafe(32)
            pending.name = payload.name
            pending.phone = payload.phone
            pending.locale_tag = normalize_locale(payload.locale_tag)
            pending.api_token_hash = _hash(raw_token)
            db.commit()
            db.refresh(pending)
            return {"user_id": pending.id, "role": payload.role, "api_token": raw_token, "locale_tag": pending.locale_tag}
    raw_token = secrets.token_urlsafe(32)
    recovery_code = _organizer_recovery_code() if role == "family_member" else None
    user = models.User(
        role=role,
        name=payload.name,
        phone=payload.phone,
        device_id=payload.device_id,
        api_token_hash=_hash(raw_token),
        organizer_recovery_verifier=_hash(recovery_code) if recovery_code else None,
        organizer_recovery_created_at=utc_now() if recovery_code else None,
        organizer_recovery_failed_attempts=0,
        locale_tag=normalize_locale(payload.locale_tag),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"user_id": user.id, "role": payload.role, "api_token": raw_token, "locale_tag": user.locale_tag, "recovery_code": recovery_code}


@router.post("/organizer/recover", response_model=schemas.OrganizerRecoveryResponse)
def recover_organizer(payload: schemas.OrganizerRecoveryRequest, db: Session = Depends(get_db)):
    """Recover a pre-existing organizer only with its user-held secret."""
    user = db.query(models.User).filter(
        models.User.device_id == payload.device_id,
        models.User.role == "family_member",
    ).first()
    now = utc_now()
    if user is None or not user.organizer_recovery_verifier:
        raise HTTPException(status_code=409, detail="organizer recovery is unavailable for this account")
    if user.organizer_recovery_one_time and user.organizer_recovery_used_at is not None:
        raise HTTPException(status_code=409, detail="organizer recovery credential has already been used")
    if user.organizer_recovery_locked_until and user.organizer_recovery_locked_until > now:
        retry_after = int((user.organizer_recovery_locked_until - now).total_seconds())
        raise HTTPException(status_code=429, detail="too many recovery attempts; try again later", headers={"Retry-After": str(max(1, retry_after))})
    if not secrets.compare_digest(user.organizer_recovery_verifier, _hash(payload.recovery_code)):
        user.organizer_recovery_failed_attempts = (user.organizer_recovery_failed_attempts or 0) + 1
        if user.organizer_recovery_failed_attempts >= ORGANIZER_RECOVERY_MAX_FAILURES:
            user.organizer_recovery_locked_until = now + timedelta(minutes=ORGANIZER_RECOVERY_LOCK_MINUTES)
            user.organizer_recovery_failed_attempts = 0
        db.commit()
        raise HTTPException(status_code=401, detail="invalid organizer recovery code")
    raw_token = secrets.token_urlsafe(32)
    user.api_token_hash = _hash(raw_token)
    user.organizer_recovery_failed_attempts = 0
    user.organizer_recovery_locked_until = None
    user.organizer_recovery_used_at = now
    db.add(models.OrganizerRecoveryAuditEvent(user_id=user.id, action="recovered", source="organizer_recovery"))
    db.commit()
    return {"user_id": user.id, "role": "FAMILY_MEMBER", "api_token": raw_token, "locale_tag": user.locale_tag or "en"}


@router.get("/users/me", response_model=schemas.V2CurrentUserResponse)
def current_user_v2(current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    active_memberships = db.query(v2_models.FamilyMembership).filter(
        v2_models.FamilyMembership.user_id == current_user.id,
        v2_models.FamilyMembership.membership_status == "active",
    ).all()
    return {
        "user_id": current_user.id,
        "role": "PARENT" if current_user.role == "parent" else "FAMILY_MEMBER",
        "name": current_user.name,
        "locale_tag": normalize_locale(current_user.locale_tag),
        "has_parent_membership": any(membership.role == "PARENT" for membership in active_memberships),
        "has_organizer_membership": any(membership.role == "ORGANIZER" for membership in active_memberships),
        "phone": current_user.phone or None,
    }


@router.put("/users/me/profile", response_model=schemas.V2CurrentUserResponse)
def update_current_user_profile(payload: schemas.UserProfileUpdateRequest, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    current_user.phone = payload.phone.strip()
    db.commit()
    db.refresh(current_user)
    return current_user_v2(current_user=current_user, db=db)


@router.post("/family-circles", response_model=schemas.CircleResponse)
def create_circle(payload: schemas.CircleCreateRequest, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    existing = db.query(v2_models.FamilyMembership).filter(
        v2_models.FamilyMembership.user_id == current_user.id,
        v2_models.FamilyMembership.role == "ORGANIZER",
        v2_models.FamilyMembership.membership_status == "active",
    ).first()
    if existing is not None:
        # Creation is idempotent at the capability boundary: switching UI
        # context or retrying must restore the one circle this account owns.
        existing_circle = _circle_any(db, existing.family_circle_id)
        return _circle_response(db, existing_circle, current_user.id)
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
    return _circle_response(db, circle, current_user.id)


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
            circles.append(_circle_response(db, circle, current_user.id))
    return circles


@router.get("/family-circles/{circle_id}", response_model=schemas.CircleResponse)
def get_circle(circle_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    circle = _circle_any(db, circle_id)
    _active_membership(db, current_user.id, circle_id)
    return _circle_response(db, circle, current_user.id)


@router.get("/family-circles/{circle_id}/members", response_model=list[schemas.MemberResponse])
def list_members(circle_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    _circle(db, circle_id)
    _active_membership(db, current_user.id, circle_id)
    members = db.query(v2_models.FamilyMembership).filter(
        v2_models.FamilyMembership.family_circle_id == circle_id,
        v2_models.FamilyMembership.membership_status == "active",
    ).order_by(v2_models.FamilyMembership.id.asc()).all()
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


@router.get(
    "/family-circles/{circle_id}/offline-alert-settings",
    response_model=schemas.OfflineAlertSettingsResponse,
)
def get_offline_alert_settings(
    circle_id: int,
    current_user: models.User = Depends(get_v2_current_user),
    db: Session = Depends(get_db),
):
    _circle_any(db, circle_id)
    _active_membership(db, current_user.id, circle_id, ("ORGANIZER",))
    setting = db.query(v2_models.OfflineAlertSetting).filter(
        v2_models.OfflineAlertSetting.family_circle_id == circle_id
    ).first()
    return _offline_setting_response(setting)


@router.put(
    "/family-circles/{circle_id}/offline-alert-settings",
    response_model=schemas.OfflineAlertSettingsResponse,
)
def update_offline_alert_settings_v2(
    circle_id: int,
    payload: schemas.OfflineAlertSettingsRequest,
    current_user: models.User = Depends(get_v2_current_user),
    db: Session = Depends(get_db),
):
    _circle_any(db, circle_id)
    _active_membership(db, current_user.id, circle_id, ("ORGANIZER",))
    setting = db.query(v2_models.OfflineAlertSetting).filter(
        v2_models.OfflineAlertSetting.family_circle_id == circle_id
    ).first()
    if setting is None:
        setting = v2_models.OfflineAlertSetting(family_circle_id=circle_id)
        db.add(setting)
    for field in (
        "offline_alert_enabled", "offline_alert_hours", "quiet_hours_enabled",
        "quiet_start_time", "quiet_end_time", "pause_until",
    ):
        setattr(setting, field, getattr(payload, field))
    db.commit()
    db.refresh(setting)
    return _offline_setting_response(setting)


@router.post("/family-circles/{circle_id}/invitations", response_model=schemas.InvitationResponse)
def create_invitation(circle_id: int, payload: schemas.InvitationCreateRequest, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    circle = _circle(db, circle_id)
    _active_membership(db, current_user.id, circle.id, ("ORGANIZER",))
    token = ""
    public_code = ""
    invitation = None
    for _ in range(PUBLIC_INVITATION_CODE_ALLOCATION_ATTEMPTS):
        candidate_token = secrets.token_urlsafe(24)
        candidate_code = _public_invitation_code()
        candidate_invitation = v2_models.FamilyInvitation(
            token_hash=_hash(candidate_token),
            public_code_hash=_hash(candidate_code),
            family_circle_id=circle.id,
            invited_role=payload.role,
            invited_relationship=payload.relationship,
            invited_by_user_id=current_user.id,
            expires_at=utc_now() + timedelta(hours=payload.expires_in_hours),
            status="pending",
        )
        try:
            with db.begin_nested():
                db.add(candidate_invitation)
                db.flush()
            token = candidate_token
            public_code = candidate_code
            invitation = candidate_invitation
            break
        except IntegrityError:
            # A code collision must never overwrite or retarget an invitation.
            # Retry with a new independently generated token and code.
            continue
    if invitation is None:
        db.rollback()
        raise HTTPException(status_code=503, detail="could not allocate invitation code")
    db.commit()
    db.refresh(invitation)
    return {
        "invitation_id": invitation.id,
        "token": token,
        "public_code": public_code,
        "role": payload.role,
        "expires_at": invitation.expires_at,
    }


@router.post("/invitations/{token}/accept", response_model=schemas.InvitationAcceptResponse)
def accept_invitation(token: str, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    credential, is_public_code = _normalize_invitation_credential(token)
    if is_public_code:
        _record_public_code_attempt(db, current_user.id)
        invitation = db.query(v2_models.FamilyInvitation).filter(
            v2_models.FamilyInvitation.public_code_hash == _hash(credential)
        ).first()
    else:
        # Legacy outstanding invitations continue to resolve through the
        # original high-entropy token hash.
        invitation = db.query(v2_models.FamilyInvitation).filter(
            v2_models.FamilyInvitation.token_hash == _hash(credential)
        ).first()
    if invitation is not None and invitation.status == "accepted" and invitation.accepted_by_user_id == current_user.id:
        membership = db.query(v2_models.FamilyMembership).filter(
            v2_models.FamilyMembership.family_circle_id == invitation.family_circle_id,
            v2_models.FamilyMembership.user_id == current_user.id,
            v2_models.FamilyMembership.membership_status == "active",
        ).first()
        profile = db.query(v2_models.ParentProfile).filter(
            v2_models.ParentProfile.family_circle_id == invitation.family_circle_id,
            v2_models.ParentProfile.user_id == current_user.id,
            v2_models.ParentProfile.active.is_(True),
        ).first()
        if membership is not None and profile is not None and profile.recovered_from_parent_profile_id is not None:
            return {"membership_id": membership.id, "family_circle_id": invitation.family_circle_id, "role": invitation.invited_role, "parent_profile_id": profile.id if profile else None}
    if invitation is None:
        raise HTTPException(status_code=400, detail="invalid invitation code")
    if invitation.status != "pending":
        raise HTTPException(status_code=400, detail="invitation code has already been used")
    if invitation.expires_at < utc_now():
        invitation.status = "expired"
        db.commit()
        raise HTTPException(status_code=400, detail="invitation code has expired")
    if current_user.recovery_device_id is not None and invitation.invited_role != "PARENT":
        raise HTTPException(status_code=403, detail="parent recovery requires a Parent invitation")
    if invitation.invited_role == "PARENT" and current_user.recovery_device_id is not None:
        # A reinstall-created identity can only be recovered into the circle
        # that owns the protected device.  It cannot be used to join another
        # circle as a normal Parent.
        old_user = db.query(models.User).filter(
            models.User.role == "parent",
            models.User.device_id == current_user.recovery_device_id,
        ).first()
        if old_user is None:
            raise HTTPException(status_code=409, detail="parent recovery is unavailable")
        if db.query(v2_models.FamilyMembership).filter(
            v2_models.FamilyMembership.user_id == current_user.id,
            v2_models.FamilyMembership.membership_status == "active",
        ).first() is not None:
            raise HTTPException(status_code=409, detail="parent recovery requires an unpaired identity")
        if invitation.invited_by_user_id == current_user.id:
            raise HTTPException(status_code=403, detail="parent recovery requires the family organizer")
        old_profiles = db.query(v2_models.ParentProfile).filter(
            v2_models.ParentProfile.user_id == old_user.id,
            v2_models.ParentProfile.active.is_(True),
        ).all()
        if len(old_profiles) != 1 or old_profiles[0].family_circle_id != invitation.family_circle_id:
            raise HTTPException(status_code=403, detail="parent recovery requires the original family circle")
        organizer_membership = db.query(v2_models.FamilyMembership).filter(
            v2_models.FamilyMembership.family_circle_id == invitation.family_circle_id,
            v2_models.FamilyMembership.user_id == invitation.invited_by_user_id,
            v2_models.FamilyMembership.role == "ORGANIZER",
            v2_models.FamilyMembership.membership_status == "active",
        ).first()
        if organizer_membership is None:
            raise HTTPException(status_code=403, detail="invitation organizer is not active")
        old_profile = old_profiles[0]
        old_membership = db.query(v2_models.FamilyMembership).filter(
            v2_models.FamilyMembership.family_circle_id == invitation.family_circle_id,
            v2_models.FamilyMembership.user_id == old_user.id,
            v2_models.FamilyMembership.role == "PARENT",
            v2_models.FamilyMembership.membership_status == "active",
        ).first()
        if old_membership is None:
            raise HTTPException(status_code=409, detail="parent recovery requires an active original parent")
        now = utc_now()
        # Move the protected owner to an audit-only tombstone before assigning
        # the physical identifier to the replacement.  This preserves the old
        # user/profile/history without leaving two active device owners.
        old_membership.membership_status = "replaced"
        old_profile.active = False
        old_user.api_token_hash = None
        old_user.fcm_token = None
        old_user.fcm_token_invalidated_at = now
        old_user.device_id = f"replaced-parent-{old_user.id}-{uuid4().hex}"
        for old_token in db.query(models.DevicePushToken).filter(models.DevicePushToken.user_id == old_user.id).all():
            old_token.push_token_invalidated_at = now
        db.query(models.DeviceStatus).filter(models.DeviceStatus.user_id == old_user.id).delete(synchronize_session=False)
        current_user.device_id = current_user.recovery_device_id
        current_user.recovery_device_id = None
        membership = v2_models.FamilyMembership(
            family_circle_id=invitation.family_circle_id,
            user_id=current_user.id,
            role="PARENT",
            relationship=invitation.invited_relationship,
            membership_status="active",
        )
        db.add(membership)
        db.flush()
        parent_profile = v2_models.ParentProfile(
            user_id=current_user.id,
            family_circle_id=invitation.family_circle_id,
            display_name=current_user.name,
            timezone=old_profile.timezone,
            active=True,
            recovered_from_parent_profile_id=old_profile.id,
        )
        db.add(parent_profile)
        invitation.status = "accepted"
        invitation.accepted_by_user_id = current_user.id
        invitation.accepted_at = now
        db.commit()
        db.refresh(membership)
        db.refresh(parent_profile)
        return {"membership_id": membership.id, "family_circle_id": invitation.family_circle_id, "role": invitation.invited_role, "parent_profile_id": parent_profile.id}
    if invitation.invited_role == "PARENT" and db.query(v2_models.ParentProfile).filter(
        v2_models.ParentProfile.family_circle_id == invitation.family_circle_id,
        v2_models.ParentProfile.active.is_(True),
    ).count() >= 1:
        raise HTTPException(status_code=409, detail="this connection already has a parent")
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
    db.query(v2_models.V2NotificationDelivery).filter(
        v2_models.V2NotificationDelivery.recipient_user_id == current_user.id
    ).delete(synchronize_session=False)
    # Keep the user as an audit tombstone while invalidating every
    # authentication and notification credential.  User has no generic
    # `invalidated_at` column; this existing field is the account's FCM
    # invalidation marker and is also used by the token-registration flow.
    current_user.fcm_token = None
    current_user.fcm_token_invalidated_at = utc_now()
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


@router.delete("/users/me/data", response_model=schemas.ActionResponse)
def delete_my_user_data(current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    """Delete device and notification data while retaining the authenticated account and memberships."""
    db.query(models.DevicePushToken).filter(models.DevicePushToken.user_id == current_user.id).delete(synchronize_session=False)
    db.query(models.DeviceStatus).filter(models.DeviceStatus.user_id == current_user.id).delete(synchronize_session=False)
    db.query(v2_models.V2NotificationDelivery).filter(v2_models.V2NotificationDelivery.recipient_user_id == current_user.id).delete(synchronize_session=False)
    current_user.fcm_token = None
    current_user.fcm_token_invalidated_at = utc_now()
    db.commit()
    return {"success": True, "status": "user_data_deleted"}


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
    help_query = db.query(v2_models.V2HelpRequest).filter(
        v2_models.V2HelpRequest.parent_profile_id == profile.id,
        v2_models.V2HelpRequest.status.in_(["created", "acknowledged"]),
    )
    if latest is not None:
        help_query = help_query.filter(v2_models.V2HelpRequest.created_at > latest.occurred_at)
    active_help = help_query.order_by(v2_models.V2HelpRequest.created_at.desc()).first()
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
    profile_ids = [profile.id]
    predecessor_id = profile.recovered_from_parent_profile_id
    while predecessor_id is not None and predecessor_id not in profile_ids:
        predecessor = db.get(v2_models.ParentProfile, predecessor_id)
        if predecessor is None or predecessor.family_circle_id != circle_id:
            break
        profile_ids.append(predecessor.id)
        predecessor_id = predecessor.recovered_from_parent_profile_id
    local_today = localize_utc(utc_now(), profile.timezone).date()
    events = db.query(v2_models.CheckInEvent).filter(v2_models.CheckInEvent.parent_profile_id.in_(profile_ids), v2_models.CheckInEvent.local_date >= local_today - timedelta(days=days - 1)).order_by(v2_models.CheckInEvent.local_date.desc(), v2_models.CheckInEvent.scheduled_for_utc.desc()).limit(31).all()
    help_requests = db.query(v2_models.V2HelpRequest).filter(v2_models.V2HelpRequest.parent_profile_id.in_(profile_ids), v2_models.V2HelpRequest.created_at >= utc_now() - timedelta(days=days)).order_by(v2_models.V2HelpRequest.created_at.desc()).limit(31).all()
    rows = [*events, *help_requests]
    rows.sort(key=lambda row: getattr(row, "occurred_at", None) or getattr(row, "created_at", None), reverse=True)
    return [
        {
            "event_id": row.id,
            "local_date": row.local_date.isoformat() if hasattr(row, "local_date") else localize_utc(row.created_at, profile.timezone).date().isoformat(),
            "occurred_at_utc": row.occurred_at if hasattr(row, "occurred_at") else row.created_at,
            "status": row.status if hasattr(row, "status") else CheckInState.HELP_REQUESTED.value,
            "battery_level": row.battery_level if hasattr(row, "battery_level") else None,
            "request_type": row.request_type if isinstance(row, v2_models.V2HelpRequest) else None,
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
    queued = queue_recipient_notification(
        db,
        f"reminder:{profile.id}:{current_user.id}:{utc_now().replace(second=0, microsecond=0).isoformat()}",
        profile.user_id,
        "reminder",
        profile.display_name,
    )
    db.commit()
    return {"success": True, "status": f"queued:{queued}"}


@router.post("/family-circles/{circle_id}/parents/{parent_id}/check-now", response_model=schemas.ActionResponse)
def check_parent_now(circle_id: int, parent_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    _circle(db, circle_id)
    _active_membership(db, current_user.id, circle_id, ("ORGANIZER", "FAMILY_MEMBER"))
    profile = db.query(v2_models.ParentProfile).filter(
        v2_models.ParentProfile.id == parent_id,
        v2_models.ParentProfile.family_circle_id == circle_id,
        v2_models.ParentProfile.active.is_(True),
    ).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="parent not found")
    queued = queue_recipient_notification(
        db,
        f"check-now:{profile.id}:{current_user.id}:{utc_now().replace(second=0, microsecond=0).isoformat()}",
        profile.user_id,
        "check_now",
        profile.display_name,
    )
    db.commit()
    return {"success": True, "status": f"queued:{queued}"}


@router.put("/family-circles/{circle_id}/parents/{parent_id}/profile", response_model=schemas.V2CurrentUserResponse)
def update_parent_profile(circle_id: int, parent_id: int, payload: schemas.UserProfileUpdateRequest, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    _circle(db, circle_id)
    _active_membership(db, current_user.id, circle_id, ("ORGANIZER",))
    profile = db.query(v2_models.ParentProfile).filter(
        v2_models.ParentProfile.id == parent_id,
        v2_models.ParentProfile.family_circle_id == circle_id,
        v2_models.ParentProfile.active.is_(True),
    ).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="parent not found")
    parent = db.get(models.User, profile.user_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="parent user not found")
    parent.phone = payload.phone.strip()
    db.commit()
    db.refresh(parent)
    return current_user_v2(current_user=parent, db=db)


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
    token_by_value = db.query(models.DevicePushToken).filter(
        models.DevicePushToken.push_provider == provider,
        models.DevicePushToken.push_token == payload.push_token,
    ).first()
    if token_by_value is not None and token_by_value.user_id != current_user.id:
        if token_by_value.push_token_invalidated_at is None:
            raise HTTPException(status_code=409, detail="push token is active for another account")
        if token is None:
            token = token_by_value
            token.user_id = current_user.id
        else:
            token_by_value.push_token = f"invalidated-{token_by_value.id}-{uuid4().hex}"
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
