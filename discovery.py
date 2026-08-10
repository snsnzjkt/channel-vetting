"""
Discovers candidate channels via YouTube search.list.

We deliberately search type=video rather than type=channel: video search
surfaces channels that are actively publishing and ranking for the
keyword right now, whereas channel search tends to surface old/inactive
channels that merely have a matching name/description.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

# Imported for `requests.RequestException` only — the actual call goes out
# through the shared retrying session below, never `requests.get()`.
import requests

from config import (
    YOUTUBE_API_BASE_URL,
    YOUTUBE_API_KEY,
    SEARCH_CACHE_FILE,
    QUOTA_COST_SEARCH_LIST,
    API_SLEEP_SECONDS,
)
from http_client import YOUTUBE as HTTP, safe_body
from quota_tracker import can_afford_search, record_spend, today_pacific

logger = logging.getLogger(__name__)


def _load_cache() -> dict:
    if not os.path.exists(SEARCH_CACHE_FILE):
        return {}
    try:
        with open(SEARCH_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("Search cache file was unreadable/corrupt; starting fresh.")
        return {}


def _save_cache(cache: dict) -> None:
    """
    Write the cache atomically: full contents to a `.tmp` sibling, fsync, then
    os.replace() over the real file.

    A plain open(..., "w") truncates the file the instant it is opened, so a
    Ctrl-C or a killed CI job part-way through json.dump() leaves a valid path
    holding half a JSON document. _load_cache() then logs "unreadable/corrupt;
    starting fresh" and returns {} — which reads as "no keyword searched
    today" and re-spends 100 units on every keyword already paid for. The
    fsync matters because os.replace() only orders the rename, not the data:
    without it a crash can leave the renamed file present but empty.
    os.replace() is atomic on both Windows and POSIX (unlike os.rename, which
    fails on Windows when the destination exists).
    """
    tmp_path = f"{SEARCH_CACHE_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, SEARCH_CACHE_FILE)
    except BaseException:
        # Same cleanup as quota_tracker._save_log(): don't leave a stale
        # scratch file behind on the interrupt this pattern exists to survive.
        # The real cache is untouched — os.replace() hasn't run.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _cache_key(keyword: str) -> str:
    """
    Cache key for one keyword on one day — where "day" is the PACIFIC day,
    the same boundary `quota_tracker` keys spend on.

    These two clocks must roll TOGETHER. When this used UTC, the cache day
    and the quota day were 7-8 hours apart: a run between 00:00 and 08:00
    UTC saw a brand-new cache day (so every keyword was re-searched at 100
    units each) while charging that spend to a Pacific quota day that was
    already partly spent — paying twice out of a ceiling that was already
    shrinking. Sharing quota_tracker.today_pacific() makes "the day the
    cache resets" and "the day the budget resets" the same instant by
    construction.

    This is NOT an invitation to unify all three clocks. The prospect day
    (`prospect_day.py`, `PROSPECT_DAY_TZ` = America/Toronto) is deliberately
    a DIFFERENT zone from the quota day — see "Time zones — three clocks,
    two deliberately different" in CLAUDE.md. Cache and quota are one clock
    because they measure the same thing (Google's reset schedule); the
    prospect day measures the reviewing team's working day and must stay
    separate.
    """
    today = today_pacific()
    return f"{today}::{keyword.lower().strip()}"


def discover_channels_by_keyword(keyword: str, max_results: int = 50, days_back: int = 90) -> list[dict]:
    """
    Search YouTube for videos matching `keyword` and extract the unique
    channels behind them.

    Quota cost: 100 units per search.list call, regardless of max_results
    (results are paginated in pages of up to 50, so max_results=50 is a
    single call; anything above 50 costs one additional 100-unit call per
    extra page of up to 50 results).

    Returns a list of {"channel_id": ..., "channel_title": ..., "matched_keywords": [keyword]}.
    Cached per keyword per **Pacific** day (the same day boundary the quota
    ledger uses — see _cache_key()) so re-running the same keyword on the
    same day costs zero additional quota.
    """
    cache = _load_cache()
    key = _cache_key(keyword)
    if key in cache:
        logger.info("Cache hit for keyword '%s' (today) — skipping search.list call.", keyword)
        return cache[key]

    if not YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY is not set. Check your .env file.")

    published_after = None
    if days_back:
        published_after = (
            datetime.now(timezone.utc) - timedelta(days=days_back)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    channels: dict[str, dict] = {}
    page_token = None
    results_collected = 0
    # Tracks whether this keyword's search finished cleanly (ran out of
    # pages/results on its own) vs. was cut short by a transient failure
    # (quota ceiling, a non-200 response, or a network exception). Only a
    # clean completion gets cached — caching a truncated/empty result from a
    # transient error would poison this keyword's cache for the rest of the
    # Pacific day (see _cache_key()), silently killing its discovery with no
    # error signal.
    complete = True

    while results_collected < max_results:
        if not can_afford_search():
            logger.warning("Quota ceiling reached — stopping discovery for keyword '%s'.", keyword)
            complete = False
            break

        # NO "key" ENTRY HERE — the shared YOUTUBE session sends the API key
        # as an `X-goog-api-key` header instead. `requests` embeds the full
        # request URL in its exception messages, so a key in `params` gets
        # printed verbatim by any unhandled network error — and in CI that
        # lands in an Actions log retained for 90 days. Do not put it back.
        params = {
            "part": "snippet",
            "q": keyword,
            "type": "video",
            "order": "relevance",
            "maxResults": min(50, max_results - results_collected),
        }
        if published_after:
            params["publishedAfter"] = published_after
        if page_token:
            params["pageToken"] = page_token

        try:
            resp = HTTP.get(f"{YOUTUBE_API_BASE_URL}/search", params=params, timeout=30)
        except requests.RequestException as exc:
            # The shared session already retried connect/read failures with
            # backoff, so arriving here means the network stayed down. Treat
            # it like any other cut-short search: log, mark incomplete so the
            # partial result is NOT cached, and stop this keyword. Without
            # this the exception propagated out of run_discovery() and killed
            # the whole run — losing every niche, not just this keyword.
            #
            # record_spend() is deliberately NOT called: a request that never
            # got a response from Google was never billed, and charging it
            # would shrink our own ceiling for calls that could still succeed.
            logger.error("search.list request failed for '%s': %s", keyword, exc)
            complete = False
            break

        if resp.status_code != 200:
            # Charge nothing on a non-200 either, and note the ordering: this
            # check comes BEFORE record_spend() on purpose. The most common
            # non-200 here is a 403 `quotaExceeded`, which Google bills at 0
            # units — booking it as 100 over-counted spend we never made and
            # needlessly shrank can_afford_search()'s remaining headroom for
            # the rest of the day.
            logger.error(
                "search.list failed for '%s': %s %s",
                keyword, resp.status_code, safe_body(resp),
            )
            complete = False
            break

        # Only a served 200 actually cost 100 units, so this is the one place
        # the spend is real. CLAUDE.md's rule is "record_spend() right after
        # the request"; right after a *successful* request is what keeps the
        # ledger accurate.
        record_spend(QUOTA_COST_SEARCH_LIST, call_name=f"search.list('{keyword}')")

        data = resp.json()
        items = data.get("items", [])
        for item in items:
            snippet = item.get("snippet", {})
            channel_id = snippet.get("channelId")
            channel_title = snippet.get("channelTitle")
            if not channel_id:
                continue
            if channel_id not in channels:
                channels[channel_id] = {
                    "channel_id": channel_id,
                    "channel_title": channel_title,
                    "matched_keywords": [keyword],
                }

        results_collected += len(items)
        page_token = data.get("nextPageToken")
        if not page_token or not items:
            break

        time.sleep(API_SLEEP_SECONDS)

    result = list(channels.values())
    if complete:
        cache[key] = result
        _save_cache(cache)
    else:
        logger.warning(
            "Not caching keyword '%s' — its search was cut short, so a healthy retry "
            "later today must re-query rather than reuse this partial result.",
            keyword,
        )
    return result


def run_discovery(
    keywords: list[str],
    max_results_per_keyword: int = 50,
    days_back: int = 90,
    exclude_ids: set[str] | None = None,
    target_fresh: int | None = None,
) -> list[dict]:
    """
    Run discover_channels_by_keyword() across `keywords`, dedupe channels
    across searches, and merge matched_keywords for channels hit by more
    than one.

    When `target_fresh` is set, stops searching further keywords once
    that many *fresh* candidates (those not in `exclude_ids`) have been
    banked. Each search.list call costs 100 units, so this is the main
    lever on daily quota spend — the caller only needs enough candidates
    to fill the day's remaining cap.

    Stopping early means later keywords in the list get searched less
    often, skewing the candidate mix toward whatever is listed first.
    Accepted deliberately; rotate the keyword order if that becomes a
    problem.

    `exclude_ids` is supplied by the caller so this module stays ignorant
    of Airtable.
    """
    exclude_ids = exclude_ids or set()
    merged: dict[str, dict] = {}

    # enumerate, not keywords.index(): a duplicated keyword would make
    # index() report the position of the first copy.
    for position, keyword in enumerate(keywords, start=1):
        logger.info("Discovering channels for keyword: '%s'", keyword)
        found = discover_channels_by_keyword(keyword, max_results=max_results_per_keyword, days_back=days_back)

        for channel in found:
            cid = channel["channel_id"]
            if cid in merged:
                existing_keywords = set(merged[cid]["matched_keywords"])
                existing_keywords.update(channel["matched_keywords"])
                merged[cid]["matched_keywords"] = sorted(existing_keywords)
            else:
                merged[cid] = channel

        if target_fresh is not None:
            fresh = len(set(merged) - exclude_ids)
            if fresh >= target_fresh:
                logger.info(
                    "Banked %d fresh candidate(s) (target %d) after %d keyword(s) — "
                    "skipping the remaining %d to save quota.",
                    fresh, target_fresh, position, len(keywords) - position,
                )
                break

        time.sleep(API_SLEEP_SECONDS)

    logger.info("Discovery complete: %d unique channels.", len(merged))
    return list(merged.values())
