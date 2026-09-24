import asyncio
import importlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import httpx
import pytest
import hashlib
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_relationship_presentation_role_fallbacks_are_directional():
    import v2_routes

    generic_parent = SimpleNamespace(display_name="Parent")
    generic_manager = SimpleNamespace(display_name="Family Member")
    assert v2_routes._presented_name(generic_parent, "Parent", "Parent") == "Parent"
    assert v2_routes._presented_name(generic_manager, "Parent", "Parent") == "Parent"
    assert v2_routes._presented_name(generic_parent, "Parent", "Family manager") == "Family manager"


@pytest.fixture()
def v2_api(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_TIMEZONE", "UTC")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'v2.db'}")
    monkeypatch.setenv("PUSH_DELIVERY_ENABLED", "false")
    monkeypatch.setenv("HARMONYOS_PUSH_ENABLED", "false")
    import database
    import fastapi.routing
    import models
    import v2_models
    import v2_auth
    import v2_routes
    import main

    importlib.reload(database)
    importlib.reload(models)
    importlib.reload(v2_models)
    importlib.reload(v2_auth)
    importlib.reload(v2_routes)
    importlib.reload(main)
    main.Base.metadata.create_all(bind=main.engine)
    main._ensure_sqlite_migrations()

    async def isolated_db():
        db = database.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[main.get_db] = isolated_db
    # V2 route and auth dependencies keep their imported get_db references.
    # Override those references too so every request in the isolated harness
    # uses the same temporary database/session.
    main.app.dependency_overrides[v2_routes.get_db] = isolated_db
    main.app.dependency_overrides[v2_auth.get_db] = isolated_db

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(fastapi.routing, "run_in_threadpool", run_inline)

    class Client:
        def request(self, method, url, **kwargs):
            async def send():
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    return await client.request(method, url, **kwargs)

            return asyncio.run(send())

        def get(self, url, **kwargs):
            return self.request("GET", url, **kwargs)

        def post(self, url, **kwargs):
            return self.request("POST", url, **kwargs)

        def put(self, url, **kwargs):
            return self.request("PUT", url, **kwargs)

        def delete(self, url, **kwargs):
            return self.request("DELETE", url, **kwargs)

    yield Client(), database
    main.app.dependency_overrides.clear()
    database.engine.dispose()


def _register(client, role, name, device, locale="en"):
    response = client.post("/api/v2/register", json={"role": role, "name": name, "phone": "", "device_id": device, "locale_tag": locale})
    assert response.status_code == 200, response.text
    value = response.json()
    return value, {"Authorization": f"Bearer {value['api_token']}"}


def _activate_test_circle(database, circle_id):
    from v2_models import FamilyCircle
    with database.SessionLocal() as db:
        db.get(FamilyCircle, circle_id).status = "ACTIVE"
        db.commit()


def test_timezone_engine_matrix_and_state_boundaries():
    from v2_time import CheckInState, evaluate_window, localize_utc, local_window_to_utc

    for zone in ("America/Sao_Paulo", "America/Bogota", "Asia/Riyadh", "Asia/Dubai", "America/Toronto"):
        scheduled = local_window_to_utc(datetime(2026, 1, 15).date(), "09:00", zone)
        assert localize_utc(scheduled, zone).strftime("%H:%M") == "09:00"

    dst_scheduled = local_window_to_utc(datetime(2026, 3, 8).date(), "09:00", "America/Toronto")
    assert localize_utc(dst_scheduled, "America/Toronto").strftime("%H:%M") == "09:00"
    assert localize_utc(datetime(2026, 1, 15, 2, 0), "America/Sao_Paulo").date().isoformat() == "2026-01-14"
    assert evaluate_window(dst_scheduled - timedelta(minutes=1), dst_scheduled, 30) == CheckInState.UPCOMING
    assert evaluate_window(dst_scheduled, dst_scheduled, 30) == CheckInState.DUE
    assert evaluate_window(dst_scheduled + timedelta(minutes=1), dst_scheduled, 30) == CheckInState.GRACE_PERIOD
    assert evaluate_window(dst_scheduled + timedelta(minutes=30), dst_scheduled, 30) == CheckInState.MISSED


