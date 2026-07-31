# HarmonyOS Push Backend Contract

This document remains the Phase 1 client handoff for push-token registration.
Outbound server configuration and delivery behavior are documented separately
in `HARMONYOS_PUSH_SERVER_SETUP.md`.

## 1. Push-token registration

### Endpoint

```http
POST /api/user/update-fcm-token
Authorization: Bearer <api_token>
Content-Type: application/json
```

Despite the legacy route name, this is the provider-neutral registration
endpoint used by HarmonyOS. HarmonyOS tokens submitted here are not treated as
FCM tokens.

### HarmonyOS request

```json
{
  "device_id": "<device_id previously sent to POST /api/register>",
  "platform": "harmonyos",
  "push_provider": "harmonyos",
  "push_token": "<token returned by pushService.getToken()>",
  "app_version": "2.1.0"
}
```

Required fields:

- `device_id`
- `platform`
- `push_provider`
- `push_token`

Optional fields:

- `app_version`

The HarmonyOS client must send the canonical combination:

```text
platform = harmonyos
push_provider = harmonyos
```

The backend supports only these platform/provider combinations:

- `android_google` + `fcm`
- `android_huawei` + `huawei_android`
- `harmonyos` + `harmonyos`

Values are normalized to lowercase with hyphens and spaces converted to
underscores before the combination is checked. The client should still send the
canonical values shown above.

### Successful response

```json
{
  "success": true,
  "registered": true,
  "platform": "harmonyos",
  "push_provider": "harmonyos",
  "token_updated_at": "2026-07-30T18:42:10.123456",
  "token_changed": true
}
```

`token_updated_at` is a UTC timestamp. The response never contains
`push_token`, `fcm_token`, API credentials, or information about another
device.

### Validation

- A valid registered-device bearer credential is required.
- `device_id` must identify the same user/device record as the bearer
  credential.
- `device_id` is trimmed and must be between 8 and 255 characters.
- `platform` and `push_provider` are trimmed and must each be between 1 and 32
  characters.
- `push_token` is trimmed, must not be empty, and must be between 1 and 4096
  characters.
- `app_version`, when supplied, is trimmed and must be between 1 and 64
  characters.
- HarmonyOS tokens are opaque. No Firebase/FCM token-format validation is
  applied.
- Unsupported or mismatched platform/provider combinations are rejected.

### Idempotency and replacement

If the same active token is uploaded again for the same device and provider:

- The request succeeds.
- `token_changed` is `false`.
- No duplicate row is created.
- `token_updated_at` remains unchanged.

If the token differs from the stored token for that device/provider:

- The existing row is updated rather than duplicated.
- `token_changed` is `true`.
- `push_token_updated_at` is updated.
- `push_token_invalidated_at` is cleared.

If the same token is present but marked invalidated:

- The upload succeeds.
- `token_changed` is `false`, because the token string did not change.
- `push_token_invalidated_at` is cleared.
- `push_token_updated_at` is refreshed.

The same HarmonyOS provider token cannot be assigned to two devices. The second
device receives `409 Conflict`.

## 2. Error responses

Handled `400` through `422` error bodies use FastAPI's `detail` field. An
unexpected `500` may use the server's generic error representation. Clients
should make decisions from the HTTP status, not by matching English error text.

### 400 Bad Request

Occurs when:

- The `platform`/`push_provider` combination is unsupported or mismatched, such
  as `harmonyos` + `fcm`.

Client interpretation:

- The payload is not valid for the selected push system.

Retry:

- Do not retry unchanged. Correct the platform/provider values first.

### 401 Unauthorized

Occurs when:

- The `Authorization` header is missing.
- The header is not a valid bearer credential.
- The API credential is unknown, stale, revoked, or belongs to a deleted
  registration.

Client interpretation:

- Device authentication is unavailable; this is not a push-token-format error.

Retry:

