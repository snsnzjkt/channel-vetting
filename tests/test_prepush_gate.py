"""
The pre-push gate: candidates that never reach Airtable at all.

This is a deliberate exception to the project's "flag, never discard" rule
(see CLAUDE.md > Qualification). After the 2026-08 criteria change it holds
every hard requirement:

  - Below the niche's average-view floor (10,000 for both niches). This
    used to be a "Below View Minimum" row for a human to dismiss; it is now
    a discard, which is what retired that Qualification value.
  - Fewer than MIN_VIDEO_COUNT (30) public videos — not enough of a
    published track record to approach.
  - Outside the allowed search zones (US, Canada, UK, Europe, Australia;
    Ireland excluded). Checked separately in process_candidate rather than
    here, since resolving the country can cost a browser page load.
  - Dead channels, on BOTH measures, so a small-but-real channel survives.
  - Shorts-only channels, which aren't the format these briefs are buying.

Shorts are detected by duration, since the API has no Shorts flag.
"""
import pytest


# A catalogue and a view count that comfortably clear the new floors, so
# each test below is exercising the one gate it names.
PASSING_VIDEOS = 100
PASSING_VIEWS = 50_000
HOME_THEATER_MIN_VIEWS = 10_000


def drop_reason(**overrides):
    """pre_push_drop_reason with everything passing unless overridden."""
    from main import pre_push_drop_reason

    kwargs = {
        "subscriber_count": 25_000,
        "avg_views": PASSING_VIEWS,
        "shorts_only": False,
        "min_avg_views": HOME_THEATER_MIN_VIEWS,
        "video_count": PASSING_VIDEOS,
        # Explicit because the gate treats an unset language as a FAILURE —
        # see main.is_english(). Passing it here keeps each test below
        # exercising the one gate it names.
        "content_language": "en",
    }
    kwargs.update(overrides)
    return pre_push_drop_reason(**kwargs)


def test_a_channel_clearing_every_gate_is_kept():
    assert drop_reason() is None


# --- the view floor, now a discard rather than a flag ---------------------


def test_drops_a_channel_below_the_view_floor():
    assert drop_reason(avg_views=9_999) == "below_view_minimum"


def test_exactly_at_the_view_floor_is_kept():
    assert drop_reason(avg_views=10_000) is None


def test_the_floor_comes_from_the_niche_not_a_constant():
    """
    Both niches sit at 10,000 today, but the threshold is still passed in
    per-niche — a hardcoded 10,000 here would silently ignore a niche that
    is later given a different bar.
    """
    assert drop_reason(avg_views=5_000, min_avg_views=2_000) is None
    assert drop_reason(avg_views=5_000, min_avg_views=10_000) == "below_view_minimum"


def test_missing_avg_views_is_treated_as_zero():
    """None here means the performance window produced nothing, not 'lots'."""
    assert drop_reason(avg_views=None) == "below_view_minimum"


def test_a_channel_that_used_to_be_flagged_is_now_dropped():
    """
    Review Center: 281 subs / 38.5 avg views, a live row that survived the
    old gate and landed as "Below View Minimum" for a human to dismiss.
    The view floor is what removes that cost.
    """
    assert drop_reason(subscriber_count=281, avg_views=38.5) == "below_view_minimum"


# --- the video-count floor -----------------------------------------------


def test_drops_a_channel_with_too_few_videos():
    assert drop_reason(video_count=29) == "too_few_videos"


def test_exactly_at_the_video_floor_is_kept():
    from main import MIN_VIDEO_COUNT

    assert MIN_VIDEO_COUNT == 30
    assert drop_reason(video_count=30) is None


def test_the_video_floor_has_no_upper_bound():
    """
    "30-40 videos" is a minimum catalogue size, not a band. A prolific
    channel clears the bar rather than failing it.
    """
    assert drop_reason(video_count=40) is None
    assert drop_reason(video_count=41) is None
    assert drop_reason(video_count=4_000) is None


def test_an_unreported_video_count_does_not_disqualify():
    """
    None means channels.list didn't report one. Absent data is not
    evidence against a channel — same rule as an unknown channel age.
    """
    assert drop_reason(video_count=None) is None


def test_a_reported_zero_video_count_is_a_real_answer():
    assert drop_reason(video_count=0) == "too_few_videos"