def test_v2_circle_parent_checkin_and_authorization(v2_api):
    client, database = v2_api
    organizer, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "organizer-device", "en")
    parent, parent_headers = _register(client, "PARENT", "Mãe", "parent-device", "pt-BR")
    sibling, sibling_headers = _register(client, "FAMILY_MEMBER", "Sibling", "sibling-device", "es")

    circle_response = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Nossa família"})
    assert circle_response.status_code == 200, circle_response.text
    circle_id = circle_response.json()["id"]
    _activate_test_circle(database, circle_id)

    parent_invite = client.post(f"/api/v2/family-circles/{circle_id}/invitations", headers=organizer_headers, json={"role": "PARENT", "relationship": "mãe"})
    sibling_invite = client.post(f"/api/v2/family-circles/{circle_id}/invitations", headers=organizer_headers, json={"role": "FAMILY_MEMBER", "relationship": "irmão"})
    assert parent_invite.status_code == sibling_invite.status_code == 200
    assert client.post(f"/api/v2/invitations/{parent_invite.json()['token']}/accept", headers=parent_headers).status_code == 200
    assert client.post(f"/api/v2/invitations/{sibling_invite.json()['token']}/accept", headers=sibling_headers).status_code == 200

    schedule = client.put("/api/v2/parents/me/schedule", headers=parent_headers, json={"local_time": "09:00", "timezone": "America/Sao_Paulo", "grace_period_minutes": 30, "days_of_week": "0,1,2,3,4,5,6", "enabled": True})
    assert schedule.status_code == 200, schedule.text
    checkin = client.post("/api/v2/parents/me/check-ins", headers=parent_headers, json={"battery_level": 82, "idempotency_key": "parent-checkin-0001"})
    assert checkin.status_code == 200, checkin.text
    duplicate = client.post("/api/v2/parents/me/check-ins", headers=parent_headers, json={"battery_level": 80, "idempotency_key": "parent-checkin-0001"})
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"] is True

    parents = client.get(f"/api/v2/family-circles/{circle_id}/parents", headers=organizer_headers)
    assert parents.status_code == 200 and parents.json()[0]["timezone"] == "America/Sao_Paulo"
    parent_id = parents.json()[0]["parent_profile_id"]
    assert client.post(f"/api/v2/family-circles/{circle_id}/parents/{parent_id}/check-now", headers=organizer_headers).status_code == 200
    status = client.get(f"/api/v2/family-circles/{circle_id}/parents/{parent_id}/status", headers=sibling_headers)
    assert status.status_code == 200
    assert client.put("/api/v2/parents/me/schedule", headers=organizer_headers, json={"local_time": "10:00", "timezone": "UTC"}).status_code == 403

    other, other_headers = _register(client, "FAMILY_MEMBER", "Other", "other-device", "ar")
    other_circle = client.post("/api/v2/family-circles", headers=other_headers, json={"name": "Other"})
    other_id = other_circle.json()["id"]
    assert client.get(f"/api/v2/family-circles/{other_id}/parents/{parent_id}/status", headers=other_headers).status_code in {403, 404}


