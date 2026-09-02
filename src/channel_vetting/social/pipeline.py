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

import requests

from channel_vetting import config
from channel_vetting.airtable import client as airtable
from channel_vetting.airtable.client import (
    _base_url,
    _headers,
    get_tracked_handles,
)
from channel_vetting.airtable.do_not_contact import fetch_blocklist
from channel_vetting.budget import credit_tracker
from channel_vetting.core.http_client import post_with_rate_limit_retry, safe_body
from channel_vetting.core.prospect_day import today_iso
from channel_vetting.social import criteria, discovery, posts
from channel_vetting.social.handles import normalize_social_handle, profile_url
from channel_vetting.social.lanes import lanes_in_order

logger = logging.getLogger(__name__)

# ONE ROW PER CREATOR, in the Creators table. The per-platform account tables
# (AIRTABLE_TABLE_SOCIAL_TIKTOK / _INSTAGRAM) are NOT written by this version —
# a creator's measured numbers go into the Creators row's Notes instead. Wiring
# the account tables needs a linked-record write, and a half-populated account
# row that looks like a full one is worse than no row: a reviewer would read the
# blanks as measured zeroes. The env vars exist so the destination is already
# configurable when that lands.


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


def _social_record(platform, candidate, followers, metrics, country=None) -> dict:
    """The Creators-table row for an admitted creator."""
    handle = candidate["handle"]
    points, out_of = criteria.auto_score(
        platform, followers=followers, metrics=metrics, country=country
    )
    band = criteria.follower_band(int(followers or 0))
    rate = metrics.engagement_rate(platform, followers) if metrics else None
    priority = criteria.is_priority(platform, followers=followers, metrics=metrics)

    notes = [
        f"Auto-screened {out_of and f'{points}/{out_of}' or points} on the automatable "
        f"rubric components only (the draft's full rubric is "
        f"{criteria.RUBRIC_MAX}, pass {criteria.RUBRIC_PASS_MARK}).",
        f"Band {band}." + (" PRIORITY band (micro, >3.5%)." if priority else ""),
        f"Median views over last {metrics.sample_size} posts: {metrics.median_views}."
        if metrics else "",
        f"Engagement {rate:.2%} "
        f"({'per view' if platform == criteria.PLATFORM_TIKTOK else 'per follower'}), "
        f"floor {criteria.engagement_floor(platform, followers):.2%}."
        if rate is not None else "",
        f"Posts/week {metrics.posts_per_week}, last post {metrics.days_since_last_post}d ago."
        if metrics else "",
        "STILL NEEDS A HUMAN: usable subject, photo quality, fake-follower risk"
        + (", audience age." if platform == criteria.PLATFORM_TIKTOK else "."),
    ]
    if metrics and metrics.media_urls:
        notes.append("Sample media: " + " ".join(metrics.media_urls[:3]))

    return {
        "Creator Name": candidate.get("channel_title") or handle,
        "Handle": handle,
        "Primary Profile URL": profile_url(platform, handle),
        "Account ID": candidate.get("influencers_user_id") or "",
        "Primary Platform": "TikTok" if platform == criteria.PLATFORM_TIKTOK else "Instagram",
        "Outreach Platform": "TikTok" if platform == criteria.PLATFORM_TIKTOK else "Instagram",
        # NOT "Qualified". The reviewer decides; see the module docstring.
        "Review Decision": "Pending",
        "Fit Score": 0,
        "Date Added": today_iso(),
        "Notes": "\n".join(n for n in notes if n),
    }


def _create_row(table_name: str, fields: dict) -> bool:
    """
    Create one Airtable row.

    Uses the airtable client's own `_base_url`/`_headers` and its
    rate-limit-retrying POST helper rather than a second HTTP path, so the token,
    the base id, the URL encoding and the 429 handling are all the ones the rest
    of the project is tested against. push_record() is not reusable here: it
    dedupes on "Channel ID", which a TikTok or Instagram creator does not have.
    """
    payload = {"fields": fields, "typecast": True}
    try:
        resp = post_with_rate_limit_retry(
            _base_url(table_name), headers=_headers(), json=payload, timeout=30
        )
    except requests.RequestException as exc:
        logger.error("social row create failed for %s: %s", fields.get("Handle"), exc)
        return False
    if resp.status_code not in (200, 201):
        logger.error(
            "social row create rejected for %s in %s: %s %s",
            fields.get("Handle"), table_name, resp.status_code, safe_body(resp),
        )
        return False
    return True


def run_platform(platform: str, *, target=None, blocklist=None, dry_run=False) -> PlatformResult:
    """One platform's pass. Never raises; returns what happened."""
    platform = (platform or "").lower()
    result = PlatformResult(platform=platform)
    target = target or config.SOCIAL_TARGET_PER_PLATFORM

    creators_table = config.AIRTABLE_TABLE_SOCIAL_CREATORS
    if not creators_table:
        result.aborted = "AIRTABLE_TABLE_SOCIAL_CREATORS is not configured"
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

    try:
        tracked = get_tracked_handles(creators_table)
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
            if reason:
                result.note_rejection(reason)
                continue

            if dry_run:
                result.admitted += 1
                continue

            fields = _social_record(platform, candidate, followers, metrics)
            if _create_row(creators_table, fields):
                result.admitted += 1
            else:
                result.write_failures += 1

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