# --- dead-channel gate ----------------------------------------------------


def test_drops_a_channel_dead_on_both_measures():
    assert drop_reason(subscriber_count=5, avg_views=5, min_avg_views=0) == "dead_channel"


def test_keeps_a_channel_with_few_subs_but_real_views():
    """A new channel with traction is a prospect, not junk."""
    assert drop_reason(subscriber_count=40, avg_views=80_000) is None


def test_keeps_a_channel_with_many_subs_but_few_views():
    """
    Low views on a big channel is a review signal, not a reason to hide it
    — but only while the view floor lets it through at all, which is why
    this asserts against the dead-channel gate specifically.
    """
    assert drop_reason(subscriber_count=469_000, avg_views=4_902, min_avg_views=0) is None


def test_both_dead_thresholds_are_exclusive_bounds():
    """100/100 is the floor: at exactly 100 the channel is kept."""
    assert drop_reason(subscriber_count=100, avg_views=100, min_avg_views=0) is None
    assert drop_reason(subscriber_count=99, avg_views=99, min_avg_views=0) == "dead_channel"


def test_the_dead_rows_from_the_live_table_are_dropped():
    """SmartFindsDaily, James Bhattrai and Easy Tech Reviews, live values."""
    for subs, views in [(5, 5), (5, 5), (0, 0)]:
        assert drop_reason(subscriber_count=subs, avg_views=views, min_avg_views=0) == "dead_channel"


def test_the_lowest_real_prospect_survives():
    """Retro Media Crypt — the lowest Qualified channel in the live table."""
    assert drop_reason(subscriber_count=2_400, avg_views=16_159.8) is None


# --- Shorts-only gate -----------------------------------------------------


def test_drops_a_shorts_only_channel():
    assert drop_reason(shorts_only=True) == "shorts_only"


def test_shorts_gate_applies_regardless_of_how_strong_the_channel_is():
    assert drop_reason(subscriber_count=2_000_000, avg_views=900_000, shorts_only=True) == "shorts_only"


# --- duration parsing -----------------------------------------------------


def test_parses_iso8601_durations():
    from enrichment import parse_iso8601_duration

    assert parse_iso8601_duration("PT45S") == 45
    assert parse_iso8601_duration("PT1M") == 60
    assert parse_iso8601_duration("PT1M1S") == 61
    assert parse_iso8601_duration("PT12M35S") == 755
    assert parse_iso8601_duration("PT1H2M3S") == 3723


def test_unparseable_duration_is_not_treated_as_a_short():
    """
    A missing or malformed duration must not make a channel look like
    Shorts — that would discard it with no row to review.
    """
    from enrichment import parse_iso8601_duration

    assert parse_iso8601_duration("") is None
    assert parse_iso8601_duration(None) is None
    assert parse_iso8601_duration("P0D") is None


def test_shorts_only_needs_every_sampled_video_under_the_cap():
    from enrichment import is_shorts_only

    assert is_shorts_only(["PT30S", "PT59S", "PT60S"]) is True
    assert is_shorts_only(["PT30S", "PT61S"]) is False


def test_shorts_only_is_false_when_nothing_is_known():
    """No durations means no evidence, and no evidence never discards."""
    from enrichment import is_shorts_only

    assert is_shorts_only([]) is False
    assert is_shorts_only(["", None]) is False


def test_an_unreadable_duration_among_shorts_does_not_force_a_drop():
    """One unparseable entry is enough doubt to keep the channel."""
    from enrichment import is_shorts_only

    assert is_shorts_only(["PT30S", "PT45S", ""]) is False


# --- the English content-language gate ------------------------------------


def test_drops_a_channel_whose_content_is_not_english():
    assert drop_reason(content_language="de") == "not_english"
    assert drop_reason(content_language="pl") == "not_english"
    assert drop_reason(content_language="vi") == "not_english"


def test_regional_english_variants_all_pass():
    """
    en-GB/en-US/en-AU must pass, and must NOT be normalised to a bare "en":
    resolve_country() reads the region subtag to place channels that declare
    no country, and that is the only zone signal for ~15% of candidates.
    """
    for tag in ("en", "en-US", "en-GB", "en-AU", "en-CA", "EN-gb"):
        assert drop_reason(content_language=tag) is None, tag


