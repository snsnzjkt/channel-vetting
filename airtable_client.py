"""
Airtable REST API integration: dedupe checks and create/update records
in the "Channel Prospects" table.
"""
import logging
import time
from urllib.parse import quote

import requests

from config import AIRTABLE_TOKEN, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME, API_SLEEP_SECONDS

logger = logging.getLogger(__name__)

AIRTABLE_API_BASE_URL = "https://api.airtable.com/v0"


def _base_url() -> str:
    # URL-encode the table name/ID so table names containing spaces (e.g.
    # the default "Channel Prospects") or other special characters don't
    # produce a malformed request URL.
    return f"{AIRTABLE_API_BASE_URL}/{AIRTABLE_BASE_ID}/{quote(AIRTABLE_TABLE_NAME, safe='')}"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }


def get_existing_channel_ids() -> set[str]:
    """
    Paginate through the entire table and collect every existing
    "Channel ID" value, so callers can pre-filter discovered candidates
    before spending YouTube quota enriching channels already tracked.
    """
    existing_ids: set[str] = set()
    offset = None

    while True:
        params = {"fields[]": "Channel ID", "pageSize": 100}
        if offset:
            params["offset"] = offset

        try:
            resp = requests.get(_base_url(), headers=_headers(), params=params, timeout=30)
        except requests.RequestException as e:
            logger.error("Airtable request failed while paginating existing channel IDs: %s", e)
            break

        if resp.status_code != 200:
            logger.error("Airtable get_existing_channel_ids failed: %s %s", resp.status_code, resp.text)
            break

        data = resp.json()
        for record in data.get("records", []):
            channel_id = record.get("fields", {}).get("Channel ID")
            if channel_id:
                existing_ids.add(channel_id)

        offset = data.get("offset")
        if not offset:
            break
        time.sleep(API_SLEEP_SECONDS)

    logger.info("Fetched %d existing channel IDs from Airtable.", len(existing_ids))
    return existing_ids


def channel_exists(channel_id: str) -> str | None:
    """
    Look up a single channel by Channel ID via filterByFormula.
    Returns the Airtable record ID if found, else None.
    """
    formula = f"{{Channel ID}} = '{channel_id}'"
    params = {"filterByFormula": formula, "maxRecords": 1}

    try:
        resp = requests.get(_base_url(), headers=_headers(), params=params, timeout=30)
    except requests.RequestException as e:
        logger.error("Airtable request failed during channel_exists(%s): %s", channel_id, e)
        return None

    if resp.status_code != 200:
        logger.error("Airtable channel_exists failed for %s: %s %s", channel_id, resp.status_code, resp.text)
        return None

    records = resp.json().get("records", [])
    return records[0]["id"] if records else None


def push_record(record: dict) -> bool:
    """
    Create or update a Channel Prospects row. Looks up the channel by
    "Channel ID" (record["Channel ID"] must be set); PATCHes the existing
    row if found, otherwise POSTs a new one. Never creates duplicates.

    Returns True on success, False on failure (errors are logged, not
    raised, so a single bad record doesn't crash the whole run).
    """
    channel_id = record.get("Channel ID")
    if not channel_id:
        logger.error("push_record called without a Channel ID — skipping: %s", record)
        return False

    existing_record_id = channel_exists(channel_id)

    # typecast=True lets Airtable auto-create missing Single/Multiple Select
    # options (e.g. a new "Content Language" country code we haven't seen
    # before) instead of rejecting the write with INVALID_MULTIPLE_CHOICE_OPTIONS.
    payload = {"fields": record, "typecast": True}

    try:
        if existing_record_id:
            resp = requests.patch(
                f"{_base_url()}/{existing_record_id}",
                headers=_headers(),
                json=payload,
                timeout=30,
            )
        else:
            resp = requests.post(
                _base_url(),
                headers=_headers(),
                json=payload,
                timeout=30,
            )
    except requests.RequestException as e:
        logger.error("Airtable request failed while pushing record for %s: %s", channel_id, e)
        return False

    if resp.status_code not in (200, 201):
        logger.error("Airtable push_record failed for %s: %s %s", channel_id, resp.status_code, resp.text)
        return False

    return True
