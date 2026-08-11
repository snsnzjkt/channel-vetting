"""
One-off audit: cross-check rows ALREADY in the niche tables against the
DO NOT CONTACT list.

The pipeline's blocklist screening only protects future runs. Rows added
before that screening existed have never been checked, and those are the
ones most likely to be contacted first.

Read-only by default. Costs no YouTube quota.

    python audit_blocklist.py            # report only
    python audit_blocklist.py --mark     # also set Status to "Do Not Contact"
"""
import argparse
import logging
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# `requests` stays imported for its exception types (RequestException in
# _status_option_exists); requests themselves go through the shared session.
import requests

from airtable_client import AIRTABLE_API_BASE_URL, _base_url, _headers, push_record
from http_client import AIRTABLE as HTTP, safe_body
from config import AIRTABLE_BASE_ID, AIRTABLE_TABLE_HOME_THEATER, AIRTABLE_TABLE_LIFESTYLE_SOFA, API_SLEEP_SECONDS
from do_not_contact import BlocklistUnavailable, fetch_blocklist
from enrichment import normalize_handle

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TABLES = {
    "Home Theater": AIRTABLE_TABLE_HOME_THEATER,
    "Lifestyle Sofa": AIRTABLE_TABLE_LIFESTYLE_SOFA,
}

# The "Status" value this script writes when marking a blocklisted row.
# It must already exist as a Single Select option on the live "Status"
# field — that field currently only has New/Reviewing/Approved/Rejected/
# Contacted. push_record's typecast=True would otherwise let Airtable
# silently CREATE this as a sixth option; the taxonomy is the human's
# call, not this script's, so _status_option_exists() below gates every
# --mark write on confirming it already exists (or an explicit opt-in).
MARK_STATUS = "Do Not Contact"

STATUS_FIELD_NAME = "Status"


def _all_records(table_name: str) -> list[dict]:
    records, offset = [], None
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        # Via the shared session, so a 429 from a colleague hammering the
        # base mid-audit is retried rather than aborting the whole audit
        # through raise_for_status().
        resp = HTTP.get(_base_url(table_name), headers=_headers(), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            return records
        # The only paginating function in the codebase that was missing
        # this — every other one in airtable_client.py/external_dedupe.py
        # sleeps between pages to stay a good API citizen.
        time.sleep(API_SLEEP_SECONDS)


def _status_option_exists(table_name: str, records: list[dict]) -> bool | None:
    """
    Best-effort preflight: does the "Status" single-select on `table_name`
    already have a MARK_STATUS option?

    Tries the schema (meta) API first — the authoritative source. Falls
    back to scanning `records` (already fetched by the caller) for an
    existing value equal to MARK_STATUS if the schema read isn't usable —
    this repo's AIRTABLE_TOKEN is known to lack schema-read scope (403 on
    the meta API).

    Returns:
        True  — confirmed the option already exists (safe to mark).
        False — confirmed the "Status" field exists and does NOT have
                this option (must not mark; the human needs to create it).
        None  — could not confirm either way (no schema access, and no
                record currently carries this value — that only proves
                it's unused, not that the option doesn't exist). Callers
                must require an explicit opt-in before writing.
    """
    try:
        resp = HTTP.get(
            f"{AIRTABLE_API_BASE_URL}/meta/bases/{AIRTABLE_BASE_ID}/tables",
            headers=_headers(),
            timeout=30,
        )
    except requests.RequestException as e:
        logger.warning("Schema read failed (%s) — falling back to scanning existing Status values.", e)
        resp = None

    if resp is not None and resp.status_code == 200:
        for table in resp.json().get("tables", []):
            if table.get("id") == table_name or table.get("name") == table_name:
                for field in table.get("fields", []):
                    if field.get("name") == STATUS_FIELD_NAME:
                        choices = field.get("options", {}).get("choices", [])
                        return any(choice.get("name") == MARK_STATUS for choice in choices)
                return None  # table found, but no "Status" field — can't confirm
        return None  # table not present in the schema response
    elif resp is not None:
        # safe_body() withholds 401/403 bodies outright, which is exactly
        # the status this path expects (this repo's token lacks schema
        # scope), and truncates anything else.
        logger.warning(
            "Schema read failed (%s %s) — falling back to scanning existing Status values.",
            resp.status_code, safe_body(resp),
        )

    existing_statuses = {r.get("fields", {}).get(STATUS_FIELD_NAME) for r in records}
    if MARK_STATUS in existing_statuses:
        return True
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit existing rows against DO NOT CONTACT")
    parser.add_argument("--mark", action="store_true", help=f'Set Status to "{MARK_STATUS}" on hits')
    parser.add_argument(
        "--yes-create-status-option",
        action="store_true",
        help=(
            f'Proceed with --mark even though this script could not confirm the "{MARK_STATUS}" '
            'Status option already exists in Airtable (the schema-read API is unavailable to '
            "this token). With typecast enabled, Airtable will silently CREATE this as a new "
            "Status option if it doesn't already exist. Only pass this after confirming with a "
            "human that the option exists, or should be created."
        ),
    )
    args = parser.parse_args()

    try:
        blocklist = fetch_blocklist()
    except BlocklistUnavailable as e:
        logger.error("ABORTING: %s", e)
        raise SystemExit(1)

    total_hits = 0
    for niche_name, table_name in TABLES.items():
        if not table_name:
            continue

        records = _all_records(table_name)

        if args.mark:
            status_ok = _status_option_exists(table_name, records)
            if status_ok is False:
                print(
                    f'ABORTING: the "Status" field on "{niche_name}" does not have a '
                    f'"{MARK_STATUS}" option yet. Create it in Airtable first, then re-run '
                    "with --mark."
                )
                raise SystemExit(1)
            if status_ok is None and not args.yes_create_status_option:
                print(
                    f'WARNING: could not confirm the "Status" field on "{niche_name}" already has '
                    f'a "{MARK_STATUS}" option (this token cannot read the base schema, and no '
                    "existing row currently carries that value). Proceeding would let Airtable's "
                    "typecast silently CREATE a new Status option. Create the option in Airtable "
                    "yourself and re-run, or pass --yes-create-status-option to proceed anyway."
                )
                raise SystemExit(1)

        for record in records:
            fields = record.get("fields", {})
            hit = blocklist.match(
                handle=normalize_handle(fields.get("Channel URL", "")),
                email=fields.get("Email", ""),
                name=fields.get("Channel Name", ""),
            )
            if not hit:
                continue
            total_hits += 1
            print(f"[{niche_name}] BLOCKLISTED: {fields.get('Channel Name')} ({hit})")
            if args.mark:
                channel_id = fields.get("Channel ID")
                if not channel_id:
                    print(f"[{niche_name}] SKIPPED marking (no Channel ID): {fields.get('Channel Name')}")
                    continue
                # overwrite_status_and_notes=True: this script's whole
                # purpose is to change Status on an already-existing
                # record. Only Channel ID + Status are sent, so Notes
                # (and everything else) survives untouched — Airtable's
                # PATCH only ever changes the fields you actually send.
                push_record(
                    table_name,
                    {"Channel ID": channel_id, "Status": MARK_STATUS},
                    overwrite_status_and_notes=True,
                )

    print(f"\n{total_hits} blocklisted row(s) found across {len(TABLES)} table(s).")
    if total_hits and not args.mark:
        print("Re-run with --mark to flag them, after reviewing the list above.")


if __name__ == "__main__":
    main()
