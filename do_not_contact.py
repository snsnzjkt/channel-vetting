"""
Enforces the "DO NOT CONTACT" suppression list (Airtable table
tblHO0kJw0cBqV8Mw, ~498 rows).

Modeled on external_dedupe.py, which solves the same shape of problem —
an Airtable table keyed by @handle URL rather than Channel ID — but with
three deliberate differences, all because this is a suppression list
rather than a dedupe list:

1. FAILS CLOSED. external_dedupe logs errors and returns partial results,
   which is fine for dedupe (worst case: a known channel is re-added).
   Here the same behaviour would yield an empty set on an Airtable hiccup
   and read as "nobody is blocklisted". Every failure raises instead, and
   the caller must abort. An empty result is treated as a failure too:
   this table is never legitimately empty. This covers more than a bad
   status code: a 200 response missing "records" (fails a page rather
   than silently returning a partial index), a non-JSON 200 body (proxy
   or captive-portal interstitial), and a non-empty table that still
   produces zero indexed entries (e.g. returnFieldsByFieldId dropped, or
   a fld... field ID gone stale because a column was deleted and
   re-added rather than renamed) are all treated as fetch failures too.

2. NO CACHING. external_dedupe caches 24h against ~18k rows. This table
   is ~5 pages and takes seconds, so it is fetched fresh every run —
   somebody added to the blocklist this morning is honoured this
   afternoon. A stale suppression cache is exactly the failure this
   module exists to prevent.

3. THREE MATCH KEYS, MATCHED GENEROUSLY. Handle is the reliable key, but
   some rows carry only a name, and some carry an agency email shared
   across several channels. Error costs are asymmetric — a false positive
   costs one lead, a false negative is the harm being prevented — so all
   three are indexed and any hit blocks.

Fields are requested BY ID (returnFieldsByFieldId=true) rather than by
name. Field IDs survive a rename — that's the whole reason to read by
ID, since reading by name is what a rename would break. What reading by
ID does NOT survive is a delete-and-recreate: re-adding a deleted column
mints a brand-new field ID, silently going stale on this manually
maintained table. That's the residual risk the empty-index backstop
below exists to catch.
"""
import logging
import time
from dataclasses import dataclass, field

import requests

from airtable_client import _base_url, _headers
from config import API_SLEEP_SECONDS
from enrichment import normalize_handle

logger = logging.getLogger(__name__)

DO_NOT_CONTACT_TABLE_ID = "tblHO0kJw0cBqV8Mw"

# Verified field IDs. Every text field here is multilineText, so values
# can carry stray newlines and padding — always strip before indexing.
FIELD_NAME = "fldCExrqXONKfUxd5"
FIELD_URL = "fldBFsOvwaBkTN7yX"
FIELD_EMAIL = "fldA5r2RO4xZJ1Nbl"

# Instagram-platform rows contribute a NAME only (they have no @handle to
# extract), not a handle — they are still indexed via FIELD_NAME, and
# over-matching is the safe direction for a suppression list.


class BlocklistUnavailable(RuntimeError):
    """
    Raised when the blocklist cannot be established with confidence.

    Callers MUST abort rather than continue. Proceeding with a partial or
    empty blocklist means contacting people who asked not to be
    contacted.
    """


@dataclass
class Blocklist:
    handles: set[str] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)

    def match(self, handle: str = "", email: str = "", name: str = "") -> str:
        """
        Return a short description of which key matched, or "" for no
        match. Blank inputs never match, even though the index may
        contain blanks from empty cells.

        Handle normalization mirrors the indexing side: normalize_handle()
        extracts a bare handle from a full URL (it requires a literal "@"
        in the input, so it returns "" for an already-bare handle like
        "linustechtips"); the strip/lstrip("@")/lower fallback covers that
        bare case. Do not collapse this to normalize_handle() alone.
        """
        raw_handle = (handle or "").strip()
        h = normalize_handle(raw_handle) or raw_handle.lstrip("@").lower()
        if h and h in self.handles:
            return f"handle @{h}"

        e = (email or "").strip().lower()
        if e and e in self.emails:
            return f"email {e}"

        n = (name or "").strip().casefold()
        if n and n in self.names:
            return f"name '{name.strip()}'"

        return ""

    def __len__(self) -> int:
        return len(self.handles) + len(self.emails) + len(self.names)


