"""
Batched writes for the follow-up categoriser.

WHY BATCHED. Airtable's rate limit is 5 requests/second PER BASE, shared with
human editors and the ten live automations. One PATCH per row over 11,490
handles is ~11,490 requests, i.e. ~38 minutes of saturating a limit other people
are trying to use. Airtable accepts 10 records per PATCH, so the same work is
~1,149 requests and about four minutes.

WHY typecast=False, ALWAYS. `airtable.client.push_record()` sends
`typecast=True`, and `outreach_store` documents what that costs on a select
field: it "silently CREATES a missing option", so one typo mints a fourteenth
`Follow-Up Category` and every page filtered on the correct thirteen quietly
stops showing that row. `config.py:96-99` records the measured version of the
same class of bug: a mistyped filter value returns HTTP 200 with ZERO rows and
no error at all. So this module refuses the value BEFORE the request, against
`categorizer.CATEGORIES`, and sends typecast=False so the API refuses it too.

WHY PER-RECORD OUTCOMES. A ten-record PATCH can fail as a unit. Reporting "1,149
requests sent" says nothing about whether 11,490 rows were written; the caller
needs to know which rows are still unwritten so it can retry them and so a run
that wrote nothing exits non-zero instead of reporting green — the "zero rows,
exit code 0" hole `docs/TODOS.md` already names as the repo's next-best
observability fix.
"""
import logging

import requests

from channel_vetting.airtable.client import _base_url, _headers
from channel_vetting.config import API_SLEEP_SECONDS
from channel_vetting.core.http_client import AIRTABLE as HTTP, safe_body
from channel_vetting.followup.categorizer import CATEGORIES

logger = logging.getLogger(__name__)

# Airtable's documented maximum records per PATCH.
BATCH_SIZE = 10


class CategoryVocabularyError(ValueError):
    """
    A category value outside `categorizer.CATEGORIES` reached the writer.

    Raised rather than logged: the whole point of the single vocabulary is that
    the writer and the page filters cannot drift, and a value that is not in the
    tuple would either 422 at the API (with typecast=False) or, far worse, mint a
    new option if anyone ever relaxed that flag.
    """


def validate_categories(updates: dict[str, dict]) -> None:
    """
    Preflight every category value before a single request goes out.

    Cheap, and it turns a partially-written table into a refusal. Checking after
    the first batch fails would leave rows 1-10 written and the rest not.
    """
    bad = {
        rec: f["Follow-Up Category"]
        for rec, f in updates.items()
        if "Follow-Up Category" in f and f["Follow-Up Category"] not in CATEGORIES
    }
    if bad:
        raise CategoryVocabularyError(
            f"{len(bad)} record(s) carry a category outside CATEGORIES: "
            f"{sorted(set(bad.values()))}. Add it to categorizer.CATEGORIES and "
            "to the Airtable single-select, or fix the caller."
        )


def patch_records(table_name: str, updates: dict[str, dict]) -> tuple[int, list[str]]:
    """
    PATCH `{record_id: {field_name: value}}` in batches. Returns
    (written, failed_record_ids).

    Never raises for a failed batch — it records which records did not land and
    lets the caller decide, because a single bad batch must not throw away the
    ten thousand rows that already succeeded. It DOES raise on a bad category
    value, before any request, via validate_categories().
    """
    validate_categories(updates)

    items = [{"id": rid, "fields": fields} for rid, fields in updates.items()]
    written, failed = 0, []

    for i in range(0, len(items), BATCH_SIZE):
        chunk = items[i:i + BATCH_SIZE]
        payload = {"records": chunk, "typecast": False}
        try:
            resp = HTTP.patch(_base_url(table_name), headers=_headers(),
                              json=payload, timeout=30)
        except requests.RequestException as e:
            logger.warning("PATCH batch %d failed: %s", i // BATCH_SIZE, e)
            failed.extend(r["id"] for r in chunk)
            continue

        if resp.status_code != 200:
            logger.warning("PATCH batch %d failed: %s %s",
                           i // BATCH_SIZE, resp.status_code, safe_body(resp))
            failed.extend(r["id"] for r in chunk)
            continue

        written += len(chunk)
        # Pace against the 5 req/s per-base limit that human editors and the
        # live send automations are also spending.
        import time
        time.sleep(API_SLEEP_SECONDS)

    return written, failed
