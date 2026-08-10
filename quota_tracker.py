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
from datetime import datetime, timedelta

from config import QUOTA_LOG_FILE, QUOTA_CEILING, QUOTA_COST_SEARCH_LIST

logger = logging.getLogger(__name__)

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

# Keep roughly a week of daily totals. The log is append-only by nature (one
# key per Pacific day, forever), and nothing in the pipeline reads yesterday:
# get_today_spend() and can_afford_search() only ever look at today's key. A
# week is kept anyway so a human debugging "why did we run dry on Tuesday"
# still has the recent trend, and so a run that straddles midnight Pacific
# doesn't lose the day it started in. Anything older is dead weight in a file
# that gets fully rewritten on every single record_spend() call.
LOG_RETENTION_DAYS = 7


def today_pacific() -> str:
    """
    Return today's date (YYYY-MM-DD) in Pacific Time, the quota reset zone.

    Public because `discovery.py` keys its search cache on the same day
    boundary — see discovery._cache_key() for why those two must roll
    together rather than each picking their own clock.
    """
    return datetime.now(PACIFIC_TZ).strftime("%Y-%m-%d")


# Kept as an alias so any older internal reference to the private name still
# resolves. New code should call today_pacific().
_today_pacific = today_pacific


def _load_log() -> dict:
    if not os.path.exists(QUOTA_LOG_FILE):
        return {}
    try:
        with open(QUOTA_LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Quota log file was unreadable/corrupt; starting fresh.")
        return {}


def _prune(log: dict) -> dict:
    """
    Drop day keys older than LOG_RETENTION_DAYS, keeping the log bounded.

    Unparseable keys are kept rather than discarded: this file is
    hand-inspectable and someone may have annotated it, and silently deleting
    a key we don't understand is worse than carrying it.
    """
    cutoff = (datetime.now(PACIFIC_TZ).date() - timedelta(days=LOG_RETENTION_DAYS))
    kept = {}
    for key, value in log.items():
        try:
            day = datetime.strptime(key, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            kept[key] = value
            continue
        if day >= cutoff:
            kept[key] = value
    return kept


def _save_log(log: dict) -> None:
    """
    Persist the spend log atomically: write to a `.tmp` sibling, fsync, then
    os.replace() over the real file.

    This is the most important write in the pipeline to get right, because
    losing it FAILS OPEN. `open(QUOTA_LOG_FILE, "w")` truncates the file the
    moment it is opened, so a Ctrl-C or a killed CI job mid-json.dump() leaves
    a half-written document. _load_log() catches the resulting
    json.JSONDecodeError and returns {} — which reads as "0 units spent
    today", so can_afford_search() cheerfully authorises a fresh full
    QUOTA_CEILING on top of everything already spent. That is the one
    direction that OVERSPENDS, and it is exactly the failure mode
    airtable_client.count_added_today() was deliberately hardened against by
    raising instead of returning 0 (see "Daily caps" in CLAUDE.md). Losing
    accuracy in the safe direction (over-counting) merely wastes headroom;
    losing it here spends real quota that isn't there.
    The fsync is load-bearing: os.replace() orders the rename but not the
    data, so without it a crash can leave the renamed file present and empty
    — the same fail-open {} by a different route. os.replace() is atomic on
    Windows as well as POSIX (os.rename is not, when the target exists).
    """
    log = _prune(log)
    tmp_path = f"{QUOTA_LOG_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, QUOTA_LOG_FILE)
    except BaseException:
        # Clean up the half-written scratch file and let the error surface.
        # BaseException, not Exception, so a KeyboardInterrupt — the very
        # interrupt this whole function is defending against — doesn't leave
        # a stale .tmp behind for the next run to trip over. The real log is
        # already safe either way; os.replace() hasn't run.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def get_today_spend() -> int:
    """Total quota units spent so far today (Pacific Time)."""
    log = _load_log()
    return log.get(today_pacific(), 0)


def record_spend(units: int, call_name: str = "") -> int:
    """
    Add `units` to today's (Pacific Time) spend total and persist it.
    Returns the new running total for today.
    """
    log = _load_log()
    today = today_pacific()
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
