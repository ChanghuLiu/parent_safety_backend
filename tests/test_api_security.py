import asyncio
from datetime import timedelta
import importlib
import logging
from pathlib import Path
import sys

import httpx
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_TIMEZONE", "UTC")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    # Keep push behavior deterministic instead of inheriting deployment flags.
    # Individual disabled-delivery tests override these values explicitly.
    monkeypatch.setenv("PUSH_DELIVERY_ENABLED", "true")
    monkeypatch.setenv("HARMONYOS_PUSH_ENABLED", "false")

    import database
    import fastapi.routing
    import main
    import models

    importlib.reload(database)
    importlib.reload(models)
    importlib.reload(main)

    main.Base.metadata.create_all(bind=main.engine)
    main._ensure_sqlite_migrations()

    async def test_get_db():
        db = database.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[main.get_db] = test_get_db

    # This execution sandbox cannot wake AnyIO worker-thread queues. Execute
    # synchronous route functions inline while retaining the full ASGI stack.
    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(fastapi.routing, "run_in_threadpool", run_inline)

    class Client:
        def request(self, method, url, **kwargs):
            async def send():
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                ) as client:
                    return await client.request(method, url, **kwargs)

            return asyncio.run(send())

        def get(self, url, **kwargs):
            return self.request("GET", url, **kwargs)

        def post(self, url, **kwargs):
            return self.request("POST", url, **kwargs)

    yield Client(), main
    main.app.dependency_overrides.clear()
    database.engine.dispose()


