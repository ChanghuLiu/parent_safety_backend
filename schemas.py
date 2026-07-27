from datetime import datetime, time
from typing import Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator
from typing_extensions import Annotated


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
PhoneText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
DeviceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=255)]
FcmToken = Annotated[str, StringConstraints(strip_whitespace=True, min_length=16, max_length=4096)]


class RegisterRequest(BaseModel):
    role: Literal["elder", "child"]
    name: ShortText
    phone: PhoneText
    device_id: DeviceId


class RegisterResponse(BaseModel):
    user_id: int
    role: str
    api_token: str


class UpdateFcmTokenRequest(BaseModel):
    user_id: int
    fcm_token: FcmToken


class SuccessResponse(BaseModel):
    success: bool


class UnbindRequest(BaseModel):
    family_link_id: int = Field(gt=0)


class ElderHeartbeatRequest(BaseModel):
    elder_user_id: int
    battery_level: int = Field(ge=0, le=100)


class UpdateAlertSettingsRequest(BaseModel):
    child_user_id: int
    elder_user_id: int
    offline_alert_enabled: bool
    offline_alert_hours: int = Field(ge=1, le=168)


class UpdateOfflineAlertSettingsRequest(BaseModel):
    child_user_id: int
    elder_user_id: int
    offline_alert_enabled: bool
    offline_alert_hours: int = Field(ge=1, le=168)
    quiet_hours_enabled: bool = True
    quiet_start_time: time = time(22, 0)
    quiet_end_time: time = time(7, 0)
    offline_pause_until: datetime | None = None

    @field_validator("quiet_start_time", "quiet_end_time")
    @classmethod
    def quiet_time_must_not_include_timezone(cls, value: time) -> time:
        if value.tzinfo is not None:
            raise ValueError("quiet time must be a local HH:MM value")
        return value


class CreateBindCodeRequest(BaseModel):
    elder_user_id: int


class CreateBindCodeResponse(BaseModel):
    code: str
    expires_in_minutes: int


class BindElderRequest(BaseModel):
    child_user_id: int
    code: str = Field(pattern=r"^\d{6}$")
    elder_relationship: ShortText
    elder_name: ShortText
    elder_phone: PhoneText


class BindElderResponse(BaseModel):
    success: bool
    family_link_id: int
    elder_user_id: int
    elder_relationship: str
    elder_name: str


class ElderCheckinRequest(BaseModel):
    elder_user_id: int
    battery_level: int = Field(ge=0, le=100)


class ElderCheckinResponse(BaseModel):
    success: bool
    message: str
    checkin_time: str


class ElderCheckinRecordResponse(BaseModel):
    event_type: Literal["checkin", "help_request"]
    record_date: str
    record_time: str
    message: str
    battery_level: int | None = None


class HelpRequestCreate(BaseModel):
    elder_user_id: int
    type: Literal["call_back", "not_feeling_well", "errand", "other"]
    message: str = Field(default="", max_length=500)


class HelpRequestResponse(BaseModel):
    success: bool
    message: str
    linked_children_count: int = 0
    children_with_fcm_token: int = 0
    notified_children: int = 0


class PendingHelpRequest(BaseModel):
    type: str
    message: str
    created_at: datetime


class ElderStatusResponse(BaseModel):
    family_link_id: int
    elder_user_id: int
    elder_relationship: str
    elder_name: str
    elder_phone: str
    today_status: str
    today_checkin_time: str | None
    battery_level: int | None
    last_online_time: str | None
    pending_help_request: PendingHelpRequest | None
    offline_alert_enabled: bool = True
    offline_alert_hours: int = 6
    offline_quiet_hours_enabled: bool = True
    offline_quiet_start_time: str = "22:00"
    offline_quiet_end_time: str = "07:00"
    offline_pause_until: str | None = None
    offline_alert_exceeded: bool = False


class HealthResponse(BaseModel):
    status: str
