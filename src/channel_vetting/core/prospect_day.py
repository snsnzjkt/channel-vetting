"""
Single source of truth for "what day is it" where prospect counting is
concerned.

Both the "Date Added" value written onto a record and the daily-cap query
that counts those records must use this, or they drift apart and the cap
silently misreads its own budget.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from channel_vetting.config import PROSPECT_DAY_TZ


def today_iso() -> str:
    """Today's date as YYYY-MM-DD in the configured prospect-day zone."""
    return datetime.now(ZoneInfo(PROSPECT_DAY_TZ)).strftime("%Y-%m-%d")
