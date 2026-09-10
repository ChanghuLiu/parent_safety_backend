"""Google Play Developer API adapter plus a deterministic test seam."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os

import httpx

from billing_config import PACKAGE_NAME


@dataclass(frozen=True)
class GooglePurchase:
    package_name: str
    product_id: str
    purchase_state: str
    acknowledgement_state: str
    obfuscated_account_id: str | None
    purchased_at: datetime | None


class GooglePlayVerifier:
    def get_purchase(self, purchase_token: str) -> GooglePurchase:
        raise NotImplementedError

    def acknowledge(self, product_id: str, purchase_token: str) -> bool:
        raise NotImplementedError


class GooglePlayDeveloperApiVerifier(GooglePlayVerifier):
    SCOPE = "https://www.googleapis.com/auth/androidpublisher"
    BASE_URL = "https://androidpublisher.googleapis.com/androidpublisher/v3"

    def __init__(self, service_account_file: str | None = None, package_name: str = PACKAGE_NAME):
        self.service_account_file = service_account_file
        self.package_name = package_name

    def _credentials(self):
        if self.service_account_file:
            from google.oauth2 import service_account
            return service_account.Credentials.from_service_account_file(
                self.service_account_file,
                scopes=[self.SCOPE],
            )
        import google.auth
        source_credentials, _ = google.auth.default()
        target_principal = os.getenv("GOOGLE_PLAY_IMPERSONATE_SERVICE_ACCOUNT", "").strip()
        if target_principal:
            from google.auth import impersonated_credentials
            return impersonated_credentials.Credentials(
                source_credentials=source_credentials,
                target_principal=target_principal,
                target_scopes=[self.SCOPE],
                lifetime=3600,
            )
        return google.auth.default(scopes=[self.SCOPE])[0]

    def _access_token(self) -> str:
        from google.auth.transport.requests import Request
        credentials = self._credentials()
        credentials.refresh(Request())
        return credentials.token

    def auth_check(self) -> dict[str, str]:
        """Resolve credentials and refresh them without exposing the token."""
        token = self._access_token()
        del token
        if self.service_account_file:
            source = "service-account-file"
        elif os.getenv("GOOGLE_PLAY_IMPERSONATE_SERVICE_ACCOUNT", "").strip():
            source = "adc-service-account-impersonation"
        else:
            source = "application-default-credentials"
        return {"status": "PASS", "credential_source": source, "package": self.package_name}

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        token = self._access_token()
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "application/json"
        with httpx.Client(timeout=15.0) as client:
            response = client.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        return response

    def get_purchase(self, purchase_token: str) -> GooglePurchase:
        url = f"{self.BASE_URL}/applications/{self.package_name}/purchases/productsv2/tokens/{purchase_token}"
        payload = self._request("GET", url).json()
        line_items = payload.get("productLineItem") or []
        item = line_items[0] if line_items else {}
        purchase_time = item.get("purchaseCompletionTime") or payload.get("purchaseCompletionTime")
        purchased_at = None
        if purchase_time:
            purchased_at = datetime.fromisoformat(purchase_time.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
        return GooglePurchase(
            package_name=self.package_name,
            product_id=item.get("productId", ""),
            purchase_state=(item.get("purchaseStateContext") or payload.get("purchaseStateContext") or {}).get("purchaseState", "UNSPECIFIED"),
            acknowledgement_state=item.get("acknowledgementState", payload.get("acknowledgementState", "ACKNOWLEDGEMENT_STATE_UNSPECIFIED")),
            obfuscated_account_id=payload.get("obfuscatedExternalAccountId"),
            purchased_at=purchased_at,
        )

    def acknowledge(self, product_id: str, purchase_token: str) -> bool:
        url = f"{self.BASE_URL}/applications/{self.package_name}/purchases/products/{product_id}/tokens/{purchase_token}:acknowledge"
        self._request("POST", url, json={})
        return True


def configured_google_play_verifier() -> GooglePlayVerifier:
    service_account_file = os.getenv("GOOGLE_PLAY_SERVICE_ACCOUNT_FILE", "").strip()
    # GOOGLE_APPLICATION_CREDENTIALS, workload identity, and metadata-server
    # ADC are resolved by google.auth.default() without putting credentials in
    # this repository or in application settings.
    return GooglePlayDeveloperApiVerifier(service_account_file or None)
