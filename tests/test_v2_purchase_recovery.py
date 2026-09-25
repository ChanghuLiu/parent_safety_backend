from test_v2_billing import PACKAGE, PRODUCT, _pending_circle, _verify_payload, billing_api
from test_v2_family import _register, v2_api


def _purchase_payload(device: str, token: str):
    return {"device_id": device, "purchase_token": token}


def test_purchase_recovery_preserves_owner_circle_and_allows_replay(billing_api):
    client, database, verifier = billing_api
    organizer, headers, circle_id = _pending_circle(client)
    token = "purchase-recovery-token"
    assert client.post("/api/v2/billing/google/verify", headers=headers, json=_verify_payload(token, circle_id)).status_code == 200

    recovered = client.post("/api/v2/organizer/recover-with-purchase", json=_purchase_payload("recovered-device-1", token))
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["user_id"] == organizer["user_id"]
    assert recovered.json()["recovery_code"] not in recovered.text.replace(recovered.json()["recovery_code"], "")
    recovered_headers = {"Authorization": f"Bearer {recovered.json()['api_token']}"}
    assert client.get("/api/v2/users/me", headers=recovered_headers).status_code == 200
    assert client.get("/api/v2/users/me", headers=headers).status_code == 401

    replay = client.post("/api/v2/organizer/recover-with-purchase", json=_purchase_payload("recovered-device-2", token))
    assert replay.status_code == 200, replay.text
    assert replay.json()["user_id"] == organizer["user_id"]
    assert client.get("/api/v2/users/me", headers=recovered_headers).status_code == 401

    with database.SessionLocal() as db:
        from models import OrganizerPurchaseRecoveryAttempt, User
        from v2_models import FamilyCircle, FamilyMembership, PurchaseEntitlement
        user = db.get(User, organizer["user_id"])
        entitlement = db.query(PurchaseEntitlement).one()
        assert user.device_id == "recovered-device-2"
        assert entitlement.organizer_user_id == organizer["user_id"]
        assert entitlement.family_circle_id == circle_id
        assert db.get(FamilyCircle, circle_id).organizer_user_id == organizer["user_id"]
        assert db.query(FamilyMembership).filter(FamilyMembership.role == "ORGANIZER").count() == 1
        assert db.query(OrganizerPurchaseRecoveryAttempt).filter_by(success=True).count() == 2


def test_purchase_recovery_rejects_invalid_purchase_and_rate_limits(billing_api):
    client, database, verifier = billing_api
    organizer, headers, circle_id = _pending_circle(client)
    token = "purchase-recovery-invalid-token"
    assert client.post("/api/v2/billing/google/verify", headers=headers, json=_verify_payload(token, circle_id)).status_code == 200

    verifier.purchase.package_name = "com.other.app"
    assert client.post("/api/v2/organizer/recover-with-purchase", json=_purchase_payload("rate-device", token)).status_code == 400
    verifier.purchase.package_name = PACKAGE
    verifier.purchase.product_id = "wrong-product"
    assert client.post("/api/v2/organizer/recover-with-purchase", json=_purchase_payload("rate-device", token)).status_code == 400
    verifier.purchase.product_id = PRODUCT
    verifier.purchase.purchase_state = "CANCELED"
    assert client.post("/api/v2/organizer/recover-with-purchase", json=_purchase_payload("rate-device", token)).status_code == 400
    verifier.purchase.purchase_state = "PURCHASED"
    for _ in range(2):
        assert client.post("/api/v2/organizer/recover-with-purchase", json=_purchase_payload("rate-device", "unknown-token")).status_code in {404, 502}
    limited = client.post("/api/v2/organizer/recover-with-purchase", json=_purchase_payload("rate-device", token))
    assert limited.status_code == 429

    with database.SessionLocal() as db:
        from models import OrganizerPurchaseRecoveryAttempt
        assert db.query(OrganizerPurchaseRecoveryAttempt).count() == 5


def test_authenticated_rotation_invalidates_old_code(v2_api):
    client, database = v2_api
    registered, headers = _register(client, "FAMILY_MEMBER", "Organizer", "rotation-device")
    circle = client.post("/api/v2/family-circles", headers=headers, json={"name": "Family"})
    assert circle.status_code == 200
    old_code = registered["recovery_code"]
    rotated = client.post("/api/v2/organizer/recovery-code/rotate", headers=headers)
    assert rotated.status_code == 200, rotated.text
    new_code = rotated.json()["recovery_code"]
    assert new_code != old_code
    assert client.post("/api/v2/organizer/recover", json={"device_id": "rotation-device", "recovery_code": old_code}).status_code == 401
    assert client.post("/api/v2/organizer/recover", json={"device_id": "rotation-device", "recovery_code": new_code}).status_code == 200
