from test_v2_family import v2_api


def _register(client, role, name, device, locale="en"):
    response = client.post(
        "/api/v2/register",
        json={"role": role, "name": name, "phone": "", "device_id": device, "locale_tag": locale},
    )
    assert response.status_code == 200, response.text
    value = response.json()
    return value, {"Authorization": f"Bearer {value['api_token']}"}


def _activate_test_circle(database, circle_id):
    from v2_models import FamilyCircle

    with database.SessionLocal() as db:
        db.get(FamilyCircle, circle_id).status = "ACTIVE"
        db.commit()


def _active_parent_fixture(client, database, device="delete-parent-device"):
    parent, parent_headers = _register(client, "PARENT", "Disposable Parent", device)
    organizer, organizer_headers = _register(
        client, "FAMILY_MEMBER", "Disposable Organizer", f"{device}-organizer"
    )
    circle = client.post(
        "/api/v2/family-circles",
        headers=organizer_headers,
        json={"name": "Disposable Circle"},
    ).json()
    _activate_test_circle(database, circle["id"])
    invitation = client.post(
        f"/api/v2/family-circles/{circle['id']}/invitations",
        headers=organizer_headers,
        json={"role": "PARENT", "relationship": "parent"},
    ).json()
    accepted = client.post(
        f"/api/v2/invitations/{invitation['token']}/accept",
        headers=parent_headers,
    )
    assert accepted.status_code == 200, accepted.text
    return parent, parent_headers, organizer, organizer_headers, circle["id"]


def test_parent_account_deletion_is_atomic_and_allows_safe_re_registration(v2_api):
    client, database = v2_api
    parent, parent_headers, organizer, organizer_headers, circle_id = _active_parent_fixture(client, database)
    token_response = client.post(
        "/api/v2/devices/me/push-token",
        headers=parent_headers,
        json={
            "device_id": "delete-parent-device",
            "platform": "android",
            "push_provider": "fcm",
            "push_token": "delete-parent-token",
        },
    )
    assert token_response.status_code == 200, token_response.text
    assert client.post(
        "/api/v2/parents/me/check-ins",
        headers=parent_headers,
        json={"idempotency_key": "delete-parent-checkin"},
    ).status_code == 200
    assert client.post(
        "/api/v2/parents/me/help-requests",
        headers=parent_headers,
        json={"request_type": "call_back", "message": "Please call"},
    ).status_code == 200

    from models import DevicePushToken, DeviceStatus, User
    from sqlalchemy import text
    from v2_models import FamilyMembership, ParentProfile, PurchaseEntitlement, V2NotificationDelivery

    with database.SessionLocal() as db:
        db.add(DeviceStatus(user_id=parent["user_id"], platform="android", device_uuid="delete-parent-device"))
        db.add(
            PurchaseEntitlement(
                family_circle_id=circle_id,
                organizer_user_id=organizer["user_id"],
                product_id="parent_checkin_family_lifetime",
                package_name="com.parent.safety.check",
                purchase_token_hash="deletion-entitlement-token",
                purchase_state="PURCHASED",
                verification_state="VERIFIED",
                acknowledgement_state="ACKNOWLEDGED",
            )
        )
        db.add(
            V2NotificationDelivery(
                event_key="deletion-recipient-event",
                recipient_user_id=parent["user_id"],
                locale_tag="en",
                title="Test",
                body="Test",
                status="pending",
            )
        )
        db.commit()

    deleted = client.delete("/api/v2/users/me", headers=parent_headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"success": True, "status": "deleted"}
    with database.SessionLocal() as db:
        assert db.get(User, parent["user_id"]).api_token_hash is None
    assert client.get("/api/v2/users/me", headers=parent_headers).status_code == 401
    assert client.delete("/api/v2/users/me", headers=parent_headers).status_code == 401

    with database.SessionLocal() as db:
        user = db.get(User, parent["user_id"])
        membership = db.query(FamilyMembership).filter(
            FamilyMembership.family_circle_id == circle_id,
            FamilyMembership.user_id == parent["user_id"],
        ).one()
        assert user.api_token_hash is None
        assert user.fcm_token is None
        assert user.fcm_token_invalidated_at is not None
        assert user.device_id.startswith("deleted-")
        assert membership.membership_status == "deleted"
        assert db.query(ParentProfile).filter(ParentProfile.user_id == parent["user_id"]).count() == 0
        assert db.query(DevicePushToken).filter(DevicePushToken.user_id == parent["user_id"]).count() == 0
        assert db.query(DeviceStatus).filter(DeviceStatus.user_id == parent["user_id"]).count() == 0
        assert db.query(V2NotificationDelivery).filter(V2NotificationDelivery.recipient_user_id == parent["user_id"]).count() == 0
        assert db.query(PurchaseEntitlement).filter(PurchaseEntitlement.family_circle_id == circle_id).one().organizer_user_id == organizer["user_id"]
        assert db.execute(text("PRAGMA integrity_check")).scalar() == "ok"

    re_registered = client.post(
        "/api/v2/register",
        json={"role": "PARENT", "name": "Replacement Parent", "phone": "", "device_id": "delete-parent-device", "locale_tag": "en"},
    )
    assert re_registered.status_code == 200, re_registered.text
    new_headers = {"Authorization": f"Bearer {re_registered.json()['api_token']}"}
    new_token = client.post(
        "/api/v2/devices/me/push-token",
        headers=new_headers,
        json={
            "device_id": "delete-parent-device",
            "platform": "android",
            "push_provider": "fcm",
            "push_token": "delete-parent-token-new",
        },
    )
    assert new_token.status_code == 200, new_token.text


