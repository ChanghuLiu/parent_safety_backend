from test_v2_family import _register, v2_api
import models
import v2_models


def test_test_entitlement_isolated_and_cleanup(v2_api, monkeypatch):
    client, database = v2_api
    monkeypatch.setenv("V3_TEST_FIXTURE_SECRET", "fixture-secret")
    from v2_test_fixtures import TestEntitlementFixture

    TestEntitlementFixture.__table__.create(bind=database.engine, checkfirst=True)
    organizer, headers = _register(client, "FAMILY_MEMBER", "TEST-Caregiver", "integration-test-care")
    created = client.post("/api/v2/family-circles", headers=headers, json={"name": "Test Circle"})
    circle_id = created.json()["id"]

    fixture = client.post(
        "/api/v2/test-fixtures/entitlement",
        headers={"X-Test-Fixture-Secret": "fixture-secret"},
        json={"user_id": organizer["user_id"], "family_circle_id": circle_id},
    )
    assert fixture.status_code == 200, fixture.text
    entitlement = client.get(f"/api/v2/family-circles/{circle_id}/entitlement", headers=headers)
    assert entitlement.status_code == 200
    assert entitlement.json()["purchase_state"] == "TEST"
    assert entitlement.json()["verification_state"] == "VERIFIED"
    with database.SessionLocal() as db:
        from v2_models import PurchaseEntitlement

        assert db.query(PurchaseEntitlement).count() == 0

    assert client.post(
        "/api/v2/test-fixtures/entitlement",
        headers={"X-Test-Fixture-Secret": "wrong"},
        json={"user_id": organizer["user_id"], "family_circle_id": circle_id},
    ).status_code == 401
    cleaned = client.delete(
        "/api/v2/test-fixtures/identities",
        headers={"X-Test-Fixture-Secret": "fixture-secret"},
        json={"user_id": organizer["user_id"], "family_circle_id": circle_id},
    )
    assert cleaned.status_code == 200, cleaned.text


def test_test_fixture_rejects_unmarked_identity(v2_api, monkeypatch):
    client, _ = v2_api
    monkeypatch.setenv("V3_TEST_FIXTURE_SECRET", "fixture-secret")
    organizer, headers = _register(client, "FAMILY_MEMBER", "Ordinary User", "ordinary-device")
    circle = client.post("/api/v2/family-circles", headers=headers, json={"name": "Ordinary Circle"}).json()
    response = client.post(
        "/api/v2/test-fixtures/entitlement",
        headers={"X-Test-Fixture-Secret": "fixture-secret"},
        json={"user_id": organizer["user_id"], "family_circle_id": circle["id"]},
    )
    assert response.status_code == 403


def test_fixture_cleanup_removes_parent_after_pairing(v2_api, monkeypatch):
    client, database = v2_api
    monkeypatch.setenv("V3_TEST_FIXTURE_SECRET", "fixture-secret")
    from v2_test_fixtures import TestEntitlementFixture

    TestEntitlementFixture.__table__.create(bind=database.engine, checkfirst=True)
    organizer, organizer_headers = _register(client, "FAMILY_MEMBER", "TEST-Caregiver", "integration-test-care")
    parent, parent_headers = _register(client, "PARENT", "TEST-Parent", "integration-test-parent")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Test Circle"}).json()
    fixture = client.post(
        "/api/v2/test-fixtures/entitlement",
        headers={"X-Test-Fixture-Secret": "fixture-secret"},
        json={"user_id": organizer["user_id"], "family_circle_id": circle["id"]},
    )
    assert fixture.status_code == 200, fixture.text
    invitation = client.post(
        f"/api/v2/family-circles/{circle['id']}/invitations",
        headers=organizer_headers,
        json={"role": "PARENT", "relationship": "parent"},
    )
    assert invitation.status_code == 200, invitation.text
    accepted = client.post(
        f"/api/v2/invitations/{invitation.json()['token']}/accept",
        headers=parent_headers,
        json={},
    )
    assert accepted.status_code == 200, accepted.text
    parent_cleaned = client.delete(
        "/api/v2/test-fixtures/identities",
        headers={"X-Test-Fixture-Secret": "fixture-secret"},
        json={"user_id": parent["user_id"], "family_circle_id": circle["id"]},
    )
    assert parent_cleaned.status_code == 200, parent_cleaned.text
    organizer_cleaned = client.delete(
        "/api/v2/test-fixtures/identities",
        headers={"X-Test-Fixture-Secret": "fixture-secret"},
        json={"user_id": organizer["user_id"], "family_circle_id": circle["id"]},
    )
    assert organizer_cleaned.status_code == 200, organizer_cleaned.text
    with database.SessionLocal() as db:
        assert db.get(models.User, parent["user_id"]) is None
        assert db.get(models.User, organizer["user_id"]) is None
        assert db.get(v2_models.FamilyCircle, circle["id"]) is None
        assert db.query(v2_models.ParentProfile).filter(v2_models.ParentProfile.user_id == parent["user_id"]).count() == 0


