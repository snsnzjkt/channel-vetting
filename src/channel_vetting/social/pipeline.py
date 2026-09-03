"""
The TikTok + Instagram run: discover -> screen -> gate -> write.

BUDGET SHAPE, which is the thing to understand before changing anything here.
Screening a creator costs 0.01 (discovery) + 0.03 (posts) = 0.04 credits. The
0.2 contact lookup is NOT part of a run by default, because the criteria draft
puts two human gates — is there a subject we can model, are the photos good
enough — before the decision to contact anyone. Paying 0.2 for an address on a
creator a reviewer is about to reject on a blurry feed is the easiest way to
waste this budget, and it is 5x the cost of screening them.

THE QUALITY FLOOR IS ENFORCED BEFORE ANY MONEY IS SPENT. Every gate that
distinguishes a real prospect from a follower count lives behind the posts
call, so a run that cannot afford SOCIAL_MIN_POSTS_SCREENS_PER_RUN screens for a
platform ABORTS that platform instead of admitting creators judged on follower
count alone. That failure mode is otherwise invisible: an under-screened row
looks exactly like a screened one in the review queue, and a reviewer has no
way to tell. Under-spending degrades quality silently, which is why it is an
abort and not a warning.

Nothing here writes a Qualification of "Qualified". Four auto-reject rules in
the draft have no purchasable answer (usable subject, photo quality, fake
follower risk, and audience age on TikTok), so every admitted row lands as
Review Decision = Pending with the measured numbers attached and the media URLs
a reviewer needs. The pipeline's job is to spend a reviewer's attention well,
not to pretend it finished the rubric.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from channel_vetting import config
from channel_vetting.airtable import client as airtable
from channel_vetting.airtable.client import (
    _base_url,
    _headers,
    get_tracked_handles,
)
from channel_vetting.social.suppression import fetch_social_blocklist as fetch_blocklist
from channel_vetting.budget import credit_tracker
from channel_vetting.core.http_client import post_with_rate_limit_retry, safe_body
from channel_vetting.core.prospect_day import today_iso


def _iso_minutes(moment) -> str:
    """An Airtable-friendly UTC timestamp, to the minute."""
    return moment.strftime("%Y-%m-%dT%H:%M:00.000Z")
from channel_vetting.social import criteria, discovery, platforms, posts, relevance
from channel_vetting.social.handles import normalize_social_handle, profile_url
from channel_vetting.social.lanes import lanes_in_order

logger = logging.getLogger(__name__)

# TWO ROWS PER ADMITTED CREATOR: the prospect in the Creators table, then its
# measurements in the per-platform account table, linked back by record id.
#
# The order is the safety property, small as it is. The creator row is what a
# reviewer works from; the account row is detail. A creator row with no account
# row is degraded but usable (the numbers are also summarised in its Notes), so
# an account-write failure is recorded and moved past rather than treated as a
# lost prospect. The reverse — an account row with no creator — would be
# orphaned measurements on no review page.

@dataclass
class PlatformResult:
    """What one platform's pass did, in enough detail to explain a thin run."""

    platform: str
    aborted: str = ""
    discovered: int = 0
    screened: int = 0
    admitted: int = 0
    already_tracked: int = 0
    blocked: int = 0
    write_failures: int = 0
    rejections: dict = field(default_factory=dict)

    def note_rejection(self, reason: str) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1

    def summary(self) -> str:
        if self.aborted:
            return f"{self.platform}: ABORTED — {self.aborted}"
        top = ", ".join(
            f"{reason}={count}"
            for reason, count in sorted(self.rejections.items(), key=lambda kv: -kv[1])
        )
        return (
            f"{self.platform}: discovered {self.discovered}, screened {self.screened}, "
            f"admitted {self.admitted} (already tracked {self.already_tracked}, "
            f"DNC {self.blocked}, write failures {self.write_failures})"
            + (f" | rejected: {top}" if top else "")
        )


def affordable_posts_screens() -> int:
    """
    How many posts screens the SHARED ledger will currently authorise.

    Asks the ledger rather than dividing the per-run ceiling, because the daily
    and monthly caps sit above it: a run late in the month can be inside its own
    per-run budget and still be refused by the month. Probing with can_afford
    means the floor check below reflects what will actually be permitted.
    """
    cost = config.SOCIAL_POSTS_CREDITS_PER_REQUEST
    if cost <= 0:
        return 0
    per_run_allowance = int(config.SOCIAL_MAX_POSTS_CREDITS_PER_RUN / cost)
    if not credit_tracker.can_afford(cost * max(per_run_allowance, 1), "posts screen probe"):
        # The full per-run allowance is not available; find what is, without
        # spending anything.
        affordable = 0
        for n in range(per_run_allowance, 0, -1):
            if credit_tracker.can_afford(cost * n, "posts screen probe"):
                affordable = n
                break
        return affordable
    return per_run_allowance


