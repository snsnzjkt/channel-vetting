"""
Tests for the TikTok + Instagram path.

The three properties worth pinning hardest, because each has a SILENT failure
mode rather than a loud one:

  1. Instagram handles survive normalisation. The YouTube normaliser returns ""
     for a bare username and `_to_candidate()` drops empty handles, so reusing
     it would have discarded every Instagram creator with no error at all.
  2. The vendor's follower count is carried on the social path and STILL
     dropped on the YouTube path. Getting this backwards either breaks every
     social gate (followers=0 rejects everyone) or lands purchased numbers in
     YouTube-labelled columns.
  3. An unmeasured creator is REJECTED, never admitted. This is what stops an
     exhausted posts budget from quietly lowering the bar to "has followers".
"""
from datetime import datetime, timedelta, timezone

import pytest

from channel_vetting import config
from channel_vetting.budget import credit_tracker
from channel_vetting.discovery.influencers_club import InfluencerDiscovery
from channel_vetting.social import criteria, discovery, pipeline, posts
from channel_vetting.social.handles import normalize_social_handle, profile_url

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def _account(username, full_name="Name", followers=50_000, engagement=4.5, user_id="u1"):
    return {
        "user_id": user_id,
        "profile": {
            "username": username,
            "full_name": full_name,
            "followers": followers,
            "engagement_percent": engagement,
        },
    }


def _post(days_ago, views=10_000, likes=400, comments=50, shares=30):
    return {
        "timestamp": (NOW - timedelta(days=days_ago)).isoformat(),
        "views": views,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "media_url": f"https://cdn.example/{days_ago}.jpg",
    }


# --- 1. handles ----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("petlover", "petlover"),               # bare — the YouTube normaliser returns ""
    ("@petlover", "petlover"),
    ("PetLover", "petlover"),
    ("https://www.instagram.com/petlover/", "petlover"),
    ("https://www.tiktok.com/@petlover", "petlover"),
    ("instagram.com/corgi.daily?igsh=x", "corgi.daily"),
    ("corgi.daily", "corgi.daily"),
])
def test_social_handles_survive_every_shape(raw, expected):
    assert normalize_social_handle(raw) == expected


@pytest.mark.parametrize("raw", [
    "",
    "https://www.instagram.com/",
    "https://www.instagram.com/p/Cabc123/",     # a POST, carries no handle
    "https://www.instagram.com/reel/Cxyz/",
    "https://www.tiktok.com/tag/dogsoftiktok",
])
def test_non_profile_urls_refuse_to_invent_a_handle(raw):
    """
    A shortcode is not a handle. Returning one would manufacture a
    plausible-looking creator we cannot identify, then dedupe and write it.
    """
    assert normalize_social_handle(raw) == ""


def test_tiktok_post_url_yields_the_handle_not_the_video_id():
    assert normalize_social_handle("https://www.tiktok.com/@corgi.daily/video/7123") == "corgi.daily"


def test_profile_url_is_machine_written():
    assert profile_url("tiktok", "@Corgi.Daily") == "https://www.tiktok.com/@corgi.daily"
    assert profile_url("instagram", "corgi.daily") == "https://www.instagram.com/corgi.daily/"
    assert profile_url("facebook", "x") == ""


# --- 2. vendor statistics, both directions -------------------------------

def test_youtube_path_still_drops_vendor_statistics():
    """Regression guard: the YouTube contract must not change."""
    client = InfluencerDiscovery(enabled=True)
    candidate = client._to_candidate(_account("@Chan"), "label")
    assert candidate["handle"] == "chan"
    assert "vendor_followers" not in candidate
    assert "followers" not in candidate
    assert "engagement_percent" not in candidate


def test_social_path_carries_vendor_followers():
    """
    Without this the social gates see followers=0 and reject everyone, because
    no free API reports a TikTok or Instagram follower count.
    """
    client = discovery.client_for_run(enabled=True)
    candidate = client._to_candidate(_account("petlover", followers=42_000), "label")
    assert candidate["handle"] == "petlover"
    assert candidate["vendor_followers"] == 42_000


# --- 3. the criteria arithmetic ------------------------------------------

