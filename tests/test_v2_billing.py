from dataclasses import dataclass

import pytest
from cryptography.fernet import Fernet

from test_v2_family import _register, v2_api


PRODUCT = "parent_checkin_family_lifetime"
PACKAGE = "com.parent.safety.check"


@dataclass
class FakePurchase:
    package_name: str = PACKAGE
    product_id: str = PRODUCT
    purchase_state: str = "PURCHASED"
    acknowledgement_state: str = "ACKNOWLEDGEMENT_STATE_PENDING"
    obfuscated_account_id: str | None = "account-hash"
    purchased_at: object | None = None


class FakeVerifier:
    def __init__(self, purchase=None):
        self.purchase = purchase or FakePurchase()
        self.acknowledged_tokens = []

    def get_purchase(self, purchase_token):
        return self.purchase

    def acknowledge(self, product_id, purchase_token):
        self.acknowledged_tokens.append((product_id, purchase_token))
        return True


@pytest.fixture()
def billing_api(v2_api, monkeypatch):
    client, database = v2_api
    monkeypatch.setenv("PURCHASE_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    import main
    import v2_billing

    verifier = FakeVerifier()
    main.app.dependency_overrides[v2_billing.configured_google_play_verifier] = lambda: verifier
    return client, database, verifier


def _pending_circle(client):
    organizer, headers = _register(client, "FAMILY_MEMBER", "Organizer", "billing-organizer")
    response = client.post("/api/v2/family-circles", headers=headers, json={"name": "Billing family"})
    assert response.status_code == 200, response.text
    return organizer, headers, response.json()["id"]


def _verify_payload(token="token-1", circle_id=1):
    return {
        "family_circle_id": circle_id,
        "product_id": PRODUCT,
        "purchase_token": token,
        "obfuscated_account_id": "account-hash",
    }


def test_pending_purchase_does_not_activate_or_acknowledge(billing_api):
    client, database, verifier = billing_api
    _, headers, circle_id = _pending_circle(client)
    verifier.purchase.purchase_state = "PENDING"

    response = client.post("/api/v2/billing/google/verify", headers=headers, json=_verify_payload(circle_id=circle_id))
    assert response.status_code == 200, response.text
    assert response.json()["verification_state"] == "PENDING"
    assert response.json()["acknowledgement_state"] == "PENDING"
    assert verifier.acknowledged_tokens == []
    with database.SessionLocal() as db:
        from v2_models import FamilyCircle
        assert db.get(FamilyCircle, circle_id).status == "PENDING_PURCHASE"


def test_verified_purchase_activates_idempotently_and_never_returns_token(billing_api):
    client, database, verifier = billing_api
    _, headers, circle_id = _pending_circle(client)
    token = "verified-token-1"

    first = client.post("/api/v2/billing/google/verify", headers=headers, json=_verify_payload(token, circle_id))
    second = client.post("/api/v2/billing/google/verify", headers=headers, json=_verify_payload(token, circle_id))
    assert first.status_code == second.status_code == 200
    assert first.json()["verification_state"] == "VERIFIED"
    assert first.json()["acknowledgement_state"] == "ACKNOWLEDGED"
    assert token not in first.text and token not in second.text
    assert len(verifier.acknowledged_tokens) == 1
    with database.SessionLocal() as db:
        from v2_models import FamilyCircle
        assert db.get(FamilyCircle, circle_id).status == "ACTIVE"


def test_wrong_product_package_and_parent_are_denied(billing_api):
    client, _, verifier = billing_api
    _, organizer_headers, circle_id = _pending_circle(client)

    wrong_product = _verify_payload(circle_id=circle_id)
    wrong_product["product_id"] = "wrong-product"
    assert client.post("/api/v2/billing/google/verify", headers=organizer_headers, json=wrong_product).status_code == 400

    verifier.purchase.package_name = "com.other.app"
    assert client.post("/api/v2/billing/google/verify", headers=organizer_headers, json=_verify_payload(circle_id=circle_id)).status_code == 400
    verifier.purchase.package_name = PACKAGE

    _, parent_headers = _register(client, "PARENT", "Parent", "billing-parent")
    assert client.post("/api/v2/billing/google/verify", headers=parent_headers, json=_verify_payload(circle_id=circle_id)).status_code == 403


def test_same_token_cannot_be_assigned_to_another_circle(billing_api):
    client, _, _ = billing_api
    _, first_headers, first_circle_id = _pending_circle(client)
    first = client.post("/api/v2/billing/google/verify", headers=first_headers, json=_verify_payload("shared-token", first_circle_id))
    assert first.status_code == 200

    _, second_headers = _register(client, "FAMILY_MEMBER", "Second", "billing-second")
    circle = client.post("/api/v2/family-circles", headers=second_headers, json={"name": "Second family"})
    assert circle.status_code == 200
    second_circle_id = circle.json()["id"]
    response = client.post("/api/v2/billing/google/verify", headers=second_headers, json=_verify_payload("shared-token", second_circle_id))
    assert response.status_code == 409


def test_restore_recovers_existing_circle_and_rtdn_is_idempotent(billing_api):
    client, database, verifier = billing_api
    _, organizer_headers, circle_id = _pending_circle(client)
    token = "restore-token"
    assert client.post("/api/v2/billing/google/verify", headers=organizer_headers, json=_verify_payload(token, circle_id)).status_code == 200

    new_account, new_headers = _register(client, "FAMILY_MEMBER", "Organizer device", "billing-reinstall")
    restored = client.post("/api/v2/billing/google/restore", headers=new_headers, json=_verify_payload(token, 999999))
    assert restored.status_code == 200, restored.text
    assert restored.json()["family_circle_id"] == circle_id
    assert restored.json()["organizer_user_id"] == new_account["user_id"]

    verifier.purchase.purchase_state = "CANCELED"
    import v2_billing
    with database.SessionLocal() as db:
        assert v2_billing.process_rtdn(
            v2_billing.RtdnRequest(message_id="rtdn-1", notification_type="ONE_TIME_PRODUCT_CANCELED", purchase_token=token),
            db,
            verifier,
        ) == "processed"
        assert v2_billing.process_rtdn(
            v2_billing.RtdnRequest(message_id="rtdn-1", notification_type="ONE_TIME_PRODUCT_CANCELED", purchase_token=token),
            db,
            verifier,
        ) == "duplicate"
        from v2_models import FamilyCircle, PurchaseEntitlement
        assert db.get(FamilyCircle, circle_id).status == "SUSPENDED"
        assert db.query(PurchaseEntitlement).one().verification_state == "INVALID"
