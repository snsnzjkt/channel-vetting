"""
Enriches discovered channels with real stats via channels.list,
playlistItems.list, and videos.list.
"""
import logging
import re
import time
from collections import Counter
from datetime import datetime, timezone

import requests

from config import (
    YOUTUBE_API_BASE_URL,
    YOUTUBE_API_KEY,
    QUOTA_COST_CHANNELS_LIST,
    QUOTA_COST_PLAYLIST_ITEMS_LIST,
    QUOTA_COST_VIDEOS_LIST,
    API_SLEEP_SECONDS,
    EMAIL_DEEP_SCAN_PAGES,
)
from quota_tracker import record_spend

logger = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
HANDLE_PATTERN = re.compile(r"@([a-zA-Z0-9_.\-]+)")


def normalize_handle(raw: str) -> str:
    """
    Extract and normalize a YouTube @handle from either a full URL (e.g.
    "https://www.youtube.com/@Foo/videos") or a bare handle ("@Foo" or the
    raw value channels.list returns in snippet.customUrl). Returns "" if
    no @handle is present (e.g. a legacy /c/ or /user/ channel that never
    set one — those can't be matched this way).
    """
    if not raw:
        return ""
    match = HANDLE_PATTERN.search(raw)
    return match.group(1).lower() if match else ""

# A one-off email mention in a single video's description is weak evidence
# (could be a shoutout, a giveaway, someone else's contact, etc.). Seeing
# the same address repeated across several videos' descriptions is a much
# stronger signal it's the creator's actual standing contact email.
EMAIL_MIN_VIDEO_REPEATS = 3

# How many recent videos to pull descriptions from when hunting for a
# contact email. 50 is the API maximum for both playlistItems.list and a
# single videos.list call, and — crucially — costs exactly the same 2
# quota units as asking for 10 would. More description text is free, so
# there is no reason to ask for less.
EMAIL_SCAN_SAMPLE_SIZE = 50

# ...but the performance metrics stay on the most recent 10 videos. The
# Airtable column is literally named "Avg Views (last 10 videos)", and
# widening this window would shift avg views, engagement rate and upload
# cadence — and therefore Overall Score — for every newly processed
# channel, making them incomparable with the records already in the base.
# Widen the email scan, hold the scoring baseline still.
PERFORMANCE_SAMPLE_SIZE = 10

# Domains belonging to a third party that routinely shows up in creator
# descriptions — shared platforms, link-aggregators, payment/tip/coupon
# services, freelance marketplaces and no-code tools (sponsor plugs, tip
# jars, "logo by ..."). An address or website at one of these is that
# service's, never the creator's. Confirmed necessary in testing: a
# "Cash App tip jar" mention produced an "@cash.app" match via the
# repeated-email path.
THIRD_PARTY_DOMAINS = {
    "youtube.com", "youtu.be", "instagram.com", "tiktok.com", "twitter.com",
    "x.com", "facebook.com", "fb.com", "linktr.ee", "linktree.com",
    "beacons.ai", "beacons.page", "discord.gg", "discord.com", "patreon.com",
    "twitch.tv", "amzn.to", "amazon.com", "bit.ly", "goo.gl", "linkedin.com",
    "threads.net", "snapchat.com", "pinterest.com", "reddit.com",
    "google.com", "apple.com", "spotify.com",
    # Payment / tip-jar / coupon-referral services
    "cash.app", "venmo.com", "paypal.com", "paypal.me", "buymeacoffee.com",
    "ko-fi.com", "coupert.com", "honey.com",
    # URL shorteners — the shortener's own domain, not the (unknown)
    # destination, is what gets queried, so these can never be trusted.
    "tinyurl.com", "t.co", "ow.ly", "rebrand.ly", "is.gd", "tiny.cc", "cutt.ly",
    # Generic freelance-marketplace / form-builder / no-code tool platforms
    # creators routinely credit or link to (a logo designer's Fiverr gig, a
    # Tally.so submission form, a Canva design, etc.) — an address at one
    # of these is that PLATFORM's own business email, not the creator's,
    # even when the creator is using the tool themselves.
    "fiverr.com", "upwork.com", "freelancer.com", "99designs.com",
    "tally.so", "typeform.com", "forms.gle", "docs.google.com",
    "calendly.com", "canva.com", "elink.io",
}

