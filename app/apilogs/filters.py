"""Date-range and status-code filters for the dashboard."""
from datetime import date, timedelta

PERIODS = [
    ("today", "Today"),
    ("yesterday", "Since yesterday"),
    ("7d", "Last 7 days"),
    ("month", "Last month"),
    ("3m", "Last 3 months"),
    ("all", "All"),
]
# "All" spans the log retention window (API_LOG_TTL_DAYS). With no TTL, rows
# never expire, so cap it here: Cassandra can't list which days hold data.
ALL_FALLBACK_DAYS = 365
# Ranges longer than this are charted per week instead of per day.
WEEKLY_CHART_AFTER_DAYS = 62
DEFAULT_PERIOD = "7d"
STATUS_CLASSES = ["2xx", "3xx", "4xx", "5xx"]


def _months_before(day, months):
    total = day.year * 12 + (day.month - 1) - months
    year, month = divmod(total, 12)
    # Clamp e.g. 31 March -> 28/29 February.
    for d in (day.day, 30, 29, 28):
        try:
            return date(year, month + 1, d)
        except ValueError:
            continue


def days_for_period(period, today, retention_days=None):
    """Inclusive list of local dates, oldest first.

    retention_days: how long log rows live; bounds "all". None/0 = no TTL.
    """
    if period == "today":
        start = today
    elif period == "yesterday":
        start = today - timedelta(days=1)
    elif period == "7d":
        start = today - timedelta(days=6)
    elif period == "month":
        start = _months_before(today, 1)
    elif period == "3m":
        start = _months_before(today, 3)
    elif period == "all":
        start = today - timedelta(days=(retention_days or ALL_FALLBACK_DAYS) - 1)
    else:
        raise ValueError(f"unknown period {period!r}")
    return [start + timedelta(days=i) for i in range((today - start).days + 1)]


def chart_buckets(days):
    """Group the (oldest-first) days into chart bars: one per day, or one per
    7 days for long ranges. Weeks end on the newest day, so the latest bar is
    always a full week and only the oldest may be partial."""
    if len(days) <= WEEKLY_CHART_AFTER_DAYS:
        return [[d] for d in days]
    newest_first = days[::-1]
    weeks = [newest_first[i:i + 7][::-1] for i in range(0, len(newest_first), 7)]
    return weeks[::-1]


def status_class(code):
    return f"{code // 100}xx"


def parse_status(value):
    """'' -> None (no filter), '4xx' -> '4xx', '404' -> 404. Invalid -> None."""
    value = (value or "").strip().lower()
    if value in STATUS_CLASSES:
        return value
    if value.isdigit() and 100 <= int(value) <= 599:
        return int(value)
    return None


def status_matches(code, status_filter):
    if status_filter is None:
        return True
    if isinstance(status_filter, str):
        return status_class(code) == status_filter
    return code == status_filter
