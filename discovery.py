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

import requests

from config import (
    YOUTUBE_API_BASE_URL,
    YOUTUBE_API_KEY,
    SEARCH_CACHE_FILE,
    QUOTA_COST_SEARCH_LIST,
    API_SLEEP_SECONDS,
)
from quota_tracker import can_afford_search, record_spend

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
    with open(SEARCH_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def _cache_key(keyword: str) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
    Cached per keyword per UTC day so re-running the same keyword on the
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
    # (quota ceiling or a non-200 response). Only a clean completion gets
    # cached — caching a truncated/empty result from a transient error
    # would poison this keyword's cache for the rest of the UTC day (see
    # _cache_key()), silently killing its discovery with no error signal.
    complete = True

    while results_collected < max_results:
        if not can_afford_search():
            logger.warning("Quota ceiling reached — stopping discovery for keyword '%s'.", keyword)
            complete = False
            break

        params = {
            "part": "snippet",
            "q": keyword,
            "type": "video",
            "order": "relevance",
            "maxResults": min(50, max_results - results_collected),
            "key": YOUTUBE_API_KEY,
        }
        if published_after:
            params["publishedAfter"] = published_after
        if page_token:
            params["pageToken"] = page_token

        resp = requests.get(f"{YOUTUBE_API_BASE_URL}/search", params=params, timeout=30)
        record_spend(QUOTA_COST_SEARCH_LIST, call_name=f"search.list('{keyword}')")

        if resp.status_code != 200:
            logger.error("search.list failed for '%s': %s %s", keyword, resp.status_code, resp.text)
            complete = False
            break

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
