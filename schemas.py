from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    role: Literal["elder", "child"]
    name: str
    phone: str
    device_id: str


class RegisterResponse(BaseModel):
    user_id: int
    role: str


class UpdateFcmTokenRequest(BaseModel):
    user_id: int
    fcm_token: str


class SuccessResponse(BaseModel):
    success: bool


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
    quiet_start_time: str = "22:00"
    quiet_end_time: str = "07:00"
    offline_pause_until: str | None = None


class CreateBindCodeRequest(BaseModel):
    elder_user_id: int


class CreateBindCodeResponse(BaseModel):
    code: str
    expires_in_minutes: int


class BindElderRequest(BaseModel):
    child_user_id: int
    code: str = Field(min_length=6, max_length=6)
    elder_relationship: str
    elder_name: str
    elder_phone: str


class BindElderResponse(BaseModel):
    success: bool
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


class HelpRequestCreate(BaseModel):
    elder_user_id: int
    type: str
    message: str = ""


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
