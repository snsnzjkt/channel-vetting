"""
Tracks daily YouTube Data API v3 quota spend in a local JSON log.

The YouTube API quota resets at midnight Pacific Time (not UTC, not local
system time), so all "today" bucketing here is done in the
America/Los_Angeles zone regardless of what timezone the machine running
this script is in.
"""
import json
import logging
import os
from zoneinfo import ZoneInfo
from datetime import datetime

from config import QUOTA_LOG_FILE, QUOTA_CEILING, QUOTA_COST_SEARCH_LIST

logger = logging.getLogger(__name__)

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")


def _today_pacific() -> str:
    """Return today's date (YYYY-MM-DD) in Pacific Time, the quota reset zone."""
    return datetime.now(PACIFIC_TZ).strftime("%Y-%m-%d")


def _load_log() -> dict:
    if not os.path.exists(QUOTA_LOG_FILE):
        return {}
    try:
        with open(QUOTA_LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Quota log file was unreadable/corrupt; starting fresh.")
        return {}


def _save_log(log: dict) -> None:
    with open(QUOTA_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)


def get_today_spend() -> int:
    """Total quota units spent so far today (Pacific Time)."""
    log = _load_log()
    return log.get(_today_pacific(), 0)


def record_spend(units: int, call_name: str = "") -> int:
    """
    Add `units` to today's (Pacific Time) spend total and persist it.
    Returns the new running total for today.
    """
    log = _load_log()
    today = _today_pacific()
    log[today] = log.get(today, 0) + units
    _save_log(log)
    logger.info("Quota spend: +%d units (%s) -> %d today", units, call_name or "call", log[today])
    return log[today]


def can_afford_search() -> bool:
    """
    Check whether spending another search.list call (100 units) would
    exceed QUOTA_CEILING. search.list is checked explicitly (rather than
    generically) because it is by far the most expensive call type and
    the one most likely to blow the daily budget if run unchecked.
    """
    projected = get_today_spend() + QUOTA_COST_SEARCH_LIST
    if projected > QUOTA_CEILING:
        logger.warning(
            "Skipping search.list call: projected spend %d would exceed QUOTA_CEILING %d",
            projected, QUOTA_CEILING,
        )
        return False
    return True
