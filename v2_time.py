"""Pure timezone and check-in window rules for Parent Check-In V2."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


UTC = timezone.utc


class CheckInState(StrEnum):
    UPCOMING = "UPCOMING"
    DUE = "DUE"
    GRACE_PERIOD = "GRACE_PERIOD"
    CHECKED_IN = "CHECKED_IN"
    LATE_CHECKED_IN = "LATE_CHECKED_IN"
    MISSED = "MISSED"
    HELP_REQUESTED = "HELP_REQUESTED"


def canonical_timezone(name: str) -> str:
    """Validate and return an IANA zone name, never a raw UTC offset."""
    value = name.strip()
    if not value or value.upper() in {"UTC+0", "UTC-0", "GMT+0"}:
        raise ValueError("an IANA timezone identifier is required")
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone: {value}") from exc
    return value


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def localize_utc(value: datetime, timezone_name: str) -> datetime:
    zone = ZoneInfo(canonical_timezone(timezone_name))
    return as_utc(value).replace(tzinfo=UTC).astimezone(zone)


def local_window_to_utc(local_date: date, local_time: str, timezone_name: str) -> datetime:
    """Convert an intended wall-clock schedule to a naive UTC database value.

    Ambiguous fall-back times use the first occurrence. Non-existent spring
    forward times are rejected rather than silently shifting a parent's
    check-in deadline.
    """
    try:
        hour, minute = (int(part) for part in local_time.split(":"))
        wall = datetime.combine(local_date, time(hour, minute))
    except (ValueError, TypeError) as exc:
        raise ValueError("local_time must use HH:MM") from exc
    zone = ZoneInfo(canonical_timezone(timezone_name))
    candidate = wall.replace(tzinfo=zone, fold=0)
    round_trip = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if round_trip != wall:
        raise ValueError("local schedule time does not exist in this timezone")
    return candidate.astimezone(UTC).replace(tzinfo=None)


def parse_days(value: str) -> set[int]:
    days = {int(part) for part in value.split(",") if part.strip()}
    if not days or not days.issubset(set(range(7))):
        raise ValueError("days_of_week must contain ISO weekday values 0..6")
    return days


def scheduled_window_for(now_utc: datetime, local_time: str, timezone_name: str, days_of_week: str) -> tuple[date, datetime] | None:
    local_now = localize_utc(now_utc, timezone_name)
    days = parse_days(days_of_week)
    for offset in range(0, 8):
        local_date = local_now.date() - timedelta(days=offset)
        if local_date.weekday() in days:
            scheduled = local_window_to_utc(local_date, local_time, timezone_name)
            if scheduled <= as_utc(now_utc):
                return local_date, scheduled
    return None


def next_scheduled_window(now_utc: datetime, local_time: str, timezone_name: str, days_of_week: str) -> tuple[date, datetime]:
    local_now = localize_utc(now_utc, timezone_name)
    days = parse_days(days_of_week)
    for offset in range(0, 8):
        local_date = local_now.date() + timedelta(days=offset)
        if local_date.weekday() not in days:
            continue
        scheduled = local_window_to_utc(local_date, local_time, timezone_name)
        if scheduled > as_utc(now_utc):
            return local_date, scheduled
    raise ValueError("no enabled schedule day found within seven days")


def evaluate_window(now_utc: datetime, scheduled_utc: datetime, grace_period_minutes: int) -> CheckInState:
    now = as_utc(now_utc)
    scheduled = as_utc(scheduled_utc)
    if now < scheduled:
        return CheckInState.UPCOMING
    if now == scheduled:
        return CheckInState.DUE
    if now < scheduled + timedelta(minutes=grace_period_minutes):
        return CheckInState.GRACE_PERIOD
    return CheckInState.MISSED


def checkin_result(occurred_at_utc: datetime, scheduled_utc: datetime) -> CheckInState:
    return (
        CheckInState.CHECKED_IN
        if as_utc(occurred_at_utc) <= as_utc(scheduled_utc)
        else CheckInState.LATE_CHECKED_IN
    )


@dataclass(frozen=True)
class WindowEvaluation:
    local_date: date
    scheduled_for_utc: datetime
    state: CheckInState
    parent_local_time: datetime


def evaluate_schedule(now_utc: datetime, local_time: str, timezone_name: str, grace_period_minutes: int, days_of_week: str = "0,1,2,3,4,5,6") -> WindowEvaluation:
    local_now = localize_utc(now_utc, timezone_name)
    candidate = scheduled_window_for(now_utc, local_time, timezone_name, days_of_week)
    if candidate is None:
        next_date, scheduled = next_scheduled_window(now_utc, local_time, timezone_name, days_of_week)
        return WindowEvaluation(next_date, scheduled, CheckInState.UPCOMING, local_now)
    local_date, scheduled = candidate
    return WindowEvaluation(local_date, scheduled, evaluate_window(now_utc, scheduled, grace_period_minutes), local_now)