def test_an_unset_language_is_dropped_not_kept():
    """
    The deliberate exception to "absent data never disqualifies": the table
    is specified to hold English channels, and a blank can't satisfy that.
    Measured cost when this was chosen: 0 of 47 otherwise-qualifying
    candidates had an unset language.
    """
    assert drop_reason(content_language="") == "not_english"
    assert drop_reason(content_language=None) == "not_english"
    assert drop_reason(content_language="   ") == "not_english"


def test_zxx_no_linguistic_content_is_not_english():
    """`zxx` is the ISO code for "no linguistic content" — a real value seen
    on a live candidate, and not a channel to approach in English."""
    assert drop_reason(content_language="zxx") == "not_english"


def test_a_language_merely_containing_en_is_not_english():
    """Prefix match, not substring: "ben" (Bengali) must not pass as English."""
    assert drop_reason(content_language="ben") == "not_english"
    assert drop_reason(content_language="hen") == "not_english"


# --- the long-form (non-Shorts) video floor -------------------------------


def test_counts_only_confirmed_long_form_videos():
    from enrichment import count_longform

    assert count_longform(["PT30S", "PT61S", "PT12M"]) == 2
    assert count_longform(["PT30S", "PT59S"]) == 0
    assert count_longform([]) == 0


def test_an_unreadable_duration_never_counts_as_long_form():
    """
    Opposite lean from is_shorts_only, and for the same reason: this number
    feeds a MINIMUM, so counting an unknown would let a channel clear the bar
    on missing data.
    """
    from enrichment import count_longform

    assert count_longform(["", None, "P0D", "garbage"]) == 0
    assert count_longform(["PT12M", ""]) == 1


def test_drops_a_shorts_factory_that_posts_the_occasional_long_video():
    """
    The gap this gate closes. is_shorts_only() only catches 100%-Shorts
    channels, and statistics.videoCount counts Shorts as videos — so a
    channel with 300 Shorts and 4 long-form uploads cleared both checks.
    """
    from main import longform_drop_reason

    assert longform_drop_reason(4) == "too_few_longform_videos"
    assert longform_drop_reason(29) == "too_few_longform_videos"


def test_exactly_thirty_long_form_videos_is_kept():
    from main import longform_drop_reason

    assert longform_drop_reason(30) is None
    assert longform_drop_reason(400) is None


# --- precedence between the gates ----------------------------------------


@pytest.mark.parametrize(
    "overrides,expected",
    [
        # Shorts wins over everything: it's the verdict about what the
        # channel IS, and the cheapest thing to report.
        ({"shorts_only": True, "video_count": 2, "avg_views": 1}, "shorts_only"),
        # Catalogue size before view count — a 5-video channel's average
        # views aren't a meaningful number to report against.
        ({"video_count": 2, "avg_views": 1}, "too_few_videos"),
        # View floor before the dead-channel gate, since it's the criterion
        # the reviewer actually set.
        ({"subscriber_count": 5, "avg_views": 5}, "below_view_minimum"),
    ],
)
def test_gate_precedence_is_stable(overrides, expected):
    """
    Only one reason is reported, and it goes in the log rather than into
    Airtable. Pinned anyway so the log stays readable and a reordering is a
    visible decision.
    """
    assert drop_reason(**overrides) == expected


# --- the per-video view floor (>= 60% of the LONG-FORM sample clears 10k) --
# Stricter than the niche average floor, which one viral upload can carry over
# the line while the rest of the sample flopped — but deliberately NOT "every
# video", which (measured 2026-08-13) demanded an effective 25-50k average and
# discarded ~99% of discovered creators. Gates on the RATIO of settled
# LONG-FORM videos clearing MIN_VIEWS_PER_VIDEO; Shorts never enter the sample
# (see enrichment.get_recent_video_performance).
#
# The 0.60 figure is calibrated against the live tables rather than chosen: on
# all 80 tracked rows, Shorts-inflated channels scored 0-3 of 10 while channels
# a human reviewer had already Approved scored 5-6 of 10. See
# MIN_VIEWS_PER_VIDEO_RATIO in main.py for the full reasoning.


def _views(above: int, below: int) -> list[int]:
    """A settled-views window: `above` videos over the floor, `below` under."""
    return [50_000] * above + [500] * below


