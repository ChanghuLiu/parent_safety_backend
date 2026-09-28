"""Server-authoritative Google Play one-time entitlement processing."""

import hashlib
import os
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
import v2_models
from billing_config import PACKAGE_NAME, PRODUCT_ID
from database import get_db
from google_play_verifier import GooglePlayVerifier, configured_google_play_verifier
from purchase_token_crypto import encrypt_purchase_token
from v2_auth import get_v2_current_user
from v2_billing_schemas import (
    EntitlementResponse,
    OrganizerPurchaseRecoveryRequest,
    OrganizerPurchaseRecoveryResponse,
    RtdnRequest,
    VerifyPurchaseRequest,
)
from v2_routes import _active_membership, _circle_any
from v2_time import utc_now

if os.getenv("APP_ENV", "production").strip().lower() not in {"production", "prod"}:
    from v2_test_fixtures import test_entitlement_for_circle
else:
    test_entitlement_for_circle = None


router = APIRouter(prefix="/api/v2", tags=["parent-check-in-billing"])

PURCHASE_BELONGS_TO_ANOTHER_ACCOUNT = "PURCHASE_BELONGS_TO_ANOTHER_ACCOUNT"
PURCHASE_RECOVERY_MAX_ATTEMPTS = 5
PURCHASE_RECOVERY_WINDOW_MINUTES = 15


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _purchase_recovery_attempt_or_429(db: Session, device_hash: str, token_hash: str) -> models.OrganizerPurchaseRecoveryAttempt:
    window_start = utc_now() - timedelta(minutes=PURCHASE_RECOVERY_WINDOW_MINUTES)
    recent = db.query(models.OrganizerPurchaseRecoveryAttempt).filter(
        models.OrganizerPurchaseRecoveryAttempt.device_fingerprint == device_hash,
        models.OrganizerPurchaseRecoveryAttempt.attempted_at >= window_start,
    ).count()
    if recent >= PURCHASE_RECOVERY_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail="too many purchase recovery attempts; try again later",
            headers={"Retry-After": str(PURCHASE_RECOVERY_WINDOW_MINUTES * 60)},
        )
    attempt = models.OrganizerPurchaseRecoveryAttempt(
        device_fingerprint=device_hash,
        purchase_token_hash=token_hash,
        attempted_at=utc_now(),
    )
    db.add(attempt)
    db.flush()
    return attempt


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


def _can_rebind_deleted_account_entitlement(
    db: Session,
    entitlement: v2_models.PurchaseEntitlement,
) -> bool:
    """Return True only when the previous organizer account was irreversibly deleted.

    This is deliberately stricter than a normal revocation/cancellation.  It lets a
    still-owned Play purchase be restored to a newly created caregiver account after
    the user explicitly deleted the old Parent Check-In account, without making active
    or suspended cross-account purchases transferable.
    """
    if entitlement.revoked_at is None:
        return False
    old_circle = db.get(v2_models.FamilyCircle, entitlement.family_circle_id)
    old_user = db.get(models.User, entitlement.organizer_user_id)
    if old_circle is None or old_user is None:
        return False
    return (
        old_circle.status == "deleted"
        and old_user.api_token_hash is None
        and old_user.organizer_recovery_verifier is None
        and old_user.recovery_device_id is None
        and old_user.device_id == f"deleted-{old_user.id}"
    )


