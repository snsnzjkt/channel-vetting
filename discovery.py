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

    while results_collected < max_results:
        if not can_afford_search():
            logger.warning("Quota ceiling reached — stopping discovery for keyword '%s'.", keyword)
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
    cache[key] = result
    _save_cache(cache)
    return result


def run_discovery(keywords: list[str], max_results_per_keyword: int = 50, days_back: int = 90) -> list[dict]:
    """
    Run discover_channels_by_keyword() across all keywords, dedupe channels
    across searches, and merge matched_keywords for channels that hit
    multiple searches.
    """
    merged: dict[str, dict] = {}

    for keyword in keywords:
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

        time.sleep(API_SLEEP_SECONDS)

    logger.info("Discovery complete: %d unique channels across %d keywords.", len(merged), len(keywords))
    return list(merged.values())