def register(client, role, device_id):
    response = client.post(
        "/api/register",
        json={
            "role": role,
            "name": f"{role} name",
            "phone": "555-0100",
            "device_id": device_id,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def create_binding(client, elder, child):
    code_response = client.post(
        "/api/elder/create-bind-code",
        headers=bearer(elder["api_token"]),
        json={"elder_user_id": elder["user_id"]},
    )
    assert code_response.status_code == 200, code_response.text
    bind_response = client.post(
        "/api/child/bind-elder",
        headers=bearer(child["api_token"]),
        json={
            "child_user_id": child["user_id"],
            "code": code_response.json()["code"],
            "elder_relationship": "parent",
            "elder_name": "Elder",
            "elder_phone": "555-0101",
        },
    )
    assert bind_response.status_code == 200, bind_response.text
    return bind_response.json()


def test_sensitive_routes_require_the_owners_token(api):
    client, _ = api
    elder = register(client, "elder", "elder-device-0001")
    child = register(client, "child", "child-device-0001")

    no_auth = client.post(
        "/api/elder/create-bind-code",
        json={"elder_user_id": elder["user_id"]},
    )
    assert no_auth.status_code == 401
    invalid_auth = client.post(
        "/api/elder/create-bind-code",
        headers=bearer("invalid-token-value"),
        json={"elder_user_id": elder["user_id"]},
    )
    assert invalid_auth.status_code == 401

    wrong_owner = client.post(
        "/api/elder/create-bind-code",
        headers=bearer(child["api_token"]),
        json={"elder_user_id": elder["user_id"]},
    )
    assert wrong_owner.status_code == 403

    binding = create_binding(client, elder, child)
    assert isinstance(binding["family_link_id"], int)

    assert client.get(
        f"/api/child/elder-status/{child['user_id']}"
    ).status_code == 401
    assert client.get(
        f"/api/child/elder-status/{child['user_id']}",
        headers=bearer(elder["api_token"]),
    ).status_code == 403
    authorized_status = client.get(
        f"/api/child/elder-status/{child['user_id']}",
        headers=bearer(child["api_token"]),
    )
    assert authorized_status.status_code == 200
    assert authorized_status.json()[0]["family_link_id"] == binding["family_link_id"]


def test_registration_never_rediscloses_token_without_authentication(api):
    client, _ = api
    child = register(client, "child", "child-device-0002")
    payload = {
        "role": "child",
        "name": "child name",
        "phone": "555-0100",
        "device_id": "child-device-0002",
    }

    assert client.post("/api/register", json=payload).status_code == 401
    authenticated = client.post(
        "/api/register",
        headers=bearer(child["api_token"]),
        json=payload,
    )
    assert authenticated.status_code == 200
    assert authenticated.json()["api_token"] == child["api_token"]


def test_bind_attempts_are_rate_limited(api):
    client, _ = api
    child = register(client, "child", "child-device-0003")
    payload = {
        "child_user_id": child["user_id"],
        "code": "000000",
        "elder_relationship": "parent",
        "elder_name": "Elder",
        "elder_phone": "555-0101",
    }

    for _ in range(5):
        assert client.post(
            "/api/child/bind-elder",
            headers=bearer(child["api_token"]),
            json=payload,
        ).status_code == 400
    limited = client.post(
        "/api/child/bind-elder",
        headers=bearer(child["api_token"]),
        json=payload,
    )
    assert limited.status_code == 429


def test_hosts_and_time_validation_are_restricted(api):
    client, _ = api
    assert client.get("/api/health", headers={"Host": "95.41.57.202"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400

    child = register(client, "child", "child-device-0004")
    invalid = client.post(
        "/api/child/update-offline-alert-settings",
        headers=bearer(child["api_token"]),
        json={
            "child_user_id": child["user_id"],
            "elder_user_id": 1,
            "offline_alert_enabled": True,
            "offline_alert_hours": 6,
            "quiet_start_time": "22:00+02:00",
            "quiet_end_time": "07:00",
        },
    )
    assert invalid.status_code == 422


def test_offline_alert_check_sends_and_records_notification(api, monkeypatch):
    client, main = api
    elder = register(client, "elder", "elder-device-0005")
    child = register(client, "child", "child-device-0005")

    db = main.SessionLocal()
    try:
        child_user = (
            db.query(main.models.User)
            .filter(main.models.User.id == child["user_id"])
            .one()
        )
        child_user.fcm_token = "fcm-token-value-long-enough"
        db.add(
            main.models.FamilyLink(
                elder_user_id=elder["user_id"],
                child_user_id=child["user_id"],
                elder_relationship="parent",
                elder_name="Elder",
                elder_phone="555-0101",
                offline_quiet_hours_enabled=False,
            )
        )
        db.add(
            main.models.DeviceStatus(
                user_id=elder["user_id"],
                battery_level=50,
                last_online_time=main._now() - timedelta(hours=7),
            )
        )
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(main, "_send_fcm_notification", lambda *_: True)
    main._run_offline_alert_check()

    db = main.SessionLocal()
    try:
        link = db.query(main.models.FamilyLink).one()
        assert link.last_offline_alert_sent_at is not None
    finally:
        db.close()


def _create_due_offline_alert(main, elder_id, child_id, token=None, platform=None):
    db = main.SessionLocal()
    try:
        child = db.query(main.models.User).filter(
            main.models.User.id == child_id
        ).one()
        child.fcm_token = token
        db.add(
            main.models.FamilyLink(
                elder_user_id=elder_id,
                child_user_id=child_id,
                elder_relationship="parent",
                elder_name="Elder",
                elder_phone="555-0101",
                offline_quiet_hours_enabled=False,
            )
        )
        db.add(
            main.models.DeviceStatus(
                user_id=elder_id,
                battery_level=50,
                last_online_time=main._now() - timedelta(hours=7),
            )
        )
        if platform is not None:
            db.add(
                main.models.DeviceStatus(
                    user_id=child_id,
                    platform=platform,
                )
            )
        db.commit()
    finally:
        db.close()


def test_fcm_unregistered_invalidates_token_and_next_run_does_not_retry(
    api, monkeypatch, caplog
):
    client, main = api
    elder = register(client, "elder", "elder-device-unregistered")
    child = register(client, "child", "child-device-unregistered")
    token = "valid-looking-fcm-registration-token"
    _create_due_offline_alert(
        main,
        elder["user_id"],
        child["user_id"],
        token=token,
        platform="android_google",
    )

    calls = []
    monkeypatch.setattr(main, "_get_firebase_app", lambda: object())

    def reject_unregistered(message, app):
        calls.append((message, app))
        raise main.messaging.UnregisteredError("NotRegistered")

    monkeypatch.setattr(main.messaging, "send", reject_unregistered)
    with caplog.at_level(logging.INFO, logger="parent_safety"):
        main._run_offline_alert_check()
        main._run_offline_alert_check()

    assert len(calls) == 1
    db = main.SessionLocal()
    try:
        stored_child = db.query(main.models.User).filter(
            main.models.User.id == child["user_id"]
        ).one()
        assert stored_child.fcm_token is None
        assert stored_child.fcm_token_invalidated_at is not None
        assert db.query(main.models.FamilyLink).one().last_offline_alert_sent_at is None
    finally:
        db.close()
    warnings = [
        record for record in caplog.records
        if record.levelno == logging.WARNING and "FCM token invalidated" in record.message
    ]
    assert len(warnings) == 1
    assert token not in caplog.text
    assert "Traceback" not in caplog.text


def test_push_delivery_disabled_prevents_external_send(api, monkeypatch, caplog):
    _, main = api
    monkeypatch.setenv("PUSH_DELIVERY_ENABLED", "false")

    def unexpected_firebase_call():
        raise AssertionError("Firebase must not be initialized")

    monkeypatch.setattr(main, "_get_firebase_app", unexpected_firebase_call)
    with caplog.at_level(logging.INFO, logger="parent_safety"):
        delivered = main._send_fcm_notification("secret-token", "title", "body")

    assert delivered is False
    assert "Push delivery disabled; notification skipped in development." in caplog.text
    assert "secret-token" not in caplog.text


@pytest.mark.parametrize(
    ("platform", "token"),
    [
        ("harmonyos", "fake-harmony-installation-uuid"),
        ("android_google", None),
    ],
)
def test_offline_alert_skips_harmonyos_and_tokenless_devices(
    api, monkeypatch, platform, token
):
    client, main = api
    elder = register(client, "elder", f"elder-device-skip-{platform}")
    child = register(client, "child", f"child-device-skip-{platform}")
    _create_due_offline_alert(
        main,
        elder["user_id"],
        child["user_id"],
        token=token,
        platform=platform,
    )
    calls = []
    monkeypatch.setattr(
        main,
        "_send_fcm_notification",
        lambda *args, **kwargs: calls.append((args, kwargs)) or True,
    )

    main._run_offline_alert_check()

    assert calls == []
    db = main.SessionLocal()
    try:
        assert db.query(main.models.FamilyLink).one().last_offline_alert_sent_at is None
    finally:
        db.close()


def test_harmonyos_token_update_does_not_store_installation_id_as_fcm(api):
    client, main = api
    child = register(client, "child", "child-device-harmony-token")
    response = client.post(
        "/api/user/update-fcm-token",
        headers=bearer(child["api_token"]),
        json={
            "user_id": child["user_id"],
            "platform": "harmonyos",
            "fcm_token": "harmony-installation-uuid-value",
        },
    )
    assert response.status_code == 200

    db = main.SessionLocal()
    try:
        stored_child = db.query(main.models.User).filter(
            main.models.User.id == child["user_id"]
        ).one()
        status = db.query(main.models.DeviceStatus).filter(
            main.models.DeviceStatus.user_id == child["user_id"]
        ).one()
        assert stored_child.fcm_token is None
        assert status.platform == "harmonyos"
        registration = db.query(main.models.DevicePushToken).one()
        assert registration.push_provider == "harmonyos"
        assert registration.push_token == "harmony-installation-uuid-value"
    finally:
        db.close()


def _register_push_token(
    client,
    registered_user,
    *,
    platform,
    push_provider,
    push_token,
    device_id=None,
    app_version=None,
):
    payload = {
        "device_id": device_id or registered_user["device_id"],
        "platform": platform,
        "push_provider": push_provider,
        "push_token": push_token,
    }
    if app_version is not None:
        payload["app_version"] = app_version
    return client.post(
        "/api/user/update-fcm-token",
        headers=bearer(registered_user["api_token"]),
        json=payload,
    )


def test_harmonyos_push_token_registration_is_typed_and_provider_neutral(
    api, caplog
):
    client, main = api
    child = register(client, "child", "child-device-harmony-phase1")
    child["device_id"] = "child-device-harmony-phase1"
    token = "harmony-secret-registration-token-1234"

    with caplog.at_level(logging.INFO, logger="parent_safety"):
        response = _register_push_token(
            client,
            child,
            platform="harmonyos",
            push_provider="harmonyos",
            push_token=token,
            app_version="2.1.0",
        )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "success": True,
        "registered": True,
        "platform": "harmonyos",
        "push_provider": "harmonyos",
        "token_updated_at": response.json()["token_updated_at"],
        "token_changed": True,
    }
    assert token not in response.text
    assert token not in caplog.text
    assert "token_length=38" in caplog.text
    assert "token_suffix=***1234" in caplog.text

    db = main.SessionLocal()
    try:
        user = db.query(main.models.User).filter(
            main.models.User.id == child["user_id"]
        ).one()
        push_token = db.query(main.models.DevicePushToken).one()
        status = db.query(main.models.DeviceStatus).one()
        assert user.fcm_token is None
        assert push_token.user_id == child["user_id"]
        assert push_token.platform == "harmonyos"
        assert push_token.push_provider == "harmonyos"
        assert push_token.push_token == token
        assert push_token.push_token_invalidated_at is None
        assert status.app_version == "2.1.0"
    finally:
        db.close()


def test_harmonyos_same_token_is_idempotent_and_changed_token_replaces_it(api):
    client, main = api
    child = register(client, "child", "child-device-harmony-replace")
    child["device_id"] = "child-device-harmony-replace"
    first_token = "harmony-registration-token-first"
    second_token = "harmony-registration-token-second"

    first = _register_push_token(
        client,
        child,
        platform="harmonyos",
        push_provider="harmonyos",
        push_token=first_token,
    )
    same = _register_push_token(
        client,
        child,
        platform="harmonyos",
        push_provider="harmonyos",
        push_token=first_token,
    )
    changed = _register_push_token(
        client,
        child,
        platform="harmonyos",
        push_provider="harmonyos",
        push_token=second_token,
    )

    assert first.status_code == same.status_code == changed.status_code == 200
    assert same.json()["token_changed"] is False
    assert same.json()["token_updated_at"] == first.json()["token_updated_at"]
    assert changed.json()["token_changed"] is True
    assert changed.json()["token_updated_at"] >= first.json()["token_updated_at"]

    db = main.SessionLocal()
    try:
        assert db.query(main.models.DevicePushToken).count() == 1
        assert db.query(main.models.DevicePushToken).one().push_token == second_token
    finally:
        db.close()


def test_push_token_registration_rejects_empty_and_unsupported_combinations(api):
    client, _ = api
    child = register(client, "child", "child-device-push-validation")
    child["device_id"] = "child-device-push-validation"

    empty = _register_push_token(
        client,
        child,
        platform="harmonyos",
        push_provider="harmonyos",
        push_token=" ",
    )
    assert empty.status_code == 422

    for platform, provider in [
        ("harmonyos", "fcm"),
        ("android_google", "harmonyos"),
        ("android_huawei", "fcm"),
    ]:
        response = _register_push_token(
            client,
            child,
            platform=platform,
            push_provider=provider,
            push_token=f"token-for-{platform}-{provider}",
        )
        assert response.status_code == 400


def test_one_device_cannot_update_another_devices_push_token(api):
    client, main = api
    first = register(client, "child", "child-device-push-owner-1")
    second = register(client, "child", "child-device-push-owner-2")
    first["device_id"] = "child-device-push-owner-1"

    forbidden = _register_push_token(
        client,
        first,
        platform="harmonyos",
        push_provider="harmonyos",
        push_token="harmony-token-wrong-owner",
        device_id="child-device-push-owner-2",
    )
    missing = _register_push_token(
        client,
        first,
        platform="harmonyos",
        push_provider="harmonyos",
        push_token="harmony-token-missing-device",
        device_id="child-device-does-not-exist",
    )

    assert forbidden.status_code == 403
    assert missing.status_code == 404
    db = main.SessionLocal()
    try:
        assert db.query(main.models.DevicePushToken).count() == 0
    finally:
        db.close()


def test_existing_android_fcm_and_huawei_registration_remain_compatible(api):
    client, main = api
    google = register(client, "child", "child-device-google-legacy")
    huawei = register(client, "child", "child-device-huawei-legacy")
    google_token = "legacy-google-fcm-registration-token"
    huawei_token = "legacy-huawei-registration-token"

    google_response = client.post(
        "/api/user/update-fcm-token",
        headers=bearer(google["api_token"]),
        json={
            "user_id": google["user_id"],
            "platform": "android_google",
            "fcm_token": google_token,
        },
    )
    huawei_response = client.post(
        "/api/user/update-fcm-token",
        headers=bearer(huawei["api_token"]),
        json={
            "user_id": huawei["user_id"],
            "platform": "android_huawei",
            "fcm_token": huawei_token,
        },
    )

    assert google_response.status_code == huawei_response.status_code == 200
    assert google_response.json()["push_provider"] == "fcm"
    assert huawei_response.json()["push_provider"] == "huawei_android"
    db = main.SessionLocal()
    try:
        google_user = db.query(main.models.User).filter(
            main.models.User.id == google["user_id"]
        ).one()
        huawei_user = db.query(main.models.User).filter(
            main.models.User.id == huawei["user_id"]
        ).one()
        assert google_user.fcm_token == google_token
        assert huawei_user.fcm_token is None
        tokens = {
            row.push_provider: row.push_token
            for row in db.query(main.models.DevicePushToken).all()
        }
        assert tokens == {
            "fcm": google_token,
            "huawei_android": huawei_token,
        }
    finally:
        db.close()


def test_disabled_delivery_does_not_block_harmonyos_token_registration(
    api, monkeypatch
):
    client, main = api
    monkeypatch.setenv("PUSH_DELIVERY_ENABLED", "false")
    child = register(client, "child", "child-device-harmony-disabled")
    child["device_id"] = "child-device-harmony-disabled"

    response = _register_push_token(
        client,
        child,
        platform="harmonyos",
        push_provider="harmonyos",
        push_token="harmony-token-with-delivery-disabled",
    )

    assert response.status_code == 200
    db = main.SessionLocal()
    try:
        assert db.query(main.models.DevicePushToken).count() == 1
    finally:
        db.close()


def test_invalidated_push_token_becomes_active_when_uploaded_again(api):
    client, main = api
    child = register(client, "child", "child-device-harmony-reactivate")
    child["device_id"] = "child-device-harmony-reactivate"
    token = "harmony-token-to-reactivate"
    first = _register_push_token(
        client,
        child,
        platform="harmonyos",
        push_provider="harmonyos",
        push_token=token,
    )
    db = main.SessionLocal()
    try:
        registration = db.query(main.models.DevicePushToken).one()
        registration.push_token_invalidated_at = main._now()
        db.commit()
    finally:
        db.close()

    refreshed = _register_push_token(
        client,
        child,
        platform="harmonyos",
        push_provider="harmonyos",
        push_token=token,
    )

    assert refreshed.status_code == 200
    assert refreshed.json()["token_changed"] is False
    assert refreshed.json()["token_updated_at"] >= first.json()["token_updated_at"]
    db = main.SessionLocal()
    try:
        assert (
            db.query(main.models.DevicePushToken)
            .one()
            .push_token_invalidated_at
            is None
        )
    finally:
        db.close()


def test_push_token_reuse_by_another_device_is_rejected(api):
    client, _ = api
    first = register(client, "child", "child-device-token-reuse-1")
    second = register(client, "child", "child-device-token-reuse-2")
    first["device_id"] = "child-device-token-reuse-1"
    second["device_id"] = "child-device-token-reuse-2"
    token = "same-harmony-token-on-two-devices"

    assert _register_push_token(
        client,
        first,
        platform="harmonyos",
        push_provider="harmonyos",
        push_token=token,
    ).status_code == 200
    conflict = _register_push_token(
        client,
        second,
        platform="harmonyos",
        push_provider="harmonyos",
        push_token=token,
    )
    assert conflict.status_code == 409


def test_push_token_table_migration_preserves_existing_user_and_fcm_token(api):
    client, main = api
    child = register(client, "child", "child-device-migration-safe")
    legacy_token = "existing-production-fcm-token"
    db = main.SessionLocal()
    try:
        user = db.query(main.models.User).filter(
            main.models.User.id == child["user_id"]
        ).one()
        user.fcm_token = legacy_token
        db.commit()
    finally:
        db.close()

    with main.engine.begin() as connection:
        connection.execute(main.sql_text("DROP TABLE device_push_tokens"))
    main._ensure_sqlite_migrations()

    db = main.SessionLocal()
    try:
        preserved = db.query(main.models.User).filter(
            main.models.User.id == child["user_id"]
        ).one()
        assert preserved.device_id == "child-device-migration-safe"
        assert preserved.fcm_token == legacy_token
        assert db.query(main.models.DevicePushToken).count() == 0
    finally:
        db.close()


class _FakeHarmonyOSProvider:
    def __init__(self, status):
        self.status = status
        self.notifications = []

    def send(self, token, notification):
        from harmonyos_push import DeliveryResult

        self.notifications.append((token, notification))
        return DeliveryResult(self.status, http_status=200, provider_code="test")


def _register_harmony_child(client, device_id):
    child = register(client, "child", device_id)
    child["device_id"] = device_id
    response = _register_push_token(
        client,
        child,
        platform="harmonyos",
        push_provider="harmonyos",
        push_token=f"harmony-token-for-{device_id}",
    )
    assert response.status_code == 200, response.text
    return child


def test_harmonyos_help_request_uses_notification_provider_and_saves_record(
    api, monkeypatch
):
    from harmonyos_push import DeliveryStatus

    client, main = api
    elder = register(client, "elder", "elder-device-harmony-help")
    child = _register_harmony_child(client, "child-device-harmony-help")
    binding = create_binding(client, elder, child)
    provider = _FakeHarmonyOSProvider(DeliveryStatus.ACCEPTED)
    monkeypatch.setattr(main, "get_harmonyos_push_provider", lambda: provider)
    monkeypatch.setattr(
        main,
        "_send_fcm_data_notification",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("HarmonyOS must never call FCM")
        ),
    )

    response = client.post(
        "/api/elder/help-request",
        headers=bearer(elder["api_token"]),
        json={
            "elder_user_id": elder["user_id"],
            "type": "call_back",
            "message": "请尽快联系我",
        },
    )

    assert response.status_code == 200
    assert response.json()["notified_children"] == 1
    assert response.json()["children_with_fcm_token"] == 0
    assert len(provider.notifications) == 1
    _, sent = provider.notifications[0]
    assert sent.title == "需要帮助"
    assert sent.body == "请尽快联系我"
    assert sent.event_type == "help_request"
    assert sent.alert_id == response.json()["alert_id"]
    assert sent.family_link_id == binding["family_link_id"]
    assert sent.target_page == "help_alert"
    db = main.SessionLocal()
    try:
        assert db.query(main.models.HelpRequest).count() == 1
    finally:
        db.close()


def test_harmonyos_offline_alert_uses_expected_service_notification(
    api, monkeypatch
):
    from harmonyos_push import DeliveryStatus

    client, main = api
    elder = register(client, "elder", "elder-device-harmony-offline")
    child = _register_harmony_child(client, "child-device-harmony-offline")
    binding = create_binding(client, elder, child)
    db = main.SessionLocal()
    try:
        link = db.query(main.models.FamilyLink).filter(
            main.models.FamilyLink.id == binding["family_link_id"]
        ).one()
        link.offline_quiet_hours_enabled = False
        db.add(
            main.models.DeviceStatus(
                user_id=elder["user_id"],
                battery_level=50,
                last_online_time=main._now() - timedelta(hours=7),
            )
        )
        db.commit()
    finally:
        db.close()

    provider = _FakeHarmonyOSProvider(DeliveryStatus.ACCEPTED)
    monkeypatch.setattr(main, "get_harmonyos_push_provider", lambda: provider)
    monkeypatch.setattr(
        main,
        "_send_fcm_notification",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("HarmonyOS must never call FCM")
        ),
    )

    assert main._run_offline_alert_check() is None
    assert len(provider.notifications) == 1
    _, sent = provider.notifications[0]
    assert sent.title == "家人长时间未在线"
    assert sent.body == "可能是手机没电、关机或没有网络，请联系确认。"
    assert sent.event_type == "offline_alert"
    assert sent.family_link_id == binding["family_link_id"]
    assert sent.target_page == "elder_status"


def test_permanent_harmonyos_token_failure_invalidates_only_token_and_is_not_retried(
    api, monkeypatch, caplog
):
    from harmonyos_push import DeliveryStatus

    client, main = api
    elder = register(client, "elder", "elder-device-harmony-invalid")
    child = _register_harmony_child(client, "child-device-harmony-invalid")
    create_binding(client, elder, child)
    provider = _FakeHarmonyOSProvider(DeliveryStatus.INVALID_TOKEN)
    monkeypatch.setattr(main, "get_harmonyos_push_provider", lambda: provider)
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="parent_safety"):
        for _ in range(2):
            response = client.post(
                "/api/elder/help-request",
                headers=bearer(elder["api_token"]),
                json={
                    "elder_user_id": elder["user_id"],
                    "type": "call_back",
                    "message": "请联系我",
                },
            )
            assert response.status_code == 200

    assert len(provider.notifications) == 1
    assert "harmony-token-for-child-device-harmony-invalid" not in caplog.text
    invalid_warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "result=invalid_token" in record.message
    ]
    assert len(invalid_warnings) == 1
    db = main.SessionLocal()
    try:
        token = db.query(main.models.DevicePushToken).one()
        assert token.push_token_invalidated_at is not None
        assert db.query(main.models.User).filter(
            main.models.User.id == child["user_id"]
        ).one().fcm_token is None
        assert db.query(main.models.HelpRequest).count() == 2
        assert db.query(main.models.FamilyLink).count() == 1
    finally:
        db.close()