def test_duplicate_registration_logs_non_secret_conflict_fingerprint(v2_api, caplog):
    client, database = v2_api
    device_id = "duplicate-registration-device"
    registered, _ = _register(client, "FAMILY_MEMBER", "Organizer", device_id)

    caplog.clear()
    with caplog.at_level("WARNING", logger="parent_safety"):
        response = client.post(
            "/api/v2/register",
            json={
                "role": "FAMILY_MEMBER",
                "name": "Organizer",
                "phone": "",
                "device_id": device_id,
                "locale_tag": "en",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "device is already registered for this role"
    messages = "\n".join(record.getMessage() for record in caplog.records)
    expected_fp = hashlib.sha256(device_id.encode("utf-8")).hexdigest()
    assert "event=register_duplicate_conflict" in messages
    assert f"existing_user_id={registered['user_id']}" in messages
    assert "role=family_member" in messages
    assert f"device_fp={expected_fp}" in messages
    assert device_id not in messages
    assert registered["api_token"] not in messages
    assert registered["recovery_code"] not in messages
    with database.SessionLocal() as db:
        assert db.query(__import__("models").User).count() == 1


def test_android_not_well_help_alias_is_accepted_and_stored_canonically(v2_api):
    client, database = v2_api
    organizer, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "alias-organizer-device")
    parent, parent_headers = _register(client, "PARENT", "Parent", "alias-parent-device")

    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Alias test"})
    assert circle.status_code == 200
    circle_id = circle.json()["id"]
    _activate_test_circle(database, circle_id)
    invite = client.post(f"/api/v2/family-circles/{circle_id}/invitations", headers=organizer_headers, json={"role": "PARENT"})
    assert invite.status_code == 200
    assert client.post(f"/api/v2/invitations/{invite.json()['token']}/accept", headers=parent_headers).status_code == 200

    response = client.post("/api/v2/parents/me/help-requests", headers=parent_headers, json={"request_type": "not_well"})
    assert response.status_code == 200, response.text
    from v2_models import V2HelpRequest
    with database.SessionLocal() as db:
        stored = db.query(V2HelpRequest).one()
        assert stored.request_type == "not_feeling_well"


def test_parent_phone_profile_round_trip_and_checkin_supersedes_older_help(v2_api):
    client, database = v2_api
    organizer, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "phone-organizer-device")
    parent, parent_headers = _register(client, "PARENT", "Parent", "phone-parent-device")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Phone family"})
    circle_id = circle.json()["id"]
    _activate_test_circle(database, circle_id)
    invite = client.post(f"/api/v2/family-circles/{circle_id}/invitations", headers=organizer_headers, json={"role": "PARENT"})
    assert client.post(f"/api/v2/invitations/{invite.json()['token']}/accept", headers=parent_headers).status_code == 200

    before = client.get("/api/v2/users/me", headers=parent_headers)
    assert before.status_code == 200 and before.json()["phone"] is None
    saved = client.put("/api/v2/users/me/profile", headers=parent_headers, json={"phone": "  +1 416 555 0100  "})
    assert saved.status_code == 200 and saved.json()["phone"] == "+1 416 555 0100"
    parents = client.get(f"/api/v2/family-circles/{circle_id}/parents", headers=organizer_headers)
    assert parents.status_code == 200 and parents.json()[0]["phone"] == "+1 416 555 0100"

    help_response = client.post("/api/v2/parents/me/help-requests", headers=parent_headers, json={"request_type": "home_help"})
    assert help_response.status_code == 200
    checkin = client.post("/api/v2/parents/me/check-ins", headers=parent_headers, json={"idempotency_key": "phone-checkin-after-help"})
    assert checkin.status_code == 200 and checkin.json()["duplicate"] is False
    parent_id = parents.json()[0]["parent_profile_id"]
    status = client.get(f"/api/v2/family-circles/{circle_id}/parents/{parent_id}/status", headers=organizer_headers)
    assert status.status_code == 200 and status.json()["state"] == "CHECKED_IN"