def test_fixture_cleanup_removes_standalone_identity(v2_api, monkeypatch):
    client, database = v2_api
    monkeypatch.setenv("V3_TEST_FIXTURE_SECRET", "fixture-secret")
    user, _ = _register(client, "PARENT", "TEST-Parent", "integration-test-orphan")
    cleaned = client.delete(
        "/api/v2/test-fixtures/identities",
        headers={"X-Test-Fixture-Secret": "fixture-secret"},
        json={"user_id": user["user_id"]},
    )
    assert cleaned.status_code == 200, cleaned.text
    with database.SessionLocal() as db:
        assert db.get(models.User, user["user_id"]) is None


def test_v2_offline_settings_round_trip_and_parent_help_type(v2_api, monkeypatch):
    client, database = v2_api
    monkeypatch.setenv("V3_TEST_FIXTURE_SECRET", "fixture-secret")
    from v2_test_fixtures import TestEntitlementFixture

    TestEntitlementFixture.__table__.create(bind=database.engine, checkfirst=True)
    organizer, organizer_headers = _register(client, "FAMILY_MEMBER", "TEST-Caregiver", "integration-test-settings-care")
    parent, parent_headers = _register(client, "PARENT", "TEST-Parent", "integration-test-settings-parent")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Settings Circle"}).json()
    fixture = client.post(
        "/api/v2/test-fixtures/entitlement",
        headers={"X-Test-Fixture-Secret": "fixture-secret"},
        json={"user_id": organizer["user_id"], "family_circle_id": circle["id"]},
    )
    assert fixture.status_code == 200, fixture.text
    invitation = client.post(
        f"/api/v2/family-circles/{circle['id']}/invitations",
        headers=organizer_headers,
        json={"role": "PARENT", "relationship": "parent"},
    ).json()
    assert client.post(f"/api/v2/invitations/{invitation['token']}/accept", headers=parent_headers, json={}).status_code == 200

    settings = client.get(f"/api/v2/family-circles/{circle['id']}/offline-alert-settings", headers=organizer_headers)
    assert settings.status_code == 200
    assert settings.json()["offline_alert_hours"] == 6
    saved = client.put(
        f"/api/v2/family-circles/{circle['id']}/offline-alert-settings",
        headers=organizer_headers,
        json={
            "offline_alert_enabled": True,
            "offline_alert_hours": 24,
            "quiet_hours_enabled": True,
            "quiet_start_time": "21:30",
            "quiet_end_time": "07:30",
            "pause_until": "2030-01-03T12:00:00",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["offline_alert_hours"] == 24
    assert saved.json()["pause_until"] == "2030-01-03T12:00:00"

    help_request = client.post(
        "/api/v2/parents/me/help-requests",
        headers=parent_headers,
        json={"request_type": "home_help", "message": ""},
    )
    assert help_request.status_code == 200, help_request.text
    assert help_request.json()["status"] == "created"

    assert client.delete(
        "/api/v2/test-fixtures/identities",
        headers={"X-Test-Fixture-Secret": "fixture-secret"},
        json={"user_id": parent["user_id"], "family_circle_id": circle["id"]},
    ).status_code == 200
    assert client.delete(
        "/api/v2/test-fixtures/identities",
        headers={"X-Test-Fixture-Secret": "fixture-secret"},
        json={"user_id": organizer["user_id"], "family_circle_id": circle["id"]},
    ).status_code == 200