def fetch_blocklist() -> Blocklist:
    """
    Build the blocklist index fresh from Airtable. Costs no YouTube quota.

    Raises BlocklistUnavailable on any request failure, non-200 response,
    or an empty result.
    """
    blocklist = Blocklist()
    offset = None
    row_count = 0
    unmatchable_row_count = 0  # rows that yielded neither a handle nor an email

    while True:
        params = {
            "fields[]": [FIELD_NAME, FIELD_URL, FIELD_EMAIL],
            "returnFieldsByFieldId": "true",
            "pageSize": 100,
        }
        if offset:
            params["offset"] = offset

        try:
            resp = requests.get(
                _base_url(DO_NOT_CONTACT_TABLE_ID), headers=_headers(), params=params, timeout=30
            )
        except requests.RequestException as e:
            raise BlocklistUnavailable(f"DO NOT CONTACT fetch failed: {e}") from e

        if resp.status_code != 200:
            raise BlocklistUnavailable(
                f"DO NOT CONTACT fetch failed: {resp.status_code} {resp.text}"
            )

        try:
            data = resp.json()
        except requests.RequestException as e:
            # requests.exceptions.JSONDecodeError (a RequestException
            # subclass) is raised for a 200 with a non-JSON body, e.g. a
            # proxy or captive-portal HTML page. Treat it the same as any
            # other fetch failure rather than letting it escape raw.
            raise BlocklistUnavailable(
                f"DO NOT CONTACT fetch failed: response body was not valid JSON: {e}"
            ) from e

        if not isinstance(data, dict):
            raise BlocklistUnavailable(
                f"DO NOT CONTACT fetch failed: expected a JSON object, got {type(data).__name__}"
            )

        if "records" not in data:
            # A 200 that omits "records" entirely (e.g. an unexpected API
            # shape change, or a truncated proxy response) must not be
            # silently treated as "this page had zero rows" — that would
            # let a partial index through as if it were complete.
            raise BlocklistUnavailable(
                "DO NOT CONTACT fetch failed: response body had no 'records' key"
            )

        records = data["records"]
        if not isinstance(records, list):
            raise BlocklistUnavailable(
                f"DO NOT CONTACT fetch failed: 'records' was {type(records).__name__}, not a list"
            )

        row_count += len(records)

        for record in records:
            fields = record.get("fields", {}) if isinstance(record, dict) else {}
            if not isinstance(fields, dict):
                fields = {}

            handle = normalize_handle(fields.get(FIELD_URL, "") or "")
            if handle:
                blocklist.handles.add(handle)

            email = (fields.get(FIELD_EMAIL, "") or "").strip().lower()
            if email:
                blocklist.emails.add(email)

            name = (fields.get(FIELD_NAME, "") or "").strip().casefold()
            if name:
                blocklist.names.add(name)

            if not handle and not email:
                unmatchable_row_count += 1

        offset = data.get("offset")
        if not offset:
            break
        time.sleep(API_SLEEP_SECONDS)

    if row_count == 0:
        raise BlocklistUnavailable(
            "DO NOT CONTACT table returned zero rows — treating as a failure, "
            "not as an empty blocklist. Check the table ID and token scope."
        )

    if len(blocklist) == 0:
        # Backstop for a non-empty table that still produces zero usable
        # entries -- e.g. returnFieldsByFieldId is dropped/typo'd, or a
        # fld... ID goes stale because someone deleted and re-added a
        # column in the manually maintained table (which mints a brand
        # new field ID; a plain rename would NOT break this, but a
        # delete-and-recreate does). Rows exist but nothing got indexed,
        # which is just as dangerous as an empty table: nobody would be
        # blocked.
        raise BlocklistUnavailable(
            f"DO NOT CONTACT table returned {row_count} row(s) but the index is "
            "empty -- check FIELD_NAME/FIELD_URL/FIELD_EMAIL and "
            "returnFieldsByFieldId. Refusing to return an empty blocklist."
        )

    if unmatchable_row_count:
        logger.warning(
            "DO NOT CONTACT: %d of %d rows (%.1f%%) yielded neither a handle nor "
            "an email -- those entries rely solely on name matching.",
            unmatchable_row_count, row_count, 100 * unmatchable_row_count / row_count,
        )

    logger.info(
        "DO NOT CONTACT index: %d rows -> %d handles, %d emails, %d names.",
        row_count, len(blocklist.handles), len(blocklist.emails), len(blocklist.names),
    )
    return blocklist
