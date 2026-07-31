from datetime import datetime, time
from typing import Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator
from typing_extensions import Annotated


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
PhoneText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
DeviceId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=8, max_length=255)]
FcmToken = Annotated[str, StringConstraints(strip_whitespace=True, min_length=16, max_length=4096)]
PushToken = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
PlatformText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
AppVersionText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


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
    user_id: int | None = Field(default=None, gt=0)
    device_id: DeviceId | None = None
    platform: PlatformText | None = None
    push_provider: PlatformText | None = None
    push_token: PushToken | None = None
    app_version: AppVersionText | None = None
    # Kept only for existing Android clients. It is normalized to push_token
    # before persistence and is never treated as FCM for another provider.
    fcm_token: PushToken | None = None

    @model_validator(mode="after")
    def validate_identity_and_token(self):
        if self.user_id is None and self.device_id is None:
            raise ValueError("device_id or legacy user_id is required")
        if self.push_token is None and self.fcm_token is None:
            raise ValueError("push_token is required")
        if self.push_token is not None and (
            self.device_id is None
            or self.platform is None
            or self.push_provider is None
        ):
            raise ValueError(
                "device_id, platform, and push_provider are required with push_token"
            )
        if (
            self.push_token is not None
            and self.fcm_token is not None
            and self.push_token != self.fcm_token
        ):
            raise ValueError("push_token and fcm_token must match when both are supplied")
        return self

    @property
    def effective_push_token(self) -> str:
        return self.push_token or self.fcm_token or ""


class PushTokenRegistrationResponse(BaseModel):
    success: bool
    registered: bool
    platform: str
    push_provider: str
    token_updated_at: datetime
    token_changed: bool


class SuccessResponse(BaseModel):
    success: bool


class UnbindRequest(BaseModel):
    family_link_id: int = Field(gt=0)


class ElderHeartbeatRequest(BaseModel):
    elder_user_id: int
    battery_level: int = Field(ge=0, le=100)
    platform: PlatformText | None = None
    app_version: AppVersionText | None = None
    role: Literal["elder"] | None = None
    device_uuid: DeviceId | None = None
    last_online: datetime | None = None


class DeviceStatusUpdateResponse(BaseModel):
    success: bool


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
    checkin_id: int
    checkin_date: str


class ElderCheckinRecordResponse(BaseModel):
    event_type: Literal["checkin", "help_request"]
    record_date: str
    record_time: str
    message: str
    battery_level: int | None = None
    alert_id: int | None = None
    alert_type: str | None = None
    status: str | None = None
    elder_user_id: int | None = None
    parent_device_id: str | None = None
    family_link_id: int | None = None


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
    alert_id: int
    alert_type: str
    created_at: datetime
    status: str
    elder_user_id: int
    parent_device_id: str
    family_link_id: int | None
    family_link_ids: list[int]


class PendingHelpRequest(BaseModel):
    alert_id: int
    alert_type: str
    type: str
    message: str
    created_at: datetime
    status: str
    elder_user_id: int
    parent_device_id: str
    family_link_id: int


class HelpAlertActionRequest(BaseModel):
    child_user_id: int = Field(gt=0)


class HelpAlertResponse(BaseModel):
    alert_id: int
    alert_type: str
    type: str
    message: str
    created_at: datetime
    status: str
    elder_user_id: int
    parent_device_id: str
    family_link_id: int


class FamilyLinkIdentifier(BaseModel):
    family_link_id: int
    child_user_id: int


class ParentCurrentStatusResponse(BaseModel):
    parent_user_id: int
    parent_device_id: str
    role: Literal["elder"]
    safety_status: Literal["normal", "need_confirm"]
    last_safety_confirmation_time: datetime | None
    battery_level: int | None
    last_online: datetime | None
    active_help_status: str | None
    active_help_alert: HelpAlertResponse | None
    family_link_id: int | None
    family_link_ids: list[int]
    current_family_links: list[FamilyLinkIdentifier]


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


class ErrorResponse(BaseModel):
    detail: str