def test_organizer_can_update_connected_parent_phone_but_parent_cannot_update_another_parent(v2_api):
    client, database = v2_api
    organizer, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "phone-owner-organizer")
    parent, parent_headers = _register(client, "PARENT", "Parent", "phone-owner-parent")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Phone ownership"})
    circle_id = circle.json()["id"]
    _activate_test_circle(database, circle_id)
    invite = client.post(f"/api/v2/family-circles/{circle_id}/invitations", headers=organizer_headers, json={"role": "PARENT"})
    accepted = client.post(f"/api/v2/invitations/{invite.json()['token']}/accept", headers=parent_headers)
    assert accepted.status_code == 200
    parent_profile_id = accepted.json()["parent_profile_id"]
    updated = client.put(f"/api/v2/family-circles/{circle_id}/parents/{parent_profile_id}/profile", headers=organizer_headers, json={"phone": " +1 416 555 0111 ", "name": "Mom", "avatar_url": "content://avatar/mom"})
    assert updated.status_code == 200 and updated.json()["phone"] == "+1 416 555 0111"
    assert updated.json()["name"] == "Parent" and updated.json()["avatar_url"] is None
    status = client.get(f"/api/v2/family-circles/{circle_id}/parents/{parent_profile_id}/status", headers=organizer_headers)
    assert status.status_code == 200 and status.json()["display_name"] == "Mom" and status.json()["avatar_url"] == "content://avatar/mom" and status.json()["current_local"]
    forbidden = client.put(f"/api/v2/family-circles/{circle_id}/parents/{parent_profile_id}/profile", headers=parent_headers, json={"phone": "+1 416 555 0222"})
    assert forbidden.status_code == 403


@pytest.mark.parametrize(
    ("request_type", "expected_wire_value"),
    [
        ("call_back", "call_back"),
        ("home_help", "home_help"),
        ("not_well", "not_feeling_well"),
        ("other", "other"),
    ],
)
def test_v2_history_exposes_help_request_type_and_preserves_checkin_shape(v2_api, request_type, expected_wire_value):
    client, database = v2_api
    _, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", f"history-organizer-{request_type}-device")
    _, parent_headers = _register(client, "PARENT", "Parent", f"history-parent-{request_type}-device")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "History contract"})
    assert circle.status_code == 200
    circle_id = circle.json()["id"]
    _activate_test_circle(database, circle_id)
    invite = client.post(f"/api/v2/family-circles/{circle_id}/invitations", headers=organizer_headers, json={"role": "PARENT"})
    assert invite.status_code == 200
    assert client.post(f"/api/v2/invitations/{invite.json()['token']}/accept", headers=parent_headers).status_code == 200
    assert client.post("/api/v2/parents/me/check-ins", headers=parent_headers, json={"idempotency_key": f"history-checkin-{request_type}"}).status_code == 200
    help_response = client.post("/api/v2/parents/me/help-requests", headers=parent_headers, json={"request_type": request_type})
    assert help_response.status_code == 200, help_response.text

    parent_id = client.get(f"/api/v2/family-circles/{circle_id}/parents", headers=organizer_headers).json()[0]["parent_profile_id"]
    history = client.get(f"/api/v2/family-circles/{circle_id}/parents/{parent_id}/history", headers=organizer_headers)
    assert history.status_code == 200, history.text
    help_item = next(item for item in history.json() if item["request_type"] is not None)
    assert help_item["request_type"] == expected_wire_value
    checkin_item = next(item for item in history.json() if item["status"] == "CHECKED_IN")
    assert checkin_item["request_type"] is None


def test_v2_history_unknown_help_request_type_is_returned_without_serialization_failure(v2_api):
    client, database = v2_api
    _, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "history-unknown-organizer-device")
    _, parent_headers = _register(client, "PARENT", "Parent", "history-unknown-parent-device")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Unknown history contract"}).json()
    circle_id = circle["id"]
    _activate_test_circle(database, circle_id)
    invite = client.post(f"/api/v2/family-circles/{circle_id}/invitations", headers=organizer_headers, json={"role": "PARENT"}).json()
    assert client.post(f"/api/v2/invitations/{invite['token']}/accept", headers=parent_headers).status_code == 200
    with database.SessionLocal() as db:
        from v2_models import ParentProfile, V2HelpRequest
        profile = db.query(ParentProfile).filter(ParentProfile.family_circle_id == circle_id).one()
        db.add(V2HelpRequest(family_circle_id=circle_id, parent_profile_id=profile.id, request_type="future_help_type"))
        db.commit()

    parent_id = client.get(f"/api/v2/family-circles/{circle_id}/parents", headers=organizer_headers).json()[0]["parent_profile_id"]
    history = client.get(f"/api/v2/family-circles/{circle_id}/parents/{parent_id}/history", headers=organizer_headers)
    assert history.status_code == 200
    assert next(item for item in history.json() if item["request_type"] is not None)["request_type"] == "future_help_type"


