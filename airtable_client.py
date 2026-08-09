"""
Airtable REST API integration: dedupe checks and create/update records.

Every function takes an explicit `table_name` (Airtable table name or
table ID) rather than reading a single global table, since the pipeline
writes to one table per niche (see NICHES in main.py).
"""
import logging
import time
from urllib.parse import quote

import requests

from config import AIRTABLE_TOKEN, AIRTABLE_BASE_ID, API_SLEEP_SECONDS
from prospect_day import today_iso

logger = logging.getLogger(__name__)

AIRTABLE_API_BASE_URL = "https://api.airtable.com/v0"


class AirtableReadError(RuntimeError):
    """
    Raised when a read that a safety decision depends on cannot be
    completed.

    Deliberately breaks this module's usual log-and-return-falsy
    convention. That convention exists so one bad record can't kill a
    run; it is wrong for count_added_today(), where a silent empty result
    reads as "nothing added today" and hands out a full daily budget —
    failing open in the one direction that overspends.
    """


def _base_url(table_name: str) -> str:
    # URL-encode the table name/ID so table names containing spaces or
    # other special characters don't produce a malformed request URL.
    return f"{AIRTABLE_API_BASE_URL}/{AIRTABLE_BASE_ID}/{quote(table_name, safe='')}"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }


def get_existing_channel_ids(table_name: str) -> set[str]:
    """
    Paginate through `table_name` and collect every existing "Channel ID"
    value, so callers can pre-filter discovered candidates before spending
    YouTube quota enriching channels already tracked in that niche's table.

    Raises AirtableReadError if the read cannot be completed — this set is
    the pipeline's only pre-filter AND its only `exclude_ids` source for
    discovery, so a silent partial result (e.g. a 429 on page 7 of 14)
    would make hundreds of already-tracked channels look "fresh". Those
    then get re-enriched and re-pushed via push_record's PATCH path, which
    would revert a reviewer's Status, wipe their Notes, and stamp a new
    Date Added — see IMPORTANT 2 in the fix-wave review. Failing loudly
    here (like count_added_today()) lets the caller abort instead of
    quietly operating on a partial set.
    """
    existing_ids: set[str] = set()
    offset = None

    while True:
        params = {"fields[]": "Channel ID", "pageSize": 100}
        if offset:
            params["offset"] = offset

        try:
            resp = requests.get(_base_url(table_name), headers=_headers(), params=params, timeout=30)
        except requests.RequestException as e:
            raise AirtableReadError(f"get_existing_channel_ids({table_name}) request failed: {e}") from e

        if resp.status_code != 200:
            raise AirtableReadError(
                f"get_existing_channel_ids({table_name}) failed: {resp.status_code} {resp.text}"
            )

        data = resp.json()
        for record in data.get("records", []):
            channel_id = record.get("fields", {}).get("Channel ID")
            if channel_id:
                existing_ids.add(channel_id)

        offset = data.get("offset")
        if not offset:
            break
        time.sleep(API_SLEEP_SECONDS)

    logger.info("Fetched %d existing channel IDs from Airtable table '%s'.", len(existing_ids), table_name)
    return existing_ids


def get_records_missing_email(table_name: str) -> list[str]:
    """
    Paginate through `table_name` and collect the Channel ID of every
    record that has a Channel ID but no Email value yet — candidates for
    a backfill pass (re-enrich + re-run the email fallback chain) without
    re-running discovery.
    """
    channel_ids: list[str] = []
    offset = None
    formula = "AND({Channel ID} != '', {Email} = '')"

    while True:
        params = {"fields[]": "Channel ID", "filterByFormula": formula, "pageSize": 100}
        if offset:
            params["offset"] = offset

        try:
            resp = requests.get(_base_url(table_name), headers=_headers(), params=params, timeout=30)
        except requests.RequestException as e:
            logger.error("Airtable request failed while paginating records missing email (%s): %s", table_name, e)
            break

        if resp.status_code != 200:
            logger.error("Airtable get_records_missing_email failed (%s): %s %s", table_name, resp.status_code, resp.text)
            break

        data = resp.json()
        for record in data.get("records", []):
            channel_id = record.get("fields", {}).get("Channel ID")
            if channel_id:
                channel_ids.append(channel_id)

        offset = data.get("offset")
        if not offset:
            break
        time.sleep(API_SLEEP_SECONDS)

    logger.info("Found %d record(s) missing an email in table '%s'.", len(channel_ids), table_name)
    return channel_ids


def channel_exists(table_name: str, channel_id: str) -> str | None:
    """
    Look up a single channel by Channel ID via filterByFormula.
    Returns the Airtable record ID if found, else None.
    """
    formula = f"{{Channel ID}} = '{channel_id}'"
    params = {"filterByFormula": formula, "maxRecords": 1}

    try:
        resp = requests.get(_base_url(table_name), headers=_headers(), params=params, timeout=30)
    except requests.RequestException as e:
        logger.error("Airtable request failed during channel_exists(%s, %s): %s", table_name, channel_id, e)
        return None

    if resp.status_code != 200:
        logger.error("Airtable channel_exists failed for %s in %s: %s %s", channel_id, table_name, resp.status_code, resp.text)
        return None

    records = resp.json().get("records", [])
    return records[0]["id"] if records else None


