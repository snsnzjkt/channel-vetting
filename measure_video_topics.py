"""
Score tag-based topic evidence against the reviewer's own verdicts.

This is the gate this repo requires before any relevance signal is given
authority, and the reason is on the record three times: the off-target gate was
measured ANTI-predictive (-38% discrimination), the Gemini text tier measured
27% against a 38% base rate, and AV-specialist vocabulary sat in Home Theater's
RESCUE list where it saved 0 approved and 6 rejected channels. Every one of
those looked obviously right when it was written.

The bar, from YIELD_OPTIMIZATION_PLAN.md section 12: a term ships only if it
catches more REJECTED than it kills APPROVED. Same test here, per topic and per
share threshold.

Cost: ~3 YouTube quota units per labelled channel (channels.list +
playlistItems + videos.list), all cached to disk, so a re-run is free. Zero
vendor credits and zero Gemini requests. Nothing here writes to Airtable or to
any pipeline state file.

    python measure_video_topics.py                 # both niches, cached
    python measure_video_topics.py --limit 40      # cheap smoke run
    python measure_video_topics.py --refresh       # ignore the cache
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

import config
import niches
import video_topics as vt
from airtable_client import get_records

CACHE_PATH = os.getenv("VIDEO_TOPICS_CACHE", "video_topics_cache.json")
SHARE_THRESHOLDS = (0.10, 0.25, 0.40, 0.60)

# The vocabularies worth testing on TAGS. Drawn from the lists that already
# exist rather than invented here, so a positive result is an input change and
# not a new set of hand-written terms nobody has measured.
VOCABULARIES = {
    **{k: v for k, v in niches.EXCLUDED_TOPIC_TERMS.items()},
    **{k: v for k, v in niches.OFF_TARGET_TERMS.items()},
}


def load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict) -> None:
    tmp = f"{CACHE_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=1, sort_keys=True)
    os.replace(tmp, CACHE_PATH)


def labelled_rows():
    """Every Approved/Rejected row with a Channel ID, per niche."""
    rows = []
    for niche, table in (("Home Theater", config.AIRTABLE_TABLE_HOME_THEATER),
                         ("Lifestyle Sofa", config.AIRTABLE_TABLE_LIFESTYLE_SOFA)):
        if not table:
            continue
        for rec in get_records(table, fields=["Channel ID", "Channel Name", "Status"]):
            f = rec.get("fields", {})
            if f.get("Status") in ("Approved", "Rejected") and f.get("Channel ID"):
                rows.append({"niche": niche, "channel_id": f["Channel ID"],
                             "name": f.get("Channel Name", "?"), "label": f["Status"]})
    return rows


def fetch_topics(channel_id: str) -> dict | None:
    """
    {"tags": [...], "categories": [...]} for one channel, or None if unreachable.

    Imported lazily so --help and the scoring path work with no network and no
    API key configured.
    """
    from enrichment import get_channel_stats, get_recent_video_performance

    stats = get_channel_stats(channel_id)
    if not stats:
        return None
    performance = get_recent_video_performance(
        channel_id, stats.get("uploads_playlist_id"))
    if performance is None:
        return None
    return {"tags": performance.get("video_tags") or [],
            "categories": performance.get("video_category_ids") or []}


def score(rows) -> None:
    """
    Per topic and threshold: how many APPROVED it would kill vs how many
    REJECTED it would catch.

    "Kills approved" is the cost and "catches rejected" is the benefit, exactly
    as section 12 framed it. A topic whose net is negative or zero does not
    ship, however sensible it reads.
    """
    totals = Counter(r["label"] for r in rows)
    tagged = [r for r in rows if r["evidence"]["tags_seen"] > 0]
    print(f"\njoined {len(rows)} labelled channels "
          f"({totals['Approved']} Approved / {totals['Rejected']} Rejected)")
    print(f"of those, {len(tagged)} have any tags at all "
          f"({100 * len(tagged) / max(1, len(rows)):.0f}%) — "
          f"a channel with no tags can never be dropped by this signal")

    if not tagged:
        print("\nNo tag data. Nothing to score.")
        return

    for threshold in SHARE_THRESHOLDS:
        print(f"\n=== share >= {threshold:.0%} " + "=" * 46)
        print(f"  {'topic':22s} {'kills approved':>15s} {'catches rejected':>17s} {'net':>6s}")
        verdicts = defaultdict(lambda: Counter())
        for r in rows:
            for topic, share in (r["evidence"].get("share") or {}).items():
                if share >= threshold:
                    verdicts[topic][r["label"]] += 1
        if not verdicts:
            print("  (nothing fires at this threshold)")
            continue
        for topic, c in sorted(verdicts.items(),
                               key=lambda kv: -(kv[1]["Rejected"] - kv[1]["Approved"])):
            killed, caught = c["Approved"], c["Rejected"]
            net = caught - killed
            flag = "SHIPS" if net > 0 else ("no effect" if net == 0 else "HARMFUL")
            print(f"  {topic:22s} {killed:>15d} {caught:>17d} {net:>6d}   {flag}")

    # Per-niche, because the two niches' drop distributions differ completely
    # and a topic that helps one can be inert or harmful in the other.
    for niche in sorted({r["niche"] for r in rows}):
        sub = [r for r in rows if r["niche"] == niche]
        fires = Counter()
        for r in sub:
            for topic, share in (r["evidence"].get("share") or {}).items():
                if share >= 0.25:
                    fires[(topic, r["label"])] += 1
        print(f"\n--- {niche} (n={len(sub)}), share >= 25% ---")
        topics = sorted({t for t, _ in fires})
        if not topics:
            print("  nothing fires")
        for t in topics:
            print(f"  {t:22s} kills {fires[(t,'Approved')]:2d}  catches {fires[(t,'Rejected')]:2d}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0,
                    help="score at most N labelled channels (cheap smoke run)")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore the cache and refetch (costs quota)")
    ap.add_argument("--offline", action="store_true",
                    help="score only what is already cached; make no requests")
    args = ap.parse_args(argv)

    cache = {} if args.refresh else load_cache()
    rows = labelled_rows()
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        print("No labelled rows found. Nothing to measure.")
        return 1

    fetched = 0
    scored = []
    for i, row in enumerate(rows, 1):
        cid = row["channel_id"]
        topics = cache.get(cid)
        if topics is None:
            if args.offline:
                continue
            topics = fetch_topics(cid)
            fetched += 1
            # Persist as we go: a run interrupted at channel 200 of 277 must not
            # throw away 200 channels' worth of quota.
            cache[cid] = topics
            if fetched % 20 == 0:
                save_cache(cache)
                print(f"  ... {i}/{len(rows)} ({fetched} fetched)", file=sys.stderr)
        if not topics:
            continue
        row = dict(row)
        row["evidence"] = vt.topic_evidence(topics.get("tags"), VOCABULARIES)
        row["categories"] = topics.get("categories") or []
        scored.append(row)

    if not args.offline:
        save_cache(cache)
    print(f"fetched {fetched} channels this run; cache holds {len(cache)}")
    score(scored)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
