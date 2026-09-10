from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class VerifyPurchaseRequest(BaseModel):
    family_circle_id: int | None = Field(default=None, gt=0)
    product_id: str = Field(min_length=1, max_length=128)
    purchase_token: str = Field(min_length=1, max_length=8192)
    obfuscated_account_id: str | None = Field(default=None, min_length=8, max_length=128)


class EntitlementResponse(BaseModel):
    family_circle_id: int
    organizer_user_id: int
    product_id: str
    purchase_state: str
    verification_state: str
    acknowledgement_state: str
    updated_at: datetime | None


class RtdnRequest(BaseModel):
    message_id: str = Field(min_length=1, max_length=255)
    notification_type: Literal["ONE_TIME_PRODUCT_PURCHASED", "ONE_TIME_PRODUCT_CANCELED"]
    purchase_token: str = Field(min_length=1, max_length=8192)
