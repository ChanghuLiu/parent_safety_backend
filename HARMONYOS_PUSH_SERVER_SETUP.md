# HarmonyOS Push Server Setup

This backend uses the HarmonyOS Push Kit V3 server API for outbound
notification delivery. It does not use the legacy Android HMS OAuth
client-ID/client-secret exchange.

## Official protocol used

The implementation follows these Huawei Push Kit documents:

- [Generate an authentication token from a service account](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/push-jwt-token)
- [Send a notification message](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/push-send-alert)
- [Scenario-based message REST API](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/push-scenariozed-api-intro)
- [Scenario-based request structure](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/push-scenariozed-api-request-struct)
- [Scenario-based response codes](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/push-scenariozed-api-response)

The server authentication and request are:

- JWT algorithm: `PS256` (RSA-PSS with SHA-256).
- JWT header: `kid`, `typ: JWT`, and `alg: PS256`.
- JWT claims: `iss`, `aud`, `iat`, and `exp`.
- `iss`: service-account file field `sub_account`.
- `aud`: `https://oauth-login.cloud.huawei.com/oauth2/v3/token`.
- JWT lifetime: 3600 seconds.
- REST endpoint:
  `POST https://push-api.cloud.huawei.com/v3/{project_id}/messages:send`.
- Headers: `Authorization: Bearer <JWT>`, `Content-Type: application/json`,
  and `push-type: 0`.

This is Huawei's production HTTPS endpoint for ordinary HarmonyOS
Next/5.x-or-later Alert notifications. It is not an Android HMS OAuth endpoint
and is not a test-only host. The URL path contains the AppGallery Connect
project ID, not the application APP ID. Huawei's documented test mode uses the
same endpoint with `pushOptions.testMessage=true` in the request body.

The exact request headers are:

```http
Authorization: Bearer <service-account JWT>
Content-Type: application/json
push-type: 0
```

No additional Huawei-specific request header is documented for Alert messages.

The provider caches a JWT and refreshes it five minutes before its documented
one-hour expiration. This five-minute refresh margin handles local scheduling
and request timing near expiration; the host clock still needs to be
synchronized because Huawei validates the documented `iat` and `exp` values.
Refresh is protected by an in-process lock.

The JWT itself does not contain or otherwise use a project ID. Push Kit V3
does use the AppGallery Connect **project ID** in the default REST URL, so the
backend requires a project ID only when it constructs that URL. A project ID
may come from the credential file or `HARMONYOS_PUSH_PROJECT_ID`.

The application APP ID is a separate identifier. This deployment validates
`HARMONYOS_PUSH_APP_ID=6917612346718148708` independently; it is never used as
or derived into a project ID. The V3 notification payload documented above
does not contain an `appId` field.

## Create the service-account credential

1. Open the Huawei Developer API Console.
2. Select the same project as the HarmonyOS application.
3. Create a service-account key credential for the Push service API.
4. Download the JSON key file once.
5. If the downloaded file contains optional `project_id` metadata, confirm it
   matches the application project shown under AppGallery Connect project
   settings. Current downloaded credentials may omit this field.
6. Store it outside the repository and restrict it to the backend service
   account:

   ```bash
   chmod 600 /protected/path/harmonyos-push-service-account.json
   ```

Downloaded credential schemas can vary. The fields used for JWT signing are:

```json
{
  "key_id": "<key ID>",
  "private_key": "<PKCS#8 PEM private key>",
  "sub_account": "<service-account ID>"
}
```

Only `key_id`, `private_key`, and `sub_account` are required. `project_id` is
accepted as optional metadata when present. The parser does not modify the
credential, invent a project ID, or treat the APP ID as a project ID. Never
commit this file. The repository ignores files matching
`*harmonyos-push-service-account*.json`.

## Environment variables

Enable both the global dispatcher and HarmonyOS provider:

```bash
export PUSH_DELIVERY_ENABLED=true
export HARMONYOS_PUSH_ENABLED=true
```

Recommended credential configuration:

```bash
export HARMONYOS_PUSH_SERVICE_ACCOUNT_FILE=/protected/path/harmonyos-push-service-account.json
export HARMONYOS_PUSH_APP_ID=6917612346718148708
```

The file must not be group- or world-accessible. When the credential has
optional project metadata, a mismatched `HARMONYOS_PUSH_PROJECT_ID` is
rejected. If it omits project metadata, configure `HARMONYOS_PUSH_PROJECT_ID`
separately before enabling real delivery so the backend can construct the
default V3 URL.