def test_engagement_is_per_view_on_tiktok_and_per_follower_on_instagram():
    """
    "TikTok and Instagram can't share a number." Same interactions, same
    account, different denominators — so the rates must differ.
    """
    tiktok = criteria.engagement_rate(
        "tiktok", interactions=500, views=10_000, followers=100_000
    )
    instagram = criteria.engagement_rate(
        "instagram", interactions=500, views=10_000, followers=100_000
    )
    assert tiktok == pytest.approx(0.05)
    assert instagram == pytest.approx(0.005)


def test_unknown_denominator_is_none_not_zero():
    """Zero fails every floor, so it would silently reject rather than flag."""
    assert criteria.engagement_rate("tiktok", interactions=500, views=0, followers=9) is None


def test_median_not_mean_so_one_viral_post_cannot_carry_an_account():
    views = [1_000, 1_000, 1_000, 1_000, 500_000]
    assert criteria.median_views(views) == 1_000          # median
    assert sum(views) / len(views) > 100_000              # the mean would pass


def test_median_ignores_unmeasured_posts_rather_than_scoring_them_zero():
    assert criteria.median_views([10_000, None, 12_000]) == 11_000


def test_engagement_floors_follow_the_draft_per_band():
    assert criteria.engagement_floor("tiktok", 5_000) == 0.040
    assert criteria.engagement_floor("tiktok", 900_000) == 0.025
    assert criteria.engagement_floor("instagram", 5_000) == 0.030
    assert criteria.engagement_floor("instagram", 900_000) == 0.010


def test_auto_score_reports_its_own_maximum_not_the_rubric_maximum():
    """
    The draft's rubric is 100 with a pass at 65, and only 35 points of it are
    computable. auto_score must never look like the finished rubric.
    """
    metrics = posts.metrics_from_items([_post(d) for d in range(0, 21, 3)], now=NOW)
    points, out_of = criteria.auto_score("tiktok", followers=50_000, metrics=metrics)
    assert out_of == criteria.AUTO_SCORE_MAX == 35
    assert out_of < criteria.RUBRIC_PASS_MARK < criteria.RUBRIC_MAX
    assert points <= out_of


# --- 4. rejection order --------------------------------------------------

def test_unmeasured_creator_is_rejected_never_admitted():
    reason = criteria.auto_reject_reason("tiktok", followers=50_000, metrics=None)
    assert reason == criteria.REASON_UNMEASURED


def test_below_follower_minimum_is_checked_before_anything_is_measured():
    reason = criteria.auto_reject_reason("tiktok", followers=10, metrics=None)
    assert reason == criteria.REASON_FOLLOWERS


def test_a_healthy_creator_clears_every_checkable_gate():
    metrics = posts.metrics_from_items([_post(d) for d in range(0, 21, 2)], now=NOW)
    monkey_now = metrics.days_since_last_post
    assert monkey_now == 0
    assert criteria.auto_reject_reason("tiktok", followers=50_000, metrics=metrics) is None


def test_stale_account_is_rejected_on_recency():
    metrics = posts.metrics_from_items(
        [_post(200), _post(210), _post(220)], now=NOW
    )
    assert criteria.auto_reject_reason("tiktok", followers=50_000, metrics=metrics) == (
        criteria.REASON_STALE
    )


# --- 5. the budget floor -------------------------------------------------

def test_platform_aborts_rather_than_screening_too_few(monkeypatch):
    """
    THE QUALITY FLOOR. Under-funding must abort, not quietly admit creators
    judged on follower count alone — a reviewer cannot tell those apart.
    """
    monkeypatch.setattr(config, "AIRTABLE_TABLE_SOCIAL_CREATORS", "Creators")
    monkeypatch.setattr(config, "SOCIAL_MAX_POSTS_CREDITS_PER_RUN", 0.06)  # 2 screens
    monkeypatch.setattr(config, "SOCIAL_MIN_POSTS_SCREENS_PER_RUN", 10)

    result = pipeline.run_platform("tiktok")

    assert result.aborted
    assert "below the SOCIAL_MIN_POSTS_SCREENS_PER_RUN floor" in result.aborted
    assert result.screened == 0
    assert result.admitted == 0
    # And nothing was bought on the way to finding out.
    assert credit_tracker.credits_today() == 0


