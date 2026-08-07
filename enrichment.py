"""
Enriches discovered channels with real stats via channels.list,
playlistItems.list, and videos.list.
"""
import logging
import re
import time
from collections import Counter
from datetime import datetime

import requests

from config import (
    YOUTUBE_API_BASE_URL,
    YOUTUBE_API_KEY,
    QUOTA_COST_CHANNELS_LIST,
    QUOTA_COST_PLAYLIST_ITEMS_LIST,
    QUOTA_COST_VIDEOS_LIST,
    API_SLEEP_SECONDS,
)
from quota_tracker import record_spend

logger = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
URL_PATTERN = re.compile(r"https?://([a-zA-Z0-9.\-]+)", re.IGNORECASE)

# A one-off email mention in a single video's description is weak evidence
# (could be a shoutout, a giveaway, someone else's contact, etc.). Seeing
# the same address repeated across several videos' descriptions is a much
# stronger signal it's the creator's actual standing contact email.
EMAIL_MIN_VIDEO_REPEATS = 3

# Shared platforms/link-aggregators are never the creator's own business
# domain — running a Hunter.io Domain Search against these would return
# that platform's corporate emails, not the creator's own.
DOMAIN_BLOCKLIST = {
    "youtube.com", "youtu.be", "instagram.com", "tiktok.com", "twitter.com",
    "x.com", "facebook.com", "fb.com", "linktr.ee", "linktree.com",
    "beacons.ai", "beacons.page", "discord.gg", "discord.com", "patreon.com",
    "twitch.tv", "amzn.to", "amazon.com", "bit.ly", "goo.gl", "linkedin.com",
    "threads.net", "snapchat.com", "pinterest.com", "reddit.com", "gmail.com",
    "google.com", "apple.com", "spotify.com",
}


def extract_candidate_domain(text: str) -> str:
    """
    Best-effort extraction of what looks like the creator's own website
    domain from free text (channel/video descriptions), for use as a
    Hunter.io Domain Search query. Skips known social/link-aggregator
    platforms, which are never the creator's own business domain.
    """
    if not text:
        return ""
    for match in URL_PATTERN.finditer(text):
        domain = match.group(1).lower()
        if domain.startswith("www."):
            domain = domain[4:]
        if domain and domain not in DOMAIN_BLOCKLIST:
            return domain
    return ""


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
    match = EMAIL_PATTERN.search(description)
    return match.group(0) if match else ""


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
        emails_in_video = set(EMAIL_PATTERN.findall(description))
        video_counter.update(emails_in_video)

    if not video_counter:
        return ""

    email, count = video_counter.most_common(1)[0]
    return email if count >= EMAIL_MIN_VIDEO_REPEATS else ""


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
        "subscriber_count": int(stats.get("subscriberCount", 0)),
        "video_count": int(stats.get("videoCount", 0)),
        "view_count": int(stats.get("viewCount", 0)),
        "uploads_playlist_id": uploads_playlist_id,
        "description": snippet.get("description", ""),
        "business_email": extract_business_email(snippet.get("description", "")),
    }


def get_recent_video_performance(channel_id: str, uploads_playlist_id: str, max_results: int = 10) -> dict | None:
    """
    Pull the last `max_results` videos from the channel's uploads playlist
    and fetch their stats to compute avg views, avg engagement rate, and
    upload dates.

    Quota cost: 1 unit for playlistItems.list + 1 unit for videos.list
    (a single videos.list call handles up to 50 IDs at once, so this is
    always exactly 2 units regardless of max_results <= 50).

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

    items = resp.json().get("items", [])
    video_ids = [i["contentDetails"]["videoId"] for i in items if i.get("contentDetails", {}).get("videoId")]
    upload_dates = [i["contentDetails"].get("videoPublishedAt") for i in items]
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
        "part": "snippet,statistics",
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

    total_views = 0
    total_engagements = 0  # likes + comments
    content_language = ""
    video_descriptions = []
    for v in video_items:
        stats = v.get("statistics", {})
        total_views += int(stats.get("viewCount", 0))
        total_engagements += int(stats.get("likeCount", 0)) + int(stats.get("commentCount", 0))

        snippet = v.get("snippet", {})
        video_descriptions.append(snippet.get("description", ""))
        if not content_language:
            content_language = snippet.get("defaultAudioLanguage") or snippet.get("defaultLanguage") or ""

    n = len(video_items)
    avg_views = total_views / n
    avg_engagement_rate = (total_engagements / total_views * 100) if total_views > 0 else 0.0

    return {
        "avg_views": avg_views,
        "avg_engagement_rate": avg_engagement_rate,
        "upload_dates": upload_dates,
        "sample_size": n,
        # Most creators never set this, so it's frequently "" (Unknown) —
        # best-effort only, not a guaranteed signal.
        "content_language": content_language,
        # An email seen in EMAIL_MIN_VIDEO_REPEATS+ of the sampled videos'
        # descriptions — a stronger signal than a single mention anywhere.
        "repeated_email": find_repeated_email(video_descriptions),
        "video_descriptions": video_descriptions,
    }


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
