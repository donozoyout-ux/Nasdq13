"""
Timezone helpers
- US market hours are defined in America/New_York (handles DST automatically)
- Display times are shown in Europe/Istanbul (Turkey, UTC+3, no DST)
"""
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

ISTANBUL = ZoneInfo("Europe/Istanbul")
NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc


def to_turkey(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a datetime to Europe/Istanbul wall time"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ISTANBUL)


def format_turkey(dt: Optional[datetime], fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Format a datetime in Turkey wall time"""
    t = to_turkey(dt)
    if t is None:
        return "-"
    return t.strftime(fmt)


def now_turkey() -> datetime:
    return datetime.now(ISTANBUL)


def now_new_york() -> datetime:
    return datetime.now(NEW_YORK)


def premarket_report_due(config: dict, now: Optional[datetime] = None) -> bool:
    """True during the pre-market window right before the regular open
    (default: 20 min before 09:30 ET). This is when the 'BUGÜN İZLE'
    pre-market report should fire — fresh candidates for today's session."""
    mh = config.get("market_hours", {})
    reg_open = _parse_hhmm(mh.get("regular_open", "09:30"))
    minutes = int(config.get("smallcap", {}).get("premarket_report", {}).get("minutes_before_open", 20))
    window_start = reg_open - timedelta(minutes=minutes)
    now_et = (now or now_new_york()).astimezone(NEW_YORK)
    secs = _seconds_since_midnight(now_et)
    return window_start <= timedelta(seconds=secs) < reg_open


def _parse_hhmm(value: str) -> timedelta:
    """Parse 'HH:MM' into a timedelta since midnight"""
    hh, mm = value.split(":")
    return timedelta(hours=int(hh), minutes=int(mm))


def _seconds_since_midnight(dt: datetime) -> int:
    return dt.hour * 3600 + dt.minute * 60 + dt.second


def market_status(config: dict, now: Optional[datetime] = None) -> dict:
    """
    Determine current US market session status using America/New_York time.
    Returns dict with keys: open, session, now_et, now_tr.
    """
    mh = config.get("market_hours", {})
    now = now or now_new_york()
    now_et = now.astimezone(NEW_YORK)

    weekday = now_et.weekday()
    secs = _seconds_since_midnight(now_et)

    pre_start = _parse_hhmm(mh.get("pre_market_start", "04:00"))
    reg_open = _parse_hhmm(mh.get("regular_open", "09:30"))
    reg_close = _parse_hhmm(mh.get("regular_close", "16:00"))
    after_end = _parse_hhmm(mh.get("after_hours_end", "20:00"))

    open_now = False
    session = "closed"
    if weekday < 5:  # Mon-Fri
        if reg_open <= timedelta(seconds=secs) <= reg_close:
            open_now = True
            session = "regular"
        elif pre_start <= timedelta(seconds=secs) < reg_open:
            session = "pre_market"
        elif reg_close < timedelta(seconds=secs) <= after_end:
            session = "after_hours"

    return {
        "open": open_now,
        "session": session,
        "now_et": now_et,
        "now_tr": now_et.astimezone(ISTANBUL),
        "timezone": "America/New_York",
        "display_timezone": "Europe/Istanbul",
    }