def test_parent_capability_can_own_one_circle_without_losing_parent_membership(v2_api):
    client, database = v2_api
    parent, parent_headers = _register(client, "PARENT", "Parent organizer", "parent-organizer-device")
    organizer, organizer_headers = _register(client, "FAMILY_MEMBER", "Existing organizer", "existing-organizer-device")
    existing = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Existing"})
    assert existing.status_code == 200
    existing_id = existing.json()["id"]
    _activate_test_circle(database, existing_id)
    invite = client.post(f"/api/v2/family-circles/{existing_id}/invitations", headers=organizer_headers, json={"role": "PARENT"})
    assert invite.status_code == 200
    assert client.post(f"/api/v2/invitations/{invite.json()['token']}/accept", headers=parent_headers).status_code == 200
    _, second_parent_headers = _register(client, "PARENT", "Second parent", "second-parent-device")
    second_invite = client.post(f"/api/v2/family-circles/{existing_id}/invitations", headers=organizer_headers, json={"role": "PARENT"})
    assert second_invite.status_code == 200
    second_accept = client.post(f"/api/v2/invitations/{second_invite.json()['token']}/accept", headers=second_parent_headers)
    assert second_accept.status_code == 409

    created = client.post("/api/v2/family-circles", headers=parent_headers, json={"name": "Parent-owned"})
    assert created.status_code == 200, created.text
    circle = created.json()
    assert circle["organizer_user_id"] == parent["user_id"]
    assert circle["status"] == "PENDING_PURCHASE"
    assert circle["membership_role"] == "ORGANIZER"

    current = client.get("/api/v2/users/me", headers=parent_headers)
    assert current.status_code == 200
    assert current.json()["has_parent_membership"] is True
    assert current.json()["has_organizer_membership"] is True

    duplicate = client.post("/api/v2/family-circles", headers=parent_headers, json={"name": "Should not create"})
    assert duplicate.status_code == 200
    assert duplicate.json()["id"] == circle["id"]

    circles = client.get("/api/v2/family-circles", headers=parent_headers)
    assert circles.status_code == 200
    assert {item["id"] for item in circles.json()} == {existing_id, circle["id"]}


def test_v2_unscheduled_checkin_is_visible_as_checked_in(v2_api):
    client, database = v2_api
    _, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "unscheduled-organizer-device")
    _, parent_headers = _register(client, "PARENT", "Parent", "unscheduled-parent-device")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Circle"}).json()
    circle_id = circle["id"]
    _activate_test_circle(database, circle_id)
    invitation = client.post(f"/api/v2/family-circles/{circle_id}/invitations", headers=organizer_headers, json={"role": "PARENT"}).json()
    assert client.post(f"/api/v2/invitations/{invitation['token']}/accept", headers=parent_headers).status_code == 200
    checkin = client.post("/api/v2/parents/me/check-ins", headers=parent_headers, json={"idempotency_key": "unscheduled-checkin-1"})
    assert checkin.status_code == 200
    parent_id = client.get(f"/api/v2/family-circles/{circle_id}/parents", headers=organizer_headers).json()[0]["parent_profile_id"]
    status = client.get(f"/api/v2/family-circles/{circle_id}/parents/{parent_id}/status", headers=organizer_headers)
    assert status.status_code == 200
    assert status.json()["state"] == "CHECKED_IN"


