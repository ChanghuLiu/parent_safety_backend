from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_v2_family import _activate_test_circle, _register


def test_legacy_parent_membership_drives_clean_data_detection(v2_api):
    client, database = v2_api
    organizer, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "legacy-owner-device")
    old_parent, old_parent_headers = _register(client, "PARENT", "Parent", "legacy-parent-device")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Legacy"}).json()
    _activate_test_circle(database, circle["id"])
    invite = client.post(f"/api/v2/family-circles/{circle['id']}/invitations", headers=organizer_headers, json={"role": "PARENT"}).json()
    assert client.post(f"/api/v2/invitations/{invite['token']}/accept", headers=old_parent_headers).status_code == 200
    with database.SessionLocal() as db:
        from models import User
        db.get(User, old_parent["user_id"]).role = "family_member"
        db.commit()
    fresh = client.post(
        "/api/v2/register",
        json={"role": "PARENT", "name": "Replacement", "phone": "", "device_id": "legacy-parent-device", "locale_tag": "en"},
    )
    assert fresh.status_code == 200, fresh.text
    replacement = fresh.json()
    assert replacement["user_id"] != old_parent["user_id"]
    with database.SessionLocal() as db:
        from models import User
        from v2_models import FamilyMembership
        pending = db.get(User, replacement["user_id"])
        assert pending.recovery_device_id == "legacy-parent-device"
        assert db.query(FamilyMembership).filter(FamilyMembership.user_id == pending.id, FamilyMembership.membership_status == "active").count() == 0


def test_explicit_reconnect_rebinds_unpaired_parent_without_device_match(v2_api):
    client, database = v2_api
    organizer, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "reconnect-owner-device")
    old_parent, old_parent_headers = _register(client, "PARENT", "Parent", "original-parent-device")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Reconnect"}).json()
    _activate_test_circle(database, circle["id"])
    first_invite = client.post(f"/api/v2/family-circles/{circle['id']}/invitations", headers=organizer_headers, json={"role": "PARENT"}).json()
    assert first_invite["purpose"] == "PARENT_CONNECT"
    assert client.post(f"/api/v2/invitations/{first_invite['token']}/accept", headers=old_parent_headers).status_code == 200
    checkin = client.post("/api/v2/parents/me/check-ins", headers=old_parent_headers, json={"idempotency_key": "history-before-reconnect"})
    assert checkin.status_code == 200, checkin.text
    reconnect = client.post(f"/api/v2/family-circles/{circle['id']}/invitations", headers=organizer_headers, json={"role": "PARENT"}).json()
    assert reconnect["purpose"] == "PARENT_RECONNECT"
    replacement, replacement_headers = _register(client, "PARENT", "Replacement", "replacement-phone-device")
    accepted = client.post(f"/api/v2/invitations/{reconnect['token']}/accept", headers=replacement_headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["parent_profile_id"] == 1
    repeated = client.post(f"/api/v2/invitations/{reconnect['token']}/accept", headers=replacement_headers)
    assert repeated.status_code == 200, repeated.text
    history = client.get(f"/api/v2/family-circles/{circle['id']}/parents/1/history", headers=organizer_headers)
    assert history.status_code == 200
    with database.SessionLocal() as db:
        from models import User
        from v2_models import FamilyMembership, ParentProfile, FamilyInvitation
        profile = db.get(ParentProfile, 1)
        assert profile.user_id == replacement["user_id"]
        assert db.query(ParentProfile).filter(ParentProfile.family_circle_id == circle["id"], ParentProfile.active.is_(True)).count() == 1
        assert db.query(FamilyMembership).filter(FamilyMembership.user_id == old_parent["user_id"], FamilyMembership.membership_status == "active").count() == 0
        assert db.query(FamilyMembership).filter(FamilyMembership.user_id == replacement["user_id"], FamilyMembership.role == "PARENT", FamilyMembership.membership_status == "active").count() == 1
        assert db.get(User, old_parent["user_id"]).api_token_hash is None
        assert db.get(FamilyInvitation, reconnect["invitation_id"]).status == "accepted"


def test_unpaired_stale_parent_does_not_block_reconnect(v2_api):
    client, database = v2_api
    organizer, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "stale-owner-device")
    old_parent, old_parent_headers = _register(client, "PARENT", "Parent", "stale-original-device")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Stale"}).json()
    _activate_test_circle(database, circle["id"])
    first = client.post(f"/api/v2/family-circles/{circle['id']}/invitations", headers=organizer_headers, json={"role": "PARENT"}).json()
    assert client.post(f"/api/v2/invitations/{first['token']}/accept", headers=old_parent_headers).status_code == 200
    reconnect = client.post(f"/api/v2/family-circles/{circle['id']}/invitations", headers=organizer_headers, json={"role": "PARENT"}).json()
    stale, _ = _register(client, "PARENT", "Stale", "stale-unpaired-device")
    assert stale["user_id"] != old_parent["user_id"]
    replacement, replacement_headers = _register(client, "PARENT", "Current", "current-unpaired-device")
    assert client.post(f"/api/v2/invitations/{reconnect['token']}/accept", headers=replacement_headers).status_code == 200
    with database.SessionLocal() as db:
        from v2_models import FamilyMembership
        assert db.query(FamilyMembership).filter(FamilyMembership.user_id == stale["user_id"], FamilyMembership.membership_status == "active").count() == 0