def test_drops_a_channel_when_too_much_of_the_sample_is_weak():
    """5 of 10 clearing is under the 60% bar."""
    assert drop_reason(settled_views=_views(5, 5)) == "video_below_view_minimum"


def test_exactly_sixty_percent_of_the_sample_is_kept():
    from main import MIN_VIEWS_PER_VIDEO, MIN_VIEWS_PER_VIDEO_RATIO

    assert MIN_VIEWS_PER_VIDEO == 10_000
    assert MIN_VIEWS_PER_VIDEO_RATIO == 0.60
    # 6 of 10 is the boundary and must PASS. This is the live case the
    # recalibration was for: ETPC (6/10, 22,198 avg) and Adrianne MG (6/10,
    # 16,883 avg) were both being discarded at 70%, and a reviewer had marked
    # ETPC Approved.
    assert drop_reason(settled_views=_views(6, 4)) is None


def test_a_video_exactly_at_the_floor_counts_as_clearing_it():
    """The comparison is >=, so a video on exactly 10,000 is not a failure."""
    assert drop_reason(settled_views=[10_000] * 10) is None


def test_the_per_video_floor_still_catches_a_shorts_inflated_channel():
    """
    The reason this gate exists at all, and the live case that proved it:
    "Explore With Jasir" reported a 140,885-view average with 0 of 10 long-form
    videos over 10k. A 3-of-10 channel (Kat and Sourabh, 57,234 avg) is caught
    at 60% too — the bar came down without letting these through.
    """
    assert drop_reason(avg_views=140_000, settled_views=_views(0, 10)) == "video_below_view_minimum"
    assert drop_reason(avg_views=57_000, settled_views=_views(3, 7)) == "video_below_view_minimum"


def test_a_single_weak_video_does_not_sink_a_strong_channel():
    """
    9 of 10 over the floor and one weak upload used to be a discard, which is
    what made the effective bar a 25-50k average rather than the 10k the brief
    asks for.
    """
    assert drop_reason(settled_views=_views(9, 1)) is None


def test_the_ratio_rounds_up_on_a_partial_video():
    """
    ceil(0.60 * 3) == 2, so on a 3-video sample two of three must clear.
    Rounding up keeps a thin sample from being judged more leniently than a
    full one.
    """
    assert drop_reason(settled_views=_views(2, 1)) is None
    assert drop_reason(settled_views=_views(1, 2)) == "video_below_view_minimum"


def test_a_nine_video_sample_still_needs_six():
    """
    ceil(0.60 * 9) == 6, so a sample short one video does not get an easier
    bar. Pins the live Diva Angel case (2 of 9), which must still fail.
    """
    assert drop_reason(settled_views=_views(6, 3)) is None
    assert drop_reason(settled_views=_views(5, 4)) == "video_below_view_minimum"
    assert drop_reason(settled_views=_views(2, 7)) == "video_below_view_minimum"


def test_an_unknown_settled_window_does_not_disqualify():
    """
    None/empty means nothing in the window has settled yet (an entirely empty
    performance window is already caught upstream in enrichment), not that
    videos underperformed. Absent data never disqualifies — same rule as
    video_count.
    """
    assert drop_reason(settled_views=None) is None
    assert drop_reason(settled_views=[]) is None


# --- the upload-cadence floor (>= 10 uploads a year) ----------------------


def test_drops_a_channel_that_uploads_too_rarely():
    assert drop_reason(uploads_per_year=9) == "upload_cadence_too_low"


def test_exactly_ten_uploads_a_year_is_kept():
    from main import MIN_UPLOADS_PER_YEAR

    assert MIN_UPLOADS_PER_YEAR == 10
    assert drop_reason(uploads_per_year=10) is None


def test_an_unknown_cadence_does_not_disqualify():
    """
    Fewer than two sampled uploads can't yield a cadence, so the caller
    passes None — unknown, not a failure.
    """
    assert drop_reason(uploads_per_year=None) is None


# --- the recency floor (last upload within a rolling 12 months) -----------


def test_drops_a_channel_whose_last_upload_is_over_a_year_old():
    assert drop_reason(days_since_last_upload=366) == "stale_channel"