def test_v2_identity_and_parent_data_contract(v2_api):
    client, database = v2_api
    _, parent_headers = _register(client, "PARENT", "Parent", "identity-parent-device")
    current = client.get("/api/v2/users/me", headers=parent_headers)
    assert current.status_code == 200
    assert current.json()["role"] == "PARENT"

    _, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "identity-organizer-device")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Circle"}).json()
    _activate_test_circle(database, circle["id"])
    invitation = client.post(f"/api/v2/family-circles/{circle['id']}/invitations", headers=organizer_headers, json={"role": "PARENT"}).json()
    assert client.post(f"/api/v2/invitations/{invitation['token']}/accept", headers=parent_headers).status_code == 200
    assert client.post("/api/v2/parents/me/check-ins", headers=parent_headers, json={"idempotency_key": "identity-checkin-1"}).status_code == 200
    deleted = client.delete("/api/v2/parents/me/data", headers=parent_headers)
    assert deleted.status_code == 200 and deleted.json()["status"] == "parent_data_deleted"
    user_data = client.delete("/api/v2/users/me/data", headers=parent_headers)
    assert user_data.status_code == 200 and user_data.json()["status"] == "user_data_deleted"
    parent_id = client.get(f"/api/v2/family-circles/{circle['id']}/parents", headers=organizer_headers).json()[0]["parent_profile_id"]
    assert client.get(f"/api/v2/family-circles/{circle['id']}/parents/{parent_id}/history", headers=organizer_headers).json() == []


def test_v2_parent_can_create_family_circle(v2_api):
    client, _ = v2_api
    _, parent_headers = _register(client, "PARENT", "Parent", "parent-cannot-organize")
    response = client.post("/api/v2/family-circles", headers=parent_headers, json={})
    assert response.status_code == 200
    assert response.json()["status"] == "PENDING_PURCHASE"


def test_v2_push_templates_are_recipient_localized():
    from v2_notifications import localized_message

    assert localized_message("missed_checkin", "pt-BR", "Mãe")[0] == "Check-in não realizado"
    assert localized_message("missed_checkin", "es", "Mamá")[0] == "Check-in no realizado"
    assert "لم" in localized_message("missed_checkin", "ar", "الأم")[0]
    assert "报平安" in localized_message("missed_checkin", "zh-CN", "妈妈")[1]


def test_v2_invitation_lifecycle_and_removal(v2_api):
    client, database = v2_api
    _, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "organizer-device-2")
    _, member_headers = _register(client, "FAMILY_MEMBER", "Member", "member-device-2")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Circle"}).json()
    circle_id = circle["id"]
    _activate_test_circle(database, circle_id)
    assert client.post(f"/api/v2/family-circles/{circle_id}/invitations", headers=member_headers, json={"role": "FAMILY_MEMBER"}).status_code == 403

    expired = client.post(f"/api/v2/family-circles/{circle_id}/invitations", headers=organizer_headers, json={"role": "FAMILY_MEMBER", "expires_in_hours": 1}).json()
    assert re.fullmatch(r"\d{6}", expired["public_code"])
    assert expired["public_code"] != expired["token"]
    with database.SessionLocal() as db:
        from datetime import timedelta
        from v2_models import FamilyInvitation
        invitation = db.get(FamilyInvitation, expired["invitation_id"])
        invitation.expires_at = datetime.now() - timedelta(minutes=1)
        db.commit()
    expired_response = client.post(f"/api/v2/invitations/{expired['public_code']}/accept", headers=member_headers)
    assert expired_response.status_code == 400
    assert expired_response.json()["detail"] == "invitation code has expired"

    fresh = client.post(f"/api/v2/family-circles/{circle_id}/invitations", headers=organizer_headers, json={"role": "FAMILY_MEMBER"}).json()
    spaced_code = f"{fresh['public_code'][:3]} {fresh['public_code'][3:]}"
    accepted = client.post(f"/api/v2/invitations/{spaced_code}/accept", headers=member_headers)
    assert accepted.status_code == 200
    reused = client.post(f"/api/v2/invitations/{fresh['public_code']}/accept", headers=member_headers)
    assert reused.status_code == 400
    assert reused.json()["detail"] == "invitation code has already been used"
    membership_id = accepted.json()["membership_id"]
    assert client.delete(f"/api/v2/family-circles/{circle_id}/members/{membership_id}", headers=organizer_headers).status_code == 200
    listed = client.get(f"/api/v2/family-circles/{circle_id}/members", headers=organizer_headers)
    assert listed.status_code == 200 and all(member["membership_id"] != membership_id for member in listed.json())
    assert client.get(f"/api/v2/family-circles/{circle_id}", headers=member_headers).status_code == 403
    assert client.get(f"/api/v2/family-circles/{circle_id}").status_code == 401


