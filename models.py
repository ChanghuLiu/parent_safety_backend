from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    role = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    device_id = Column(String, nullable=False, index=True)
    fcm_token = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class BindCode(Base):
    __tablename__ = "bind_codes"

    id = Column(Integer, primary_key=True, index=True)
    elder_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    code = Column(String(6), nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, server_default=func.now())

    elder = relationship("User")


class FamilyLink(Base):
    __tablename__ = "family_links"

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