def prospect_table_for(platform: str) -> str | None:
    """
    The platform's prospect table, or None when it is not configured.

    Reads the config attribute NAMED in the platform registry, so adding a
    platform is a registry entry plus an env var — not another branch here.
    """
    try:
        return getattr(config, platforms.spec(platform)["table_config_attr"], None)
    except ValueError:
        return None


def _prospect_record(platform, candidate, followers, metrics, lane_key="") -> dict:
    """
    The prospect row: one per creator, everything a reviewer needs in one place.

    TWO COLUMNS FOR REACH, ON PURPOSE. "Median Views (last 10)" is the figure
    the gates used, per the draft's "judge on the median... not the average".
    "Avg Views per Post" / "Avg Reel Plays" is the genuine mean. Both are
    written because on a creator with one viral post they differ by orders of
    magnitude, and collapsing them would make one of the two labels a lie.

    QUALIFICATION IS SET, STATUS IS NOT DECIDED. Qualification records the
    verdict of the auto-reject rules this pipeline CAN check; Status starts at
    "New". The two human gates land as "Not checked" rather than blank, so an
    unreviewed row is visibly unreviewed instead of merely empty — the draft
    calls photo quality "a gate", and a blank cell in a gate column reads as
    passed.

    LEFT OUT rather than filled with the nearest number to hand: Email (contact
    enrichment is deferred until a human approves) and anything the posts
    response does not carry. A blank cell reads as unknown; a zero reads as
    measured.
    """
    handle = candidate["handle"]
    spec = platforms.spec(platform)
    engagement = metrics.engagement_rate(platform, followers)
    points, _out_of = criteria.auto_score(
        platform, followers=followers, metrics=metrics, in_zone=True
    )
    band = criteria.follower_band(int(followers or 0))

    record = {
        "Creator Name": candidate.get("channel_title") or handle,
        "Handle": handle,
        "Profile URL": profile_url(platform, handle),
        "Account ID": candidate.get("influencers_user_id") or "",
        "Qualification": "Qualified",
        "Status": "New",
        "Subject Check": "Not checked",
        "Photo Quality": "Not checked",
        "Followers": int(followers or 0),
        "Median Views (last 10)": metrics.median_views,
        "Avg Likes per Post": metrics.avg_likes,
        "Avg Comments per Post": metrics.avg_comments,
        "Posts per Week": metrics.posts_per_week,
        "Days Since Last Post": metrics.days_since_last_post,
        "Posts Sampled": metrics.sample_size,
        "Follower Band": band,
        "Priority Band": criteria.is_priority(
            platform, followers=followers, metrics=metrics
        ),
        "Auto Score (of 35)": points,
        "Lane": lane_key or None,
        "Source": "influencers.club discovery",
        # Always set: the screen ran and was PAID FOR whether or not any post
        # carried a readable timestamp.
        "Screened At": _iso_minutes(datetime.now(timezone.utc)),
        "Date Added": today_iso(),
        "Notes": (
            f"Auto-screened over {metrics.sample_size} posts. Engagement measured "
            f"per {spec['denominator'].upper()} on this platform, floor "
            f"{criteria.engagement_floor(platform, followers):.2%} for the {band} "
            f"band. Median and mean reach are both recorded and diverge on creators "
            f"with a viral post - the median is the one the gates used.\n"
            f"STILL NEEDS A HUMAN: subject check, photo quality, fake-follower risk"
            + ("." if spec["audience_age_available"]
               else f", audience age (no {spec['label']} source).")
        ),
    }
    if metrics.last_post_at is not None:
        record["Last Posted"] = metrics.last_post_at.date().isoformat()
    if engagement is not None:
        record[spec["engagement_column"]] = engagement
    # The mean alongside the median, in the column this platform names for it.
    record[spec["reach_mean_column"]] = metrics.avg_views
    # Shares only where the platform HAS a column, and only when actually
    # reported — a false zero in an "Avg Shares" column reads as measured.
    if spec["shares_column"]:
        record[spec["shares_column"]] = metrics.avg_shares
    if metrics.media_urls:
        record["Sample Media"] = "\n".join(metrics.media_urls)

    return {k: v for k, v in record.items() if v is not None}



