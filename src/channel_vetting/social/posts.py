"""
The 0.03-credit posts screen — where every TikTok/Instagram gate gets its data.

`POST /public/v1/creators/content/posts/` returns, per the vendor's docs,
"URL, caption, media URLs, timestamps, and engagement metrics (likes, comments,
views, shares)". One request per creator answers every numeric rule in the
criteria draft: median reach over the last 10 posts, days since the last post,
posts per week, and engagement computed per VIEW for TikTok / per FOLLOWER for
Instagram.

WHY THIS ENDPOINT AND NOT `enrich by handle analytics`. Analytics costs 0.8
credits against this one's 0.03 — 27x — and the extra it buys is audience
demographics and income estimates, neither of which is an auto-reject rule we
can act on (Instagram creators over 10k already carry audience data in the
discovery response, and TikTok has none either way). Screening a creator for
0.04 all-in instead of 1.04 is the difference between this being affordable
inside the existing monthly ceiling and not.

Page sizes are platform-specific and both cover the draft's 10-post window in a
single request: Instagram is fixed at 12 posts, TikTok defaults to 30 (max 35).

UNVERIFIED: the exact JSON nesting of the item list. The docs name the fields
(`result`, `items`, `count`, `more_available`, `next_token`) but this has never
run against a live response, so `_items_from()` checks the plausible shapes
rather than assuming one. Everything degrades to "unmeasured" instead of
raising, and an unmeasured creator is REJECTED by criteria.auto_reject_reason
rather than admitted — so a wrong guess here costs rows, never quality. Pin the
real shape with a recorded response the first time this runs for real.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from channel_vetting import config
from channel_vetting.budget.credit_tracker import (
    KIND_DISCOVERY,
    can_afford,
    record_spend,
)
from channel_vetting.config import INFLUENCERS_BASE_URL
from channel_vetting.core.http_client import INFLUENCERS as HTTP, safe_body
from channel_vetting.social.criteria import (
    engagement_rate as _engagement_rate,
    median_views as _median_views,
)

logger = logging.getLogger(__name__)

POSTS_PATH = "/public/v1/creators/content/posts/"
REQUEST_TIMEOUT_SECONDS = 45

# Requested page size per platform. Instagram ignores it (fixed 12); TikTok
# caps at 35. Asking for the draft's 10 would not be cheaper — the price is per
# REQUEST, not per post — so ask for enough that the cadence figure has some
# spine to it and then compute the median over the newest SOCIAL_POSTS_SAMPLE_SIZE.
_PAGE_SIZE = {"instagram": 12, "tiktok": 30}


@dataclass(frozen=True)
class PostMetrics:
    """
    Everything the criteria gates need from one posts request.

    `measured` is the honest-ignorance flag: False means the request failed, was
    unaffordable, or returned nothing usable. It is NOT the same as "failed the
    gates", and the two must never collapse into each other — a creator we could
    not measure has to be rejected as unmeasured, not recorded as below-criteria,
    or the run summary would claim evidence it does not have.
    """

    measured: bool = False
    sample_size: int = 0
    median_views: int | None = None
    days_since_last_post: int | None = None
    posts_per_week: float | None = None
    total_interactions: int = 0
    total_shares: int = 0
    total_views: int = 0
    reason: str = ""
    media_urls: tuple = field(default_factory=tuple)

    def engagement_rate(self, platform: str, followers) -> float | None:
        """
        Engagement over the sampled window, per this platform's own rule.

        Uses the WINDOW totals rather than a per-post average so a creator whose
        posts vary wildly is judged on aggregate behaviour, and so a post with a
        missing view count cannot become a zero-view outlier.
        """
        if not self.measured:
            return None
        return _engagement_rate(
            platform,
            interactions=self.total_interactions,
            views=self.total_views,
            followers=int(followers or 0),
        )


UNMEASURED = PostMetrics(measured=False, reason="not_attempted")


def _as_int(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _first_present(item: dict, *names):
    """First non-None value among `names`, so field aliases are tolerated."""
    for name in names:
        if name in item and item[name] is not None:
            return item[name]
    return None


def _items_from(body) -> list:
    """
    The post list, whichever documented shape it arrives in.

    Deliberately permissive: see the module docstring on why guessing wrong here
    must cost rows rather than quality.
    """
    if not isinstance(body, dict):
        return []
    for path in (("result", "items"), ("result", "posts"), ("items",), ("posts",), ("data",)):
        node = body
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, list):
            return node
    return []


def _parse_timestamp(raw):
    """A timezone-aware datetime from an ISO string or a unix epoch, else None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def metrics_from_items(items, *, sample_size=None, now=None) -> PostMetrics:
    """
    Turn a raw post list into PostMetrics. Pure — no HTTP, no credits.

    Split out from the request so the arithmetic is testable without a network
    or a ledger, which is what lets the gates be pinned by tests.
    """
    if not items:
        return PostMetrics(measured=False, reason="no_posts_returned")

    limit = sample_size or config.SOCIAL_POSTS_SAMPLE_SIZE
    now = now or datetime.now(timezone.utc)

    # Newest first, so the "last 10" really is the newest 10 whatever order the
    # vendor returns. Posts with no readable timestamp sort last rather than
    # being dropped: they still carry usable view and like counts.
    dated = []
    for item in items:
        if not isinstance(item, dict):
            continue
        stamp = _parse_timestamp(
            _first_present(item, "timestamp", "taken_at", "created_at", "posted_at", "date")
        )
        dated.append((stamp, item))
    if not dated:
        return PostMetrics(measured=False, reason="no_usable_posts")
    dated.sort(key=lambda pair: (pair[0] is not None, pair[0]), reverse=True)

    window = dated[:limit]
    views, interactions, shares, total_views, media = [], 0, 0, 0, []
    for stamp, item in window:
        v = _first_present(item, "views", "view_count", "play_count", "plays")
        if v is not None:
            views.append(_as_int(v))
            total_views += _as_int(v)
        likes = _as_int(_first_present(item, "likes", "like_count"))
        comments = _as_int(_first_present(item, "comments", "comment_count"))
        share_count = _as_int(_first_present(item, "shares", "share_count"))
        interactions += likes + comments + share_count
        shares += share_count
        url = _first_present(item, "media_url", "media_urls", "thumbnail_url", "url", "post_url")
        if isinstance(url, str) and url:
            media.append(url)
        elif isinstance(url, (list, tuple)):
            media.extend(u for u in url if isinstance(u, str) and u)

    stamps = [s for s, _ in window if s is not None]
    days_since = None
    per_week = None
    if stamps:
        newest, oldest = max(stamps), min(stamps)
        days_since = max((now - newest).days, 0)
        span_days = max((newest - oldest).days, 0)
        # One post cannot establish a cadence. Reporting None keeps
        # auto_reject_reason honest instead of inventing a rate from a single
        # point, and an unknown cadence is a rejection, not a pass.
        if len(stamps) >= 2 and span_days >= 1:
            per_week = round(len(stamps) / (span_days / 7.0), 2)
        elif len(stamps) >= 2:
            # Two or more posts inside one day is comfortably above 1/week.
            per_week = float(len(stamps) * 7)

    return PostMetrics(
        measured=True,
        sample_size=len(window),
        median_views=_median_views(views),
        days_since_last_post=days_since,
        posts_per_week=per_week,
        total_interactions=interactions,
        total_shares=shares,
        total_views=total_views,
        reason="",
        media_urls=tuple(media[:6]),
    )


