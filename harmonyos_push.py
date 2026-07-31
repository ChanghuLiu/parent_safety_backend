from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Callable, Mapping
from urllib.parse import urlparse

import httpx
import jwt


HARMONYOS_PUSH_AUDIENCE = "https://oauth-login.cloud.huawei.com/oauth2/v3/token"
HARMONYOS_PUSH_APP_ID = "6917612346718148708"
HARMONYOS_PUSH_DEFAULT_API_URL = (
    "https://push-api.cloud.huawei.com/v3/{project_id}/messages:send"
)
HARMONYOS_PUSH_JWT_LIFETIME_SECONDS = 3600
HARMONYOS_PUSH_JWT_REFRESH_SKEW_SECONDS = 300
HARMONYOS_PUSH_MAX_MESSAGE_BYTES = 4096
HARMONYOS_PUSH_SUCCESS_CODE = "80000000"
HARMONYOS_PUSH_PARTIAL_SUCCESS_CODE = "80100000"
HARMONYOS_PUSH_INVALID_TOKEN_CODE = "80300007"
HARMONYOS_PUSH_JWT_EXPIRED_CODE = "80200005"
HARMONYOS_PUSH_RATE_LIMIT_CODES = {"80300029"}
HARMONYOS_PUSH_TEMPORARY_CODES = {"81000001"}
HARMONYOS_PUSH_PERMANENT_TOKEN_REASONS = {
    "decryptError",
    "tokenFormatError",
}


class HarmonyOSPushConfigurationError(Exception):
    pass


class DeliveryStatus(str, Enum):
    ACCEPTED = "accepted"
    INVALID_TOKEN = "invalid_token"
    AUTHENTICATION_FAILURE = "authentication_failure"
    PROVIDER_REJECTED = "provider_rejected"
    RATE_LIMITED = "rate_limited"
    TEMPORARY_FAILURE = "temporary_failure"
    DELIVERY_DISABLED = "delivery_disabled"
    UNSUPPORTED_PROVIDER = "unsupported_provider"
    NO_TOKEN = "no_token"


@dataclass(frozen=True)
class HarmonyOSServiceAccount:
    key_id: str
    private_key: str
    sub_account: str
    project_id: str | None = None


@dataclass(frozen=True)
class HarmonyOSNotification:
    title: str
    body: str
    category: str
    event_type: str
    target_page: str
    family_link_id: int | None = None
    alert_id: int | None = None

    def routing_data(self) -> dict[str, str]:
        data = {
            "event_type": self.event_type,
            "target_page": self.target_page,
        }
        if self.family_link_id is not None:
            data["family_link_id"] = str(self.family_link_id)
        if self.alert_id is not None:
            data["alert_id"] = str(self.alert_id)
        return data


@dataclass(frozen=True)
class DeliveryResult:
    status: DeliveryStatus
    http_status: int | None = None
    provider_code: str | None = None
    request_id: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == DeliveryStatus.ACCEPTED


