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


# Captions matter now: pet content is a hard requirement, so a fixture with no
# caption is correctly rejected as pet_content_unknown_no_captions.
_PET_CAPTIONS = (
    "my corgi being dramatic again #dogsoftiktok",
    "vet day for the pup",
    "she found the zoomies",
    "a very tired doggo",
    "breakfast with my dog",
)


def _post(days_ago, views=10_000, likes=400, comments=50, shares=30, caption=None):
    return {
        "timestamp": (NOW - timedelta(days=days_ago)).isoformat(),
        "views": views,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "media_url": f"https://cdn.example/{days_ago}.jpg",
        "caption": caption if caption is not None else _PET_CAPTIONS[days_ago % len(_PET_CAPTIONS)],
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


# --- 5. the budget floor and the daily cap -------------------------------

def _configure(monkeypatch, *, tiktok="TikTok – Prospects", instagram="Instagram – Prospects"):
    monkeypatch.setattr(config, "AIRTABLE_TABLE_TIKTOK_PROSPECTS", tiktok)
    monkeypatch.setattr(config, "AIRTABLE_TABLE_INSTAGRAM_PROSPECTS", instagram)
    monkeypatch.setattr(pipeline.airtable, "count_added_today", lambda *a, **k: 0)
    monkeypatch.setattr(pipeline, "get_tracked_handles", lambda table: set())
    monkeypatch.setattr(pipeline, "fetch_blocklist", lambda: None)


def _one_candidate(handle="corgi.daily", followers=50_000):
    return lambda platform, *, lane, target, exclude_handles, client: (
        [{"handle": handle, "channel_title": "Corgi Daily",
          "influencers_user_id": "u1", "vendor_followers": followers}]
        if lane["priority"] == 1 else []
    )


def _healthy_metrics(*a, **k):
    return posts.metrics_from_items([_post(d) for d in range(0, 20, 2)], now=NOW)


def test_platform_aborts_rather_than_screening_too_few(monkeypatch):
    """
    THE QUALITY FLOOR. Under-funding must abort, not quietly admit creators
    judged on follower count alone — a reviewer cannot tell those apart.
    """
    _configure(monkeypatch)
    monkeypatch.setattr(config, "SOCIAL_MAX_POSTS_CREDITS_PER_RUN", 0.06)  # 2 screens
    monkeypatch.setattr(config, "SOCIAL_MIN_POSTS_SCREENS_PER_RUN", 10)

    result = pipeline.run_platform("tiktok")

    assert result.aborted
    assert "below the SOCIAL_MIN_POSTS_SCREENS_PER_RUN floor" in result.aborted
    assert result.screened == 0 and result.admitted == 0
    assert credit_tracker.credits_today() == 0, "nothing bought on the way to finding out"


def test_missing_destination_table_aborts_before_spending(monkeypatch):
    _configure(monkeypatch, tiktok=None)
    result = pipeline.run_platform("tiktok")
    assert "AIRTABLE_TABLE_TIKTOK_PROSPECTS" in result.aborted
    assert credit_tracker.credits_today() == 0


def test_daily_cap_is_counted_from_the_destination_table(monkeypatch):
    """Same knob and same accounting as the YouTube niches."""
    _configure(monkeypatch)
    monkeypatch.setattr(config, "DAILY_QUALIFIED_CAP", 5)
    monkeypatch.setattr(pipeline.airtable, "count_added_today", lambda *a, **k: 5)

    result = pipeline.run_platform("tiktok")

    assert "daily cap reached: 5/5" in result.aborted
    assert credit_tracker.credits_today() == 0


def test_unreadable_cap_aborts_rather_than_assuming_empty(monkeypatch):
    """A cap we cannot read must never be assumed empty."""
    _configure(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("airtable down")

    monkeypatch.setattr(pipeline.airtable, "count_added_today", boom)
    result = pipeline.run_platform("tiktok")
    assert "could not read today's row count" in result.aborted
    assert credit_tracker.credits_today() == 0


def test_cap_headroom_limits_the_target(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(config, "DAILY_QUALIFIED_CAP", 60)
    monkeypatch.setattr(pipeline.airtable, "count_added_today", lambda *a, **k: 59)
    monkeypatch.setattr(pipeline.discovery, "discover", _one_candidate())
    monkeypatch.setattr(pipeline.posts, "fetch_metrics", _healthy_metrics)
    writes = []
    monkeypatch.setattr(pipeline, "_create_row",
                        lambda table, fields: writes.append((table, fields)) or "recX")

    result = pipeline.run_platform("tiktok", target=10)

    assert result.admitted == 1, "one row of headroom left, so one row admitted"


# --- 6. the prospect row -------------------------------------------------

def test_prospect_row_records_median_and_mean_in_separate_columns():
    """
    Both, never one. On a creator with a viral post they differ by orders of
    magnitude, so collapsing them would make one column label a lie — and the
    median is the figure the gates actually used.
    """
    flat = [_post(d, views=10_000) for d in range(2, 20, 2)]
    metrics = posts.metrics_from_items(flat[:-1] + [_post(0, views=5_000_000)], now=NOW)
    candidate = {"handle": "corgi.daily", "channel_title": "Corgi Daily"}

    row = pipeline._prospect_record("tiktok", candidate, 50_000, metrics, "pet_breed")

    assert row["Median Views (last 10)"] == 10_000
    assert row["Avg Views per Post"] > 500_000
    assert row["Posts Sampled"] == metrics.sample_size
    assert row["Lane"] == "pet_breed"


def test_prospect_row_uses_each_platforms_own_engagement_column():
    metrics = _healthy_metrics()
    candidate = {"handle": "h", "channel_title": "H"}

    tiktok = pipeline._prospect_record("tiktok", candidate, 50_000, metrics)
    instagram = pipeline._prospect_record("instagram", candidate, 50_000, metrics)

    assert "Engagement Rate (per view)" in tiktok
    assert "Engagement Rate (per follower)" not in tiktok
    assert "Engagement Rate (per follower)" in instagram
    assert "Engagement Rate (per view)" not in instagram
    assert (tiktok["Engagement Rate (per view)"]
            != instagram["Engagement Rate (per follower)"])
    # Instagram's views are Reel plays, and it has no shares column.
    assert "Avg Reel Plays" in instagram and "Avg Shares per Post" not in instagram
    assert "Avg Views per Post" in tiktok and "Avg Shares per Post" in tiktok


def test_prospect_row_marks_the_human_gates_as_unchecked_not_blank():
    """
    A blank cell in a GATE column reads as passed. The draft calls photo
    quality "a gate", so an unreviewed row must look unreviewed.
    """
    row = pipeline._prospect_record(
        "tiktok", {"handle": "h", "channel_title": "H"}, 50_000, _healthy_metrics()
    )
    assert row["Subject Check"] == "Not checked"
    assert row["Photo Quality"] == "Not checked"
    assert row["Status"] == "New"
    assert "STILL NEEDS A HUMAN" in row["Notes"]


def test_prospect_row_leaves_email_and_unmeasured_fields_out():
    """
    Contact enrichment is 0.2 credits and deferred until a human approves. A
    blank cell reads as unknown; a zero reads as measured.
    """
    metrics = posts.PostMetrics(
        measured=True, sample_size=4, median_views=9_000, total_views=36_000,
        views_sample_size=4, total_likes=200, total_comments=20, total_shares=10,
        total_interactions=230,
    )
    row = pipeline._prospect_record("tiktok", {"handle": "h", "channel_title": "H"},
                                    20_000, metrics)
    assert "Email" not in row
    assert "Last Posted" not in row      # genuinely unknown
    assert "Posts per Week" not in row   # genuinely unknown
    assert "Screened At" in row, "the screen ran and was paid for regardless"


def test_auto_score_column_is_labelled_out_of_35_not_100():
    row = pipeline._prospect_record(
        "tiktok", {"handle": "h", "channel_title": "H"}, 50_000, _healthy_metrics()
    )
    assert "Auto Score (of 35)" in row
    assert row["Auto Score (of 35)"] <= criteria.AUTO_SCORE_MAX == 35


# --- 7. end to end, with the network mocked out --------------------------

def test_run_platform_writes_one_prospect_row_per_creator(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(config, "SOCIAL_TARGET_PER_PLATFORM", 2)
    candidates = [
        {"handle": "goodpet", "channel_title": "Good Pet", "influencers_user_id": "u1",
         "vendor_followers": 50_000},
        {"handle": "tinypet", "channel_title": "Tiny Pet", "influencers_user_id": "u2",
         "vendor_followers": 40},
    ]
    calls = {"n": 0, "posts": 0}

    def fake_discover(platform, *, lane, target, exclude_handles, client):
        calls["n"] += 1
        return candidates if calls["n"] == 1 else []

    def fake_metrics(platform, handle, **kw):
        calls["posts"] += 1
        credit_tracker.record_spend(
            config.SOCIAL_POSTS_CREDITS_PER_REQUEST,
            kind=credit_tracker.KIND_DISCOVERY, detail="test posts",
        )
        return _healthy_metrics()

    writes = []
    monkeypatch.setattr(pipeline.discovery, "discover", fake_discover)
    monkeypatch.setattr(pipeline.posts, "fetch_metrics", fake_metrics)
    monkeypatch.setattr(pipeline, "_create_row",
                        lambda table, fields: writes.append((table, fields)) or "recX")

    result = pipeline.run_platform("tiktok")

    assert result.admitted == 1
    assert result.rejections.get(criteria.REASON_FOLLOWERS) == 1
    assert len(writes) == 1, "one row per creator, not two"
    table, fields = writes[0]
    assert table == "TikTok – Prospects"
    assert fields["Handle"] == "goodpet"
    assert fields["Qualification"] == "Qualified"
    # The shared ledger recorded the screens, so the daily/monthly caps see them.
    assert credit_tracker.credits_today() == pytest.approx(
        calls["posts"] * config.SOCIAL_POSTS_CREDITS_PER_REQUEST
    )


def test_instagram_rows_go_to_the_instagram_table(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(pipeline.discovery, "discover", _one_candidate())
    monkeypatch.setattr(pipeline.posts, "fetch_metrics", _healthy_metrics)
    writes = []
    monkeypatch.setattr(pipeline, "_create_row",
                        lambda table, fields: writes.append((table, fields)) or "recX")

    pipeline.run_platform("instagram")

    assert writes[0][0] == "Instagram – Prospects"
    assert "Engagement Rate (per follower)" in writes[0][1]


def test_write_failure_is_counted_not_admitted(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(pipeline.discovery, "discover", _one_candidate())
    monkeypatch.setattr(pipeline.posts, "fetch_metrics", _healthy_metrics)
    monkeypatch.setattr(pipeline, "_create_row", lambda table, fields: None)

    result = pipeline.run_platform("tiktok")

    assert result.admitted == 0
    assert result.write_failures == 1


def test_do_not_contact_blocks_before_any_screen_is_bought(monkeypatch):
    _configure(monkeypatch)

    class Blocked:
        def match(self, handle="", email="", name=""):
            return "handle @blockedpet" if handle == "blockedpet" else ""

    monkeypatch.setattr(pipeline, "fetch_blocklist", lambda: Blocked())
    monkeypatch.setattr(pipeline.discovery, "discover", _one_candidate("blockedpet"))
    spent = {"posts": 0}
    monkeypatch.setattr(pipeline.posts, "fetch_metrics",
                        lambda *a, **k: spent.__setitem__("posts", spent["posts"] + 1))

    result = pipeline.run_platform("tiktok")

    assert result.blocked == 1
    assert result.screened == 0
    assert spent["posts"] == 0, "a suppressed creator must not cost a screen"


def test_dry_run_screens_but_writes_nothing(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(pipeline.discovery, "discover", _one_candidate())
    monkeypatch.setattr(pipeline.posts, "fetch_metrics", _healthy_metrics)
    writes = []
    monkeypatch.setattr(pipeline, "_create_row",
                        lambda table, fields: writes.append(fields) or "recX")

    result = pipeline.run_platform("tiktok", dry_run=True)

    assert result.admitted == 1
    assert writes == [], "dry run must not write"


# --- 8. social DO NOT CONTACT --------------------------------------------
#
# The first live run died here: fetch_blocklist() is pinned to Valencia by a
# hardcoded table id, by field IDs, and by treating zero rows as a failure. Each
# of those is pinned below so the social reader cannot regress into any of them.

from channel_vetting.airtable.do_not_contact import Blocklist, BlocklistUnavailable
from channel_vetting.social import suppression


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = {} if body is None else body

    def json(self):
        return self._body


def _dnc_row(**fields):
    return {"fields": fields}


def test_social_dnc_reads_field_names_not_valencia_field_ids(monkeypatch):
    """
    The Valencia reader asks for `returnFieldsByFieldId` with Valencia field
    IDs. Against another base those ids do not exist, so it would index ZERO
    handles while reporting success — a silent failure, and the worse one.
    """
    seen = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen["params"] = params
        return _Resp(body={"records": [
            _dnc_row(**{"Handle": "@BlockedPet", "Email": "X@Example.com",
                        "Creator Name": "Blocked Pet"}),
        ]})

    monkeypatch.setattr(suppression.HTTP, "get", fake_get)
    blocklist = suppression.fetch_social_blocklist("DO NOT CONTACT")

    assert "returnFieldsByFieldId" not in seen["params"]
    assert seen["params"]["fields[]"] == [
        "Creator Name", "Handle", "Profile URL", "Email",
    ]
    assert blocklist.handles == {"blockedpet"}
    assert blocklist.emails == {"x@example.com"}
    assert blocklist.names == {"blocked pet"}


def test_social_dnc_indexes_handles_from_both_columns(monkeypatch):
    """A row may carry only the bare handle, only a profile URL, or both."""
    monkeypatch.setattr(suppression.HTTP, "get", lambda *a, **k: _Resp(body={"records": [
        _dnc_row(**{"Handle": "barehandle"}),
        _dnc_row(**{"Profile URL": "https://www.instagram.com/urlonly/"}),
        _dnc_row(**{"Handle": "both", "Profile URL": "https://www.tiktok.com/@alsoboth"}),
    ]}))
    blocklist = suppression.fetch_social_blocklist("T")
    assert blocklist.handles == {"barehandle", "urlonly", "both", "alsoboth"}


def test_empty_social_dnc_is_accurate_on_a_new_base_by_default(monkeypatch):
    """
    Zero rows is the CORRECT state on a new base — nobody has asked Mythumi to
    stop yet. The Valencia reader raises here because its table has ~1,330 rows.
    """
    monkeypatch.setattr(config, "SOCIAL_REQUIRE_NON_EMPTY_DNC", False)
    monkeypatch.setattr(suppression.HTTP, "get", lambda *a, **k: _Resp(body={"records": []}))

    blocklist = suppression.fetch_social_blocklist("T")

    assert isinstance(blocklist, Blocklist)
    assert len(blocklist) == 0
    assert blocklist.match(handle="anyone") == ""


def test_empty_social_dnc_aborts_once_the_list_is_seeded(monkeypatch):
    """
    Flip the flag and zero rows becomes a failure again — from that point it can
    only mean the table name, token scope or field names have drifted.
    """
    monkeypatch.setattr(config, "SOCIAL_REQUIRE_NON_EMPTY_DNC", True)
    monkeypatch.setattr(suppression.HTTP, "get", lambda *a, **k: _Resp(body={"records": []}))

    with pytest.raises(BlocklistUnavailable, match="zero rows"):
        suppression.fetch_social_blocklist("T")


@pytest.mark.parametrize("resp,match", [
    (_Resp(status=403, body={}), "403"),
    (_Resp(body={"no_records_key": 1}), "no 'records' key"),
    (_Resp(body={"records": "nope"}), "not a list"),
])
def test_social_dnc_fails_closed_on_a_real_failure(monkeypatch, resp, match):
    """
    A 403 is exactly how the first live run failed. Every genuine failure must
    raise: sourcing creators with no suppression list is the one failure here
    that can reach someone who asked to be left alone.
    """
    monkeypatch.setattr(suppression.HTTP, "get", lambda *a, **k: resp)
    with pytest.raises(BlocklistUnavailable, match=match):
        suppression.fetch_social_blocklist("T")


def test_social_dnc_fails_closed_on_a_transport_error(monkeypatch):
    import requests as _requests

    def boom(*a, **k):
        raise _requests.RequestException("connection reset")

    monkeypatch.setattr(suppression.HTTP, "get", boom)
    with pytest.raises(BlocklistUnavailable, match="connection reset"):
        suppression.fetch_social_blocklist("T")


def test_unconfigured_social_dnc_table_refuses_rather_than_defaulting(monkeypatch):
    monkeypatch.setattr(config, "AIRTABLE_TABLE_SOCIAL_DNC", None)
    with pytest.raises(BlocklistUnavailable, match="AIRTABLE_TABLE_SOCIAL_DNC"):
        suppression.fetch_social_blocklist()


def test_pipeline_uses_the_social_reader_not_the_valencia_one():
    """
    Regression guard on the exact bug that broke the first live run: the
    pipeline must not be wired to the Valencia-pinned fetch_blocklist.
    """
    assert pipeline.fetch_blocklist is suppression.fetch_social_blocklist


def test_daily_cap_asks_for_a_field_the_prospect_table_actually_has(monkeypatch):
    """
    The second live-run failure: count_added_today defaults to returning
    "Channel ID", which the prospect tables do not have, and Airtable answers
    422 UNKNOWN_FIELD_NAME rather than ignoring it.
    """
    from channel_vetting.airtable import client as airtable_client

    seen = {}

    class _R:
        status_code = 200

        @staticmethod
        def json():
            return {"records": []}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen["fields"] = params.get("fields[]")
        return _R()

    monkeypatch.setattr(airtable_client.HTTP, "get", fake_get)

    airtable_client.count_added_today("TikTok – Prospects", "Qualified", id_field="Handle")
    assert seen["fields"] == "Handle"

    # And the YouTube default is untouched.
    airtable_client.count_added_today("Home Theatre – Prospects", "Qualified")
    assert seen["fields"] == "Channel ID"


# --- 9. the REAL posts payload -------------------------------------------
#
# Captured from live 0.03-credit calls on 2026-09-03, one per platform. This is
# the shape that broke the first end-to-end run: engagement is NESTED, and an
# earlier version read it from the item root, so every median came out None and
# 30 of 30 creators on both platforms were rejected as "below_median_reach".

def _live_item(taken_at, views, likes, comments, pk="7678009073421405471"):
    """One item in the vendor's real shape."""
    return {
        "pk": pk,
        "taken_at": taken_at,                      # unix epoch, not ISO
        "url": f"https://www.tiktok.com/@khaby.lame/video/{pk}",
        "device_timestamp": taken_at * 1_000_000,
        "media_url": "https://v15m.tiktokcdn-eu.com/signed/expiring/link",
        "media_id": pk,
        "image_versions": None,
        "media_type": 2,
        "caption": "They were there yesterday, I promise",
        "user": {"full_name": "Khabane lame", "pk": "1", "username": "khaby.lame",
                 "profile_pic_url": "https://x/y.jpg"},
        "engagement": {"likes": likes, "comments": comments, "views": views},
    }


def _live_body(items):
    return {"result": {"items": items}, "credits_cost": 0.03}


def test_metrics_read_the_nested_engagement_object():
    """The bug that made the first live run reject everything."""
    base = int(NOW.timestamp())
    items = [_live_item(base - i * 86_400 * 2, views=60_000, likes=3_000, comments=40)
             for i in range(10)]

    metrics = posts.metrics_from_items(items, now=NOW)

    assert metrics.measured
    assert metrics.median_views == 60_000, "views come from item['engagement']"
    assert metrics.avg_likes == 3_000
    assert metrics.avg_comments == 40
    assert metrics.days_since_last_post == 0
    assert metrics.posts_per_week is not None


def test_no_shares_field_means_zero_not_a_crash():
    """Neither platform returns shares, so the column stays blank."""
    base = int(NOW.timestamp())
    metrics = posts.metrics_from_items(
        [_live_item(base - i * 86_400, views=9_000, likes=100, comments=5) for i in range(5)],
        now=NOW,
    )
    assert metrics.total_shares == 0
    row = pipeline._prospect_record("tiktok", {"handle": "h", "channel_title": "H"},
                                    50_000, metrics)
    assert "Avg Shares per Post" not in row, "a false zero would read as measured"


def test_sample_media_uses_the_permalink_not_the_expiring_cdn_url():
    """
    A reviewer judges photo quality by opening the post. `media_url` is a signed
    CDN link that expires; `url` is the permalink.
    """
    base = int(NOW.timestamp())
    metrics = posts.metrics_from_items([_live_item(base, 9_000, 100, 5)], now=NOW)
    assert metrics.media_urls
    assert all(u.startswith("https://www.tiktok.com/") for u in metrics.media_urls)


def test_items_are_found_at_result_items():
    body = _live_body([_live_item(int(NOW.timestamp()), 9_000, 100, 5)])
    assert len(posts._items_from(body)) == 1


def test_no_view_data_is_a_distinct_reason_from_below_the_floor():
    """
    The diagnosis fix. Both used to return below_median_reach, which is how a
    parsing bug looked exactly like a genuinely low-reach creator.
    """
    base = int(NOW.timestamp())
    # Engagement present but no views at all (e.g. Instagram static posts).
    no_views = [{"taken_at": base - i * 86_400, "engagement": {"likes": 10, "comments": 1}}
                for i in range(5)]
    metrics = posts.metrics_from_items(no_views, now=NOW)
    assert metrics.measured and metrics.median_views is None
    assert criteria.auto_reject_reason("tiktok", followers=50_000, metrics=metrics) == (
        criteria.REASON_NO_VIEW_DATA
    )

    # Measured, but genuinely under the floor.
    low = [_live_item(base - i * 86_400, views=100, likes=5, comments=1) for i in range(5)]
    assert criteria.auto_reject_reason(
        "tiktok", followers=50_000, metrics=posts.metrics_from_items(low, now=NOW)
    ) == criteria.REASON_MEDIAN_VIEWS


def test_median_reach_floors_are_the_retuned_ones():
    """
    Lowered 2026-09-03 for the >=10 records/day target, on measured evidence
    that the reach gate alone removed 25 of 30 on both platforms. Pinned so the
    next change is deliberate — these are CRITERIA, not throughput: a creator
    admitted at 3,000 would never have been admitted at 5,000.
    """
    assert criteria.min_median_views("tiktok") == 3_000
    assert criteria.min_median_views("instagram") == 1_500
    # Instagram stays the lower of the two: it reports plays for Reels only, so
    # its median is taken over a smaller, noisier sample than TikTok's.
    assert criteria.min_median_views("instagram") < criteria.min_median_views("tiktok")


def test_screening_budget_can_actually_reach_the_daily_target():
    """
    30 screens per platform was the binding constraint, not the gates: at the
    measured 13% admit rate no threshold could have produced 10 rows from 30.
    """
    screens = int(config.SOCIAL_MAX_POSTS_CREDITS_PER_RUN
                  / config.SOCIAL_POSTS_CREDITS_PER_REQUEST)
    assert screens >= config.SOCIAL_TARGET_PER_PLATFORM * 3, (
        "the posts budget must allow several screens per admitted row, or the "
        "target is unreachable however the floors are set"
    )


# --- 10. the pet-content requirement -------------------------------------
#
# The first real run admitted four pet-portrait ARTISTS from the gift-intent
# lane. An artist is a competitor, not a customer. Pet content is now a hard
# requirement (operator instruction 2026-09-03) and sellers are excluded.

from channel_vetting.social import relevance


def _metrics_with(captions):
    """
    Metrics that clear every NUMERIC gate, so a test about content reaches the
    content gate. Engagement is 8,000/200,000 = 4%, above the 3% micro-band
    TikTok floor — an earlier version used 2.75% and every such test was
    silently rejected on below_engagement_floor before the pet gate ever ran.
    """
    return posts.PostMetrics(
        measured=True, sample_size=max(len(captions), 1), median_views=20_000,
        total_views=200_000, views_sample_size=max(len(captions), 1),
        total_likes=7_200, total_comments=800, total_interactions=8_000,
        days_since_last_post=1, posts_per_week=3.0, captions=tuple(captions),
    )


def test_pet_owner_passes_the_requirement():
    assert relevance.pet_content_reason(_metrics_with(_PET_CAPTIONS)) is None


def test_pet_portrait_artist_is_excluded():
    """The exact failure from run 33762468002 — four artists admitted."""
    artist = _metrics_with([
        "custom pet portrait commission for a client",
        "new prints available in my etsy shop",
        "dm to order yours",
        "watercolour pet portrait of a dog",
        "now taking orders, slots open",
    ])
    assert relevance.pet_content_reason(artist) == relevance.REASON_SELLS_PET_ART


def test_non_pet_creator_is_excluded():
    nonpet = _metrics_with(["gym day", "new recipe", "travel vlog", "outfit of the day"])
    assert relevance.pet_content_reason(nonpet) == relevance.REASON_NO_PET_CONTENT


def test_no_captions_is_its_own_reason_not_a_pass():
    """
    Given a distinct reason so its frequency is visible. If it turns out common,
    loosen it deliberately rather than by accident.
    """
    assert relevance.pet_content_reason(_metrics_with([])) == (
        relevance.REASON_PET_CONTENT_UNKNOWN
    )


def test_one_pet_hashtag_is_not_enough():
    """
    The draft: "a hashtag is not a niche. One use of #dogsofinstagram doesn't
    make someone a pet creator. Judge from the last 20 posts."
    """
    mostly_other = _metrics_with([
        "#dogsofinstagram", "gym day", "new recipe", "travel vlog",
        "outfit of the day", "coffee run", "work from home", "book haul",
        "concert night", "grocery run",
    ])
    assert relevance.pet_content_reason(mostly_other) == relevance.REASON_NO_PET_CONTENT


def test_an_owner_who_mentions_a_sticker_once_is_not_treated_as_a_seller():
    """The seller test is whether selling is a THEME, not whether a word appears."""
    owner = _metrics_with([
        "my corgi being dramatic #dogsoftiktok",
        "vet day for the pup",
        "got a sticker of my dog made, obsessed",
        "she found the zoomies",
        "breakfast with my dog",
    ])
    assert relevance.pet_content_reason(owner) is None


def test_run_platform_rejects_an_artist_and_says_why(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(pipeline.discovery, "discover", _one_candidate("petartist"))
    monkeypatch.setattr(pipeline.posts, "fetch_metrics", lambda *a, **k: _metrics_with([
        "custom pet portrait commission", "prints in my etsy shop",
        "dm to order yours", "watercolour pet portrait", "slots open now",
    ]))
    monkeypatch.setattr(pipeline, "_create_row", lambda t, f: "recX")

    result = pipeline.run_platform("tiktok")

    assert result.admitted == 0
    assert result.rejections.get(relevance.REASON_SELLS_PET_ART) == 1


def test_lanes_target_owners_and_the_secondary_verticals_are_off():
    """
    Every enabled lane must require pet content, and none may reuse the
    gift-intent phrasing that surfaced artists.
    """
    from channel_vetting.social.lanes import lanes_in_order, LANES

    enabled = lanes_in_order()
    assert enabled, "at least one lane must be enabled"
    assert all(l["pet_required"] for l in enabled)
    assert all(l["key"].startswith("pet_") for l in enabled)
    assert {l["key"] for l in LANES if not l["enabled"]} == {"people", "trpg"}
    # Each query must describe the POSTER, not a product.
    for lane in enabled:
        q = lane["ai_search"].lower()
        assert "their own" in q or "owner" in q or "parent" in q or "adopted" in q
        assert "commission" not in q and "portrait" not in q


def test_social_zone_matches_valencia():
    """
    Operator instruction 2026-09-03: same location as Valencia. Pinned rather
    than imported so config.py keeps no dependency on search_zones.
    """
    from channel_vetting.discovery.search_zones import ZONE_CORE
    assert set(config.SOCIAL_ALLOWED_COUNTRIES) == set(ZONE_CORE)


def test_discovery_sends_the_verified_location_names_and_ai_search():
    from channel_vetting.discovery.search_zones import ZONE_CORE, vendor_locations_for
    from channel_vetting.social.lanes import lanes_in_order

    filters = discovery.build_filters("tiktok", lanes_in_order()[0])

    assert filters["location"] == vendor_locations_for(ZONE_CORE)
    assert filters["location"] == ["Australia", "Canada", "United Kingdom", "United States"]
    assert filters["ai_search"] == lanes_in_order()[0]["ai_search"]
    # keywords_not_in_description is ACCEPTED but inert on these platforms —
    # probed 2026-09-03 — so it must not be sent as though it worked.
    assert "keywords_not_in_description" not in filters


def test_zone_points_are_awarded_because_location_is_enforced_server_side():
    """
    auto_score's audience component took a `country` that was always None, so
    it could never fire. Location is filtered server-side now.
    """
    metrics = _metrics_with(_PET_CAPTIONS)
    without = criteria.auto_score("tiktok", followers=50_000, metrics=metrics, in_zone=False)
    within = criteria.auto_score("tiktok", followers=50_000, metrics=metrics, in_zone=True)
    assert within[0] == without[0] + 12


# --- 11. the Airtable schema contract ------------------------------------
#
# Airtable answers an unknown field name with 422 UNKNOWN_FIELD_NAME rather than
# ignoring it, so ONE renamed or mistyped column makes the whole write fail —
# and that is exactly how the second live run died (on "Channel ID"). These
# names are transcribed from the live tables on 2026-09-03; if a column is
# renamed in Airtable, this test is what tells you before a run does.

_LIVE_COMMON = {
    "Creator Name", "Handle", "Profile URL", "Account ID", "Qualification",
    "Status", "Subject Check", "Photo Quality", "Followers",
    "Median Views (last 10)", "Avg Likes per Post", "Avg Comments per Post",
    "Posts per Week", "Last Posted", "Days Since Last Post", "Posts Sampled",
    "Follower Band", "Priority Band", "Auto Score (of 35)", "Sample Media",
    "Lane", "Email", "Do Not Contact", "Send email now", "Send Requested At",
    "Source", "Screened At", "Date Added", "Notes", "Outreach Log",
    "Last Send State", "Outreach Ineligible Reason",
}
LIVE_FIELDS = {
    "tiktok": _LIVE_COMMON | {
        "Avg Views per Post", "Engagement Rate (per view)", "Avg Shares per Post",
    },
    "instagram": _LIVE_COMMON | {
        "Avg Reel Plays", "Engagement Rate (per follower)",
    },
}


@pytest.mark.parametrize("platform", ["tiktok", "instagram"])
def test_prospect_record_only_writes_fields_that_exist(platform):
    metrics = _metrics_with(_PET_CAPTIONS)
    candidate = {"handle": "h", "channel_title": "H", "influencers_user_id": "u1"}

    row = pipeline._prospect_record(platform, candidate, 50_000, metrics, "pet_breed")

    unknown = set(row) - LIVE_FIELDS[platform]
    assert not unknown, (
        f"{platform} row writes columns that do not exist on the table: "
        f"{sorted(unknown)} — Airtable answers 422 UNKNOWN_FIELD_NAME and the "
        f"whole write fails"
    )


@pytest.mark.parametrize("platform", ["tiktok", "instagram"])
def test_prospect_record_never_writes_the_other_platforms_columns(platform):
    """
    The per-view and per-follower engagement columns are NOT interchangeable, and
    writing one into the other's table would be both a 422 and a lie.
    """
    other = "instagram" if platform == "tiktok" else "tiktok"
    only_other = LIVE_FIELDS[other] - LIVE_FIELDS[platform]

    row = pipeline._prospect_record(
        platform, {"handle": "h", "channel_title": "H"}, 50_000,
        _metrics_with(_PET_CAPTIONS), "pet_breed",
    )

    assert not (set(row) & only_other)


# --- 12. the platform registry -------------------------------------------
#
# Adding a platform used to mean editing per-platform branches in six modules,
# and a forgotten one did not raise — it fell into the TikTok branch, so a new
# platform would be judged on TikTok's floors and written to TikTok's columns.
# These tests pin that the registry is now the only place that varies.

from channel_vetting.social import platforms

REQUIRED_KEYS = {
    "label", "table_config_attr", "min_median_views_attr", "denominator",
    "engagement_floors", "profile_url", "page_size", "asset_set",
    "engagement_column", "reach_mean_column", "shares_column",
    "audience_age_available",
}


@pytest.mark.parametrize("platform", platforms.SUPPORTED)
def test_every_platform_entry_is_complete(platform):
    """
    A missing key is an AttributeError or a silent None at write time, deep in a
    run that has already been paid for.
    """
    spec = platforms.PLATFORMS[platform]
    assert set(spec) == REQUIRED_KEYS, (
        f"{platform} registry entry differs from the contract: "
        f"missing {sorted(REQUIRED_KEYS - set(spec))}, "
        f"extra {sorted(set(spec) - REQUIRED_KEYS)}"
    )
    assert set(spec["engagement_floors"]) == {
        criteria.BAND_SMALL, criteria.BAND_MICRO, criteria.BAND_MID, criteria.BAND_BIG,
    }
    assert spec["denominator"] in (platforms.DENOM_VIEWS, platforms.DENOM_FOLLOWERS)
    assert "{handle}" in spec["profile_url"]
    assert getattr(config, spec["min_median_views_attr"]) > 0


def test_an_unknown_platform_raises_rather_than_defaulting():
    """
    A silent default is how a new platform inherits TikTok's floors and columns.
    """
    with pytest.raises(ValueError, match="unsupported social platform"):
        platforms.spec("twitter")
    for fn in (criteria.engagement_floor, criteria.min_median_views):
        with pytest.raises(ValueError):
            fn("twitter") if fn is criteria.min_median_views else fn("twitter", 5_000)


def test_the_two_platforms_never_share_an_engagement_column():
    """
    Per-view and per-follower are not comparable, so the columns must differ —
    that is what makes a mis-write a 422 rather than a silent lie.
    """
    columns = [platforms.PLATFORMS[p]["engagement_column"] for p in platforms.SUPPORTED]
    assert len(set(columns)) == len(columns)
    means = [platforms.PLATFORMS[p]["reach_mean_column"] for p in platforms.SUPPORTED]
    assert len(set(means)) == len(means)


def test_registry_drives_the_derived_lookups():
    """One entry should reach every accessor, with no second place to edit."""
    for platform in platforms.SUPPORTED:
        spec = platforms.PLATFORMS[platform]
        assert criteria.engagement_floor(platform, 5_000) == spec["engagement_floors"]["small"]
        assert criteria.min_median_views(platform) == getattr(
            config, spec["min_median_views_attr"]
        )
        assert profile_url(platform, "corgi.daily").startswith("https://")
        assert posts._page_size(platform) == spec["page_size"]
        assert platform in discovery.SUPPORTED
        assert criteria._ENGAGEMENT_FLOORS[platform] is spec["engagement_floors"]


def test_engagement_denominator_follows_the_registry_not_a_hardcoded_name():
    per_view = [p for p in platforms.SUPPORTED
                if platforms.denominator(p) == platforms.DENOM_VIEWS]
    per_follower = [p for p in platforms.SUPPORTED
                    if platforms.denominator(p) == platforms.DENOM_FOLLOWERS]
    assert per_view == ["tiktok"]
    assert per_follower == ["instagram"]
    # Same interactions, same account, different denominators.
    tt = criteria.engagement_rate("tiktok", interactions=500, views=10_000, followers=100_000)
    ig = criteria.engagement_rate("instagram", interactions=500, views=10_000, followers=100_000)
    assert tt != ig


# --- 13. the per-business daily reservation ------------------------------

def test_social_spend_lands_in_its_own_ledger_bucket():
    """
    One ledger, separate buckets. Two ledgers would each believe they held a
    full allowance and the pair would double-spend one real subscription.
    """
    credit_tracker.record_spend(0.5, kind=credit_tracker.KIND_SOCIAL, detail="t")
    credit_tracker.record_spend(0.3, kind=credit_tracker.KIND_DISCOVERY, detail="t")

    assert credit_tracker.credits_today_for_kind(credit_tracker.KIND_SOCIAL) == pytest.approx(0.5)
    assert credit_tracker.credits_today_for_kind(credit_tracker.KIND_DISCOVERY) == pytest.approx(0.3)
    # The shared total still sees BOTH — that is the point.
    assert credit_tracker.credits_today() == pytest.approx(0.8)


def test_the_reservation_caps_social_without_touching_valencias_spend(monkeypatch):
    monkeypatch.setattr(config, "SOCIAL_MAX_CREDITS_PER_DAY", 1.0)
    monkeypatch.setattr(config, "SOCIAL_MAX_POSTS_CREDITS_PER_RUN", 99.0)

    assert pipeline.social_daily_headroom() == pytest.approx(1.0)
    # 1.0 / 0.03 = 33 screens
    assert pipeline.affordable_posts_screens() == 33

    # Valencia spending does NOT eat the social slice.
    credit_tracker.record_spend(0.9, kind=credit_tracker.KIND_DISCOVERY, detail="valencia")
    assert pipeline.social_daily_headroom() == pytest.approx(1.0)

    # Social spending does.
    credit_tracker.record_spend(0.9, kind=credit_tracker.KIND_SOCIAL, detail="mythumi")
    assert pipeline.social_daily_headroom() == pytest.approx(0.1)


def test_an_exhausted_reservation_trips_the_quality_floor(monkeypatch):
    """
    Running out of reservation must ABORT the platform, not admit creators
    screened on follower count alone.
    """
    _configure(monkeypatch)
    monkeypatch.setattr(config, "SOCIAL_MAX_CREDITS_PER_DAY", 0.15)  # 5 screens
    monkeypatch.setattr(config, "SOCIAL_MIN_POSTS_SCREENS_PER_RUN", 10)

    result = pipeline.run_platform("tiktok")

    assert "below the SOCIAL_MIN_POSTS_SCREENS_PER_RUN floor" in result.aborted
    assert result.screened == 0


def test_the_shared_ceiling_still_wins_over_the_reservation(monkeypatch, credit_ceilings):
    """
    The reservation decides how much of a shared day one business may take. It
    does NOT let it past the shared ceiling — that would be the double-spend a
    second ledger causes.
    """
    monkeypatch.setattr(config, "SOCIAL_MAX_CREDITS_PER_DAY", 99.0)
    monkeypatch.setattr(config, "SOCIAL_MAX_POSTS_CREDITS_PER_RUN", 99.0)
    credit_ceilings(day=0.30, month=99.0)   # only 10 screens in the whole day

    assert pipeline.affordable_posts_screens() == 10
