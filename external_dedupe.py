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
import re
import time
from dataclasses import dataclass, field

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
# tables this pipeline itself owns — irrelevant to this check). `name_field`
# is the channel-name column, indexed alongside the handle so a creator who
# RENAMED their @handle is still caught by name — the exact miss that let a
# channel already in "Follow-up Outreach" (old handle @Newrecordday2013) get
# re-added to Prospects after it became @newrecordday.
EXTERNAL_TABLES = [
    {"table_id": "tblFDvQiElfy7sER7", "name": "Home Theatre – YouTube Outreach", "link_field": "Link", "name_field": "Channel Name"},
    {"table_id": "tbllgU6ITa4vkI6dG", "name": "Home Theatre – YouTube Leads", "link_field": "Link", "name_field": "Channel Name"},
    {"table_id": "tblWJm5pRazEtBVqb", "name": "Home Theatre – YouTube Follow-up Outreach", "link_field": "Link", "name_field": "Channel Name"},
    {"table_id": "tbl9OOxhwR5ujGZtF", "name": "Lifestyle – Sofa Influencers", "link_field": "YouTube URL", "name_field": "Name"},
]


def _normalize_name(raw: str) -> str:
    """
    Fold a channel name for matching: strip, collapse internal whitespace,
    casefold. The external table's "Channel Name" and the pipeline's
    channels.list title are both the creator's raw display name, so this makes
    "New Record Day" match "new record  day" while staying blank-safe.
    """
    return re.sub(r"\s+", " ", (raw or "").strip()).casefold()


def _handle_key(handle: str) -> str:
    """
    Normalize a handle for index lookup. Prefers enrichment.normalize_handle
    (which understands a full channel URL), falling back to a bare
    strip/@-trim/lowercase for a plain handle string that isn't a URL. Both
    sites that key a handle go through this, so the two lookup paths
    (ExternalIndex.match and match_external's dict branch) can't drift apart.
    """
    return normalize_handle(handle) or (handle or "").strip().lstrip("@").lower()


@dataclass
class ExternalIndex:
    """
    What the base's other YouTube tables already track, keyed two ways.

    `handles` is the reliable key (an @handle can't be confused with another
    channel), so it is checked first. `names` is the fallback that survives a
    handle RENAME — its cost is that two different channels sharing a display
    name collide and one is skipped. For a DEDUPE list that fails open (worst
    case: a lead we skip, never a wrong contact) that trade is the right one,
    the same reason do_not_contact.Blocklist also matches on name.
    """

    handles: dict[str, str] = field(default_factory=dict)  # normalized handle -> source table
    names: dict[str, str] = field(default_factory=dict)    # normalized name -> source table

    def match(self, handle: str = "", name: str = "") -> str:
        """The source table a candidate is already tracked in, or ""."""
        h = _handle_key(handle)
        if h and h in self.handles:
            return self.handles[h]
        n = _normalize_name(name)
        if n and n in self.names:
            return self.names[n]
        return ""

    # Handle-dict read compatibility, so existing handle-only consumers
    # (cleanup_external_duplicates.py's `in`/`[]`/`len`, and the discovery
    # exclude set's `set(index)`) keep working unchanged — only match()'s
    # name awareness is new.
    def __contains__(self, handle: str) -> bool:
        return handle in self.handles

    def __getitem__(self, handle: str) -> str:
        return self.handles[handle]

    def __iter__(self):
        return iter(self.handles)

    def __len__(self) -> int:
        return len(self.handles)


def match_external(external, handle: str = "", name: str = "") -> str:
    """
    Which external table a candidate is already tracked in, or "".

    Accepts an ExternalIndex (matches @handle first, then channel name), or a
    plain {handle: table} dict — the pre-names on-disk cache format, and the
    shape lightweight callers/tests pass. A bare dict carries no names, so it
    is handle-only, which is exactly the old behaviour.
    """
    if isinstance(external, ExternalIndex):
        return external.match(handle=handle, name=name)
    h = _handle_key(handle)
    return external.get(h, "") if h else ""


def _fetch_table_entries(table_id: str, link_field: str, name_field: str) -> tuple[set[str], set[str]]:
    """Return (normalized handles, normalized names) for one external table."""
    handles: set[str] = set()
    names: set[str] = set()
    offset = None

    while True:
        params = {"fields[]": [link_field, name_field], "pageSize": 100}
        if offset:
            params["offset"] = offset

        try:
            resp = HTTP.get(_base_url(table_id), headers=_headers(), params=params, timeout=30)
        except requests.RequestException as e:
            # Log-and-return-partial is CORRECT here (unlike
            # do_not_contact.py, which must fail closed): a missing entry
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
            fields = record.get("fields", {})
            handle = normalize_handle(fields.get(link_field, "") or "")
            if handle:
                handles.add(handle)
            name = _normalize_name(fields.get(name_field, "") or "")
            if name:
                names.add(name)

        offset = data.get("offset")
        if not offset:
            break
        time.sleep(API_SLEEP_SECONDS)

    return handles, names


def fetch_external_handles(force_refresh: bool = False) -> ExternalIndex:
    """
    Return an ExternalIndex (handles + names) covering every channel tracked
    across EXTERNAL_TABLES. Uses a local cache unless it's missing, stale
    (> EXTERNAL_CACHE_MAX_AGE_HOURS old), or force_refresh=True.

    The name kept for backwards familiarity; it now returns the index, not a
    bare handle dict — see ExternalIndex for why a name key was added.
    """
    if not force_refresh and os.path.exists(EXTERNAL_HANDLES_CACHE_FILE):
        try:
            with open(EXTERNAL_HANDLES_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            age_hours = (time.time() - cache.get("fetched_at", 0)) / 3600
            if age_hours < EXTERNAL_CACHE_MAX_AGE_HOURS:
                # `names` defaults empty for a cache written before names were
                # indexed — it just refreshes into the fuller index next cycle.
                logger.info(
                    "Using cached external index (%.1fh old, %d handles, %d names).",
                    age_hours, len(cache.get("handles", {})), len(cache.get("names", {})),
                )
                # .get for BOTH keys: a cache missing either (truncated, hand-
                # edited, or written by another version) fails open to an empty
                # index — never a KeyError that would abort the run, which this
                # deliberately fail-open module must not do.
                return ExternalIndex(
                    handles=cache.get("handles", {}), names=cache.get("names", {})
                )
        except (json.JSONDecodeError, OSError):
            logger.warning("External index cache was unreadable/corrupt; refreshing.")

    handles: dict[str, str] = {}
    names: dict[str, str] = {}
    for table in EXTERNAL_TABLES:
        table_handles, table_names = _fetch_table_entries(
            table["table_id"], table["link_field"], table["name_field"]
        )
        for h in table_handles:
            handles.setdefault(h, table["name"])
        for n in table_names:
            names.setdefault(n, table["name"])
        logger.info(
            "'%s': found %d handle(s), %d name(s).", table["name"], len(table_handles), len(table_names)
        )
        time.sleep(API_SLEEP_SECONDS)

    _write_cache_atomically({"fetched_at": time.time(), "handles": handles, "names": names})

    logger.info(
        "Built external index: %d unique handle(s), %d unique name(s).", len(handles), len(names)
    )
    return ExternalIndex(handles=handles, names=names)


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
