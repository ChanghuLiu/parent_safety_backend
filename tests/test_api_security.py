import asyncio
from datetime import timedelta
import importlib
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
    client, _ = api
    elder = register(client, "elder", "elder-device-0013")
    child = register(client, "child", "child-device-0013")
    unrelated_child = register(client, "child", "child-device-0014")
    create_binding(client, elder, child)

    for battery_level in range(10, 16):
        response = client.post(
            "/api/elder/checkin",
            headers=bearer(elder["api_token"]),
            json={
                "elder_user_id": elder["user_id"],
                "battery_level": battery_level,
            },
        )
        assert response.status_code == 200

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

    child_history = client.get(path, headers=bearer(child["api_token"]))
    assert child_history.status_code == 200
    assert child_history.json() == elder_history.json()

    assert client.get(
        path,
        headers=bearer(unrelated_child["api_token"]),
    ).status_code == 403


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
