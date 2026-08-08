from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


JST = ZoneInfo('Asia/Tokyo')
RENEWAL_WINDOW = timedelta(hours=12)


def now_in_jst() -> datetime:
    return datetime.now(JST)


def renewal_window_for_expiry_date(expiry_date: date) -> tuple[datetime, datetime]:
    """Return the final 12 hours of the displayed XServer expiry date.

    XServer exposes the expiry as a calendar date without a time. The renewal
    window is therefore 12:00 through 24:00 JST on that date. Do not infer it
    from the previous renewal time; that caused premature logins around
    midnight when the actual renewal controls were still unavailable.
    """
    expires_at = datetime.combine(expiry_date + timedelta(days=1), time.min, tzinfo=JST)
    return expires_at - RENEWAL_WINDOW, expires_at


def renewal_window_from_state(_state: dict, expiry_date: date) -> tuple[datetime, datetime]:
    # Keep this wrapper for callers that still pass state, but deliberately
    # ignore legacy inferred timestamps such as renewal_opens_at_jst.
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