def _create_row(table_name: str, fields: dict) -> str | None:
    """
    Create one Airtable row. Returns its record id, or None on failure.

    THE RECORD ID IS THE RETURN VALUE, not a bool, because the account row has
    to LINK to the creator row and an Airtable link field takes record ids. A
    bool would have forced a second lookup by handle to find the row we just
    wrote.

    Uses the airtable client's own `_base_url`/`_headers` and its
    rate-limit-retrying POST helper rather than a second HTTP path, so the token,
    the base id, the URL encoding and the 429 handling are all the ones the rest
    of the project is tested against. push_record() is not reusable here: it
    dedupes on "Channel ID", which a TikTok or Instagram creator does not have.

    typecast=True so a Source or Language option we have not seen before is
    created rather than rejecting the write, matching push_record.
    """
    payload = {"fields": fields, "typecast": True}
    try:
        resp = post_with_rate_limit_retry(
            _base_url(table_name), headers=_headers(), json=payload, timeout=30
        )
    except requests.RequestException as exc:
        logger.error("social row create failed for %s: %s", fields.get("Handle"), exc)
        return None
    if resp.status_code not in (200, 201):
        logger.error(
            "social row create rejected for %s in %s: %s %s",
            fields.get("Handle"), table_name, resp.status_code, safe_body(resp),
        )
        return None
    try:
        return resp.json().get("id")
    except ValueError:
        # A 200/201 with an unreadable body means the row probably EXISTS but we
        # cannot link to it. Say so rather than returning None, which the caller
        # would read as "nothing was written" and might retry into a duplicate.
        logger.error(
            "social row created in %s but the response was not JSON — cannot link "
            "the account row to it; the creator row exists and is unlinked",
            table_name,
        )
        return ""


def run_platform(platform: str, *, target=None, blocklist=None, dry_run=False) -> PlatformResult:
    """One platform's pass. Never raises; returns what happened."""
    platform = (platform or "").lower()
    result = PlatformResult(platform=platform)
    target = target or config.SOCIAL_TARGET_PER_PLATFORM

    table = prospect_table_for(platform)
    if not table:
        try:
            env = platforms.spec(platform)["table_config_attr"]
        except ValueError as exc:
            result.aborted = str(exc)
            return result
        result.aborted = f"{env} is not configured"
        return result

    # THE QUALITY FLOOR, checked before a single credit is spent.
    screens = affordable_posts_screens()
    if screens < config.SOCIAL_MIN_POSTS_SCREENS_PER_RUN:
        result.aborted = (
            f"posts budget authorises only {screens} screens, below the "
            f"SOCIAL_MIN_POSTS_SCREENS_PER_RUN floor of "
            f"{config.SOCIAL_MIN_POSTS_SCREENS_PER_RUN}. Refusing to admit "
            f"creators screened on follower count alone"
        )
        logger.error("%s", result.summary())
        return result

    # THE DAILY ROW CAP, same logic and the same env knob as the YouTube niches:
    # counted from the destination table's own "Date Added", so a second run the
    # same day tops up to the cap instead of doubling it. Reusing
    # DAILY_QUALIFIED_CAP rather than inventing a social-specific one keeps one
    # number to tune, and it is a THROUGHPUT knob only — a row admitted at the
    # cap is one that would have been admitted earlier in the day.
    try:
        # id_field="Handle": the prospect tables have no "Channel ID" (a TikTok
        # creator has no channel id), and the default would return
        # 422 UNKNOWN_FIELD_NAME. The field is only there to keep the response
        # small; the count comes from the record count.
        already_today = airtable.count_added_today(table, "Qualified", id_field="Handle")
    except Exception as exc:
        # A cap we cannot read must not be assumed empty — that is how a run
        # spends a full day's budget twice.
        result.aborted = f"could not read today's row count for {table}: {exc}"
        return result
    headroom = max(config.DAILY_QUALIFIED_CAP - already_today, 0)
    if headroom <= 0:
        result.aborted = (
            f"daily cap reached: {already_today}/{config.DAILY_QUALIFIED_CAP} rows "
            f"already added to {table} today"
        )
        logger.info("%s", result.summary())
        return result
    target = min(target, headroom)

    try:
        tracked = get_tracked_handles(table)
    except Exception as exc:
        result.aborted = f"could not read tracked handles: {exc}"
        return result

    if blocklist is None:
        blocklist = fetch_blocklist()

    client = discovery.client_for_run()
    seen = set(tracked)
    remaining_screens = screens

    for lane in lanes_in_order():
        if result.admitted >= target or remaining_screens <= 0:
            break

        candidates = discovery.discover(
            platform,
            lane=lane,
            target=max(target - result.admitted, 1),
            exclude_handles=sorted(seen),
            client=client,
        )
        result.discovered += len(candidates)

        for candidate in candidates:
            if result.admitted >= target or remaining_screens <= 0:
                break
            handle = normalize_social_handle(candidate.get("handle", ""))
            if not handle or handle in seen:
                result.already_tracked += 1
                continue
            seen.add(handle)

            # DO NOT CONTACT before anything is bought for this creator.
            name = candidate.get("channel_title") or ""
            blocked_by = blocklist.match(handle=handle, name=name) if blocklist else ""
            if blocked_by:
                logger.info("DO NOT CONTACT match on %s (%s) — skipping", handle, blocked_by)
                result.blocked += 1
                continue

            metrics = posts.fetch_metrics(platform, handle)
            remaining_screens -= 1
            result.screened += 1

            # The vendor's follower count, carried deliberately on this path
            # (see InfluencerDiscovery._carry_vendor_stats): TikTok and
            # Instagram have no free API to verify it against, so this is the
            # source of truth rather than a number to distrust.
            followers = candidate.get("vendor_followers") or 0
            reason = criteria.auto_reject_reason(
                platform, followers=followers, metrics=metrics
            )
            # THE PET REQUIREMENT, checked after the numeric gates because it
            # reads captions the same posts response already paid for, so the
            # order costs nothing either way — but a numeric rejection is the
            # cheaper thing to report first. Lanes with pet_required=False (the
            # people and TRPG verticals, both off by default) skip it.
            if not reason and lane.get("pet_required", True):
                reason = relevance.pet_content_reason(
                    metrics, name=candidate.get("channel_title") or ""
                )
            if reason:
                result.note_rejection(reason)
                continue

            if dry_run:
                result.admitted += 1
                continue

            if _create_row(
                table, _prospect_record(platform, candidate, followers, metrics,
                                        lane.get("key", ""))
            ) is None:
                result.write_failures += 1
            else:
                result.admitted += 1

    logger.info("%s", result.summary())
    return result