def test_temporary_harmonyos_failure_preserves_token_and_help_record(
    api, monkeypatch
):
    from harmonyos_push import DeliveryStatus

    client, main = api
    elder = register(client, "elder", "elder-device-harmony-temporary")
    child = _register_harmony_child(client, "child-device-harmony-temporary")
    create_binding(client, elder, child)
    provider = _FakeHarmonyOSProvider(DeliveryStatus.TEMPORARY_FAILURE)
    monkeypatch.setattr(main, "get_harmonyos_push_provider", lambda: provider)

    response = client.post(
        "/api/elder/help-request",
        headers=bearer(elder["api_token"]),
        json={
            "elder_user_id": elder["user_id"],
            "type": "other",
            "message": "需要协助",
        },
    )

    assert response.status_code == 200
    assert response.json()["notified_children"] == 0
    db = main.SessionLocal()
    try:
        token = db.query(main.models.DevicePushToken).one()
        assert token.push_token_invalidated_at is None
        assert db.query(main.models.HelpRequest).count() == 1
    finally:
        db.close()


def test_unexpected_harmonyos_provider_error_does_not_fail_saved_help_request(
    api, monkeypatch
):
    client, main = api
    elder = register(client, "elder", "elder-device-harmony-exception")
    child = _register_harmony_child(client, "child-device-harmony-exception")
    create_binding(client, elder, child)

    class UnexpectedFailureProvider:
        def send(self, token, notification):
            raise RuntimeError("provider implementation failed")

    monkeypatch.setattr(
        main,
        "get_harmonyos_push_provider",
        lambda: UnexpectedFailureProvider(),
    )

    response = client.post(
        "/api/elder/help-request",
        headers=bearer(elder["api_token"]),
        json={
            "elder_user_id": elder["user_id"],
            "type": "other",
            "message": "仍然保存这条求助",
        },
    )

    assert response.status_code == 200
    assert response.json()["notified_children"] == 0
    db = main.SessionLocal()
    try:
        assert db.query(main.models.HelpRequest).count() == 1
        assert db.query(main.models.HelpRequest).one().message == "仍然保存这条求助"
    finally:
        db.close()


