"""Date-window resolution for the admin dashboard / analytics (§6.3).

The back office passes an optional ``date_from`` / ``date_to`` (calendar dates).
This helper turns them into a half-open ``[start, end)`` UTC datetime window —
``end`` is the day *after* ``date_to`` so the whole ``date_to`` day is included —
and applies a sensible default window when either bound is omitted.
"""

from datetime import UTC, date, datetime, time, timedelta

# Default look-back when no ``date_from`` is supplied (§6.3).
DEFAULT_WINDOW_DAYS: int = 30


def resolve_window(
    date_from: date | None,
    date_to: date | None,
    *,
    default_days: int = DEFAULT_WINDOW_DAYS,
) -> tuple[datetime, datetime]:
    """Resolve an optional date pair into a half-open ``[start, end)`` window.

    Args:
        date_from: Inclusive start date, or ``None`` for
            ``date_to - default_days``.
        date_to: Inclusive end date, or ``None`` for today (UTC).
        default_days: Look-back applied when ``date_from`` is omitted.

    Returns:
        tuple[datetime, datetime]: ``(start, end)`` UTC datetimes, ``end``
            exclusive (the day after ``date_to``).
    """
    today = datetime.now(UTC).date()
    end_date = date_to or today
    start_date = date_from or (end_date - timedelta(days=default_days))
    # Naive UTC datetimes: the order timestamps are ``TIMESTAMP WITHOUT TIME
    # ZONE`` (naive) so a tz-aware bound can't be compared to them; asyncpg still
    # treats a naive value as UTC for the ``visit.occurred_at`` timestamptz
    # column, so one naive window works against both.
    start = datetime.combine(start_date, time.min)
    end = datetime.combine(end_date + timedelta(days=1), time.min)
    return start, end