def _restore_circle_for_user(db: Session, current_user: models.User) -> v2_models.FamilyCircle:
    """Resolve the only server-authorized caregiver target for a restore.

    Restore deliberately does not accept a client-supplied circle as proof of
    ownership.  A caregiver with no circle receives the same pending circle
    that the normal Connect Parent flow expects, after Google verification has
    succeeded.
    """
    if current_user.role != "family_member":
        raise HTTPException(status_code=403, detail="only a caregiver can restore a family purchase")
    memberships = db.query(v2_models.FamilyMembership).filter(
        v2_models.FamilyMembership.user_id == current_user.id,
        v2_models.FamilyMembership.role == "ORGANIZER",
        v2_models.FamilyMembership.membership_status == "active",
    ).all()
    if len(memberships) > 1:
        raise HTTPException(status_code=409, detail="multiple family circles require explicit selection")
    if memberships:
        return _circle_any(db, memberships[0].family_circle_id)
    circle = v2_models.FamilyCircle(organizer_user_id=current_user.id, status="PENDING_PURCHASE")
    db.add(circle)
    db.flush()
    db.add(v2_models.FamilyMembership(
        family_circle_id=circle.id,
        user_id=current_user.id,
        role="ORGANIZER",
        relationship="organizer",
        membership_status="active",
    ))
    db.flush()
    return circle


def _audit_restore(
    db: Session,
    current_user: models.User,
    entitlement: v2_models.PurchaseEntitlement,
    token_hash: str,
    result: str,
) -> None:
    db.add(v2_models.BillingAuditEvent(
        event_type="google_purchase_restore",
        source="verified_orphan_recovery",
        user_id=current_user.id,
        entitlement_id=entitlement.id,
        family_circle_id=entitlement.family_circle_id,
        product_id=PRODUCT_ID,
        purchase_token_hash=token_hash,
        result=result,
    ))


