"""Server-authoritative Google Play one-time entitlement processing."""

import hashlib
import os

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

import models
import v2_models
from billing_config import PACKAGE_NAME, PRODUCT_ID
from database import get_db
from google_play_verifier import GooglePlayVerifier, configured_google_play_verifier
from purchase_token_crypto import encrypt_purchase_token
from v2_auth import get_v2_current_user
from v2_billing_schemas import EntitlementResponse, RtdnRequest, VerifyPurchaseRequest
from v2_routes import _active_membership, _circle_any
from v2_time import utc_now


router = APIRouter(prefix="/api/v2", tags=["parent-check-in-billing"])


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _state(value: str | None) -> str:
    normalized = (value or "UNSPECIFIED").upper()
    return normalized.rsplit("_", 1)[-1]


def _entitlement_response(entitlement: v2_models.PurchaseEntitlement) -> dict:
    return {
        "family_circle_id": entitlement.family_circle_id,
        "organizer_user_id": entitlement.organizer_user_id,
        "product_id": entitlement.product_id,
        "purchase_state": entitlement.purchase_state,
        "verification_state": entitlement.verification_state,
        "acknowledgement_state": entitlement.acknowledgement_state,
        "updated_at": entitlement.updated_at,
    }


def _verify_and_apply(payload: VerifyPurchaseRequest, current_user: models.User, db: Session, verifier: GooglePlayVerifier) -> dict:
    if payload.product_id != PRODUCT_ID:
        raise HTTPException(status_code=400, detail="unexpected product")
    token_hash = _token_hash(payload.purchase_token)
    existing = db.query(v2_models.PurchaseEntitlement).filter(v2_models.PurchaseEntitlement.purchase_token_hash == token_hash).first()
    if payload.family_circle_id is not None:
        circle = _circle_any(db, payload.family_circle_id)
        membership = _active_membership(db, current_user.id, circle.id, ("ORGANIZER",))
        del membership
        if existing is not None and existing.family_circle_id != circle.id:
            raise HTTPException(status_code=409, detail="purchase is already assigned to another family circle")
        if existing is None and circle.status != "PENDING_PURCHASE":
            raise HTTPException(status_code=409, detail="family circle is not awaiting purchase")
    elif existing is None:
        raise HTTPException(status_code=404, detail="purchase is not associated with a family circle")

    try:
        purchase = verifier.get_purchase(payload.purchase_token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Google Play verification unavailable") from exc
    if purchase.package_name != PACKAGE_NAME or purchase.product_id != PRODUCT_ID:
        raise HTTPException(status_code=400, detail="purchase does not match this application")
    if purchase.obfuscated_account_id and payload.obfuscated_account_id and purchase.obfuscated_account_id != payload.obfuscated_account_id:
        raise HTTPException(status_code=403, detail="purchase attribution mismatch")
    if existing is not None and existing.obfuscated_account_hash and purchase.obfuscated_account_id and _token_hash(purchase.obfuscated_account_id) != existing.obfuscated_account_hash:
        raise HTTPException(status_code=403, detail="purchase attribution mismatch")
    purchase_state = _state(purchase.purchase_state)
    if purchase_state not in {"PENDING", "PURCHASED", "CANCELED", "CANCELLED"}:
        purchase_state = "INVALID"
    if existing is None:
        try:
            encrypted_token = encrypt_purchase_token(payload.purchase_token)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="purchase storage is not configured") from exc
        circle = _circle_any(db, payload.family_circle_id)
        existing = v2_models.PurchaseEntitlement(
            family_circle_id=circle.id,
            organizer_user_id=current_user.id,
            product_id=PRODUCT_ID,
            package_name=PACKAGE_NAME,
            purchase_token_hash=token_hash,
            purchase_token_ciphertext=encrypted_token,
            purchase_state=purchase_state,
            verification_state="PENDING",
            acknowledgement_state="ACKNOWLEDGED" if _state(purchase.acknowledgement_state) == "ACKNOWLEDGED" else "PENDING",
            obfuscated_account_hash=_token_hash(purchase.obfuscated_account_id) if purchase.obfuscated_account_id else None,
            purchased_at=purchase.purchased_at,
            last_verified_at=utc_now(),
        )
        db.add(existing)
    else:
        if existing.purchase_token_ciphertext is None:
            try:
                existing.purchase_token_ciphertext = encrypt_purchase_token(payload.purchase_token)
            except RuntimeError as exc:
                raise HTTPException(status_code=503, detail="purchase storage is not configured") from exc
        existing.purchase_state = purchase_state
        if _state(purchase.acknowledgement_state) == "ACKNOWLEDGED":
            existing.acknowledgement_state = "ACKNOWLEDGED"
        existing.last_verified_at = utc_now()
        existing.purchased_at = existing.purchased_at or purchase.purchased_at
        if payload.family_circle_id is None and existing.organizer_user_id != current_user.id:
            # Reinstall/second-device recovery is only possible after the
            # server has re-verified the already-owned Play token.
            existing.organizer_user_id = current_user.id
            circle = _circle_any(db, existing.family_circle_id)
            circle.organizer_user_id = current_user.id
            if db.query(v2_models.FamilyMembership).filter(v2_models.FamilyMembership.family_circle_id == circle.id, v2_models.FamilyMembership.user_id == current_user.id).first() is None:
                db.add(v2_models.FamilyMembership(family_circle_id=circle.id, user_id=current_user.id, role="ORGANIZER", relationship="organizer", membership_status="active"))

    circle = _circle_any(db, existing.family_circle_id)
    if purchase_state == "PURCHASED":
        existing.verification_state = "VERIFIED"
        existing.verified_at = existing.verified_at or utc_now()
        existing.revoked_at = None
        circle.status = "ACTIVE"
    elif purchase_state in {"CANCELED", "CANCELLED", "INVALID"}:
        existing.verification_state = "INVALID"
        existing.revoked_at = utc_now()
        circle.status = "SUSPENDED"
    else:
        existing.verification_state = "PENDING"

    db.commit()
    if purchase_state == "PURCHASED" and existing.acknowledgement_state != "ACKNOWLEDGED":
        try:
            if verifier.acknowledge(PRODUCT_ID, payload.purchase_token):
                existing.acknowledgement_state = "ACKNOWLEDGED"
                db.commit()
        except Exception:
            existing.acknowledgement_state = "FAILED"
            db.commit()
    db.refresh(existing)
    return _entitlement_response(existing)


