import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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
    import v2_routes
    import main

    importlib.reload(database)
    importlib.reload(models)
    importlib.reload(v2_models)
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
    status = client.get(f"/api/v2/family-circles/{circle_id}/parents/{parent_id}/status", headers=sibling_headers)
    assert status.status_code == 200
    assert client.put("/api/v2/parents/me/schedule", headers=organizer_headers, json={"local_time": "10:00", "timezone": "UTC"}).status_code == 403

    other, other_headers = _register(client, "FAMILY_MEMBER", "Other", "other-device", "ar")
    other_circle = client.post("/api/v2/family-circles", headers=other_headers, json={"name": "Other"})
    other_id = other_circle.json()["id"]
    assert client.get(f"/api/v2/family-circles/{other_id}/parents/{parent_id}/status", headers=other_headers).status_code in {403, 404}


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


def test_v2_parent_cannot_create_family_circle(v2_api):
    client, _ = v2_api
    _, parent_headers = _register(client, "PARENT", "Parent", "parent-cannot-organize")
    response = client.post("/api/v2/family-circles", headers=parent_headers, json={})
    assert response.status_code == 403


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
    with database.SessionLocal() as db:
        from datetime import timedelta
        from v2_models import FamilyInvitation
        invitation = db.get(FamilyInvitation, expired["invitation_id"])
        invitation.expires_at = datetime.now() - timedelta(minutes=1)
        db.commit()
    assert client.post(f"/api/v2/invitations/{expired['token']}/accept", headers=member_headers).status_code == 400

    fresh = client.post(f"/api/v2/family-circles/{circle_id}/invitations", headers=organizer_headers, json={"role": "FAMILY_MEMBER"}).json()
    accepted = client.post(f"/api/v2/invitations/{fresh['token']}/accept", headers=member_headers)
    assert accepted.status_code == 200
    assert client.post(f"/api/v2/invitations/{fresh['token']}/accept", headers=member_headers).status_code == 400
    membership_id = accepted.json()["membership_id"]
    assert client.delete(f"/api/v2/family-circles/{circle_id}/members/{membership_id}", headers=organizer_headers).status_code == 200
    listed = client.get(f"/api/v2/family-circles/{circle_id}/members", headers=organizer_headers)
    assert listed.status_code == 200 and all(member["membership_id"] != membership_id for member in listed.json())
    assert client.get(f"/api/v2/family-circles/{circle_id}", headers=member_headers).status_code == 403
    assert client.get(f"/api/v2/family-circles/{circle_id}").status_code == 401


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
