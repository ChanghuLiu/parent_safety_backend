"""Neutral Parent Check-In V2 persistence models.

The legacy tables remain intact while V2 is developed against a clean,
explicitly named table set.  This lets the test database be recreated without
silently rewriting the historical API's data model.
"""

from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship as sa_relationship
from sqlalchemy.sql import func

from database import Base


class FamilyCircle(Base):
    __tablename__ = "v2_family_circles"

    id = Column(Integer, primary_key=True, index=True)
    organizer_user_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    status = Column(String(24), nullable=False, default="PENDING_PURCHASE", index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    organizer = sa_relationship("User", foreign_keys=[organizer_user_id])


class PurchaseEntitlement(Base):
    __tablename__ = "v2_purchase_entitlements"
    __table_args__ = (
        UniqueConstraint("family_circle_id", name="uq_v2_entitlement_circle"),
        UniqueConstraint("purchase_token_hash", name="uq_v2_entitlement_token"),
        Index("ix_v2_entitlement_organizer_state", "organizer_user_id", "verification_state"),
    )

    id = Column(Integer, primary_key=True, index=True)
    family_circle_id = Column(Integer, ForeignKey("v2_family_circles.id", ondelete="CASCADE"), nullable=False)
    organizer_user_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    product_id = Column(String(128), nullable=False)
    package_name = Column(String(255), nullable=False)
    purchase_token_hash = Column(String(64), nullable=False)
    purchase_token_ciphertext = Column(String(8192), nullable=True)
    purchase_state = Column(String(24), nullable=False, default="PENDING", index=True)
    verification_state = Column(String(24), nullable=False, default="PENDING", index=True)
    acknowledgement_state = Column(String(24), nullable=False, default="PENDING")
    source = Column(String(64), nullable=False, default="google_verify")
    obfuscated_account_hash = Column(String(64), nullable=True)
    purchased_at = Column(DateTime, nullable=True)
    verified_at = Column(DateTime, nullable=True)
    last_verified_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class BillingAuditEvent(Base):
    """Safe, non-secret audit trail for server-authorized billing changes."""

    __tablename__ = "v2_billing_audit_events"
    __table_args__ = (
        Index("ix_v2_billing_audit_user_created", "user_id", "created_at"),
        Index("ix_v2_billing_audit_token_result", "purchase_token_hash", "result"),
    )

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String(64), nullable=False)
    source = Column(String(64), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    entitlement_id = Column(Integer, ForeignKey("v2_purchase_entitlements.id", ondelete="RESTRICT"), nullable=True, index=True)
    family_circle_id = Column(Integer, ForeignKey("v2_family_circles.id", ondelete="RESTRICT"), nullable=True, index=True)
    product_id = Column(String(128), nullable=False)
    purchase_token_hash = Column(String(64), nullable=False)
    result = Column(String(24), nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class BillingRtdnEvent(Base):
    __tablename__ = "v2_billing_rtdn_events"
    __table_args__ = (UniqueConstraint("message_id", name="uq_v2_rtdn_message"),)

    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(String(255), nullable=False)
    notification_type = Column(String(64), nullable=False)
    purchase_token_hash = Column(String(64), nullable=False, index=True)
    received_at = Column(DateTime, nullable=False, server_default=func.now())
    processed_at = Column(DateTime, nullable=True)
    status = Column(String(24), nullable=False, default="received")


class FamilyMembership(Base):
    __tablename__ = "v2_family_memberships"
    __table_args__ = (
        UniqueConstraint("family_circle_id", "user_id", name="uq_v2_circle_user"),
        Index("ix_v2_membership_circle_status", "family_circle_id", "membership_status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    family_circle_id = Column(Integer, ForeignKey("v2_family_circles.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(24), nullable=False, index=True)
    relationship = Column(String(64), nullable=True)
    membership_status = Column(String(24), nullable=False, default="active", index=True)
    joined_at = Column(DateTime, nullable=False, server_default=func.now())

    circle = sa_relationship("FamilyCircle", backref="memberships")
    user = sa_relationship("User")


class RelationshipPresentation(Base):
    """Viewer-specific presentation for a linked family member.

    Names and avatars here are intentionally not account identity fields: each
    member may choose how the other member appears on their own device.
    """
    __tablename__ = "v2_relationship_presentations"
    __table_args__ = (UniqueConstraint("family_circle_id", "viewer_user_id", "subject_user_id", name="uq_v2_relationship_presentation"),)

    id = Column(Integer, primary_key=True, index=True)
    family_circle_id = Column(Integer, ForeignKey("v2_family_circles.id", ondelete="CASCADE"), nullable=False, index=True)
    viewer_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    subject_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    display_name = Column(String(100), nullable=True)
    avatar_url = Column(String(2048), nullable=True)

    circle = sa_relationship("FamilyCircle")
    viewer = sa_relationship("User", foreign_keys=[viewer_user_id])
    subject = sa_relationship("User", foreign_keys=[subject_user_id])


class ParentProfile(Base):
    __tablename__ = "v2_parent_profiles"
    __table_args__ = (
        UniqueConstraint("family_circle_id", "user_id", name="uq_v2_circle_parent"),
        Index("ix_v2_parent_profile_active", "family_circle_id", "active"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    family_circle_id = Column(Integer, ForeignKey("v2_family_circles.id", ondelete="CASCADE"), nullable=False)
    display_name = Column(String(100), nullable=False)
    timezone = Column(String(64), nullable=False, default="UTC")
    active = Column(Boolean, nullable=False, default=True, index=True)
    # Links replacement history without moving historical events away from
    # the original Parent profile.
    recovered_from_parent_profile_id = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    user = sa_relationship("User")
    circle = sa_relationship("FamilyCircle")


class OfflineAlertSetting(Base):
    """V3 adapter for the proven offline/quiet/pause policy.

    Legacy family_links remain the source for legacy relationships. V2 has a
    different relationship table, so this stores the same policy fields for
    a V2 circle without altering the legacy schema or worker semantics.
    """

    __tablename__ = "v2_offline_alert_settings"
    __table_args__ = (UniqueConstraint("family_circle_id", name="uq_v2_offline_setting_circle"),)

    id = Column(Integer, primary_key=True, index=True)
    family_circle_id = Column(Integer, ForeignKey("v2_family_circles.id", ondelete="CASCADE"), nullable=False)
    offline_alert_enabled = Column(Boolean, nullable=False, default=True)
    offline_alert_hours = Column(Integer, nullable=False, default=6)
    quiet_hours_enabled = Column(Boolean, nullable=False, default=True)
    quiet_start_time = Column(String(5), nullable=False, default="22:00")
    quiet_end_time = Column(String(5), nullable=False, default="07:00")
    pause_until = Column(String, nullable=True)
    last_alert_sent_at = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class CheckInSchedule(Base):
    __tablename__ = "v2_checkin_schedules"
    __table_args__ = (
        Index("ix_v2_schedule_parent_enabled", "parent_profile_id", "enabled"),
    )

    id = Column(Integer, primary_key=True, index=True)
    parent_profile_id = Column(Integer, ForeignKey("v2_parent_profiles.id", ondelete="CASCADE"), nullable=False)
    local_time = Column(String(5), nullable=False)
    timezone = Column(String(64), nullable=False)
    grace_period_minutes = Column(Integer, nullable=False, default=30)
    days_of_week = Column(String(20), nullable=False, default="0,1,2,3,4,5,6")
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    parent_profile = sa_relationship("ParentProfile")


class CheckInEvent(Base):
    __tablename__ = "v2_checkin_events"
    __table_args__ = (
        UniqueConstraint("parent_profile_id", "schedule_id", "scheduled_for_utc", name="uq_v2_schedule_window"),
        Index("ix_v2_event_parent_local_date", "parent_profile_id", "local_date"),
        Index("ix_v2_event_status_scheduled", "status", "scheduled_for_utc"),
    )

    id = Column(Integer, primary_key=True, index=True)
    parent_profile_id = Column(Integer, ForeignKey("v2_parent_profiles.id", ondelete="CASCADE"), nullable=False)
    schedule_id = Column(Integer, ForeignKey("v2_checkin_schedules.id", ondelete="SET NULL"), nullable=True)
    scheduled_for_utc = Column(DateTime, nullable=False)
    occurred_at = Column(DateTime, nullable=True)
    local_date = Column(Date, nullable=False)
    event_type = Column(String(24), nullable=False, default="scheduled")
    status = Column(String(32), nullable=False, default="UPCOMING", index=True)
    escalation_state = Column(String(24), nullable=False, default="NONE", index=True)
    current_escalation_priority = Column(Integer, nullable=True)
    battery_level = Column(Integer, nullable=True)
    idempotency_key = Column(String(128), nullable=True, unique=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    parent_profile = sa_relationship("ParentProfile")
    schedule = sa_relationship("CheckInSchedule")


class FamilyInvitation(Base):
    __tablename__ = "v2_family_invitations"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_v2_invitation_token"),
        UniqueConstraint("public_code_hash", name="uq_v2_invitation_public_code"),
        Index("ix_v2_invitation_circle_status", "family_circle_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    token_hash = Column(String(64), nullable=False)
    # The six-digit code is a separate, human-readable lookup credential.  It
    # never replaces the high-entropy legacy token and is hashed at rest.
    public_code_hash = Column(String(64), nullable=True)
    family_circle_id = Column(Integer, ForeignKey("v2_family_circles.id", ondelete="CASCADE"), nullable=False)
    invited_role = Column(String(24), nullable=False)
    invited_relationship = Column(String(64), nullable=True)
    invited_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    accepted_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    status = Column(String(24), nullable=False, default="pending", index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    accepted_at = Column(DateTime, nullable=True)

    circle = sa_relationship("FamilyCircle")


class V2InvitationAttempt(Base):
    __tablename__ = "v2_invitation_attempts"
    __table_args__ = (
        Index("ix_v2_invitation_attempt_user_time", "user_id", "attempted_at"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    attempted_at = Column(DateTime, nullable=False, index=True)


class EscalationRule(Base):
    __tablename__ = "v2_escalation_rules"
    __table_args__ = (
        UniqueConstraint("family_circle_id", "family_member_id", "priority", name="uq_v2_escalation_target"),
        Index("ix_v2_escalation_circle_enabled", "family_circle_id", "enabled", "priority"),
    )

    id = Column(Integer, primary_key=True, index=True)
    family_circle_id = Column(Integer, ForeignKey("v2_family_circles.id", ondelete="CASCADE"), nullable=False)
    family_member_id = Column(Integer, ForeignKey("v2_family_memberships.id", ondelete="CASCADE"), nullable=False)
    priority = Column(Integer, nullable=False)
    delay_minutes = Column(Integer, nullable=False, default=30)
    enabled = Column(Boolean, nullable=False, default=True)


class V2HelpRequest(Base):
    __tablename__ = "v2_help_requests"

    id = Column(Integer, primary_key=True, index=True)
    family_circle_id = Column(Integer, ForeignKey("v2_family_circles.id", ondelete="CASCADE"), nullable=False, index=True)
    parent_profile_id = Column(Integer, ForeignKey("v2_parent_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    request_type = Column(String(32), nullable=False)
    message = Column(String(500), nullable=False, default="")
    status = Column(String(24), nullable=False, default="created", index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    acknowledged_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    acknowledged_by_membership_id = Column(Integer, ForeignKey("v2_family_memberships.id", ondelete="SET NULL"), nullable=True)


class V2NotificationDelivery(Base):
    """Durable idempotency ledger for logical notification events."""

    __tablename__ = "v2_notification_deliveries"
    __table_args__ = (
        UniqueConstraint("event_key", "recipient_user_id", name="uq_v2_notification_recipient"),
        Index("ix_v2_notification_status", "status", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    event_key = Column(String(160), nullable=False)
    recipient_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    locale_tag = Column(String(32), nullable=False, default="en")
    provider = Column(String(32), nullable=True)
    title = Column(String(160), nullable=False)
    body = Column(String(500), nullable=False)
    status = Column(String(24), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    delivered_at = Column(DateTime, nullable=True)