Alternatively, inject the three required credential fields directly through the
deployment secret manager:

```bash
export HARMONYOS_PUSH_SERVICE_ACCOUNT_ID='<sub_account>'
export HARMONYOS_PUSH_KEY_ID='<key_id>'
export HARMONYOS_PUSH_PRIVATE_KEY='<PKCS#8 PEM private key>'
```

Do not put these values in `.env` files committed to source control. The
private-key variable accepts either real PEM newlines or escaped `\n`
sequences.

Optional variables:

- `HARMONYOS_PUSH_PROJECT_ID`: AppGallery Connect project ID used in the
  default V3 API URL. Optional while parsing/signing credentials, but required
  for real delivery unless `HARMONYOS_PUSH_API_URL` is a complete URL without
  a `{project_id}` placeholder.
- `HARMONYOS_PUSH_API_URL`: HTTPS endpoint override. Leave unset to use the
  official V3 URL. `{project_id}` may be used as a placeholder.
- `HARMONYOS_PUSH_TEST_MESSAGE=true`: adds the official
  `pushOptions.testMessage` flag for controlled device testing.

## Safe dry-run sender

The repository includes `scripts/send_harmonyos_test_push.py`. Its default mode
constructs and serializes the notification through `HarmonyOSPushProvider`,
prints only a sanitized summary, and performs zero network calls. It does not
generate a JWT and does not require a Push token.

Run the current credential review and dry-run with:

```bash
HARMONYOS_PUSH_SERVICE_ACCOUNT_FILE=/etc/parent-safety/secrets/harmonyos-push-service-account.json \
HARMONYOS_PUSH_APP_ID=6917612346718148708 \
PUSH_DELIVERY_ENABLED=false \
HARMONYOS_PUSH_ENABLED=true \
python3 scripts/send_harmonyos_test_push.py
```

The summary may show the default path as
`/v3/{project_id}/messages:send` when the credential has no optional project
metadata. That is valid for dry-run inspection. Before any real send, configure
the actual AppGallery Connect project ID separately:

```bash
HARMONYOS_PUSH_PROJECT_ID='<project ID>'
```

Do not substitute `HARMONYOS_PUSH_APP_ID` for it.

Supported preview fields are `--title`, `--body`, `--event-type`,
`--target-page`, and `--json-output`. Unknown arguments are rejected. The
default test content contains no family identifiers, phone numbers, Android
intent fields, or FCM fields.

### Explicitly guarded real-send mode

A future real send requires all of:

1. `--send`
2. `PUSH_DELIVERY_ENABLED=true`
3. `HARMONYOS_PUSH_ENABLED=true`
4. A non-empty token from `--token` or `--token-file`
5. Interactive confirmation, unless `--yes` is also explicitly supplied
6. Valid service-account credentials, APP ID, and a resolvable API URL

Prefer a protected token file so the token does not enter shell history:

```bash
chmod 600 /protected/path/harmonyos-test-push-token

HARMONYOS_PUSH_SERVICE_ACCOUNT_FILE=/etc/parent-safety/secrets/harmonyos-push-service-account.json \
HARMONYOS_PUSH_APP_ID=6917612346718148708 \
HARMONYOS_PUSH_PROJECT_ID='<project ID>' \
PUSH_DELIVERY_ENABLED=true \
HARMONYOS_PUSH_ENABLED=true \
HARMONYOS_PUSH_TEST_MESSAGE=true \
python3 scripts/send_harmonyos_test_push.py \
  --send \
  --token-file /protected/path/harmonyos-test-push-token
```

The token file must be a regular, non-symlink file with no group/world access.
The utility never prints a full token, JWT, key ID, private key, bearer value,
credential document, or raw provider response. `--yes` bypasses only the
confirmation prompt; it does not bypass any other safeguard.

Normalized results are `request accepted`, `invalid token`,
`authentication failure`, `provider rejection`, `rate limited`,
`temporary network failure`, and `delivery disabled`. “Request accepted” means
Huawei accepted the API request; it does not prove that a physical device
displayed the notification.

## Disabled development mode

Use:

```bash
PUSH_DELIVERY_ENABLED=false
```

to disable every outbound provider, or:

```bash
HARMONYOS_PUSH_ENABLED=false
```

to disable HarmonyOS only. The HarmonyOS-specific flag defaults to disabled.
In either mode the backend does not load credentials, generate a JWT, or call
Push Kit. Recipient calculation and push-token registration continue, and a
delivery is not recorded as accepted.

## Notification payloads

