"""
One-off: fill the "Handle" column on rows written before that column existed.

Why this is worth a script. Discovery's server-side `exclude_handles` takes
@handles, not channel IDs (see discovery/influencers_club.py). pipeline.py started
storing the handle on every new row in 2026-08, but the rows already in the
tables have an empty cell — so `airtable.client.get_tracked_handles()` returns
nothing for them and the vendor RE-RETURNS and RE-BILLS (0.01 each) every
already-tracked creator on every run, then costs a further YouTube unit each to
resolve just so the channel-ID dedupe can discard them. That waste is permanent
for those rows: it never self-heals, because a row already in the table is never
pushed again.

Cost of the fix: channels.list accepts up to 50 IDs in ONE 1-unit call, so the
whole backlog resolves for a couple of quota units regardless of table size.
That is why this batches rather than reusing enrichment.get_channel_stats(),
which is deliberately one-channel-at-a-time.

Rows whose Handle is already set are skipped, so this is safe to re-run. A
channel with no @handle at all (a legacy /c/ or /user/ channel that never set
one) is left blank and reported — there is nothing to store, and a blank is
what get_tracked_handles() already tolerates.

    python scripts/backfill/backfill_handles.py            # report only
    python scripts/backfill/backfill_handles.py --confirm  # actually write
"""
import argparse
import logging
import time

import requests

from channel_vetting.airtable.client import get_records, push_record, table_has_field
from channel_vetting.config import API_SLEEP_SECONDS, YOUTUBE_API_BASE_URL
from channel_vetting.enrichment.channels import normalize_handle
from channel_vetting.core.http_client import YOUTUBE as HTTP, safe_body
from channel_vetting.pipeline import NICHES
from channel_vetting.budget.quota_tracker import record_spend

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# channels.list caps `id` at 50 per request, and the call is a flat 1 unit
# however many are asked for — so the batch size IS the saving here.
CHANNELS_LIST_BATCH = 50


def resolve_handles(channel_ids: list[str]) -> dict[str, str]:
    """
    Map channel_id -> bare @handle for as many of `channel_ids` as resolve.

    Costs 1 quota unit per batch of CHANNELS_LIST_BATCH. Channels that are
    private, deleted, or that never set a handle are simply absent from the
    result; the caller reports them rather than writing an empty cell.
    """
    resolved: dict[str, str] = {}

    for start in range(0, len(channel_ids), CHANNELS_LIST_BATCH):
        batch = channel_ids[start:start + CHANNELS_LIST_BATCH]
        try:
            resp = HTTP.get(
                f"{YOUTUBE_API_BASE_URL}/channels",
                params={"part": "snippet", "id": ",".join(batch)},
                timeout=30,
            )
        except requests.RequestException as e:
            # Retries are already exhausted by here (see core/http_client.py), so
            # this batch is genuinely unreachable. Keep what resolved.
            logger.warning("channels.list request failed for a batch of %d: %s", len(batch), e)
            continue

        if resp.status_code != 200:
            logger.warning("channels.list failed: %s %s", resp.status_code, safe_body(resp))
            continue

        # Charged only for a call that returned data — the same rule
        # enrichment.get_channel_stats() follows.
        record_spend(1, call_name=f"channels.list(backfill x{len(batch)})")

        try:
            payload = resp.json()
        except ValueError:
            logger.warning("channels.list returned a non-JSON 200 — skipping this batch.")
            continue

        for item in payload.get("items", []):
            handle = normalize_handle(item.get("snippet", {}).get("customUrl", ""))
            if handle:
                resolved[item.get("id", "")] = handle

        time.sleep(API_SLEEP_SECONDS)

    return resolved


def backfill_table(niche_name: str, table_name: str, confirm: bool) -> tuple[int, int]:
    """Fill the Handle column for one niche table. Returns (written, unresolved)."""
    if not table_has_field(table_name, "Handle"):
        logger.error(
            "'%s' has no 'Handle' column — add it in Airtable first, or this "
            "would fail every write.", niche_name,
        )
        return 0, 0

    records = get_records(table_name, fields=["Channel ID", "Channel Name", "Handle"])
    missing = [
        r for r in records
        if r["fields"].get("Channel ID")
        and not str(r["fields"].get("Handle") or "").strip()
    ]
    print(f"\n[{niche_name}] {len(records)} row(s), {len(missing)} missing a handle.")
    if not missing:
        return 0, 0

    channel_ids = [r["fields"]["Channel ID"] for r in missing]
    resolved = resolve_handles(channel_ids)

    written = 0
    unresolved = 0
    for record in missing:
        fields = record["fields"]
        channel_id = fields["Channel ID"]
        name = (fields.get("Channel Name") or "")[:40]
        handle = resolved.get(channel_id)
        if not handle:
            print(f"  --  {name}: no @handle (legacy /c/ or /user/ channel, or gone)")
            unresolved += 1
            continue

        if not confirm:
            print(f"  ok  {name} -> @{handle}")
            written += 1
            continue

        # push_record PATCHes by Channel ID and strips Status/Notes on an
        # update, so an in-flight reviewer's workflow state is untouched.
        if push_record(table_name, {"Channel ID": channel_id, "Handle": handle}):
            print(f"  SET {name} -> @{handle}")
            written += 1
        else:
            print(f"  FAILED {name} — left blank")
        time.sleep(API_SLEEP_SECONDS)

    return written, unresolved


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fill the Handle column so discovery stops re-buying tracked rows"
    )
    parser.add_argument("--niche", default=None, help="Only this niche (default: all)")
    parser.add_argument(
        "--confirm", action="store_true",
        help="Actually write. Without this, report only.",
    )
    args = parser.parse_args()

    if args.niche and args.niche not in NICHES:
        parser.error(f"Unknown niche {args.niche!r}. Known: {', '.join(NICHES)}")
    niches = {args.niche: NICHES[args.niche]} if args.niche else dict(NICHES)

    print(f"Mode: {'WRITE' if args.confirm else 'REPORT-ONLY'}")

    total_written = 0
    total_unresolved = 0
    for niche_name, config in niches.items():
        written, unresolved = backfill_table(
            niche_name, config.get("table_name", ""), args.confirm
        )
        total_written += written
        total_unresolved += unresolved

    verb = "Wrote" if args.confirm else "Would write"
    print(f"\n{verb} {total_written} handle(s); {total_unresolved} unresolvable.")
    if not args.confirm and total_written:
        print("Re-run with --confirm to apply.")


if __name__ == "__main__":
    main()
