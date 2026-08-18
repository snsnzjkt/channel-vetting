"""
One-off maintenance script: re-check rows already in a niche table against the
CURRENT criteria, and report (or delete) the ones that no longer fit.

Why this exists. Rows were written under criteria that have since changed, and
under one rule that was outright wrong: until 2026-08-14 the per-video view
floor judged SHORTS alongside long-form uploads, so it both admitted channels
it shouldn't and rejected ones it should have kept. The stored Airtable fields
cannot answer the current rule — "70% of recent LONG-FORM videos cleared
10,000 views" needs per-video counts and per-video durations, and the table
holds only an average. So this re-enriches from the YouTube Data API rather
than reasoning from what's already in the row.

Cost: ~3 quota units per row (channels.list + playlistItems + videos.list),
the same as one candidate in a normal run. A 34-row table is ~100 units.

REPORT-ONLY BY DEFAULT. Deleting Airtable rows is irreversible on our end, so
the default run writes nothing and just prints the verdicts. Pass --confirm to
actually delete, and note the two guards that still apply on top of it:

  - Only rows whose Status is in DELETABLE_STATUSES ("New") are ever touched.
    A row a human has moved to Contacted/Approved/Rejected represents work or a
    judgement call, and this script must not undo either.
  - The mass-delete circuit breaker (see MAX_DELETES / MAX_DELETE_FRACTION),
    modelled on cleanup_external_duplicates.py: refusing an unexpectedly large
    delete catches a criteria bug or a bad table argument BEFORE it empties a
    reviewer's queue. --yes-delete-many overrides it deliberately.

Makes no direct HTTP calls of its own: everything goes through
airtable_client / enrichment, so it inherits the shared retrying sessions in
http_client.py. Don't add a bare requests call here.
"""
import argparse
import logging
import sys
import time
from collections import Counter

# Channel titles can carry characters outside Windows' console codepage;
# without this, printing one aborts the run partway with UnicodeEncodeError.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from airtable_client import AirtableReadError, get_records, delete_record
from enrichment import (
    get_channel_stats,
    get_recent_video_performance,
    count_longform_in_older_videos,
    # Imported from enrichment, which OWNS them, not re-exported through main.
    # main's import list is its own dependency list, not a public facade — a
    # linter's unused-import autofix there would have broken this script at
    # import time, far from the cause.
    calc_uploads_per_year,
    days_since_last_upload,
)
from search_zones import zone_verdict, description_location_outside_zone
from main import (
    NICHES,
    MIN_LONGFORM_VIDEO_COUNT,
    description_is_non_english,
    excluded_topic_reason,
    longform_drop_reason,
    non_latin_script_chars,
    pre_push_drop_reason,
    resolve_country,
    DROP_EXCLUDED_TOPIC,
    DROP_NON_ENGLISH_DESCRIPTION,
    DROP_OUTSIDE_SEARCH_ZONE,
)
from config import API_SLEEP_SECONDS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Only rows still sitting in the reviewer's inbox are eligible for deletion.
# Anything a human has already acted on is theirs, not ours.
DELETABLE_STATUSES = {"New"}

# Circuit breaker, same shape as cleanup_external_duplicates.py. Both bounds
# exist because either alone has a blind spot: a fraction misses tiny tables,
# an absolute cap misses large ones.
MAX_DELETES = 25
MAX_DELETE_FRACTION = 0.5

# A row we could not re-enrich (private, deleted, renamed, or a transient API
# failure) is NEVER a deletion candidate. "We couldn't look" is not evidence
# against a channel — the same rule the pipeline's gates follow for absent data.
VERDICT_UNREACHABLE = "unreachable"
VERDICT_PASS = "pass"


