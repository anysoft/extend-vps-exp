from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


JST = ZoneInfo('Asia/Tokyo')
RENEWAL_WINDOW = timedelta(hours=12)
RENEWAL_PERIOD = timedelta(hours=24)


def now_in_jst() -> datetime:
    return datetime.now(JST)


def renewal_window_for_expiry_date(expiry_date: date) -> tuple[datetime, datetime]:
    """Return a fallback window when an old state has only an expiry date.

    XServer exposes the expiry as a calendar date without a time. Treat the
    service as valid through that date, so its exclusive deadline is midnight
    at the start of the following day in Japan. New state files use precise
    timestamps recorded after a successful renewal instead.
    """
    expires_at = datetime.combine(expiry_date + timedelta(days=1), time.min, tzinfo=JST)
    return expires_at - RENEWAL_WINDOW, expires_at


def renewal_window_after_success(renewed_at_jst: datetime) -> tuple[datetime, datetime]:
    renewed_at = _as_jst(renewed_at_jst)
    return renewed_at + RENEWAL_WINDOW, renewed_at + RENEWAL_PERIOD


def renewal_window_from_state(state: dict, expiry_date: date) -> tuple[datetime, datetime]:
    try:
        renewal_opens_at = _parse_state_datetime(state.get('renewal_opens_at_jst'))
        expires_at = _parse_state_datetime(state.get('estimated_expiry_at_jst'))
        state_expiry_date = datetime.strptime(state.get('next_expiry_date', ''), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return renewal_window_for_expiry_date(expiry_date)

    if (
        renewal_opens_at < expires_at
        and state_expiry_date == expiry_date
        and expires_at.date() == expiry_date
    ):
        return renewal_opens_at, expires_at

    return renewal_window_for_expiry_date(expiry_date)


def is_in_renewal_window(expiry_date: date, now_jst: datetime, state: dict | None = None) -> bool:
    renewal_opens_at, expires_at = renewal_window_from_state(state or {}, expiry_date)
    normalized_now = _as_jst(now_jst)
    return renewal_opens_at <= normalized_now < expires_at


def should_attempt_login_from_state(state: dict, now_jst: datetime) -> bool:
    """Use the cached date to avoid logging in outside the renewal window.

    Missing or malformed state deliberately falls back to logging in so that a
    damaged cache cannot permanently prevent renewal.
    """
    next_expiry = state.get('next_expiry_date')
    if not next_expiry:
        return True

    try:
        expiry_date = datetime.strptime(next_expiry, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return True

    return is_in_renewal_window(expiry_date, now_jst, state)


def _as_jst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=JST)
    return value.astimezone(JST)


def _parse_state_datetime(value: str | None) -> datetime:
    if not value:
        raise ValueError('missing state datetime')
    return _as_jst(datetime.fromisoformat(value.replace('Z', '+00:00')))