def test_caregiver_deletion_requires_circle_resolution_and_is_safe_without_circle(v2_api):
    client, database = v2_api
    organizer, organizer_headers = _register(client, "FAMILY_MEMBER", "Organizer", "delete-organizer-device")
    blocked_circle = client.post(
        "/api/v2/family-circles",
        headers=organizer_headers,
        json={"name": "Owned Circle"},
    )
    assert blocked_circle.status_code == 200
    blocked = client.delete("/api/v2/users/me", headers=organizer_headers)
    assert blocked.status_code == 409
    assert client.get("/api/v2/users/me", headers=organizer_headers).status_code == 200

    disposable, disposable_headers = _register(client, "FAMILY_MEMBER", "Disposable", "delete-no-circle-device")
    deleted = client.delete("/api/v2/users/me", headers=disposable_headers)
    assert deleted.status_code == 200, deleted.text
    assert client.get("/api/v2/users/me", headers=disposable_headers).status_code == 401
    assert client.post(
        "/api/v2/register",
        json={"role": "FAMILY_MEMBER", "name": "Recreated", "phone": "", "device_id": "delete-no-circle-device", "locale_tag": "en"},
    ).status_code == 200
    assert disposable["user_id"] != organizer["user_id"]

    with database.SessionLocal() as db:
        from sqlalchemy import text

        assert db.execute(text("PRAGMA integrity_check")).scalar() == "ok"


def test_delete_all_data_keeps_identity_and_membership_but_removes_user_data(v2_api):
    client, database = v2_api
    from sqlalchemy import text

    parent, parent_headers, _, _, circle_id = _active_parent_fixture(client, database, "delete-data-parent-device")
    assert client.post(
        "/api/v2/devices/me/push-token",
        headers=parent_headers,
        json={"device_id": "delete-data-parent-device", "platform": "android", "push_provider": "fcm", "push_token": "delete-data-token"},
    ).status_code == 200
    assert client.post(
        "/api/v2/parents/me/check-ins",
        headers=parent_headers,
        json={"idempotency_key": "delete-data-checkin"},
    ).status_code == 200
    assert client.post(
        "/api/v2/parents/me/help-requests",
        headers=parent_headers,
        json={"request_type": "call_back", "message": "Please call"},
    ).status_code == 200

    parent_data = client.delete("/api/v2/parents/me/data", headers=parent_headers)
    user_data = client.delete("/api/v2/users/me/data", headers=parent_headers)
    assert parent_data.status_code == 200
    assert user_data.status_code == 200
    assert client.get("/api/v2/users/me", headers=parent_headers).status_code == 200
    assert client.delete("/api/v2/parents/me/data", headers=parent_headers).status_code == 200
    assert client.delete("/api/v2/users/me/data", headers=parent_headers).status_code == 200

    with database.SessionLocal() as db:
        from models import DevicePushToken, DeviceStatus, User
        from v2_models import CheckInEvent, FamilyMembership, ParentProfile, V2HelpRequest, V2NotificationDelivery

        assert db.get(User, parent["user_id"]).api_token_hash is not None
        assert db.query(FamilyMembership).filter(FamilyMembership.family_circle_id == circle_id, FamilyMembership.user_id == parent["user_id"], FamilyMembership.membership_status == "active").count() == 1
        profile = db.query(ParentProfile).filter(ParentProfile.user_id == parent["user_id"], ParentProfile.active.is_(True)).one()
        assert db.query(CheckInEvent).filter(CheckInEvent.parent_profile_id == profile.id).count() == 0
        assert db.query(V2HelpRequest).filter(V2HelpRequest.parent_profile_id == profile.id).count() == 0
        assert db.query(DevicePushToken).filter(DevicePushToken.user_id == parent["user_id"]).count() == 0
        assert db.query(DeviceStatus).filter(DeviceStatus.user_id == parent["user_id"]).count() == 0
        assert db.query(V2NotificationDelivery).filter(V2NotificationDelivery.recipient_user_id == parent["user_id"]).count() == 0
        assert db.execute(text("PRAGMA integrity_check")).scalar() == "ok"


def test_account_deletion_only_affects_authenticated_user_and_no_invalid_model_field_remains(v2_api):
    client, _ = v2_api
    first, first_headers = _register(client, "PARENT", "First", "delete-owner-a")
    second, second_headers = _register(client, "PARENT", "Second", "delete-owner-b")
    assert client.delete("/api/v2/users/me", headers=second_headers).status_code == 200
    assert client.get("/api/v2/users/me", headers=first_headers).status_code == 200
    assert first["user_id"] != second["user_id"]

    source = open("v2_routes.py", encoding="utf-8").read()
    assert "current_user.invalidated_at" not in source
