# Organizer recovery credential design (future, not deployed)

## Security boundary

`device_id`/`ANDROID_ID`, role, name, phone, locale, and package metadata are
identifiers only. None is sufficient proof to recover an organizer bearer
token. Existing organizers that never received a recovery credential remain
`LEGACY_IDENTITY_UNRECOVERABLE` after local app data is lost.

## Provisioning

When an organizer first registers (or explicitly enables recovery while
authenticated), generate at least 32 random bytes with the OS CSPRNG. Show
the one-time secret once, with an explicit instruction to store it outside the
app. Never log or return it again. Store only a memory-hard verifier (Argon2id
preferred; an equivalent vetted password-hash implementation is acceptable),
plus a key id, creation time, last-used time, expiry/revocation state, and a
failure counter. Do not derive it from `ANDROID_ID`.

## Recovery endpoint contract

`POST /api/v2/auth/organizer/recover` accepts `{device_id, recovery_secret}`.
The server looks up an organizer recovery record by an opaque identifier,
verifies the supplied secret with a constant-time password-hash check, and
returns a freshly generated bearer token only after a successful match. The
response is deliberately indistinguishable for unknown device/secret pairs
(generic 401), and never includes the old token or internal user identifiers.

On success, revoke the prior bearer-token hash (if present), issue a new
random token, rotate the recovery record's one-time use counter, and record a
non-secret audit event. Recovery is allowed only for the same organizer user;
it never creates a duplicate user or changes family membership.

## Abuse controls and tests

Apply per-device, per-IP, and global failure limits with exponential backoff;
return `Retry-After` without revealing whether the device exists. Do not log
the secret, bearer token, request body, or authorization header. Add tests for:

* provisioning stores a verifier, never plaintext;
* valid recovery issues a fresh token and rotates the old token;
* invalid/unknown secrets return generic 401;
* repeated failures trigger rate limiting;
* a recovery cannot cross users or create a duplicate organizer;
* authenticated circle operations succeed with the fresh token;
* request and application logs contain no secret material.

The additive migration and API/client contract are now implemented locally in
the 20260923 migration and `/api/v2/organizer/recover`; production deployment
is still a separate change. The Android client displays the newly issued code
once and provides the recovery entry state after a duplicate-registration
conflict. This cannot recover legacy organizers that never received a secret.