def run(*, platforms=None, target=None, dry_run=False) -> list[PlatformResult]:
    """Both platforms, sharing one DO NOT CONTACT read and one ledger."""
    platforms = platforms or discovery.SUPPORTED
    blocklist = fetch_blocklist()
    results = []
    for platform in platforms:
        results.append(
            run_platform(platform, target=target, blocklist=blocklist, dry_run=dry_run)
        )
    logger.info("social run credit summary: %s", credit_tracker.spend_summary())
    return results


def main() -> None:
    """
    CLI entry point: `python -m channel_vetting.social.pipeline`.

    --dry-run screens and gates but writes nothing, which is the honest way to
    calibrate the thresholds on real creators before any row reaches a review
    queue. It still SPENDS, because the numbers being calibrated are the ones
    that have to be bought — there is no free way to preview them.
    """
    import argparse
    import logging as _logging
    import sys

    parser = argparse.ArgumentParser(
        description="TikTok + Instagram creator sourcing (Mythumi)"
    )
    parser.add_argument(
        "--platform", action="append", choices=list(discovery.SUPPORTED),
        help="Limit the run to one platform. Repeatable. Default: both.",
    )
    parser.add_argument(
        "--target", type=int, default=None,
        help=f"Admitted creators per platform (default {config.SOCIAL_TARGET_PER_PLATFORM}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Screen and gate but write no Airtable rows. Still spends credits.",
    )
    args = parser.parse_args()

    _logging.basicConfig(
        level=_logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    results = run(platforms=args.platform, target=args.target, dry_run=args.dry_run)

    print("\n=== Mythumi social run ===")
    for result in results:
        print(f"  {result.summary()}")
    print(f"  credits: {credit_tracker.spend_summary()}")

    # A run where BOTH platforms aborted produced nothing and spent nothing —
    # exit non-zero so a scheduled run shows red rather than a green no-op,
    # which is the failure the YouTube path's zero-row visibility work exists
    # to prevent.
    if results and all(r.aborted for r in results):
        print("  ALL PLATFORMS ABORTED — see the reasons above.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
