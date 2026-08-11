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
EXTERNAL_CACHE_MAX_AGE_HOURS. That cache is replaced ATOMICALLY (see
_write_cache_atomically) — a plain open(..., "w") truncates first, so an
interrupted refresh used to leave a corrupt file behind and throw away a
still-good index.

Unlike do_not_contact.py, read failures here LOG AND RETURN PARTIAL
results on purpose. This is a dedupe list: the worst case of a missing
handle is re-adding a channel that is already tracked elsewhere, which a
human can spot. Failing the run closed over that would be a much worse
trade. Don't "harden" this into raising.
"""
import json
import logging
import os
import time

# `requests` stays imported for its exception types (RequestException
# below); the requests themselves go through the shared retrying session.
import requests

from airtable_client import _base_url, _headers
from config import API_SLEEP_SECONDS
from enrichment import normalize_handle
from http_client import AIRTABLE as HTTP, safe_body

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
            resp = HTTP.get(_base_url(table_id), headers=_headers(), params=params, timeout=30)
        except requests.RequestException as e:
            # Log-and-return-partial is CORRECT here (unlike
            # do_not_contact.py, which must fail closed): a missing handle
            # only risks re-adding a channel someone already tracks. The
            # shared session has already retried transient 429/5xx, so a
            # partial index now means a persistent problem, not a blip.
            logger.error("Airtable request failed while paginating %s: %s", table_id, e)
            break

        if resp.status_code != 200:
            logger.error(
                "Airtable pagination failed for %s: %s %s", table_id, resp.status_code, safe_body(resp)
            )
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

    _write_cache_atomically({"fetched_at": time.time(), "handles": handles})

    logger.info("Built external handle index: %d unique handle(s) total.", len(handles))
    return handles


def _write_cache_atomically(payload: dict) -> None:
    """
    Replace the cache file in one step: write a .tmp sibling, fsync it,
    then os.replace() it over the real path.

    Why not just open(..., "w"): that TRUNCATES the existing file before a
    single byte of the new content is written. This function paginates
    ~18k rows across four tables and can run for a while, so the window
    between truncate and complete write is real — a Ctrl-C, a killed CI
    job, or a crash mid-`json.dump` leaves a zero-length or half-written
    JSON file on disk. Reading that back is survivable (the loader catches
    JSONDecodeError and refetches), but it silently throws away a
    perfectly good 24h cache and re-spends thousands of Airtable requests
    on the next run.

    os.replace() is atomic on both POSIX and Windows (unlike os.rename(),
    which fails on Windows when the destination exists), so a reader
    either sees the whole old file or the whole new one — never a partial.
    The fsync() before it is what makes that hold across a power loss
    rather than only across a process death: without it the rename can
    reach disk before the data it points at.

    The .tmp sibling lives in the SAME directory on purpose — os.replace()
    is only atomic within a filesystem, so a temp file in the system temp
    dir could land on a different volume and silently degrade to a copy.
    """
    tmp_path = f"{EXTERNAL_HANDLES_CACHE_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
            # flush() moves bytes out of Python's buffer into the OS;
            # fsync() forces the OS to commit them to the device. Both are
            # needed — flush alone still leaves the data only in page cache.
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, EXTERNAL_HANDLES_CACHE_FILE)
    except BaseException:
        # BaseException, not Exception: KeyboardInterrupt is the single
        # most likely way this gets interrupted on a long local run, and
        # it is not an Exception. Whatever went wrong, the real cache file
        # is untouched (it was never opened for writing) — all that is
        # needed is to not leave a stray .tmp behind to accumulate or to
        # confuse the next run. Best-effort: if even the unlink fails
        # there is nothing useful left to do, so swallow that and re-raise
        # the original failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