def test_android_huawei_delivery_remains_skipped_without_fcm_fallback(
    api, monkeypatch
):
    client, main = api
    child = register(client, "child", "child-device-huawei-routing")
    response = client.post(
        "/api/user/update-fcm-token",
        headers=bearer(child["api_token"]),
        json={
            "user_id": child["user_id"],
            "platform": "android_huawei",
            "fcm_token": "huawei-android-provider-token",
        },
    )
    assert response.status_code == 200
    monkeypatch.setattr(
        main,
        "_send_fcm_data_notification",
        lambda *_: (_ for _ in ()).throw(
            AssertionError("Huawei Android must not fall back to FCM")
        ),
    )
    db = main.SessionLocal()
    try:
        user = db.query(main.models.User).filter(
            main.models.User.id == child["user_id"]
        ).one()
        assert main._send_push_data_notification(
            db,
            user,
            {"event_type": "help_request"},
        ) is False
    finally:
        db.close()


def test_harmonyos_disabled_does_not_disable_android_google_fcm(api, monkeypatch):
    client, main = api
    child = register(client, "child", "child-device-google-harmony-disabled")
    token = "google-token-while-harmony-disabled"
    response = client.post(
        "/api/user/update-fcm-token",
        headers=bearer(child["api_token"]),
        json={
            "user_id": child["user_id"],
            "platform": "android_google",
            "fcm_token": token,
        },
    )
    assert response.status_code == 200
    monkeypatch.setenv("HARMONYOS_PUSH_ENABLED", "false")
    sent = []
    monkeypatch.setattr(
        main,
        "_send_fcm_data_notification",
        lambda sent_token, data: sent.append((sent_token, data)) or True,
    )
    db = main.SessionLocal()
    try:
        user = db.query(main.models.User).filter(
            main.models.User.id == child["user_id"]
        ).one()
        data = {"event_type": "help_request"}
        assert main._send_push_data_notification(db, user, data) is True
    finally:
        db.close()
    assert sent == [(token, {"event_type": "help_request"})]