def evaluate_row(record: dict, niche_config: dict) -> tuple[str, str]:
    """
    Re-run the current hard gates against one tracked row.

    Returns (verdict, detail) where verdict is VERDICT_PASS,
    VERDICT_UNREACHABLE, or a DROP_* reason from the pipeline's own gate
    functions — deliberately reusing pre_push_drop_reason / longform_drop_reason
    / zone_verdict rather than reimplementing the thresholds, so this script and
    the pipeline can never disagree about what "fits".
    """
    channel_id = record["fields"].get("Channel ID")
    if not channel_id:
        return VERDICT_UNREACHABLE, "row has no Channel ID"

    stats = get_channel_stats(channel_id)
    time.sleep(API_SLEEP_SECONDS)
    if stats is None:
        return VERDICT_UNREACHABLE, "channels.list returned nothing (private/deleted?)"

    topic = excluded_topic_reason(stats.get("channel_title", ""), stats.get("description", ""))
    if topic:
        return DROP_EXCLUDED_TOPIC, topic

    # Same order as process_candidate: the free description checks first.
    if description_is_non_english(stats.get("description", "")):
        return DROP_NON_ENGLISH_DESCRIPTION, (
            f"{non_latin_script_chars(stats.get('description', ''))} non-Latin script chars in the bio"
        )

    desc_country = description_location_outside_zone(stats.get("description", ""))
    if desc_country:
        return DROP_OUTSIDE_SEARCH_ZONE, f"description says {desc_country}"

    performance = get_recent_video_performance(channel_id, stats.get("uploads_playlist_id"))
    time.sleep(API_SLEEP_SECONDS)
    if performance is None:
        return VERDICT_UNREACHABLE, "no accessible recent video performance"

    upload_dates = performance.get("upload_dates", [])
    uploads_per_year = calc_uploads_per_year(upload_dates)
    days_since = days_since_last_upload(upload_dates)

    settled = performance.get("settled_views") or []
    drop = pre_push_drop_reason(
        stats.get("subscriber_count"),
        performance.get("avg_views"),
        performance.get("shorts_only", False),
        min_avg_views=niche_config["min_avg_views"],
        video_count=stats.get("video_count"),
        content_language=performance.get("content_language"),
        settled_views=settled,
        uploads_per_year=uploads_per_year,
        days_since_last_upload=days_since,
    )
    if drop:
        clearing = sum(1 for v in settled if v >= 10_000)
        return drop, (
            f"avg {round(performance.get('avg_views') or 0):,} views, "
            f"{clearing}/{len(settled)} long-form videos over 10k, "
            f"{stats.get('video_count')} videos, lang {performance.get('content_language') or 'unset'}"
        )

    country = resolve_country(stats, performance)
    if zone_verdict(country) is False:
        return DROP_OUTSIDE_SEARCH_ZONE, f"country {country}"

    # Long-form floor last, because it is the only check that can spend extra
    # quota — exactly the ordering process_candidate uses.
    longform = performance.get("longform_count", 0)
    if longform < MIN_LONGFORM_VIDEO_COUNT:
        longform = count_longform_in_older_videos(
            channel_id,
            stats.get("uploads_playlist_id", ""),
            performance.get("next_page_token", ""),
            already_counted=longform,
            target=MIN_LONGFORM_VIDEO_COUNT,
        )
    drop = longform_drop_reason(longform)
    if drop:
        return drop, f"{longform} confirmed long-form videos"

    return VERDICT_PASS, ""


def audit_table(niche_name: str, table_name: str, limit: int | None) -> list[dict]:
    """Re-check every row in one niche table. Returns the per-row verdicts."""
    if not table_name:
        logger.error("No table configured for niche '%s' — skipping.", niche_name)
        return []

    records = get_records(
        table_name, fields=["Channel ID", "Channel Name", "Status", "Qualification"]
    )
    if limit:
        records = records[:limit]

    logger.info("'%s': re-checking %d row(s).", niche_name, len(records))
    niche_config = NICHES[niche_name]
    results = []

    for i, record in enumerate(records, start=1):
        fields = record["fields"]
        verdict, detail = evaluate_row(record, niche_config)
        status = fields.get("Status") or "(unset)"
        name = (fields.get("Channel Name") or "")[:40]
        results.append({
            "record_id": record["id"],
            "name": name,
            "status": status,
            "verdict": verdict,
            "detail": detail,
            "deletable": verdict not in (VERDICT_PASS, VERDICT_UNREACHABLE)
                         and status in DELETABLE_STATUSES,
        })
        mark = "OK  " if verdict == VERDICT_PASS else ("?   " if verdict == VERDICT_UNREACHABLE else "FAIL")
        print(f"[{niche_name}] {i}/{len(records)} {mark} {name} [{status}] {verdict} {detail}")
        time.sleep(API_SLEEP_SECONDS)

    return results