@router.post("/billing/google/verify", response_model=EntitlementResponse)
def verify_purchase(payload: VerifyPurchaseRequest, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db), verifier: GooglePlayVerifier = Depends(configured_google_play_verifier)):
    return _verify_and_apply(payload, current_user, db, verifier)


@router.post("/billing/google/restore", response_model=EntitlementResponse)
def restore_purchase(payload: VerifyPurchaseRequest, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db), verifier: GooglePlayVerifier = Depends(configured_google_play_verifier)):
    restore_payload = payload.model_copy(update={"family_circle_id": None})
    return _verify_and_apply(restore_payload, current_user, db, verifier)


@router.get("/family-circles/{circle_id}/entitlement", response_model=EntitlementResponse | None)
def get_entitlement(circle_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    _circle_any(db, circle_id)
    _active_membership(db, current_user.id, circle_id)
    entitlement = db.query(v2_models.PurchaseEntitlement).filter(v2_models.PurchaseEntitlement.family_circle_id == circle_id).first()
    return _entitlement_response(entitlement) if entitlement else None


def process_rtdn(payload: RtdnRequest, db: Session, verifier: GooglePlayVerifier) -> str:
    token_hash = _token_hash(payload.purchase_token)
    if db.query(v2_models.BillingRtdnEvent).filter(v2_models.BillingRtdnEvent.message_id == payload.message_id).first() is not None:
        return "duplicate"
    event = v2_models.BillingRtdnEvent(message_id=payload.message_id, notification_type=payload.notification_type, purchase_token_hash=token_hash, status="received")
    db.add(event)
    db.flush()
    entitlement = db.query(v2_models.PurchaseEntitlement).filter(v2_models.PurchaseEntitlement.purchase_token_hash == token_hash).first()
    if entitlement is None:
        event.status = "unassigned"
        event.processed_at = utc_now()
        db.commit()
        return "unassigned"
    purchase = verifier.get_purchase(payload.purchase_token)
    if purchase.package_name != PACKAGE_NAME or purchase.product_id != PRODUCT_ID:
        event.status = "invalid"
        event.processed_at = utc_now()
        db.commit()
        return "invalid"
    state = _state(purchase.purchase_state)
    circle = _circle_any(db, entitlement.family_circle_id)
    if state == "PURCHASED":
        entitlement.purchase_state = "PURCHASED"
        entitlement.verification_state = "VERIFIED"
        entitlement.revoked_at = None
        circle.status = "ACTIVE"
    else:
        entitlement.purchase_state = "CANCELED" if state in {"CANCELED", "CANCELLED"} else "INVALID"
        entitlement.verification_state = "INVALID"
        entitlement.revoked_at = utc_now()
        circle.status = "SUSPENDED"
    entitlement.last_verified_at = utc_now()
    event.status = "processed"
    event.processed_at = utc_now()
    db.commit()
    if state == "PURCHASED" and entitlement.acknowledgement_state != "ACKNOWLEDGED":
        try:
            if verifier.acknowledge(PRODUCT_ID, payload.purchase_token):
                entitlement.acknowledgement_state = "ACKNOWLEDGED"
                db.commit()
        except Exception:
            entitlement.acknowledgement_state = "FAILED"
            db.commit()
    return "processed"


@router.post("/internal/google-play/rtdn", include_in_schema=False)
def receive_rtdn(payload: RtdnRequest, x_rtdn_secret: str | None = Header(default=None), db: Session = Depends(get_db), verifier: GooglePlayVerifier = Depends(configured_google_play_verifier)):
    expected = os.getenv("GOOGLE_PLAY_RTDN_SECRET", "").strip()
    if not expected or x_rtdn_secret != expected:
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        return {"status": process_rtdn(payload, db, verifier)}
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail="purchase lifecycle verification unavailable") from exc
