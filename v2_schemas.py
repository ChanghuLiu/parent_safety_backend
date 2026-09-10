from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from v2_time import canonical_timezone


Role = Literal["PARENT", "FAMILY_MEMBER"]
InvitationRole = Literal["PARENT", "FAMILY_MEMBER"]


class V2RegisterRequest(BaseModel):
    role: Role
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(default="", max_length=32)
    device_id: str = Field(min_length=8, max_length=255)
    locale_tag: str = Field(default="en", min_length=2, max_length=32)


class V2RegisterResponse(BaseModel):
    user_id: int
    role: Role
    api_token: str
    locale_tag: str


class V2CurrentUserResponse(BaseModel):
    user_id: int
    role: Role
    name: str
    locale_tag: str


class CircleCreateRequest(BaseModel):
    name: str = Field(default="Family Circle", min_length=1, max_length=100)


class CircleResponse(BaseModel):
    id: int
    organizer_user_id: int
    status: str
    member_count: int
    organizer_name: str


class MemberResponse(BaseModel):
    membership_id: int
    user_id: int
    display_name: str
    role: str
    relationship: str | None
    phone: str | None = None
    membership_status: str


class InvitationCreateRequest(BaseModel):
    role: InvitationRole
    relationship: str | None = Field(default=None, max_length=64)
    expires_in_hours: int = Field(default=72, ge=1, le=720)


class InvitationResponse(BaseModel):
    invitation_id: int
    token: str
    role: InvitationRole
    expires_at: datetime


class InvitationAcceptResponse(BaseModel):
    membership_id: int
    family_circle_id: int
    role: InvitationRole
    parent_profile_id: int | None = None


class ScheduleRequest(BaseModel):
    local_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(min_length=1, max_length=64)
    grace_period_minutes: int = Field(default=30, ge=1, le=1440)
    days_of_week: str = "0,1,2,3,4,5,6"
    enabled: bool = True

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        return canonical_timezone(value)


class ScheduleResponse(ScheduleRequest):
    id: int
    parent_profile_id: int


class CheckInRequest(BaseModel):
    battery_level: int | None = Field(default=None, ge=0, le=100)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)
    schedule_id: int | None = Field(default=None, gt=0)


class CheckInResponse(BaseModel):
    event_id: int
    status: str
    occurred_at_utc: datetime
    local_date: str
    duplicate: bool = False


class ParentStatusResponse(BaseModel):
    parent_profile_id: int
    parent_user_id: int
    display_name: str
    phone: str | None
    timezone: str
    state: str
    last_checkin_utc: datetime | None
    last_checkin_local: str | None
    next_checkin_local: str | None
    battery_level: int | None
    last_online_utc: datetime | None


class HistoryItemResponse(BaseModel):
    event_id: int
    local_date: str
    occurred_at_utc: datetime | None
    status: str
    battery_level: int | None


class DeviceStatusRequest(BaseModel):
    device_id: str = Field(min_length=8, max_length=255)
    platform: str = Field(default="android", min_length=1, max_length=32)
    app_version: str | None = Field(default=None, max_length=64)
    battery_level: int | None = Field(default=None, ge=0, le=100)
    locale_tag: str = Field(default="en", min_length=2, max_length=32)


class PushTokenRequest(BaseModel):
    device_id: str = Field(min_length=8, max_length=255)
    platform: str = Field(default="android", min_length=1, max_length=32)
    push_provider: Literal["fcm", "hms", "harmonyos"]
    push_token: str = Field(min_length=1, max_length=4096)


class HelpRequestCreate(BaseModel):
    request_type: Literal["call_back", "not_feeling_well", "errand", "other"]
    message: str = Field(default="", max_length=500)


class HelpRequestResponse(BaseModel):
    id: int
    status: str
    created_at: datetime


class ActionResponse(BaseModel):
    success: bool
    status: str | None = None


class EscalationRuleRequest(BaseModel):
    family_member_id: int = Field(gt=0)
    priority: int = Field(ge=1, le=20)
    delay_minutes: int = Field(default=30, ge=1, le=10080)
    enabled: bool = True