def test_missing_destination_table_aborts_before_spending(monkeypatch):
    monkeypatch.setattr(config, "AIRTABLE_TABLE_SOCIAL_CREATORS", None)
    result = pipeline.run_platform("instagram")
    assert "AIRTABLE_TABLE_SOCIAL_CREATORS" in result.aborted
    assert credit_tracker.credits_today() == 0


# --- 6. end to end, with the network mocked out --------------------------

def test_run_platform_admits_screened_creators_and_charges_the_shared_ledger(monkeypatch):
    monkeypatch.setattr(config, "AIRTABLE_TABLE_SOCIAL_CREATORS", "Creators")
    monkeypatch.setattr(config, "SOCIAL_TARGET_PER_PLATFORM", 2)
    monkeypatch.setattr(pipeline, "get_tracked_handles", lambda table: set())
    monkeypatch.setattr(pipeline, "fetch_blocklist", lambda: None)

    candidates = [
        {"handle": "goodpet", "channel_title": "Good Pet", "influencers_user_id": "u1",
         "vendor_followers": 50_000, "matched_keywords": ["lane"]},
        {"handle": "tinypet", "channel_title": "Tiny Pet", "influencers_user_id": "u2",
         "vendor_followers": 40, "matched_keywords": ["lane"]},
    ]
    calls = {"discover": 0, "posts": 0, "writes": []}

    def fake_discover(platform, *, lane, target, exclude_handles, client):
        calls["discover"] += 1
        return candidates if calls["discover"] == 1 else []

    def fake_fetch_metrics(platform, handle, **kw):
        calls["posts"] += 1
        credit_tracker.record_spend(
            config.SOCIAL_POSTS_CREDITS_PER_REQUEST,
            kind=credit_tracker.KIND_DISCOVERY, detail="test posts",
        )
        return posts.metrics_from_items([_post(d) for d in range(0, 21, 2)], now=NOW)

    monkeypatch.setattr(pipeline.discovery, "discover", fake_discover)
    monkeypatch.setattr(pipeline.posts, "fetch_metrics", fake_fetch_metrics)
    monkeypatch.setattr(
        pipeline, "_create_row",
        lambda table, fields: calls["writes"].append(fields) or True,
    )

    result = pipeline.run_platform("tiktok")

    assert result.admitted == 1
    assert result.rejections.get(criteria.REASON_FOLLOWERS) == 1
    written = calls["writes"][0]
    assert written["Handle"] == "goodpet"
    assert written["Primary Platform"] == "TikTok"
    # Never pre-approved: four auto-reject rules have no purchasable answer.
    assert written["Review Decision"] == "Pending"
    assert "STILL NEEDS A HUMAN" in written["Notes"]
    # The shared ledger recorded the screens, so the daily/monthly ceilings see them.
    assert credit_tracker.credits_today() == pytest.approx(
        calls["posts"] * config.SOCIAL_POSTS_CREDITS_PER_REQUEST
    )


def test_do_not_contact_blocks_before_any_screen_is_bought(monkeypatch):
    monkeypatch.setattr(config, "AIRTABLE_TABLE_SOCIAL_CREATORS", "Creators")
    monkeypatch.setattr(pipeline, "get_tracked_handles", lambda table: set())

    class Blocked:
        def match(self, handle="", email="", name=""):
            return "handle @blockedpet" if handle == "blockedpet" else ""

    monkeypatch.setattr(pipeline, "fetch_blocklist", lambda: Blocked())
    monkeypatch.setattr(
        pipeline.discovery, "discover",
        lambda platform, *, lane, target, exclude_handles, client: (
            [{"handle": "blockedpet", "channel_title": "B", "vendor_followers": 50_000}]
            if lane["priority"] == 1 else []
        ),
    )
    spent = {"posts": 0}
    monkeypatch.setattr(
        pipeline.posts, "fetch_metrics",
        lambda *a, **k: spent.__setitem__("posts", spent["posts"] + 1),
    )

    result = pipeline.run_platform("tiktok")

    assert result.blocked == 1
    assert result.screened == 0
    assert spent["posts"] == 0, "a suppressed creator must not cost a screen"


# --- 7. the account tables ----------------------------------------------

