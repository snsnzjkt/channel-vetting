"""
DO NOT CONTACT screening for the social path.

Shares the SAFETY-CRITICAL half with the YouTube path — the `Blocklist`
dataclass and its `match()`, so "does this creator appear on the list" is
decided by exactly one implementation — and replaces only the reading.

WHY THE READING HAD TO BE REPLACED. `do_not_contact.fetch_blocklist()` is
pinned to the Valencia base in three separate ways, and each fails differently
against another base:

  1. `DO_NOT_CONTACT_TABLE_ID = "tblHO0kJw0cBqV8Mw"` is hardcoded. Asking the
     Mythumi base for a Valencia table id returns 403 — this is exactly how the
     first live run failed.
  2. It requests `returnFieldsByFieldId: true` with Valencia FIELD IDs
     (`fldCExrqXONKfUxd5`...). Against another base those ids do not exist, so
     it would have indexed ZERO handles while reporting a successful fetch —
     the worse failure of the two, because it is silent.
  3. Zero rows raises `BlocklistUnavailable`. Right for a table with ~1,330
     rows, where a zero can only mean misconfiguration. Wrong for a new base,
     where an empty suppression list is the accurate state.

None of those are bugs there. All three are wrong here.

STILL FAIL-CLOSED on anything that is actually a failure: a request error, a
non-200, a malformed body, or a missing `records` key raises and the run stops.
Sourcing creators with no suppression list is the one failure in this pipeline
that can reach a person who asked to be left alone.
"""
import logging
import time

import requests

from channel_vetting import config
from channel_vetting.airtable.client import _base_url, _headers
from channel_vetting.airtable.do_not_contact import Blocklist, BlocklistUnavailable
from channel_vetting.config import API_SLEEP_SECONDS
from channel_vetting.core.http_client import AIRTABLE as HTTP, safe_body
from channel_vetting.social.handles import normalize_social_handle

logger = logging.getLogger(__name__)

# Field NAMES, not ids. The social tables are ours and their names are stable;
# ids would have to be rediscovered for every base this ever points at, which
# is the trap that made the Valencia reader unportable.
FIELD_NAME = "Creator Name"
FIELD_HANDLE = "Handle"
FIELD_URL = "Profile URL"
FIELD_EMAIL = "Email"


def fetch_social_blocklist(table_name: str | None = None) -> Blocklist:
    """
    The social suppression index, or raise BlocklistUnavailable.

    Indexes on all three keys the shared `match()` checks: handle (from the
    bare Handle column AND from a Profile URL, since either may be the only one
    filled), email, and name.
    """
    table = table_name or config.AIRTABLE_TABLE_SOCIAL_DNC
    if not table:
        raise BlocklistUnavailable(
            "AIRTABLE_TABLE_SOCIAL_DNC is not configured — refusing to source "
            "creators with no suppression list"
        )

    blocklist = Blocklist()
    offset = None
    row_count = 0

    while True:
        params = {
            "fields[]": [FIELD_NAME, FIELD_HANDLE, FIELD_URL, FIELD_EMAIL],
            "pageSize": 100,
        }
        if offset:
            params["offset"] = offset

        try:
            resp = HTTP.get(_base_url(table), headers=_headers(), params=params, timeout=30)
        except requests.RequestException as exc:
            raise BlocklistUnavailable(
                f"social DO NOT CONTACT fetch failed: {exc}"
            ) from exc

        if resp.status_code != 200:
            # safe_body withholds a 401/403 body so a pasted log cannot leak
            # auth detail.
            raise BlocklistUnavailable(
                f"social DO NOT CONTACT fetch failed: {resp.status_code} "
                f"{safe_body(resp)} (table {table!r})"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise BlocklistUnavailable(
                f"social DO NOT CONTACT fetch failed: body was not valid JSON: {exc}"
            ) from exc

        if not isinstance(data, dict) or "records" not in data:
            # A 200 without "records" must not be read as "zero rows on this
            # page" — that would let a partial index through as complete.
            raise BlocklistUnavailable(
                "social DO NOT CONTACT fetch failed: response had no 'records' key"
            )
        records = data["records"]
        if not isinstance(records, list):
            raise BlocklistUnavailable(
                f"social DO NOT CONTACT fetch failed: 'records' was "
                f"{type(records).__name__}, not a list"
            )

        row_count += len(records)
        for record in records:
            fields = record.get("fields", {}) if isinstance(record, dict) else {}
            if not isinstance(fields, dict):
                fields = {}

            # Both handle columns feed the same index: a row may carry only the
            # bare handle, only a profile URL, or both.
            for key in (FIELD_HANDLE, FIELD_URL):
                handle = normalize_social_handle(fields.get(key, "") or "")
                if handle:
                    blocklist.handles.add(handle)

            email = (fields.get(FIELD_EMAIL, "") or "").strip().lower()
            if email:
                blocklist.emails.add(email)

            name = (fields.get(FIELD_NAME, "") or "").strip().casefold()
            if name:
                blocklist.names.add(name)

        offset = data.get("offset")
        if not offset:
            break
        time.sleep(API_SLEEP_SECONDS)

    if row_count == 0:
        if config.SOCIAL_REQUIRE_NON_EMPTY_DNC:
            raise BlocklistUnavailable(
                f"social DO NOT CONTACT table {table!r} returned zero rows while "
                f"SOCIAL_REQUIRE_NON_EMPTY_DNC is on — treating as a failure, not "
                f"as an empty blocklist. Check the table name, the token scope and "
                f"the field names."
            )
        # Accurate on a new base, and said out loud rather than passed over in
        # silence: this is the one state where the screen protects nobody.
        logger.warning(
            "social DO NOT CONTACT table %r is EMPTY — no creator will be "
            "suppressed this run. Correct on a new base; set "
            "SOCIAL_REQUIRE_NON_EMPTY_DNC=true once the table has entries so a "
            "misconfiguration cannot look like this.",
            table,
        )
        return blocklist

    logger.info(
        "social DO NOT CONTACT index: %d rows -> %d handles, %d emails, %d names.",
        row_count, len(blocklist.handles), len(blocklist.emails), len(blocklist.names),
    )
    return blocklist