The provider sends only V3 Alert messages (`push-type: 0`) using:

```json
{
  "payload": {
    "notification": {
      "category": "<service category>",
      "title": "<service title>",
      "body": "<service body>",
      "clickAction": {
        "actionType": 0,
        "data": {
          "event_type": "<event type>",
          "target_page": "<client routing hint>"
        }
      }
    }
  },
  "target": {
    "token": ["<one HarmonyOS Push token>"]
  }
}
```

Implemented service notifications:

- Help request: category `HEALTH`, title `需要帮助`, body equal to the selected
  help message, `event_type=help_request`, `target_page=help_alert`, and safe
  `alert_id`/`family_link_id` routing IDs.
- Offline alert: category `DEVICE_REMINDER`, title `家人长时间未在线`, existing
  offline-alert body, `event_type=offline_alert`,
  `target_page=elder_status`, and `family_link_id`.

The existing safety-confirmation notification remains Android data-only. It has
no existing Android title/body to reproduce, so Phase 2 does not invent a new
HarmonyOS notification for that event.

`actionType: 0` opens the application's entry ability. The HarmonyOS client
must explicitly read the supported `clickAction.data` values and implement any
page routing. The server fields alone do not configure navigation.

The application must have the matching service-notification classification
rights enabled in AppGallery Connect. Push Kit may apply its documented
marketing classification/frequency behavior when the relevant rights are not
enabled.

## Safe test procedure

1. Use a non-production project and a real signed HarmonyOS application on a
   test device.
2. Complete notification permission and Push Kit client setup.
3. Obtain a fresh token with `pushService.getToken()` and register it through
   the existing backend token endpoint.
4. Configure the test project service-account credential.
5. Set:

   ```bash
   PUSH_DELIVERY_ENABLED=true
   HARMONYOS_PUSH_ENABLED=true
   HARMONYOS_PUSH_TEST_MESSAGE=true
   ```

6. Trigger one help request or one due offline alert for the bound test child.
7. Check the backend's normalized result and AppGallery Connect diagnostics.
8. Confirm display/tap behavior on the signed physical device. A server
   `accepted` result alone does not prove device display.
9. Disable the provider again after testing if the environment is not intended
   for live sends.

Automated tests always use mocked HTTP transports and never send a real
notification.

## Delivery result categories

- `accepted`: Push Kit returned HTTP success with business code `80000000`.
  This means request accepted, not displayed on the device.
- `invalid_token`: Push Kit explicitly identified the single target under a
  permanent malformed/undecryptable token reason.
- `authentication_failure`: credentials cannot create a PS256 JWT, HTTP
  authentication failed, or Push Kit returned JWT-expired code `80200005`.
- `provider_rejected`: a non-transient response that is not a confirmed
  permanent-token error.
- `rate_limited`: HTTP rate limiting/traffic control or documented test-message
  rate limit `80300029`.
- `temporary_failure`: network error, transient HTTP failure, or provider
  internal code `81000001`.
- `delivery_disabled`: global or HarmonyOS delivery is disabled.
- `unsupported_provider`: no supported dispatcher route.
- `no_token`: no active provider token is available.

If JWT authentication fails, verify the key belongs to the configured project
and ensure the backend host clock is synchronized to UTC. The official `iat`
and `exp` validation depends on correct server time.

## Token invalidation

The send response contains `code`, `msg`, and `requestId`. The backend never
logs `msg`, because its `illegalTokens` details can contain full tokens.

Only an explicit single-token `tokenFormatError` or `decryptError` result marks
that HarmonyOS row invalid by setting
`device_push_tokens.push_token_invalidated_at`. Configuration/entitlement
reasons such as `noRight` and `appinfoError`, unknown provider errors, rate
limits, and network failures preserve the token.

An invalidated token is excluded from later sends. Uploading it freshly through
the existing authenticated registration endpoint clears the invalidation and
reactivates it. Users, devices, family links, alerts, and preferences are never
deleted by push invalidation.

## Logging and secrets

Logs contain only internal IDs, provider, event type, HTTP status, provider
code, normalized result, token length, and a masked token suffix. They never
contain:

- Full Push tokens
- JWTs
- Private keys
- Service-account JSON
- Backend bearer credentials
- Full notification payloads

## Real-device requirement

Unit and integration tests verify JWT structure, caching, provider requests,
routing, failure isolation, and invalidation with mocks. Real notification
delivery, notification display, AppGallery Connect classification rights, and
tap routing still require a real signed HarmonyOS application and physical
device. They are not verified by the automated backend suite.
