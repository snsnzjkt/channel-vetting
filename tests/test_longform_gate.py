"""
The "30+ videos that are NOT Shorts" gate.

statistics.videoCount counts Shorts as videos, and is_shorts_only() only
discards channels that are 100% Shorts — so a channel with 300 Shorts and 4
long-form uploads cleared both. This gate closes that gap by counting
confirmed non-Shorts uploads, paging deeper when the newest-50 window doesn't
already show MIN_LONGFORM_VIDEO_COUNT of them (2 quota units per extra page).

No network: every YouTube call is monkeypatched.
"""
import pytest
from search_zones import ZONE_CORE


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


def _playlist_payload(count, next_page_token=None):
    payload = {"items": [{"contentDetails": {"videoId": f"v{i}"}} for i in range(count)]}
    if next_page_token:
        payload["nextPageToken"] = next_page_token
    return payload


def _videos_payload(durations):
    return {"items": [{"id": f"v{i}", "contentDetails": {"duration": d}}
                      for i, d in enumerate(durations)]}


LONG = "PT12M"    # comfortably long-form
SHORT = "PT30S"   # a Short


class _FakeApi:
    """`pages` is a list of (playlist_payload, videos_payload) per allowed page."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.playlist_calls = 0
        self.video_calls = 0

    def get(self, url, params=None, timeout=None):
        if "/playlistItems" in url:
            if self.playlist_calls >= len(self._pages):
                raise AssertionError("paged further than the test allowed")
            payload = self._pages[self.playlist_calls][0]
            self.playlist_calls += 1
            return _Resp(200, payload)
        if "/videos" in url:
            payload = self._pages[self.video_calls][1]
            self.video_calls += 1
            return _Resp(200, payload)
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture
def spend(monkeypatch):
    """Records quota units the scan books, so cost is asserted not assumed."""
    import enrichment

    units = []
    monkeypatch.setattr(enrichment, "record_spend", lambda u, call_name="": units.append(u))
    monkeypatch.setattr(enrichment.time, "sleep", lambda s: None)
    return units


def _count(monkeypatch, pages, already_counted, target=30, max_pages=3):
    import enrichment

    api = _FakeApi(pages)
    monkeypatch.setattr(enrichment.HTTP, "get", api.get)
    total = enrichment.count_longform_in_older_videos(
        "UC1", "PL1", "token0",
        already_counted=already_counted, target=target, max_pages=max_pages,
    )
    return total, api


def test_already_at_target_costs_nothing(monkeypatch, spend):
    """The common case — 29 of 47 measured candidates — must be free."""
    import enrichment

    monkeypatch.setattr(
        enrichment.HTTP, "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not page when already at target")),
    )
    total = enrichment.count_longform_in_older_videos(
        "UC1", "PL1", "token0", already_counted=30, target=30,
    )
    assert total == 30
    assert spend == []


def test_no_page_token_means_no_older_uploads_to_read(monkeypatch, spend):
    import enrichment

    monkeypatch.setattr(
        enrichment.HTTP, "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not page without a token")),
    )
    assert enrichment.count_longform_in_older_videos("UC1", "PL1", "", 5, 30) == 5
    assert spend == []


def test_max_pages_zero_disables_the_scan(monkeypatch, spend):
    import enrichment

    monkeypatch.setattr(
        enrichment.HTTP, "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not page when disabled")),
    )
    assert enrichment.count_longform_in_older_videos("UC1", "PL1", "t", 5, 30, max_pages=0) == 5
    assert spend == []


def test_stops_paging_the_moment_the_target_is_reached(monkeypatch, spend):
    """
    A mixed-format channel: 25 long-form already seen, one more page finds 5
    more. It must stop there and not spend a second page.
    """
    pages = [
        (_playlist_payload(50, "token1"), _videos_payload([LONG] * 5 + [SHORT] * 45)),
        (_playlist_payload(50), _videos_payload([LONG] * 50)),  # must never be fetched
    ]
    total, api = _count(monkeypatch, pages, already_counted=25)

    assert total == 30
    assert api.playlist_calls == 1
    assert sum(spend) == 2, "one extra page is playlistItems.list + videos.list = 2 units"


def test_accumulates_across_pages(monkeypatch, spend):
    pages = [
        (_playlist_payload(50, "token1"), _videos_payload([LONG] * 10 + [SHORT] * 40)),
        (_playlist_payload(50, "token2"), _videos_payload([LONG] * 10 + [SHORT] * 40)),
        (_playlist_payload(50), _videos_payload([LONG] * 10 + [SHORT] * 40)),
    ]
    total, api = _count(monkeypatch, pages, already_counted=0)

    assert total == 30
    assert api.playlist_calls == 3
    assert sum(spend) == 6


def test_a_shorts_factory_is_rejected_at_the_page_cap(monkeypatch, spend):
    """
    Measured shape: Dadrianca, 2 long-form in its newest 50 across 791
    uploads. The cap is what makes rejecting it cheap rather than a walk
    through the whole catalogue.
    """
    pages = [
        (_playlist_payload(50, "token1"), _videos_payload([LONG] * 2 + [SHORT] * 48)),
        (_playlist_payload(50, "token2"), _videos_payload([LONG] * 2 + [SHORT] * 48)),
        (_playlist_payload(50, "token3"), _videos_payload([LONG] * 2 + [SHORT] * 48)),
    ]
    total, api = _count(monkeypatch, pages, already_counted=2, max_pages=3)

    assert total == 8
    assert api.playlist_calls == 3, "must stop at max_pages, not follow token3"
    assert sum(spend) == 6

    from main import longform_drop_reason
    assert longform_drop_reason(total) == "too_few_longform_videos"


def test_running_out_of_pages_returns_what_was_counted(monkeypatch, spend):
    pages = [(_playlist_payload(20), _videos_payload([LONG] * 20))]
    total, api = _count(monkeypatch, pages, already_counted=0)

    assert total == 20
    assert api.playlist_calls == 1


def test_an_unreadable_duration_never_counts_toward_the_floor(monkeypatch, spend):
    """A missing duration is unknown, and unknown must not clear a minimum."""
    pages = [(_playlist_payload(50), _videos_payload([""] * 25 + [None] * 25))]
    total, _ = _count(monkeypatch, pages, already_counted=0)

    assert total == 0


def test_a_failed_page_returns_the_count_so_far_without_raising(monkeypatch, spend):
    """Soft failure, like the rest of enrichment — and it leans toward
    discarding, since the count stays below target."""
    import enrichment

    monkeypatch.setattr(enrichment.HTTP, "get", lambda *a, **k: _Resp(503))
    monkeypatch.setattr(enrichment.time, "sleep", lambda s: None)

    total = enrichment.count_longform_in_older_videos("UC1", "PL1", "t", 7, 30)

    assert total == 7
    assert spend == [], "a non-200 is not billed by Google and must not be charged here"


def test_a_request_exception_returns_the_count_so_far(monkeypatch, spend):
    import enrichment
    import requests

    def boom(*a, **k):
        raise requests.RequestException("network down")

    monkeypatch.setattr(enrichment.HTTP, "get", boom)
    monkeypatch.setattr(enrichment.time, "sleep", lambda s: None)

    assert enrichment.count_longform_in_older_videos("UC1", "PL1", "t", 7, 30) == 7
    assert spend == []


def test_dominant_language_wins_over_a_single_odd_upload(monkeypatch):
    """
    The tag is set per-video by the creator and is inconsistent. Reading the
    FIRST non-empty one let a single mislabelled upload decide the channel;
    the most common value across the window is the robust reading.
    """
    from enrichment import dominant_language

    assert dominant_language(["es", "en", "en", "en"]) == "en"
    assert dominant_language(["", "", "en-GB", "en-GB", "de"]) == "en-GB"
    assert dominant_language(["", "", ""]) == ""
    assert dominant_language([]) == ""


def test_process_candidate_does_not_page_for_a_candidate_it_already_rejected(monkeypatch):
    """
    The long-form scan is the only discard gate that costs quota, so it must
    run AFTER the free ones. A channel below the view floor must be dropped
    without paging.
    """
    import main

    monkeypatch.setattr(
        main, "count_longform_in_older_videos",
        lambda *a, **k: pytest.fail("paged for a candidate the free gates already dropped"),
    )
    monkeypatch.setattr(main, "get_channel_stats", lambda cid: {
        "channel_id": "UC1", "channel_title": "Chan", "handle": "chan", "published_at": "",
        "subscriber_count": 10_000, "uploads_playlist_id": "PL1", "business_email": "",
        "video_count": 500, "country": "US",
    })
    monkeypatch.setattr(main, "get_recent_video_performance", lambda cid, pl: {
        "avg_views": 9_000,          # below the 10,000 floor
        "avg_engagement_rate": 1.0, "upload_dates": [], "content_language": "en",
        "repeated_email": "", "longform_count": 1, "duration_sample_size": 50,
        "next_page_token": "t",
    })
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    class _NullBlocklist:
        def match(self, handle="", email="", name=""):
            return ""

    record, reason = main.process_candidate(
        {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []},
        {}, _NullBlocklist(),
        {"min_avg_views": 10_000, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE}, None,
    )

    assert record is None
    assert reason == "below_view_minimum"


def test_process_candidate_pages_when_the_newest_window_is_short(monkeypatch):
    """...and DOES page for a candidate that passed every free gate."""
    import main

    calls = []

    def fake_page(channel_id, playlist, token, already_counted, target):
        calls.append((already_counted, target))
        return 30

    monkeypatch.setattr(main, "count_longform_in_older_videos", fake_page)
    monkeypatch.setattr(main, "get_channel_stats", lambda cid: {
        "channel_id": "UC1", "channel_title": "Chan", "handle": "chan", "published_at": "",
        "subscriber_count": 10_000, "uploads_playlist_id": "PL1", "business_email": "",
        "video_count": 500, "country": "US",
    })
    monkeypatch.setattr(main, "get_recent_video_performance", lambda cid, pl: {
        "avg_views": 50_000, "avg_engagement_rate": 1.0, "upload_dates": [],
        "content_language": "en", "repeated_email": "",
        "longform_count": 22, "duration_sample_size": 50, "next_page_token": "t",
    })
    monkeypatch.setattr(main, "channel_age_months", lambda p: 100)
    # process_candidate resolves the email via resolve_email_with_source now;
    # None keeps the no-social drop dormant so this test isolates the paging.
    monkeypatch.setattr(main, "resolve_email_with_source", lambda *a, **k: ("", "", None))
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    class _NullBlocklist:
        def match(self, handle="", email="", name=""):
            return ""

    record, qualification = main.process_candidate(
        {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []},
        {}, _NullBlocklist(),
        {"min_avg_views": 10_000, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE}, None,
    )

    assert calls == [(22, 30)], "must resume from the 22 already seen, not recount from zero"
    assert record is not None
    assert qualification == "Qualified"


def test_the_videos_call_asks_for_duration_and_orientation(monkeypatch, spend):
    """
    Duration AND orientation, because a vertical upload is short-form at any
    length. The call is 1 unit regardless of parts, so `player` is free — but
    asking for snippet+statistics as well would make the intent unclear.

    maxWidth is asserted because it is LOAD-BEARING: without it the player part
    omits embedWidth/embedHeight entirely, every video reads as
    orientation-unknown, and the vertical half of count_longform silently stops
    working here while continuing to work in the main window.
    """
    import enrichment

    seen = {}

    def get(url, params=None, timeout=None):
        if "/videos" in url:
            seen["part"] = params.get("part")
            seen["maxWidth"] = params.get("maxWidth")
            return _Resp(200, _videos_payload([LONG] * 50))
        return _Resp(200, _playlist_payload(50))

    monkeypatch.setattr(enrichment.HTTP, "get", get)
    monkeypatch.setattr(enrichment.time, "sleep", lambda s: None)

    enrichment.count_longform_in_older_videos("UC1", "PL1", "t", 0, 30)

    assert seen["part"] == "contentDetails,player"
    assert seen["maxWidth"] == enrichment.PLAYER_EMBED_MAX_WIDTH