# Free consumer mail providers. Unlike THIRD_PARTY_DOMAINS these are NOT
# someone else's domain — a creator's real contact address is very often
# a gmail one. Kept separate from EMAIL_DOMAIN_BLOCKLIST for that reason
# (see below) — still used for reporting in backfill_missing_emails.py.
FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "yahoo.com", "ymail.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "protonmail.com", "proton.me", "gmx.com", "gmx.de",
    "mail.com", "zoho.com", "yandex.com", "yandex.ru", "fastmail.com",
}

# What can never be the creator's own *email address* — third-party
# services ONLY. Freemail is deliberately absent: folding these two
# lists into one silently discarded every @gmail.com match, which is by
# far the most common kind of creator contact address (53% of the
# addresses this pipeline had collected at the time this was split).
EMAIL_DOMAIN_BLOCKLIST = THIRD_PARTY_DOMAINS


# The API exposes no "is this a Short" flag, so duration is the proxy.
# 60s is the classic Shorts cap. YouTube raised it to 3 minutes in late
# 2024, so a 60s cutoff under-detects newer Shorts channels — chosen
# deliberately: a channel misread as Shorts-only is DISCARDED before it
# ever reaches Airtable (main.pre_push_drop_reason), so a false positive
# costs a real prospect with no row left to review, while a false negative
# only costs one flagged row a human can dismiss.
SHORTS_MAX_SECONDS = 60