def test_account_row_records_median_and_mean_in_separate_columns():
    """
    Both, never one. On a creator with a viral post they differ by orders of
    magnitude, so collapsing them would make one column label a lie — and the
    median is the figure the gates actually used.
    """
    flat = [_post(d, views=10_000) for d in range(2, 20, 2)]
    viral = flat[:-1] + [_post(0, views=5_000_000)]
    metrics = posts.metrics_from_items(viral, now=NOW)

    row = pipeline._account_record("tiktok", "corgi.daily", 50_000, metrics, "recABC")

    assert row["Median Views (last 10)"] == 10_000
    assert row["Avg Views per Video"] > 500_000
    assert row["Median Views (last 10)"] != row["Avg Views per Video"]
    assert row["Posts Sampled"] == metrics.sample_size


def test_account_row_uses_each_platforms_own_engagement_column():
    metrics = posts.metrics_from_items([_post(d) for d in range(0, 20, 2)], now=NOW)

    tiktok = pipeline._account_record("tiktok", "h", 50_000, metrics, "recA")
    instagram = pipeline._account_record("instagram", "h", 50_000, metrics, "recA")

    assert "Engagement Rate per View" in tiktok
    assert "Engagement Rate per Follower" not in tiktok
    assert "Engagement Rate per Follower" in instagram
    assert "Engagement Rate per View" not in instagram
    # Different denominators, so the two rates must not be equal.
    assert tiktok["Engagement Rate per View"] != instagram["Engagement Rate per Follower"]
    # Instagram's views are Reel plays, and it has no per-video share column.
    assert "Avg Reel Plays" in instagram
    assert "Avg Shares per Video" not in instagram


def test_account_link_is_an_array_of_record_ids():
    """
    A bare string in a link field is typecast as a NAME to match, which would
    create a junk Creators row instead of linking to the one just written.
    """
    metrics = posts.metrics_from_items([_post(d) for d in range(0, 20, 2)], now=NOW)
    row = pipeline._account_record("tiktok", "h", 50_000, metrics, "recABC123")
    assert row["Creators"] == ["recABC123"]


def test_account_row_omits_the_link_when_the_creator_id_is_unknown():
    """Unlinked measurements still beat no measurements."""
    metrics = posts.metrics_from_items([_post(d) for d in range(0, 20, 2)], now=NOW)
    row = pipeline._account_record("tiktok", "h", 50_000, metrics, "")
    assert "Creators" not in row


def test_account_row_leaves_lifetime_and_unmeasured_fields_blank():
    """
    A blank cell reads as unknown; a zero or a nearest-guess reads as measured.
    "Total Likes" and "Posts Count" are LIFETIME account figures, and this
    window is 10 posts.
    """
    metrics = posts.metrics_from_items([_post(d) for d in range(0, 20, 2)], now=NOW)
    tiktok = pipeline._account_record("tiktok", "h", 50_000, metrics, "recA")
    instagram = pipeline._account_record("instagram", "h", 50_000, metrics, "recA")

    for absent in ("Total Likes", "Verified", "Region", "Language", "Bio"):
        assert absent not in tiktok
    for absent in ("Posts Count", "Following", "Account Type", "Verified"):
        assert absent not in instagram


def test_screened_at_is_set_even_when_no_post_carried_a_timestamp():
    metrics = posts.PostMetrics(
        measured=True, sample_size=4, median_views=9_000, total_views=36_000,
        views_sample_size=4, total_likes=200, total_comments=20, total_shares=10,
        total_interactions=230,
    )
    row = pipeline._account_record("tiktok", "h", 20_000, metrics, "recA")
    assert "Screened At" in row
    assert "Last Posted" not in row      # genuinely unknown
    assert "Posts per Week" not in row   # genuinely unknown