def delete_failures(
    table_name: str,
    results: list[dict],
    yes_delete_many: bool,
    only_names: set[str] | None = None,
) -> int:
    """
    Delete the rows the audit marked deletable, subject to the breaker.

    `only_names` is an explicit ALLOWLIST of Channel Names. When given, a row is
    deleted only if the audit failed it AND it is named — two independent gates.
    That matters because deletion is irreversible and the audit re-runs against
    live YouTube data: a channel's view counts can shift between the report a
    human approved and the run that deletes, so "whatever fails this time" is
    not the same set the human signed off on. Naming them removes that gap.
    """
    targets = [r for r in results if r["deletable"]]
    if only_names is not None:
        skipped = [r for r in targets if r["name"] not in only_names]
        targets = [r for r in targets if r["name"] in only_names]
        for r in skipped:
            print(f"SKIP (not in --only) {r['name']} [{r['status']}] {r['verdict']}")
    if not targets:
        print("Nothing to delete.")
        return 0

    total = len(results)
    fraction = len(targets) / total if total else 0
    if not yes_delete_many and (len(targets) > MAX_DELETES or fraction > MAX_DELETE_FRACTION):
        print(
            f"\nREFUSING to delete {len(targets)} of {total} rows "
            f"({fraction:.0%}) — over the circuit breaker "
            f"({MAX_DELETES} rows or {MAX_DELETE_FRACTION:.0%}).\n"
            "That is usually a criteria bug or the wrong table, not a real "
            "result. Re-read the report above; pass --yes-delete-many only if "
            "this many really should go."
        )
        return 0

    deleted = 0
    for target in targets:
        if delete_record(table_name, target["record_id"]):
            deleted += 1
            print(f"DELETED {target['name']} ({target['verdict']})")
        else:
            print(f"FAILED to delete {target['name']} — left in place")
        time.sleep(API_SLEEP_SECONDS)
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-check tracked prospects against the current criteria"
    )
    parser.add_argument("--niche", default=None, help="Only this niche (default: all)")
    parser.add_argument("--limit", type=int, default=None, help="Max rows per niche")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually DELETE failing rows whose Status is New. Without this, report only.",
    )
    parser.add_argument(
        "--yes-delete-many",
        action="store_true",
        help=f"Override the circuit breaker (>{MAX_DELETES} rows or >{MAX_DELETE_FRACTION:.0%} of a table).",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="CHANNEL_NAME",
        help="Repeatable allowlist: delete ONLY these Channel Names, and only if "
             "the audit also fails them. Use this to act on a report a human "
             "approved, so a view count that shifted since then can't widen the "
             "delete set.",
    )
    args = parser.parse_args()

    niches = {args.niche: NICHES[args.niche]} if args.niche else dict(NICHES)
    if args.niche and args.niche not in NICHES:
        parser.error(f"Unknown niche {args.niche!r}. Known: {', '.join(NICHES)}")

    mode = "DELETE" if args.confirm else "REPORT-ONLY"
    print(f"Mode: {mode}. Deletable statuses: {sorted(DELETABLE_STATUSES)}\n")

    grand_total = 0
    grand_deleted = 0
    # Names the allowlist matched anywhere. Accumulated across niches and
    # reported ONCE at the end, not per table: an --only name naturally belongs
    # to one table, so warning per table cried wolf about every name that simply
    # lived in the other one — burying a real mismatch in false ones.
    matched_names: set[str] = set()
    for niche_name, niche_config in niches.items():
        try:
            results = audit_table(niche_name, niche_config.get("table_name"), args.limit)
        except AirtableReadError as e:
            logger.error("Cannot read '%s' (%s) — skipping this niche.", niche_name, e)
            continue

        if not results:
            continue
        grand_total += len(results)

        counts = Counter(r["verdict"] for r in results)
        deletable = [r for r in results if r["deletable"]]
        print(f"\n--- {niche_name}: {len(results)} row(s) ---")
        for verdict, n in counts.most_common():
            print(f"  {verdict}: {n}")
        print(f"  -> {len(deletable)} failing row(s) with Status in {sorted(DELETABLE_STATUSES)}")

        # Rows that fail but are NOT deletable are the interesting ones: a human
        # already acted on them, so they need a decision rather than a delete.
        locked = [
            r for r in results
            if r["verdict"] not in (VERDICT_PASS, VERDICT_UNREACHABLE) and not r["deletable"]
        ]
        if locked:
            print(f"  NOTE: {len(locked)} failing row(s) left alone (Status not New):")
            for r in locked:
                print(f"    - {r['name']} [{r['status']}] {r['verdict']}")

        if args.confirm:
            allowlist = set(args.only) if args.only else None
            if allowlist is not None:
                matched_names |= {
                    r["name"] for r in results if r["deletable"] and r["name"] in allowlist
                }
            grand_deleted += delete_failures(
                niche_config["table_name"], results, args.yes_delete_many,
                only_names=allowlist,
            )

    print(f"\n=== {grand_total} row(s) checked, {grand_deleted} deleted ===")
    if args.confirm and args.only:
        # Loud, and only now that every table has been seen: an unmatched name
        # means the allowlist and reality disagree — the row was renamed, is
        # already gone, or now PASSES. None of those should read as "done".
        unmatched = set(args.only) - matched_names
        if unmatched:
            print(
                "\nWARNING: --only named these but no table failed them — "
                f"renamed, already deleted, or now passing: {sorted(unmatched)}"
            )
    if not args.confirm and grand_total:
        print("Report-only run — nothing was changed. Re-run with --confirm to delete.")


if __name__ == "__main__":
    main()
