import importlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytest_plugins = ["test_v2_family"]

from test_v2_family import _activate_test_circle, _register


def test_parent_reinstall_is_pending_until_original_organizer_authorizes(v2_api):
    client, database = v2_api
    organizer, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "recovery-organizer-device")
    old_parent, old_parent_headers = _register(client, "PARENT", "Old Parent", "recovery-parent-device")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Recovery"}).json()
    circle_id = circle["id"]
    _activate_test_circle(database, circle_id)
    invite = client.post(f"/api/v2/family-circles/{circle_id}/invitations", headers=organizer_headers, json={"role": "PARENT"}).json()
    assert client.post(f"/api/v2/invitations/{invite['token']}/accept", headers=old_parent_headers).status_code == 200
    token_response = client.post(
        "/api/v2/devices/me/push-token",
        headers=old_parent_headers,
        json={"device_id": "recovery-parent-device", "platform": "android", "push_provider": "fcm", "push_token": "old-recovery-token"},
    )
    assert token_response.status_code == 200
    with database.SessionLocal() as db:
        from v2_models import PurchaseEntitlement
        db.add(PurchaseEntitlement(
            family_circle_id=circle_id,
            organizer_user_id=organizer["user_id"],
            product_id="parent_checkin_family_lifetime",
            package_name="com.parent.safety.check",
            purchase_token_hash="entitlement-recovery-hash",
            purchase_state="PURCHASED",
            verification_state="VERIFIED",
            acknowledgement_state="ACKNOWLEDGED",
        ))
        db.commit()
    recovery_invite = client.post(f"/api/v2/family-circles/{circle_id}/invitations", headers=organizer_headers, json={"role": "PARENT"}).json()

    fresh = client.post("/api/v2/register", json={"role": "PARENT", "name": "Replacement", "phone": "", "device_id": "recovery-parent-device", "locale_tag": "en"})
    assert fresh.status_code == 200, fresh.text
    replacement = fresh.json()
    assert replacement["user_id"] != old_parent["user_id"]
    replacement_headers = {"Authorization": f"Bearer {replacement['api_token']}"}
    current = client.get("/api/v2/users/me", headers=replacement_headers)
    assert current.status_code == 200
    assert current.json()["has_parent_membership"] is False
    with database.SessionLocal() as db:
        from models import User
        from v2_models import FamilyMembership, ParentProfile
        assert db.query(User).filter(User.device_id == "recovery-parent-device", User.role == "parent").count() == 1
        assert db.query(FamilyMembership).filter(FamilyMembership.user_id == replacement["user_id"], FamilyMembership.membership_status == "active").count() == 0
        assert db.query(ParentProfile).filter(ParentProfile.user_id == old_parent["user_id"], ParentProfile.active.is_(True)).count() == 1

    recovered = client.post(f"/api/v2/invitations/{recovery_invite['token']}/accept", headers=replacement_headers)
    assert recovered.status_code == 200, recovered.text
    new_token_response = client.post(
        "/api/v2/devices/me/push-token",
        headers=replacement_headers,
        json={"device_id": "recovery-parent-device", "platform": "android", "push_provider": "fcm", "push_token": "new-recovery-token"},
    )
    assert new_token_response.status_code == 200, new_token_response.text
    repeated = client.post(f"/api/v2/invitations/{recovery_invite['token']}/accept", headers=replacement_headers)
    assert repeated.status_code == 200, repeated.text
    with database.SessionLocal() as db:
        from models import User
        from models import DevicePushToken
        from v2_models import FamilyMembership, FamilyCircle, ParentProfile, PurchaseEntitlement
        assert db.query(FamilyCircle).filter(FamilyCircle.id == circle_id).count() == 1
        assert db.query(FamilyMembership).filter(FamilyMembership.family_circle_id == circle_id, FamilyMembership.role == "PARENT", FamilyMembership.membership_status == "active").count() == 1
        assert db.query(ParentProfile).filter(ParentProfile.family_circle_id == circle_id, ParentProfile.active.is_(True)).count() == 1
        assert db.query(User).filter(User.id == old_parent["user_id"], User.api_token_hash.is_(None)).count() == 1
        recovered_profile = db.query(ParentProfile).filter(ParentProfile.user_id == replacement["user_id"], ParentProfile.active.is_(True)).one()
        assert recovered_profile.recovered_from_parent_profile_id is not None
        old_token = db.query(DevicePushToken).filter(DevicePushToken.user_id == old_parent["user_id"]).one()
        assert old_token.push_token_invalidated_at is not None
        assert db.query(DevicePushToken).filter(DevicePushToken.user_id == replacement["user_id"], DevicePushToken.push_provider == "fcm").count() == 1
        entitlement = db.query(PurchaseEntitlement).filter(PurchaseEntitlement.family_circle_id == circle_id).one()
        assert entitlement.organizer_user_id == organizer["user_id"]
        assert entitlement.verification_state == "VERIFIED"

    history = client.get(f"/api/v2/family-circles/{circle_id}/parents/{recovered.json()['parent_profile_id']}/history", headers=organizer_headers)
    assert history.status_code == 200