def test_run_platform_writes_a_creator_row_and_a_linked_account_row(monkeypatch):
    monkeypatch.setattr(config, "AIRTABLE_TABLE_SOCIAL_CREATORS", "Creators")
    monkeypatch.setattr(config, "AIRTABLE_TABLE_SOCIAL_TIKTOK", "TikTok Accounts")
    monkeypatch.setattr(config, "SOCIAL_TARGET_PER_PLATFORM", 1)
    monkeypatch.setattr(pipeline, "get_tracked_handles", lambda table: set())
    monkeypatch.setattr(pipeline, "fetch_blocklist", lambda: None)
    monkeypatch.setattr(
        pipeline.discovery, "discover",
        lambda platform, *, lane, target, exclude_handles, client: (
            [{"handle": "corgi.daily", "channel_title": "Corgi Daily",
              "influencers_user_id": "u1", "vendor_followers": 50_000}]
            if lane["priority"] == 1 else []
        ),
    )
    monkeypatch.setattr(
        pipeline.posts, "fetch_metrics",
        lambda *a, **k: posts.metrics_from_items([_post(d) for d in range(0, 20, 2)], now=NOW),
    )
    writes = []

    def fake_create(table, fields):
        writes.append((table, fields))
        return "recCREATOR" if table == "Creators" else "recACCOUNT"

    monkeypatch.setattr(pipeline, "_create_row", fake_create)

    result = pipeline.run_platform("tiktok")

    assert result.admitted == 1
    assert result.accounts_written == 1
    assert [t for t, _ in writes] == ["Creators", "TikTok Accounts"]
    account_fields = writes[1][1]
    assert account_fields["Creators"] == ["recCREATOR"], "must link to the row just written"
    assert account_fields["Handle"] == "corgi.daily"


def test_account_write_failure_does_not_undo_the_creator_row(monkeypatch):
    """
    A creator row with no account row is degraded but correct — the reviewer
    reads the numbers from Notes. It must not be counted as a lost prospect.
    """
    monkeypatch.setattr(config, "AIRTABLE_TABLE_SOCIAL_CREATORS", "Creators")
    monkeypatch.setattr(config, "AIRTABLE_TABLE_SOCIAL_TIKTOK", "TikTok Accounts")
    monkeypatch.setattr(config, "SOCIAL_TARGET_PER_PLATFORM", 1)
    monkeypatch.setattr(pipeline, "get_tracked_handles", lambda table: set())
    monkeypatch.setattr(pipeline, "fetch_blocklist", lambda: None)
    monkeypatch.setattr(
        pipeline.discovery, "discover",
        lambda platform, *, lane, target, exclude_handles, client: (
            [{"handle": "corgi.daily", "channel_title": "C", "vendor_followers": 50_000}]
            if lane["priority"] == 1 else []
        ),
    )
    monkeypatch.setattr(
        pipeline.posts, "fetch_metrics",
        lambda *a, **k: posts.metrics_from_items([_post(d) for d in range(0, 20, 2)], now=NOW),
    )
    monkeypatch.setattr(
        pipeline, "_create_row",
        lambda table, fields: "recCREATOR" if table == "Creators" else None,
    )

    result = pipeline.run_platform("tiktok")

    assert result.admitted == 1
    assert result.account_failures == 1
    assert result.accounts_written == 0


def test_unconfigured_account_table_is_skipped_not_failed(monkeypatch):
    monkeypatch.setattr(config, "AIRTABLE_TABLE_SOCIAL_CREATORS", "Creators")
    monkeypatch.setattr(config, "AIRTABLE_TABLE_SOCIAL_TIKTOK", None)
    monkeypatch.setattr(config, "SOCIAL_TARGET_PER_PLATFORM", 1)
    monkeypatch.setattr(pipeline, "get_tracked_handles", lambda table: set())
    monkeypatch.setattr(pipeline, "fetch_blocklist", lambda: None)
    monkeypatch.setattr(
        pipeline.discovery, "discover",
        lambda platform, *, lane, target, exclude_handles, client: (
            [{"handle": "corgi.daily", "channel_title": "C", "vendor_followers": 50_000}]
            if lane["priority"] == 1 else []
        ),
    )
    monkeypatch.setattr(
        pipeline.posts, "fetch_metrics",
        lambda *a, **k: posts.metrics_from_items([_post(d) for d in range(0, 20, 2)], now=NOW),
    )
    monkeypatch.setattr(pipeline, "_create_row", lambda table, fields: "recX")

    result = pipeline.run_platform("tiktok")

    assert result.admitted == 1
    assert result.accounts_skipped == 1
    assert result.account_failures == 0
    assert "table not configured" in result.summary()