def test_v2_public_invitation_code_resolves_only_its_invitation_and_legacy_token_still_works(v2_api):
    client, database = v2_api
    _, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "public-code-organizer")
    _, first_headers = _register(client, "FAMILY_MEMBER", "First", "public-code-first")
    _, second_headers = _register(client, "FAMILY_MEMBER", "Second", "public-code-second")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Circle"}).json()
    _activate_test_circle(database, circle["id"])

    first_invitation = client.post(
        f"/api/v2/family-circles/{circle['id']}/invitations",
        headers=organizer_headers,
        json={"role": "FAMILY_MEMBER"},
    ).json()
    legacy_invitation = client.post(
        f"/api/v2/family-circles/{circle['id']}/invitations",
        headers=organizer_headers,
        json={"role": "FAMILY_MEMBER"},
    ).json()

    wrong = client.post("/api/v2/invitations/999 998/accept", headers=first_headers)
    assert wrong.status_code == 400
    assert wrong.json()["detail"] == "invalid invitation code"

    accepted = client.post(
        f"/api/v2/invitations/{first_invitation['public_code']}/accept",
        headers=first_headers,
    )
    assert accepted.status_code == 200
    assert accepted.json()["family_circle_id"] == circle["id"]

    legacy_accepted = client.post(
        f"/api/v2/invitations/{legacy_invitation['token']}/accept",
        headers=second_headers,
    )
    assert legacy_accepted.status_code == 200
    assert legacy_accepted.json()["family_circle_id"] == circle["id"]


def test_v2_public_invitation_code_collision_retries_without_overwriting(v2_api, monkeypatch):
    client, database = v2_api
    _, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "collision-organizer")
    _, first_headers = _register(client, "FAMILY_MEMBER", "First", "collision-first")
    _, second_headers = _register(client, "FAMILY_MEMBER", "Second", "collision-second")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Circle"}).json()
    _activate_test_circle(database, circle["id"])

    generated = iter((123456, 123456, 654321))
    monkeypatch.setattr("v2_routes.secrets.randbelow", lambda _: next(generated))

    first = client.post(
        f"/api/v2/family-circles/{circle['id']}/invitations",
        headers=organizer_headers,
        json={"role": "FAMILY_MEMBER"},
    ).json()
    second = client.post(
        f"/api/v2/family-circles/{circle['id']}/invitations",
        headers=organizer_headers,
        json={"role": "FAMILY_MEMBER"},
    ).json()

    assert first["public_code"] == "123456"
    assert second["public_code"] == "654321"
    first_accept = client.post("/api/v2/invitations/123456/accept", headers=first_headers)
    second_accept = client.post("/api/v2/invitations/654321/accept", headers=second_headers)
    assert first_accept.status_code == second_accept.status_code == 200
    assert first_accept.json()["family_circle_id"] == circle["id"]
    assert second_accept.json()["family_circle_id"] == circle["id"]


def test_v2_public_invitation_code_attempts_are_rate_limited(v2_api):
    client, _ = v2_api
    _, member_headers = _register(client, "FAMILY_MEMBER", "Member", "rate-limit-member")

    for code in ("900001", "900002", "900003", "900004", "900005"):
        assert client.post(f"/api/v2/invitations/{code}/accept", headers=member_headers).status_code == 400

    limited = client.post("/api/v2/invitations/900006/accept", headers=member_headers)
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "900"