def _enabled(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _required_text(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HarmonyOSPushConfigurationError(
            f"HarmonyOS Push service account is missing {key}"
        )
    return value.strip()


def _optional_text(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HarmonyOSPushConfigurationError(
            f"HarmonyOS Push service account has invalid {key}"
        )
    return value.strip() or None


def _load_service_account_file(path_value: str) -> dict[str, object]:
    path = Path(path_value).expanduser()
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise HarmonyOSPushConfigurationError(
            "HarmonyOS Push service-account file cannot be read"
        ) from exc
    if mode & 0o077:
        raise HarmonyOSPushConfigurationError(
            "HarmonyOS Push service-account file must not be group/world accessible"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarmonyOSPushConfigurationError(
            "HarmonyOS Push service-account file is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise HarmonyOSPushConfigurationError(
            "HarmonyOS Push service-account file must contain a JSON object"
        )
    return value


def load_harmonyos_service_account(
    environ: Mapping[str, str] | None = None,
) -> HarmonyOSServiceAccount:
    env = environ if environ is not None else os.environ
    file_path = env.get("HARMONYOS_PUSH_SERVICE_ACCOUNT_FILE", "").strip()
    if file_path:
        values = _load_service_account_file(file_path)
        configured_project = env.get("HARMONYOS_PUSH_PROJECT_ID", "").strip()
        file_project = _optional_text(values, "project_id")
        if (
            configured_project
            and file_project
            and configured_project != file_project
        ):
            raise HarmonyOSPushConfigurationError(
                "HarmonyOS Push project ID does not match the service-account file"
            )
        return HarmonyOSServiceAccount(
            key_id=_required_text(values, "key_id"),
            private_key=_required_text(values, "private_key"),
            sub_account=_required_text(values, "sub_account"),
            project_id=configured_project or file_project,
        )

    required = {
        "key_id": env.get("HARMONYOS_PUSH_KEY_ID", ""),
        "private_key": env.get("HARMONYOS_PUSH_PRIVATE_KEY", ""),
        "sub_account": env.get("HARMONYOS_PUSH_SERVICE_ACCOUNT_ID", ""),
    }
    account = HarmonyOSServiceAccount(
        key_id=_required_text(required, "key_id"),
        private_key=_required_text(required, "private_key").replace("\\n", "\n"),
        sub_account=_required_text(required, "sub_account"),
        project_id=env.get("HARMONYOS_PUSH_PROJECT_ID", "").strip() or None,
    )
    return account


def validate_harmonyos_push_app_id(
    environ: Mapping[str, str] | None = None,
) -> str:
    env = environ if environ is not None else os.environ
    app_id = env.get("HARMONYOS_PUSH_APP_ID", "").strip()
    if app_id != HARMONYOS_PUSH_APP_ID:
        raise HarmonyOSPushConfigurationError(
            "HarmonyOS Push APP ID is missing or does not match this application"
        )
    return app_id


class HarmonyOSJWTAuthenticator:
    def __init__(
        self,
        credentials_loader: Callable[[], HarmonyOSServiceAccount] = (
            load_harmonyos_service_account
        ),
        clock: Callable[[], float] = time.time,
        lifetime_seconds: int = HARMONYOS_PUSH_JWT_LIFETIME_SECONDS,
        refresh_skew_seconds: int = HARMONYOS_PUSH_JWT_REFRESH_SKEW_SECONDS,
    ):
        self._credentials_loader = credentials_loader
        self._clock = clock
        self._lifetime_seconds = lifetime_seconds
        self._refresh_skew_seconds = refresh_skew_seconds
        self._cached_token: str | None = None
        self._cached_credentials: HarmonyOSServiceAccount | None = None
        self._expires_at = 0
        self._lock = threading.Lock()

    def _create_jwt(
        self,
        credentials: HarmonyOSServiceAccount,
        issued_at: int,
        expires_at: int,
    ) -> str:
        try:
            return jwt.encode(
                {
                    "aud": HARMONYOS_PUSH_AUDIENCE,
                    "iss": credentials.sub_account,
                    "iat": issued_at,
                    "exp": expires_at,
                },
                credentials.private_key,
                algorithm="PS256",
                headers={
                    "kid": credentials.key_id,
                    "typ": "JWT",
                    "alg": "PS256",
                },
            )
        except Exception as exc:
            raise HarmonyOSPushConfigurationError(
                "HarmonyOS Push service-account private key is invalid"
            ) from exc

    def get_jwt(self) -> tuple[str, HarmonyOSServiceAccount]:
        now = int(self._clock())
        if (
            self._cached_token is not None
            and self._cached_credentials is not None
            and now < self._expires_at - self._refresh_skew_seconds
        ):
            return self._cached_token, self._cached_credentials

        with self._lock:
            now = int(self._clock())
            credentials = self._credentials_loader()
            if (
                self._cached_token is not None
                and self._cached_credentials is not None
                and now < self._expires_at - self._refresh_skew_seconds
            ):
                return self._cached_token, self._cached_credentials
            expires_at = now + self._lifetime_seconds
            token = self._create_jwt(credentials, now, expires_at)
            self._cached_token = token
            self._cached_credentials = credentials
            self._expires_at = expires_at
            return token, credentials

    def invalidate_cache(self) -> None:
        with self._lock:
            self._cached_token = None
            self._cached_credentials = None
            self._expires_at = 0


class HarmonyOSPushProvider:
    def __init__(
        self,
        authenticator: HarmonyOSJWTAuthenticator | None = None,
        http_client: httpx.Client | None = None,
        api_url: str | None = None,
        timeout_seconds: float = 10.0,
    ):
        self._authenticator = authenticator or HarmonyOSJWTAuthenticator()
        self._http_client = http_client or httpx.Client()
        self._api_url = api_url
        self._timeout_seconds = timeout_seconds

    def _delivery_enabled(self) -> bool:
        return _enabled("PUSH_DELIVERY_ENABLED", True) and _enabled(
            "HARMONYOS_PUSH_ENABLED", False
        )

    def api_endpoint_preview(self, project_id: str | None) -> str:
        configured = self._api_url or os.getenv(
            "HARMONYOS_PUSH_API_URL", ""
        ).strip()
        if not configured:
            configured = HARMONYOS_PUSH_DEFAULT_API_URL
        if "{project_id}" in configured and project_id:
            endpoint = configured.replace("{project_id}", project_id)
        else:
            endpoint = configured
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise HarmonyOSPushConfigurationError(
                "HarmonyOS Push API URL must be an HTTPS URL"
            )
        return endpoint

    def api_endpoint(self, project_id: str | None) -> str:
        endpoint = self.api_endpoint_preview(project_id)
        if "{project_id}" in endpoint:
            raise HarmonyOSPushConfigurationError(
                "HarmonyOS Push project ID is required by the API URL"
            )
        return endpoint

    @staticmethod
    def _validate_notification(notification: HarmonyOSNotification) -> None:
        required_text = {
            "category": notification.category,
            "title": notification.title,
            "body": notification.body,
            "event_type": notification.event_type,
            "target_page": notification.target_page,
        }
        for field, value in required_text.items():
            if not isinstance(value, str) or not value.strip():
                raise HarmonyOSPushConfigurationError(
                    f"HarmonyOS Push notification is missing {field}"
                )
            if "\x00" in value:
                raise HarmonyOSPushConfigurationError(
                    f"HarmonyOS Push notification has invalid {field}"
                )
        safe_metadata = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
        for field in ("event_type", "target_page"):
            if not safe_metadata.fullmatch(required_text[field]):
                raise HarmonyOSPushConfigurationError(
                    f"HarmonyOS Push notification has invalid {field}"
                )

    def build_request_body(
        self,
        token: str,
        notification: HarmonyOSNotification,
    ) -> dict[str, object]:
        if not isinstance(token, str) or not token.strip():
            raise HarmonyOSPushConfigurationError(
                "HarmonyOS Push token is required"
            )
        self._validate_notification(notification)
        body: dict[str, object] = {
            "payload": {
                "notification": {
                    "category": notification.category,
                    "title": notification.title,
                    "body": notification.body,
                    "clickAction": {
                        "actionType": 0,
                        "data": notification.routing_data(),
                    },
                }
            },
            "target": {"token": [token]},
        }
        if _enabled("HARMONYOS_PUSH_TEST_MESSAGE", False):
            body["pushOptions"] = {"testMessage": True}
        size_check_body = dict(body)
        size_check_body["target"] = {"token": []}
        message_size = len(
            json.dumps(
                size_check_body,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        if message_size > HARMONYOS_PUSH_MAX_MESSAGE_BYTES:
            raise HarmonyOSPushConfigurationError(
                "HarmonyOS Push message exceeds the documented size limit"
            )
        return body

    @staticmethod
    def _response_json(response: httpx.Response) -> dict[str, object]:
        try:
            value = response.json()
        except (ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _single_token_is_permanently_invalid(
        code: str | None,
        response_data: Mapping[str, object],
        token: str,
    ) -> bool:
        if code not in {
            HARMONYOS_PUSH_PARTIAL_SUCCESS_CODE,
            HARMONYOS_PUSH_INVALID_TOKEN_CODE,
        }:
            return False
        message = response_data.get("msg")
        if not isinstance(message, str):
            return False
        try:
            details = json.loads(message)
        except json.JSONDecodeError:
            return False
        if not isinstance(details, dict):
            return False
        illegal_tokens = details.get("illegalTokens")
        if not isinstance(illegal_tokens, dict):
            return False
        return any(
            isinstance(illegal_tokens.get(reason), list)
            and illegal_tokens[reason] == [token]
            for reason in HARMONYOS_PUSH_PERMANENT_TOKEN_REASONS
        )

    def send(
        self,
        token: str,
        notification: HarmonyOSNotification,
    ) -> DeliveryResult:
        if not self._delivery_enabled():
            return DeliveryResult(DeliveryStatus.DELIVERY_DISABLED)
        if not token:
            return DeliveryResult(DeliveryStatus.NO_TOKEN)

        try:
            validate_harmonyos_push_app_id()
            signed_jwt, credentials = self._authenticator.get_jwt()
            endpoint = self.api_endpoint(credentials.project_id)
        except HarmonyOSPushConfigurationError:
            return DeliveryResult(DeliveryStatus.AUTHENTICATION_FAILURE)

        try:
            response = self._http_client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {signed_jwt}",
                    "Content-Type": "application/json",
                    "push-type": "0",
                },
                json=self.build_request_body(token, notification),
                timeout=self._timeout_seconds,
            )
        except httpx.RequestError:
            return DeliveryResult(DeliveryStatus.TEMPORARY_FAILURE)

        data = self._response_json(response)
        code_value = data.get("code")
        code = str(code_value) if code_value is not None else None
        request_id_value = data.get("requestId")
        request_id = (
            str(request_id_value) if request_id_value is not None else None
        )
        base = {
            "http_status": response.status_code,
            "provider_code": code,
            "request_id": request_id,
        }

        if response.is_success and code == HARMONYOS_PUSH_SUCCESS_CODE:
            return DeliveryResult(DeliveryStatus.ACCEPTED, **base)
        if self._single_token_is_permanently_invalid(code, data, token):
            return DeliveryResult(DeliveryStatus.INVALID_TOKEN, **base)
        if response.status_code in {401, 403} or code == HARMONYOS_PUSH_JWT_EXPIRED_CODE:
            self._authenticator.invalidate_cache()
            return DeliveryResult(DeliveryStatus.AUTHENTICATION_FAILURE, **base)
        if response.status_code in {429, 503} or code in HARMONYOS_PUSH_RATE_LIMIT_CODES:
            return DeliveryResult(DeliveryStatus.RATE_LIMITED, **base)
        if (
            response.status_code in {408, 425, 500, 502, 504}
            or code in HARMONYOS_PUSH_TEMPORARY_CODES
        ):
            return DeliveryResult(DeliveryStatus.TEMPORARY_FAILURE, **base)
        return DeliveryResult(DeliveryStatus.PROVIDER_REJECTED, **base)


_default_provider: HarmonyOSPushProvider | None = None
_default_provider_lock = threading.Lock()


def get_harmonyos_push_provider() -> HarmonyOSPushProvider:
    global _default_provider
    if _default_provider is None:
        with _default_provider_lock:
            if _default_provider is None:
                _default_provider = HarmonyOSPushProvider()
    return _default_provider
