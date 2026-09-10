# Parent Check-In V2 backend

V2 is additive during the client transition. The legacy `elder`/`child`
routes and tables remain available, while the neutral V2 tables use the
`v2_` prefix and the `/api/v2/` route namespace. Existing local data is test
data and is not migrated or reset by this change.

## Domain and authorization

`FamilyCircle` has one organizer. `FamilyMembership` grants `ORGANIZER`,
`FAMILY_MEMBER`, or `PARENT` access. Parent profiles are scoped to a circle;
all V2 authorization first resolves the bearer-token user and then checks an
active membership in the requested circle. Parent check-in and help requests
derive the parent from the authenticated identity, never a submitted parent
ID.

## Schedule rules

Schedules store `HH:MM`, an IANA timezone, ISO weekday values, and a grace
period. Event timestamps are UTC-naive database values representing UTC; local
date/time is derived from the parent's zone. A schedule window is unique by
parent, schedule, and scheduled UTC instant.

The state machine is deterministic:

* before the scheduled instant: `UPCOMING`
* exactly at the instant: `DUE`
* after the instant and before grace expiry: `GRACE_PERIOD`
* at or after grace expiry without a check-in: `MISSED`
* a check-in at/before the scheduled instant: `CHECKED_IN`
* a check-in after it: `LATE_CHECKED_IN`
* an active help request overlays the family presentation as `HELP_REQUESTED`

## Worker and notifications

`scripts/run_v2_scheduler.py` runs one explicitly non-production scheduler
pass. `v2_worker.process_checkin_windows` materializes and updates durable
windows, and `process_escalations` queues due escalation deliveries. Unique
event/recipient keys make retries and restarts duplicate-safe. A deployment
should run this from a durable cron/worker rather than relying on the legacy
in-process 15-minute offline loop.

`v2_notifications.py` creates one logical localized event pipeline. Recipient
locale is read from the recipient's user/device context, and FCM/HMS provider
selection is recorded per delivery. The existing provider adapters remain
untouched; credential-backed sending is a deployment hardening step.

## Migration and data safety

Alembic is now the explicit V2 migration tool. The initial revision creates
the V2 tables and the nullable per-user `locale_tag` column. It is additive,
does not drop legacy tables, and can bootstrap a local identity table for a
fresh V2 test database. The legacy startup migration remains for legacy
clients only and should not be used as the long-term V2 schema migration
mechanism.

Circle deletion is explicit and organizer-only. A member can leave without
deleting the circle; removal deactivates that membership and any parent
profile in the circle. Account deletion is explicit at `DELETE /api/v2/users/me`;
an organizer must first explicitly delete or transfer each circle, while a
non-organizer can delete their own V2 membership/profile data without deleting
the circle.

Billing is deliberately absent. Circle creation is temporarily available to
support backend/client integration tests; organizer entitlement enforcement
belongs to the later Billing phase.