def test_v2_scheduler_is_restart_safe(v2_api):
    from datetime import datetime
    import models
    import v2_models
    from v2_worker import process_checkin_windows

    _, database = v2_api
    with database.SessionLocal() as db:
        organizer = models.User(role="family_member", name="Organizer", phone="", device_id="worker-org-device", api_token_hash="worker-org-token")
        parent = models.User(role="parent", name="Parent", phone="", device_id="worker-parent-device", api_token_hash="worker-parent-token")
        db.add_all([organizer, parent])
        db.flush()
        circle = v2_models.FamilyCircle(organizer_user_id=organizer.id, status="active")
        db.add(circle)
        db.flush()
        db.add_all([
            v2_models.FamilyMembership(family_circle_id=circle.id, user_id=organizer.id, role="ORGANIZER", membership_status="active"),
            v2_models.FamilyMembership(family_circle_id=circle.id, user_id=parent.id, role="PARENT", membership_status="active"),
        ])
        db.flush()
        profile = v2_models.ParentProfile(user_id=parent.id, family_circle_id=circle.id, display_name="Parent", timezone="America/Sao_Paulo", active=True)
        db.add(profile)
        db.flush()
        schedule = v2_models.CheckInSchedule(parent_profile_id=profile.id, local_time="09:00", timezone="America/Sao_Paulo", grace_period_minutes=30, days_of_week="0,1,2,3,4,5,6", enabled=True)
        db.add(schedule)
        db.commit()
        now = datetime(2026, 1, 15, 14, 0)  # 11:00 parent-local, after grace
        first = process_checkin_windows(db, now)
        second = process_checkin_windows(db, now)
        assert first >= 1
        assert second == 0
        assert db.query(v2_models.CheckInEvent).filter(v2_models.CheckInEvent.status == "MISSED").count() >= 1


def test_v2_mock_multidevice_localized_workflow(v2_api):
    client, database = v2_api
    _, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "mock-organizer-device", "en")
    _, parent_headers = _register(client, "PARENT", "Mãe", "mock-parent-device", "pt-BR")
    _, arabic_headers = _register(client, "FAMILY_MEMBER", "Family", "mock-arabic-device", "ar")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Mock family"}).json()
    circle_id = circle["id"]
    _activate_test_circle(database, circle_id)
    for role, headers in (("PARENT", parent_headers), ("FAMILY_MEMBER", arabic_headers)):
        invite = client.post(f"/api/v2/family-circles/{circle_id}/invitations", headers=organizer_headers, json={"role": role}).json()
        assert client.post(f"/api/v2/invitations/{invite['token']}/accept", headers=headers).status_code == 200
    assert client.post("/api/v2/devices/me/push-token", headers=organizer_headers, json={"device_id": "mock-organizer-device", "platform": "android", "push_provider": "fcm", "push_token": "fcm-mock-token"}).status_code == 200
    assert client.post("/api/v2/devices/me/push-token", headers=arabic_headers, json={"device_id": "mock-arabic-device", "platform": "android_huawei", "push_provider": "hms", "push_token": "hms-mock-token"}).status_code == 200
    assert client.post("/api/v2/parents/me/check-ins", headers=parent_headers, json={"battery_level": 91, "idempotency_key": "mock-checkin-key"}).status_code == 200
    with database.SessionLocal() as db:
        from v2_models import V2NotificationDelivery
        rows = db.query(V2NotificationDelivery).order_by(V2NotificationDelivery.id.asc()).all()
        assert {row.locale_tag for row in rows} == {"en", "ar"}
        assert any(row.provider == "fcm" and "checked in" in row.body for row in rows)
        assert any(row.provider == "android_huawei" and "أكد" in row.body for row in rows)