def fetch_metrics(platform: str, handle: str, *, source_label="social posts screen") -> PostMetrics:
    """
    One posts request for one creator, budget-checked, then reduced to metrics.

    Charges SOCIAL_POSTS_CREDITS_PER_REQUEST against the SHARED ledger via
    can_afford/record_spend, so this path cannot exceed the same daily and
    monthly ceilings the YouTube run answers to. Recorded under KIND_DISCOVERY
    because it is part of deciding what we pay to look at, not contact data.

    Fail-soft in every direction, and every failure returns an UNMEASURED-style
    result that criteria.auto_reject_reason turns into a rejection.
    """
    cost = config.SOCIAL_POSTS_CREDITS_PER_REQUEST
    if not can_afford(cost, source_label):
        return PostMetrics(measured=False, reason="posts_budget_exhausted")

    payload = {
        "platform": (platform or "").lower(),
        "handle": handle,
        "num_results": _PAGE_SIZE.get((platform or "").lower(), 12),
    }
    url = f"{INFLUENCERS_BASE_URL}{POSTS_PATH}"
    try:
        resp = HTTP.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        logger.warning("posts screen request failed for %s/%s: %s", platform, handle, exc)
        return PostMetrics(measured=False, reason="request_failed")

    if resp.status_code != 200:
        logger.warning(
            "posts screen returned %s for %s/%s: %s",
            resp.status_code, platform, handle, safe_body(resp),
        )
        return PostMetrics(measured=False, reason=f"http_{resp.status_code}")

    try:
        body = resp.json()
    except ValueError:
        logger.warning("posts screen returned a non-JSON 200 for %s/%s", platform, handle)
        return PostMetrics(measured=False, reason="non_json_response")

    items = _items_from(body)
    # The vendor charges only for a successful result, and reports what it
    # billed. Trust the reported figure over our own estimate when present.
    billed = body.get("credits_cost") if isinstance(body, dict) else None
    billed = float(billed) if isinstance(billed, (int, float)) and not isinstance(billed, bool) else (cost if items else 0.0)
    if billed:
        record_spend(billed, kind=KIND_DISCOVERY, detail=f"{source_label} ({platform})")

    if not items:
        return PostMetrics(measured=False, reason="no_posts_returned")
    return metrics_from_items(items)