def test_exactly_a_year_since_the_last_upload_is_kept():
    from main import MAX_DAYS_SINCE_LAST_UPLOAD

    assert MAX_DAYS_SINCE_LAST_UPLOAD == 365
    assert drop_reason(days_since_last_upload=365) is None


def test_an_unknown_last_upload_date_does_not_disqualify():
    """No parseable upload timestamp is unknown, not stale."""
    assert drop_reason(days_since_last_upload=None) is None


def test_a_channel_clearing_the_new_activity_gates_too_is_kept():
    assert drop_reason(
        settled_views=[10_000] * 10, uploads_per_year=52, days_since_last_upload=3
    ) is None


@pytest.mark.parametrize(
    "overrides,expected",
    [
        # The niche's own average floor is reported before the per-video one.
        ({"avg_views": 5_000, "settled_views": [5_000] * 10}, "below_view_minimum"),
        # Per-video views before the activity gates: it's a numbers criterion
        # the reviewer set, cadence/recency are about the channel's rhythm.
        ({"settled_views": [500] * 10, "uploads_per_year": 1, "days_since_last_upload": 999},
         "video_below_view_minimum"),
        # Cadence before recency.
        ({"uploads_per_year": 1, "days_since_last_upload": 999}, "upload_cadence_too_low"),
    ],
)
def test_new_activity_gate_precedence(overrides, expected):
    assert drop_reason(**overrides) == expected


# --- wiring: process_candidate feeds the three signals into the gate -------
# Unit tests above pin pre_push_drop_reason directly; these pin that
# process_candidate actually maps performance -> the right gate arguments, so a
# future mis-wire (e.g. settled_views swapped with days_since) can't pass unseen.


def _passing_perf(**overrides):
    perf = {
        "avg_views": 50_000, "min_views": 50_000, "avg_engagement_rate": 1.0,
        # The gate reads settled_views, not min_views — a channel whose whole
        # settled window clears the floor.
        "settled_views": [50_000] * 10,
        "shorts_only": False, "content_language": "en",
        # Recent, single date: passes recency; <2 dates -> cadence unknown.
        "upload_dates": ["2026-08-01T00:00:00Z"],
        "longform_count": 30, "duration_sample_size": 50, "next_page_token": "",
    }
    perf.update(overrides)
    return perf


def _process_candidate(monkeypatch, perf):
    import main

    stats = {
        "channel_id": "UC1", "channel_title": "Clean Channel", "handle": "chan",
        "description": "", "published_at": "2020-01-01T00:00:00Z",
        "subscriber_count": 25_000, "uploads_playlist_id": "PL1",
        "business_email": "", "video_count": 500, "country": "US",
    }
    monkeypatch.setattr(main, "get_channel_stats", lambda *a, **k: stats)
    monkeypatch.setattr(main, "get_recent_video_performance", lambda *a, **k: perf)
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)

    class _NullBlocklist:
        handles: set = set()

        def match(self, handle="", email="", name=""):
            return ""

    return main.process_candidate(
        {"channel_id": "UC1", "channel_title": "Clean Channel", "matched_keywords": []},
        {}, _NullBlocklist(),
        {"min_avg_views": 10_000, "min_channel_age_months": None}, None,
    )


def test_process_candidate_drops_a_channel_with_a_mostly_weak_window(monkeypatch):
    """Wiring test: settled_views must reach the gate. 2 of 10 clearing is
    under the 70% bar, so this is a discard."""
    record, reason = _process_candidate(
        monkeypatch, _passing_perf(settled_views=[50_000] * 2 + [500] * 8))
    assert record is None
    assert reason == "video_below_view_minimum"


def test_process_candidate_keeps_a_channel_with_one_weak_video(monkeypatch):
    """The other half of the wiring test, and the 2026-08-14 behaviour change:
    9 of 10 clearing is a keep, where the old every-video rule discarded it."""
    record, reason = _process_candidate(
        monkeypatch, _passing_perf(settled_views=[50_000] * 9 + [500]))
    assert record is not None


def test_process_candidate_drops_a_stale_channel(monkeypatch):
    record, reason = _process_candidate(
        monkeypatch, _passing_perf(upload_dates=["2020-01-01T00:00:00Z"]))
    assert record is None
    assert reason == "stale_channel"