ISO8601_DURATION_PATTERN = re.compile(
    r"^P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


def is_blocklisted_email_domain(domain: str) -> bool:
    return domain.lower() in EMAIL_DOMAIN_BLOCKLIST


def parse_iso8601_duration(value: str | None) -> int | None:
    """
    Seconds for an ISO 8601 duration like videos.list returns ("PT12M35S"),
    or None when it's missing or unparseable.

    None means "unknown", never "zero" — see is_shorts_only().
    """
    if not value:
        return None
    match = ISO8601_DURATION_PATTERN.match(value.strip())
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    total = (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )
    # "P0D" and friends parse structurally but carry no duration; a
    # zero-second video is not evidence of anything.
    return total or None


def is_shorts_only(durations) -> bool:
    """
    True only when every sampled video is a Short, i.e. no evidence of any
    long-form upload.

    Returns False whenever ANY duration is unknown, and False for an empty
    sample. This asymmetry is deliberate: the caller discards a
    Shorts-only channel outright, so the burden of proof sits on excluding,
    not on including.
    """
    if not durations:
        return False
    parsed = [parse_iso8601_duration(d) for d in durations]
    if any(seconds is None for seconds in parsed):
        return False
    return all(seconds <= SHORTS_MAX_SECONDS for seconds in parsed)


def extract_business_email(description: str) -> str:
    """
    Best-effort extraction of a plain-text contact email from a channel's
    public About description.

    Note: YouTube deliberately does NOT expose a channel's "business
    inquiries" email through the Data API — it's gated behind a
    CAPTCHA-protected reveal button on the About page specifically to
    block scraping, and we don't attempt to circumvent that. This only
    picks up an email if a creator has typed one directly into their
    (fully public, already-fetched) description text, e.g.
    "Business inquiries: name@studio.com". Returns "" if none found.
    """
    if not description:
        return ""
    for candidate in EMAIL_PATTERN.findall(description):
        domain = candidate.rsplit("@", 1)[-1].lower()
        if not is_blocklisted_email_domain(domain):
            return candidate
    return ""


def find_repeated_email(video_descriptions: list[str]) -> str:
    """
    Scan a set of video descriptions (same channel) for an email address
    that recurs across at least EMAIL_MIN_VIDEO_REPEATS distinct videos —
    a much stronger "this is really their contact email" signal than a
    single mention. Returns the most-repeated qualifying email, or "" if
    none clears the threshold.
    """
    video_counter = Counter()
    for description in video_descriptions:
        if not description:
            continue
        # dedupe within a single video's description so one video that
        # happens to print the same address twice doesn't inflate its count
        emails_in_video = {
            e for e in set(EMAIL_PATTERN.findall(description))
            if not is_blocklisted_email_domain(e.rsplit("@", 1)[-1].lower())
        }
        video_counter.update(emails_in_video)

    if not video_counter:
        return ""

    email, count = video_counter.most_common(1)[0]
    return email if count >= EMAIL_MIN_VIDEO_REPEATS else ""


# Average days per month, for turning a channel's age into the "months"
# unit the briefs are written in. Approximate on purpose — nothing here
# depends on calendar-exact month boundaries.
DAYS_PER_MONTH = 30.44


def channel_age_months(published_at: str | None) -> float | None:
    """
    Age of a channel in months, from the ISO 8601 timestamp channels.list
    returns in snippet.publishedAt.

    Returns None when the value is missing or unparseable — callers must
    treat None as "unknown" and NOT as "new", since absent data is not
    evidence against a channel.
    """
    if not published_at:
        return None
    try:
        created = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        logger.info("Unparseable publishedAt %r — treating channel age as unknown.", published_at)
        return None
    delta_days = (datetime.now(timezone.utc) - created).days
    return delta_days / DAYS_PER_MONTH


def get_channel_stats(channel_id: str) -> dict | None:
    """
    Fetch subscriberCount, videoCount, viewCount, country, title, and
    the uploads playlist ID for a channel.

    Quota cost: 1 unit (channels.list with part=snippet,statistics,contentDetails).

    Returns None (and logs a warning) if the channel is private, deleted,
    or otherwise inaccessible, so callers can skip it without crashing.
    """
    params = {
        "part": "snippet,statistics,contentDetails",
        "id": channel_id,
        "key": YOUTUBE_API_KEY,
    }
    resp = requests.get(f"{YOUTUBE_API_BASE_URL}/channels", params=params, timeout=30)
    record_spend(QUOTA_COST_CHANNELS_LIST, call_name=f"channels.list({channel_id})")

    if resp.status_code != 200:
        logger.warning("channels.list failed for %s: %s %s", channel_id, resp.status_code, resp.text)
        return None

    items = resp.json().get("items", [])
    if not items:
        logger.warning("Channel %s not found (private, deleted, or terminated) — skipping.", channel_id)
        return None

    item = items[0]
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    content_details = item.get("contentDetails", {})
    uploads_playlist_id = content_details.get("relatedPlaylists", {}).get("uploads")

    if stats.get("hiddenSubscriberCount"):
        logger.info("Channel %s has a hidden subscriber count.", channel_id)

    return {
        "channel_id": channel_id,
        "channel_title": snippet.get("title"),
        "country": snippet.get("country", "Unknown"),
        # From the snippet already being fetched — no extra quota.
        "published_at": snippet.get("publishedAt", ""),
        "subscriber_count": int(stats.get("subscriberCount", 0)),
        "video_count": int(stats.get("videoCount", 0)),
        "view_count": int(stats.get("viewCount", 0)),
        "uploads_playlist_id": uploads_playlist_id,
        "description": snippet.get("description", ""),
        "business_email": extract_business_email(snippet.get("description", "")),
        # "" for legacy /c/ or /user/ channels that never set an @handle —
        # those can't be matched against the @handle-only external tables.
        "handle": normalize_handle(snippet.get("customUrl", "")),
    }


def get_recent_video_performance(
    channel_id: str,
    uploads_playlist_id: str,
    max_results: int = EMAIL_SCAN_SAMPLE_SIZE,
) -> dict | None:
    """
    Pull the last `max_results` videos from the channel's uploads playlist
    and fetch their stats.

    Two different window sizes come out of this one pair of calls:

    - Email scanning reads all `max_results` descriptions (default 50).
    - avg views / engagement rate / upload cadence are computed over only
      the newest PERFORMANCE_SAMPLE_SIZE (10) videos, so those figures —
      and the Overall Score built on them — stay comparable with records
      already in Airtable under the "Avg Views (last 10 videos)" column.

    Quota cost: 1 unit for playlistItems.list + 1 unit for videos.list
    (a single videos.list call handles up to 50 IDs at once, so this is
    always exactly 2 units regardless of max_results <= 50 — which is why
    scanning 50 descriptions instead of 10 is free).

    Returns None if the channel has no accessible uploads playlist (e.g.
    channel has zero public videos).
    """
    if not uploads_playlist_id:
        logger.warning("Channel %s has no uploads playlist — skipping video performance.", channel_id)
        return None

    params = {
        "part": "contentDetails",
        "playlistId": uploads_playlist_id,
        "maxResults": max_results,
        "key": YOUTUBE_API_KEY,
    }
    resp = requests.get(f"{YOUTUBE_API_BASE_URL}/playlistItems", params=params, timeout=30)
    record_spend(QUOTA_COST_PLAYLIST_ITEMS_LIST, call_name=f"playlistItems.list({channel_id})")

    if resp.status_code != 200:
        logger.warning("playlistItems.list failed for %s: %s %s", channel_id, resp.status_code, resp.text)
        return None

    payload = resp.json()
    items = payload.get("items", [])
    video_ids = [i["contentDetails"]["videoId"] for i in items if i.get("contentDetails", {}).get("videoId")]
    # playlistItems returns newest-first, so the leading slice is "the last
    # N videos" — the window upload cadence is measured over, matching the
    # performance window below rather than the wider email-scan window.
    upload_dates = [i["contentDetails"].get("videoPublishedAt") for i in items[:PERFORMANCE_SAMPLE_SIZE]]
    upload_dates = [d for d in upload_dates if d]

    if not video_ids:
        logger.warning("Channel %s uploads playlist has no videos — skipping video performance.", channel_id)
        return None

    time.sleep(API_SLEEP_SECONDS)

    # part=snippet adds no extra quota cost (videos.list is a flat 1 unit
    # regardless of parts requested) and lets us read defaultAudioLanguage,
    # a per-video signal for "Content Language" — channel-level language
    # isn't reliably exposed by the API at all.
    video_params = {
        # contentDetails adds `duration`, which is the only Shorts signal the
        # API offers. Free: videos.list is a flat 1 unit regardless of parts.
        "part": "snippet,statistics,contentDetails",
        "id": ",".join(video_ids),
        "key": YOUTUBE_API_KEY,
    }
    video_resp = requests.get(f"{YOUTUBE_API_BASE_URL}/videos", params=video_params, timeout=30)
    record_spend(QUOTA_COST_VIDEOS_LIST, call_name=f"videos.list({channel_id})")

    if video_resp.status_code != 200:
        logger.warning("videos.list failed for %s: %s %s", channel_id, video_resp.status_code, video_resp.text)
        return None

    video_items = video_resp.json().get("items", [])
    if not video_items:
        return None

    # Select the performance window by video ID rather than by slicing
    # video_items, since videos.list gives no ordering guarantee — the
    # authoritative "newest first" order is the playlistItems one.
    performance_ids = set(video_ids[:PERFORMANCE_SAMPLE_SIZE])

    total_views = 0
    total_engagements = 0  # likes + comments
    performance_count = 0
    content_language = ""
    video_descriptions = []
    durations = []
    for v in video_items:
        snippet = v.get("snippet", {})
        # Every fetched video feeds the email scan — that's the whole
        # point of pulling EMAIL_SCAN_SAMPLE_SIZE of them.
        video_descriptions.append(snippet.get("description", ""))
        # Shorts detection reads the WIDE window (every fetched video, up to
        # EMAIL_SCAN_SAMPLE_SIZE) rather than the 10-video performance
        # window: more videos means more chances to see a long-form upload,
        # which makes a false "Shorts-only" verdict less likely. That matters
        # because that verdict discards the channel outright.
        durations.append(v.get("contentDetails", {}).get("duration"))
        if not content_language:
            content_language = snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage") or ""

        # ...but only the newest PERFORMANCE_SAMPLE_SIZE feed the metrics.
        if v.get("id") not in performance_ids:
            continue
        stats = v.get("statistics", {})
        total_views += int(stats.get("viewCount", 0))
        total_engagements += int(stats.get("likeCount", 0)) + int(stats.get("commentCount", 0))
        performance_count += 1

    if not performance_count:
        logger.warning("Channel %s returned no videos in the performance window — skipping.", channel_id)
        return None

    avg_views = total_views / performance_count
    avg_engagement_rate = (total_engagements / total_views * 100) if total_views > 0 else 0.0

    return {
        "avg_views": avg_views,
        "avg_engagement_rate": avg_engagement_rate,
        "upload_dates": upload_dates,
        # Size of the *performance* window (still 10), not the email scan.
        "sample_size": performance_count,
        # Most creators never set this, so it's frequently "" (Unknown) —
        # best-effort only, not a guaranteed signal.
        "content_language": content_language,
        # True only if EVERY fetched video is <= SHORTS_MAX_SECONDS and none
        # had an unreadable duration. main.pre_push_drop_reason discards
        # these before any Airtable row is written.
        "shorts_only": is_shorts_only(durations),
        # An email seen in EMAIL_MIN_VIDEO_REPEATS+ of the sampled videos'
        # descriptions — a stronger signal than a single mention anywhere.
        "repeated_email": find_repeated_email(video_descriptions),
        "video_descriptions": video_descriptions,
        # Where the newest-EMAIL_SCAN_SAMPLE_SIZE window ended, so
        # scan_older_videos_for_email() can continue from here instead of
        # re-fetching page 1 (2 wasted units) to find the same place.
        # "" when this channel has no older uploads.
        "next_page_token": payload.get("nextPageToken", ""),
    }


def scan_older_videos_for_email(
    channel_id: str,
    uploads_playlist_id: str,
    page_token: str,
    known_descriptions: list[str],
    max_pages: int = EMAIL_DEEP_SCAN_PAGES,
) -> str:
    """
    Keep paging back through a channel's uploads, looking for a contact
    email that the newest-EMAIL_SCAN_SAMPLE_SIZE window didn't turn up.

    Step 3 of the email chain (see main.resolve_email). Only worth running
    when steps 1-2 found nothing: channels that pivoted to Shorts often
    have terse recent descriptions while their older long-form videos
    still carry a "business inquiries" line.

    `known_descriptions` are the descriptions already scanned by step 1.
    Older descriptions are ADDED to those rather than counted on their
    own, so a channel with two recent mentions plus one older mention
    clears EMAIL_MIN_VIDEO_REPEATS — while a single mention anywhere
    still doesn't, which is the same bar step 1 applies.

    Quota cost: 2 units per page fetched (playlistItems.list +
    videos.list), and zero when `page_token` is empty or max_pages is 0.
    Stops as soon as an email clears the threshold, so a channel that
    answers on the first extra page never pays for the second.

    Every failure is soft — a bad page returns "" rather than raising,
    matching the rest of this module.
    """
    if not uploads_playlist_id or not page_token or max_pages <= 0:
        return ""

    descriptions = list(known_descriptions)

    for _ in range(max_pages):
        params = {
            "part": "contentDetails",
            "playlistId": uploads_playlist_id,
            "maxResults": EMAIL_SCAN_SAMPLE_SIZE,
            "pageToken": page_token,
            "key": YOUTUBE_API_KEY,
        }
        resp = requests.get(f"{YOUTUBE_API_BASE_URL}/playlistItems", params=params, timeout=30)
        record_spend(QUOTA_COST_PLAYLIST_ITEMS_LIST, call_name=f"playlistItems.list({channel_id}, older)")

        if resp.status_code != 200:
            logger.info(
                "Older-uploads page failed for %s: %s — stopping the email scan here.",
                channel_id, resp.status_code,
            )
            return ""

        payload = resp.json()
        video_ids = [
            i["contentDetails"]["videoId"]
            for i in payload.get("items", [])
            if i.get("contentDetails", {}).get("videoId")
        ]
        if not video_ids:
            return ""

        time.sleep(API_SLEEP_SECONDS)

        video_params = {
            "part": "snippet",
            "id": ",".join(video_ids),
            "key": YOUTUBE_API_KEY,
        }
        video_resp = requests.get(f"{YOUTUBE_API_BASE_URL}/videos", params=video_params, timeout=30)
        record_spend(QUOTA_COST_VIDEOS_LIST, call_name=f"videos.list({channel_id}, older)")

        if video_resp.status_code != 200:
            logger.info(
                "Older-uploads videos.list failed for %s: %s — stopping the email scan here.",
                channel_id, video_resp.status_code,
            )
            return ""

        descriptions.extend(
            v.get("snippet", {}).get("description", "")
            for v in video_resp.json().get("items", [])
        )

        email = find_repeated_email(descriptions)
        if email:
            logger.info(
                "Found %s for %s after scanning %d descriptions (older uploads).",
                email, channel_id, len(descriptions),
            )
            return email

        page_token = payload.get("nextPageToken", "")
        if not page_token:
            return ""

        time.sleep(API_SLEEP_SECONDS)

    return ""


def calc_upload_frequency(upload_dates: list[str]) -> float:
    """
    Estimate videos-per-month upload cadence from a list of ISO 8601
    upload timestamps (as returned by playlistItems.list).

    Uses the span between the oldest and newest sampled video, so this is
    a local estimate over the sampled window (not full channel history).
    """
    if not upload_dates or len(upload_dates) < 2:
        return 0.0

    parsed = sorted(
        datetime.strptime(d, "%Y-%m-%dT%H:%M:%SZ") for d in upload_dates
    )
    span_days = (parsed[-1] - parsed[0]).days
    if span_days <= 0:
        # All sampled videos published on the same day; can't extrapolate a
        # monthly cadence from a zero-width window.
        return float(len(parsed))

    videos_per_day = len(parsed) / span_days
    return round(videos_per_day * 30, 2)
