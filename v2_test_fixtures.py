"""Explicitly isolated integration-test fixtures.

This module is imported only when APP_ENV is non-production.  Fixtures live in
their own table and are projected into the normal read-only entitlement view
for the local test server; Google Play verification records are never written.
"""

import hmac
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

import models
import v2_models
from database import Base
from database import get_db


TEST_FIXTURE_MARKER = "TEST_FIXTURE"
TEST_USER_PREFIX = "TEST-"
TEST_DEVICE_PREFIX = "integration-test-"


class TestEntitlementFixture(Base):
    __tablename__ = "v2_test_entitlement_fixtures"
    __table_args__ = (UniqueConstraint("family_circle_id", name="uq_v2_test_fixture_circle"),)

    id = Column(Integer, primary_key=True)
    family_circle_id = Column(Integer, ForeignKey("v2_family_circles.id", ondelete="CASCADE"), nullable=False)
    organizer_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(String(128), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class FixtureRequest(BaseModel):
    user_id: int = Field(gt=0)
    family_circle_id: int = Field(gt=0)
    expires_in_minutes: int = Field(default=60, ge=5, le=240)


class FixtureCleanupRequest(BaseModel):
    user_id: int = Field(gt=0)
    family_circle_id: int | None = Field(default=None, gt=0)


router = APIRouter(prefix="/api/v2/test-fixtures", tags=["integration-test-fixtures"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _require_access(provided_secret: str | None) -> None:
    if os.getenv("APP_ENV", "production").strip().lower() in {"production", "prod"}:
        raise HTTPException(status_code=404, detail="not found")
    expected = os.getenv("V3_TEST_FIXTURE_SECRET", "").strip()
    if not expected or not provided_secret or not hmac.compare_digest(provided_secret, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


def is_disposable_test_user(user: models.User | None) -> bool:
    return bool(
        user is not None
        and user.name.startswith(TEST_USER_PREFIX)
        and user.device_id.startswith(TEST_DEVICE_PREFIX)
        and user.id != 139
    )


def test_entitlement_for_circle(db: Session, circle_id: int, user_id: int) -> TestEntitlementFixture | None:
    fixture = db.query(TestEntitlementFixture).filter(
        TestEntitlementFixture.family_circle_id == circle_id,
        TestEntitlementFixture.organizer_user_id == user_id,
    ).first()
    if fixture is not None and fixture.expires_at <= _now():
        db.delete(fixture)
        db.commit()
        return None
    return fixture


def _assert_target(db: Session, payload: FixtureRequest) -> tuple[models.User, v2_models.FamilyCircle, v2_models.FamilyMembership]:
    user = db.get(models.User, payload.user_id)
    circle = db.get(v2_models.FamilyCircle, payload.family_circle_id)
    if not is_disposable_test_user(user) or circle is None:
        raise HTTPException(status_code=403, detail="test fixture target is not disposable")
    if circle.organizer_user_id != user.id:
        raise HTTPException(status_code=403, detail="test fixture target is not circle owner")
    membership = db.query(v2_models.FamilyMembership).filter(
        v2_models.FamilyMembership.family_circle_id == circle.id,
        v2_models.FamilyMembership.user_id == user.id,
        v2_models.FamilyMembership.role == "ORGANIZER",
        v2_models.FamilyMembership.membership_status == "active",
    ).first()
    if membership is None:
        raise HTTPException(status_code=403, detail="test fixture target is not organizer")
    real_entitlement = db.query(v2_models.PurchaseEntitlement).filter(
        (v2_models.PurchaseEntitlement.family_circle_id == circle.id)
        | (v2_models.PurchaseEntitlement.organizer_user_id == user.id)
    ).first()
    if real_entitlement is not None:
        raise HTTPException(status_code=409, detail="real entitlement exists for target")
    return user, circle, membership


def _assert_cleanup_target(
    db: Session, payload: FixtureCleanupRequest
) -> tuple[models.User, v2_models.FamilyCircle, v2_models.FamilyMembership]:
    """Allow cleanup of either disposable side of the isolated relationship.

    The organizer is protected by the test entitlement fixture; the parent is
    protected by an active membership in that same disposable circle.  In
    both cases the identity must be marked by the debug app and must have no
    retained production dependencies.
    """
    user = db.get(models.User, payload.user_id)
    circle = db.get(v2_models.FamilyCircle, payload.family_circle_id)
    if not is_disposable_test_user(user) or circle is None:
        raise HTTPException(status_code=403, detail="test fixture target is not disposable")
    membership = db.query(v2_models.FamilyMembership).filter(
        v2_models.FamilyMembership.family_circle_id == circle.id,
        v2_models.FamilyMembership.user_id == user.id,
        v2_models.FamilyMembership.membership_status == "active",
    ).first()
    if membership is None:
        raise HTTPException(status_code=403, detail="test fixture target is not a circle member")
    if membership.role == "ORGANIZER" and circle.organizer_user_id != user.id:
        raise HTTPException(status_code=403, detail="test fixture organizer mismatch")
    if membership.role != "ORGANIZER" and circle.organizer_user_id == user.id:
        raise HTTPException(status_code=403, detail="test fixture organizer membership missing")

    other_memberships = db.query(v2_models.FamilyMembership).filter(
        v2_models.FamilyMembership.user_id == user.id,
        v2_models.FamilyMembership.family_circle_id != circle.id,
    ).count()
    real_rows = db.query(v2_models.PurchaseEntitlement).filter(
        (v2_models.PurchaseEntitlement.family_circle_id == circle.id)
        | (v2_models.PurchaseEntitlement.organizer_user_id == user.id)
    ).count()
    if other_memberships or real_rows or user.id == 139:
        raise HTTPException(status_code=409, detail="target has retained dependencies")
    return user, circle, membership


def _assert_standalone_cleanup_target(db: Session, user_id: int) -> models.User:
    user = db.get(models.User, user_id)
    if not is_disposable_test_user(user):
        raise HTTPException(status_code=403, detail="test fixture target is not disposable")
    if db.query(v2_models.FamilyMembership).filter(v2_models.FamilyMembership.user_id == user_id).count():
        raise HTTPException(status_code=409, detail="target still has circle memberships")
    if db.query(v2_models.PurchaseEntitlement).filter(v2_models.PurchaseEntitlement.organizer_user_id == user_id).count():
        raise HTTPException(status_code=409, detail="target has retained dependencies")
    return user


def _delete_disposable_user_rows(db: Session, user_id: int) -> None:
    """Remove only user-scoped test rows after dependency checks."""
    profiles = db.query(v2_models.ParentProfile).filter(
        v2_models.ParentProfile.user_id == user_id
    ).all()
    for profile in profiles:
        db.query(v2_models.CheckInSchedule).filter(
            v2_models.CheckInSchedule.parent_profile_id == profile.id
        ).delete(synchronize_session=False)
        db.query(v2_models.CheckInEvent).filter(
            v2_models.CheckInEvent.parent_profile_id == profile.id
        ).delete(synchronize_session=False)
        db.query(v2_models.V2HelpRequest).filter(
            v2_models.V2HelpRequest.parent_profile_id == profile.id
        ).delete(synchronize_session=False)
        db.delete(profile)
    links = db.query(models.FamilyLink).filter(
        (models.FamilyLink.elder_user_id == user_id) | (models.FamilyLink.child_user_id == user_id)
    ).all()
    for link in links:
        other_id = link.child_user_id if link.elder_user_id == user_id else link.elder_user_id
        other = db.get(models.User, other_id)
        if not is_disposable_test_user(other):
            raise HTTPException(status_code=409, detail="test identity has retained legacy relationship")
        db.delete(link)
    db.query(models.BindCode).filter(models.BindCode.elder_user_id == user_id).delete(synchronize_session=False)
    db.query(models.BindAttempt).filter(models.BindAttempt.child_user_id == user_id).delete(synchronize_session=False)
    db.query(models.DailyCheckin).filter(models.DailyCheckin.elder_user_id == user_id).delete(synchronize_session=False)
    db.query(models.DevicePushToken).filter(models.DevicePushToken.user_id == user_id).delete(synchronize_session=False)
    db.query(models.DeviceStatus).filter(models.DeviceStatus.user_id == user_id).delete(synchronize_session=False)
    db.query(v2_models.V2NotificationDelivery).filter(
        v2_models.V2NotificationDelivery.recipient_user_id == user_id
    ).delete(synchronize_session=False)


@router.post("/entitlement")
def create_test_entitlement(payload: FixtureRequest, x_test_fixture_secret: str | None = Header(default=None), db: Session = Depends(get_db)):
    _require_access(x_test_fixture_secret)
    user, circle, _ = _assert_target(db, payload)
    existing = test_entitlement_for_circle(db, circle.id, user.id)
    if existing is not None:
        return {"fixture_id": existing.id, "state": TEST_FIXTURE_MARKER, "expires_at": existing.expires_at}
    if circle.status.upper() not in {"PENDING_PURCHASE", "ACTIVE"}:
        raise HTTPException(status_code=409, detail="circle is not fixture-eligible")
    fixture = TestEntitlementFixture(
        family_circle_id=circle.id,
        organizer_user_id=user.id,
        product_id="parent_checkin_family_lifetime",
        expires_at=_now() + timedelta(minutes=payload.expires_in_minutes),
    )
    db.add(fixture)
    circle.status = "ACTIVE"
    db.commit()
    db.refresh(fixture)
    return {"fixture_id": fixture.id, "state": TEST_FIXTURE_MARKER, "expires_at": fixture.expires_at}


@router.delete("/identities")
def cleanup_test_identity(payload: FixtureCleanupRequest, x_test_fixture_secret: str | None = Header(default=None), db: Session = Depends(get_db)):
    _require_access(x_test_fixture_secret)
    if payload.family_circle_id is None:
        user = _assert_standalone_cleanup_target(db, payload.user_id)
        _delete_disposable_user_rows(db, user.id)
        db.delete(user)
        db.commit()
        return {"success": True, "state": TEST_FIXTURE_MARKER, "user_id": payload.user_id, "family_circle_id": None}
    user, circle, membership = _assert_cleanup_target(db, payload)
    fixture = db.query(TestEntitlementFixture).filter(
        TestEntitlementFixture.family_circle_id == circle.id,
        TestEntitlementFixture.organizer_user_id == user.id,
    ).first()
    if membership.role == "ORGANIZER" and fixture is None:
        raise HTTPException(status_code=409, detail="test entitlement fixture missing")
    _delete_disposable_user_rows(db, user.id)
    if membership.role == "ORGANIZER":
        db.delete(fixture)
        db.query(v2_models.FamilyInvitation).filter(
            v2_models.FamilyInvitation.family_circle_id == circle.id
        ).delete(synchronize_session=False)
        db.query(v2_models.FamilyMembership).filter(
            v2_models.FamilyMembership.family_circle_id == circle.id
        ).delete(synchronize_session=False)
        db.delete(circle)
    else:
        db.delete(membership)
    db.delete(user)
    db.commit()
    return {"success": True, "state": TEST_FIXTURE_MARKER, "user_id": payload.user_id, "family_circle_id": payload.family_circle_id}
