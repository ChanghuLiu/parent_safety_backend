import json
from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import threading

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
import pytest

from harmonyos_push import (
    DeliveryStatus,
    HARMONYOS_PUSH_APP_ID,
    HARMONYOS_PUSH_AUDIENCE,
    HarmonyOSJWTAuthenticator,
    HarmonyOSNotification,
    HarmonyOSPushConfigurationError,
    HarmonyOSPushProvider,
    HarmonyOSServiceAccount,
    load_harmonyos_service_account,
    validate_harmonyos_push_app_id,
)


@pytest.fixture()
def service_account():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return (
        HarmonyOSServiceAccount(
            project_id="test-project-123",
            key_id="test-key-id",
            private_key=private_pem,
            sub_account="test-service-account",
        ),
        private_key.public_key(),
    )


def notification(event_type="help_request"):
    return HarmonyOSNotification(
        title="需要帮助",
        body="请给我回电话",
        category="HEALTH",
        event_type=event_type,
        alert_id=42,
        family_link_id=7,
        target_page="help_alert",
    )


def provider_with_transport(service_account, handler, monkeypatch):
    account, _ = service_account
    monkeypatch.setenv("PUSH_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("HARMONYOS_PUSH_ENABLED", "true")
    monkeypatch.setenv("HARMONYOS_PUSH_APP_ID", HARMONYOS_PUSH_APP_ID)
    authenticator = HarmonyOSJWTAuthenticator(
        credentials_loader=lambda: account,
        clock=lambda: 1_900_000_000,
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HarmonyOSPushProvider(authenticator=authenticator, http_client=client)


def test_jwt_creation_uses_official_ps256_header_and_claims(service_account):
    fixture_account, public_key = service_account
    account = HarmonyOSServiceAccount(
        key_id=fixture_account.key_id,
        private_key=fixture_account.private_key,
        sub_account=fixture_account.sub_account,
    )
    now = 1_900_000_000
    authenticator = HarmonyOSJWTAuthenticator(
        credentials_loader=lambda: account,
        clock=lambda: now,
    )

    token, returned_account = authenticator.get_jwt()

    assert returned_account == account
    assert jwt.get_unverified_header(token) == {
        "alg": "PS256",
        "kid": "test-key-id",
        "typ": "JWT",
    }
    claims = jwt.decode(
        token,
        public_key,
        algorithms=["PS256"],
        audience=HARMONYOS_PUSH_AUDIENCE,
        issuer="test-service-account",
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims == {
        "aud": HARMONYOS_PUSH_AUDIENCE,
        "iss": "test-service-account",
        "iat": now,
        "exp": now + 3600,
    }


def test_jwt_is_cached_and_refreshed_before_expiration(service_account):
    account, _ = service_account
    current_time = [1_900_000_000]
    authenticator = HarmonyOSJWTAuthenticator(
        credentials_loader=lambda: account,
        clock=lambda: current_time[0],
    )

    first, _ = authenticator.get_jwt()
    current_time[0] += 3299
    cached, _ = authenticator.get_jwt()
    current_time[0] += 1
    refreshed, _ = authenticator.get_jwt()

    assert cached == first
    assert refreshed != first


def test_concurrent_jwt_refresh_creates_only_one_token(service_account, monkeypatch):
    account, _ = service_account
    authenticator = HarmonyOSJWTAuthenticator(
        credentials_loader=lambda: account,
        clock=lambda: 1_900_000_000,
    )
    original_create = authenticator._create_jwt
    create_count = 0
    count_lock = threading.Lock()

    def counted_create(*args):
        nonlocal create_count
        with count_lock:
            create_count += 1
        return original_create(*args)

    monkeypatch.setattr(authenticator, "_create_jwt", counted_create)
    with ThreadPoolExecutor(max_workers=8) as executor:
        tokens = list(executor.map(lambda _: authenticator.get_jwt()[0], range(16)))

    assert create_count == 1
    assert len(set(tokens)) == 1


def test_missing_and_invalid_credentials_fail_without_logging_secrets(
    service_account, monkeypatch, caplog
):
    with pytest.raises(HarmonyOSPushConfigurationError):
        load_harmonyos_service_account({})

    account, _ = service_account
    secret_marker = "never-log-this-private-material"
    invalid_account = HarmonyOSServiceAccount(
        project_id=account.project_id,
        key_id=account.key_id,
        private_key=secret_marker,
        sub_account=account.sub_account,
    )
    monkeypatch.setenv("PUSH_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("HARMONYOS_PUSH_ENABLED", "true")
    monkeypatch.setenv("HARMONYOS_PUSH_APP_ID", HARMONYOS_PUSH_APP_ID)
    provider = HarmonyOSPushProvider(
        authenticator=HarmonyOSJWTAuthenticator(
            credentials_loader=lambda: invalid_account
        )
    )

    with caplog.at_level(logging.INFO):
        result = provider.send("sensitive-push-token", notification())

    assert result.status == DeliveryStatus.AUTHENTICATION_FAILURE
    assert secret_marker not in caplog.text
    assert "sensitive-push-token" not in caplog.text


def _write_service_account(path: Path, account, **changes):
    values = {
        "key_id": account.key_id,
        "private_key": account.private_key,
        "sub_account": account.sub_account,
    }
    values.update(changes)
    path.write_text(json.dumps(values), encoding="utf-8")
    path.chmod(0o600)


def test_official_service_account_without_project_id_parses_successfully(
    service_account, monkeypatch, tmp_path: Path
):
    account, _ = service_account
    credential_path = tmp_path / "harmonyos-push-service-account.json"
    _write_service_account(credential_path, account)
    monkeypatch.setenv("HARMONYOS_PUSH_SERVICE_ACCOUNT_FILE", str(credential_path))

    loaded = load_harmonyos_service_account()
    assert loaded.project_id is None
    assert loaded.key_id == account.key_id
    assert loaded.private_key == account.private_key.strip()
    assert loaded.sub_account == account.sub_account


@pytest.mark.parametrize("missing_field", ["key_id", "private_key", "sub_account"])
def test_service_account_missing_required_jwt_field_fails(
    service_account, monkeypatch, tmp_path: Path, missing_field
):
    account, _ = service_account
    credential_path = tmp_path / "harmonyos-push-service-account.json"
    _write_service_account(credential_path, account, **{missing_field: ""})
    monkeypatch.setenv("HARMONYOS_PUSH_SERVICE_ACCOUNT_FILE", str(credential_path))

    with pytest.raises(HarmonyOSPushConfigurationError, match=missing_field):
        load_harmonyos_service_account()


def test_optional_project_id_is_accepted_when_present(
    service_account, monkeypatch, tmp_path: Path
):
    account, _ = service_account
    credential_path = tmp_path / "harmonyos-push-service-account.json"
    _write_service_account(
        credential_path, account, project_id=account.project_id
    )
    monkeypatch.setenv("HARMONYOS_PUSH_SERVICE_ACCOUNT_FILE", str(credential_path))

    loaded = load_harmonyos_service_account()

    assert loaded.project_id == account.project_id


def test_service_account_file_requires_protected_permissions(
    service_account, monkeypatch, tmp_path: Path
):
    account, _ = service_account
    credential_path = tmp_path / "harmonyos-push-service-account.json"
    _write_service_account(credential_path, account)
    monkeypatch.setenv("HARMONYOS_PUSH_SERVICE_ACCOUNT_FILE", str(credential_path))

    credential_path.chmod(0o644)
    with pytest.raises(HarmonyOSPushConfigurationError, match="group/world accessible"):
        load_harmonyos_service_account()


def test_app_id_is_validated_independently():
    assert (
        validate_harmonyos_push_app_id(
            {"HARMONYOS_PUSH_APP_ID": HARMONYOS_PUSH_APP_ID}
        )
        == HARMONYOS_PUSH_APP_ID
    )
    for value in ("", "not-the-project-id"):
        with pytest.raises(HarmonyOSPushConfigurationError, match="APP ID"):
            validate_harmonyos_push_app_id({"HARMONYOS_PUSH_APP_ID": value})


def test_successful_send_uses_official_v3_notification_shape(
    service_account, monkeypatch
):
    captured = {}

    def accept(request):
        captured["request"] = request
        return httpx.Response(
            200,
            json={
                "code": "80000000",
                "msg": "Success",
                "requestId": "safe-request-id",
            },
        )

    provider = provider_with_transport(service_account, accept, monkeypatch)
    result = provider.send("test-harmony-token", notification())

    assert result.status == DeliveryStatus.ACCEPTED
    assert result.request_id == "safe-request-id"
    request = captured["request"]
    assert request.url == (
        "https://push-api.cloud.huawei.com/v3/"
        "test-project-123/messages:send"
    )
    assert request.headers["push-type"] == "0"
    assert request.headers["authorization"].startswith("Bearer ")
    assert json.loads(request.content) == {
        "payload": {
            "notification": {
                "category": "HEALTH",
                "title": "需要帮助",
                "body": "请给我回电话",
                "clickAction": {
                    "actionType": 0,
                    "data": {
                        "event_type": "help_request",
                        "target_page": "help_alert",
                        "family_link_id": "7",
                        "alert_id": "42",
                    },
                },
            }
        },
        "target": {"token": ["test-harmony-token"]},
    }


@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (
            500,
            {"code": "81000001", "msg": "Internal system error"},
            DeliveryStatus.TEMPORARY_FAILURE,
        ),
        (
            401,
            {"code": "80200005", "msg": "Jwt token expired"},
            DeliveryStatus.AUTHENTICATION_FAILURE,
        ),
        (
            503,
            {"code": "80300029", "msg": "Traffic limit"},
            DeliveryStatus.RATE_LIMITED,
        ),
        (
            400,
            {"code": "80300008", "msg": "Push message size is too long"},
            DeliveryStatus.PROVIDER_REJECTED,
        ),
    ],
)
def test_provider_response_categories(
    service_account, monkeypatch, status_code, body, expected
):
    provider = provider_with_transport(
        service_account,
        lambda _: httpx.Response(status_code, json=body),
        monkeypatch,
    )

    assert provider.send("test-harmony-token", notification()).status == expected


def test_network_failure_is_temporary(service_account, monkeypatch):
    def fail(request):
        raise httpx.ConnectError("network unavailable", request=request)

    provider = provider_with_transport(service_account, fail, monkeypatch)

    assert (
        provider.send("test-harmony-token", notification()).status
        == DeliveryStatus.TEMPORARY_FAILURE
    )


def test_only_explicit_permanent_token_details_invalidate(
    service_account, monkeypatch
):
    token = "test-harmony-token"
    responses = iter(
        [
            {
                "code": "80300007",
                "msg": json.dumps(
                    {"failure": 1, "illegalTokens": {"tokenFormatError": [token]}}
                ),
            },
            {
                "code": "80300007",
                "msg": json.dumps(
                    {"failure": 1, "illegalTokens": {"noRight": [token]}}
                ),
            },
        ]
    )
    provider = provider_with_transport(
        service_account,
        lambda _: httpx.Response(400, json=next(responses)),
        monkeypatch,
    )

    assert provider.send(token, notification()).status == DeliveryStatus.INVALID_TOKEN
    assert (
        provider.send(token, notification()).status
        == DeliveryStatus.PROVIDER_REJECTED
    )


@pytest.mark.parametrize(
    "disabled_variable",
    ["PUSH_DELIVERY_ENABLED", "HARMONYOS_PUSH_ENABLED"],
)
def test_disabled_delivery_never_creates_jwt_or_calls_provider(
    service_account, monkeypatch, disabled_variable
):
    account, _ = service_account
    calls = []

    def unexpected_credentials():
        calls.append("credentials")
        return account

    def unexpected_http(request):
        calls.append("http")
        return httpx.Response(200, json={"code": "80000000"})

    monkeypatch.setenv("PUSH_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("HARMONYOS_PUSH_ENABLED", "true")
    monkeypatch.setenv("HARMONYOS_PUSH_APP_ID", HARMONYOS_PUSH_APP_ID)
    monkeypatch.setenv(disabled_variable, "false")
    provider = HarmonyOSPushProvider(
        authenticator=HarmonyOSJWTAuthenticator(
            credentials_loader=unexpected_credentials
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(unexpected_http)),
    )

    result = provider.send("test-harmony-token", notification())

    assert result.status == DeliveryStatus.DELIVERY_DISABLED
    assert calls == []