def _verify_and_apply(
    payload: VerifyPurchaseRequest,
    current_user: models.User,
    db: Session,
    verifier: GooglePlayVerifier,
    *,
    restore: bool = False,
) -> dict:
    # Google verification is intentionally the first ownership-sensitive
    # operation.  An unknown token must never be rejected locally before the
    # Play Developer API has established what was purchased.
    try:
        purchase = verifier.get_purchase(payload.purchase_token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Google Play verification unavailable") from exc
    if purchase.package_name != PACKAGE_NAME or purchase.product_id != PRODUCT_ID:
        raise HTTPException(status_code=400, detail="purchase does not match this application")
    if payload.product_id != PRODUCT_ID:
        raise HTTPException(status_code=400, detail="unexpected product")
    if purchase.obfuscated_account_id and payload.obfuscated_account_id and purchase.obfuscated_account_id != payload.obfuscated_account_id:
        raise HTTPException(status_code=403, detail="purchase attribution mismatch")
    purchase_state = _state(purchase.purchase_state)
    if purchase_state not in {"PENDING", "PURCHASED", "CANCELED", "CANCELLED"}:
        purchase_state = "INVALID"
    if restore and purchase_state != "PURCHASED":
        raise HTTPException(status_code=400, detail="purchase is not currently purchased")

    token_hash = _token_hash(payload.purchase_token)
    existing = db.query(v2_models.PurchaseEntitlement).filter(
        v2_models.PurchaseEntitlement.purchase_token_hash == token_hash
    ).with_for_update().first()
    if restore and current_user.role != "family_member":
        raise HTTPException(status_code=403, detail="only a caregiver can restore a family purchase")
    rebind_deleted_account = False
    if existing is not None and existing.organizer_user_id != current_user.id:
        # A normal active/suspended binding is never transferable.  The sole
        # exception is an explicitly deleted organizer account: Google still owns
        # the lifetime purchase, so Restore purchase must be able to bind it to the
        # newly created caregiver account instead of stranding the purchase forever.
        if restore and _can_rebind_deleted_account_entitlement(db, existing):
            rebind_deleted_account = True
        else:
            raise HTTPException(status_code=409, detail=PURCHASE_BELONGS_TO_ANOTHER_ACCOUNT)
    if existing is not None and existing.obfuscated_account_hash and purchase.obfuscated_account_id and _token_hash(purchase.obfuscated_account_id) != existing.obfuscated_account_hash:
        raise HTTPException(status_code=403, detail="purchase attribution mismatch")

    circle = None
    if rebind_deleted_account:
        circle = _restore_circle_for_user(db, current_user)
        existing.family_circle_id = circle.id
        existing.organizer_user_id = current_user.id
        existing.source = "deleted_account_rebind"
        existing.obfuscated_account_hash = _token_hash(purchase.obfuscated_account_id) if purchase.obfuscated_account_id else None
    if payload.family_circle_id is not None and not restore:
        circle = circle or _circle_any(db, payload.family_circle_id)
        _active_membership(db, current_user.id, circle.id, ("ORGANIZER",))
        if existing is not None and existing.family_circle_id != circle.id:
            raise HTTPException(status_code=409, detail="purchase is already assigned to another family circle")
        if existing is None and circle.status != "PENDING_PURCHASE":
            raise HTTPException(status_code=409, detail="family circle is not awaiting purchase")
    elif existing is None and not restore:
        raise HTTPException(status_code=404, detail="purchase is not associated with a family circle")
    elif existing is None and restore:
        circle = _restore_circle_for_user(db, current_user)

    restore_audit_result = "rebound_after_account_deletion" if rebind_deleted_account else None
    if existing is None:
        try:
            encrypted_token = encrypt_purchase_token(payload.purchase_token)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="purchase storage is not configured") from exc
        circle = circle or _circle_any(db, payload.family_circle_id)
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
            source="verified_orphan_recovery" if restore else "google_verify",
            obfuscated_account_hash=_token_hash(purchase.obfuscated_account_id) if purchase.obfuscated_account_id else None,
            purchased_at=purchase.purchased_at,
            last_verified_at=utc_now(),
        )
        db.add(existing)
        restore_audit_result = "bound" if restore else None
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
        if restore and not getattr(existing, "source", None):
            existing.source = "verified_orphan_recovery"
        if restore and restore_audit_result is None:
            restore_audit_result = "idempotent"
    circle = circle or _circle_any(db, existing.family_circle_id)
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

    try:
        db.flush()
        if restore and restore_audit_result is not None:
            _audit_restore(db, current_user, existing, token_hash, restore_audit_result)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # The unique token constraint is the final arbiter for two caregivers
        # racing to claim an orphan.  A retry by the same owner is idempotent;
        # a different owner remains rejected and cannot receive a second row.
        claimed = db.query(v2_models.PurchaseEntitlement).filter(
            v2_models.PurchaseEntitlement.purchase_token_hash == token_hash
        ).first()
        if claimed is not None and claimed.organizer_user_id == current_user.id:
            return _entitlement_response(claimed)
        if claimed is not None:
            raise HTTPException(status_code=409, detail=PURCHASE_BELONGS_TO_ANOTHER_ACCOUNT) from exc
        raise HTTPException(status_code=409, detail="purchase restore could not be claimed") from exc
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
    return _verify_and_apply(restore_payload, current_user, db, verifier, restore=True)