def test_unexpected_firebase_error_is_reported_without_token(
    api, monkeypatch, caplog
):
    _, main = api
    token = "sensitive-fcm-token"
    monkeypatch.setattr(main, "_get_firebase_app", lambda: object())

    def fail_send(message, app):
        raise RuntimeError(f"network failed while sending {token}")

    monkeypatch.setattr(main.messaging, "send", fail_send)
    with caplog.at_level(logging.ERROR, logger="parent_safety"):
        delivered = main._send_fcm_notification(token, "title", "body")

    assert delivered is False
    assert "FCM send failed exception_type=RuntimeError" in caplog.text
    assert token not in caplog.text


def test_android_google_offline_alert_remains_fcm_compatible(api, monkeypatch):
    client, main = api
    elder = register(client, "elder", "elder-device-google-push")
    child = register(client, "child", "child-device-google-push")
    token = "valid-google-fcm-registration-token"
    _create_due_offline_alert(
        main,
        elder["user_id"],
        child["user_id"],
        token=token,
        platform="android_google",
    )
    calls = []
    monkeypatch.setattr(
        main,
        "_send_fcm_notification",
        lambda sent_token, title, body, invalidator: calls.append(sent_token) or True,
    )

    main._run_offline_alert_check()

    assert calls == [token]


