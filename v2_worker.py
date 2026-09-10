"""Restart-safe local scheduler primitives for V2.

Production deployment can call ``process_checkin_windows`` from a durable
worker/cron runner. Each parent + schedule + UTC window is unique, so a
restart or two workers racing cannot create a second missed event or delivery.
"""

from datetime import timedelta

from sqlalchemy.orm import Session

import v2_models
from v2_notifications import queue_circle_notifications, queue_recipient_notification
from v2_time import CheckInState, evaluate_schedule, local_window_to_utc, localize_utc, parse_days


def _window_dates(now_utc, timezone_name, days_of_week):
    local_now = localize_utc(now_utc, timezone_name)
    days = parse_days(days_of_week)
    for offset in range(0, 2):
        local_date = local_now.date() - timedelta(days=offset)
        if local_date.weekday() in days:
            yield local_date


def process_checkin_windows(db: Session, now_utc) -> int:
    """Materialize recent schedule windows and mark only overdue windows missed."""
    changed = 0
    schedules = db.query(v2_models.CheckInSchedule).filter(v2_models.CheckInSchedule.enabled.is_(True)).all()
    for schedule in schedules:
        profile = db.get(v2_models.ParentProfile, schedule.parent_profile_id)
        if profile is None or not profile.active:
            continue
        for local_date in _window_dates(now_utc, schedule.timezone, schedule.days_of_week):
            scheduled = local_window_to_utc(local_date, schedule.local_time, schedule.timezone)
            event = db.query(v2_models.CheckInEvent).filter(
                v2_models.CheckInEvent.parent_profile_id == profile.id,
                v2_models.CheckInEvent.schedule_id == schedule.id,
                v2_models.CheckInEvent.scheduled_for_utc == scheduled,
            ).first()
            if event is None:
                event = v2_models.CheckInEvent(
                    parent_profile_id=profile.id,
                    schedule_id=schedule.id,
                    scheduled_for_utc=scheduled,
                    local_date=local_date,
                    event_type="scheduled",
                    status=CheckInState.UPCOMING.value,
                )
                db.add(event)
                db.flush()
            state = evaluate_schedule(now_utc, schedule.local_time, schedule.timezone, schedule.grace_period_minutes, schedule.days_of_week)
            if event.occurred_at is None and scheduled <= now_utc and now_utc >= scheduled + timedelta(minutes=schedule.grace_period_minutes) and event.status != CheckInState.MISSED.value:
                event.status = CheckInState.MISSED.value
                changed += 1
                queue_circle_notifications(db, profile.family_circle_id, f"missed:{event.id}", "missed_checkin", profile.display_name)
            elif event.occurred_at is None and event.status not in {CheckInState.MISSED.value, CheckInState.HELP_REQUESTED.value}:
                new_state = (
                    CheckInState.GRACE_PERIOD.value
                    if now_utc > scheduled
                    else CheckInState.DUE.value
                    if now_utc == scheduled
                    else CheckInState.UPCOMING.value
                )
                if event.status != new_state:
                    event.status = new_state
                    changed += 1
    db.commit()
    return changed


def process_escalations(db: Session, now_utc) -> int:
    """Queue the next configured member once an unresolved missed event ages."""
    changed = 0
    missed = db.query(v2_models.CheckInEvent).filter(v2_models.CheckInEvent.status == CheckInState.MISSED.value).all()
    for event in missed:
        profile = db.get(v2_models.ParentProfile, event.parent_profile_id)
        if profile is None:
            continue
        rules = db.query(v2_models.EscalationRule).filter(
            v2_models.EscalationRule.family_circle_id == profile.family_circle_id,
            v2_models.EscalationRule.enabled.is_(True),
        ).order_by(v2_models.EscalationRule.priority.asc()).all()
        for rule in rules:
            schedule = db.get(v2_models.CheckInSchedule, event.schedule_id) if event.schedule_id else None
            grace_minutes = schedule.grace_period_minutes if schedule else 0
            trigger_at = event.scheduled_for_utc + timedelta(minutes=grace_minutes + rule.delay_minutes)
            if now_utc < trigger_at:
                continue
            member = db.get(v2_models.FamilyMembership, rule.family_member_id)
            if member is None or member.membership_status != "active":
                continue
            if queue_recipient_notification(db, f"escalation:{event.id}:{rule.id}", member.user_id, "missed_checkin", profile.display_name):
                changed += 1
                event.escalation_state = "ESCALATED"
                event.current_escalation_priority = rule.priority
            # One escalation pass notifies all rules whose delay is due. The
            # unique event/rule/recipient key makes retries duplicate-safe.
    db.commit()
    return changed
