from test_v2_family import _register, v2_api


def test_new_organizer_receives_recovery_code_and_can_rotate_token(v2_api):
    client, database = v2_api
    registered, old_headers = _register(client, "FAMILY_MEMBER", "Organizer", "recovery-device")
    recovery_code = registered.get("recovery_code")
    assert isinstance(recovery_code, str)
    assert len(recovery_code) >= 40

    circle = client.post("/api/v2/family-circles", headers=old_headers, json={"name": "Family"})
    assert circle.status_code == 200
    circle_id = circle.json()["id"]

    recovered = client.post(
        "/api/v2/organizer/recover",
        json={"device_id": "recovery-device", "recovery_code": recovery_code},
    )
    assert recovered.status_code == 200, recovered.text
    new_headers = {"Authorization": f"Bearer {recovered.json()['api_token']}"}
    assert client.get("/api/v2/users/me", headers=new_headers).status_code == 200
    assert client.get("/api/v2/users/me", headers=old_headers).status_code == 401
    circles = client.get("/api/v2/family-circles", headers=new_headers)
    assert circles.status_code == 200
    assert circles.json()[0]["id"] == circle_id
    assert database.SessionLocal().query(__import__("models").User).count() == 1


def test_recovery_secret_and_tokens_are_not_logged(v2_api, caplog):
    client, _ = v2_api
    registered, _ = _register(client, "FAMILY_MEMBER", "Organizer", "logging-device")
    secret = registered["recovery_code"]
    response = client.post(
        "/api/v2/organizer/recover",
        json={"device_id": "logging-device", "recovery_code": secret},
    )
    assert response.status_code == 200
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in logs
    assert response.json()["api_token"] not in logs


def test_invalid_recovery_is_rate_limited_and_legacy_is_explicitly_unrecoverable(v2_api):
    client, database = v2_api
    registered, _ = _register(client, "FAMILY_MEMBER", "Organizer", "legacy-device")
    for _ in range(5):
        response = client.post(
            "/api/v2/organizer/recover",
            json={"device_id": "legacy-device", "recovery_code": "x" * 40},
        )
        assert response.status_code == 401
    limited = client.post(
        "/api/v2/organizer/recover",
        json={"device_id": "legacy-device", "recovery_code": registered["recovery_code"]},
    )
    assert limited.status_code == 429

    with database.SessionLocal() as db:
        user = db.query(__import__("models").User).filter_by(device_id="legacy-device").one()
        user.organizer_recovery_verifier = None
        db.commit()
    unavailable = client.post(
        "/api/v2/organizer/recover",
        json={"device_id": "legacy-device", "recovery_code": "x" * 40},
    )
    assert unavailable.status_code == 409
    assert "unavailable" in unavailable.json()["detail"]


def test_trusted_bootstrap_is_one_time_and_requires_organizer_membership(v2_api):
    client, database = v2_api
    registered, _ = _register(client, "FAMILY_MEMBER", "Organizer", "bootstrap-device")
    circle = client.post("/api/v2/family-circles", headers={"Authorization": f"Bearer {registered['api_token']}"}, json={"name": "Family"})
    assert circle.status_code == 200
    from scripts.bootstrap_organizer_recovery import bootstrap_recovery
    with database.SessionLocal() as db:
        recovery_code = bootstrap_recovery(db, registered["user_id"])
    first = client.post("/api/v2/organizer/recover", json={"device_id": "bootstrap-device", "recovery_code": recovery_code})
    assert first.status_code == 200
    second = client.post("/api/v2/organizer/recover", json={"device_id": "bootstrap-device", "recovery_code": recovery_code})
    assert second.status_code == 409