def test_device_id_alone_does_not_recover_parent_account(v2_api):
    client, database = v2_api
    organizer, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "unauthorized-organizer-device")
    old_parent, old_parent_headers = _register(client, "PARENT", "Old Parent", "unauthorized-parent-device")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Protected"}).json()
    _activate_test_circle(database, circle["id"])
    invite = client.post(f"/api/v2/family-circles/{circle['id']}/invitations", headers=organizer_headers, json={"role": "PARENT"}).json()
    assert client.post(f"/api/v2/invitations/{invite['token']}/accept", headers=old_parent_headers).status_code == 200
    recovery_invite = client.post(f"/api/v2/family-circles/{circle['id']}/invitations", headers=organizer_headers, json={"role": "PARENT"}).json()
    intruder, intruder_headers = _register(client, "PARENT", "Intruder", "intruder-device")
    # A normal Parent identity cannot use a device ID to claim the protected
    # membership, and receives no old account token or active membership.
    assert intruder["user_id"] != old_parent["user_id"]
    assert client.post(f"/api/v2/invitations/{recovery_invite['token']}/accept", headers=intruder_headers).status_code == 409
    with database.SessionLocal() as db:
        from v2_models import FamilyMembership
        assert db.query(FamilyMembership).filter(FamilyMembership.family_circle_id == circle["id"], FamilyMembership.role == "PARENT", FamilyMembership.membership_status == "active").count() == 1


def test_pending_recovery_cannot_join_another_circle(v2_api):
    client, database = v2_api
    organizer, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "cross-circle-organizer-device")
    other, other_headers = _register(client, "FAMILY_MEMBER", "Other", "cross-circle-other-device")
    old_parent, old_parent_headers = _register(client, "PARENT", "Old Parent", "cross-circle-parent-device")
    circle1 = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Original"}).json()
    _activate_test_circle(database, circle1["id"])
    invite1 = client.post(f"/api/v2/family-circles/{circle1['id']}/invitations", headers=organizer_headers, json={"role": "PARENT"}).json()
    assert client.post(f"/api/v2/invitations/{invite1['token']}/accept", headers=old_parent_headers).status_code == 200
    circle2 = client.post("/api/v2/family-circles", headers=other_headers, json={"name": "Other"}).json()
    _activate_test_circle(database, circle2["id"])
    invite2 = client.post(f"/api/v2/family-circles/{circle2['id']}/invitations", headers=other_headers, json={"role": "PARENT"}).json()
    fresh = client.post("/api/v2/register", json={"role": "PARENT", "name": "Replacement", "phone": "", "device_id": "cross-circle-parent-device", "locale_tag": "en"}).json()
    replacement_headers = {"Authorization": f"Bearer {fresh['api_token']}"}
    blocked = client.post(f"/api/v2/invitations/{invite2['token']}/accept", headers=replacement_headers)
    assert blocked.status_code == 403


def test_reminder_and_check_now_target_parent_after_recovery(v2_api):
    client, database = v2_api
    _, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "push-organizer-device")
    parent, parent_headers = _register(client, "PARENT", "Parent", "push-parent-device")
    circle = client.post("/api/v2/family-circles", headers=organizer_headers, json={"name": "Push target"}).json()
    _activate_test_circle(database, circle["id"])
    invite = client.post(f"/api/v2/family-circles/{circle['id']}/invitations", headers=organizer_headers, json={"role": "PARENT"}).json()
    assert client.post(f"/api/v2/invitations/{invite['token']}/accept", headers=parent_headers).status_code == 200
    parent_profile_id = client.get(f"/api/v2/family-circles/{circle['id']}/parents", headers=organizer_headers).json()[0]["parent_profile_id"]
    assert client.post(f"/api/v2/family-circles/{circle['id']}/parents/{parent_profile_id}/reminders", headers=organizer_headers).status_code == 200
    assert client.post(f"/api/v2/family-circles/{circle['id']}/parents/{parent_profile_id}/check-now", headers=organizer_headers).status_code == 200
    with database.SessionLocal() as db:
        from v2_models import V2NotificationDelivery
        deliveries = db.query(V2NotificationDelivery).order_by(V2NotificationDelivery.id.asc()).all()
        assert {delivery.recipient_user_id for delivery in deliveries} == {parent["user_id"]}