def test_single_binding_unbind_is_authorized_and_idempotent(api):
    client, _ = api
    elder = register(client, "elder", "elder-device-0006")
    other_elder = register(client, "elder", "elder-device-0012")
    child = register(client, "child", "child-device-0006")
    stranger = register(client, "child", "stranger-device-0006")
    binding = create_binding(client, elder, child)
    other_binding = create_binding(client, other_elder, child)
    payload = {"family_link_id": binding["family_link_id"]}

    forbidden = client.post(
        "/api/user/unbind",
        headers=bearer(stranger["api_token"]),
        json=payload,
    )
    assert forbidden.status_code == 403

    removed = client.post(
        "/api/user/unbind",
        headers=bearer(elder["api_token"]),
        json=payload,
    )
    assert removed.status_code == 200
    assert removed.json() == {"success": True}

    retry = client.post(
        "/api/user/unbind",
        headers=bearer(child["api_token"]),
        json=payload,
    )
    assert retry.status_code == 200
    assert retry.json() == {"success": True}

    remaining = client.get(
        f"/api/child/elder-status/{child['user_id']}",
        headers=bearer(child["api_token"]),
    )
    assert remaining.status_code == 200
    assert [
        record["family_link_id"] for record in remaining.json()
    ] == [other_binding["family_link_id"]]


def test_account_deletion_removes_owned_data_and_is_idempotent(api):
    client, main = api
    elder = register(client, "elder", "elder-device-0007")
    child = register(client, "child", "child-device-0007")
    create_binding(client, elder, child)

    assert client.post("/api/user/delete-data").status_code == 401

    heartbeat = client.post(
        "/api/elder/heartbeat",
        headers=bearer(elder["api_token"]),
        json={"elder_user_id": elder["user_id"], "battery_level": 75},
    )
    assert heartbeat.status_code == 200
    assert client.post(
        "/api/elder/checkin",
        headers=bearer(elder["api_token"]),
        json={"elder_user_id": elder["user_id"], "battery_level": 74},
    ).status_code == 200
    assert client.post(
        "/api/elder/help-request",
        headers=bearer(elder["api_token"]),
        json={"elder_user_id": elder["user_id"], "type": "call_back"},
    ).status_code == 200

    deleted = client.post(
        "/api/user/delete-data",
        headers=bearer(elder["api_token"]),
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"success": True}

    retry = client.post(
        "/api/user/delete-data",
        headers=bearer(elder["api_token"]),
    )
    assert retry.status_code == 200
    assert retry.json() == {"success": True}

    assert client.post(
        "/api/user/delete-data",
        headers=bearer("unknown-token"),
    ).status_code == 401

    db = main.SessionLocal()
    try:
        assert db.query(main.models.User).filter(
            main.models.User.id == elder["user_id"]
        ).first() is None
        assert db.query(main.models.User).filter(
            main.models.User.id == child["user_id"]
        ).first() is not None
        assert db.query(main.models.FamilyLink).count() == 0
        assert db.query(main.models.BindCode).count() == 0
        assert db.query(main.models.DailyCheckin).count() == 0
        assert db.query(main.models.DeviceStatus).count() == 0
        assert db.query(main.models.HelpRequest).count() == 0
        assert db.query(main.models.DeletedApiToken).count() == 1
    finally:
        db.close()


def test_child_account_deletion_preserves_elder_owned_history(api):
    client, main = api
    elder = register(client, "elder", "elder-device-0011")
    child = register(client, "child", "child-device-0011")
    create_binding(client, elder, child)
    assert client.post(
        "/api/elder/checkin",
        headers=bearer(elder["api_token"]),
        json={"elder_user_id": elder["user_id"], "battery_level": 80},
    ).status_code == 200

    assert client.post(
        "/api/user/delete-data",
        headers=bearer(child["api_token"]),
    ).status_code == 200

    db = main.SessionLocal()
    try:
        assert db.query(main.models.User).filter(
            main.models.User.id == child["user_id"]
        ).first() is None
        assert db.query(main.models.User).filter(
            main.models.User.id == elder["user_id"]
        ).first() is not None
        assert db.query(main.models.FamilyLink).count() == 0
        assert db.query(main.models.BindAttempt).count() == 0
        assert db.query(main.models.DailyCheckin).count() == 1
        assert db.query(main.models.DeviceStatus).count() == 1
    finally:
        db.close()


def test_used_and_expired_bind_codes_are_rejected(api):
    client, main = api
    elder = register(client, "elder", "elder-device-0008")
    first_child = register(client, "child", "child-device-0008")
    second_child = register(client, "child", "child-device-0009")

    code_response = client.post(
        "/api/elder/create-bind-code",
        headers=bearer(elder["api_token"]),
        json={"elder_user_id": elder["user_id"]},
    )
    assert code_response.status_code == 200
    assert code_response.json()["expires_in_minutes"] == 30
    code = code_response.json()["code"]
    db = main.SessionLocal()
    try:
        bind_code = db.query(main.models.BindCode).filter(
            main.models.BindCode.code == code
        ).one()
        remaining = bind_code.expires_at - main._now()
        assert timedelta(minutes=29, seconds=55) < remaining <= timedelta(minutes=30)
    finally:
        db.close()

    create_payload = {
        "child_user_id": first_child["user_id"],
        "code": code,
        "elder_relationship": "parent",
        "elder_name": "Elder",
        "elder_phone": "555-0101",
    }
    assert client.post(
        "/api/child/bind-elder",
        headers=bearer(first_child["api_token"]),
        json=create_payload,
    ).status_code == 200

    used_payload = dict(create_payload, child_user_id=second_child["user_id"])
    assert client.post(
        "/api/child/bind-elder",
        headers=bearer(second_child["api_token"]),
        json=used_payload,
    ).status_code == 400

    expired_response = client.post(
        "/api/elder/create-bind-code",
        headers=bearer(elder["api_token"]),
        json={"elder_user_id": elder["user_id"]},
    )
    expired_code = expired_response.json()["code"]
    db = main.SessionLocal()
    try:
        bind_code = db.query(main.models.BindCode).filter(
            main.models.BindCode.code == expired_code
        ).one()
        bind_code.expires_at = main._now() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    expired_payload = dict(used_payload, code=expired_code)
    assert client.post(
        "/api/child/bind-elder",
        headers=bearer(second_child["api_token"]),
        json=expired_payload,
    ).status_code == 400


def test_checkin_uses_exact_data_only_fcm_payload(api, monkeypatch):
    client, main = api
    elder = register(client, "elder", "elder-device-0012")
    child = register(client, "child", "child-device-0012")
    create_binding(client, elder, child)

    db = main.SessionLocal()
    try:
        child_user = db.query(main.models.User).filter(
            main.models.User.id == child["user_id"]
        ).one()
        child_user.fcm_token = "fcm-token-value-long-enough"
        db.commit()
    finally:
        db.close()

    sent = []

    def capture_data_message(token, data):
        sent.append((token, data))
        return True

    monkeypatch.setattr(main, "_send_fcm_data_notification", capture_data_message)
    response = client.post(
        "/api/elder/checkin",
        headers=bearer(elder["api_token"]),
        json={"elder_user_id": elder["user_id"], "battery_level": 80},
    )
    assert response.status_code == 200
    assert len(sent) == 1
    token, data = sent[0]
    assert token == "fcm-token-value-long-enough"
    assert data == {
        "event_type": "checkin",
        "child_user_id": str(child["user_id"]),
        "elder_user_id": str(elder["user_id"]),
        "checkin_time": response.json()["checkin_time"],
    }
    assert all(isinstance(value, str) for value in data.values())


