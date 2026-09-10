from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("device_id", "role", name="uq_users_device_role"),
    )

    id = Column(Integer, primary_key=True, index=True)
    role = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    device_id = Column(String, nullable=False, index=True)
    fcm_token = Column(String, nullable=True)
    fcm_token_invalidated_at = Column(DateTime, nullable=True)
    api_token_hash = Column(String(64), nullable=True, unique=True, index=True)
    # Device-local UI/notification language.  This is deliberately attached
    # to the authenticated user/device, never to a family relationship.
    locale_tag = Column(String(32), nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class DevicePushToken(Base):
    __tablename__ = "device_push_tokens"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "push_provider",
            name="uq_device_push_tokens_user_provider",
        ),
        UniqueConstraint(
            "push_provider",
            "push_token",
            name="uq_device_push_tokens_provider_token",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    platform = Column(String(32), nullable=False)
    push_provider = Column(String(32), nullable=False)
    push_token = Column(String(4096), nullable=False)
    push_token_updated_at = Column(DateTime, nullable=False)
    push_token_invalidated_at = Column(DateTime, nullable=True)

    user = relationship("User")


class DeletedApiToken(Base):
    __tablename__ = "deleted_api_tokens"

    token_hash = Column(String(64), primary_key=True)
    deleted_at = Column(DateTime, nullable=False, server_default=func.now())


class BindCode(Base):
    __tablename__ = "bind_codes"

    id = Column(Integer, primary_key=True, index=True)
    elder_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    code = Column(String(6), nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    elder = relationship("User")


class BindAttempt(Base):
    __tablename__ = "bind_attempts"

    id = Column(Integer, primary_key=True)
    child_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    attempted_at = Column(DateTime, nullable=False, index=True)


class FamilyLink(Base):
    __tablename__ = "family_links"
    __table_args__ = (
        UniqueConstraint("elder_user_id", "child_user_id", name="uq_family_link_pair"),
    )

    id = Column(Integer, primary_key=True, index=True)
    elder_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    child_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    elder_relationship = Column(String, nullable=False)
    elder_name = Column(String, nullable=False)
    elder_phone = Column(String, nullable=False)
    offline_alert_enabled = Column(Boolean, nullable=False, default=True)
    offline_alert_hours = Column(Integer, nullable=False, default=6)
    offline_quiet_hours_enabled = Column(Boolean, nullable=False, default=True)
    offline_quiet_start_time = Column(String, nullable=False, default="22:00")
    offline_quiet_end_time = Column(String, nullable=False, default="07:00")
    offline_pause_until = Column(String, nullable=True)
    last_offline_alert_sent_at = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    elder = relationship("User", foreign_keys=[elder_user_id])
    child = relationship("User", foreign_keys=[child_user_id])


class DailyCheckin(Base):
    __tablename__ = "daily_checkins"
    __table_args__ = (
        Index(
            "uq_daily_checkins_elder_date",
            "elder_user_id",
            "checkin_date",
            unique=True,
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    elder_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    checkin_date = Column(Date, nullable=False, index=True)
    checkin_time = Column(String(5), nullable=False)
    battery_level = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    elder = relationship("User")


class DeviceStatus(Base):
    __tablename__ = "device_status"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    platform = Column(String, nullable=True)
    app_version = Column(String, nullable=True)
    device_uuid = Column(String, nullable=True)
    battery_level = Column(Integer, nullable=True)
    last_online_time = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    user = relationship("User")


class HelpRequest(Base):
    __tablename__ = "help_requests"

    id = Column(Integer, primary_key=True, index=True)
    elder_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    type = Column(String, nullable=False)
    message = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending", index=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    elder = relationship("User")