@router.post("/organizer/recover-with-purchase", response_model=OrganizerPurchaseRecoveryResponse)
def recover_organizer_with_purchase(
    payload: OrganizerPurchaseRecoveryRequest,
    db: Session = Depends(get_db),
    verifier: GooglePlayVerifier = Depends(configured_google_play_verifier),
):
    """Recover the organizer already bound to a verified, still-owned Lifetime purchase.

    The purchase token is an ownership proof, never an organizer selector.  A
    successful request always resolves the existing entitlement's organizer
    and circle, and repeated recovery remains allowed while Google reports the
    same purchase as valid.
    """
    token_hash = _token_hash(payload.purchase_token)
    device_hash = _token_hash(payload.device_id)
    try:
        attempt = _purchase_recovery_attempt_or_429(db, device_hash, token_hash)
        db.commit()
        purchase = verifier.get_purchase(payload.purchase_token)
        if purchase.package_name != PACKAGE_NAME or purchase.product_id != PRODUCT_ID:
            raise HTTPException(status_code=400, detail="purchase does not match this application")
        if _state(purchase.purchase_state) != "PURCHASED":
            raise HTTPException(status_code=400, detail="purchase is not currently purchased")
        entitlement = db.query(v2_models.PurchaseEntitlement).filter(
            v2_models.PurchaseEntitlement.purchase_token_hash == token_hash,
        ).with_for_update().first()
        if entitlement is None:
            raise HTTPException(status_code=404, detail="purchase is not associated with an organizer")
        if (
            entitlement.package_name != PACKAGE_NAME
            or entitlement.product_id != PRODUCT_ID
            or entitlement.verification_state != "VERIFIED"
            or entitlement.purchase_state != "PURCHASED"
            or entitlement.revoked_at is not None
        ):
            raise HTTPException(status_code=400, detail="purchase entitlement is not currently valid")
        circle = _circle_any(db, entitlement.family_circle_id)
        organizer = db.get(models.User, entitlement.organizer_user_id)
        if organizer is None or organizer.role != "family_member" or circle.organizer_user_id != organizer.id:
            raise HTTPException(status_code=409, detail="purchase organizer binding is invalid")
        membership = db.query(v2_models.FamilyMembership).filter(
            v2_models.FamilyMembership.family_circle_id == circle.id,
            v2_models.FamilyMembership.user_id == organizer.id,
            v2_models.FamilyMembership.role == "ORGANIZER",
            v2_models.FamilyMembership.membership_status == "active",
        ).first()
        if membership is None:
            raise HTTPException(status_code=409, detail="purchase organizer membership is unavailable")
        raw_token = secrets.token_urlsafe(32)
        recovery_code = secrets.token_urlsafe(32)
        organizer.device_id = payload.device_id
        organizer.recovery_device_id = payload.device_id
        organizer.api_token_hash = _token_hash(raw_token)
        organizer.organizer_recovery_verifier = _token_hash(recovery_code)
        organizer.organizer_recovery_created_at = utc_now()
        organizer.organizer_recovery_used_at = None
        organizer.organizer_recovery_failed_attempts = 0
        organizer.organizer_recovery_locked_until = None
        organizer.organizer_recovery_one_time = False
        attempt.user_id = organizer.id
        attempt.success = True
        db.add(models.OrganizerRecoveryAuditEvent(
            user_id=organizer.id,
            action="purchase_recovered",
            source="google_purchase_recovery",
        ))
        db.commit()
        return {
            "user_id": organizer.id,
            "role": "FAMILY_MEMBER",
            "api_token": raw_token,
            "locale_tag": organizer.locale_tag or "en",
            "recovery_code": recovery_code,
        }
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail="purchase recovery verification unavailable") from exc


@router.get("/family-circles/{circle_id}/entitlement", response_model=EntitlementResponse | None)
def get_entitlement(circle_id: int, current_user: models.User = Depends(get_v2_current_user), db: Session = Depends(get_db)):
    _circle_any(db, circle_id)
    _active_membership(db, current_user.id, circle_id)
    entitlement = db.query(v2_models.PurchaseEntitlement).filter(v2_models.PurchaseEntitlement.family_circle_id == circle_id).first()
    if entitlement is not None:
        return _entitlement_response(entitlement)
    if test_entitlement_for_circle is not None:
        fixture = test_entitlement_for_circle(db, circle_id, current_user.id)
        if fixture is not None:
            return {
                "family_circle_id": fixture.family_circle_id,
                "organizer_user_id": fixture.organizer_user_id,
                "product_id": fixture.product_id,
                "purchase_state": "TEST",
                "verification_state": "VERIFIED",
                "acknowledgement_state": "ACKNOWLEDGED",
                "updated_at": fixture.created_at,
            }
    return None


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
    if _can_rebind_deleted_account_entitlement(db, entitlement):
        entitlement.last_verified_at = utc_now()
        event.status = "ignored_deleted_account"
        event.processed_at = utc_now()
        db.commit()
        return "ignored_deleted_account"
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