def test_checkin_history_returns_newest_five_to_family_only(api):
    client, main = api
    elder = register(client, "elder", "elder-device-0013")
    child = register(client, "child", "child-device-0013")
    unrelated_child = register(client, "child", "child-device-0014")
    create_binding(client, elder, child)

    db = main.SessionLocal()
    try:
        for battery_level in range(10, 16):
            days_ago = 15 - battery_level
            created_at = main._now() - timedelta(days=days_ago)
            db.add(
                main.models.DailyCheckin(
                    elder_user_id=elder["user_id"],
                    checkin_date=main._local_now().date() - timedelta(days=days_ago),
                    checkin_time="09:00",
                    battery_level=battery_level,
                    created_at=created_at,
                )
            )
        db.commit()
    finally:
        db.close()

    help_response = client.post(
        "/api/elder/help-request",
        headers=bearer(elder["api_token"]),
        json={
            "elder_user_id": elder["user_id"],
            "type": "call_back",
            "message": "Please call me back",
        },
    )
    assert help_response.status_code == 200

    path = f"/api/elder/{elder['user_id']}/checkins"
    elder_history = client.get(path, headers=bearer(elder["api_token"]))
    assert elder_history.status_code == 200
    assert [
        (record["event_type"], record["message"], record["battery_level"])
        for record in elder_history.json()
    ] == [
        ("help_request", "Please call me back", None),
        ("checkin", "I'm fine", 15),
        ("checkin", "I'm fine", 14),
        ("checkin", "I'm fine", 13),
        ("checkin", "I'm fine", 12),
    ]
    assert elder_history.json()[0]["alert_id"] == help_response.json()["alert_id"]
    assert elder_history.json()[0]["alert_type"] == "call_back"
    assert elder_history.json()[0]["status"] == "pending"
    assert elder_history.json()[0]["parent_device_id"] == "elder-device-0013"
    assert elder_history.json()[0]["family_link_id"] is not None

    child_history = client.get(path, headers=bearer(child["api_token"]))
    assert child_history.status_code == 200
    assert child_history.json() == elder_history.json()

    assert client.get(
        path,
        headers=bearer(unrelated_child["api_token"]),
    ).status_code == 403


def test_heartbeat_preserves_android_payload_and_accepts_optional_device_fields(api):
    client, main = api
    elder = register(client, "elder", "elder-device-0015")

    legacy = client.post(
        "/api/elder/heartbeat",
        headers=bearer(elder["api_token"]),
        json={"elder_user_id": elder["user_id"], "battery_level": 61},
    )
    assert legacy.status_code == 200
    assert legacy.json() == {"success": True}

    harmony = client.post(
        "/api/elder/heartbeat",
        headers=bearer(elder["api_token"]),
        json={
            "elder_user_id": elder["user_id"],
            "battery_level": 60,
            "platform": "HarmonyOS",
            "app_version": "2.0.0",
            "role": "elder",
            "device_uuid": "harmony-device-uuid-0015",
            "last_online": "2026-07-29T14:30:00Z",
        },
    )
    assert harmony.status_code == 200, harmony.text
    body = harmony.json()
    assert body == {"success": True}
    assert "api_token" not in body
    assert "fcm_token" not in body

    db = main.SessionLocal()
    try:
        status = db.query(main.models.DeviceStatus).one()
        assert status.platform == "HarmonyOS"
        assert status.app_version == "2.0.0"
        assert status.device_uuid == "harmony-device-uuid-0015"
        assert status.battery_level == 60
        assert status.last_online_time.isoformat() == "2026-07-29T14:30:00"
    finally:
        db.close()


def test_parent_current_status_returns_confirmed_status_and_family_ids(api):
    client, _ = api
    elder = register(client, "elder", "elder-device-0016")
    child = register(client, "child", "child-device-0016")
    binding = create_binding(client, elder, child)

    assert client.post(
        "/api/elder/heartbeat",
        headers=bearer(elder["api_token"]),
        json={"elder_user_id": elder["user_id"], "battery_level": 72},
    ).status_code == 200
    checkin = client.post(
        "/api/elder/checkin",
        headers=bearer(elder["api_token"]),
        json={"elder_user_id": elder["user_id"], "battery_level": 71},
    )
    assert checkin.status_code == 200
    alert = client.post(
        "/api/elder/help-request",
        headers=bearer(elder["api_token"]),
        json={"elder_user_id": elder["user_id"], "type": "call_back"},
    )
    assert alert.status_code == 200

    assert client.get("/api/elder/current-status").status_code == 401
    assert client.get(
        "/api/elder/current-status",
        headers=bearer(child["api_token"]),
    ).status_code == 403
    response = client.get(
        "/api/elder/current-status",
        headers=bearer(elder["api_token"]),
    )
    assert response.status_code == 200, response.text
    status = response.json()
    assert status["parent_user_id"] == elder["user_id"]
    assert status["parent_device_id"] == "elder-device-0016"
    assert status["safety_status"] == "normal"
    assert status["last_safety_confirmation_time"] is not None
    assert status["battery_level"] == 71
    assert status["last_online"] is not None
    assert status["active_help_status"] == "pending"
    assert status["active_help_alert"]["alert_id"] == alert.json()["alert_id"]
    assert status["family_link_id"] == binding["family_link_id"]
    assert status["family_link_ids"] == [binding["family_link_id"]]
    assert status["current_family_links"] == [
        {
            "family_link_id": binding["family_link_id"],
            "child_user_id": child["user_id"],
        }
    ]
    assert "api_token" not in status
    assert "fcm_token" not in status

    child_status = client.get(
        f"/api/child/elder-status/{child['user_id']}",
        headers=bearer(child["api_token"]),
    ).json()[0]
    assert child_status["family_link_id"] == binding["family_link_id"]
    assert child_status["pending_help_request"]["alert_id"] == alert.json()["alert_id"]
    assert (
        child_status["pending_help_request"]["family_link_id"]
        == binding["family_link_id"]
    )