def delete_record(table_name: str, record_id: str) -> bool:
    """
    Permanently delete a single record from `table_name`. No undo on our
    end — callers must be certain before calling this.
    """
    try:
        resp = requests.delete(f"{_base_url(table_name)}/{record_id}", headers=_headers(), timeout=30)
    except requests.RequestException as e:
        logger.error("Airtable request failed while deleting %s from %s: %s", record_id, table_name, e)
        return False

    if resp.status_code != 200:
        logger.error("Airtable delete_record failed for %s in %s: %s %s", record_id, table_name, resp.status_code, resp.text)
        return False

    return True


# Fields that must never be silently clobbered by a PATCH to an existing
# record. "Status" is a human reviewer's workflow state (New/Reviewing/
# Approved/Rejected/Contacted); "Notes" is their free-text write-up. Both
# are only ever meant to be set by main.py at CREATE time (fresh
# defaults) or by a caller that deliberately opts in via
# `overwrite_status_and_notes=True` (e.g. audit_blocklist.py's --mark,
# which exists specifically to change Status).
PROTECTED_UPDATE_FIELDS = ("Status", "Notes")


def push_record(table_name: str, record: dict, overwrite_status_and_notes: bool = False) -> bool:
    """
    Create or update a row in `table_name`. Looks up the channel by
    "Channel ID" (record["Channel ID"] must be set); PATCHes the existing
    row if found, otherwise POSTs a new one. Never creates duplicates.

    On an UPDATE (the channel already exists), "Status" and "Notes" are
    stripped from the outgoing payload unless `overwrite_status_and_notes`
    is True. This matters because `get_existing_channel_ids()`'s pre-filter
    is the only thing that normally keeps already-tracked channels from
    reaching this function at all; if that pre-filter ever returns a
    partial set (see its docstring), an already-tracked channel can be
    "rediscovered" and re-pushed with fresh defaults
    (Status=DEFAULT_STATUS, Notes=""), silently reverting a reviewer's
    workflow state and erasing their notes. On a CREATE (no existing
    record), both fields are sent as-is — there is nothing to preserve.

    Returns True on success, False on failure (errors are logged, not
    raised, so a single bad record doesn't crash the whole run).
    """
    channel_id = record.get("Channel ID")
    if not channel_id:
        logger.error("push_record called without a Channel ID — skipping: %s", record)
        return False

    existing_record_id = channel_exists(table_name, channel_id)

    fields = record
    if existing_record_id and not overwrite_status_and_notes:
        fields = {k: v for k, v in record.items() if k not in PROTECTED_UPDATE_FIELDS}

    # typecast=True lets Airtable auto-create missing Single/Multiple Select
    # options (e.g. a new "Content Language" country code we haven't seen
    # before) instead of rejecting the write with INVALID_MULTIPLE_CHOICE_OPTIONS.
    payload = {"fields": fields, "typecast": True}

    try:
        if existing_record_id:
            resp = requests.patch(
                f"{_base_url(table_name)}/{existing_record_id}",
                headers=_headers(),
                json=payload,
                timeout=30,
            )
        else:
            resp = requests.post(
                _base_url(table_name),
                headers=_headers(),
                json=payload,
                timeout=30,
            )
    except requests.RequestException as e:
        logger.error("Airtable request failed while pushing record for %s to %s: %s", channel_id, table_name, e)
        return False

    if resp.status_code not in (200, 201):
        logger.error("Airtable push_record failed for %s in %s: %s %s", channel_id, table_name, resp.status_code, resp.text)
        return False

    return True


def count_added_today(table_name: str, qualification: str | None = None) -> int:
    """
    Count records in `table_name` whose "Date Added" is today, optionally
    narrowed to a single "Qualification" value.

    Filters server-side, so this returns at most the day's own records
    (~60) rather than paginating the whole table. Costs no YouTube quota.

    Raises AirtableReadError if the read cannot be completed — callers
    must skip the niche rather than assume a full budget.
    """
    conditions = [f"DATESTR({{Date Added}}) = '{today_iso()}'"]
    if qualification:
        conditions.append(f"{{Qualification}} = '{qualification}'")
    formula = f"AND({', '.join(conditions)})" if len(conditions) > 1 else conditions[0]

    count = 0
    offset = None
    while True:
        params = {"fields[]": "Channel ID", "filterByFormula": formula, "pageSize": 100}
        if offset:
            params["offset"] = offset

        try:
            resp = requests.get(_base_url(table_name), headers=_headers(), params=params, timeout=30)
        except requests.RequestException as e:
            raise AirtableReadError(f"count_added_today({table_name}) request failed: {e}") from e

        if resp.status_code != 200:
            raise AirtableReadError(
                f"count_added_today({table_name}) failed: {resp.status_code} {resp.text}"
            )

        data = resp.json()
        count += len(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
        time.sleep(API_SLEEP_SECONDS)

    return count