- Do not repeatedly retry with the same rejected credential. Preserve the
  pending push token, restore valid device authentication through the existing
  registration/authentication flow, and then retry.

### 403 Forbidden

Occurs when:

- A legacy `user_id`, if supplied, does not belong to the authenticated user.
- `device_id` exists but belongs to a different authenticated user/device.

Client interpretation:

- The credential is valid, but it is not authorized to update that device.

Retry:

- Do not retry unchanged. Reconcile the locally stored device registration and
  credential first.

### 404 Not Found

Occurs when:

- The supplied `device_id` is not registered.

Client interpretation:

- Backend device registration is missing or the client is using the wrong
  `device_id`.

Retry:

- Do not retry unchanged. Complete or repair `POST /api/register` first, then
  retry token registration.

### 409 Conflict

Occurs when:

- The same provider token is already assigned to another device.
- A concurrent database registration conflicts with the uniqueness rules.

Client interpretation:

- The backend refused to move or duplicate a token across device records.

Retry:

- Do not use an immediate infinite retry. Re-read/re-obtain the device's current
  HarmonyOS token and retry only after local or backend ownership state has
  changed. A transient concurrent conflict may be retried with bounded backoff.

### 422 Unprocessable Entity

Occurs when:

- A required field is missing.
- `push_token` is empty or longer than 4096 characters.
- `device_id`, `platform`, `push_provider`, or `app_version` violates its length
  constraint.
- A field has the wrong JSON type.
- Both `push_token` and the legacy `fcm_token` are supplied with different
  values.

Client interpretation:

- The request does not match the endpoint schema.

Retry:

- Do not retry unchanged. Correct the request locally first.

### 500 Internal Server Error

Occurs when:

- An unexpected, unhandled backend or database failure prevents registration.

Client interpretation:

- Authentication and token data may still be valid, but synchronization was
  not confirmed.
- Do not depend on a particular response body for an unexpected `500`.

Retry:

- Keep the token pending and retry later with bounded exponential backoff.
  Do not mark it synchronized, and do not create a tight or infinite retry
  loop.

## 3. Authentication sequence

1. Register the device first:

   ```http
   POST /api/register
   Content-Type: application/json
   ```

   using the device's normal registration payload, including its stable
   `device_id`.

2. Store the `api_token` returned by `POST /api/register` in secure device
   storage. The push-token endpoint uses this credential:

   ```http
   Authorization: Bearer <api_token>
   ```

3. After both the backend device registration and a HarmonyOS token from
   `pushService.getToken()` exist, upload the push token using
   `POST /api/user/update-fcm-token`.

4. The `device_id` in the push-token request must match the device record owned
   by the bearer credential.

5. If the credential is stale or invalid, the backend returns `401`. Preserve
   the pending HarmonyOS token. Do not treat it as synchronized and do not keep
   retrying the rejected credential. Restore valid device authentication, then
   upload the pending token again.

Calling `POST /api/register` for an already registered device also requires
that device's existing bearer credential. It is not an unauthenticated token
recovery endpoint.

## 4. Persistence behavior

HarmonyOS registrations are stored in the `device_push_tokens` table using:

- `user_id`
- `platform`
- `push_provider`
- `push_token`
- `push_token_updated_at`
- `push_token_invalidated_at`

The row is unique by `(user_id, push_provider)`. The combination
`(push_provider, push_token)` is also unique, preventing cross-device reuse
within a provider.

HarmonyOS tokens are never written to `users.fcm_token`. That legacy field is
reserved for existing Android Google FCM delivery. When a HarmonyOS
registration is processed, any legacy `users.fcm_token` value on that user is
cleared so the device cannot be routed through FCM.

`token_changed` is `true` when no row exists yet or when the uploaded token
string differs from the stored token. Reactivating the same invalidated token
returns `token_changed: false` while clearing its invalidated timestamp and
refreshing its update timestamp.