def test_help_alert_actions_are_stable_authorized_idempotent_and_retained(api):
    client, main = api
    elder = register(client, "elder", "elder-device-0017")
    child = register(client, "child", "child-device-0017")
    stranger = register(client, "child", "child-device-0018")
    binding = create_binding(client, elder, child)
    created = client.post(
        "/api/elder/help-request",
        headers=bearer(elder["api_token"]),
        json={
            "elder_user_id": elder["user_id"],
            "type": "not_feeling_well",
            "message": "Need assistance",
        },
    )
    assert created.status_code == 200
    alert_id = created.json()["alert_id"]
    assert isinstance(alert_id, int)
    assert created.json()["alert_type"] == "not_feeling_well"
    assert created.json()["status"] == "pending"
    assert created.json()["family_link_id"] == binding["family_link_id"]
    assert created.json()["family_link_ids"] == [binding["family_link_id"]]

    path = f"/api/child/help-alerts/{alert_id}/acknowledge"
    forbidden = client.post(
        path,
        headers=bearer(stranger["api_token"]),
        json={"child_user_id": stranger["user_id"]},
    )
    assert forbidden.status_code == 403
    wrong_identity = client.post(
        path,
        headers=bearer(child["api_token"]),
        json={"child_user_id": stranger["user_id"]},
    )
    assert wrong_identity.status_code == 403

    acknowledged = client.post(
        path,
        headers=bearer(child["api_token"]),
        json={"child_user_id": child["user_id"]},
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["alert_id"] == alert_id
    assert acknowledged.json()["family_link_id"] == binding["family_link_id"]
    assert acknowledged.json()["elder_user_id"] == elder["user_id"]
    assert acknowledged.json()["parent_device_id"] == "elder-device-0017"
    assert acknowledged.json()["status"] == "acknowledged"
    retry = client.post(
        path,
        headers=bearer(child["api_token"]),
        json={"child_user_id": child["user_id"]},
    )
    assert retry.json() == acknowledged.json()

    resolve_path = f"/api/child/help-alerts/{alert_id}/resolve"
    resolved = client.post(
        resolve_path,
        headers=bearer(child["api_token"]),
        json={"child_user_id": child["user_id"]},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert client.post(
        resolve_path,
        headers=bearer(child["api_token"]),
        json={"child_user_id": child["user_id"]},
    ).json() == resolved.json()

    db = main.SessionLocal()
    try:
        retained = db.query(main.models.HelpRequest).filter(
            main.models.HelpRequest.id == alert_id
        ).one()
        assert retained.status == "resolved"
    finally:
        db.close()


def test_repeated_same_day_checkin_updates_one_daily_record(api):
    client, main = api
    elder = register(client, "elder", "elder-device-0019")
    first = client.post(
        "/api/elder/checkin",
        headers=bearer(elder["api_token"]),
        json={"elder_user_id": elder["user_id"], "battery_level": 80},
    )
    second = client.post(
        "/api/elder/checkin",
        headers=bearer(elder["api_token"]),
        json={"elder_user_id": elder["user_id"], "battery_level": 79},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["checkin_id"] == second.json()["checkin_id"]
    assert first.json()["checkin_date"] == second.json()["checkin_date"]

    db = main.SessionLocal()
    try:
        assert db.query(main.models.DailyCheckin).count() == 1
        assert db.query(main.models.DailyCheckin).one().battery_level == 79
    finally:
        db.close()


def test_phase3_routes_and_models_are_documented_in_openapi(api):
    client, _ = api
    document = client.get("/openapi.json").json()
    paths = document["paths"]
    assert "/api/elder/current-status" in paths
    assert "/api/child/help-alerts/{alert_id}/acknowledge" in paths
    assert "/api/child/help-alerts/{alert_id}/resolve" in paths
    assert paths["/api/elder/heartbeat"]["post"]["requestBody"]
    assert paths["/api/elder/current-status"]["get"]["responses"]["401"]
    assert paths["/api/elder/current-status"]["get"]["security"]
    assert paths["/api/child/help-alerts/{alert_id}/acknowledge"]["post"]["responses"]["403"]
    assert paths["/api/child/help-alerts/{alert_id}/acknowledge"]["post"]["security"]
    assert "DeviceStatusUpdateResponse" in document["components"]["schemas"]
    assert "ParentCurrentStatusResponse" in document["components"]["schemas"]
    assert "HelpAlertResponse" in document["components"]["schemas"]


def test_help_request_uses_exact_data_only_fcm_payload(api, monkeypatch):
    client, main = api
    elder = register(client, "elder", "elder-device-0010")
    child = register(client, "child", "child-device-0010")
    create_binding(client, elder, child)

    db = main.SessionLocal()
    try:
        child_user = db.query(main.models.User).filter(
            main.models.User.id == child["user_id"]
        ).one()
        child_user.fcm_token = "fcm-token-value-long-enough"
        db.commit()
    finally:
        db.close()

    sent = []

    def capture_data_message(token, data):
        sent.append((token, data))
        return True

    monkeypatch.setattr(main, "_send_fcm_data_notification", capture_data_message)
    response = client.post(
        "/api/elder/help-request",
        headers=bearer(elder["api_token"]),
        json={
            "elder_user_id": elder["user_id"],
            "type": "call_back",
            "message": "Please call me",
        },
    )
    assert response.status_code == 200
    assert len(sent) == 1
    token, data = sent[0]
    assert token == "fcm-token-value-long-enough"
    assert data == {
        "event_type": "help_request",
        "child_user_id": str(child["user_id"]),
        "elder_user_id": str(elder["user_id"]),
        "title": "parent Elder 需要帮助",
        "body": "Please call me",
    }
    assert all(isinstance(value, str) for value in data.values())


def test_data_only_fcm_message_has_high_android_priority(api, monkeypatch):
    _, main = api
    captured = []
    fake_app = object()
    monkeypatch.setattr(main, "_get_firebase_app", lambda: fake_app)
    monkeypatch.setattr(
        main.messaging,
        "send",
        lambda message, app: captured.append((message, app)),
    )
    data = {
        "event_type": "help_request",
        "child_user_id": "34",
        "elder_user_id": "12",
        "title": "Help",
        "body": "Call me",
    }

    assert main._send_fcm_data_notification("token", data) is True
    assert len(captured) == 1
    message, app = captured[0]
    assert app is fake_app
    assert message.notification is None
    assert message.data == data
    assert message.android.priority == "high"
