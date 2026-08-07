"""
Cross-references our Channel Prospects tables against other YouTube-
channel-tracking tables that already exist elsewhere in the same
Airtable base (manually maintained outreach/leads/influencer tables),
so we don't track a channel that's already known there.

Those tables only store an @handle (via a channel URL like
"youtube.com/@Foo"), not a Channel ID, and there are ~18k such rows
across them — resolving all of them to real Channel IDs via the YouTube
API would cost ~18k quota units (nearly 2x the whole daily budget) just
for this. Flipped instead: we resolve OUR much smaller set of prospects'
own @handles (already free, piggybacking on the channels.list call we
make anyway) and check those against this module's handle index, which
is built via Airtable's own API — no YouTube quota involved at all.

The handle index is cached locally (EXTERNAL_HANDLES_CACHE_FILE) since
paginating ~18k Airtable records on every pipeline run would be slow and
wasteful; it's refreshed automatically once the cache is older than
EXTERNAL_CACHE_MAX_AGE_HOURS.
"""
import json
import logging
import os
import time

import requests

from airtable_client import _base_url, _headers
from config import API_SLEEP_SECONDS
from enrichment import normalize_handle

logger = logging.getLogger(__name__)

EXTERNAL_HANDLES_CACHE_FILE = "external_handles_cache.json"
EXTERNAL_CACHE_MAX_AGE_HOURS = 24

# The 4 tables elsewhere in the base that track YouTube channels (the
# other 5 tables in the base track Instagram/websites or are the two
# tables this pipeline itself owns — irrelevant to this check).
EXTERNAL_TABLES = [
    {"table_id": "tblFDvQiElfy7sER7", "name": "Home Theatre – YouTube Outreach", "link_field": "Link"},
    {"table_id": "tbllgU6ITa4vkI6dG", "name": "Home Theatre – YouTube Leads", "link_field": "Link"},
    {"table_id": "tblWJm5pRazEtBVqb", "name": "Home Theatre – YouTube Follow-up Outreach", "link_field": "Link"},
    {"table_id": "tbl9OOxhwR5ujGZtF", "name": "Lifestyle – Sofa Influencers", "link_field": "YouTube URL"},
]


def _fetch_table_handles(table_id: str, link_field: str) -> set[str]:
    handles: set[str] = set()
    offset = None

    while True:
        params = {"fields[]": link_field, "pageSize": 100}
        if offset:
            params["offset"] = offset

        try:
            resp = requests.get(_base_url(table_id), headers=_headers(), params=params, timeout=30)
        except requests.RequestException as e:
            logger.error("Airtable request failed while paginating %s: %s", table_id, e)
            break

        if resp.status_code != 200:
            logger.error("Airtable pagination failed for %s: %s %s", table_id, resp.status_code, resp.text)
            break

        data = resp.json()
        for record in data.get("records", []):
            raw = record.get("fields", {}).get(link_field, "")
            handle = normalize_handle(raw)
            if handle:
                handles.add(handle)

        offset = data.get("offset")
        if not offset:
            break
        time.sleep(API_SLEEP_SECONDS)

    return handles


def fetch_external_handles(force_refresh: bool = False) -> dict[str, str]:
    """
    Returns {normalized_handle: source_table_name} covering every handle
    found across EXTERNAL_TABLES. Uses a local cache unless it's missing,
    stale (> EXTERNAL_CACHE_MAX_AGE_HOURS old), or force_refresh=True.
    """
    if not force_refresh and os.path.exists(EXTERNAL_HANDLES_CACHE_FILE):
        try:
            with open(EXTERNAL_HANDLES_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            age_hours = (time.time() - cache.get("fetched_at", 0)) / 3600
            if age_hours < EXTERNAL_CACHE_MAX_AGE_HOURS:
                logger.info(
                    "Using cached external handle index (%.1fh old, %d handles).",
                    age_hours, len(cache.get("handles", {})),
                )
                return cache["handles"]
        except (json.JSONDecodeError, OSError):
            logger.warning("External handles cache was unreadable/corrupt; refreshing.")

    handles: dict[str, str] = {}
    for table in EXTERNAL_TABLES:
        table_handles = _fetch_table_handles(table["table_id"], table["link_field"])
        for h in table_handles:
            handles.setdefault(h, table["name"])
        logger.info("'%s': found %d handle(s).", table["name"], len(table_handles))
        time.sleep(API_SLEEP_SECONDS)

    with open(EXTERNAL_HANDLES_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": time.time(), "handles": handles}, f)

    logger.info("Built external handle index: %d unique handle(s) total.", len(handles))
    return handles