The persistence model permits one device/user record to have one row per push
provider. Registering a provider does not delete rows for other providers.
However, `device_status.platform` records the current platform, and HarmonyOS
does not fall back to another provider for delivery.

## 5. Compatibility

- Existing Android Google clients may continue using the legacy
  `user_id`/`platform`/`fcm_token` payload. Their token remains mirrored to
  `users.fcm_token`, and existing FCM delivery behavior is unchanged.
- Existing Android Huawei clients may continue using their legacy payload.
  Their provider is resolved as `huawei_android`; outbound Huawei Android Push
  remains unimplemented/skipped exactly as before.
- HarmonyOS tokens are stored under provider `harmonyos`, never in
  `users.fcm_token`, and never fall back to FCM.
- HarmonyOS outbound delivery uses its dedicated Push Kit V3 provider; it does
  not alter this token-registration contract.

## 6. HarmonyOS client synchronization flow

The expected client flow is:

1. Call `pushService.getToken()` and obtain the HarmonyOS token.
2. Persist the token locally as pending before attempting network
   synchronization.
3. Wait until `POST /api/register` has completed and a valid `api_token` and
   matching `device_id` are available.
4. Upload the pending token to `POST /api/user/update-fcm-token`.
5. Mark the local token synchronized only after a successful HTTP 2xx response.
6. On network failure, `401`, `403`, `404`, `409`, `422`, or `5xx`, preserve
   pending state until the specific cause is resolved.
7. Retry transient network and server failures later using bounded exponential
   backoff with a maximum attempt interval. Avoid immediate recursion, tight
   loops, or unbounded retries.
8. A repeated upload is safe. The backend treats the same token on the same
   device/provider idempotently.
9. If HarmonyOS returns a different token later, replace the locally pending
   value and upload it. A successful response with `token_changed: true`
   confirms replacement on the backend.

## 7. Security behavior

- Full push tokens are never included in endpoint responses.
- Registration logs do not contain the full token.
- Logs include the database user ID, device-status record ID, platform,
  provider, `token_changed`, token length, and a masked suffix.
- For tokens longer than four characters, the logged suffix has the form
  `***1234`. Tokens of four characters or fewer are logged only as `***`.
- The bearer credential is resolved to a registered user/device before any
  token write.
- Supplied `device_id` ownership is checked against that authenticated record.
- Platform/provider combinations are checked before persistence.
- Cross-device reuse of the same provider token is rejected rather than
  silently transferring ownership.

## 8. Backend tests

The backend test suite covers:

- Successful HarmonyOS registration and typed response.
- Same-token idempotency and stable update timestamp.
- Changed-token replacement.
- Empty-token rejection.
- Unsupported platform/provider combinations.
- Verification that HarmonyOS tokens are absent from `users.fcm_token`.
- Prevention of one device updating another device.
- Existing Android Google FCM registration compatibility.
- Existing Android Huawei registration compatibility.
- Absence of full tokens from registration logs and responses.
- Registration while `PUSH_DELIVERY_ENABLED=false`.
- Reactivation of an invalidated token.
- Safe rejection of provider-token reuse across devices.
- Migration creation without changing an existing user's FCM token.

Latest full backend result:

```text
34 passed
```

## Backend source references

- Endpoint function:
  `main.py` — `update_fcm_token`
- Platform/provider validation:
  `main.py` — `SUPPORTED_PUSH_COMBINATIONS`,
  `DEFAULT_PROVIDER_BY_PLATFORM`, and `_resolve_push_platform_provider`
- Registered-device authentication:
  `main.py` — `get_current_user`, `_user_from_credentials`
- Request schema:
  `schemas.py` — `UpdateFcmTokenRequest`
- Response schema:
  `schemas.py` — `PushTokenRegistrationResponse`
- Persistence model:
  `models.py` — `DevicePushToken`
- SQLite migration:
  `main.py` — `_ensure_sqlite_migrations`
- Tests:
  `tests/test_api_security.py`
